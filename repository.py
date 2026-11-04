"""SQLite 持久层：schema、迁移、事务、单写入线程与在线备份。

设计要点（对应技术路线 §9）：
- 数据库放在 ``self.ctx.paths.data_dir / "affect.sqlite3"``，不碰主库。
- 数值统一以百分之一分（centi）整数保存：60 分存 6000，0.15 存 15。
- 启用 WAL、``busy_timeout`` 与 ``foreign_keys``。
- 写操作走 ``BEGIN IMMEDIATE`` 短事务；模型调用不进事务。
- 所有写入经单写入队列/专用线程完成，连接不在线程之间共享。
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from .models import (
    AffectEvent,
    DailyUserStats,
    EngineConfig,
    MoodState,
    UserRelationState,
)

T = TypeVar("T")

logger = logging.getLogger("plugin.local.whale_affect.repository")

SCHEMA_VERSION = 2
_SCHEMA_META_KEY = "schema_version"

_SCHEMA_STATEMENTS: List[str] = [
    """CREATE TABLE IF NOT EXISTS successful_sends (
        receipt_key TEXT PRIMARY KEY,
        user_key TEXT NOT NULL,
        sent_utc INTEGER NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_mood (
        bot_key TEXT PRIMARY KEY,
        local_date TEXT NOT NULL,
        mood_centi INTEGER NOT NULL,
        last_update_utc INTEGER NOT NULL,
        version INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_relation (
        user_key TEXT PRIMARY KEY,
        affinity_centi INTEGER NOT NULL,
        offset_centi INTEGER NOT NULL DEFAULT 0,
        offset_local_date TEXT NOT NULL DEFAULT '',
        offset_update_utc INTEGER NOT NULL DEFAULT 0,
        active_days INTEGER NOT NULL DEFAULT 0,
        valid_interactions INTEGER NOT NULL DEFAULT 0,
        last_valid_utc INTEGER NOT NULL DEFAULT 0,
        preferred_title TEXT NOT NULL DEFAULT '',
        title_opt_in INTEGER NOT NULL DEFAULT 0,
        title_enabled INTEGER NOT NULL DEFAULT 0,
        successful_reply_count INTEGER NOT NULL DEFAULT 0,
        last_title_reply_count INTEGER NOT NULL DEFAULT 0,
        last_attack_utc INTEGER NOT NULL DEFAULT 0,
        reaction TEXT NOT NULL DEFAULT 'none',
        reaction_expires_utc INTEGER NOT NULL DEFAULT 0,
        reaction_replies_left INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_user_stats (
        user_key TEXT NOT NULL,
        local_date TEXT NOT NULL,
        positive_f_used INTEGER NOT NULL DEFAULT 0,
        negative_f_used INTEGER NOT NULL DEFAULT 0,
        positive_m_used INTEGER NOT NULL DEFAULT 0,
        negative_m_used INTEGER NOT NULL DEFAULT 0,
        semantic_positive_count INTEGER NOT NULL DEFAULT 0,
        valid_count INTEGER NOT NULL DEFAULT 0,
        active_bonus_granted INTEGER NOT NULL DEFAULT 0,
        apology_repair_granted INTEGER NOT NULL DEFAULT 0,
        first_valid_utc INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_key, local_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        event_key TEXT PRIMARY KEY,
        user_key TEXT NOT NULL,
        bot_key TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        message_id TEXT NOT NULL DEFAULT '',
        event_time_utc INTEGER NOT NULL,
        received_time_utc INTEGER NOT NULL,
        local_date TEXT NOT NULL,
        status TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'neutral',
        confidence REAL NOT NULL DEFAULT 0,
        classifier TEXT NOT NULL DEFAULT '',
        reason_code TEXT NOT NULL DEFAULT '',
        text_hash TEXT NOT NULL DEFAULT '',
        score_before_json TEXT NOT NULL DEFAULT '{}',
        delta_json TEXT NOT NULL DEFAULT '{}',
        score_after_json TEXT NOT NULL DEFAULT '{}',
        config_version TEXT NOT NULL DEFAULT '',
        created_utc INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_user_time ON events (user_key, event_time_utc)",
    "CREATE INDEX IF NOT EXISTS idx_events_date ON events (local_date, status)",
    """
    CREATE TABLE IF NOT EXISTS cooldowns (
        user_key TEXT NOT NULL,
        bucket TEXT NOT NULL,
        last_event_utc INTEGER NOT NULL,
        text_hash TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_key, bucket)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_key TEXT NOT NULL,
        operation TEXT NOT NULL,
        target_key TEXT NOT NULL DEFAULT '',
        before_json TEXT NOT NULL DEFAULT '{}',
        after_json TEXT NOT NULL DEFAULT '{}',
        time_utc INTEGER NOT NULL
    )
    """,
]


class SchemaMismatchError(RuntimeError):
    """数据库 schema 版本高于当前插件支持版本时抛出。

    技术路线 §15：迁移失败必须禁用插件并清楚报错，不得自动重建覆盖已有数据库。
    """


# user_relation 的全部列，顺序与 _SCHEMA_STATEMENTS 中的建表语句一致。
# 管理写入整行 upsert 时复用，避免列名散落在多处。
_USER_RELATION_COLUMNS: List[str] = [
    "user_key",
    "affinity_centi",
    "offset_centi",
    "offset_local_date",
    "offset_update_utc",
    "active_days",
    "valid_interactions",
    "last_valid_utc",
    "preferred_title",
    "title_opt_in",
    "title_enabled",
    "successful_reply_count",
    "last_title_reply_count",
    "last_attack_utc",
    "reaction",
    "reaction_expires_utc",
    "reaction_replies_left",
    "version",
]


def _configure_connection(connection: sqlite3.Connection) -> None:
    """应用统一的 PRAGMA 设置。

    Args:
        connection: 已打开的 SQLite 连接。
    """

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=1000")
    connection.execute("PRAGMA synchronous=NORMAL")


def open_connection(db_path: Path) -> sqlite3.Connection:
    """打开并配置一个 SQLite 连接。

    Args:
        db_path: 数据库文件路径。

    Returns:
        sqlite3.Connection: 已配置好的连接；调用方负责关闭。
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    _configure_connection(connection)
    return connection


def read_schema_version(connection: sqlite3.Connection) -> Optional[int]:
    """读取当前数据库的 schema 版本。

    Args:
        connection: 已打开的连接。

    Returns:
        Optional[int]: 版本号；尚未初始化时返回 ``None``。
    """

    row = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
    if row is None:
        return None
    row = connection.execute("SELECT value FROM schema_meta WHERE key = ?", (_SCHEMA_META_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return int(str(row["value"]))
    except (TypeError, ValueError):
        return None


def migrate(connection: sqlite3.Connection) -> int:
    """创建或迁移数据库 schema。

    Args:
        connection: 由本线程创建并使用的连接。

    Returns:
        int: 迁移完成后的 schema 版本。

    Raises:
        SchemaMismatchError: 数据库版本高于当前插件支持的版本。
    """

    existing = read_schema_version(connection)
    if existing is not None and existing > SCHEMA_VERSION:
        raise SchemaMismatchError(
            f"数据库 schema 版本 {existing} 高于插件支持的 {SCHEMA_VERSION}；请升级插件或恢复备份，不会自动重建"
        )

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_SCHEMA_META_KEY, str(SCHEMA_VERSION)),
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            ("migrated_at_utc", str(int(time.time()))),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


class AffectRepository:
    """插件数据库访问层。

    默认使用专用写入线程：所有写操作通过队列串行执行，连接只在该线程内创建和使用。
    测试可以传入 ``single_threaded=True`` 直接在当前线程同步执行。
    """

    def __init__(self, db_path: Path, *, single_threaded: bool = False) -> None:
        """初始化仓储。

        Args:
            db_path: 数据库文件路径。
            single_threaded: 若为 ``True``，不走后台线程，所有操作在调用线程内同步执行。
        """

        self._db_path = Path(db_path)
        self._single_threaded = single_threaded
        self._queue: "queue.Queue[Optional[Callable[[sqlite3.Connection], Any]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._local_connection: Optional[sqlite3.Connection] = None
        self._closed = False
        self._error_count = 0
        self._lock = threading.Lock()
        # 写入线程内部再调用 run()/atomic() 时必须复用同一连接，否则会自我等待。
        self._nested = threading.local()

    # ──── 生命周期 ────────────────────────────────────────

    @staticmethod
    def delivery_key(bot_key: str, session_id: str, message_id: str) -> str:
        """Use unambiguous structured keys for persisted delivery deduplication."""
        return json.dumps([bot_key, session_id, message_id], ensure_ascii=False, separators=(",", ":"))

    def start(self) -> None:
        """初始化 schema 并按需启动写入线程。"""

        if self._single_threaded:
            if self._local_connection is None:
                self._local_connection = open_connection(self._db_path)
                migrate(self._local_connection)
            return

        if self._thread is not None:
            return
        ready = threading.Event()
        failure: Dict[str, BaseException] = {}

        def _worker() -> None:
            connection: Optional[sqlite3.Connection] = None
            try:
                connection = open_connection(self._db_path)
                migrate(connection)
            except BaseException as exc:  # noqa: BLE001 - 需要把启动失败传回调用线程
                failure["error"] = exc
                ready.set()
                return
            ready.set()
            while True:
                task = self._queue.get()
                if task is None:
                    self._queue.task_done()
                    break
                try:
                    self._nested.connection = connection
                    task(connection)
                except BaseException:  # noqa: BLE001 - 单任务失败不能杀死写入线程
                    with self._lock:
                        self._error_count += 1
                    logger.exception("whale-affect 数据库任务执行失败")
                finally:
                    self._nested.connection = None
                    self._queue.task_done()
            connection.close()

        self._thread = threading.Thread(target=_worker, name="whale-affect-db", daemon=True)
        self._thread.start()
        ready.wait(timeout=15.0)
        if "error" in failure:
            raise failure["error"]

    def close(self, *, drain_timeout: float = 5.0) -> None:
        """限时排空写入队列并释放连接。

        Args:
            drain_timeout: 等待队列排空的最长秒数。
        """

        if self._closed:
            return
        self._closed = True

        if self._single_threaded:
            if self._local_connection is not None:
                try:
                    self._local_connection.close()
                finally:
                    self._local_connection = None
            return

        thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + max(0.1, drain_timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        self._queue.put(None)
        thread.join(timeout=max(0.1, drain_timeout))

    @property
    def error_count(self) -> int:
        """返回写入任务失败累计次数。"""

        with self._lock:
            return self._error_count

    @property
    def db_path(self) -> Path:
        """返回数据库文件路径。"""

        return self._db_path

    # ──── 事务执行 ────────────────────────────────────────

    def run(self, task: Callable[[sqlite3.Connection], T], *, timeout: float = 10.0) -> T:
        """在写入线程内执行一个短事务任务。

        Args:
            task: 接收连接并返回结果的可调用对象；内部自行开闭事务。
            timeout: 等待结果的最长秒数。

        Returns:
            T: 任务返回值。

        Raises:
            TimeoutError: 等待超时。
            RuntimeError: 仓储已关闭。
        """

        if self._closed:
            raise RuntimeError("仓储已关闭")

        nested = getattr(self._nested, "connection", None)
        if nested is not None:
            return task(nested)

        if self._single_threaded:
            if self._local_connection is None:
                raise RuntimeError("仓储尚未初始化，请先调用 start()")
            return task(self._local_connection)

        holder: Dict[str, Any] = {}
        done = threading.Event()

        def _wrapper(connection: sqlite3.Connection) -> None:
            try:
                holder["result"] = task(connection)
            except BaseException as exc:  # noqa: BLE001 - 需要把异常传回调用线程
                holder["error"] = exc
            finally:
                done.set()

        self._queue.put(_wrapper)
        if not done.wait(timeout):
            raise TimeoutError("等待数据库写入超时")
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")  # type: ignore[return-value]

    # ──── 备份 ────────────────────────────────────────────

    def backup_to(self, target_path: Path) -> Path:
        """使用 SQLite backup API 做在线备份。

        Args:
            target_path: 备份目标文件路径。

        Returns:
            Path: 备份文件路径。
        """

        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()

        def _do(connection: sqlite3.Connection) -> Path:
            destination = sqlite3.connect(str(target_path))
            try:
                connection.backup(destination)
            finally:
                destination.close()
            return target_path

        return self.run(_do, timeout=30.0)

    # ──── 事务封装 ────────────────────────────────────────

    def atomic(self, task: Callable[[sqlite3.Connection], T], *, timeout: float = 10.0) -> T:
        """在单个 ``BEGIN IMMEDIATE`` 事务内执行任务，异常时回滚。

        Args:
            task: 接收连接并返回结果的可调用对象。
            timeout: 等待写入线程的最长秒数。

        Returns:
            T: 任务返回值。
        """

        def _do(connection: sqlite3.Connection) -> T:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = task(connection)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
            return result

        return self.run(_do, timeout=timeout)


class SqliteStateStorage:
    """``StateStorage`` 协议的 SQLite 实现。

    每次方法调用都通过 ``AffectRepository.run()`` 进入写入线程，连接不跨线程使用。
    ``engine.apply()`` 需要整体原子性时，请使用 :meth:`AffectRepository.atomic`
    包裹，而不是依赖本类逐条提交。
    """

    def __init__(self, repository: AffectRepository, config_provider: Callable[[], EngineConfig]) -> None:
        """初始化存储适配层。

        Args:
            repository: 插件仓储。
            config_provider: 返回当前引擎参数的工厂，用于新用户初始好感。
        """

        self._repository = repository
        self._config_provider = config_provider

    # ──── 事务作用域 ──────────────────────────────────────

    def run_atomic(self, task: Callable[[], T], *, timeout: float = 10.0) -> T:
        """在单个 ``BEGIN IMMEDIATE`` 事务内执行整段计分逻辑。

        事务体在写入线程中运行；期间本类其他存储方法会复用同一连接，
        不会再次入队，因此整段计分对其它连接原子可见。

        Args:
            task: 无参可调用对象，内部可自由调用本类存储方法。
            timeout: 等待写入线程的最长秒数。

        Returns:
            T: 任务返回值。
        """

        def _do(connection: sqlite3.Connection) -> T:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = task()
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
            return result

        return self._repository.run(_do, timeout=timeout)

    # ──── bot_mood ────────────────────────────────────────

    def load_mood(self, bot_key: str) -> Optional[MoodState]:
        """读取机器人心情状态。"""

        def _do(connection: sqlite3.Connection) -> Optional[MoodState]:
            row = connection.execute(
                "SELECT bot_key, local_date, mood_centi, last_update_utc, version FROM bot_mood WHERE bot_key = ?",
                (bot_key,),
            ).fetchone()
            if row is None:
                return None
            return MoodState(
                bot_key=row["bot_key"],
                local_date=row["local_date"],
                mood_centi=int(row["mood_centi"]),
                last_update_utc=int(row["last_update_utc"]),
                version=int(row["version"]),
            )

        return self._repository.run(_do)

    def save_mood(self, state: MoodState) -> None:
        """写入机器人心情状态。"""

        def _do(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO bot_mood (bot_key, local_date, mood_centi, last_update_utc, version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bot_key) DO UPDATE SET
                    local_date = excluded.local_date,
                    mood_centi = excluded.mood_centi,
                    last_update_utc = excluded.last_update_utc,
                    version = excluded.version
                """,
                (
                    state.bot_key,
                    state.local_date,
                    int(state.mood_centi),
                    int(state.last_update_utc),
                    int(state.version),
                ),
            )

        self._repository.run(_do)

    # ──── user_relation ───────────────────────────────────

    def load_user(self, user_key: str, config: Optional[EngineConfig] = None) -> UserRelationState:
        """读取用户关系状态；不存在时返回默认状态（未落库）。"""

        engine_config = config or self._config_provider()

        def _do(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return connection.execute(
                "SELECT * FROM user_relation WHERE user_key = ?",
                (user_key,),
            ).fetchone()

        row = self._repository.run(_do)
        if row is None:
            return UserRelationState(
                user_key=user_key,
                affinity_centi=engine_config.initial_affinity_centi,
            )
        return UserRelationState(
            user_key=row["user_key"],
            affinity_centi=int(row["affinity_centi"]),
            offset_centi=int(row["offset_centi"]),
            offset_local_date=row["offset_local_date"],
            offset_update_utc=int(row["offset_update_utc"]),
            active_days=int(row["active_days"]),
            valid_interactions=int(row["valid_interactions"]),
            last_valid_utc=int(row["last_valid_utc"]),
            preferred_title=row["preferred_title"],
            title_opt_in=bool(row["title_opt_in"]),
            title_enabled=bool(row["title_enabled"]),
            successful_reply_count=int(row["successful_reply_count"]),
            last_title_reply_count=int(row["last_title_reply_count"]),
            last_attack_utc=int(row["last_attack_utc"]),
            reaction=row["reaction"],
            reaction_expires_utc=int(row["reaction_expires_utc"]),
            reaction_replies_left=int(row["reaction_replies_left"]),
            version=int(row["version"]),
        )

    def save_user(self, state: UserRelationState) -> None:
        """写入用户关系状态。"""

        def _do(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO user_relation (
                    user_key, affinity_centi, offset_centi, offset_local_date, offset_update_utc,
                    active_days, valid_interactions, last_valid_utc, preferred_title,
                    title_opt_in, title_enabled, successful_reply_count, last_title_reply_count,
                    last_attack_utc, reaction, reaction_expires_utc, reaction_replies_left, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key) DO UPDATE SET
                    affinity_centi = excluded.affinity_centi,
                    offset_centi = excluded.offset_centi,
                    offset_local_date = excluded.offset_local_date,
                    offset_update_utc = excluded.offset_update_utc,
                    active_days = excluded.active_days,
                    valid_interactions = excluded.valid_interactions,
                    last_valid_utc = excluded.last_valid_utc,
                    preferred_title = excluded.preferred_title,
                    title_opt_in = excluded.title_opt_in,
                    title_enabled = excluded.title_enabled,
                    successful_reply_count = excluded.successful_reply_count,
                    last_title_reply_count = excluded.last_title_reply_count,
                    last_attack_utc = excluded.last_attack_utc,
                    reaction = excluded.reaction,
                    reaction_expires_utc = excluded.reaction_expires_utc,
                    reaction_replies_left = excluded.reaction_replies_left,
                    version = excluded.version
                """,
                (
                    state.user_key,
                    int(state.affinity_centi),
                    int(state.offset_centi),
                    state.offset_local_date,
                    int(state.offset_update_utc),
                    int(state.active_days),
                    int(state.valid_interactions),
                    int(state.last_valid_utc),
                    state.preferred_title,
                    1 if state.title_opt_in else 0,
                    1 if state.title_enabled else 0,
                    int(state.successful_reply_count),
                    int(state.last_title_reply_count),
                    int(state.last_attack_utc),
                    state.reaction,
                    int(state.reaction_expires_utc),
                    int(state.reaction_replies_left),
                    int(state.version),
                ),
            )

        self._repository.run(_do)

    # ──── daily_user_stats ────────────────────────────────

    def load_daily(self, user_key: str, local_date: str) -> DailyUserStats:
        """读取当日统计；不存在时返回零值（未落库）。"""

        def _do(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return connection.execute(
                "SELECT * FROM daily_user_stats WHERE user_key = ? AND local_date = ?",
                (user_key, local_date),
            ).fetchone()

        row = self._repository.run(_do)
        if row is None:
            return DailyUserStats(user_key=user_key, local_date=local_date)
        return DailyUserStats(
            user_key=row["user_key"],
            local_date=row["local_date"],
            positive_f_used=int(row["positive_f_used"]),
            negative_f_used=int(row["negative_f_used"]),
            positive_m_used=int(row["positive_m_used"]),
            negative_m_used=int(row["negative_m_used"]),
            semantic_positive_count=int(row["semantic_positive_count"]),
            valid_count=int(row["valid_count"]),
            active_bonus_granted=bool(row["active_bonus_granted"]),
            apology_repair_granted=bool(row["apology_repair_granted"]),
            first_valid_utc=int(row["first_valid_utc"]),
        )

    def save_daily(self, stats: DailyUserStats) -> None:
        """写入当日统计。"""

        def _do(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO daily_user_stats (
                    user_key, local_date, positive_f_used, negative_f_used,
                    positive_m_used, negative_m_used, semantic_positive_count, valid_count,
                    active_bonus_granted, apology_repair_granted, first_valid_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key, local_date) DO UPDATE SET
                    positive_f_used = excluded.positive_f_used,
                    negative_f_used = excluded.negative_f_used,
                    positive_m_used = excluded.positive_m_used,
                    negative_m_used = excluded.negative_m_used,
                    semantic_positive_count = excluded.semantic_positive_count,
                    valid_count = excluded.valid_count,
                    active_bonus_granted = excluded.active_bonus_granted,
                    apology_repair_granted = excluded.apology_repair_granted,
                    first_valid_utc = excluded.first_valid_utc
                """,
                (
                    stats.user_key,
                    stats.local_date,
                    int(stats.positive_f_used),
                    int(stats.negative_f_used),
                    int(stats.positive_m_used),
                    int(stats.negative_m_used),
                    int(stats.semantic_positive_count),
                    int(stats.valid_count),
                    1 if stats.active_bonus_granted else 0,
                    1 if stats.apology_repair_granted else 0,
                    int(stats.first_valid_utc),
                ),
            )

        self._repository.run(_do)

    # ──── events ──────────────────────────────────────────

    def expire_pending(self, now_utc: int, local_date: str, max_age: int) -> None:
        """Never replay expired semantic jobs after restart or across midnight."""
        self._repository.run(lambda connection: connection.execute(
            "UPDATE events SET status='dropped', reason_code='semantic_expired' "
            "WHERE status='pending' AND (event_time_utc < ? OR local_date != ?)",
            (now_utc - max_age, local_date),
        ))

    def reserve_pending(self, event: AffectEvent) -> bool:
        """Reserve a semantic event durably without applying any score twice."""
        def reserve(_connection):
            status = self.get_event_status(event.event_key)
            if status is not None:
                return status == "pending"
            self.write_event(event, status="pending", category="neutral", confidence=0,
                             classifier="llm", reason_code="semantic_pending",
                             score_before={}, delta={}, score_after={})
            return True
        return self._repository.atomic(reserve)

    def get_event_status(self, event_key: str) -> Optional[str]:
        """读取事件已落库状态。"""

        def _do(connection: sqlite3.Connection) -> Optional[str]:
            row = connection.execute(
                "SELECT status FROM events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            return None if row is None else str(row["status"])

        return self._repository.run(_do)

    def write_event(
        self,
        event: AffectEvent,
        *,
        status: str,
        category: str,
        confidence: float,
        classifier: str,
        reason_code: str,
        score_before: Dict[str, Any],
        delta: Dict[str, Any],
        score_after: Dict[str, Any],
    ) -> None:
        """写入或更新事件记录。"""

        created_utc = int(time.time())

        def _do(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO events (
                    event_key, user_key, bot_key, session_id, message_id,
                    event_time_utc, received_time_utc, local_date, status, category,
                    confidence, classifier, reason_code, text_hash,
                    score_before_json, delta_json, score_after_json, config_version, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    status = excluded.status,
                    category = excluded.category,
                    confidence = excluded.confidence,
                    classifier = excluded.classifier,
                    reason_code = excluded.reason_code,
                    score_before_json = excluded.score_before_json,
                    delta_json = excluded.delta_json,
                    score_after_json = excluded.score_after_json
                """,
                (
                    event.event_key,
                    event.user_key,
                    event.bot_key,
                    event.session_id,
                    event.message_id,
                    int(event.event_time_utc),
                    int(event.received_time_utc),
                    event.local_date,
                    status,
                    category,
                    float(confidence),
                    classifier,
                    reason_code,
                    event.text_hash,
                    json.dumps(score_before, ensure_ascii=False, sort_keys=True),
                    json.dumps(delta, ensure_ascii=False, sort_keys=True),
                    json.dumps(score_after, ensure_ascii=False, sort_keys=True),
                    event.config_version,
                    created_utc,
                ),
            )

        self._repository.run(_do)

    # ──── cooldowns ───────────────────────────────────────

    def get_cooldown(self, user_key: str, bucket: str) -> Optional[Tuple[int, str]]:
        """读取冷却记录。"""

        def _do(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return connection.execute(
                "SELECT last_event_utc, text_hash FROM cooldowns WHERE user_key = ? AND bucket = ?",
                (user_key, bucket),
            ).fetchone()

        row = self._repository.run(_do)
        if row is None:
            return None
        return int(row["last_event_utc"]), str(row["text_hash"])

    def set_cooldown(self, user_key: str, bucket: str, last_event_utc: int, text_hash: str) -> None:
        """写入冷却记录。"""

        def _do(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO cooldowns (user_key, bucket, last_event_utc, text_hash)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_key, bucket) DO UPDATE SET
                    last_event_utc = excluded.last_event_utc,
                    text_hash = excluded.text_hash
                """,
                (user_key, bucket, int(last_event_utc), text_hash or ""),
            )

        self._repository.run(_do)

    def has_unrepaired_attack(self, user_key: str, since_utc: int) -> bool:
        """判断指定时间以来是否存在未修复的攻击事件。

        判定规则：存在 ``category`` 为攻击类、``status='applied'`` 且其后再无
        任何 ``apology`` 已应用事件。
        """

        def _do(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return connection.execute(
                """
                SELECT 1
                FROM events
                WHERE user_key = ?
                  AND event_time_utc >= ?
                  AND status = 'applied'
                  AND category IN ('mild_attack', 'severe_attack')
                  AND event_time_utc > COALESCE(
                      (SELECT MAX(event_time_utc)
                       FROM events
                       WHERE user_key = ?
                         AND status = 'applied'
                         AND category = 'apology'),
                      0
                  )
                LIMIT 1
                """,
                (user_key, int(since_utc), user_key),
            ).fetchone()

        return self._repository.run(_do) is not None

    # ──── 管理查询（只读） ────────────────────────────────

    def load_daily_for_date(self, local_date: str) -> Dict[str, Dict[str, Any]]:
        """读取指定本地日期全部用户的当日统计。"""

        def _do(connection: sqlite3.Connection) -> List[sqlite3.Row]:
            return connection.execute(
                "SELECT * FROM daily_user_stats WHERE local_date = ?",
                (local_date,),
            ).fetchall()

        return {
            str(row["user_key"]): {
                "positive_f_used": int(row["positive_f_used"]),
                "negative_f_used": int(row["negative_f_used"]),
                "positive_m_used": int(row["positive_m_used"]),
                "negative_m_used": int(row["negative_m_used"]),
                "semantic_positive_count": int(row["semantic_positive_count"]),
                "valid_count": int(row["valid_count"]),
                "active_bonus_granted": bool(row["active_bonus_granted"]),
                "apology_repair_granted": bool(row["apology_repair_granted"]),
                "first_valid_utc": int(row["first_valid_utc"]),
            }
            for row in self._repository.run(_do)
        }

    def list_users(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        local_date: str,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """按好感度降序列出用户，并附带当日统计与最近事件时间。

        Args:
            limit: 本页返回条数。
            offset: 跳过条数。
            local_date: 用于关联当日统计的本地日期。

        Returns:
            Tuple[int, List[Dict[str, Any]]]: 总用户数与当前页数据。
        """

        def _do(connection: sqlite3.Connection) -> Tuple[int, List[sqlite3.Row]]:
            total_row = connection.execute("SELECT COUNT(*) AS total FROM user_relation").fetchone()
            rows = connection.execute(
                """
                SELECT
                    r.*,
                    d.valid_count AS today_valid_count,
                    d.positive_f_used AS today_positive_f,
                    d.negative_f_used AS today_negative_f,
                    d.semantic_positive_count AS today_semantic_positive,
                    (
                        SELECT MAX(event_time_utc) FROM events WHERE events.user_key = r.user_key
                    ) AS last_event_utc
                FROM user_relation AS r
                LEFT JOIN daily_user_stats AS d
                    ON d.user_key = r.user_key AND d.local_date = ?
                ORDER BY r.affinity_centi DESC, r.user_key ASC
                LIMIT ? OFFSET ?
                """,
                (local_date, int(limit), int(offset)),
            ).fetchall()
            return (0 if total_row is None else int(total_row["total"])), rows

        total, rows = self._repository.run(_do)
        return total, [
            {
                "user_key": str(row["user_key"]),
                "affinity_centi": int(row["affinity_centi"]),
                "offset_centi": int(row["offset_centi"]),
                "active_days": int(row["active_days"]),
                "valid_interactions": int(row["valid_interactions"]),
                "last_valid_utc": int(row["last_valid_utc"]),
                "preferred_title": str(row["preferred_title"] or ""),
                "title_opt_in": bool(row["title_opt_in"]),
                "title_enabled": bool(row["title_enabled"]),
                "successful_reply_count": int(row["successful_reply_count"]),
                "last_attack_utc": int(row["last_attack_utc"]),
                "reaction": str(row["reaction"] or "none"),
                "today_valid_count": 0 if row["today_valid_count"] is None else int(row["today_valid_count"]),
                "today_positive_f": 0 if row["today_positive_f"] is None else int(row["today_positive_f"]),
                "today_negative_f": 0 if row["today_negative_f"] is None else int(row["today_negative_f"]),
                "today_semantic_positive": (
                    0 if row["today_semantic_positive"] is None else int(row["today_semantic_positive"])
                ),
                "last_event_utc": 0 if row["last_event_utc"] is None else int(row["last_event_utc"]),
            }
            for row in rows
        ]

    def recent_events(self, *, user_key: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """按时间倒序读取最近事件；``user_key`` 为空时跨用户读取。

        ``delta_json`` / ``score_*_json`` 在 Python 侧解析，不依赖 SQLite JSON 扩展。
        """

        def _do(connection: sqlite3.Connection) -> List[sqlite3.Row]:
            if user_key:
                return connection.execute(
                    """
                    SELECT * FROM events WHERE user_key = ?
                    ORDER BY event_time_utc DESC, created_utc DESC, event_key ASC
                    LIMIT ?
                    """,
                    (user_key, int(limit)),
                ).fetchall()
            return connection.execute(
                """
                SELECT * FROM events
                ORDER BY event_time_utc DESC, created_utc DESC, event_key ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

        return [_describe_event_row(row) for row in self._repository.run(_do)]

    def count_events_by_status(self, local_date: str) -> Dict[str, int]:
        """统计指定本地日期各状态事件数量。"""

        def _do(connection: sqlite3.Connection) -> List[sqlite3.Row]:
            return connection.execute(
                "SELECT status, COUNT(*) AS total FROM events WHERE local_date = ? GROUP BY status",
                (local_date,),
            ).fetchall()

        return {str(row["status"]): int(row["total"]) for row in self._repository.run(_do)}

    def count_suppressed_today(self, local_date: str) -> int:
        """统计指定本地日期被防刷/预算规则部分抑制的已计分事件数。

        判定依据是 ``delta_json`` 里的 ``suppressed`` 数组非空；数组为空说明
        该事件全部计分项都已生效。
        """

        def _do(connection: sqlite3.Connection) -> List[str]:
            rows = connection.execute(
                "SELECT delta_json FROM events WHERE local_date = ? AND status = 'applied'",
                (local_date,),
            ).fetchall()
            return [str(row["delta_json"] or "") for row in rows]

        total = 0
        for raw in self._repository.run(_do):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("suppressed"):
                total += 1
        return total

    def daily_aggregate(self, local_date: str) -> Dict[str, Any]:
        """汇总指定本地日期的全局数据，供管理页统计卡片使用。"""

        def _do(connection: sqlite3.Connection) -> Dict[str, Any]:
            events_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_events,
                    COUNT(DISTINCT user_key) AS active_users,
                    SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) AS applied_events,
                    SUM(CASE WHEN category IN ('mild_attack', 'severe_attack') THEN 1 ELSE 0 END) AS attack_events
                FROM events WHERE local_date = ?
                """,
                (local_date,),
            ).fetchone()
            stats_row = connection.execute(
                """
                SELECT
                    SUM(positive_f_used) AS positive_f,
                    SUM(negative_f_used) AS negative_f,
                    SUM(valid_count) AS valid_count,
                    SUM(active_bonus_granted) AS active_bonus_count
                FROM daily_user_stats WHERE local_date = ?
                """,
                (local_date,),
            ).fetchone()
            audit_row = connection.execute("SELECT COUNT(*) AS total FROM admin_audit").fetchone()
            return {
                "total_events": 0 if events_row is None else int(events_row["total_events"] or 0),
                "applied_events": 0 if events_row is None else int(events_row["applied_events"] or 0),
                "attack_events": 0 if events_row is None else int(events_row["attack_events"] or 0),
                "active_users": 0 if events_row is None else int(events_row["active_users"] or 0),
                "positive_f": 0 if stats_row is None else int(stats_row["positive_f"] or 0),
                "negative_f": 0 if stats_row is None else int(stats_row["negative_f"] or 0),
                "valid_count": 0 if stats_row is None else int(stats_row["valid_count"] or 0),
                "active_bonus_count": 0 if stats_row is None else int(stats_row["active_bonus_count"] or 0),
                "audit_total": 0 if audit_row is None else int(audit_row["total"] or 0),
            }

        return self._repository.run(_do)

    # ──── 管理写入 ────────────────────────────────────────

    def find_user_keys(self, user_id: str) -> List[str]:
        """按 QQ 号查找所有匹配的用户身份键。

        身份键最后一段是 ``user_id``，因此可以用后缀精确匹配，
        不需要管理员知道 platform / bot / scope 前缀。
        """

        needle = str(user_id or "").strip()
        if not needle:
            return []

        def _do(connection: sqlite3.Connection) -> List[str]:
            rows = connection.execute(
                "SELECT user_key FROM user_relation ORDER BY affinity_centi DESC, user_key ASC",
            ).fetchall()
            return [str(row["user_key"]) for row in rows]

        matched: List[str] = []
        for key in self._repository.run(_do):
            parts = key.split("\x1f")
            if len(parts) == 4 and parts[3] == needle:
                matched.append(key)
        return matched

    def mutate_user(
        self,
        user_key: str,
        mutator: Callable[[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """在单个事务内读改写一名用户的关系状态。

        ``mutator`` 收到当前行（不存在时为 ``None``），必须返回待写入的行字典
        或 ``None`` 表示不写入。返回值为 ``{"before": ..., "after": ...}``，
        供审计直接记录。

        Args:
            user_key: 用户身份键。
            mutator: 读改写回调。

        Returns:
            Dict[str, Any]: 修改前后状态。
        """

        def _task(connection: sqlite3.Connection) -> Dict[str, Any]:
            row = connection.execute("SELECT * FROM user_relation WHERE user_key = ?", (user_key,)).fetchone()
            before: Optional[Dict[str, Any]] = None if row is None else dict(row)
            target = mutator(before)
            if target is None:
                return {"before": before or {}, "after": before or {}}

            target = dict(target)
            target["user_key"] = user_key
            columns = _USER_RELATION_COLUMNS
            values = []
            for column in columns:
                value = target.get(column)
                if isinstance(value, bool):
                    value = 1 if value else 0
                values.append(value)
            updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "user_key")
            connection.execute(
                f"""
                INSERT INTO user_relation ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                ON CONFLICT(user_key) DO UPDATE SET {updates}
                """,
                values,
            )
            saved = connection.execute("SELECT * FROM user_relation WHERE user_key = ?", (user_key,)).fetchone()
            return {"before": before or {}, "after": {} if saved is None else dict(saved)}

        return self._repository.atomic(_task)

    def delete_user(self, user_key: str) -> Dict[str, int]:
        """删除单个用户的全部插件状态，返回各表删除行数。

        只清理该用户自己的记录；``bot_mood`` 是全局状态，不在清理范围内。
        整个删除在单个 ``BEGIN IMMEDIATE`` 事务内完成。
        """

        def _task(connection: sqlite3.Connection) -> Dict[str, int]:
            counts: Dict[str, int] = {}
            for table in ("successful_sends", "events", "daily_user_stats", "cooldowns", "user_relation"):
                cursor = connection.execute(f"DELETE FROM {table} WHERE user_key = ?", (user_key,))
                counts[table] = int(cursor.rowcount or 0)
            return counts

        return self._repository.atomic(_task)

    def write_audit(
        self,
        *,
        actor_key: str,
        operation: str,
        target_key: str = "",
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
    ) -> int:
        """追加一行管理审计记录，返回自增主键。"""

        time_utc = int(time.time())

        def _do(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                """
                INSERT INTO admin_audit (actor_key, operation, target_key, before_json, after_json, time_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_key,
                    operation,
                    target_key or "",
                    json.dumps(before or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(after or {}, ensure_ascii=False, sort_keys=True),
                    time_utc,
                ),
            )
            return int(cursor.lastrowid or 0)

        return self._repository.run(_do)

    def list_audit(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """按时间倒序读取管理审计记录。"""

        def _do(connection: sqlite3.Connection) -> List[sqlite3.Row]:
            return connection.execute(
                "SELECT * FROM admin_audit ORDER BY time_utc DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()

        result: List[Dict[str, Any]] = []
        for row in self._repository.run(_do):
            result.append(
                {
                    "id": int(row["id"]),
                    "actor_key": str(row["actor_key"] or ""),
                    "operation": str(row["operation"] or ""),
                    "target_key": str(row["target_key"] or ""),
                    "before": _loads_object(row["before_json"]),
                    "after": _loads_object(row["after_json"]),
                    "time_utc": int(row["time_utc"]),
                }
            )
        return result


def _loads_object(raw: Any) -> Dict[str, Any]:
    """把数据库里的 JSON 文本还原为字典；失败时返回空字典。"""

    if not raw:
        return {}
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _describe_event_row(row: sqlite3.Row) -> Dict[str, Any]:
    """把事件行转换为便于展示与审计的字典。"""

    return {
        "event_key": str(row["event_key"]),
        "user_key": str(row["user_key"]),
        "bot_key": str(row["bot_key"]),
        "session_id": str(row["session_id"] or ""),
        "message_id": str(row["message_id"] or ""),
        "event_time_utc": int(row["event_time_utc"]),
        "local_date": str(row["local_date"]),
        "status": str(row["status"]),
        "category": str(row["category"] or "neutral"),
        "confidence": float(row["confidence"] or 0.0),
        "classifier": str(row["classifier"] or ""),
        "reason_code": str(row["reason_code"] or ""),
        "score_before": _loads_object(row["score_before_json"]),
        "delta": _loads_object(row["delta_json"]),
        "score_after": _loads_object(row["score_after_json"]),
    }


def create_storage(repository: AffectRepository, config_provider: Callable[[], EngineConfig]) -> SqliteStateStorage:
    """构造引擎所需的 SQLite 存储实现。

    Args:
        repository: 插件仓储。
        config_provider: 返回当前引擎参数的工厂。

    Returns:
        SqliteStateStorage: 存储实现。
    """

    return SqliteStateStorage(repository, config_provider)
