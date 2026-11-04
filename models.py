"""心情与好感度插件的内部数据模型。

这些模型只在插件内部与测试之间流转，不依赖宿主私有结构。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Target(str, Enum):
    """分类结果中的对象判定。"""

    BOT = "bot"
    OTHER = "other"
    QUOTED = "quoted"
    UNKNOWN = "unknown"


class EventCategory(str, Enum):
    """固定的交互事件枚举。"""

    NEUTRAL = "neutral"
    FRIENDLY_CHAT = "friendly_chat"
    PRAISE = "praise"
    COMFORT = "comfort"
    APOLOGY = "apology"
    AFFECTIONATE_TEASE = "affectionate_tease"
    JEALOUSY_CANDIDATE = "jealousy_candidate"
    CRITICISM = "criticism"
    MILD_ATTACK = "mild_attack"
    SEVERE_ATTACK = "severe_attack"


class Reaction(str, Enum):
    """临时反应状态。"""

    NONE = "none"
    POUT = "pout"
    JEALOUS = "jealous"
    SOOTHED = "soothed"
    HURT = "hurt"


class EventStatus(str, Enum):
    """事件在数据库中的生命周期状态。"""

    PENDING = "pending"
    APPLIED = "applied"
    NEUTRAL = "neutral"
    DROPPED = "dropped"
    ERROR = "error"


@dataclass(frozen=True)
class NormalizedMessage:
    """从宿主消息字典归一化后的入站消息。

    业务层只接受这个结构，不再直接读取宿主原始字典。

    ``scope`` 是宿主正式路由作用域（连接 ID），单连接部署时通常为空字符串；
    它属于用户身份键的一部分，而群/私聊的区别用 ``group_id`` 判断，
    因此同一 QQ 用户跨群与私聊共享同一份好感。
    """

    platform: str
    bot_account_id: str
    scope: str
    user_id: str
    group_id: str
    session_id: str
    message_id: str
    text: str
    timestamp: int
    is_at: bool = False
    is_mentioned: bool = False
    is_notify: bool = False
    is_command: bool = False
    is_picture: bool = False
    is_emoji: bool = False
    reply_to: str = ""
    raw_fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        """消息是否来自群聊。"""

        return bool(self.group_id)

    def identity_key(self) -> "UserKey":
        """返回该消息所属用户的稳定身份键。"""

        return UserKey(
            platform=self.platform,
            bot_account_id=self.bot_account_id,
            scope=self.scope,
            user_id=self.user_id,
        )

    def event_key(self) -> "EventKey":
        """返回该消息的唯一事件键。"""

        return EventKey(
            platform=self.platform,
            bot_account_id=self.bot_account_id,
            scope=self.scope,
            session_id=self.session_id,
            message_id=self.message_id,
        )


@dataclass(frozen=True)
class UserKey:
    """每用户长期好感的身份键。

    技术路线 4.3 要求身份键为 (platform, bot_account_id, scope, user_id)，
    不使用昵称、群名或 session_id。
    """

    platform: str
    bot_account_id: str
    scope: str
    user_id: str

    def serialize(self) -> str:
        """返回用于数据库的规范化稳定字符串。"""

        return "\x1f".join((self.platform, self.bot_account_id, self.scope, self.user_id))

    @classmethod
    def deserialize(cls, raw: str) -> "UserKey":
        """从数据库字符串还原身份键。"""

        parts = str(raw).split("\x1f")
        if len(parts) != 4:
            raise ValueError(f"非法 user_key: {raw!r}")
        return cls(platform=parts[0], bot_account_id=parts[1], scope=parts[2], user_id=parts[3])


@dataclass(frozen=True)
class EventKey:
    """事件唯一键，覆盖同一消息的重复投递。"""

    platform: str
    bot_account_id: str
    scope: str
    session_id: str
    message_id: str

    def serialize(self) -> str:
        """返回用于数据库唯一约束的规范化稳定字符串。"""

        return "\x1f".join((self.platform, self.bot_account_id, self.scope, self.session_id, self.message_id))


@dataclass(frozen=True)
class Classification:
    """规则或模型输出的统一分类结构。"""

    target: str = Target.UNKNOWN.value
    event: str = EventCategory.NEUTRAL.value
    confidence: float = 0.0
    reason_code: str = "unset"

    def is_trusted(self, min_confidence: float) -> bool:
        """判断该分类是否足以进入分数表。"""

        return self.target == Target.BOT.value and self.confidence >= min_confidence


@dataclass
class MoodState:
    """每日基础心情 M 的持久状态。"""

    bot_key: str
    local_date: str
    mood_centi: int
    last_update_utc: int
    version: int = 0


@dataclass
class UserRelationState:
    """单用户长期好感 F 与短期偏移 D 的持久状态。"""

    user_key: str
    affinity_centi: int
    offset_centi: int = 0
    offset_local_date: str = ""
    offset_update_utc: int = 0
    active_days: int = 0
    valid_interactions: int = 0
    last_valid_utc: int = 0
    preferred_title: str = ""
    title_opt_in: bool = False
    title_enabled: bool = False
    successful_reply_count: int = 0
    last_title_reply_count: int = 0
    last_attack_utc: int = 0
    reaction: str = Reaction.NONE.value
    reaction_expires_utc: int = 0
    reaction_replies_left: int = 0
    version: int = 0


@dataclass
class DailyUserStats:
    """单用户当日预算与计数。"""

    user_key: str
    local_date: str
    positive_f_used: int = 0
    negative_f_used: int = 0
    positive_m_used: int = 0
    negative_m_used: int = 0
    semantic_positive_count: int = 0
    valid_count: int = 0
    active_bonus_granted: bool = False
    apology_repair_granted: bool = False
    first_valid_utc: int = 0


@dataclass
class Snapshot:
    """一次回复所需的只读状态快照。"""

    user_key: str
    mood_centi: int
    offset_centi: int
    affinity_centi: int
    reaction: str
    reaction_expires_utc: int
    title_allowed: bool
    title_should_use: bool
    effective_centi: int
    local_date: str
    active_days: int
    valid_interactions: int
    is_group: bool


@dataclass
class ApplyResult:
    """engine.apply 的返回值，用于日志与测试断言。"""

    status: str
    snapshot: Optional[Snapshot] = None
    delta_mood_centi: int = 0
    delta_offset_centi: int = 0
    delta_affinity_centi: int = 0
    suppressed: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class AffectEvent:
    """一条待计分事件。

    事件的 ``local_date`` 在消息到达时固定，用于拒绝跨日补扣分。
    """

    event_key: str
    user_key: str
    bot_key: str
    session_id: str
    message_id: str
    event_time_utc: int
    received_time_utc: int
    local_date: str
    classification: Classification
    text_hash: str = ""
    is_group: bool = False
    is_approved: bool = True
    config_version: str = ""
    classifier: str = "rules"


@dataclass(frozen=True)
class EngineConfig:
    """引擎使用的纯数值参数（统一以 centi 为单位）。

    该结构刻意与 SDK 配置模型解耦，便于脱离宿主做纯逻辑测试。
    """

    daily_initial_centi: int = 6000
    global_recovery_half_life_hours: float = 4.0
    user_offset_half_life_hours: float = 2.0
    offset_min_centi: int = -3000
    offset_max_centi: int = 2000
    positive_m_cap_centi: int = 600
    negative_m_cap_centi: int = 600

    initial_affinity_centi: int = 1500
    daily_positive_cap_centi: int = 400
    daily_negative_cap_centi: int = 800
    valid_chat_gain_centi: int = 15
    valid_chat_interval_seconds: int = 600
    active_day_bonus_centi: int = 50
    active_day_required_interactions: int = 2
    active_day_min_span_seconds: int = 600

    title_enter_centi: int = 8500
    title_exit_centi: int = 8000
    title_required_active_days: int = 14
    title_required_interactions: int = 40
    title_requires_opt_in: bool = True
    title_allow_in_groups: bool = False
    title_min_reply_interval: int = 5

    pout_ttl_seconds: int = 180
    soothed_ttl_seconds: int = 180
    hurt_ttl_seconds: int = 1800
    pout_max_replies: int = 2
    soothed_max_replies: int = 1

    duplicate_text_window_seconds: int = 600
    positive_semantic_cooldown_seconds: int = 600
    positive_semantic_daily_count_cap: int = 3
    attack_cooldown_seconds: int = 60
    source_event_max_age_seconds: int = 120
    continuation_window_seconds: int = 120
    apology_repair_window_seconds: int = 86400
    min_confidence: float = 0.8


# 首版分数表（技术路线 §6.3），单位 centi：(ΔM, ΔD, ΔF)
SCORE_TABLE: Dict[str, tuple] = {
    EventCategory.NEUTRAL.value: (0, 0, 0),
    EventCategory.FRIENDLY_CHAT.value: (0, 0, 0),
    EventCategory.PRAISE.value: (200, 300, 30),
    EventCategory.COMFORT.value: (200, 800, 20),
    EventCategory.APOLOGY.value: (100, 600, 10),
    EventCategory.AFFECTIONATE_TEASE.value: (0, 0, 0),
    # A playful moment, not a relationship penalty or a bot-wide bad mood.
    EventCategory.JEALOUSY_CANDIDATE.value: (0, -150, 0),
    EventCategory.CRITICISM.value: (0, 0, 0),
    EventCategory.MILD_ATTACK.value: (-200, -800, -100),
    EventCategory.SEVERE_ATTACK.value: (-400, -1500, -400),
}

# 允许产生「有效交流」进度的分类：真实对她说的话，攻击类不计。
VALID_CHAT_CATEGORIES = frozenset(
    {
        EventCategory.FRIENDLY_CHAT.value,
        EventCategory.PRAISE.value,
        EventCategory.COMFORT.value,
        EventCategory.APOLOGY.value,
        EventCategory.AFFECTIONATE_TEASE.value,
        EventCategory.JEALOUSY_CANDIDATE.value,
        EventCategory.CRITICISM.value,
    }
)

# 需要占用「正向语义奖励」次数额度的分类。
POSITIVE_SEMANTIC_CATEGORIES = frozenset(
    {
        EventCategory.PRAISE.value,
        EventCategory.COMFORT.value,
        EventCategory.APOLOGY.value,
    }
)

ATTACK_CATEGORIES = frozenset(
    {
        EventCategory.MILD_ATTACK.value,
        EventCategory.SEVERE_ATTACK.value,
    }
)


def mood_affinity_coefficient(affinity_centi: int, *, negative: bool = False) -> float:
    """Scale mood effects by the relationship before the event is applied.

    Unfamiliar users have less influence; close users' positive interactions
    matter more. Negative effects are never amplified above the previous base
    amount, which limits the impact of an incorrectly classified message.
    """
    if affinity_centi < 2000:
        factor = 0.5
    elif affinity_centi < 4000:
        factor = 0.75
    elif affinity_centi < 6000:
        factor = 1.0
    elif affinity_centi < 8000:
        factor = 1.2
    else:
        factor = 1.4
    return min(factor, 1.0) if negative else factor


def positive_decay_coefficient(affinity_centi: int) -> float:
    """返回好感正向增量的递减系数（按更新前 F 计算）。

    Args:
        affinity_centi: 更新前的好感值（centi）。

    Returns:
        float: 1.0 / 0.6 / 0.3。
    """

    if affinity_centi < 6000:
        return 1.0
    if affinity_centi < 8000:
        return 0.6
    return 0.3


def mood_level(effective_centi: int) -> str:
    """把有效心情 E（万分之一单位）映射为离散情绪段。

    Args:
        effective_centi: E * 100 的整数表示（0..10000）。

    Returns:
        str: 情绪分段标识。
    """

    if effective_centi < 2000:
        return "low"
    if effective_centi < 4000:
        return "gloomy"
    if effective_centi < 6500:
        return "calm"
    if effective_centi < 8500:
        return "happy"
    return "delighted"


def affinity_level(affinity_centi: int) -> str:
    """把长期好感 F（万分之一单位）映射为关系分段。

    Args:
        affinity_centi: F * 100 的整数表示（0..10000）。

    Returns:
        str: 关系分段标识。
    """

    if affinity_centi < 2000:
        return "stranger"
    if affinity_centi < 4000:
        return "acquaintance"
    if affinity_centi < 6000:
        return "familiar"
    if affinity_centi < 8000:
        return "close"
    return "intimate"
