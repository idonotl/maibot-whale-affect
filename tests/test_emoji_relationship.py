"""Relationship-aware emoji planning, without touching production affinity scores."""
import time
from dataclasses import replace
import unittest
from unittest.mock import patch

from test_plugin import PluginTestCase, message_dict, SESSION_ID, GROUP_SESSION_ID
from test_prompt_builder import make_snapshot
from whale_affect_under_test import prompt_builder as pb, host_adapter


class EmojiGuidanceTests(unittest.TestCase):
    def test_close_happy_increases_spontaneous_use(self):
        self.assertIn('每2至4轮', pb.emoji_guidance(make_snapshot(affinity_centi=7000,effective_centi=7500)))

    def test_familiar_has_lower_frequency(self):
        self.assertIn('每4至6轮', pb.emoji_guidance(make_snapshot(affinity_centi=4500,effective_centi=6000)))

    def test_stranger_not_given_intimate_stickers(self):
        self.assertIn('避免亲密', pb.emoji_guidance(make_snapshot(affinity_centi=1500)))

    def test_hurt_overrides_high_affinity(self):
        self.assertIn('不发开心', pb.emoji_guidance(make_snapshot(affinity_centi=9000,effective_centi=8500,reaction='hurt')))

    def test_same_qq_identity_across_sessions(self):
        private=host_adapter.normalize_inbound(message_dict(message_id='p',text='hi',timestamp=1))
        group=host_adapter.normalize_inbound(message_dict(message_id='g',text='hi',timestamp=1,group_id='another-group',session_id='another-session'))
        self.assertEqual(private.identity_key(),group.identity_key())


class EmojiPlanningTests(PluginTestCase):
    async def test_unaccepted_observation_cannot_change_planning(self):
        plugin=await self.load()
        await plugin.remember_inbound_identity(message=message_dict(message_id='x',text='hi',timestamp=int(time.time())))
        self.assertEqual(await plugin.plan_relationship_emoji(session_id=SESSION_ID,items=[],item_schema_version=1),{'action':'continue'})

    async def prepare(self):
        plugin=await self.load()
        raw=message_dict(message_id='accepted',text='你好',timestamp=int(time.time()))
        self.ctx.message.by_time[SESSION_ID]=[raw]
        await plugin.capture_accepted_messages(session_id=SESSION_ID)
        return plugin

    async def test_planner_gets_state_without_requesting_sticker(self):
        plugin=await self.prepare()
        result=await plugin.plan_relationship_emoji(session_id=SESSION_ID,items=[],item_schema_version=1)
        item=result['modified_kwargs']['items'][0]
        self.assertEqual(item['item_type'],'UserMessageItem')
        self.assertIn(pb.EMOJI_PLANNER_MARKER,item['parts'][0]['text'])
        self.assertIn('send_emoji',item['parts'][0]['text'])
        retried=await plugin.plan_relationship_emoji(**result['modified_kwargs'])
        self.assertEqual(len(retried['modified_kwargs']['items']),1)

    async def test_only_successful_emoji_delivery_creates_cooldown(self):
        plugin=await self.prepare()
        message={'session_id':SESSION_ID,'is_emoji':True}
        await plugin.remember_emoji_delivery(sent=False,message=message)
        self.assertNotIn(SESSION_ID,plugin._emoji_last_sent)
        await plugin.remember_emoji_delivery(sent=True,message=message)
        result=await plugin.plan_relationship_emoji(session_id=SESSION_ID,items=[],item_schema_version=1)
        self.assertIn('30秒',result['modified_kwargs']['items'][0]['parts'][0]['text'])
        sent_at=plugin._emoji_last_sent[SESSION_ID]
        with patch.object(plugin._engine.clock,'now_utc',return_value=sent_at+29):
            result=await plugin.plan_relationship_emoji(session_id=SESSION_ID,items=[],item_schema_version=1)
            self.assertIn('30秒',str(result))
        with patch.object(plugin._engine.clock,'now_utc',return_value=sent_at+30):
            result=await plugin.plan_relationship_emoji(session_id=SESSION_ID,items=[],item_schema_version=1)
            self.assertNotIn('30秒',str(result))

    async def test_prefilter_other_speaker_is_not_added(self):
        plugin=await self.prepare()
        await plugin.remember_inbound_identity(message=message_dict(message_id='unaccepted',user_id='FORBIDDEN',text='hi',timestamp=int(time.time())))
        result=await plugin.plan_relationship_emoji(session_id=SESSION_ID,items=[],item_schema_version=1)
        self.assertNotIn('FORBIDDEN',str(result))


if __name__=='__main__':
    unittest.main()
