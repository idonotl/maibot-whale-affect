# 心情与好感度：用户与 Agent 接入指南

## 1. 安装前检查

先阅读 README、`_manifest.json`、`config.example.toml` 和配置参考。确认宿主版本、Python 环境、实际插件目录、数据目录和模型配置。不要收集或输出密钥，不从使用者原配置复制令牌进 Git。

本地旧版本经历过开发测试；本次公开副本经过默认值和隐私调整，测试结果以仓库发布记录为准。不要把版本范围理解为全平台实测。

## 2. 本插件做什么

每日心情、每用户好感、短期反应、关系语气和表情建议。插件只影响表达，不训练模型。

配置 admin.bot_account_id 与 admin.user_ids，先保持 shadow_mode=true，确认模型别名和管理页状态后再开启正式注入。

管理员、私聊和群名单默认不含任何真实用户。配置类要求非空机器人号时，必须填部署者自己的机器人号后才能加载。启用状态和 dry_run/shadow_mode 是独立概念：前者控制启动，后者控制是否实际生效或提交任务。

## 3. 数据与边界

插件 ID：`local.whale-affect`。不要自行更改 ID。数据目录由 SDK 提供，路径不可硬编码。聊天内容、提炼记忆、关系状态和转写都可能是私人数据；部署者需自行配置保留周期与访问权限。不要把数据目录、模型权重、缓存、日志和真实配置上传源码仓库。

升级前备份；恢复数据前停止写入，核对 schema 和插件 ID。恢复是覆盖，不是自动合并。关掉插件不会自动删除数据库。

## 4. 模型、网络与工具

模型通过宿主别名选择，在管理页核对存在性。学校、OpenAI 兼容服务或其他供应商是否可用取决于宿主配置；本仓库不提供 API 密钥。模型超时、配额不足、权限不足分别检查，不能声称某次调用已成功。

QQ 收发由外部适配器承担。需要 QQ 的功能必须配置适配器并由本人扫码，不由 Agent 代登录。自动功能先限定小范围，避免无人看守时向大群测试。

已声明入口（具体参数、返回结构和可见性以各 Python 文件及 `webui.json` 为准；不是所有 API 都对其他插件公开）：

- `HookHandler`：`chat.receive.after_process`（plugin.py）
- `HookHandler`：`maisaka.planner.before_request`（plugin.py）
- `HookHandler`：`maisaka.replyer.before_request`（plugin.py）
- `HookHandler`：`send_service.after_send`（plugin.py）
- `HookHandler`：`maisaka.planner.before_request`（plugin.py）
- `HookHandler`：`send_service.after_send`（plugin.py）
- `API`：`global_state`（plugin.py）
- `API`：`user_state`（plugin.py）
- `API`：`list_users`（plugin.py）
- `API`：`recent_events`（plugin.py）
- `API`：`list_audit`（plugin.py）
- `API`：`export_user`（plugin.py）
- `API`：`adjust_affinity`（plugin.py）
- `API`：`reset_user`（plugin.py）
- `API`：`revoke_title`（plugin.py）
- `API`：`set_title_opt_in`（plugin.py）
- `API`：`delete_user`（plugin.py）
- `API`：`backup_database`（plugin.py）
- `API`：`runtime_status`（plugin.py）
- `API`：`recent_identities`（plugin.py）

跨插件连接不能假定短名唯一，优先用稳定全名。只有 `public=True` 的 API 才可跨插件调用，并须声明 `api.call` 能力。不要替其他插件扩大权限来消除报错。

## 5. 上线步骤

1. 将源代码放在插件独立目录；不要多套一层解压文件夹。
2. 依赖安装到宿主 Python，复制示例配置并填写自己的账号和白名单。
3. 在模型配置中建立需要的别名。无相关能力时保持对应开关关闭。
4. 启动插件，检查 manifest、依赖和配置错误；先查询状态，不发测试 QQ 消息。
5. 在隔离 WebUI 或用户同意的私聊验证一次；观察成功发送与队列状态的区别。
6. 用户确认后再启用目标群和自动行为。保留恢复配置和数据备份。

## 6. 验收

运行 README 的测试命令，记录通过、失败、跳过数。单元测试是本地合成数据，不保证外部 API 或 QQ 实时可用。不得通过删除失败断言伪造通过。宿主契约测试缺少源码时设置 `MAIBOT_HOST_ROOT`，再运行；保留跳过说明。

实际验收检查：插件 ready；模型别名可用；白名单只含授权对象；本机无日志泄露；关闭插件后宿主可正常聊天。遇到联网失败，不更改系统代理、账号设备或清理登录数据来试错。

## 7. 故障排查

| 症状 | 检查 |
|---|---|
| 不显示插件 | 仓库根层 manifest、宿主扫描目录、版本范围、导入依赖 |
| 配置错误 | 真实机器人号、空白名单项、字段边界与版本 |
| 没反应 | enabled、试运行、授权范围、已过滤的消息是否进入宿主 |
| 模型失败 | 宿主模型别名、额度、模型工具能力与超时日志 |
| 不主动发言 | 空闲、静默、冷却、日上限、等待真人新消息、QQ在线与Planner决定 |
| 记不住 | 是否已提炼、当前场景/用户、词法检索是否命中、衰减及额度 |
| B站失败 | 具体接口、限制、资料范围、可选模型目录及依赖；不要把弹幕当字幕 |

## 8. Agent 修改与报告要求

先读实际实现，不假定示例是 API。禁止用正式聊天做无授权回归；禁止提交真实 config.toml、sqlite、日志或登录状态。新增配置同步文档和示例，迁移 schema 需备份与版本校验，异步热路径避免同步阻塞。

交付时写明：修改文件、依赖和环境要求、配置步骤、测试结果、外网/QQ未验证项、如何关闭和回滚。对于不确定的推断明确标记，不把尝试说成成功。

## 本插件的机制与实际使用

状态包含全局每日心情、每用户短期偏移和长期好感。好感身份包含平台和机器人账号，同一 QQ 用户在相同机器人下跨聊天保留关系；群聊注入须解析真实回复对象，不能按群友继承私人称呼。具体身份键见 engine/repository，不把界面昵称用作主键。

合格消息进入 Planner 后才计分，入站身份观察不计分；Replyer 只注入语气，成功发送后才计回复/称呼等计数。重复消息有去重，正常昵称和引用辱骂不应当作独立攻击；语义分类仍可能出错，管理员应查看并修正。

管理员配置位于 `[admin]`：填本人机器人号、管理者账号。好感阈值、每日额度在 `[affinity]`，心情与恢复在 `[mood]`，语义模型在 `[classifier]`。首次设置 `plugin.enabled=true` 与 `shadow_mode=true` 观察，之后才将 shadow_mode 改为 false。影子模式不会注入语气，不应被理解为完整无写入沙箱。

在测试会话由用户依次发送中性问候、夸奖、安慰和正常纠错，检查状态变化及日志；不要用辱骂真人作为测试。正常意见不同不应扣好感。亲昵称呼还受 opt-in、阈值、活跃天数等限制，不能只调一个数字就保证每轮出现。

管理页包括全局/用户状态、用户列表、调整好感、重置、撤销称呼、删除和备份等，操作需遵守宿主认证。QQ 管理命令另有管理员白名单；不要为解决权限报错添加任意用户。数据包含关系与事件记录；清除用户前先备份。没有 GPU 要求，语义模型属于云端/宿主配置成本。
