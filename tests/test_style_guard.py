import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap
from test_plugin import PluginTestCase, message_dict, SESSION_ID
from whale_affect_under_test import style_guard


class StyleRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = style_guard.load_rules()

    def test_typography_variants_and_invisible_spaces(self):
        self.assertTrue(style_guard.find_hits('我再补\u200b一刀', '', self.rules))
        self.assertTrue(style_guard.find_hits('稳稳地 接住你', '', self.rules))

    def test_normal_affection_literal_actions_and_technical_language(self):
        for text in ('抱抱你，我在。', '我选蓝的，你说得对。', '接住杯子，别摔了。',
                     '我不是生气，是没听清。', '这里的闭环指控制系统的反馈。'):
            self.assertFalse(style_guard.find_hits(text, '', self.rules))

    def test_merely_mentioning_or_forbidding_does_not_grant_exception(self):
        for text in ('别说我补一刀', '不要模仿鱼八股', '不用模仿鱼八股', '不必来一段鱼八股', '别人总说这条我认', '随便开个玩笑'):
            self.assertTrue(style_guard.find_hits('这条我认', text, self.rules))

    def test_explicit_quote_exception_is_narrow(self):
        target = '解释一下“这条我认”是什么意思'
        self.assertFalse(style_guard.find_hits('“这条我认”是一种点评起手。', target, self.rules))
        self.assertTrue(style_guard.find_hits('这条我认，我再补一刀', target, self.rules))

    def test_exception_expires_on_next_target(self):
        self.assertFalse(style_guard.find_hits('这条我认', '来一段鱼八股', self.rules))
        self.assertTrue(style_guard.find_hits('这条我认', '今天有点难过', self.rules))
        self.assertIn('当前实际清单：这条我认', style_guard.prompt(self.rules, '展示禁词表'))

    def test_rule_disabled_and_prompt_merge_preserves_other_plugins(self):
        rules = {**self.rules, 'enabled': False}
        self.assertEqual(style_guard.prompt(rules), '')
        self.assertFalse(style_guard.find_hits('这条我认', '', rules))
        block = style_guard.prompt(self.rules)
        merged = style_guard.merge('MEMORY\nOPINION', block)
        merged = style_guard.merge(merged, block)
        self.assertIn('MEMORY\nOPINION', merged)
        self.assertEqual(merged.count(style_guard.START), 1)

    def test_clarity_and_fact_boundaries_cover_both_generation_stages(self):
        replyer = style_guard.prompt(self.rules)
        self.assertIn('不删理解句意必需的主语', replyer)
        self.assertIn('Planner 的回复方向只是参考', replyer)
        self.assertIn('长时间没有消息只说明没有新消息', replyer)
        self.assertIn('假设和试演不等于真实发生', replyer)

        planner = style_guard.planner_prompt()
        self.assertIn('reply_reference 只写有据可依的回应方向', planner)
        self.assertIn('刚睡醒、关机、离开、重新上线或恢复', planner)
        self.assertIn('用户原话里的“我”属于用户', planner)


class StyleHookTest(PluginTestCase):
    async def test_retry_once_then_passes_response_unchanged(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='今天有点难过')
        p.ctx.llm.generate = AsyncMock(return_value={'success':True,'response':'我会稳稳接住你'})
        args = dict(response='我会稳稳接住你', session_id=SESSION_ID, retry_count=0)
        result = await p.check_style_rules(**args)
        self.assertEqual(result, {'action':'continue'})
        for count in (1, 2, 3):
            self.assertEqual(await p.check_style_rules(**{**args, 'retry_count': count}), {'action':'continue'})
        self.assertEqual(p._stats['style_retries'], 1)
        self.assertEqual(p._stats['style_leaks'], 4)
        self.assertEqual(p.ctx.llm.generate.await_count, 1)

    async def test_current_target_only_not_previous_meme(self):
        p = await self.load()
        p._style_target_text = AsyncMock(side_effect=['来一段鱼八股', '陪我说两句'])
        p.ctx.llm.generate = AsyncMock(return_value={'success':True,'response':'陪着呢，今天过得怎么样？'})
        self.assertEqual(await p.check_style_rules(response='这条我认'), {'action':'continue'})
        self.assertEqual((await p.check_style_rules(response='这条我认'))['modified_kwargs']['response'], '陪着呢，今天过得怎么样？')

    async def test_rewrite_network_error_retains_original_reply(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='你好')
        p.ctx.llm.generate = AsyncMock(side_effect=RuntimeError('network unavailable'))
        self.assertEqual(await p.check_style_rules(response='这条我认'), {'action':'continue'})

    async def test_rewrite_cannot_remove_original_source_link(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='给我来源')
        p.ctx.llm.generate = AsyncMock(return_value={'success':True,'response':'这里是资料'})
        self.assertEqual(await p.check_style_rules(response='这条我认 https://example.org/page'), {'action':'continue'})

    async def test_long_reply_is_not_truncated_or_regenerated(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='长文')
        p.ctx.llm.generate = AsyncMock()
        self.assertEqual(await p.check_style_rules(response='这条我认'+'a'*6001), {'action':'continue'})
        p.ctx.llm.generate.assert_not_awaited()

    async def test_timeout_retains_reply_and_does_not_stack_model_calls(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='你好')
        p._style_rules = {**style_guard.load_rules(), 'rewrite_timeout_seconds':.01}
        async def pending(**kwargs):
            await asyncio.sleep(5)
        p.ctx.llm.generate = AsyncMock(side_effect=pending)
        self.assertEqual(await p.check_style_rules(response='这条我认'), {'action':'continue'})
        self.assertEqual(await p.check_style_rules(response='这条我认'), {'action':'continue'})
        self.assertEqual(p.ctx.llm.generate.await_count, 1)

    async def test_error_is_fail_open_and_has_no_replacement(self):
        p = await self.load()
        p._style_target_text = AsyncMock(side_effect=RuntimeError('test_failure'))
        self.assertEqual(await p.check_style_rules(response='这条我认'), {'action':'continue'})

    async def test_shadow_mode_does_not_apply_style(self):
        from test_plugin import make_config
        p = await self.load(make_config(plugin={'shadow_mode':True}))
        self.assertEqual(await p.check_style_rules(response='这条我认'), {'action':'continue'})
        self.assertEqual(await p.inject_style_rules(extra_prompt='MEMORY'), {'action':'continue'})

    async def test_before_request_preserves_tone_memory_and_opinion(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='你好')
        result = await p.inject_style_rules(extra_prompt='TONE\nMEMORY\nOPINION')
        text = result['modified_kwargs']['extra_prompt']
        self.assertIn('TONE\nMEMORY\nOPINION', text)
        self.assertIn(style_guard.START, text)

    async def test_short_reply_style_is_neutralized_without_losing_tool_arguments(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='hello')
        original = {
            'reply_style': '简短表达',
            'reply_reference': '对方只是打招呼；不要从隔夜沉默推断刚睡醒。',
            'other_argument': 'keep',
        }
        result = await p.inject_style_rules(
            extra_prompt='MEMORY', reply_tool_args=original, session_id=SESSION_ID,
            reply_message_id='hello', unrelated_kwarg='preserved',
        )
        updated = result['modified_kwargs']
        self.assertEqual(updated['reply_tool_args']['reply_style'], '正常回复')
        self.assertEqual(updated['reply_tool_args']['reply_reference'], original['reply_reference'])
        self.assertEqual(updated['reply_tool_args']['other_argument'], 'keep')
        self.assertEqual(updated['unrelated_kwarg'], 'preserved')
        self.assertEqual(original['reply_style'], '简短表达')
        self.assertIn('不删理解句意必需的主语', updated['extra_prompt'])

    async def test_other_reply_styles_are_not_rewritten(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='你好')
        for style in ('正常回复', '长回复'):
            with self.subTest(style=style):
                args = {'reply_style': style, 'reply_reference': '保留'}
                result = await p.inject_style_rules(extra_prompt='', reply_tool_args=args)
                self.assertEqual(result['modified_kwargs']['reply_tool_args'], args)

    async def test_style_prompt_retry_replaces_previous_block(self):
        p = await self.load()
        p._style_target_text = AsyncMock(return_value='你好')
        first = await p.inject_style_rules(extra_prompt='MEMORY')
        second = await p.inject_style_rules(extra_prompt=first['modified_kwargs']['extra_prompt'])
        text = second['modified_kwargs']['extra_prompt']
        self.assertEqual(text.count(style_guard.START), 1)
        self.assertEqual(text.count(style_guard.FACT_BOUNDARIES[0]), 1)
        self.assertIn('MEMORY', text)

    async def test_planner_grounding_is_idempotent_and_preserves_other_items(self):
        p = await self.load()
        host_item = {
            'item_type': 'UserMessageItem',
            'meta': {'item_id': 'host-message'},
            'parts': [{'type': 'text', 'text': 'hello'}],
        }
        first = await p.ground_planner_facts(items=[host_item], item_schema_version=1)
        first_items = first['modified_kwargs']['items']
        self.assertIs(first_items[0], host_item)
        self.assertEqual(len(first_items), 2)
        self.assertIn(style_guard.PLANNER_MARKER, first_items[1]['parts'][0]['text'])

        second = await p.ground_planner_facts(items=first_items, item_schema_version=1)
        second_items = second['modified_kwargs']['items']
        self.assertEqual(len(second_items), 2)
        self.assertEqual(sum(
            str((item.get('meta') or {}).get('item_id', '')).startswith(style_guard.PLANNER_ITEM_PREFIX)
            for item in second_items
        ), 1)

    async def test_shadow_mode_does_not_add_planner_grounding(self):
        from test_plugin import make_config
        p = await self.load(make_config(plugin={'shadow_mode': True}))
        result = await p.ground_planner_facts(items=[], item_schema_version=1)
        self.assertEqual(result, {'action': 'continue'})

    async def test_target_lookup_timeout_keeps_guard_bounded(self):
        p = await self.load()
        async def slow(*args):
            await asyncio.sleep(5)
        p._resolve_target = slow
        self.assertEqual(await p._style_target_text({}), '')


if __name__ == '__main__':
    unittest.main()
