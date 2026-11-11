# 发布前验证（2026-11-11）

274项通过、0跳过；其中 15 项宿主 WebUI 契约测试此前因路径写死而永久跳过，现按 MAIBOT_HOST_ROOT 或 ../MaiBot 布局解析。未启动真实宿主/QQ。

测试环境：Windows、CPython 3.13.17、本机部署的 SDK 依赖环境。没有据此声明 Linux、macOS、Docker、GPU 或所有模型服务已实测。静态检查包括 Python 语法、JSON、示例 TOML 和已知个人信息扫描；不能保证所有潜在隐私风险都可被正则发现。
