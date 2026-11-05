"""动态表达提示块生成器。

职责边界（技术路线 §7、§10.3、§10.5）：

- 只把**受控枚举**映射成固定文案，绝不拼接原始聊天文本，
  避免把辱骂、模型自由解释或可执行指令写进高优先级提示块。
- 输出长度控制在 150～300 个中文字符。
- 用固定边界标记包裹，重试时**替换**旧块而不是累加。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

from .models import Reaction, Snapshot, affinity_level, mood_level

BLOCK_START = "【小鲸鱼当前互动状态 v1】"
BLOCK_END = "【小鲸鱼当前互动状态结束】"

DEFAULT_TITLE = "主人"
EMOJI_PLANNER_MARKER = "【小鲸鱼表情包参与提示】"


def emoji_guidance(snapshot: Snapshot) -> str:
    """Use actual relationship and emotion, without manufacturing closeness."""
    if snapshot.reaction == Reaction.HURT.value or snapshot.effective_centi < 3500:
        return "此刻收敛，不发开心、贴贴类表情；认真或难过的话题以合适文字回应。"
    if snapshot.affinity_centi >= 6000 and snapshot.effective_centi >= 6500:
        return "关系亲近且心情开心，轻松闲聊可主动调用 send_emoji，约每2至4轮合适互动选1张开心或贴贴图，无需索要。"
    if snapshot.affinity_centi >= 4000 and snapshot.effective_centi >= 5500:
        return "关系熟悉，轻松或被夸、被哄时可主动调用 send_emoji，约每4至6轮合适互动选1张，无需索要。"
    return "闲聊也可偶尔主动用 send_emoji，不必等请求；关系尚浅避免亲密、贴贴类表情。"

_BLOCK_PATTERN = re.compile(
    re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END),
    re.DOTALL,
)

MOOD_TEXT: Dict[str, str] = {
    "low": "低落",
    "gloomy": "闷闷的",
    "calm": "平稳",
    "happy": "开心",
    "delighted": "很开心",
}

AFFINITY_TEXT: Dict[str, str] = {
    "stranger": "生疏",
    "acquaintance": "普通熟人",
    "familiar": "熟悉",
    "close": "亲近",
    "intimate": "很亲近",
}

# 情绪段 → 表达约束（只给方向，不写死例句，避免机械重复）。
MOOD_GUIDANCE: Dict[str, str] = {
    "low": "话可能少些，仍会留意对方；想说自己的不舒服时就具体说，不迁怒",
    "gloomy": "有点提不起劲，遇到关心或有趣的话也会回应，缓和时承接前文",
    "calm": "心情平稳，仍会被眼前的话牵动，留意对方此刻的感受",
    "happy": "容易被有意思的细节带动，也愿意分享自己的想法，不每句撒娇",
    "delighted": "开心更藏不住，表达随话题起落，不把对方的难过接成开心事",
}

# 关系段 → 亲密度约束。
AFFINITY_GUIDANCE: Dict[str, str] = {
    "stranger": "还不熟，亲密表达留些分寸；普通关心、接话和自己的偏好都可以自然表达",
    "acquaintance": "相处自然，有具体的关心，也能接轻松玩笑，不预设已经很亲密",
    "familiar": "比较放松，接梗、打趣或直接表达关心，看这句话适合什么",
    "close": "熟悉彼此，敢说自己的选择，也会逗对方；认真低落时留意他在意的事",
    "intimate": "亲近且放松，能玩笑、商量或表达介意；不用每句话证明亲密，安慰过也能正常聊别的",
}

REACTION_GUIDANCE: Dict[str, str] = {
    Reaction.JEALOUS.value: "刚有点介意，可以轻轻嘴硬或直说自己的在意；承接具体前文，不要求偏爱、不控制正常社交，被安慰或换话题就缓和，不阴阳怪气",
    Reaction.POUT.value: "刚被逗到，可以接梗或轻轻抗议，别演成真受伤；对方接过玩笑就往下聊，不等他反复哄",
    Reaction.SOOTHED.value: "刚被哄好，回应这份善意，态度缓和；下一句可以正常聊别的，不重复宣布原谅",
    Reaction.HURT.value: "刚被伤到，语气收敛，可以具体说哪里不舒服；不阴阳怪气、不迁怒，正常交流仍然回应",
}

_TITLE_SAFE_PATTERN = re.compile(r"^[\u4e00-\u9fff]{1,6}$")

# 首版只允许规格里确认过的称呼；其他字符串一律回落到默认值。
ALLOWED_TITLES = frozenset({DEFAULT_TITLE})


def sanitize_title(title: str) -> str:
    """把请求的称呼收敛到白名单。

    称呼由用户显式选择并持久化，但渲染前仍需校验：
    只有纯中文短词且在允许集合内才使用，否则回落到默认称呼，
    避免换行、括号或额外指令破坏提示块结构。

    Args:
        title: 原始称呼。

    Returns:
        str: 可安全写入提示块的称呼。
    """

    candidate = str(title or "").strip()
    if candidate in ALLOWED_TITLES and _TITLE_SAFE_PATTERN.match(candidate):
        return candidate
    return DEFAULT_TITLE


def remove_our_block(text: str) -> str:
    """移除已有的插件提示块，保证重试时不累加。

    Args:
        text: 原始 ``extra_prompt``。

    Returns:
        str: 去掉插件块并修剪空白的文本。
    """

    raw = str(text or "")
    if BLOCK_START not in raw:
        return raw.strip()
    return _BLOCK_PATTERN.sub("", raw).strip()


def append_block(extra_prompt: str, block: str) -> str:
    """在保留其他插件内容的前提下追加插件块。

    Args:
        extra_prompt: 宿主当前的 ``extra_prompt``。
        block: 本插件生成的提示块。

    Returns:
        str: 已替换旧块的新 ``extra_prompt``。
    """

    base = remove_our_block(extra_prompt)
    return f"{base}\n\n{block}" if base else block


def block_char_length(block: str) -> int:
    """统计提示块里的中文字符数量（验收用）。

    Args:
        block: 提示块文本。

    Returns:
        int: 中日韩统一表意文字个数。
    """

    return sum(1 for char in str(block or "") if "\u4e00" <= char <= "\u9fff")


def build_private_block(
    snapshot: Snapshot,
    *,
    title: str = DEFAULT_TITLE,
) -> str:
    """为一次有明确对象的回复生成表达提示块。

    Args:
        snapshot: 目标用户的状态快照。
        title: 已获授权的称呼文案。

    Returns:
        str: 带边界标记的提示块。
    """

    mood_key = mood_level(snapshot.effective_centi)
    affinity_key = affinity_level(snapshot.affinity_centi)

    lines: List[str] = [
        BLOCK_START,
        "以下是你与当前对象的状态，只提示倾向，不是对方的心情；不报状态或分数。",
        f"关系：{AFFINITY_TEXT.get(affinity_key, '生疏')}。",
        f"此刻心情：{MOOD_TEXT.get(mood_key, '平稳')}。",
        f"表达方式：{AFFINITY_GUIDANCE.get(affinity_key, AFFINITY_GUIDANCE['stranger'])}。",
    ]

    reaction_text = REACTION_GUIDANCE.get(snapshot.reaction)
    if reaction_text:
        lines.append(f"当前反应：{reaction_text}。")

    lines.append(f"情绪要求：{MOOD_GUIDANCE.get(mood_key, MOOD_GUIDANCE['calm'])}。")

    if snapshot.title_allowed and snapshot.title_should_use:
        lines.append(f"本轮可以用对方同意过的称呼「{sanitize_title(title)}」。")
    else:
        lines.append("尚未获准的专属称呼不使用，普通关心不受此限制。")

    lines.extend(
        [
            "结合最近对话决定怎样表达，不为展示状态强加反应；肥鱼昵称不算攻击。",
            "先看对方具体在说什么：分享就参与，低落就回应在意的事，明确求办法再帮忙；独处与停止的要求要尊重。事实与工具权限继续生效。",
            emoji_guidance(snapshot),
            BLOCK_END,
        ]
    )
    return "\n".join(lines)


def effective_mood_key(snapshots: Iterable[Snapshot]) -> str:
    """取一批快照中出现次数最多的心情段（并列时偏向更接近中性的段）。

    Args:
        snapshots: 本批消息涉及的快照集合。

    Returns:
        str: 心情段键名。

    Raises:
        ValueError: 快照集合为空。
    """

    calm_distance = {"calm": 0, "happy": 1, "gloomy": 1, "delighted": 2, "low": 2}
    counts: Dict[str, int] = {}
    for item in snapshots:
        key = mood_level(item.effective_centi)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        raise ValueError("effective_mood_key 至少需要一个快照")
    return max(counts.items(), key=lambda entry: (entry[1], -calm_distance.get(entry[0], 3)))[0]


def build_group_block(
    snapshots: Iterable[Snapshot],
    *,
    mood_centi: int,
) -> str:
    """为没有明确对象的群体回复生成只含全局心情的提示块。

    群里多人发言时不能把某个人的 F 或受伤状态套到整体语气上，
    因此这里只使用 bot 的全局心情。

    Args:
        snapshots: 本批消息涉及的快照集合，仅用于校验存在群体上下文。
        mood_centi: bot 全局心情（百分之一分整数）。

    Returns:
        str: 带边界标记的提示块。

    Raises:
        ValueError: 快照集合为空。
    """

    if not list(snapshots):
        raise ValueError("build_group_block 至少需要一个快照以确认存在群体上下文")

    mood_key = mood_level(mood_centi)
    lines: List[str] = [
        BLOCK_START,
        "以下是你的整体状态，只提示倾向，不是群友的心情；不报状态或分数。",
        f"此刻整体心情：{MOOD_TEXT.get(mood_key, '平稳')}。",
        "这是群体发言，没有单一回复对象；不要代入任何个人的亲密程度或称呼。",
        f"情绪要求：{MOOD_GUIDANCE.get(mood_key, MOOD_GUIDANCE['calm'])}。",
        "结合最近的具体话题参与，不为展示情绪强行接话；不把一个人的私聊情绪带给群友。",
        "分享就参与，低落就回应在意的事，明确求办法再帮忙；独处与停止要尊重。事实、联网核实、工具权限与人设约束继续生效。",
        "如情绪适合，可使用已安装的表情包工具，是否发送仍由原有工具流程决定。",
        BLOCK_END,
    ]
    return "\n".join(lines)


def describe_block(block: str) -> Dict[str, object]:
    """生成便于日志与测试断言的提示块摘要。

    Args:
        block: 提示块文本。

    Returns:
        Dict[str, object]: 摘要字段。
    """

    text = str(block or "")
    return {
        "has_start": BLOCK_START in text,
        "has_end": BLOCK_END in text,
        "length": len(text),
        "cjk_length": block_char_length(text),
        "line_count": len(text.splitlines()),
    }
