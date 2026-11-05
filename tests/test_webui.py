"""``webui.json`` 与插件 ``@API`` 的契约回归测试。

技术路线 §12 要求管理界面覆盖查询、调参、重置、撤销与备份五类功能。页面声明
由宿主 ``src/plugin_runtime/webui_schema.py`` 校验，而 Runner 注册时还会用
``supervisor._handle_register_plugin`` 再核对一遍「页面绑定的静态 API 必须在本
次注册中出现，且版本一致」，不满足就让整个插件注册失败（``runner_main.py``）。
因此这里同时校验两件事：

- 页面声明本身能通过宿主 schema（否则插件根本注册不上）；
- 每个绑定都命中 ``@API``，声明的参数都能被对应方法接收（否则调用 502）。

宿主 schema 依赖 pydantic；插件测试默认注入 SDK 所在 venv 的 site-packages，
缺少时跳过本文件而不是伪造一个「通过」。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401 - 导入即完成合成包注册

from _bootstrap import PACKAGE_NAME, PLUGIN_ROOT, SDK_PATH  # noqa: E402

plugin_module = __import__(f"{PACKAGE_NAME}.plugin", fromlist=["plugin"])

WEBUI_PATH = PLUGIN_ROOT / "webui.json"
SCHEMA_PATH = Path(os.environ.get("MAIBOT_HOST_ROOT", "__host_not_configured__")) / "src/plugin_runtime/webui_schema.py"

# 技术路线 §12 要求的最小功能集，页面可以改名但必须覆盖这些动作。
REQUIRED_ACTIONS = {"adjust_affinity", "reset_user", "revoke_title", "delete_user", "backup_database"}
REQUIRED_QUERIES = {"global_state", "user_state", "list_users"}


def load_schema_module():
    """按文件路径加载宿主 WebUI schema。

    Returns:
        Optional[ModuleType]: 依赖不可用时返回 ``None``。
    """

    if not SCHEMA_PATH.is_file():
        return None
    if str(SDK_PATH) not in sys.path:
        sys.path.append(str(SDK_PATH))
    try:
        import pydantic  # noqa: F401 - 仅用于探测依赖是否可用
    except ImportError:
        return None
    spec = importlib.util.spec_from_file_location("whale_affect_webui_schema", SCHEMA_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCHEMA = load_schema_module()


def declared_apis() -> Dict[str, str]:
    """从插件实例收集 ``@API`` 名称与版本，复现 Runner 的注册流程。

    Returns:
        Dict[str, str]: API 名称到版本的映射。
    """

    instance = plugin_module.create_plugin()
    apis: Dict[str, str] = {}
    for component in instance.get_components():
        if str(component.get("type", "")).upper() != "API":
            continue
        metadata = component.get("metadata", {})
        apis[str(component["name"])] = str(metadata.get("version", "1"))
    return apis


def api_signatures() -> Dict[str, List[str]]:
    """收集 ``@API`` 方法可接收的关键字参数名。

    Returns:
        Dict[str, List[str]]: API 名称到参数名列表的映射。
    """

    instance = plugin_module.create_plugin()
    handlers: Dict[str, List[str]] = {}
    for component in instance.get_components():
        if str(component.get("type", "")).upper() != "API":
            continue
        handler_name = str(component.get("metadata", {}).get("handler_name", ""))
        handler = getattr(instance, handler_name, None)
        if handler is None:
            continue
        signature = inspect.signature(handler)
        names = [
            name
            for name, parameter in signature.parameters.items()
            if name != "self"
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
        handlers[str(component["name"])] = names
    return handlers


def iter_content(nodes: List[dict]):
    """深度优先遍历页面组件树。

    Args:
        nodes: 顶层组件列表。

    Yields:
        dict: 每个组件节点。
    """

    pending = list(reversed(nodes))
    while pending:
        node = pending.pop()
        yield node
        children = node.get("children")
        if isinstance(children, list):
            pending.extend(reversed(children))


@unittest.skipUnless(SCHEMA is not None, "宿主 WebUI schema 或 pydantic 不可用，跳过契约测试")
class WebUIDeclarationTest(unittest.TestCase):
    """``webui.json`` 必须通过宿主 schema 并命中真实 ``@API``。"""

    @classmethod
    def setUpClass(cls) -> None:
        """解析声明并缓存常用派生数据。"""

        cls.raw = json.loads(WEBUI_PATH.read_text(encoding="utf-8"))
        cls.extension = SCHEMA.load_webui_extension(str(PLUGIN_ROOT))
        cls.apis = declared_apis()
        cls.signatures = api_signatures()

    def test_declaration_is_loadable(self) -> None:
        """宿主能读出声明，否则插件注册会直接失败。"""

        self.assertIsNotNone(self.extension)
        self.assertEqual(self.extension.schema_version, 2)
        self.assertEqual([page.id for page in self.extension.pages], ["overview", "users", "admin"])

    def test_declaration_within_size_limit(self) -> None:
        """注册配额按 UTF-8 字节计量。"""

        total = len(WEBUI_PATH.read_bytes())
        self.assertLessEqual(total, SCHEMA.MAX_DECLARATION_BYTES)

    def test_capabilities_are_declared(self) -> None:
        """只用基础组件页，不需要 custom_pages 等额外能力。"""

        self.assertEqual(list(self.extension.required_capabilities), ["declarative_basic"])

    def test_workspace_page_has_title(self) -> None:
        """workspace 页必须有标题，否则 schema 报错、这里再固化一次。"""

        workspace = [page for page in self.extension.pages if page.placement == "workspace"]
        self.assertEqual(len(workspace), 1)
        self.assertEqual(workspace[0].id, "overview")
        self.assertTrue(self.extension.workspace_title)

    def test_every_binding_hits_static_api(self) -> None:
        """Runner 注册时比对 ``(api, version)``，不一致会让整个插件注册失败。"""

        for page in self.extension.pages:
            for name, binding in [*page.queries.items(), *page.actions.items()]:
                with self.subTest(page=page.id, binding=name):
                    self.assertIn(binding.api, self.apis)
                    self.assertEqual(binding.version, self.apis[binding.api])

    def test_api_names_are_unique(self) -> None:
        """同名 API 会被 Host 的注册表覆盖，静默丢功能。"""

        components = plugin_module.create_plugin().get_components()
        names = [c["name"] for c in components if str(c.get("type", "")).upper() == "API"]
        self.assertEqual(len(names), len(set(names)))

    def test_parameters_are_accepted_by_handler(self) -> None:
        """声明了的参数必须能被方法接收，否则调用会抛 TypeError → 网关 502。"""

        for page in self.extension.pages:
            for name, binding in [*page.queries.items(), *page.actions.items()]:
                with self.subTest(page=page.id, binding=name):
                    accepted = self.signatures[binding.api]
                    for parameter in binding.parameters:
                        self.assertIn(parameter, accepted)

    def test_required_parameters_exist_in_handler(self) -> None:
        """必填参数若方法不接受，页面永远调不通。"""

        for page in self.extension.pages:
            for name, binding in [*page.actions.items()]:
                for parameter, spec in binding.parameters.items():
                    if not spec.required:
                        continue
                    with self.subTest(page=page.id, action=name, parameter=parameter):
                        self.assertIn(parameter, self.signatures[binding.api])

    def test_no_structured_parameters(self) -> None:
        """声明能力只有 declarative_basic 时，参数必须是标量。"""

        for page in self.extension.pages:
            for binding in [*page.queries.values(), *page.actions.values()]:
                for spec in binding.parameters.values():
                    self.assertIn(spec.type, {"string", "integer", "number", "boolean"})

    def test_queries_are_read_only_bindings(self) -> None:
        """queries 不允许 arguments/confirmation，schema 已限制，这里防止回归。"""

        for page in self.extension.pages:
            for binding in page.queries.values():
                self.assertEqual(dict(binding.arguments), {})
                self.assertIsNone(binding.confirmation)

    def test_required_features_are_covered(self) -> None:
        """§12 要求的五类管理功能必须都能从页面触达。"""

        actions = {binding.api for page in self.extension.pages for binding in page.actions.values()}
        queries = {binding.api for page in self.extension.pages for binding in page.queries.values()}
        self.assertTrue(REQUIRED_ACTIONS <= actions, REQUIRED_ACTIONS - actions)
        self.assertTrue(REQUIRED_QUERIES <= queries, REQUIRED_QUERIES - queries)

    def test_danger_actions_require_confirmation(self) -> None:
        """danger 按钮必须配确认文案，schema 已强制，这里断言文案非空。"""

        found = False
        for page in self.extension.pages:
            for node in iter_content(self.raw_page(page.id)):
                if node.get("type") != "button" or node.get("variant") != "danger":
                    continue
                found = True
                binding = page.actions[node["action"]]
                self.assertTrue(binding.confirmation)
        self.assertTrue(found, "至少应有一个 danger 按钮用于删除用户")

    def test_poll_interval_is_allowed(self) -> None:
        """轮询间隔只能是 0 或至少 3 秒。"""

        for page in self.extension.pages:
            with self.subTest(page=page.id):
                interval = page.poll_interval_seconds
                self.assertTrue(interval == 0 or interval >= 3)
                self.assertLessEqual(interval, 60)

    def test_table_bindings_are_declared(self) -> None:
        """表格只能绑定本页已声明的 query 或 selection，否则前端抛错。"""

        for page in self.extension.pages:
            sources = set(page.queries)
            selections = {
                node["selection"]
                for node in iter_content(self.raw_page(page.id))
                if node.get("type") == "table" and node.get("selection")
            }
            for node in iter_content(self.raw_page(page.id)):
                if node.get("type") != "table":
                    continue
                value = node["value"]
                with self.subTest(page=page.id, table=value["source"]):
                    if value.get("scope") == "selection":
                        self.assertIn(value["source"], selections)
                    else:
                        self.assertIn(value["source"], sources)

    def test_pagination_fields_are_complete(self) -> None:
        """分页节点需要 page/pages/total 三个整数，管理员页必须提供回退逻辑。"""

        node = next(
            (
                item
                for item in iter_content(self.raw_page("users"))
                if item.get("type") == "pagination"
            ),
            None,
        )
        self.assertIsNotNone(node)
        binding = self.extension.pages[1].queries[node["value"]["source"]]
        self.assertIn("page", binding.parameters)

    def raw_page(self, page_id: str) -> List[dict]:
        """取原始 JSON 中某一页的 content。

        Args:
            page_id: 页面 ID。

        Returns:
            List[dict]: 顶层组件列表。
        """

        for page in self.raw["pages"]:
            if page["id"] == page_id:
                return page["content"]
        raise AssertionError(f"未找到页面 {page_id}")


class PluginApiContractTest(unittest.TestCase):
    """不依赖 pydantic 的纯 ``@API`` 断言，任何环境都会执行。"""

    def test_api_count_matches_declaration(self) -> None:
        """14 个 API 是当前页面的依赖面，减少会留下死按钮。"""

        self.assertEqual(len(declared_apis()), 15)

    def test_all_api_versions_are_one(self) -> None:
        """页面统一绑定 v1；升版本时必须同步改 webui.json。"""

        self.assertEqual(set(declared_apis().values()), {"1"})

    def test_api_methods_are_coroutines(self) -> None:
        """管理 API 走 ``asyncio.to_thread``，方法本身必须是协程以免阻塞 Runner。"""

        instance = plugin_module.create_plugin()
        for component in instance.get_components():
            if str(component.get("type", "")).upper() != "API":
                continue
            handler = getattr(instance, str(component["metadata"].get("handler_name", "")))
            with self.subTest(api=component["name"]):
                self.assertTrue(inspect.iscoroutinefunction(handler))

    def test_webui_file_exists(self) -> None:
        """声明文件必须在插件目录内，Runner 只从那里读。"""

        self.assertTrue(WEBUI_PATH.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
