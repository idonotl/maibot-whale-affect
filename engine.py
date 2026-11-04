"""纯状态转移引擎：唯一写分入口 ``AffectEngine.apply()``。

该模块刻意不依赖宿主 SDK，也不直接操作 SQLite：所有持久化通过 ``StateStorage``
协议注入，因此可以用临时数据库或内存 fake 做确定性测试。

计分顺序严格对应技术路线附录 A。
"""

from __future__ import annotations

import hashlib

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple
from zoneinfo import ZoneInfo

from .models import (
    ATTACK_CATEGORIES,
    POSITIVE_SEMANTIC_CATEGORIES,
    SCORE_TABLE,
    VALID_CHAT_CATEGORIES,
    AffectEvent,
    ApplyResult,
    Classification,
    DailyUserStats,
    EngineConfig,
    EventCategory,
    EventStatus,
    MoodState,
    Reaction,
    Snapshot,
    Target,
    UserRelationState,
    affinity_level,
    mood_affinity_coefficient,
    mood_level,
    positive_decay_coefficient,
)


class StateStorage(Protocol):
    """引擎所需的最小存储接口。"""

    def load_mood(self, bot_key: str) -> Optional[MoodState]:
        """读取机器人心情状态。"""

    def save_mood(self, state: MoodState) -> None:
        """写入机器人心情状态。"""

    def load_user(self, user_key: str, config: EngineConfig) -> UserRelationState:
        """读取（必要时创建）用户关系状态。"""

    def save_user(self, state: UserRelationState) -> None:
        """写入用户关系状态。"""

    def load_daily(self, user_key: str, local_date: str) -> DailyUserStats:
        """读取（必要时创建）当日统计。"""

    def save_daily(self, stats: DailyUserStats) -> None:
        """写入当日统计。"""

    def get_event_status(self, event_key: str) -> Optional[str]:
        """读取事件已落库状态。"""

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
        """写入（或替换）事件记录。"""

    def get_cooldown(self, user_key: str, bucket: str) -> Optional[Tuple[int, str]]:
        """读取冷却记录：(last_event_utc, text_hash)。"""

    def set_cooldown(self, user_key: str, bucket: str, last_event_utc: int, text_hash: str) -> None:
        """写入冷却记录。"""

    def has_unrepaired_attack(self, user_key: str, since_utc: int) -> bool:
        """判断指定时间以来是否存在未修复的攻击事件。"""


def to_centi(points: float) -> int:
    """把 0..100 的分数点转换为 centi 整数。

    Args:
        points: 分数点，例如 60 或 0.15。

    Returns:
        int: centi 值，例如 6000 或 15。
    """

    return int(round(float(points) * 100))


def from_centi(centi: int) -> float:
    """把 centi 整数还原为分数点。

    Args:
        centi: centi 值。

    Returns:
        float: 分数点。
    """

    return round(int(centi) / 100.0, 2)


def clamp(value: int, lower: int, upper: int) -> int:
    """把整数限制在闭区间内。"""

    return max(lower, min(upper, value))


def decay_toward(center_centi: int, current_centi: int, elapsed_seconds: float, half_life_hours: float) -> int:
    """按指数半衰期把数值向中心值恢复。

    Args:
        center_centi: 目标中心值（centi）。
        current_centi: 当前值（centi）。
        elapsed_seconds: 已过秒数。
        half_life_hours: 半衰期（小时）。

    Returns:
        int: 恢复后的 centi 值（四舍五入到整数）。
    """

    if elapsed_seconds <= 0 or half_life_hours <= 0:
        return int(current_centi)
    factor = 2.0 ** (-(elapsed_seconds / 3600.0) / half_life_hours)
    return int(round(center_centi + (current_centi - center_centi) * factor))


def decay_to_zero(current_centi: int, elapsed_seconds: float, half_life_hours: float) -> int:
    """按指数半衰期把数值向 0 恢复。

    Args:
        current_centi: 当前值（centi）。
        elapsed_seconds: 已过秒数。
        half_life_hours: 半衰期（小时）。

    Returns:
        int: 恢复后的 centi 值。
    """

    return decay_toward(0, current_centi, elapsed_seconds, half_life_hours)


class LocalClock:
    """可注入的时钟，统一把 UTC 时间戳转成本地日期。"""

    def __init__(self, timezone_name: str = "Asia/Shanghai") -> None:
        """初始化时钟。

        Args:
            timezone_name: IANA 时区名。

        Raises:
            ValueError: 时区不可用。
        """

        try:
            self._tz = ZoneInfo(timezone_name)
        except Exception as exc:  # noqa: BLE001 - 转成明确的配置错误
            raise ValueError(f"时区不可用: {timezone_name}") from exc
        self.timezone_name = timezone_name

    def now_utc(self) -> int:
        """返回当前 UTC Unix 秒。"""

        return int(datetime.now(tz=timezone.utc).timestamp())

    def local_date(self, now_utc: int) -> str:
        """把 UTC 时间戳转换成本地日期字符串。"""

        return datetime.fromtimestamp(int(now_utc), tz=self._tz).strftime("%Y-%m-%d")

    def local_date_obj(self, now_utc: int) -> date:
        """把 UTC 时间戳转换成本地 ``date``。"""

        return datetime.fromtimestamp(int(now_utc), tz=self._tz).date()

    def minutes_of_day(self, now_utc: int) -> int:
        """返回本地时刻在当天的分钟数，用于调试与测试断言。"""

        local = datetime.fromtimestamp(int(now_utc), tz=self._tz)
        return local.hour * 60 + local.minute


@dataclass
class ReactionDecision:
    """由事件推导出的临时反应。"""

    reaction: str = Reaction.NONE.value
    ttl_seconds: int = 0
    replies_left: int = 0
    explicit: bool = False
    keep_expires_utc: int = 0


class AffectEngine:
    """M / D / F / reaction 的确定性状态计算器。"""

    def __init__(self, storage: StateStorage, config: EngineConfig, clock: LocalClock) -> None:
        """初始化引擎。

        Args:
            storage: 持久化实现。
            config: 纯数值参数。
            clock: 时区时钟。
        """

        self._storage = storage
        self._config = config
        self._clock = clock

    @property
    def config(self) -> EngineConfig:
        """返回当前数值参数。"""

        return self._config

    @property
    def clock(self) -> LocalClock:
        """返回当前时钟。"""

        return self._clock

    # ──── 对外查询 ────────────────────────────────────────

    def build_snapshot(self, user_key: str, bot_key: str, scene: str, now_utc: int) -> Snapshot:
        """构造只读快照，不写任何数据。

        Args:
            user_key: 用户身份键。
            bot_key: 机器人身份键。
            scene: 会话范围（``private`` / ``group``）。
            now_utc: 当前 UTC 时间戳。

        Returns:
            Snapshot: 已做衰减投影的状态快照。
        """

        local_date = self._clock.local_date(now_utc)
        mood = self._project_mood(bot_key, local_date, now_utc)
        user = self._project_user(user_key, local_date, now_utc)
        effective = clamp(mood.mood_centi + user.offset_centi, 0, 10000)
        is_group = scene == "group"
        title_allowed, should_use = self.compute_title_state(user, effective, is_group)
        return Snapshot(
            user_key=user_key,
            mood_centi=mood.mood_centi,
            offset_centi=user.offset_centi,
            affinity_centi=user.affinity_centi,
            reaction=user.reaction,
            reaction_expires_utc=user.reaction_expires_utc,
            title_allowed=title_allowed,
            title_should_use=should_use,
            effective_centi=effective,
            local_date=local_date,
            active_days=user.active_days,
            valid_interactions=user.valid_interactions,
            is_group=is_group,
        )

    def project_global(self, bot_key: str, now_utc: int) -> MoodState:
        """只读投影机器人当日的全局心情 M。

        Args:
            bot_key: 机器人身份键。
            now_utc: 当前 UTC 时间戳。

        Returns:
            MoodState: 已做衰减投影的心情状态，不写库。
        """

        return self._project_mood(bot_key, self._clock.local_date(now_utc), now_utc)

    def compute_title_state(
        self,
        user: UserRelationState,
        effective_centi: int,
        is_group: bool,
    ) -> Tuple[bool, bool]:
        """计算称呼许可与本次是否应使用。

        Args:
            user: 用户状态。
            effective_centi: 有效心情 E。
            is_group: 是否群聊。

        Returns:
            Tuple[bool, bool]: (是否处于许可状态, 本次回复是否应使用称呼)。
        """

        cfg = self._config
        enabled = bool(user.title_enabled)

        if enabled and user.affinity_centi < cfg.title_exit_centi:
            enabled = False
        if not enabled and user.affinity_centi >= cfg.title_enter_centi:
            conditions_met = (
                user.active_days >= cfg.title_required_active_days
                and user.valid_interactions >= cfg.title_required_interactions
                and (user.title_opt_in or not cfg.title_requires_opt_in)
                and bool(user.preferred_title)
            )
            if conditions_met:
                enabled = True

        if is_group and not cfg.title_allow_in_groups:
            return False, False

        # 许可状态与「本次是否说出来」分开：低心情或刚受伤时不使用。
        runtime_blocked = effective_centi < 4000 or user.reaction == Reaction.HURT.value
        if not enabled or runtime_blocked:
            return enabled and not runtime_blocked, False

        gap = user.successful_reply_count - user.last_title_reply_count
        should_use = gap >= max(1, cfg.title_min_reply_interval)
        return True, should_use

    def mark_title_used(self, user_key: str, bot_key: str, now_utc: int) -> None:
        """记录一次称呼使用，让计数间隔重新开始。

        Args:
            user_key: 用户身份键。
            bot_key: 机器人身份键。
            now_utc: 当前 UTC 时间戳。
        """

        del bot_key
        local_date = self._clock.local_date(now_utc)
        user = self._project_user(user_key, local_date, now_utc)
        updated = replace(user, last_title_reply_count=user.successful_reply_count, version=user.version + 1)
        self._storage.save_user(updated)

    def record_successful_reply(self, user_key: str, bot_key: str, now_utc: int) -> None:
        """统计一次成功发出的普通回复，并消耗 reaction 的剩余次数。

        Args:
            user_key: 用户身份键。
            bot_key: 机器人身份键。
            now_utc: 当前 UTC 时间戳。
        """

        del bot_key
        local_date = self._clock.local_date(now_utc)
        user = self._project_user(user_key, local_date, now_utc)
        replies_left = max(0, user.reaction_replies_left - 1)
        reaction = user.reaction
        expires = user.reaction_expires_utc
        if reaction != Reaction.NONE.value and replies_left <= 0:
            reaction = Reaction.NONE.value
            expires = 0
        updated = replace(
            user,
            successful_reply_count=user.successful_reply_count + 1,
            reaction=reaction,
            reaction_expires_utc=expires,
            reaction_replies_left=replies_left,
            version=user.version + 1,
        )
        self._storage.save_user(updated)

    def set_preferred_title(
        self,
        user_key: str,
        bot_key: str,
        now_utc: int,
        title: str,
        opt_in: bool,
    ) -> UserRelationState:
        """设置或撤销用户的称呼偏好。

        Args:
            user_key: 用户身份键。
            bot_key: 机器人身份键。
            now_utc: 当前 UTC 时间戳。
            title: 偏好称呼；空字符串表示撤销。
            opt_in: 是否明确同意。

        Returns:
            UserRelationState: 更新后的状态。
        """

        del bot_key
        local_date = self._clock.local_date(now_utc)
        user = self._project_user(user_key, local_date, now_utc)
        updated = replace(
            user,
            preferred_title=str(title).strip(),
            title_opt_in=bool(opt_in),
            title_enabled=False if not opt_in or not str(title).strip() else user.title_enabled,
            version=user.version + 1,
        )
        self._storage.save_user(updated)
        return updated

    # ──── 投影（只读衰减，不写库） ────────────────────────

    def _project_mood(self, bot_key: str, local_date: str, now_utc: int) -> MoodState:
        """计算指定时刻的心情投影值。"""

        cfg = self._config
        stored = self._storage.load_mood(bot_key)
        if stored is None or stored.local_date != local_date:
            return MoodState(
                bot_key=bot_key,
                local_date=local_date,
                mood_centi=cfg.daily_initial_centi,
                last_update_utc=now_utc,
            )
        elapsed = max(0, now_utc - stored.last_update_utc)
        recovered = decay_toward(
            cfg.daily_initial_centi,
            stored.mood_centi,
            elapsed,
            cfg.global_recovery_half_life_hours,
        )
        return MoodState(
            bot_key=bot_key,
            local_date=local_date,
            mood_centi=clamp(recovered, 0, 10000),
            last_update_utc=now_utc,
            version=stored.version,
        )

    def _project_user(self, user_key: str, local_date: str, now_utc: int) -> UserRelationState:
        """计算指定时刻的用户状态投影值。"""

        cfg = self._config
        stored = self._storage.load_user(user_key, cfg)

        offset = stored.offset_centi
        offset_date = stored.offset_local_date or local_date
        if offset_date != local_date:
            offset = 0
        else:
            elapsed = max(0, now_utc - stored.offset_update_utc)
            offset = decay_to_zero(offset, elapsed, cfg.user_offset_half_life_hours)

        reaction = stored.reaction
        expires = stored.reaction_expires_utc
        replies_left = stored.reaction_replies_left
        if reaction != Reaction.NONE.value and now_utc >= expires:
            reaction = Reaction.NONE.value
            expires = 0
            replies_left = 0

        return replace(
            stored,
            offset_centi=clamp(offset, cfg.offset_min_centi, cfg.offset_max_centi),
            offset_local_date=local_date,
            offset_update_utc=now_utc,
            reaction=reaction,
            reaction_expires_utc=expires,
            reaction_replies_left=replies_left,
        )

    # ──── 唯一写分入口 ────────────────────────────────────

    def apply(self, event: AffectEvent, now_utc: int) -> ApplyResult:
        """对一条事件执行去重、衰减、预算与计分，并写入事实。

        整体在一个事务作用域内完成（存储实现支持时）。

        Args:
            event: 待计分事件。
            now_utc: 处理时刻（UTC 秒）。

        Returns:
            ApplyResult: 状态、增量与抑制原因。
        """

        run_atomic = getattr(self._storage, "run_atomic", None)
        if run_atomic is None:
            return self._apply_locked(event, now_utc)
        return run_atomic(lambda: self._apply_locked(event, now_utc))

    def _apply_locked(self, event: AffectEvent, now_utc: int) -> ApplyResult:
        """在事务作用域内执行计分。

        Args:
            event: 待计分事件。
            now_utc: 处理时刻（UTC 秒）。

        Returns:
            ApplyResult: 状态、增量与抑制原因。
        """

        cfg = self._config
        now_utc = int(now_utc)
        local_date = self._clock.local_date(now_utc)

        existing = self._storage.get_event_status(event.event_key)
        if existing is not None and existing != EventStatus.PENDING.value:
            snapshot = self.build_snapshot(event.user_key, event.bot_key, self._scene(event), now_utc)
            return ApplyResult(
                status="duplicate",
                snapshot=snapshot,
                suppressed=[f"event_already_{existing}"],
                reason="同一消息已处理过，不重复计分",
            )

        if not event.is_approved:
            self._write_event(
                event,
                now_utc,
                local_date,
                status=EventStatus.DROPPED.value,
                reason_code="not_approved",
            )
            return ApplyResult(status=EventStatus.DROPPED.value, reason="消息未获准进入对话流")

        age = max(0, now_utc - event.event_time_utc)
        if age > cfg.source_event_max_age_seconds or event.local_date != local_date:
            self._write_event(
                event,
                now_utc,
                local_date,
                status=EventStatus.DROPPED.value,
                reason_code="stale_event",
            )
            return ApplyResult(status=EventStatus.DROPPED.value, reason="事件已过期或跨日，不补扣分")

        classification = event.classification
        if not (
            classification.target == Target.BOT.value
            and classification.event in SCORE_TABLE
            and classification.confidence >= cfg.min_confidence
        ):
            self._write_event(
                event,
                now_utc,
                local_date,
                status=EventStatus.NEUTRAL.value,
                reason_code=classification.reason_code or "not_trusted",
            )
            return ApplyResult(status=EventStatus.NEUTRAL.value, reason="分类不可信或对象不是机器人")

        suppressed: List[str] = []
        category = classification.event

        mood = self._project_mood(event.bot_key, local_date, now_utc)
        user = self._project_user(event.user_key, local_date, now_utc)
        daily = self._storage.load_daily(event.user_key, local_date)
        if daily.local_date != local_date:
            daily = DailyUserStats(user_key=event.user_key, local_date=local_date)

        before = {
            "mood_centi": mood.mood_centi,
            "offset_centi": user.offset_centi,
            "affinity_centi": user.affinity_centi,
            "reaction": user.reaction,
        }

        if category == EventCategory.JEALOUSY_CANDIDATE.value:
            # Respect a prior real hurt state and keep this private and infrequent.
            cooldown = self._storage.get_cooldown(event.user_key, "jealousy")
            chance = 0.0 if user.affinity_centi < 4000 else (0.25 if user.affinity_centi < 6000 else (0.40 if user.affinity_centi < 8000 else 0.55))
            draw = int.from_bytes(hashlib.sha256(event.event_key.encode("utf-8")).digest()[:8], "big") / 2**64
            selected = (not event.is_group and user.reaction != Reaction.HURT.value and chance > 0
                        and (cooldown is None or now_utc - cooldown[0] >= 1800) and draw < chance)
            if not selected:
                category = EventCategory.FRIENDLY_CHAT.value
                suppressed.append("jealousy_not_selected")

        attack = category in ATTACK_CATEGORIES
        positive_semantic = category in POSITIVE_SEMANTIC_CATEGORIES

        # 1) 文本重复抑制
        text_hash = event.text_hash
        if text_hash:
            duplicate = self._storage.get_cooldown(event.user_key, "text")
            if (
                duplicate is not None
                and duplicate[1] == text_hash
                and now_utc - duplicate[0] < cfg.duplicate_text_window_seconds
            ):
                suppressed.append("duplicate_text")
                return self._finish_without_score(
                    event, now_utc, local_date, before, suppressed, "相同文本在去重窗口内"
                )
            self._storage.set_cooldown(event.user_key, "text", now_utc, text_hash)

        # 2) 分类冷却与每日次数预算
        allow_semantic = True
        if attack:
            cooldown = self._storage.get_cooldown(event.user_key, "attack")
            if cooldown is not None and now_utc - cooldown[0] < cfg.attack_cooldown_seconds:
                suppressed.append("attack_cooldown")
                allow_semantic = False
        elif positive_semantic:
            cooldown = self._storage.get_cooldown(event.user_key, "positive_semantic")
            if cooldown is not None and now_utc - cooldown[0] < cfg.positive_semantic_cooldown_seconds:
                suppressed.append("positive_semantic_cooldown")
                allow_semantic = False
            elif daily.semantic_positive_count >= cfg.positive_semantic_daily_count_cap:
                suppressed.append("positive_semantic_daily_cap")
                allow_semantic = False

        # 3) 确定 reaction（与是否仍有计分预算无关）
        decision = self._decide_reaction(category, user, now_utc)

        # 4) 语义增量
        delta_m, delta_d, delta_f = SCORE_TABLE[category] if allow_semantic else (0, 0, 0)
        if not allow_semantic:
            delta_m = 0

        # Affect global mood and this user's short-term offset according to the
        # relationship *before* this event. Long-term affinity scoring remains
        # separate; negative mood changes cannot exceed their old base values.
        mood_factor = mood_affinity_coefficient(user.affinity_centi, negative=attack)
        delta_m = int(round(delta_m * mood_factor))
        delta_d = int(round(delta_d * mood_factor))

        apology_blocked = False
        if category == EventCategory.APOLOGY.value and delta_f > 0:
            repaired = daily.apology_repair_granted
            has_attack = self._storage.has_unrepaired_attack(
                event.user_key, now_utc - cfg.apology_repair_window_seconds
            )
            if repaired or not has_attack:
                delta_f = 0
                apology_blocked = True
                suppressed.append("apology_no_repair_target")

        # 5) 有效交流与活跃日
        valid_chat_gain = 0
        active_bonus = 0
        if category in VALID_CHAT_CATEGORIES:
            last_valid = user.last_valid_utc
            if last_valid == 0 or now_utc - last_valid >= cfg.valid_chat_interval_seconds:
                valid_chat_gain = cfg.valid_chat_gain_centi
            else:
                suppressed.append("valid_chat_interval")
        if attack:
            suppressed.append("attack_not_valid_chat")

        new_valid_count = daily.valid_count
        first_valid_utc = daily.first_valid_utc
        if valid_chat_gain > 0:
            new_valid_count += 1
            if first_valid_utc == 0:
                first_valid_utc = now_utc
            if (
                not daily.active_bonus_granted
                and new_valid_count >= cfg.active_day_required_interactions
                and now_utc - first_valid_utc >= cfg.active_day_min_span_seconds
            ):
                active_bonus = cfg.active_day_bonus_centi
            else:
                if not daily.active_bonus_granted:
                    suppressed.append("active_day_not_reached")

        # 6) 正向 F 按更新前 F 统一递减
        coefficient = positive_decay_coefficient(user.affinity_centi)
        scaled_semantic = int(round(delta_f * coefficient)) if delta_f > 0 else delta_f
        scaled_valid = int(round(valid_chat_gain * coefficient)) if valid_chat_gain else 0
        scaled_active = int(round(active_bonus * coefficient)) if active_bonus else 0

        # 7) 依次分配有限正向 F 预算：语义 → 有效交流 → 活跃日
        remaining_positive = max(0, cfg.daily_positive_cap_centi - daily.positive_f_used)
        headroom = max(0, 10000 - user.affinity_centi)

        granted_semantic = 0
        granted_valid = 0
        granted_active = 0
        if scaled_semantic > 0:
            granted_semantic = min(scaled_semantic, remaining_positive, headroom)
            remaining_positive -= granted_semantic
            headroom -= granted_semantic
            if granted_semantic < scaled_semantic:
                suppressed.append("positive_f_daily_cap")
        if scaled_valid > 0:
            granted_valid = min(scaled_valid, remaining_positive, headroom)
            remaining_positive -= granted_valid
            headroom -= granted_valid
            if granted_valid < scaled_valid:
                suppressed.append("positive_f_daily_cap")
        if scaled_active > 0:
            granted_active = min(scaled_active, remaining_positive, headroom)
            if granted_active < scaled_active:
                suppressed.append("positive_f_daily_cap")
        total_positive_f = granted_semantic + granted_valid + granted_active

        # 8) 负向 F 不递减，受每日损失上限约束
        total_negative_f = 0
        if delta_f < 0:
            remaining_negative = max(0, cfg.daily_negative_cap_centi - daily.negative_f_used)
            requested = abs(delta_f)
            total_negative_f = min(requested, remaining_negative, user.affinity_centi)
            if total_negative_f < requested:
                suppressed.append("negative_f_daily_cap")

        # 9) M 的单用户日贡献预算
        applied_m = 0
        if delta_m > 0:
            remaining_m = max(0, cfg.positive_m_cap_centi - daily.positive_m_used)
            applied_m = min(delta_m, remaining_m)
            if applied_m < delta_m:
                suppressed.append("positive_m_daily_cap")
        elif delta_m < 0:
            remaining_m = max(0, cfg.negative_m_cap_centi - daily.negative_m_used)
            applied_m = -min(abs(delta_m), remaining_m)
            if abs(applied_m) < abs(delta_m):
                suppressed.append("negative_m_daily_cap")

        # 10) D 边界；comfort / apology 只在 D<0 时最多恢复到 0
        applied_d = 0
        if category in (EventCategory.COMFORT.value, EventCategory.APOLOGY.value):
            if user.offset_centi < 0 and delta_d > 0:
                applied_d = min(delta_d, -user.offset_centi)
        elif delta_d:
            applied_d = delta_d

        # 11) clamp 并落库
        new_mood_centi = clamp(mood.mood_centi + applied_m, 0, 10000)
        new_offset_centi = clamp(user.offset_centi + applied_d, cfg.offset_min_centi, cfg.offset_max_centi)
        new_affinity_centi = clamp(
            user.affinity_centi + total_positive_f - total_negative_f,
            0,
            10000,
        )

        reaction_state, reaction_expires, replies_left = self._apply_reaction(decision, user, now_utc)

        title_enabled = user.title_enabled
        if title_enabled and new_affinity_centi < cfg.title_exit_centi:
            title_enabled = False
        elif not title_enabled and new_affinity_centi >= cfg.title_enter_centi:
            title_enabled = (
                user.active_days >= cfg.title_required_active_days
                and user.valid_interactions >= cfg.title_required_interactions
                and (user.title_opt_in or not cfg.title_requires_opt_in)
                and bool(user.preferred_title)
            )

        # 活跃日是真实行为事实：判定条件满足即记账，F 预算不足只影响增量不到账。
        active_day_qualified = active_bonus > 0 and not daily.active_bonus_granted
        new_active_days = user.active_days + 1 if active_day_qualified else user.active_days
        new_valid_interactions = user.valid_interactions + (1 if valid_chat_gain > 0 else 0)

        updated_user = replace(
            user,
            affinity_centi=new_affinity_centi,
            offset_centi=new_offset_centi,
            offset_local_date=local_date,
            offset_update_utc=now_utc,
            active_days=new_active_days,
            valid_interactions=new_valid_interactions,
            last_valid_utc=now_utc if valid_chat_gain > 0 else user.last_valid_utc,
            last_attack_utc=now_utc if (attack and allow_semantic) else user.last_attack_utc,
            reaction=reaction_state,
            reaction_expires_utc=reaction_expires,
            reaction_replies_left=replies_left,
            title_enabled=title_enabled,
            version=user.version + 1,
        )
        updated_mood = MoodState(
            bot_key=event.bot_key,
            local_date=local_date,
            mood_centi=new_mood_centi,
            last_update_utc=now_utc,
            version=mood.version + 1,
        )
        updated_daily = replace(
            daily,
            local_date=local_date,
            positive_f_used=daily.positive_f_used + total_positive_f,
            negative_f_used=daily.negative_f_used + total_negative_f,
            positive_m_used=daily.positive_m_used + max(0, applied_m),
            negative_m_used=daily.negative_m_used + max(0, -applied_m),
            semantic_positive_count=(
                daily.semantic_positive_count + 1 if positive_semantic and allow_semantic else daily.semantic_positive_count
            ),
            valid_count=new_valid_count,
            active_bonus_granted=daily.active_bonus_granted or active_day_qualified,
            apology_repair_granted=daily.apology_repair_granted
            or (category == EventCategory.APOLOGY.value and granted_semantic > 0),
            first_valid_utc=first_valid_utc,
        )

        if attack and allow_semantic:
            self._storage.set_cooldown(event.user_key, "attack", now_utc, text_hash)
        if positive_semantic and allow_semantic:
            self._storage.set_cooldown(event.user_key, "positive_semantic", now_utc, text_hash)
        if category == EventCategory.JEALOUSY_CANDIDATE.value:
            self._storage.set_cooldown(event.user_key, "jealousy", now_utc, text_hash)

        self._storage.save_mood(updated_mood)
        self._storage.save_user(updated_user)
        self._storage.save_daily(updated_daily)

        if apology_blocked:
            suppressed.append("apology_repair_not_applied")

        after = {
            "mood_centi": new_mood_centi,
            "offset_centi": new_offset_centi,
            "affinity_centi": new_affinity_centi,
            "reaction": reaction_state,
        }
        delta = {
            "mood_centi": applied_m,
            "offset_centi": applied_d,
            "mood_affinity_factor": mood_factor,
            "affinity_centi": total_positive_f - total_negative_f,
            "positive_f": total_positive_f,
            "negative_f": total_negative_f,
            "valid_chat": granted_valid,
            "active_bonus": granted_active,
            "suppressed": suppressed,
        }
        self._storage.write_event(
            event,
            status=EventStatus.APPLIED.value,
            category=category,
            confidence=classification.confidence,
            classifier=event.classifier,
            reason_code=classification.reason_code,
            score_before=before,
            delta=delta,
            score_after=after,
        )

        snapshot = Snapshot(
            user_key=event.user_key,
            mood_centi=new_mood_centi,
            offset_centi=new_offset_centi,
            affinity_centi=new_affinity_centi,
            reaction=reaction_state,
            reaction_expires_utc=reaction_expires,
            title_allowed=self.compute_title_state(updated_user, clamp(new_mood_centi + new_offset_centi, 0, 10000), event.is_group)[0],
            title_should_use=False,
            effective_centi=clamp(new_mood_centi + new_offset_centi, 0, 10000),
            local_date=local_date,
            active_days=new_active_days,
            valid_interactions=new_valid_interactions,
            is_group=event.is_group,
        )
        return ApplyResult(
            status=EventStatus.APPLIED.value,
            snapshot=snapshot,
            delta_mood_centi=applied_m,
            delta_offset_centi=applied_d,
            delta_affinity_centi=total_positive_f - total_negative_f,
            suppressed=suppressed,
            reason=f"{category}",
        )

    # ──── 内部辅助 ────────────────────────────────────────

    @staticmethod
    def _scene(event: AffectEvent) -> str:
        """由事件推断会话范围。"""

        return "group" if event.is_group else "private"

    def _decide_reaction(
        self,
        category: str,
        user: UserRelationState,
        now_utc: int,
    ) -> ReactionDecision:
        """根据事件类别决定临时反应。

        Args:
            category: 事件类别。
            user: 当前用户状态。
            now_utc: 当前 UTC 时间戳。

        Returns:
            ReactionDecision: reactor 决策。
        """

        cfg = self._config
        current = user.reaction
        if current != Reaction.NONE.value and user.reaction_expires_utc and now_utc >= user.reaction_expires_utc:
            current = Reaction.NONE.value

        if category in (EventCategory.MILD_ATTACK.value, EventCategory.SEVERE_ATTACK.value):
            return ReactionDecision(Reaction.HURT.value, cfg.hurt_ttl_seconds, 0, True)
        if category == EventCategory.JEALOUSY_CANDIDATE.value:
            return ReactionDecision(Reaction.JEALOUS.value, 180, 1, True)
        if category == EventCategory.AFFECTIONATE_TEASE.value:
            if current == Reaction.HURT.value:
                # 不能靠再喊昵称覆盖真实受伤
                return ReactionDecision(
                    current,
                    0,
                    user.reaction_replies_left,
                    False,
                    keep_expires_utc=user.reaction_expires_utc,
                )
            # Persona nicknames are normal playful address, not a persistent mood.
            # Let the reply model occasionally joke back without requiring protest
            # across subsequent replies or waiting for the user to comfort it.
            return ReactionDecision(Reaction.NONE.value, 0, 0, False)
        if category in (EventCategory.COMFORT.value, EventCategory.APOLOGY.value):
            return ReactionDecision(Reaction.SOOTHED.value, cfg.soothed_ttl_seconds, cfg.soothed_max_replies, True)
        return ReactionDecision(
            current,
            0,
            user.reaction_replies_left,
            False,
            keep_expires_utc=user.reaction_expires_utc,
        )

    @staticmethod
    def _apply_reaction(
        decision: ReactionDecision,
        user: UserRelationState,
        now_utc: int,
    ) -> Tuple[str, int, int]:
        """把 reaction 决策转成写入值。"""

        if decision.explicit:
            return decision.reaction, now_utc + decision.ttl_seconds, decision.replies_left
        if decision.reaction == Reaction.NONE.value:
            return Reaction.NONE.value, 0, 0
        return decision.reaction, decision.keep_expires_utc, decision.replies_left

    def _finish_without_score(
        self,
        event: AffectEvent,
        now_utc: int,
        local_date: str,
        before: Dict[str, Any],
        suppressed: List[str],
        reason: str,
    ) -> ApplyResult:
        """记录一条被抑制但不算错误的事件。"""

        self._storage.write_event(
            event,
            status=EventStatus.NEUTRAL.value,
            category=event.classification.event,
            confidence=event.classification.confidence,
            classifier=event.classifier,
            reason_code="suppressed",
            score_before=before,
            delta={"suppressed": suppressed},
            score_after=before,
        )
        snapshot = self.build_snapshot(event.user_key, event.bot_key, self._scene(event), now_utc)
        return ApplyResult(
            status=EventStatus.NEUTRAL.value,
            snapshot=snapshot,
            suppressed=suppressed,
            reason=reason,
        )

    def _write_event(
        self,
        event: AffectEvent,
        now_utc: int,
        local_date: str,
        *,
        status: str,
        reason_code: str,
    ) -> None:
        """写入一条未被计分的事件记录。"""

        before = {"mood_centi": None, "offset_centi": None, "affinity_centi": None, "reaction": None}
        self._storage.write_event(
            event,
            status=status,
            category=event.classification.event,
            confidence=event.classification.confidence,
            classifier=event.classifier,
            reason_code=reason_code,
            score_before=before,
            delta={"suppressed": [reason_code]},
            score_after=before,
        )


def build_engine_config(
    *,
    mood: Any,
    affinity: Any,
    reactions: Any,
    scoring: Any,
    classifier: Any,
) -> EngineConfig:
    """把 SDK 配置分段转换成本模块的纯数值参数。

    Args:
        mood: ``MoodSectionConfig`` 实例或等价对象。
        affinity: ``AffinitySectionConfig`` 实例或等价对象。
        reactions: ``ReactionsSectionConfig`` 实例或等价对象。
        scoring: ``ScoringSectionConfig`` 实例或等价对象。
        classifier: ``ClassifierSectionConfig`` 实例或等价对象。

    Returns:
        EngineConfig: 引擎参数。
    """

    return EngineConfig(
        daily_initial_centi=to_centi(mood.daily_initial),
        global_recovery_half_life_hours=float(mood.global_recovery_half_life_hours),
        user_offset_half_life_hours=float(mood.user_offset_half_life_hours),
        offset_min_centi=to_centi(mood.offset_min),
        offset_max_centi=to_centi(mood.offset_max),
        positive_m_cap_centi=to_centi(mood.per_user_daily_positive_global_cap),
        negative_m_cap_centi=to_centi(mood.per_user_daily_negative_global_cap),
        initial_affinity_centi=to_centi(affinity.initial),
        daily_positive_cap_centi=to_centi(affinity.daily_positive_cap),
        daily_negative_cap_centi=to_centi(affinity.daily_negative_cap),
        valid_chat_gain_centi=to_centi(affinity.valid_chat_gain),
        valid_chat_interval_seconds=int(affinity.valid_chat_interval_seconds),
        active_day_bonus_centi=to_centi(affinity.active_day_bonus),
        active_day_required_interactions=int(affinity.active_day_required_interactions),
        active_day_min_span_seconds=int(affinity.active_day_min_span_seconds),
        title_enter_centi=to_centi(affinity.title_enter_threshold),
        title_exit_centi=to_centi(affinity.title_exit_threshold),
        title_required_active_days=int(affinity.title_required_active_days),
        title_required_interactions=int(affinity.title_required_interactions),
        title_requires_opt_in=bool(affinity.title_requires_opt_in),
        title_allow_in_groups=bool(affinity.title_allow_in_groups),
        title_min_reply_interval=int(affinity.title_min_reply_interval),
        pout_ttl_seconds=int(reactions.pout_ttl_seconds),
        soothed_ttl_seconds=int(reactions.soothed_ttl_seconds),
        hurt_ttl_seconds=int(reactions.hurt_ttl_seconds),
        pout_max_replies=int(reactions.pout_max_replies),
        soothed_max_replies=int(reactions.soothed_max_replies),
        duplicate_text_window_seconds=int(scoring.duplicate_text_window_seconds),
        positive_semantic_cooldown_seconds=int(scoring.positive_semantic_cooldown_seconds),
        positive_semantic_daily_count_cap=int(scoring.positive_semantic_daily_count_cap),
        attack_cooldown_seconds=int(scoring.attack_cooldown_seconds),
        source_event_max_age_seconds=int(scoring.source_event_max_age_seconds),
        continuation_window_seconds=int(scoring.continuation_window_seconds),
        min_confidence=float(classifier.min_confidence),
    )


def describe_snapshot(snapshot: Snapshot) -> Dict[str, Any]:
    """把快照转换为便于日志与管理页展示的字典。

    Args:
        snapshot: 状态快照。

    Returns:
        Dict[str, Any]: 可读字段字典。
    """

    return {
        "user_key": snapshot.user_key,
        "local_date": snapshot.local_date,
        "mood": from_centi(snapshot.mood_centi),
        "offset": from_centi(snapshot.offset_centi),
        "effective": from_centi(snapshot.effective_centi),
        "affinity": from_centi(snapshot.affinity_centi),
        "mood_level": mood_level(snapshot.effective_centi),
        "affinity_level": affinity_level(snapshot.affinity_centi),
        "reaction": snapshot.reaction,
        "reaction_expires_utc": snapshot.reaction_expires_utc,
        "title_allowed": snapshot.title_allowed,
        "title_should_use": snapshot.title_should_use,
        "active_days": snapshot.active_days,
        "valid_interactions": snapshot.valid_interactions,
        "is_group": snapshot.is_group,
    }
