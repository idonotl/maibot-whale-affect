import importlib.util
from pathlib import Path
import time
import unittest


spec = importlib.util.spec_from_file_location("reply_echo", Path(__file__).parents[1] / "reply_echo.py")
echo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(echo)


class EchoTests(unittest.TestCase):
    def test_blocks_only_after_the_whole_reply_was_delivered_in_this_chat(self):
        guard = echo.ReplyEcho()
        first = "修的时候轻点哦，别越修我越看不了。修好了我第一个替你试"
        replay = "修的时候轻点，别越修我越看不了。修好了我第一个替你试。"
        guard.expect("chat", first)
        guard.receipt("chat", "修的时候轻点哦")
        self.assertFalse(guard.repeats("chat", replay))
        guard.receipt("chat", "别越修我越看不了。修好了我第一个替你试")
        self.assertTrue(guard.repeats("chat", replay))
        self.assertFalse(guard.repeats("another", replay))
        self.assertFalse(guard.repeats("chat", replay + "还有一个新问题，要不要看下日志？"))
        self.assertFalse(guard.repeats("chat", replay.replace("替你试", "不替你试")))
        self.assertFalse(guard.repeats("chat", "嗯嗯"))

    def test_splitted_and_rewritten_delivery_counts_as_said(self):
        """拆句与错别字改写后仍算「说过」，只要送达内容基本覆盖原句。"""

        guard = echo.ReplyEcho()
        said = "我把你的日志翻了一遍，第三行的报错和封面没关系，得先看音频那段。"
        guard.expect("chat", said)
        guard.receipt("chat", "我把你的日志翻了一遍，第三行的报错和")
        self.assertFalse(guard.repeats("chat", said))
        guard.receipt("chat", "封面没关系，得先看看音频那段。")
        self.assertTrue(guard.repeats("chat", said))

    def test_delivery_that_never_reached_the_chat_leaves_retry_allowed(self):
        """只送达了表情或语气词时，重试同一句仍然允许。"""

        guard = echo.ReplyEcho()
        said = "我把你的日志翻了一遍，第三行的报错和封面没关系。"
        guard.expect("chat", said)
        guard.receipt("chat", "嗯嗯")
        self.assertFalse(guard.repeats("chat", said))

    def test_window_is_configurable(self):
        """窗口来自配置，过期后不再抑制。"""

        guard = echo.ReplyEcho(window_seconds=1.0)
        said = "这条我刚刚才说过，不该再来一遍。"
        guard.expect("chat", said)
        guard.receipt("chat", said)
        self.assertTrue(guard.repeats("chat", said))
        stored, arrived = guard.delivered["chat"][0]
        guard.delivered["chat"][0] = (stored, arrived - 2)
        self.assertFalse(guard.repeats("chat", said))
