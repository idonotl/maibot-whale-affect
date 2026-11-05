"""Regression tests for defects reproduced during the real acceptance review."""
from dataclasses import replace
import asyncio
import json
import time
import unittest

from test_plugin import PluginTestCase, message_dict, make_config, SESSION_ID, GROUP_SESSION_ID, BOT_ACCOUNT
from whale_affect_under_test import host_adapter


class TargetIsolationTest(PluginTestCase):
    async def test_explicit_group_target_has_individual_relationship(self):
        plugin = await self.load()
        message = message_dict(message_id="a", text="小鲸你好", timestamp=int(time.time()), group_id="g", session_id=GROUP_SESSION_ID)
        self.ctx.message.by_id["a"] = message
        result = await plugin.inject_affect_tone(session_id=GROUP_SESSION_ID, reply_message_id="a", extra_prompt="")
        self.assertIn("关系：", result["modified_kwargs"]["extra_prompt"])
        self.assertNotIn("群体发言", result["modified_kwargs"]["extra_prompt"])
        self.assertNotIn("「主人」", result["modified_kwargs"]["extra_prompt"])

    async def test_missing_explicit_reference_never_becomes_last_speaker(self):
        plugin = await self.load()
        await plugin.remember_inbound_identity(message=message_dict(message_id="b", user_id="B", text="另一个问题", timestamp=int(time.time())))
        self.assertIsNone(await plugin._resolve_target(SESSION_ID, "missing-a"))

    async def test_same_message_id_in_other_session_cannot_replace_target(self):
        plugin = await self.load()
        for session, user in [("group-one", "A"), ("group-two", "B")]:
            await plugin.remember_inbound_identity(message=message_dict(message_id="same-id", text="小鲸你好", timestamp=int(time.time()), user_id=user, session_id=session, group_id=session))
        target = await plugin._resolve_target("group-one", "same-id")
        self.assertIsNotNone(target)
        self.assertEqual(target.user_id, "A")

    async def test_prompt_and_retry_do_not_consume_title(self):
        plugin = await self.load()
        message = message_dict(message_id="title", text="你好", timestamp=int(time.time()))
        self.ctx.message.by_id["title"] = message
        target = host_adapter.normalize_inbound(message)
        user = plugin._storage.load_user(target.identity_key().serialize(), plugin._engine.config)
        plugin._storage.save_user(replace(user, affinity_centi=9000, active_days=14, valid_interactions=40, preferred_title="主人", title_opt_in=True, successful_reply_count=10))
        first = await plugin._build_block({"session_id": SESSION_ID, "reply_message_id": "title"})
        second = await plugin._build_block({"session_id": SESSION_ID, "reply_message_id": "title"})
        current = plugin._storage.load_user(user.user_key, plugin._engine.config)
        self.assertIn("本轮可以用", first)
        self.assertIn("本轮可以用", second)
        self.assertEqual(current.last_title_reply_count, 0)

    async def test_natural_comfort_clears_pout_instead_of_plain_chat(self):
        plugin = await self.load()
        message = host_adapter.normalize_inbound(message_dict(message_id="comfort", text="好啦，小鲸最可爱了，摸摸，不逗你啦", timestamp=int(time.time())))
        self.assertEqual(plugin._rules.classify(message).event, "comfort")

    async def test_group_without_target_only_gets_global_mood(self):
        plugin = await self.load()
        self.ctx.message.by_time[GROUP_SESSION_ID] = [message_dict(message_id="a", text="小鲸你好", timestamp=int(time.time()), group_id="g", session_id=GROUP_SESSION_ID)]
        await plugin.capture_accepted_messages(session_id=GROUP_SESSION_ID)
        block = await plugin._build_block({"session_id": GROUP_SESSION_ID, "reply_message_id": ""})
        self.assertIn("群体发言", block)
        self.assertNotIn("关系：", block)

    async def test_capability_cannot_return_message_from_different_session(self):
        plugin = await self.load()
        self.ctx.message.by_id["a"] = message_dict(message_id="a", text="你好", timestamp=int(time.time()), session_id="other")
        self.assertIsNone(await plugin._resolve_target(SESSION_ID, "a"))


class SuccessfulSendTest(PluginTestCase):
    async def prepare_title(self):
        plugin = await self.load()
        message = message_dict(message_id="in", text="你好", timestamp=int(time.time()))
        self.ctx.message.by_id["in"] = message
        target = host_adapter.normalize_inbound(message)
        key = target.identity_key().serialize()
        state = plugin._storage.load_user(key, plugin._engine.config)
        plugin._storage.save_user(replace(state, affinity_centi=9000, active_days=14, valid_interactions=40, preferred_title="主人", title_opt_in=True, successful_reply_count=10))
        return plugin, key

    def outbound(self, text="主人，早呀", message_id="out"):
        return message_dict(message_id=message_id, text=text, timestamp=int(time.time()), user_id=BOT_ACCOUNT)

    async def test_only_successful_delivery_with_actual_title_consumes_interval(self):
        plugin, key = await self.prepare_title()
        await plugin.inject_affect_tone(session_id=SESSION_ID, reply_message_id="in", extra_prompt="")
        await plugin.count_successful_reply(sent=False, reply_message_id="in", message=self.outbound())
        self.assertEqual(plugin._storage.load_user(key, plugin._engine.config).last_title_reply_count, 0)
        await plugin.count_successful_reply(sent=True, reply_message_id="in", message=self.outbound())
        state = plugin._storage.load_user(key, plugin._engine.config)
        self.assertEqual(state.successful_reply_count, 11)
        self.assertEqual(state.last_title_reply_count, 11)

    async def test_replayed_successful_send_does_not_increment_twice(self):
        plugin, key = await self.prepare_title()
        for _ in range(3):
            await plugin.count_successful_reply(sent=True, reply_message_id="in", message=self.outbound())
        self.assertEqual(plugin._storage.load_user(key, plugin._engine.config).successful_reply_count, 11)

    async def test_ordinary_text_does_not_consume_unused_title(self):
        plugin, key = await self.prepare_title()
        await plugin.count_successful_reply(sent=True, reply_message_id="in", message=self.outbound("早呀"))
        state = plugin._storage.load_user(key, plugin._engine.config)
        self.assertEqual(state.successful_reply_count, 11)
        self.assertEqual(state.last_title_reply_count, 0)

    async def test_media_delivery_does_not_count_as_ordinary_reply(self):
        plugin, key = await self.prepare_title()
        outbound = self.outbound()
        outbound["is_emoji"] = True
        await plugin.count_successful_reply(sent=True, reply_message_id="in", message=outbound)
        self.assertEqual(plugin._storage.load_user(key, plugin._engine.config).successful_reply_count, 10)

    async def test_send_deduplication_survives_reload(self):
        plugin, key = await self.prepare_title()
        await plugin.count_successful_reply(sent=True, reply_message_id="in", message=self.outbound())
        await self.load(make_config())
        self.ctx.message.by_id["in"] = message_dict(message_id="in", text="你好", timestamp=int(time.time()))
        await self.plugin.count_successful_reply(sent=True, reply_message_id="in", message=self.outbound())
        self.assertEqual(self.plugin._storage.load_user(key, self.plugin._engine.config).successful_reply_count, 11)

    async def test_concurrent_sends_do_not_lose_counters(self):
        plugin, key = await self.prepare_title()
        await asyncio.gather(*[
            plugin.count_successful_reply(sent=True, reply_message_id="in", message=self.outbound("早呀", "out-" + str(index)))
            for index in range(4)
        ])
        self.assertEqual(plugin._storage.load_user(key, plugin._engine.config).successful_reply_count, 14)

    async def test_v1_database_upgrade_preserves_existing_affinity(self):
        plugin, key = await self.prepare_title()
        def old_database(connection):
            connection.execute("DROP TABLE successful_sends")
            connection.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
        plugin._repository.run(old_database)
        await self.load(make_config())
        state = self.plugin._storage.load_user(key, self.plugin._engine.config)
        self.assertEqual(state.affinity_centi, 9000)
        self.assertEqual(state.valid_interactions, 40)
        self.assertEqual(state.preferred_title, "主人")
        self.assertEqual(self.plugin._repository.run(lambda c: c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]), "2")


class ContinuationTest(PluginTestCase):
    async def engage(self):
        plugin = await self.load()
        now = int(time.time())
        first = message_dict(message_id="a", text="小鲸，明天谁赢", timestamp=now, group_id="g", session_id=GROUP_SESSION_ID)
        self.ctx.message.by_time[GROUP_SESSION_ID] = [first]
        self.ctx.message.by_id["a"] = first
        await plugin.capture_accepted_messages(session_id=GROUP_SESSION_ID)
        outbound = message_dict(message_id="bot-out", text="我觉得甲队能赢", timestamp=now, user_id=BOT_ACCOUNT, group_id="g", session_id=GROUP_SESSION_ID)
        await plugin.count_successful_reply(sent=True, reply_message_id="a", message=outbound)
        return plugin, plugin._engine.clock.now_utc()

    async def test_followup_without_at_is_counted(self):
        plugin, now = await self.engage()
        followup = message_dict(message_id="followup", text="那你预测错了怎么办呀", timestamp=now, group_id="g", session_id=GROUP_SESSION_ID)
        self.ctx.message.by_time[GROUP_SESSION_ID] = [followup]
        await plugin.capture_accepted_messages(session_id=GROUP_SESSION_ID)
        row = plugin._repository.run(lambda c: dict(c.execute("select * from events where message_id='followup'").fetchone()))
        self.assertEqual(row["category"], "friendly_chat")
        self.assertEqual(row["status"], "applied")

    async def test_other_person_does_not_inherit_engagement(self):
        plugin, now = await self.engage()
        message = host_adapter.normalize_inbound(message_dict(message_id="b", text="那你预测错了怎么办呀", timestamp=now, user_id="B", group_id="g", session_id=GROUP_SESSION_ID))
        self.assertFalse(plugin._is_continuation(message))

    async def test_at_other_person_is_not_bot_continuation(self):
        plugin, now = await self.engage()
        raw = message_dict(message_id="other", text="那你预测错了怎么办呀", timestamp=now, group_id="g", session_id=GROUP_SESSION_ID)
        raw["raw_message"] = [{"type": "at", "data": {"target_user_id": "B"}}]
        self.assertFalse(plugin._is_continuation(host_adapter.normalize_inbound(raw)))

    async def test_expired_engagement_does_not_grant_credit(self):
        plugin, now = await self.engage()
        message = host_adapter.normalize_inbound(message_dict(message_id="late", text="那你怎么办呀", timestamp=now + 121, group_id="g", session_id=GROUP_SESSION_ID))
        self.assertFalse(plugin._is_continuation(message))


class SemanticWiringTest(PluginTestCase):
    async def prepare_semantic(self):
        self.gate = asyncio.Event()
        self.calls = []
        gate, calls = self.gate, self.calls
        class FakeLLM:
            async def generate(self, **kwargs):
                calls.append(kwargs)
                await gate.wait()
                return {"success": True, "response": json.dumps({"schema_version": 1, "target": "bot", "event": "praise", "confidence": 0.96, "reason_code": "positive"})}
        self.ctx.llm = FakeLLM()
        plugin = await self.load(make_config(classifier={"llm_enabled": True}))
        self.message = message_dict(message_id="semantic", text="你把这团乱麻理顺了，给你一朵小红花", timestamp=int(time.time()))
        self.ctx.message.by_time[SESSION_ID] = [self.message]
        return plugin

    async def test_background_model_is_actually_called_and_scored_once(self):
        plugin = await self.prepare_semantic()
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        row = plugin._repository.run(lambda c: dict(c.execute("select * from events where message_id='semantic'").fetchone()))
        self.assertEqual(row["status"], "pending")
        self.gate.set()
        await asyncio.wait_for(asyncio.gather(*list(plugin._semantic_tasks.values())), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["model_name"], "deepseek-flash-affect")
        row = plugin._repository.run(lambda c: dict(c.execute("select * from events where message_id='semantic'").fetchone()))
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["category"], "praise")
        self.assertEqual(row["classifier"], "llm")
        after = row["score_after_json"]
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        self.assertEqual(plugin._repository.run(lambda c: c.execute("select score_after_json from events where message_id='semantic'").fetchone()[0]), after)

    async def test_late_praise_cannot_overwrite_newer_attack(self):
        plugin = await self.prepare_semantic()
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        self.ctx.message.by_time[SESSION_ID] = [message_dict(message_id="attack", text="你这个废物", timestamp=int(time.time()))]
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        self.gate.set()
        await asyncio.wait_for(asyncio.gather(*list(plugin._semantic_tasks.values())), 2)
        rows = plugin._repository.run(lambda c: [dict(row) for row in c.execute("select * from events order by message_id")])
        late = next(row for row in rows if row["message_id"] == "semantic")
        self.assertEqual(late["status"], "neutral")
        state = plugin._storage.load_user(host_adapter.normalize_inbound(self.message).identity_key().serialize(), plugin._engine.config)
        self.assertEqual(state.reaction, "hurt")

    async def test_disable_cancels_pending_without_corrupting_database(self):
        plugin = await self.prepare_semantic()
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        await plugin.on_unload()
        self.assertFalse(plugin.describe_state()["ready"])
        self.assertFalse(plugin._semantic_tasks)

    async def test_timeout_does_not_create_unbounded_host_requests(self):
        plugin = await self.prepare_semantic()
        settings = plugin._semantic.settings
        prompt = [{"role": "user", "content": "test"}]
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(plugin._generate_semantic(prompt, settings), 0.02)
        result = await plugin._generate_semantic(prompt, settings)
        self.assertFalse(result["success"])
        self.assertEqual(len(self.calls), 1)
        self.gate.set()

    async def test_deleted_pending_event_is_not_resurrected_by_model(self):
        plugin = await self.prepare_semantic()
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        key = host_adapter.normalize_inbound(self.message).identity_key().serialize()
        plugin._storage.delete_user(key)
        self.gate.set()
        await asyncio.wait_for(asyncio.gather(*list(plugin._semantic_tasks.values())), 2)
        self.assertEqual(plugin._repository.run(lambda c: c.execute("SELECT COUNT(*) FROM events").fetchone()[0]), 0)

    async def test_expired_pending_is_dropped_on_restart(self):
        plugin = await self.prepare_semantic()
        message = host_adapter.normalize_inbound(replace_dict(self.message, timestamp=str(int(time.time()) - 180)))
        event = plugin._make_event(message, plugin._rules.classify(message), int(time.time()))
        plugin._storage.reserve_pending(event)
        await self.load(make_config())
        self.assertEqual(self.plugin._storage.get_event_status(event.event_key), "dropped")


def replace_dict(value, **changes):
    result = dict(value)
    result.update(changes)
    return result


if __name__ == "__main__":
    unittest.main()
