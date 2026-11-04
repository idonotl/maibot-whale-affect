"""规则分类器的行为测试。

覆盖技术路线 §6.2 明确列出的场景与 §5.2 必须排除的场景。
所有用例都是脱敏示例，不包含真实聊天记录。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import PACKAGE_NAME  # noqa: E402

from whale_affect_under_test.classifier_rules import (  # noqa: E402
    RulesClassifier,
    is_greeting,
    normalize_for_rules,
)
from whale_affect_under_test.models import (  # noqa: E402
    EventCategory,
    NormalizedMessage,
    Target,
)

BOT_ID = "10000"


def make_message(
    text: str,
    *,
    group_id: str = "",
    is_at: bool = False,
    is_notify: bool = False,
    is_command: bool = False,
    is_picture: bool = False,
    is_emoji: bool = False,
    user_id: str = "20000",
    message_id: str = "1",
    reply_to: str = "",
) -> NormalizedMessage:
    """构造一条测试用归一化消息。"""

    return NormalizedMessage(
        platform="qq",
        bot_account_id=BOT_ID,
        scope="",
        user_id=user_id,
        group_id=group_id,
        session_id="session",
        message_id=message_id,
        text=text,
        timestamp=1_767_225_600,
        is_at=is_at,
        is_notify=is_notify,
        is_command=is_command,
        is_picture=is_picture,
        is_emoji=is_emoji,
        reply_to=reply_to,
    )


class RulesClassifierTestCase(unittest.TestCase):
    """验证高把握规则分类。"""

    def setUp(self) -> None:
        """准备分类器。"""

        self.classifier = RulesClassifier()

    def test_jealousy_requires_real_personal_social_plan(self) -> None:
        for text in ("我今天下午要去找别的ai聊天", "小鲸，我今晚要和ChatGPT聊天", "大肥鱼，我要和别人聊会儿", "我想去和别的AI说说话"):
            with self.subTest(text=text):
                result = self.classifier.classify(make_message(text))
                self.assertEqual(result.event, EventCategory.JEALOUSY_CANDIDATE.value)
        for text in ("你知道别的AI吗", "我不去找别的AI聊天", "我不找别的AI聊天", "我没有和别人聊天", "如果我去找别的AI聊天呢",
                     "我找别的AI写代码", "我今天去和朋友聊天", "他说我要去找别的AI聊天",
                     "我想比较别的AI聊天模型能力"):
            with self.subTest(text=text):
                result = self.classifier.classify(make_message(text))
                self.assertNotEqual(result.event, EventCategory.JEALOUSY_CANDIDATE.value)
        group = self.classifier.classify(make_message("我今天要去找别的AI聊天",group_id="group",is_at=True))
        self.assertNotEqual(group.event, EventCategory.JEALOUSY_CANDIDATE.value)

    def assert_event(self, text: str, expected: str, **kwargs: object) -> None:
        """断言分类结果的事件类型。"""

        message = make_message(text, **kwargs)  # type: ignore[arg-type]
        result = self.classifier.classify(message)
        self.assertEqual(
            result.event,
            expected,
            msg=f"{text!r} -> {result.event} ({result.reason_code})，期望 {expected}",
        )

    def test_nickname_greeting_is_affectionate_tease(self) -> None:
        """「hello，大肥鱼」应判为亲昵逗弄。"""

        self.assert_event("hello，大肥鱼", EventCategory.AFFECTIONATE_TEASE.value)

    def test_nickname_question_is_affectionate_tease(self) -> None:
        """「小胖鲸，在吗」应判为亲昵逗弄。"""

        self.assert_event("小胖鲸，在吗", EventCategory.AFFECTIONATE_TEASE.value)

    def test_weight_persona_words_are_not_attacks(self) -> None:
        cases = (
            "今天合肥天气很好，所以心情很好，你呢？心情如何？",
            "你喜欢大肥鱼还是小肥鱼称呼？",
            "其实叫你大肥鱼不是说明你真的很胖，只是一个爱称，逗你玩的 [吃糖]",
            "大肥鱼，今天合肥下雨了，你知道吗？",
            "你吃胖了呀，胖乎乎也很可爱",
            "我最近在减肥，你有什么建议？",
            "小肥鱼，在吗？",
        )
        for text in cases:
            with self.subTest(text=text):
                result = self.classifier.classify(make_message(text))
                self.assertNotIn(result.event, (EventCategory.MILD_ATTACK.value, EventCategory.SEVERE_ATTACK.value))

    def test_persona_nickname_does_not_mask_independent_abuse(self) -> None:
        self.assert_event("小肥鱼你这个废物，滚", EventCategory.SEVERE_ATTACK.value)

    def test_negated_insult_is_praise(self) -> None:
        """「你才不胖呢」应判为夸奖而不是攻击。"""

        self.assert_event("你才不胖呢", EventCategory.PRAISE.value)

    def test_comfort_with_praise(self) -> None:
        """夸奖加摸摸优先作为哄好，不叠加两个语义奖励。"""

        self.assert_event("你最可爱啦，摸摸", EventCategory.COMFORT.value)

    def test_normal_correction_is_criticism(self) -> None:
        """「你刚才说错了，重新查查」应判为纠错且不扣分。"""

        self.assert_event("你刚才说错了，重新查查", EventCategory.CRITICISM.value)

    def test_self_report_not_target(self) -> None:
        """「我被骂了」不是对机器人的攻击。"""

        result = self.classifier.classify(make_message("我被骂了"))
        self.assertNotEqual(result.target, Target.BOT.value)
        self.assertEqual(result.event, EventCategory.NEUTRAL.value)

    def test_third_party_insult_not_target(self) -> None:
        """「他说你很笨」是转述，不应按当前用户攻击处理。"""

        result = self.classifier.classify(make_message("他说你很笨"))
        self.assertNotEqual(result.target, Target.BOT.value)

    def test_nickname_does_not_exempt_real_attack(self) -> None:
        """昵称白名单不能覆盖真实攻击。"""

        self.assert_event("大肥鱼你这个废物，滚", EventCategory.SEVERE_ATTACK.value)

    def test_plain_insult_is_severe(self) -> None:
        """裸骂应判为严重攻击。"""

        self.assert_event("傻逼", EventCategory.SEVERE_ATTACK.value)

    def test_mild_insult(self) -> None:
        """「你真的很笨」应判为轻度攻击。"""

        self.assert_event("你真的很笨", EventCategory.MILD_ATTACK.value)

    def test_insult_about_object_is_not_attack(self) -> None:
        """「这游戏真垃圾」骂的是游戏，不是她。"""

        result = self.classifier.classify(make_message("这游戏真垃圾"))
        self.assertNotEqual(result.target, Target.BOT.value)

    def test_greeting_only(self) -> None:
        """纯招呼是友好聊天。"""

        self.assert_event("在吗", EventCategory.FRIENDLY_CHAT.value)
        self.assertTrue(is_greeting("在吗"))

    def test_apology(self) -> None:
        """道歉应被识别。"""

        self.assert_event("对不起嘛", EventCategory.APOLOGY.value)

    def test_comfort(self) -> None:
        """安慰应被识别。"""

        self.assert_event("别难过啦，抱抱", EventCategory.COMFORT.value)

    def test_empty_text_is_neutral(self) -> None:
        """空文本保持中性。"""

        result = self.classifier.classify(make_message(""))
        self.assertEqual(result.event, EventCategory.NEUTRAL.value)
        self.assertEqual(result.reason_code, "empty_text")

    def test_symbol_only_is_neutral(self) -> None:
        """纯符号保持中性。"""

        result = self.classifier.classify(make_message("。。。"))
        self.assertEqual(result.reason_code, "excluded_symbol_only")

    def test_group_message_without_address_is_other(self) -> None:
        """群内没有 @ 也没有昵称时不计入。"""

        result = self.classifier.classify(make_message("今天吃什么", group_id="999"))
        self.assertNotEqual(result.target, Target.BOT.value)
        self.assertEqual(result.reason_code, "group_not_addressed")

    def test_group_message_with_at_is_counted(self) -> None:
        """群内真实 @ 时计入。"""

        result = self.classifier.classify(make_message("小鲸鱼你好", group_id="999", is_at=True))
        self.assertEqual(result.target, Target.BOT.value)

    def test_group_message_with_nickname_is_counted(self) -> None:
        """群内明确点名昵称时计入。"""

        result = self.classifier.classify(make_message("小鲸鱼今天怎么样", group_id="999"))
        self.assertEqual(result.target, Target.BOT.value)

    def test_notify_and_command_excluded(self) -> None:
        """通知与管理命令必须排除。"""

        self.assertEqual(
            self.classifier.classify(make_message("戳一戳", is_notify=True)).reason_code,
            "excluded_notify",
        )
        self.assertEqual(
            self.classifier.classify(make_message("/status", is_command=True)).reason_code,
            "excluded_command",
        )

    def test_media_excluded(self) -> None:
        """图片与表情包首版不参与。"""

        self.assertEqual(
            self.classifier.classify(make_message("[图片]", is_picture=True)).reason_code,
            "excluded_non_text",
        )
        self.assertEqual(
            self.classifier.classify(make_message("[表情]", is_emoji=True)).reason_code,
            "excluded_non_text",
        )

    def test_natural_language_is_friendly_chat(self) -> None:
        """普通自然语言计入友好聊天。"""

        self.assert_event("今天上课好累啊", EventCategory.FRIENDLY_CHAT.value)

    def test_normalize_folds_whitespace(self) -> None:
        """规范化应折叠空白与全角字符。"""

        self.assertEqual(normalize_for_rules("  你  好  "), "你 好")
        self.assertEqual(normalize_for_rules("ＡＢＣ"), "ABC")


if __name__ == "__main__":
    unittest.main(verbosity=2)
