"""小鲸鱼心情与好感度插件的配置模型。

配置分区与默认值严格对应技术路线附录 C：
plugin / mood / affinity / reactions / scoring / classifier / privacy / admin。
所有字段都必须有默认值，否则 Runner 无法生成默认配置文件。
"""

from typing import List

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件总开关与灰度模式。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description="是否启用插件（关闭后完全恢复原回复逻辑）",
        json_schema_extra={"label": "启用插件", "order": 0},
    )
    shadow_mode: bool = Field(
        default=True,
        description="影子模式：只计算并记录状态，不向回复注入任何提示块",
        json_schema_extra={"label": "影子模式", "order": 1},
    )
    config_version: str = Field(
        default="0.2.0",
        description="配置结构版本，缺少或被清空时插件拒绝启用",
        json_schema_extra={"label": "配置版本", "order": 2, "disabled": True},
    )


class MoodSectionConfig(PluginConfigBase):
    """每日基础心情 M 与每用户短期偏移 D 的参数。"""

    __ui_label__ = "心情"
    __ui_icon__ = "heart"
    __ui_order__ = 1

    timezone: str = Field(
        default="Asia/Shanghai",
        description="本地日期所用时区；修改后建议停用插件再迁移",
        json_schema_extra={"label": "时区", "order": 0},
    )
    daily_initial: int = Field(
        default=60,
        ge=0,
        le=100,
        description="每日基础心情初值（0-100）",
        json_schema_extra={"label": "每日初始心情", "order": 1, "step": 1},
    )
    global_recovery_half_life_hours: float = Field(
        default=4.0,
        gt=0,
        description="基础心情向初值自然恢复的半衰期（小时）",
        json_schema_extra={"label": "心情恢复半衰期", "order": 2, "step": 0.5},
    )
    user_offset_half_life_hours: float = Field(
        default=2.0,
        gt=0,
        description="每用户短期偏移向 0 自然恢复的半衰期（小时）",
        json_schema_extra={"label": "偏移恢复半衰期", "order": 3, "step": 0.5},
    )
    offset_min: int = Field(
        default=-30,
        description="短期偏移 D 的下限",
        json_schema_extra={"label": "偏移下限", "order": 4, "step": 1},
    )
    offset_max: int = Field(
        default=20,
        description="短期偏移 D 的上限",
        json_schema_extra={"label": "偏移上限", "order": 5, "step": 1},
    )
    per_user_daily_positive_global_cap: int = Field(
        default=6,
        ge=0,
        description="单个用户每日对基础心情 M 的正向贡献上限",
        json_schema_extra={"label": "单人日正向 M 上限", "order": 6, "step": 1},
    )
    per_user_daily_negative_global_cap: int = Field(
        default=6,
        ge=0,
        description="单个用户每日对基础心情 M 的负向贡献上限（绝对值）",
        json_schema_extra={"label": "单人日负向 M 上限", "order": 7, "step": 1},
    )


class AffinitySectionConfig(PluginConfigBase):
    """每用户长期好感 F 与亲昵称呼规则。"""

    __ui_label__ = "好感"
    __ui_icon__ = "users"
    __ui_order__ = 2

    initial: int = Field(
        default=15,
        ge=0,
        le=100,
        description="新用户初始好感（只影响此后创建的用户）",
        json_schema_extra={"label": "初始好感", "order": 0, "step": 1},
    )
    daily_positive_cap: float = Field(
        default=4.0,
        ge=0,
        description="每用户每日好感正向增长总上限",
        json_schema_extra={"label": "日正向 F 上限", "order": 1, "step": 0.5},
    )
    daily_negative_cap: float = Field(
        default=8.0,
        ge=0,
        description="每用户每日好感负向损失总上限（绝对值）",
        json_schema_extra={"label": "日负向 F 上限", "order": 2, "step": 0.5},
    )
    valid_chat_gain: float = Field(
        default=0.15,
        ge=0,
        description="单次有效交流的好感增量",
        json_schema_extra={"label": "有效交流增益", "order": 3, "step": 0.05},
    )
    valid_chat_interval_seconds: int = Field(
        default=600,
        gt=0,
        description="同一用户两次有效交流计分的最小间隔（秒）",
        json_schema_extra={"label": "有效交流间隔", "order": 4, "step": 60},
    )
    active_day_bonus: float = Field(
        default=0.5,
        ge=0,
        description="活跃日额外好感增量（同日只给一次）",
        json_schema_extra={"label": "活跃日奖励", "order": 5, "step": 0.1},
    )
    active_day_required_interactions: int = Field(
        default=2,
        ge=1,
        description="判定活跃日所需当日有效交流次数",
        json_schema_extra={"label": "活跃日所需交流数", "order": 6, "step": 1},
    )
    active_day_min_span_seconds: int = Field(
        default=600,
        gt=0,
        description="判定活跃日所需的最小首末交流间隔（秒）",
        json_schema_extra={"label": "活跃日最小间隔", "order": 7, "step": 60},
    )
    decay_when_absent: bool = Field(
        default=False,
        description="长时间不聊天是否扣好感（首版关闭）",
        json_schema_extra={"label": "不活跃衰减", "order": 8},
    )
    title_enter_threshold: int = Field(
        default=85,
        ge=0,
        le=100,
        description="启用亲昵称呼所需的好感阈值",
        json_schema_extra={"label": "称呼启用阈值", "order": 9, "step": 1},
    )
    title_exit_threshold: int = Field(
        default=80,
        ge=0,
        le=100,
        description="停用亲昵称呼的好感阈值（必须小于启用阈值）",
        json_schema_extra={"label": "称呼停用阈值", "order": 10, "step": 1},
    )
    title_required_active_days: int = Field(
        default=14,
        ge=0,
        description="启用称呼所需的活跃天数",
        json_schema_extra={"label": "称呼所需活跃日", "order": 11, "step": 1},
    )
    title_required_interactions: int = Field(
        default=40,
        ge=0,
        description="启用称呼所需的累计有效交流次数",
        json_schema_extra={"label": "称呼所需交流数", "order": 12, "step": 1},
    )
    title_requires_opt_in: bool = Field(
        default=True,
        description="是否必须由用户明确同意才可使用称呼",
        json_schema_extra={"label": "需要用户同意", "order": 13},
    )
    title_allow_in_groups: bool = Field(
        default=False,
        description="是否允许在群聊使用亲昵称呼",
        json_schema_extra={"label": "群聊允许称呼", "order": 14},
    )
    title_min_reply_interval: int = Field(
        default=5,
        ge=1,
        description="每 N 次普通回复最多使用一次称呼",
        json_schema_extra={"label": "称呼最小回复间隔", "order": 15, "step": 1},
    )


class ReactionsSectionConfig(PluginConfigBase):
    """临时反应 pout / soothed / hurt 的存活时间。"""

    __ui_label__ = "临时反应"
    __ui_icon__ = "zap"
    __ui_order__ = 3

    pout_ttl_seconds: int = Field(
        default=180,
        gt=0,
        description="赌气（pout）最长持续时间",
        json_schema_extra={"label": "赌气时长", "order": 0, "step": 30},
    )
    soothed_ttl_seconds: int = Field(
        default=180,
        gt=0,
        description="被哄好（soothed）最长持续时间",
        json_schema_extra={"label": "缓和时长", "order": 1, "step": 30},
    )
    hurt_ttl_seconds: int = Field(
        default=1800,
        gt=0,
        description="受伤（hurt）最长持续时间",
        json_schema_extra={"label": "受伤时长", "order": 2, "step": 60},
    )
    pout_max_replies: int = Field(
        default=2,
        ge=1,
        description="赌气最多消耗的回复次数（与时间先到为准）",
        json_schema_extra={"label": "赌气回复次数", "order": 3, "step": 1},
    )
    soothed_max_replies: int = Field(
        default=1,
        ge=1,
        description="缓和最多消耗的回复次数（与时间先到为准）",
        json_schema_extra={"label": "缓和回复次数", "order": 4, "step": 1},
    )


class ScoringSectionConfig(PluginConfigBase):
    """去重、冷却与事件新鲜度参数。"""

    __ui_label__ = "计分约束"
    __ui_icon__ = "shield"
    __ui_order__ = 4

    duplicate_text_window_seconds: int = Field(
        default=600,
        gt=0,
        description="同一用户相同文本的去重窗口",
        json_schema_extra={"label": "文本去重窗口", "order": 0, "step": 60},
    )
    positive_semantic_cooldown_seconds: int = Field(
        default=600,
        gt=0,
        description="正向语义奖励同类冷却时间",
        json_schema_extra={"label": "正向语义冷却", "order": 1, "step": 60},
    )
    positive_semantic_daily_count_cap: int = Field(
        default=3,
        ge=0,
        description="每用户每日正向语义奖励次数上限",
        json_schema_extra={"label": "日正向语义次数", "order": 2, "step": 1},
    )
    attack_cooldown_seconds: int = Field(
        default=60,
        gt=0,
        description="攻击计分冷却（冷却期仍可表达不舒服，但不重复扣分）",
        json_schema_extra={"label": "攻击冷却", "order": 3, "step": 10},
    )
    source_event_max_age_seconds: int = Field(
        default=120,
        gt=0,
        description="来源事件的最大新鲜度，过期分类不补扣分",
        json_schema_extra={"label": "事件最大时龄", "order": 4, "step": 30},
    )
    continuation_window_seconds: int = Field(
        default=120,
        gt=0,
        description="连续对话延续判定窗口",
        json_schema_extra={"label": "连续对话窗口", "order": 5, "step": 30},
    )


class ClassifierSectionConfig(PluginConfigBase):
    """异步语义分类（首版默认关闭）。"""

    __ui_label__ = "语义分类"
    __ui_icon__ = "cpu"
    __ui_order__ = 5

    llm_enabled: bool = Field(
        default=False,
        description="是否启用异步模型分类（规则版稳定后再开启）",
        json_schema_extra={"label": "启用模型分类", "order": 0},
    )
    model_name: str = Field(
        default="deepseek-flash-affect",
        description="使用的模型配置别名，不是供应商 model_identifier",
        json_schema_extra={"label": "模型别名", "order": 1},
    )
    min_confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="允许语义奖惩的最低置信度",
        json_schema_extra={"label": "最小置信度", "order": 2, "step": 0.05},
    )
    timeout_seconds: float = Field(
        default=8.0,
        gt=0,
        description="单次分类调用超时，不做插件级重试",
        json_schema_extra={"label": "超时", "order": 3, "step": 1},
    )
    max_concurrency: int = Field(
        default=1,
        ge=1,
        description="分类并发上限",
        json_schema_extra={"label": "并发上限", "order": 4, "step": 1},
    )
    queue_capacity: int = Field(
        default=100,
        ge=1,
        description="分类候选队列容量，满后降级为 neutral",
        json_schema_extra={"label": "队列容量", "order": 5, "step": 10},
    )
    global_calls_per_minute: int = Field(
        default=6,
        ge=1,
        description="全局每分钟分类调用上限",
        json_schema_extra={"label": "全局每分钟调用", "order": 6, "step": 1},
    )
    per_user_interval_seconds: int = Field(
        default=30,
        gt=0,
        description="同一用户两次分类调用的最小间隔",
        json_schema_extra={"label": "单用户调用间隔", "order": 7, "step": 5},
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        description="分类模型温度",
        json_schema_extra={"label": "温度", "order": 8, "step": 0.1},
    )
    max_tokens: int = Field(
        default=180,
        ge=32,
        description="分类模型最大输出 token",
        json_schema_extra={"label": "最大输出", "order": 9, "step": 10},
    )


class PrivacySectionConfig(PluginConfigBase):
    """诊断文本保留与事件清理。"""

    __ui_label__ = "隐私"
    __ui_icon__ = "lock"
    __ui_order__ = 6

    diagnostic_text_enabled: bool = Field(
        default=False,
        description="是否保留脱敏诊断文本（默认只存哈希）",
        json_schema_extra={"label": "保留诊断文本", "order": 0},
    )
    event_retention_days: int = Field(
        default=30,
        ge=1,
        description="事件日志保留天数",
        json_schema_extra={"label": "事件保留天数", "order": 1, "step": 1},
    )


class AdminSectionConfig(PluginConfigBase):
    """维护者身份（与好感度完全无关）。"""

    __ui_label__ = "维护者"
    __ui_icon__ = "key"
    __ui_order__ = 7

    platform: str = Field(
        default="qq",
        description="维护者所在平台",
        json_schema_extra={"label": "平台", "order": 0},
    )
    bot_account_id: str = Field(
        default="",
        description="机器人账号 ID，用于确认管理命令确实发给了本人",
        json_schema_extra={"label": "机器人账号", "order": 1},
    )
    user_ids: List[str] = Field(
        default_factory=list,
        description="允许执行管理操作的 QQ 号列表",
        json_schema_extra={"label": "维护者 QQ 列表", "order": 2},
    )


class WhaleAffectConfig(PluginConfigBase):
    """小鲸鱼心情与好感度完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    mood: MoodSectionConfig = Field(default_factory=MoodSectionConfig)
    affinity: AffinitySectionConfig = Field(default_factory=AffinitySectionConfig)
    reactions: ReactionsSectionConfig = Field(default_factory=ReactionsSectionConfig)
    scoring: ScoringSectionConfig = Field(default_factory=ScoringSectionConfig)
    classifier: ClassifierSectionConfig = Field(default_factory=ClassifierSectionConfig)
    privacy: PrivacySectionConfig = Field(default_factory=PrivacySectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)


def validate_config_sections(config: WhaleAffectConfig) -> List[str]:
    """校验分段之间的单调性与范围约束。

    Args:
        config: 已构造的强类型配置实例。

    Returns:
        List[str]: 校验失败原因列表；为空表示通过。
    """

    problems: List[str] = []

    if config.affinity.title_exit_threshold >= config.affinity.title_enter_threshold:
        problems.append(
            "affinity.title_exit_threshold 必须小于 affinity.title_enter_threshold"
            f"（当前 {config.affinity.title_exit_threshold} >= {config.affinity.title_enter_threshold}）"
        )
    if config.mood.offset_min >= config.mood.offset_max:
        problems.append(
            f"mood.offset_min 必须小于 mood.offset_max（当前 {config.mood.offset_min} >= {config.mood.offset_max}）"
        )
    if not 0 <= config.mood.daily_initial <= 100:
        problems.append("mood.daily_initial 必须在 0..100 之间")
    if not 0 <= config.affinity.initial <= 100:
        problems.append("affinity.initial 必须在 0..100 之间")
    if config.affinity.active_day_required_interactions < 2:
        problems.append("affinity.active_day_required_interactions 至少为 2，否则活跃日无意义")
    if config.affinity.active_day_min_span_seconds < config.affinity.valid_chat_interval_seconds:
        problems.append("affinity.active_day_min_span_seconds 不应小于 valid_chat_interval_seconds")

    normalized_admin_ids = [str(item).strip() for item in config.admin.user_ids if str(item).strip()]
    if len(normalized_admin_ids) != len(config.admin.user_ids):
        problems.append("admin.user_ids 中存在空白项")
    if not str(config.admin.bot_account_id).strip():
        problems.append("admin.bot_account_id 不能为空")
    if not str(config.admin.platform).strip():
        problems.append("admin.platform 不能为空")

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(config.mood.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"mood.timezone 不是可用的 IANA 时区: {config.mood.timezone}")

    return problems
