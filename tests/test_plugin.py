"""插件生命周期与 Hook 接线测试。

不连接 QQ、不启动 NapCat：用假 ``PluginContext`` 与假的消息能力替换宿主，
只验证「配置注入 → 启动 → Hook 参数解析 → 计分/注入/计数」这条链路本身。

真实宿主行为（Hook 是否触发、kwargs 形状）由阶段 0 探针单独验证并记录在
``interface-contract.md``，本文件不做任何真实聊天断言。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401 - 导入即完成合成包注册

from maibot_sdk.context import PluginContext, PluginPaths  # noqa: E402

from whale_affect_under_test import plugin as plugin_module  # noqa: E402
from whale_affect_under_test import prompt_builder  # noqa: E402

BOT_ACCOUNT = "100000002"
USER_ID = "100000001"
SESSION_ID = "session-private-1"
GROUP_SESSION_ID = "session-group-1"


def message_dict(
    *,
    message_id: str,
    text: str,
    timestamp: int,
    user_id: str = USER_ID,
    group_id: str = "",
    session_id: str = SESSION_ID,
    reply_to: str = "",
) -> dict:
    """构造与本机 Host ``_session_message_to_dict`` 同形的消息字典。

    Args:
        message_id: 消息 ID。
        text: 纯文本内容。
        timestamp: Unix 秒时间戳。
        user_id: 发送者 ID。
        group_id: 群号，空串表示私聊。
        session_id: 会话 ID。
        reply_to: 被回复消息 ID。

    Returns:
        dict: 消息字典。
    """

    return {
        "message_id": message_id,
        "timestamp": str(timestamp),
        "platform": "qq",
        "account_id": BOT_ACCOUNT,
        "scope": "",
        "message_info": {
            "user_info": {"user_id": user_id, "user_nickname": "n", "user_cardname": ""},
            "group_info": {"group_id": group_id, "group_name": "g"} if group_id else None,
            "additional_config": {},
        },
        "raw_message": [],
        "is_mentioned": False,
        "is_at": False,
        "is_emoji": False,
        "is_picture": False,
        "is_command": False,
        "is_notify": False,
        "session_id": session_id,
        "reply_to": reply_to,
        "processed_plain_text": text,
    }


class FakeMessageCapability:
    """替身消息能力，只返回测试预置的消息。"""

    def __init__(self) -> None:
        self.by_id: dict[str, dict] = {}
        self.by_time: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []

    async def get_by_id(self, message_id: str, **kwargs):
        """返回预置的单条消息。"""

        self.calls.append(("get_by_id", message_id, kwargs))
        return self.by_id.get(message_id)

    async def get_by_time_in_chat(self, chat_id: str, start_time, end_time, **kwargs):
        """返回预置的时间窗消息。"""

        self.calls.append(("get_by_time_in_chat", chat_id, start_time, end_time, kwargs))
        return list(self.by_time.get(chat_id, []))


def build_context(data_dir: Path) -> PluginContext:
    """构造带假消息能力的插件上下文。

    Args:
        data_dir: 插件数据目录。

    Returns:
        PluginContext: 上下文实例。
    """

    ctx = PluginContext("local.whale-affect", paths=PluginPaths(data_dir=data_dir, runtime_dir=data_dir / "runtime"))
    ctx.message = FakeMessageCapability()  # type: ignore[assignment]
    return ctx


def make_config(**overrides) -> dict:
    """构造一份启用插件、关闭影子模式的完整配置字典。

    Args:
        **overrides: 逐段覆盖的配置片段。

    Returns:
        dict: 配置字典。
    """

    config = plugin_module.WhaleAffectPlugin.build_default_config()
    config["admin"]["bot_account_id"] = BOT_ACCOUNT
    config["admin"]["user_ids"] = ["100000001"]
    config["plugin"]["enabled"] = True
    config["plugin"]["shadow_mode"] = False
    config["affinity"]["valid_chat_interval_seconds"] = 600
    for section, values in overrides.items():
        config[section].update(values)
    return config


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    """共用装配逻辑的测试基类。"""

    async def asyncSetUp(self) -> None:
        """准备临时目录与插件实例。"""

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.ctx = build_context(self.data_dir)
        self.plugin = self._new_plugin(self.ctx)

    async def asyncTearDown(self) -> None:
        """确保插件卸载，释放数据库线程。"""

        await self.plugin.on_unload()

    def _new_plugin(self, ctx: PluginContext, config: dict | None = None):
        """创建并装配一个插件实例。"""

        instance = plugin_module.create_plugin()
        instance._set_context(ctx)
        instance.set_plugin_config(config or make_config())
        return instance

    async def load(self, config: dict | None = None):
        """初始化并加载插件。"""

        if config is not None:
            await self.plugin.on_unload()
            self.plugin = self._new_plugin(self.ctx, config)
        await self.plugin.on_load()
        return self.plugin


class LifecycleTest(PluginTestCase):
    """生命周期与失败兜底。"""

    async def test_disabled_plugin_stays_inert(self) -> None:
        """enabled=false 时不建库、不解析任何 Hook 数据。"""

        config = make_config()
        config["plugin"]["enabled"] = False
        plugin = await self.load(config)
        state = plugin.describe_state()
        self.assertFalse(state["ready"])
        self.assertEqual(state["db_path"], "")

    async def test_load_creates_database(self) -> None:
        """启用后创建数据目录与数据库文件。"""

        plugin = await self.load()
        state = plugin.describe_state()
        self.assertTrue(state["ready"])
        self.assertTrue(Path(state["db_path"]).is_file())
        self.assertEqual(Path(state["db_path"]).name, "affect.sqlite3")

    async def test_invalid_config_blocks_startup(self) -> None:
        """分段校验失败时不启动后端。"""

        config = make_config()
        config["affinity"]["title_exit_threshold"] = 90
        plugin = await self.load(config)
        state = plugin.describe_state()
        self.assertFalse(state["ready"])
        self.assertIn("title_exit_threshold", state["startup_error"])

    async def test_unload_is_idempotent(self) -> None:
        """重复卸载不报错，且状态归零。"""

        plugin = await self.load()
        await plugin.on_unload()
        await plugin.on_unload()
        self.assertFalse(plugin.describe_state()["ready"])

    async def test_on_config_update_can_enable_later(self) -> None:
        """配置更新后可从惰性状态启动后端。"""

        config = make_config()
        config["plugin"]["enabled"] = False
        plugin = await self.load(config)
        self.assertFalse(plugin.describe_state()["ready"])

        enabled = make_config()
        plugin.set_plugin_config(enabled)
        await plugin.on_config_update("self", enabled, "0.1.0")
        self.assertTrue(plugin.describe_state()["ready"])

    async def test_on_config_update_can_disable(self) -> None:
        """关闭开关后配置热重载会释放后端。"""

        plugin = await self.load()
        self.assertTrue(plugin.describe_state()["ready"])

        disabled = make_config()
        disabled["plugin"]["enabled"] = False
        plugin.set_plugin_config(disabled)
        await plugin.on_config_update("self", disabled, "0.1.0")
        self.assertFalse(plugin.describe_state()["ready"])

    async def test_on_config_update_ignores_global_scope(self) -> None:
        """全局配置广播不影响本插件状态。"""

        plugin = await self.load()
        await plugin.on_config_update("model", {}, "")
        self.assertTrue(plugin.describe_state()["ready"])


class InboundHookTest(PluginTestCase):
    """入站 Hook 只记身份，不计分。"""

    async def test_inbound_hook_records_identity_without_scoring(self) -> None:
        """入站观察不写事件、不改状态。"""

        plugin = await self.load()
        now = int(time.time())
        await plugin.remember_inbound_identity(message=message_dict(message_id="m1", text="今天好累呀", timestamp=now))

        state = plugin.describe_state()
        self.assertEqual(state["identity_cache"], 1)
        self.assertEqual(state["stats"]["applied"], 0)
        self.assertEqual(state["stats"]["suppressed"], 0)

        identities = plugin.recent_identities()
        self.assertEqual(identities[0]["message_id"], "m1")
        self.assertFalse(identities[0]["is_group"])

    async def test_inbound_hook_skips_self_and_notify(self) -> None:
        """机器人自己的回声与通知消息不进入身份缓存。"""

        plugin = await self.load()
        now = int(time.time())
        await plugin.remember_inbound_identity(
            message=message_dict(message_id="m1", text="回声", timestamp=now, user_id=BOT_ACCOUNT)
        )
        notify = message_dict(message_id="m2", text="[戳一戳]", timestamp=now)
        notify["is_notify"] = True
        await plugin.remember_inbound_identity(message=notify)
        self.assertEqual(plugin.describe_state()["identity_cache"], 0)

    async def test_inbound_hook_ignores_malformed_payload(self) -> None:
        """非法载荷不抛异常，只计入错误。"""

        plugin = await self.load()
        await plugin.remember_inbound_identity(message="not-a-dict")
        self.assertEqual(plugin.describe_state()["identity_cache"], 0)

    async def test_identity_cache_is_bounded(self) -> None:
        """身份缓存容量有上限，不会无界增长。"""

        plugin = await self.load()
        now = int(time.time())
        for index in range(plugin_module._MAX_IDENTITY_CACHE + 40):
            await plugin.remember_inbound_identity(
                message=message_dict(message_id=f"m{index}", text="hi", timestamp=now)
            )
        self.assertEqual(plugin.describe_state()["identity_cache"], plugin_module._MAX_IDENTITY_CACHE)


class PlannerCaptureTest(PluginTestCase):
    """Planner 补捉路径：已落库消息才允许计分。"""

    async def test_capture_scores_accepted_messages(self) -> None:
        """库内存在的消息会被分类并计分。"""

        plugin = await self.load()
        now = int(time.time())
        self.ctx.message.by_time[SESSION_ID] = [
            message_dict(message_id="m1", text="小鲸鱼你好可爱", timestamp=now - 5),
        ]

        result = await plugin.capture_accepted_messages(hook_name="maisaka.planner.before_request", session_id=SESSION_ID)

        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(plugin.describe_state()["stats"]["applied"], 1)

    async def test_capture_is_idempotent(self) -> None:
        """重复补捉同一条消息不会重复加分。"""

        plugin = await self.load()
        now = int(time.time())
        self.ctx.message.by_time[SESSION_ID] = [
            message_dict(message_id="m1", text="小鲸鱼你好可爱", timestamp=now - 5),
        ]
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        state = plugin.describe_state()
        self.assertEqual(state["stats"]["applied"], 1)
        self.assertEqual(state["stats"]["duplicate"], 1)

    async def test_capture_never_aborts_and_never_mutates(self) -> None:
        """补捉失败时仍返回 continue 且不带 modified_kwargs。"""

        plugin = await self.load()

        class Boom:
            async def get_by_time_in_chat(self, *args, **kwargs):
                raise RuntimeError("宿主查询失败")

        self.ctx.message = Boom()  # type: ignore[assignment]
        result = await plugin.capture_accepted_messages(session_id=SESSION_ID)
        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(plugin.describe_state()["stats"]["hook_errors"], 1)

    async def test_capture_ignores_bot_echo(self) -> None:
        """filter_mai 之外的兜底：机器人自己的消息不计分。"""

        plugin = await self.load()
        now = int(time.time())
        self.ctx.message.by_time[SESSION_ID] = [
            message_dict(message_id="m1", text="小鲸鱼你好可爱", timestamp=now - 5, user_id=BOT_ACCOUNT),
        ]
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        state = plugin.describe_state()
        self.assertEqual(state["stats"]["applied"], 0)
        self.assertEqual(state["stats"]["suppressed"], 0)

    async def test_capture_requires_session(self) -> None:
        """没有 session_id 时不做任何查询。"""

        plugin = await self.load()
        result = await plugin.capture_accepted_messages(session_id="")
        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(self.ctx.message.calls, [])


class ReplyerInjectionTest(PluginTestCase):
    """Replyer 语气注入。"""

    async def test_shadow_mode_does_not_inject(self) -> None:
        """影子模式保持 extra_prompt 原样。"""

        config = make_config()
        config["plugin"]["shadow_mode"] = True
        plugin = await self.load(config)
        now = int(time.time())
        self.ctx.message.by_id["m1"] = message_dict(message_id="m1", text="小鲸鱼你好", timestamp=now)

        result = await plugin.inject_affect_tone(
            session_id=SESSION_ID,
            reply_message_id="m1",
            extra_prompt="原有要求",
        )
        self.assertEqual(result, {"action": "continue"})

    async def test_injects_block_and_preserves_existing_prompt(self) -> None:
        """启用注入后追加带边界标记的块，并保留其他插件内容。"""

        plugin = await self.load()
        now = int(time.time())
        self.ctx.message.by_id["m1"] = message_dict(message_id="m1", text="小鲸鱼你好", timestamp=now)

        result = await plugin.inject_affect_tone(
            session_id=SESSION_ID,
            reply_message_id="m1",
            extra_prompt="其他插件的要求",
        )

        self.assertEqual(result["action"], "continue")
        updated = result["modified_kwargs"]
        self.assertIn("其他插件的要求", updated["extra_prompt"])
        self.assertIn(prompt_builder.BLOCK_START, updated["extra_prompt"])
        self.assertIn(prompt_builder.BLOCK_END, updated["extra_prompt"])
        self.assertEqual(plugin.describe_state()["stats"]["blocks_injected"], 1)

    async def test_injection_replaces_previous_block(self) -> None:
        """重试时替换旧块，不重复累加。"""

        plugin = await self.load()
        now = int(time.time())
        self.ctx.message.by_id["m1"] = message_dict(message_id="m1", text="小鲸鱼你好", timestamp=now)

        first = await plugin.inject_affect_tone(session_id=SESSION_ID, reply_message_id="m1", extra_prompt="前缀")
        second = await plugin.inject_affect_tone(
            session_id=SESSION_ID,
            reply_message_id="m1",
            extra_prompt=first["modified_kwargs"]["extra_prompt"],
        )
        text = second["modified_kwargs"]["extra_prompt"]
        self.assertEqual(text.count(prompt_builder.BLOCK_START), 1)
        self.assertEqual(text.count("前缀"), 1)

    async def test_injection_keeps_other_hook_fields(self) -> None:
        """modified_kwargs 必须是完整 kwargs，不能丢字段。"""

        plugin = await self.load()
        now = int(time.time())
        self.ctx.message.by_id["m1"] = message_dict(message_id="m1", text="小鲸鱼你好", timestamp=now)

        result = await plugin.inject_affect_tone(
            session_id=SESSION_ID,
            reply_message_id="m1",
            extra_prompt="",
            task_name="replyer",
            attempt=2,
        )
        updated = result["modified_kwargs"]
        self.assertEqual(updated["task_name"], "replyer")
        self.assertEqual(updated["attempt"], 2)
        self.assertEqual(updated["session_id"], SESSION_ID)

    async def test_unknown_target_keeps_prompt(self) -> None:
        """无法确定目标用户时不注入，避免把关系错套到别人身上。"""

        plugin = await self.load()
        result = await plugin.inject_affect_tone(session_id="unknown", reply_message_id="missing", extra_prompt="原样")
        self.assertEqual(result, {"action": "continue"})

    async def test_group_reply_does_not_use_private_title(self) -> None:
        """有明确对象的群聊使用个人关系，但不使用私聊专属称呼。"""

        plugin = await self.load()
        now = int(time.time())
        group_message = message_dict(
            message_id="m1",
            text="@小鲸鱼 你好",
            timestamp=now,
            group_id="123456",
            session_id=GROUP_SESSION_ID,
        )
        self.ctx.message.by_id["m1"] = group_message
        await plugin.remember_inbound_identity(message=group_message)

        result = await plugin.inject_affect_tone(session_id=GROUP_SESSION_ID, reply_message_id="m1", extra_prompt="")
        injected = result["modified_kwargs"]["extra_prompt"]
        self.assertIn(prompt_builder.BLOCK_START, injected)
        self.assertNotIn("群体发言", injected)
        self.assertNotIn(prompt_builder.DEFAULT_TITLE, injected)
        self.assertIn("关系：", injected)

    async def test_private_jealousy_does_not_appear_in_group_prompt(self) -> None:
        """私聊的小吃醋不会作为明示状态写进同一人的群聊提示。"""
        from whale_affect_under_test.models import UserRelationState
        from whale_affect_under_test.host_adapter import normalize_inbound

        plugin = await self.load()
        now = int(time.time())
        private = message_dict(message_id="private",text="小鲸你好",timestamp=now)
        group = message_dict(message_id="group",text="@小鲸你好",timestamp=now,
                             group_id="123456",session_id=GROUP_SESSION_ID)
        key = normalize_inbound(private).identity_key().serialize()
        await asyncio.to_thread(plugin._storage.save_user,UserRelationState(
            user_key=key,affinity_centi=7000,reaction="jealous",
            reaction_expires_utc=now+180,reaction_replies_left=1))
        self.ctx.message.by_id.update({"private":private,"group":group})
        private_result=await plugin.inject_affect_tone(session_id=SESSION_ID,reply_message_id="private",extra_prompt="")
        group_result=await plugin.inject_affect_tone(session_id=GROUP_SESSION_ID,reply_message_id="group",extra_prompt="")
        self.assertIn("吃醋",private_result["modified_kwargs"]["extra_prompt"])
        self.assertNotIn("吃醋",group_result["modified_kwargs"]["extra_prompt"])


class SendCountingTest(PluginTestCase):
    """成功回复计数只在真实发出后触发。"""

    async def test_failed_send_is_not_counted(self) -> None:
        """sent=False 不计数。"""

        plugin = await self.load()
        await plugin.remember_inbound_identity(
            message=message_dict(message_id="m1", text="小鲸鱼你好", timestamp=int(time.time()))
        )
        await plugin.count_successful_reply(sent=False, reply_message_id="m1")
        self.assertEqual(plugin.describe_state()["stats"]["replies_counted"], 0)

    async def test_successful_send_increments_counter(self) -> None:
        """sent=True 且能解析目标时计数一次。"""

        plugin = await self.load()
        await plugin.remember_inbound_identity(
            message=message_dict(message_id="m1", text="小鲸鱼你好", timestamp=int(time.time()))
        )
        outbound = message_dict(message_id="out-1", text="你好", timestamp=int(time.time()), user_id=BOT_ACCOUNT)
        await plugin.count_successful_reply(sent=True, reply_message_id="m1", message=outbound)
        self.assertEqual(plugin.describe_state()["stats"]["replies_counted"], 1)

    async def test_counted_reply_persists(self) -> None:
        """计数写入数据库，重载后仍可见。"""

        plugin = await self.load()
        now = int(time.time())
        await plugin.remember_inbound_identity(message=message_dict(message_id="m1", text="小鲸鱼你好", timestamp=now))
        outbound = message_dict(message_id="out-1", text="你好", timestamp=now, user_id=BOT_ACCOUNT)
        await plugin.count_successful_reply(sent=True, reply_message_id="m1", message=outbound)

        target = await plugin._resolve_target(SESSION_ID, "m1")
        await asyncio.to_thread(plugin._record_reply, target)
        user_key = target.identity_key().serialize()
        state = await asyncio.to_thread(plugin._storage.load_user, user_key, plugin._engine.config)
        self.assertEqual(state.successful_reply_count, 2)

    async def test_unknown_target_is_skipped(self) -> None:
        """无法解析目标时不计数。"""

        plugin = await self.load()
        await plugin.count_successful_reply(sent=True, reply_message_id="missing")
        self.assertEqual(plugin.describe_state()["stats"]["replies_counted"], 0)


if __name__ == "__main__":
    unittest.main()
