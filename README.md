# 心情与好感度 · MaiBot 插件

每日心情、每用户好感、短期反应、关系语气和表情建议。插件只影响表达，不训练模型。

> 首次开源预发布，版本 0.2.5。尚未在所有宿主版本及系统实测；不是 DeepSeek 官方插件。

## 快速开始

1. 安装 MaiBot 与其要求的 Python（建议 3.12）。本插件使用 SDK 2.16.0 及以上的接口；旧环境验证基线为 MaiBot 1.3.15，新源码 1.3.19 的完整运行兼容尚待验证。
2. 将本仓库完整克隆到宿主实际的插件目录，确保 `_manifest.json`、`plugin.py` 位于该插件目录根层。Windows、Linux、Docker 要使用各自实际挂载路径，不照抄开发者电脑路径。
3. 在宿主使用的同一 Python 环境运行 `python -m pip install -r requirements.txt`。已有同版本依赖可复用，不要安装到无关的系统 Python。
4. 复制 `config.example.toml` 为 `config.toml`，或通过宿主插件配置页生成实际配置。**先填配置，再显式启用**。
5. 配置 admin.bot_account_id 与 admin.user_ids，先保持 shadow_mode=true，确认模型别名和管理页状态后再开启正式注入。
6. 重启或通过宿主支持的方式重新加载插件，在插件页查看状态和错误。先用隔离测试会话，不要在大群直接测试自动发送。

```sh
git clone https://github.com/idonotl/maibot-whale-affect.git <宿主插件目录>/whale-affect
# 在克隆后的插件目录、宿主 Python 环境执行
python -m pip install -r requirements.txt
```

## 环境与成本

- 需要运行中的 MaiBot 宿主和匹配的 SDK；不是独立运行 `python plugin.py` 的程序。
- 基础状态和 SQLite 不需要 GPU。Windows 缺少 IANA 时区数据库时由 tzdata 提供支持。
- 模型能力复用宿主配置；`deepseek-flash-affect` 等只是示例别名，不是硬编码供应商 API 或附带密钥。请替换成自己配置中可用的模型别名。
- 分类、记忆提炼、摘要、实际生成回复可能消耗所配置模型的额度；本地抽签本身不调用模型。
- 网络、模型和适配器的可用性由使用者环境决定，安装插件不等于已经登录 QQ。

## 使用和限制

配置 admin.bot_account_id 与 admin.user_ids，先保持 shadow_mode=true，确认模型别名和管理页状态后再开启正式注入。

运行数据位于 SDK `ctx.paths.data_dir` 指向的独立目录，通常为 `instance/data/plugins/local.whale-affect/`。源码仓库不包含任何用户记忆、好感数据库、账号配置或登录会话。备份使用 SQLite backup API 或宿主提供的备份操作，不只复制仍在写入的主文件而漏掉 WAL。

关闭功能：先在配置将 `plugin.enabled=false`，重新加载；已有数据不会因关闭开关自动删除。删除和迁移前应备份。不要随意修改插件 ID，否则运行目录、跨插件调用和已有数据不会自动迁移。

## 文档

- [用户与 Agent 接入指南](docs/AGENT_GUIDE.md)：架构、配置步骤、验证、数据与排错。
- [完整配置参考](docs/CONFIGURATION.md)：字段、默认值与说明。
- [贡献指南](CONTRIBUTING.md) 与 [隐私说明](PRIVACY.md)。

## 开发与测试

```sh
python -m unittest discover -s tests -v
```

测试账号只使用合成数据。部分宿主契约测试需要通过 `MAIBOT_HOST_ROOT` 指向兼容 MaiBot 源码；缺失时会明确跳过，不算完整通过。外网和真实 QQ 行为不由单元测试保证。

## 来源与许可

本插件许可证为 `GPL-3.0-or-later`，见 [LICENSE](LICENSE)。相关上游声明如有则保留在 NOTICE.md、UPSTREAM-LICENSE。本项目独立适配 MaiBot，不安装或冒充原 DSH 插件；依赖及模型权重适用其各自许可证。
