"""状态引擎的领域规则测试。

覆盖技术路线 §14 中可在纯逻辑层验证的编号：
T01、T02/T04（reaction）、T10-T15、T18-T20、T26/T27（称呼滞回）、T30、T31。

所有时间都由注入的固定时间戳驱动，不依赖真实时钟。
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import PACKAGE_NAME  # noqa: E402

config = __import__(f"{PACKAGE_NAME}.config", fromlist=["config"])
engine_module = __import__(f"{PACKAGE_NAME}.engine", fromlist=["engine"])
host_adapter = __import__(f"{PACKAGE_NAME}.host_adapter", fromlist=["host_adapter"])
repository_module = __import__(f"{PACKAGE_NAME}.repository", fromlist=["repository"])
models_module = __import__(f"{PACKAGE_NAME}.models", fromlist=["models"])

AffectEvent = models_module.AffectEvent
Classification = models_module.Classification
EventCategory = models_module.EventCategory
Reaction = models_module.Reaction
Target = models_module.Target
UserRelationState = models_module.UserRelationState
affinity_level = models_module.affinity_level
mood_level = models_module.mood_level

# 2026-01-01 12:00:00 +08:00
BASE_NOW = int(datetime(2026, 1, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp())

BOT_KEY = host_adapter.build_bot_key("qq", "10000")
USER_KEY = "qq\x1f10000\x1f\x1f20000"
OTHER_USER_KEY = "qq\x1f10000\x1f\x1f30000"


def build_engine_config() -> "engine_module.EngineConfig":
    """按默认插件配置构造引擎参数。"""

    cfg = config.WhaleAffectConfig()
    return engine_module.build_engine_config(
        mood=cfg.mood,
        affinity=cfg.affinity,
        reactions=cfg.reactions,
        scoring=cfg.scoring,
        classifier=cfg.classifier,
    )


class EngineTestCase(unittest.TestCase):
    """使用临时 SQLite 数据库验证引擎规则。"""

    def setUp(self) -> None:
        """准备临时数据库与引擎。"""

        self.tmpdir = Path(tempfile.mkdtemp(prefix="whale-affect-test-"))
        self.engine_config = build_engine_config()
        self.repository = repository_module.AffectRepository(
            self.tmpdir / "affect.sqlite3",
            single_threaded=True,
        )
        self.repository.start()
        self.storage = repository_module.create_storage(self.repository, lambda: self.engine_config)
        self.clock = engine_module.LocalClock("Asia/Shanghai")
        self.engine = engine_module.AffectEngine(self.storage, self.engine_config, self.clock)
        self._seq = 0

    def tearDown(self) -> None:
        """关闭数据库并清理临时目录。"""

        self.repository.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_event(
        self,
        category: str,
        *,
        now: int = BASE_NOW,
        user_key: str = USER_KEY,
        confidence: float = 0.95,
        target: str = Target.BOT.value,
        text: str = "",
        is_group: bool = False,
        message_id: str = "",
    ) -> AffectEvent:
        """构造一条待计分事件。"""

        self._seq += 1
        resolved_text = text or f"{category}-{self._seq}-{now}"
        resolved_id = message_id or f"m{self._seq}"
        return AffectEvent(
            event_key=f"evt-{resolved_id}",
            user_key=user_key,
            bot_key=BOT_KEY,
            session_id="session",
            message_id=resolved_id,
            event_time_utc=now,
            received_time_utc=now,
            local_date=self.clock.local_date(now),
            classification=Classification(
                target=target,
                event=category,
                confidence=confidence,
                reason_code="test",
            ),
            text_hash=host_adapter.text_hash(resolved_text),
            is_group=is_group,
        )

    def snapshot(self, *, now: int = BASE_NOW, user_key: str = USER_KEY, scene: str = "private") -> object:
        """读取当前快照。"""

        return self.engine.build_snapshot(user_key, BOT_KEY, scene, now)

    # ──── T01 ────────────────────────────────────────────

    @staticmethod
    def jealousy_id(minimum: float = 0.0, maximum: float = 0.25, prefix: str = "j") -> str:
        for i in range(10000):
            mid = f"{prefix}-{i}"
            draw = int.from_bytes(hashlib.sha256(f"evt-{mid}".encode()).digest()[:8], "big") / 2**64
            if minimum <= draw < maximum:
                return mid
        raise AssertionError("no matching deterministic event")

    def test_jealousy_triggers_only_sometimes_and_cools_down(self) -> None:
        self.storage.save_user(UserRelationState(user_key=USER_KEY, affinity_centi=5000))
        mid=self.jealousy_id()
        first=self.engine.apply(self.make_event(EventCategory.JEALOUSY_CANDIDATE.value,message_id=mid),BASE_NOW)
        self.assertEqual(first.snapshot.reaction,Reaction.JEALOUS.value)
        self.assertEqual(first.delta_mood_centi,0)
        self.assertEqual(first.delta_offset_centi,-150)
        self.assertGreaterEqual(first.delta_affinity_centi,0)
        second=self.engine.apply(self.make_event(EventCategory.JEALOUSY_CANDIDATE.value,now=BASE_NOW+60,
                                  message_id=self.jealousy_id(prefix="another")),BASE_NOW+60)
        self.assertIn("jealousy_not_selected",second.suppressed)
        self.assertEqual(second.delta_offset_centi,0)
        self.assertEqual(second.snapshot.reaction,Reaction.JEALOUS.value)

    def test_jealousy_probability_increases_and_low_affinity_never_triggers(self) -> None:
        mid=self.jealousy_id(.40,.55)
        self.storage.save_user(UserRelationState(user_key=USER_KEY,affinity_centi=9000))
        high=self.engine.apply(self.make_event(EventCategory.JEALOUSY_CANDIDATE.value,message_id=mid),BASE_NOW)
        self.assertEqual(high.snapshot.reaction,Reaction.JEALOUS.value)
        other=self.jealousy_id(prefix="low")
        low=self.engine.apply(self.make_event(EventCategory.JEALOUSY_CANDIDATE.value,user_key=OTHER_USER_KEY,message_id=other),BASE_NOW)
        self.assertEqual(low.snapshot.reaction,Reaction.NONE.value)
        self.assertEqual(low.delta_offset_centi,0)

    def test_jealousy_never_exposes_group_and_comfort_recovers(self) -> None:
        self.storage.save_user(UserRelationState(user_key=USER_KEY,affinity_centi=5000))
        group=self.engine.apply(self.make_event(EventCategory.JEALOUSY_CANDIDATE.value,is_group=True,
                                  message_id=self.jealousy_id(prefix="group")),BASE_NOW)
        self.assertEqual(group.snapshot.reaction,Reaction.NONE.value)
        moment=self.engine.apply(self.make_event(EventCategory.JEALOUSY_CANDIDATE.value,now=BASE_NOW+1,
                                   message_id=self.jealousy_id(prefix="private")),BASE_NOW+1)
        self.assertEqual(moment.snapshot.reaction,Reaction.JEALOUS.value)
        soothed=self.engine.apply(self.make_event(EventCategory.COMFORT.value,now=BASE_NOW+2),BASE_NOW+2)
        self.assertEqual(soothed.snapshot.reaction,Reaction.SOOTHED.value)
        self.assertGreaterEqual(soothed.snapshot.offset_centi,0)

    def test_jealousy_does_not_override_actual_hurt(self) -> None:
        self.storage.save_user(UserRelationState(user_key=USER_KEY,affinity_centi=9000))
        self.engine.apply(self.make_event(EventCategory.SEVERE_ATTACK.value),BASE_NOW)
        result=self.engine.apply(self.make_event(EventCategory.JEALOUSY_CANDIDATE.value,now=BASE_NOW+5,
                                 message_id=self.jealousy_id()),BASE_NOW+5)
        self.assertEqual(result.snapshot.reaction,Reaction.HURT.value)
        self.assertEqual(result.delta_offset_centi,0)

    def test_t01_new_user_defaults(self) -> None:
        """新用户 F=15、D=0、M 当日 60，且不同用户不串。"""

        snap = self.snapshot()
        self.assertEqual(snap.mood_centi, 6000)
        self.assertEqual(snap.offset_centi, 0)
        self.assertEqual(snap.affinity_centi, 1500)

        self.engine.apply(self.make_event(EventCategory.PRAISE.value), BASE_NOW)
        other = self.snapshot(user_key=OTHER_USER_KEY)
        self.assertEqual(other.affinity_centi, 1500)
        self.assertGreater(self.snapshot().affinity_centi, 1500)

    # ──── 去重与防刷 ─────────────────────────────────────

    def test_t10_replay_same_event_only_once(self) -> None:
        """同一消息重放 3 次只更新一次。"""

        event = self.make_event(EventCategory.PRAISE.value, message_id="dup")
        first = self.engine.apply(event, BASE_NOW)
        self.assertEqual(first.status, "applied")
        affinity_after_first = self.snapshot().affinity_centi

        for offset in (1, 2, 3):
            replay = self.engine.apply(event, BASE_NOW + offset)
            self.assertEqual(replay.status, "duplicate")
        self.assertEqual(self.snapshot().affinity_centi, affinity_after_first)

    def test_t11_same_text_different_message_id_is_cooled(self) -> None:
        """换消息 ID 但文本相同会触发文本冷却。"""

        self.engine.apply(self.make_event(EventCategory.PRAISE.value, text="你好可爱"), BASE_NOW)
        second = self.engine.apply(
            self.make_event(EventCategory.PRAISE.value, text="你好可爱", now=BASE_NOW + 60),
            BASE_NOW + 60,
        )
        self.assertEqual(second.status, "neutral")
        self.assertIn("duplicate_text", second.suppressed)

    def test_t12_positive_daily_caps(self) -> None:
        """每天大量夸奖：正向 F <=4，语义奖励次数 <=3。"""

        now = BASE_NOW
        for index in range(20):
            now = BASE_NOW + index * 700
            self.engine.apply(
                self.make_event(EventCategory.PRAISE.value, now=now, text=f"夸你第{index}句"),
                now,
            )
        user = self.storage.load_user(USER_KEY, self.engine_config)
        gained = user.affinity_centi - self.engine_config.initial_affinity_centi
        self.assertLessEqual(gained, self.engine_config.daily_positive_cap_centi)

        local_date = self.clock.local_date(now)
        daily = self.storage.load_daily(USER_KEY, local_date)
        self.assertLessEqual(
            daily.semantic_positive_count,
            self.engine_config.positive_semantic_daily_count_cap,
        )

    def test_t13_negative_daily_caps(self) -> None:
        """每天大量辱骂：负向 F 绝对值 <=8，M 的该用户负贡献 <=6。"""

        now = BASE_NOW
        for index in range(12):
            now = BASE_NOW + index * 120
            self.engine.apply(
                self.make_event(
                    EventCategory.SEVERE_ATTACK.value,
                    now=now,
                    text=f"难听话第{index}句",
                ),
                now,
            )
        user = self.storage.load_user(USER_KEY, self.engine_config)
        lost = self.engine_config.initial_affinity_centi - user.affinity_centi
        self.assertLessEqual(lost, self.engine_config.daily_negative_cap_centi)

        local_date = self.clock.local_date(now)
        daily = self.storage.load_daily(USER_KEY, local_date)
        self.assertLessEqual(daily.negative_m_used, self.engine_config.negative_m_cap_centi)

    def test_t14_two_users_are_isolated(self) -> None:
        """两人分别夸/骂：各自 F/D 独立，共享 M。"""

        self.engine.apply(self.make_event(EventCategory.PRAISE.value, user_key=USER_KEY), BASE_NOW)
        self.engine.apply(
            self.make_event(EventCategory.SEVERE_ATTACK.value, user_key=OTHER_USER_KEY, now=BASE_NOW + 5),
            BASE_NOW + 5,
        )
        good = self.snapshot()
        bad = self.snapshot(now=BASE_NOW + 5, user_key=OTHER_USER_KEY)
        self.assertGreater(good.affinity_centi, 1500)
        self.assertLess(bad.affinity_centi, 1500)
        self.assertGreater(good.offset_centi, 0)
        self.assertLess(bad.offset_centi, 0)
        # M 是共享的：一人夸、一人骂后不会回到 6000。
        self.assertNotEqual(good.mood_centi, 6000)

    def test_t15_same_user_shares_affinity_across_scenes(self) -> None:
        """同一人在私聊与群里共享同一份 F。"""

        self.engine.apply(self.make_event(EventCategory.PRAISE.value), BASE_NOW)
        after_private = self.snapshot().affinity_centi
        after_group = self.snapshot(scene="group").affinity_centi
        self.assertEqual(after_private, after_group)

    # ──── 跨日与衰减 ─────────────────────────────────────

    def test_t18_midnight_resets_mood_and_offset(self) -> None:
        """23:59→00:00：M/D 按新日重置一次，F 保留。"""

        late = int(datetime(2026, 1, 1, 15, 59, 0, tzinfo=timezone.utc).timestamp())  # 23:59 +08
        self.engine.apply(self.make_event(EventCategory.PRAISE.value, now=late), late)
        before = self.snapshot(now=late)
        self.assertEqual(before.local_date, "2026-01-01")

        after_midnight = late + 120
        snapshot = self.snapshot(now=after_midnight)
        self.assertEqual(snapshot.local_date, "2026-01-02")
        self.assertEqual(snapshot.mood_centi, 6000)
        self.assertEqual(snapshot.offset_centi, 0)
        self.assertEqual(snapshot.affinity_centi, before.affinity_centi)

    def test_t18_stale_event_is_dropped(self) -> None:
        """旧事件跨日不补扣分。"""

        stale = self.make_event(EventCategory.SEVERE_ATTACK.value, now=BASE_NOW - 3600)
        result = self.engine.apply(stale, BASE_NOW)
        self.assertEqual(result.status, "dropped")

    def test_t19_restart_keeps_affinity_only(self) -> None:
        """停机跨两天再启动：M=60、F 保留。"""

        self.engine.apply(self.make_event(EventCategory.PRAISE.value), BASE_NOW)
        affinity = self.snapshot().affinity_centi

        self.repository.close()
        restarted = repository_module.AffectRepository(
            self.tmpdir / "affect.sqlite3",
            single_threaded=True,
        )
        restarted.start()
        try:
            storage = repository_module.create_storage(restarted, lambda: self.engine_config)
            engine = engine_module.AffectEngine(storage, self.engine_config, self.clock)
            later = BASE_NOW + 2 * 86400
            snapshot = engine.build_snapshot(USER_KEY, BOT_KEY, "private", later)
            self.assertEqual(snapshot.mood_centi, 6000)
            self.assertEqual(snapshot.offset_centi, 0)
            self.assertEqual(snapshot.affinity_centi, affinity)
        finally:
            restarted.close()

    # ──── reaction ───────────────────────────────────────

    def test_affectionate_tease_does_not_require_pouting(self) -> None:
        """昵称不启动持久赌气；轻松接梗由回复模型按语境选择。"""

        before = self.snapshot().affinity_centi
        result = self.engine.apply(self.make_event(EventCategory.AFFECTIONATE_TEASE.value), BASE_NOW)
        self.assertEqual(result.snapshot.reaction, Reaction.NONE.value)
        self.assertEqual(result.delta_mood_centi, 0)
        self.assertEqual(result.delta_offset_centi, 0)
        # 唯一增量来自有效交流 +0.15，不来自语义表。
        self.assertEqual(
            result.snapshot.affinity_centi - before,
            self.engine_config.valid_chat_gain_centi,
        )

    def test_comfort_clears_hurt(self) -> None:
        """安慰可以提前清除 hurt。"""

        self.engine.apply(self.make_event(EventCategory.MILD_ATTACK.value), BASE_NOW)
        self.assertEqual(self.snapshot().reaction, Reaction.HURT.value)
        result = self.engine.apply(
            self.make_event(EventCategory.COMFORT.value, now=BASE_NOW + 30),
            BASE_NOW + 30,
        )
        self.assertEqual(result.snapshot.reaction, Reaction.SOOTHED.value)

    def test_nickname_cannot_override_hurt(self) -> None:
        """再喊昵称不能覆盖真实受伤。"""

        self.engine.apply(self.make_event(EventCategory.SEVERE_ATTACK.value), BASE_NOW)
        result = self.engine.apply(
            self.make_event(EventCategory.AFFECTIONATE_TEASE.value, now=BASE_NOW + 30),
            BASE_NOW + 30,
        )
        self.assertEqual(result.snapshot.reaction, Reaction.HURT.value)

    def test_reaction_expires_by_ttl(self) -> None:
        """reaction 超过 TTL 后自动过期。"""

        self.engine.apply(self.make_event(EventCategory.MILD_ATTACK.value), BASE_NOW)
        expired = self.snapshot(now=BASE_NOW + self.engine_config.hurt_ttl_seconds + 5)
        self.assertEqual(expired.reaction, Reaction.NONE.value)

    def test_reply_consumes_reaction_quota(self) -> None:
        """成功回复会消耗 reaction 的剩余次数。"""

        self.engine.apply(self.make_event(EventCategory.COMFORT.value), BASE_NOW)
        for index in range(self.engine_config.soothed_max_replies):
            self.engine.record_successful_reply(USER_KEY, BOT_KEY, BASE_NOW + index)
        self.assertEqual(self.snapshot(now=BASE_NOW + 5).reaction, Reaction.NONE.value)

    def test_comfort_only_restores_offset_to_zero(self) -> None:
        """comfort 的 D 只在 D<0 时最多恢复到 0。"""

        self.engine.apply(self.make_event(EventCategory.MILD_ATTACK.value), BASE_NOW)
        negative = self.snapshot(now=BASE_NOW + 1).offset_centi
        self.assertLess(negative, 0)

        self.engine.apply(
            self.make_event(EventCategory.COMFORT.value, now=BASE_NOW + 2),
            BASE_NOW + 2,
        )
        recovered = self.snapshot(now=BASE_NOW + 3).offset_centi
        self.assertGreater(recovered, negative)
        self.assertLessEqual(recovered, 0)

    # ──── 称呼 ───────────────────────────────────────────

    def test_t26_title_requires_all_conditions(self) -> None:
        """F 高但条件不足时不使用称呼；全部满足才允许。"""

        cfg = self.engine_config
        user = self.storage.load_user(USER_KEY, cfg)
        rich = UserRelationState(
            user_key=user.user_key,
            affinity_centi=9000,
            active_days=1,
            valid_interactions=5,
            preferred_title="主人",
            title_opt_in=True,
        )
        self.storage.save_user(rich)
        allowed, should_use = self.engine.compute_title_state(rich, 7000, False)
        self.assertFalse(allowed)
        self.assertFalse(should_use)

        qualified = UserRelationState(
            user_key=user.user_key,
            affinity_centi=9000,
            active_days=cfg.title_required_active_days,
            valid_interactions=cfg.title_required_interactions,
            preferred_title="主人",
            title_opt_in=True,
            successful_reply_count=10,
            last_title_reply_count=0,
        )
        self.storage.save_user(qualified)
        allowed, should_use = self.engine.compute_title_state(qualified, 7000, False)
        self.assertTrue(allowed)
        self.assertTrue(should_use)

    def test_t26_title_not_used_in_groups(self) -> None:
        """群聊默认不使用称呼。"""

        cfg = self.engine_config
        qualified = UserRelationState(
            user_key=USER_KEY,
            affinity_centi=9000,
            active_days=cfg.title_required_active_days,
            valid_interactions=cfg.title_required_interactions,
            preferred_title="主人",
            title_opt_in=True,
            successful_reply_count=10,
        )
        allowed, should_use = self.engine.compute_title_state(qualified, 7000, True)
        self.assertFalse(allowed)
        self.assertFalse(should_use)

    def test_t27_title_blocked_when_mood_low_or_hurt(self) -> None:
        """低心情或受伤时不使用称呼。"""

        cfg = self.engine_config
        qualified = UserRelationState(
            user_key=USER_KEY,
            affinity_centi=9000,
            active_days=cfg.title_required_active_days,
            valid_interactions=cfg.title_required_interactions,
            preferred_title="主人",
            title_opt_in=True,
            successful_reply_count=10,
        )
        allowed, should_use = self.engine.compute_title_state(qualified, 3000, False)
        self.assertFalse(should_use)

        hurt = UserRelationState(
            user_key=USER_KEY,
            affinity_centi=9000,
            active_days=cfg.title_required_active_days,
            valid_interactions=cfg.title_required_interactions,
            preferred_title="主人",
            title_opt_in=True,
            successful_reply_count=10,
            reaction=Reaction.HURT.value,
            reaction_expires_utc=BASE_NOW + 600,
        )
        allowed, should_use = self.engine.compute_title_state(hurt, 7000, False)
        self.assertFalse(should_use)

    def test_t27_title_hysteresis(self) -> None:
        """F 跌破 80 停用，回升到 85 才重新启用。"""

        cfg = self.engine_config
        base = UserRelationState(
            user_key=USER_KEY,
            affinity_centi=8600,
            active_days=cfg.title_required_active_days,
            valid_interactions=cfg.title_required_interactions,
            preferred_title="主人",
            title_opt_in=True,
            title_enabled=True,
            successful_reply_count=10,
        )
        allowed, _ = self.engine.compute_title_state(base, 7000, False)
        self.assertTrue(allowed)

        below_exit = UserRelationState(**{**base.__dict__, "affinity_centi": 7900})
        allowed, _ = self.engine.compute_title_state(below_exit, 7000, False)
        self.assertFalse(allowed)

        between = UserRelationState(**{**base.__dict__, "affinity_centi": 8200, "title_enabled": False})
        allowed, _ = self.engine.compute_title_state(between, 7000, False)
        self.assertFalse(allowed)

        re_entered = UserRelationState(**{**base.__dict__, "affinity_centi": 8500, "title_enabled": False})
        allowed, _ = self.engine.compute_title_state(re_entered, 7000, False)
        self.assertTrue(allowed)

    def test_title_respects_reply_interval(self) -> None:
        """每 N 次回复最多使用一次称呼。"""

        cfg = self.engine_config
        state = UserRelationState(
            user_key=USER_KEY,
            affinity_centi=9000,
            active_days=cfg.title_required_active_days,
            valid_interactions=cfg.title_required_interactions,
            preferred_title="主人",
            title_opt_in=True,
            successful_reply_count=10,
            last_title_reply_count=9,
        )
        _, should_use = self.engine.compute_title_state(state, 7000, False)
        self.assertFalse(should_use)

        state.successful_reply_count = 10 + cfg.title_min_reply_interval
        _, should_use = self.engine.compute_title_state(state, 7000, False)
        self.assertTrue(should_use)

    # ──── 并发与事务 ─────────────────────────────────────

    def test_t20_two_writes_do_not_lose_updates(self) -> None:
        """连续两次写状态不丢更新，日预算仍正确。"""

        first = self.make_event(EventCategory.PRAISE.value, text="第一次夸")
        second = self.make_event(EventCategory.PRAISE.value, text="第二次夸", now=BASE_NOW + 800)
        self.engine.apply(first, BASE_NOW)
        self.engine.apply(second, BASE_NOW + 800)

        user = self.storage.load_user(USER_KEY, self.engine_config)
        daily = self.storage.load_daily(USER_KEY, self.clock.local_date(BASE_NOW + 800))
        self.assertEqual(daily.semantic_positive_count, 2)
        self.assertEqual(
            user.affinity_centi - self.engine_config.initial_affinity_centi,
            daily.positive_f_used,
        )

    def test_t30_backup_and_restore(self) -> None:
        """备份后恢复值、schema 与去重保持一致。"""

        event = self.make_event(EventCategory.PRAISE.value, message_id="backup-me")
        self.engine.apply(event, BASE_NOW)
        affinity = self.snapshot().affinity_centi

        backup_path = self.tmpdir / "backup" / "affect-backup.sqlite3"
        self.repository.backup_to(backup_path)
        self.assertTrue(backup_path.exists())

        restored_dir = self.tmpdir / "restored"
        restored_dir.mkdir(parents=True, exist_ok=True)
        restored_db = restored_dir / "affect.sqlite3"
        shutil.copyfile(backup_path, restored_db)

        restored = repository_module.AffectRepository(restored_db, single_threaded=True)
        restored.start()
        try:
            storage = repository_module.create_storage(restored, lambda: self.engine_config)
            engine = engine_module.AffectEngine(storage, self.engine_config, self.clock)
            snapshot = engine.build_snapshot(USER_KEY, BOT_KEY, "private", BASE_NOW)
            self.assertEqual(snapshot.affinity_centi, affinity)
            self.assertEqual(storage.get_event_status(event.event_key), "applied")
            replay = engine.apply(event, BASE_NOW + 5)
            self.assertEqual(replay.status, "duplicate")

            connection = repository_module.open_connection(restored_db)
            try:
                self.assertEqual(
                    repository_module.read_schema_version(connection),
                    repository_module.SCHEMA_VERSION,
                )
            finally:
                connection.close()
        finally:
            restored.close()

    # ──── T31 边界 ───────────────────────────────────────

    def test_t31_mood_level_boundaries(self) -> None:
        """E 分段无重叠空隙。"""

        cases = {
            0: "low",
            1999: "low",
            2000: "gloomy",
            3999: "gloomy",
            4000: "calm",
            6499: "calm",
            6500: "happy",
            8499: "happy",
            8500: "delighted",
            10000: "delighted",
        }
        for value, expected in cases.items():
            self.assertEqual(mood_level(value), expected, msg=f"E={value}")

    def test_t31_affinity_level_boundaries(self) -> None:
        """F 分段无重叠空隙。"""

        cases = {
            0: "stranger",
            1999: "stranger",
            2000: "acquaintance",
            3999: "acquaintance",
            4000: "familiar",
            5999: "familiar",
            6000: "close",
            7999: "close",
            8000: "intimate",
            10000: "intimate",
        }
        for value, expected in cases.items():
            self.assertEqual(affinity_level(value), expected, msg=f"F={value}")

    def test_decay_half_life_is_exact(self) -> None:
        """半衰期后偏差减半。"""

        recovered = engine_module.decay_toward(6000, 10000, 4 * 3600, 4.0)
        self.assertEqual(recovered, 8000)
        recovered = engine_module.decay_to_zero(8000, 2 * 3600, 2.0)
        self.assertEqual(recovered, 4000)

    def test_trust_gate_rejects_low_confidence_and_other_target(self) -> None:
        """低置信度或非 bot 对象不进入分数表。"""

        low = self.make_event(EventCategory.PRAISE.value, confidence=0.2)
        self.assertEqual(self.engine.apply(low, BASE_NOW).status, "neutral")

        other = self.make_event(EventCategory.PRAISE.value, target=Target.OTHER.value, now=BASE_NOW + 1)
        self.assertEqual(self.engine.apply(other, BASE_NOW + 1).status, "neutral")

    def test_apology_needs_prior_attack_for_f_repair(self) -> None:
        """无攻击时道歉不给语义 F 修复（仅有效交流按规范加分）。"""

        result = self.engine.apply(self.make_event(EventCategory.APOLOGY.value), BASE_NOW)
        self.assertIn("apology_no_repair_target", result.suppressed)
        self.assertEqual(
            result.delta_affinity_centi,
            self.engine_config.valid_chat_gain_centi,
        )
        # 新用户好感15，心情影响为基础值的50%。
        self.assertEqual(result.delta_mood_centi, 50)

    def test_praise_mood_effect_increases_by_relationship(self) -> None:
        """同一夸奖在不同关系下改变心情幅度不同，且按事件前好感选档。"""
        observed = []
        for i, affinity in enumerate((1500, 3000, 5000, 7000, 9000)):
            key = f"qq\x1f10000\x1f\x1ftest-{i}"
            self.storage.save_user(UserRelationState(user_key=key, affinity_centi=affinity))
            result = self.engine.apply(self.make_event(EventCategory.PRAISE.value, user_key=key), BASE_NOW)
            observed.append((result.delta_mood_centi, result.delta_offset_centi, result.delta_affinity_centi))
        self.assertEqual(observed, [(100, 150, 45), (150, 225, 45), (200, 300, 45),
                                    (240, 360, 27), (280, 420, 13)])

    def test_stranger_attack_is_smaller_but_close_attack_not_amplified(self) -> None:
        """陌生人影响较小；高好感不放大攻击的心情伤害，长期好感扣分仍相同。"""
        deltas = []
        for i, affinity in enumerate((1500, 5000, 9000)):
            key = f"qq\x1f10000\x1f\x1fattack-{i}"
            self.storage.save_user(UserRelationState(user_key=key, affinity_centi=affinity))
            result = self.engine.apply(self.make_event(EventCategory.MILD_ATTACK.value, user_key=key), BASE_NOW)
            deltas.append((result.delta_mood_centi, result.delta_offset_centi, result.delta_affinity_centi))
        self.assertEqual(deltas, [(-100, -400, -100), (-200, -800, -100), (-200, -800, -100)])

    def test_high_affinity_positive_mood_still_respects_daily_cap(self) -> None:
        """亲近者更有影响，但不能绕过原本的每日心情贡献上限。"""
        self.storage.save_user(UserRelationState(user_key=USER_KEY, affinity_centi=9000))
        changes = []
        for i in range(3):
            now = BASE_NOW + i * 601
            result = self.engine.apply(self.make_event(EventCategory.PRAISE.value, now=now), now)
            changes.append(result.delta_mood_centi)
        self.assertEqual(changes, [280, 280, 40])
        self.assertEqual(self.storage.load_daily(USER_KEY, self.clock.local_date(BASE_NOW)).positive_m_used, 600)

    def test_comfort_respects_relationship_and_cannot_raise_offset_above_zero(self) -> None:
        for i, affinity in enumerate((1500, 9000)):
            key = f"qq\x1f10000\x1f\x1fcomfort-{i}"
            self.storage.save_user(UserRelationState(user_key=key, affinity_centi=affinity,
                offset_centi=-300, offset_local_date=self.clock.local_date(BASE_NOW), offset_update_utc=BASE_NOW))
            result = self.engine.apply(self.make_event(EventCategory.COMFORT.value, user_key=key), BASE_NOW)
            self.assertEqual(result.delta_offset_centi, 300)
            self.assertEqual(result.snapshot.offset_centi, 0)

    def test_apology_can_repair_after_attack(self) -> None:
        """有未修复攻击时道歉可给 F 修复。"""

        self.engine.apply(self.make_event(EventCategory.MILD_ATTACK.value), BASE_NOW)
        result = self.engine.apply(
            self.make_event(EventCategory.APOLOGY.value, now=BASE_NOW + 1200),
            BASE_NOW + 1200,
        )
        self.assertGreater(result.delta_affinity_centi, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
