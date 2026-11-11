"""Detect repetition of a complete, successfully delivered reply in one chat."""
from collections import OrderedDict
import re
import time
import unicodedata


# 与 [scoring].reply_echo_window_seconds 默认值一致；调用方按配置覆盖。
DEFAULT_WINDOW_SECONDS = 600.0
MAX_CHATS = 128
# 送达文本会被拆句、注入错别字、去掉语气词，逐字前缀比对几乎对不上。
# 三段字符指纹覆盖 60% 即视为「这条她已经说完了」。
MIN_COVERAGE = 0.6
REPEAT_REQUEST = re.compile(r"(?:再|重新)(?:说|念|讲|发)(?:一|一遍|一回)?|复述|重复一遍|原话")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.strip()).casefold()
    text = re.sub(r"[哦噢喔呀啊啦呐呢]+(?=$|[,.!?;，。！？；\s])", "", text)
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _grams(text: str) -> frozenset:
    """返回用于统计送达比例的三字符指纹集合。"""

    if len(text) < 3:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[index:index + 3] for index in range(len(text) - 2))


class ReplyEcho:
    """记住每个会话里完整送达过的回复，阻止她把同一句话再说一遍。"""

    def __init__(self, window_seconds: float = DEFAULT_WINDOW_SECONDS) -> None:
        """初始化去重记录。

        Args:
            window_seconds: 一条已送达回复仍然「算刚说过」的时长。
        """

        self.window_seconds = float(window_seconds)
        self.pending = OrderedDict()
        self.delivered = OrderedDict()

    def repeats(self, session: str, text: str) -> bool:
        """判断这条回复是否与窗口内已完整送达的某条一字不差。"""

        candidate = normalize(text)
        if not 6 <= len(candidate) <= 1000:
            return False
        now = time.monotonic()
        for previous, delivered_at in self.delivered.get(session, ()):
            if now - delivered_at > self.window_seconds:
                continue
            if candidate == previous:
                return True
        return False

    def expect(self, session: str, text: str) -> None:
        """登记准备发送的回复，等待送达回执。"""

        self.pending.pop(session, None)
        candidate = normalize(text)
        if 6 <= len(candidate) <= 1000:
            expected = _grams(candidate)
            self.pending[session] = (candidate, expected, frozenset(), time.monotonic())
            if len(self.pending) > MAX_CHATS:
                self.pending.popitem(last=False)

    def receipt(self, session: str, text: str) -> None:
        """累加一段实际送达的文本，够完整就把这条回复记为「说过」。"""

        current = self.pending.get(session)
        if current is None:
            return
        original, expected, seen, started_at = current
        now = time.monotonic()
        if now - started_at > self.window_seconds:
            self.pending.pop(session, None)
            return
        seen = seen | (_grams(normalize(text)) & expected)
        if len(seen) < MIN_COVERAGE * len(expected):
            self.pending[session] = (original, expected, seen, started_at)
            return
        self.pending.pop(session, None)
        recent = self.delivered.pop(session, [])
        self.delivered[session] = [(original, now), *recent[:3]]
        if len(self.delivered) > MAX_CHATS:
            self.delivered.popitem(last=False)
