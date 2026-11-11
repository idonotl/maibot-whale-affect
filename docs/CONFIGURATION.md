# 配置字段参考

由本次发布的 config.py 提取；最终校验以代码与宿主配置 UI 为准。不要把实际配置提交到仓库。

| 配置类.字段 | 类型 | 默认值/工厂 | 说明 |
|---|---|---|---|
| PluginSectionConfig.enabled | `bool` | `False` | 是否启用插件（关闭后完全恢复原回复逻辑） |
| PluginSectionConfig.shadow_mode | `bool` | `True` | 影子模式：只计算并记录状态，不向回复注入任何提示块 |
| PluginSectionConfig.config_version | `str` | `'0.2.0'` | 配置结构版本，缺少或被清空时插件拒绝启用 |
| MoodSectionConfig.timezone | `str` | `'Asia/Shanghai'` | 本地日期所用时区；修改后建议停用插件再迁移 |
| MoodSectionConfig.daily_initial | `int` | `60` | 每日基础心情初值（0-100） |
| MoodSectionConfig.global_recovery_half_life_hours | `float` | `4.0` | 基础心情向初值自然恢复的半衰期（小时） |
| MoodSectionConfig.user_offset_half_life_hours | `float` | `2.0` | 每用户短期偏移向 0 自然恢复的半衰期（小时） |
| MoodSectionConfig.offset_min | `int` | `-30` | 短期偏移 D 的下限 |
| MoodSectionConfig.offset_max | `int` | `20` | 短期偏移 D 的上限 |
| MoodSectionConfig.per_user_daily_positive_global_cap | `int` | `6` | 单个用户每日对基础心情 M 的正向贡献上限 |
| MoodSectionConfig.per_user_daily_negative_global_cap | `int` | `6` | 单个用户每日对基础心情 M 的负向贡献上限（绝对值） |
| AffinitySectionConfig.initial | `int` | `15` | 新用户初始好感（只影响此后创建的用户） |
| AffinitySectionConfig.daily_positive_cap | `float` | `4.0` | 每用户每日好感正向增长总上限 |
| AffinitySectionConfig.daily_negative_cap | `float` | `8.0` | 每用户每日好感负向损失总上限（绝对值） |
| AffinitySectionConfig.valid_chat_gain | `float` | `0.15` | 单次有效交流的好感增量 |
| AffinitySectionConfig.valid_chat_interval_seconds | `int` | `600` | 同一用户两次有效交流计分的最小间隔（秒） |
| AffinitySectionConfig.active_day_bonus | `float` | `0.5` | 活跃日额外好感增量（同日只给一次） |
| AffinitySectionConfig.active_day_required_interactions | `int` | `2` | 判定活跃日所需当日有效交流次数 |
| AffinitySectionConfig.active_day_min_span_seconds | `int` | `600` | 判定活跃日所需的最小首末交流间隔（秒） |
| AffinitySectionConfig.decay_when_absent | `bool` | `False` | 长时间不聊天是否扣好感（首版关闭） |
| AffinitySectionConfig.title_enter_threshold | `int` | `85` | 启用亲昵称呼所需的好感阈值 |
| AffinitySectionConfig.title_exit_threshold | `int` | `80` | 停用亲昵称呼的好感阈值（必须小于启用阈值） |
| AffinitySectionConfig.title_required_active_days | `int` | `14` | 启用称呼所需的活跃天数 |
| AffinitySectionConfig.title_required_interactions | `int` | `40` | 启用称呼所需的累计有效交流次数 |
| AffinitySectionConfig.title_requires_opt_in | `bool` | `True` | 是否必须由用户明确同意才可使用称呼 |
| AffinitySectionConfig.title_allow_in_groups | `bool` | `False` | 是否允许在群聊使用亲昵称呼 |
| AffinitySectionConfig.title_min_reply_interval | `int` | `5` | 每 N 次普通回复最多使用一次称呼 |
| ReactionsSectionConfig.pout_ttl_seconds | `int` | `180` | 赌气（pout）最长持续时间 |
| ReactionsSectionConfig.soothed_ttl_seconds | `int` | `180` | 被哄好（soothed）最长持续时间 |
| ReactionsSectionConfig.hurt_ttl_seconds | `int` | `1800` | 受伤（hurt）最长持续时间 |
| ReactionsSectionConfig.pout_max_replies | `int` | `2` | 赌气最多消耗的回复次数（与时间先到为准） |
| ReactionsSectionConfig.soothed_max_replies | `int` | `1` | 缓和最多消耗的回复次数（与时间先到为准） |
| ScoringSectionConfig.duplicate_text_window_seconds | `int` | `600` | 同一用户相同文本的去重窗口 |
| ScoringSectionConfig.positive_semantic_cooldown_seconds | `int` | `600` | 正向语义奖励同类冷却时间 |
| ScoringSectionConfig.positive_semantic_daily_count_cap | `int` | `3` | 每用户每日正向语义奖励次数上限 |
| ScoringSectionConfig.attack_cooldown_seconds | `int` | `60` | 攻击计分冷却（冷却期仍可表达不舒服，但不重复扣分） |
| ScoringSectionConfig.source_event_max_age_seconds | `int` | `120` | 来源事件的最大新鲜度，过期分类不补扣分 |
| ScoringSectionConfig.continuation_window_seconds | `int` | `120` | 连续对话延续判定窗口 |
| ScoringSectionConfig.reply_echo_window_seconds | `int` | `600` | 同一条已完整送达的回复仍视为「刚说过」的窗口 |
| ClassifierSectionConfig.llm_enabled | `bool` | `False` | 是否启用异步模型分类（规则版稳定后再开启） |
| ClassifierSectionConfig.model_task | `str` | `'fast_model'` | 使用的 MaiBot 模型任务，按其候选列表切换 |
| ClassifierSectionConfig.min_confidence | `float` | `0.8` | 允许语义奖惩的最低置信度 |
| ClassifierSectionConfig.timeout_seconds | `float` | `110.0` | 后台分类总等待时间，含候选模型切换，不做插件级重试 |
| ClassifierSectionConfig.max_concurrency | `int` | `1` | 分类并发上限 |
| ClassifierSectionConfig.queue_capacity | `int` | `100` | 分类候选队列容量，满后降级为 neutral |
| ClassifierSectionConfig.global_calls_per_minute | `int` | `6` | 全局每分钟分类调用上限 |
| ClassifierSectionConfig.per_user_interval_seconds | `int` | `30` | 同一用户两次分类调用的最小间隔 |
| ClassifierSectionConfig.temperature | `float` | `0.0` | 分类模型温度 |
| ClassifierSectionConfig.max_tokens | `int` | `180` | 分类模型最大输出 token |
| PrivacySectionConfig.diagnostic_text_enabled | `bool` | `False` | 是否保留脱敏诊断文本（默认只存哈希） |
| PrivacySectionConfig.event_retention_days | `int` | `30` | 事件日志保留天数 |
| AdminSectionConfig.platform | `str` | `'qq'` | 维护者所在平台 |
| AdminSectionConfig.bot_account_id | `str` | `''` | 机器人账号 ID，用于确认管理命令确实发给了本人 |
| AdminSectionConfig.user_ids | `List[str]` | `list` | 允许执行管理操作的 QQ 号列表 |
| WhaleAffectConfig.plugin | `PluginSectionConfig` | `PluginSectionConfig` |  |
| WhaleAffectConfig.mood | `MoodSectionConfig` | `MoodSectionConfig` |  |
| WhaleAffectConfig.affinity | `AffinitySectionConfig` | `AffinitySectionConfig` |  |
| WhaleAffectConfig.reactions | `ReactionsSectionConfig` | `ReactionsSectionConfig` |  |
| WhaleAffectConfig.scoring | `ScoringSectionConfig` | `ScoringSectionConfig` |  |
| WhaleAffectConfig.classifier | `ClassifierSectionConfig` | `ClassifierSectionConfig` |  |
| WhaleAffectConfig.privacy | `PrivacySectionConfig` | `PrivacySectionConfig` |  |
| WhaleAffectConfig.admin | `AdminSectionConfig` | `AdminSectionConfig` |  |
