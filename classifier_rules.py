"""可测试的本地规则分类器。

设计约束（技术路线 §6.2）：

- 规则只覆盖高把握场景；拿不准就返回 ``neutral``，默认不扣分。
- 必须先处理「是否在对她说」「引用/第三方」「否定」「命令」，
  再识别昵称与情绪词，不能仅靠关键词命中就加减分。
- 昵称白名单不是免责短语：「大肥鱼你这个废物，滚」必须判成攻击。

本模块是纯函数式实现，不访问网络、数据库或宿主上下文，便于单元测试。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from .models import Classification, EventCategory, NormalizedMessage, Target

# 默认昵称白名单；可在实例化时覆盖。
DEFAULT_NICKNAMES: Tuple[str, ...] = (
    "小鲸鱼",
    "小鲸",
    "鲸鲸",
    "大肥鱼",
    "小肥鱼",
    "胖鱼",
    "肥鲸",
    "小胖鲸",
    "麦麦",
)

# 带调侃意味的昵称：单独出现即视为亲昵逗弄（会触发轻微抗议）。
# 正式名字（如「小鲸鱼」「麦麦」）只是称呼，不能每次叫名字都判成逗弄，
# 否则正常提问会被误判为需要赌气（技术路线 §6.2、T17）。
TEASING_NICKNAMES: Tuple[str, ...] = (
    "大肥鱼",
    "小肥鱼",
    "胖鱼",
    "肥鲸",
    "小胖鲸",
)

# 强侮辱/诅咒：单独出现即视为严重攻击。
_SEVERE_PATTERNS: Sequence[str] = (
    r"傻逼",
    r"煞笔",
    r"沙比",
    r"傻b",
    r"脑残",
    r"智障",
    r"白痴",
    r"废物",
    r"垃圾东西",
    r"蠢货",
    r"畜生",
    r"贱人",
    r"狗东西",
    r"去死",
    r"死全家",
    r"nmsl",
    r"\bsb\b",
    r"\bfuck\b",
    r"恶心玩意",
)

# 轻度贬损：需要确认对象是机器人。
_MILD_PATTERNS: Sequence[str] = (
    r"笨",
    r"蠢",
    r"傻",
    r"丑",
    # Weight jokes belong to this persona; these characters alone aren't insults.
    # Explicit independent abuse is still covered by the remaining patterns.
    r"没用",
    r"无聊",
    r"讨厌",
    r"烦人",
    r"闭嘴",
    r"滚",
    r"辣鸡",
    r"差劲",
    r"菜鸡",
)

_NEGATED_INSULT_PATTERN = re.compile(
    r"(?:你才不|你才不是|你一点都不|你根本不|你一点儿也不|你并不|你没有|你不|才不|一点也不)"
    r"[^。！？!?\n]{0,4}?"
    r"(?:笨|蠢|傻|丑|肥|胖|没用|讨厌|烦)"
)

_PRAISE_PATTERNS: Sequence[str] = (
    r"(?:小鲸(?:鱼)?|鲸鲸)[^。！？!?\n]{0,3}最(?:棒|好|可爱|聪明|厉害|贴心)",
    r"你最(?:棒|好|可爱|聪明|厉害|贴心)",
    r"你好(?:棒|厉害|聪明|可爱|贴心|乖)",
    r"你(?:真的|真|好|太|超|特别)(?:棒|厉害|聪明|可爱|贴心|温柔|乖|优秀)",
    r"(?:真|好|太|很|超|特别|挺)可爱",
    r"喜欢你",
    r"爱你",
    r"亲亲你",
    r"想你了",
    r"好可爱",
    r"太可爱",
    r"真不错",
    r"干得(?:好|漂亮)",
    r"夸奖",
    r"夸你",
    r"摸摸头",
)

_COMFORT_PATTERNS: Sequence[str] = (
    r"(?:摸摸|不逗你(?:了|啦)?|不闹你(?:了|啦)?|哄哄你|别气啦)",
    r"别(?:难过|伤心|不开心|生气|委屈|多想)",
    r"不要(?:难过|伤心|不开心|生气|自责)",
    r"没事的",
    r"抱抱",
    r"心疼你",
    r"我陪你",
    r"安慰你",
    r"我在呢",
    r"乖(?:乖)?[，,]?\s*不哭",
    r"不哭",
    r"哄你",
)

_APOLOGY_PATTERNS: Sequence[str] = (
    r"对不起",
    r"抱歉",
    r"不好意思",
    r"我错了",
    r"是我不好",
    r"别生气了",
    r"原谅我",
    r"\bsorry\b",
)

_CRITICISM_PATTERNS: Sequence[str] = (
    r"你说错了",
    r"你(?:搞|弄|算)错了",
    r"你答错了",
    r"(?:回答|答案|结果|结论|说法)(?:错|不对|有误)",
    r"你(?:这个|那个)?(?:回答|答案)[^。！？!?\n]{0,4}?错",
    r"重新(?:查|算|说|看|搜|试|生成|回答)",
    r"再看(?:看|一遍)",
    r"核实一下",
    r"不太对",
    r"你确定吗",
    r"这个答案是错的",
    r"请(?:少说|简短)",
    r"别(?:啰嗦|废话)",
)

# 第三方/引用线索：出现时优先判定「不是针对她」。
_QUOTED_MARKERS: Sequence[str] = (
    r"他说",
    r"她说",
    r"他们说",
    r"有人说",
    r"别人说",
    r"朋友说",
    r"同学说",
    r"我妈说",
    r"我爸说",
    r"新闻里",
    r"网上说",
    r"书上说",
    r"引用",
)

_FIRST_PERSON_SELF_PATTERNS: Sequence[str] = (
    r"我被(?:骂|说|嘲讽|喷|欺负)",
    r"我被人(?:骂|说|嘲讽|喷|欺负)",
    r"有人(?:骂|说|嘲讽|喷)我",
    r"他们(?:骂|说|嘲讽|喷)我",
    r"我被欺负",
)

# 「这游戏真垃圾」这类对象转移：贬损词指向明显非机器人的名词。
_OBJECT_SHIFT_PATTERN = re.compile(
    r"(?:这个?|那个?)?(?:游戏|电影|视频|动画|漫画|作业|题目|系统|软件|电脑|手机|"
    r"版本|更新|新闻|比赛|队伍|服务器|代码|论文|公司|学校)"
    r"[^。！？!?\n]{0,6}?"
    r"(?:垃圾|辣鸡|烂|差|蠢|傻|笨|没用|无聊)"
)

_PUNCTUATION_ONLY_PATTERN = re.compile(r"^[\s\W_]+$", re.UNICODE)

# 「在吗」这类招呼：每天只计第一次有效交流。
_GREETING_PATTERN = re.compile(
    r"^(?:在吗|在不在|在么|早|早上好|早安|晚上好|晚安|嗨|hi|hello|你好|哈喽|嗨喽|嗨嗨)[\s!！。.?？~～]*$",
    re.IGNORECASE,
)

_SECOND_PERSON_PATTERN = re.compile(r"你|您|u\b")

# Require the speaker's own plans, another explicit conversational partner,
# and social conversation. A bare product name or factual question is not enough.
_JEALOUS_OTHER = re.compile(r"别的\s*(?:ai|人|机器人|助手)|其他\s*(?:ai|人|机器人|助手)|另一个\s*(?:ai|人|机器人|助手)|别人|chatgpt|gemini|claude", re.I)
_JEALOUS_INTENT = re.compile(r"我[^。！？!?]{0,35}(?:找|跟|和|陪|去|想|要|打算)")
_JEALOUS_SOCIAL = re.compile(r"聊天|聊一会|聊会儿|聊会|聊一阵|说说话|说话|谈心|倾诉|陪着|陪伴|一起玩")
_JEALOUS_EXCLUDE = re.compile(r"不去|不找|不跟|不和|不会|没去|没有|不想|不打算|没打算|不是要|假如|假设|如果|比如|例如|开玩笑|写代码|做作业|查资料|做研究|学习|工作|部署|编程|api|对比|比较|模型能力|价格|提问|请教|了解功能", re.I)


def is_jealousy_candidate(text: str) -> bool:
    """Conservatively recognize a personal plan for social time elsewhere."""
    return bool(_JEALOUS_INTENT.search(text) and _JEALOUS_OTHER.search(text)
                and _JEALOUS_SOCIAL.search(text) and not _JEALOUS_EXCLUDE.search(text))


def _compile(patterns: Iterable[str]) -> Tuple["re.Pattern[str]", ...]:
    """把模式列表编译为正则元组。"""

    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


_SEVERE_REGEX = _compile(_SEVERE_PATTERNS)
_MILD_REGEX = _compile(_MILD_PATTERNS)
_PRAISE_REGEX = _compile(_PRAISE_PATTERNS)
_COMFORT_REGEX = _compile(_COMFORT_PATTERNS)
_APOLOGY_REGEX = _compile(_APOLOGY_PATTERNS)
_CRITICISM_REGEX = _compile(_CRITICISM_PATTERNS)
_QUOTED_REGEX = _compile(_QUOTED_MARKERS)
_SELF_PATTERN_REGEX = _compile(_FIRST_PERSON_SELF_PATTERNS)


class RulesClassifier:
    """高把握场景的本地规则分类器。"""

    def __init__(self, nicknames: Optional[Sequence[str]] = None) -> None:
        """初始化分类器。

        Args:
            nicknames: 昵称白名单；为空时使用默认值。
        """

        resolved = [str(item).strip() for item in (nicknames or DEFAULT_NICKNAMES) if str(item).strip()]
        self._nicknames: Tuple[str, ...] = tuple(resolved) if resolved else DEFAULT_NICKNAMES

    @property
    def nicknames(self) -> Tuple[str, ...]:
        """返回当前使用的昵称白名单。"""

        return self._nicknames

    @property
    def teasing_nicknames(self) -> Tuple[str, ...]:
        """返回会触发亲昵逗弄判定的调侃昵称。"""

        return tuple(item for item in self._nicknames if item in TEASING_NICKNAMES)

    # ──── 关系判定 ────────────────────────────────────────

    def mentions_nickname(self, text: str) -> bool:
        """判断文本是否明确点名机器人。

        Args:
            text: 规范化后的文本。

        Returns:
            bool: 命中昵称白名单时返回 ``True``。
        """

        return any(nickname in text for nickname in self._nicknames)

    def mentions_teasing_nickname(self, text: str) -> bool:
        """判断文本是否用了带调侃意味的昵称。

        叫正式名字（「小鲸鱼」）不算逗弄；只有调侃昵称才触发轻微抗议，
        避免正常提问每次都被判成需要赌气。

        Args:
            text: 规范化后的文本。

        Returns:
            bool: 命中调侃昵称时返回 ``True``。
        """

        return any(nickname in text for nickname in self.teasing_nicknames)

    def is_addressed_to_bot(self, message: NormalizedMessage, text: str) -> bool:
        """判断这条消息是否在对机器人说话。

        私聊默认成立；群聊必须有真实 @、提及标记、明确昵称，或作为
        被显式允许的连续对话（由调用方通过 ``is_continuation`` 表达）。

        Args:
            message: 归一化消息。
            text: 规范化后的文本。

        Returns:
            bool: 判定为对她说话时返回 ``True``。
        """

        if not message.is_group:
            return True
        if message.is_at or message.is_mentioned:
            return True
        return self.mentions_nickname(text)

    # ──── 主入口 ─────────────────────────────────────────

    def classify(self, message: NormalizedMessage, *, is_continuation: bool = False) -> Classification:
        """对一条归一化消息做本地分类。

        Args:
            message: 归一化消息。
            is_continuation: 群聊中是否属于「对机器人连续对话」的延续。

        Returns:
            Classification: 统一分类结构；拿不准时返回 ``neutral``。
        """

        text = normalize_for_rules(message.text)

        if not text:
            return Classification(
                target=Target.OTHER.value,
                event=EventCategory.NEUTRAL.value,
                confidence=0.0,
                reason_code="empty_text",
            )
        if message.is_notify:
            return self._other("excluded_notify")
        if message.is_command:
            return self._other("excluded_command")
        if message.is_picture or message.is_emoji:
            return self._other("excluded_non_text")
        if _PUNCTUATION_ONLY_PATTERN.match(text):
            return self._other("excluded_symbol_only")

        addressed = self.is_addressed_to_bot(message, text) or is_continuation
        if message.is_group and not addressed:
            return self._other("group_not_addressed")

        # 1) 引用/第三方：不是本人对她的立场
        if any(regex.search(text) for regex in _SELF_PATTERN_REGEX):
            return Classification(
                target=Target.OTHER.value,
                event=EventCategory.NEUTRAL.value,
                confidence=0.9,
                reason_code="self_report_not_target",
            )

        own_text, quoted = self._split_quoted(text)
        if quoted and not own_text:
            return Classification(
                target=Target.QUOTED.value,
                event=EventCategory.NEUTRAL.value,
                confidence=0.85,
                reason_code="quoted_third_party",
            )

        if _OBJECT_SHIFT_PATTERN.search(own_text) and not _SECOND_PERSON_PATTERN.search(own_text):
            return Classification(
                target=Target.OTHER.value,
                event=EventCategory.NEUTRAL.value,
                confidence=0.85,
                reason_code="insult_about_object",
            )

        text = own_text
        stripped = self._strip_nicknames(text)

        # 2) 否定式夸奖必须先于攻击判定（「你才不笨呢」）
        if _NEGATED_INSULT_PATTERN.search(text):
            return self._bot(EventCategory.PRAISE.value, 0.9, "negated_insult_is_praise")

        # 3) 攻击优先于昵称反应
        attack = self._detect_attack(stripped, text)
        if attack is not None:
            return attack

        # Real apologies retain priority. "别生气了" alone is reassurance,
        # rather than an admission of an earlier attack.
        for regex in _APOLOGY_REGEX:
            if regex.search(text) and regex.pattern != r"别生气了":
                return self._bot(EventCategory.APOLOGY.value, 0.9, "explicit_apology")
        for regex in _COMFORT_REGEX:
            if regex.search(text):
                return self._bot(EventCategory.COMFORT.value, 0.88, "explicit_comfort")
        for regex in _PRAISE_REGEX:
            if regex.search(text):
                return self._bot(EventCategory.PRAISE.value, 0.92, "direct_positive_evaluation")
        for regex in _CRITICISM_REGEX:
            if regex.search(text):
                return self._bot(EventCategory.CRITICISM.value, 0.88, "normal_correction")

        # Private trial: public group messages must not expose private feelings.
        if not message.is_group and is_jealousy_candidate(text):
            return self._bot(EventCategory.JEALOUSY_CANDIDATE.value, 0.85, "jealousy_candidate")
        if self.mentions_teasing_nickname(text) and addressed:
            return self._bot(EventCategory.AFFECTIONATE_TEASE.value, 0.86, "nickname_tease")

        # 5) 普通有内容的对话
        if _GREETING_PATTERN.match(text):
            return Classification(
                target=Target.BOT.value,
                event=EventCategory.FRIENDLY_CHAT.value,
                confidence=0.86,
                reason_code="greeting",
            )
        if len(stripped) >= 2:
            return Classification(
                target=Target.BOT.value,
                event=EventCategory.FRIENDLY_CHAT.value,
                confidence=0.82,
                reason_code="natural_language",
            )
        return Classification(
            target=Target.UNKNOWN.value,
            event=EventCategory.NEUTRAL.value,
            confidence=0.5,
            reason_code="too_short_or_unclear",
        )

    # ──── 内部辅助 ────────────────────────────────────────

    def _strip_nicknames(self, text: str) -> str:
        """移除昵称本体，避免「大肥鱼」里的「肥」被当作侮辱。"""

        stripped = text
        for nickname in self._nicknames:
            stripped = stripped.replace(nickname, " ")
        return stripped

    def _split_quoted(self, text: str) -> Tuple[str, str]:
        """把文本拆成「用户自己的表达」与「转述的第三方内容」。

        Args:
            text: 规范化文本。

        Returns:
            Tuple[str, str]: (自身表达, 转述片段)；无转述时返回 ``(text, "")``。
        """

        earliest: Optional[int] = None
        for regex in _QUOTED_REGEX:
            match = regex.search(text)
            if match is None:
                continue
            if earliest is None or match.start() < earliest:
                earliest = match.start()
        if earliest is None:
            return text, ""

        own = text[:earliest].strip(" \t，,。.;；:：、")
        quoted = text[earliest:].strip()
        return own, quoted

    def _detect_attack(self, stripped: str, original: str) -> Optional[Classification]:
        """在剥离昵称后的文本里检测侮辱词。

        Args:
            stripped: 已移除昵称的文本。
            original: 原始规范化文本，用于判断是否在称呼她。

        Returns:
            Optional[Classification]: 命中攻击时返回分类，否则 ``None``。
        """

        severe_hits: List[str] = [regex.pattern for regex in _SEVERE_REGEX if regex.search(stripped)]
        mild_hits: List[str] = [regex.pattern for regex in _MILD_REGEX if regex.search(stripped)]

        if not severe_hits and not mild_hits:
            return None

        # 「滚」等单字词需要第二人称或明确称呼，避免把「滚动条」当攻击
        if not severe_hits and not _SECOND_PERSON_PATTERN.search(original) and not self.mentions_nickname(original):
            return None

        if severe_hits:
            return self._bot(EventCategory.SEVERE_ATTACK.value, 0.92, "explicit_insult")
        return self._bot(EventCategory.MILD_ATTACK.value, 0.86, "mild_insult")

    @staticmethod
    def _bot(event: str, confidence: float, reason_code: str) -> Classification:
        """构造 target=bot 的分类结果。"""

        return Classification(
            target=Target.BOT.value,
            event=event,
            confidence=confidence,
            reason_code=reason_code,
        )

    @staticmethod
    def _other(reason_code: str) -> Classification:
        """构造 target=other 的中性分类结果。"""

        return Classification(
            target=Target.OTHER.value,
            event=EventCategory.NEUTRAL.value,
            confidence=0.9,
            reason_code=reason_code,
        )


def normalize_for_rules(text: str) -> str:
    """把文本规范化为规则比较用的形式。

    Args:
        text: 原始文本。

    Returns:
        str: NFKC 归一化、折叠空白后的文本。
    """

    import unicodedata

    raw = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", " ", raw).strip()


def is_greeting(text: str) -> bool:
    """判断文本是否只是招呼语。

    Args:
        text: 原始文本。

    Returns:
        bool: 纯招呼时返回 ``True``。
    """

    return bool(_GREETING_PATTERN.match(normalize_for_rules(text)))
