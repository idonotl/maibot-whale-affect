"""Hypothetical dialogue must not fabricate real injury or reconciliation."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import time
import unittest

import test_classifier_rules as rules_test
import test_engine as engine_test
import test_plugin as plugin_test

RulesClassifier = rules_test.RulesClassifier
make_message = rules_test.make_message
EventCategory, Reaction = engine_test.EventCategory, engine_test.Reaction
BASE_NOW, USER_KEY = engine_test.BASE_NOW, engine_test.USER_KEY


class DescriptionClassificationTests(unittest.TestCase):
    def test_questions_and_hypothetical_insults_are_not_real_events(self):
        cases = (
            '你生气是什么样子的？我好像还没见过啊',
            '你会发什么表情包来表达你的愤怒？',
            '那你先开口说什么？是你道歉？还是希望我哄哄你',
            '怎么哄你才算合适？',
            '如果我说你是蠢货，你会怎么反应？',
            '假设我让你原谅我，你会答应吗？',
        )
        for text in cases:
            with self.subTest(text=text):
                result = RulesClassifier().classify(make_message(text))
                self.assertEqual(result.event, 'neutral')
                self.assertEqual(result.reason_code, 'interaction_description')

    def test_actual_reassurance_and_independent_abuse_keep_meaning(self):
        cases = {
            '好鲸鲸，别生气了好不好？': 'comfort',
            '你需要我哄哄你吗？抱抱': 'comfort',
            '对不起，刚刚是我说过分了': 'apology',
            '如果你不开心，我会陪你，抱抱你': 'comfort',
            '你就是废物。那怎么哄你才合适？': 'severe_attack',
            '假设我哄你，但你本来就是废物': 'severe_attack',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(RulesClassifier().classify(make_message(text)).event, expected)


class RecoveryTests(unittest.TestCase):
    setUp = engine_test.EngineTestCase.setUp
    tearDown = engine_test.EngineTestCase.tearDown
    make_event = engine_test.EngineTestCase.make_event

    def test_kindness_without_injury_does_not_invent_recovery(self):
        for category in (EventCategory.COMFORT.value, EventCategory.APOLOGY.value):
            with self.subTest(category=category):
                # A different identity avoids cooldowns between the controls.
                user_key = USER_KEY + category
                result = self.engine.apply(self.make_event(category, user_key=user_key), BASE_NOW)
                self.assertEqual(result.snapshot.reaction, Reaction.NONE.value)
                if category == EventCategory.COMFORT.value:
                    self.assertGreater(result.delta_mood_centi, 0)

    def test_real_injury_can_still_be_soothed(self):
        self.engine.apply(self.make_event(EventCategory.MILD_ATTACK.value), BASE_NOW)
        result = self.engine.apply(self.make_event(EventCategory.COMFORT.value, now=BASE_NOW + 30), BASE_NOW + 30)
        self.assertEqual(result.snapshot.reaction, Reaction.SOOTHED.value)

    def test_lingering_negative_offset_can_recover_after_reaction_expires(self):
        user = self.storage.load_user(USER_KEY, self.engine_config)
        self.storage.save_user(replace(user, offset_centi=-100, offset_local_date='2026-01-01', offset_update_utc=BASE_NOW))
        result = self.engine.apply(self.make_event(EventCategory.COMFORT.value), BASE_NOW)
        self.assertEqual(result.snapshot.reaction, Reaction.SOOTHED.value)


class DescriptionPluginIntegrationTests(plugin_test.PluginTestCase):
    async def test_descriptive_question_does_not_enqueue_semantic_or_soothed(self):
        plugin = await self.load()
        message = plugin_test.message_dict(message_id='description', text='是你道歉？还是希望我哄哄你', timestamp=int(time.time()))
        self.ctx.message.by_id['description'] = message
        self.ctx.message.by_time[plugin_test.SESSION_ID] = [message]
        original = plugin._semantic
        submit = Mock(side_effect=AssertionError('descriptive_question_must_not_use_semantic_rpc'))
        plugin._semantic = SimpleNamespace(enabled=True, submit=submit)
        try:
            await plugin.capture_accepted_messages(session_id=plugin_test.SESSION_ID)
            submit.assert_not_called()
        finally:
            plugin._semantic = original
        block = await plugin._build_block({'session_id': plugin_test.SESSION_ID, 'reply_message_id': 'description'})
        self.assertNotIn('当前反应：', block)
        self.assertIn('假设', block)
        self.assertEqual(plugin._stats['semantic_submitted'], 0)
