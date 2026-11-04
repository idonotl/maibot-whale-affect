"""宿主消息与身份适配层。

业务层只接受 :class:`models.NormalizedMessage`，本模块负责把本机 SDK 2.16.0 /
Host 1.3.15 真实返回的字典转换成它，并统一处理身份键、文本规范化与哈希。

已确认的入站字段（``chat.receive.after_process`` 的 ``message``）::

    message_id, timestamp（Unix 秒字符串）, platform, account_id, scope, session_id,
    message_info.user_info.user_id, message_info.group_info.group_id,
    processed_plain_text, raw_message, is_at, is_mentioned, is_notify,
    is_command, reply_to

注意事项（阶段 0 实测）：

- SnowLuma 适配器入站字典里 ``scope`` 可能是 ``None``、``session_id`` 可能是空串；
  ``session_id`` 由 Host 在 Hook 触发前补写，因此适配层不做任何 hash 计算，
  缺失时如实返回空串，让上层决定能否使用。
- 不猜测缺失字段，不把昵称拼成身份。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Dict, Mapping, Optional

from .models import NormalizedMessage

# 规范化文本时统一折叠的空白字符（含全角空格与换行）。
_WHITESPACE_PATTERN = re.compile(r"\s+")

# 隐私脱敏：连续 5 位以上数字视为可能的 QQ 号/群号。
_LONG_DIGITS_PATTERN = re.compile(r"\d{5,}")


def _as_text(value: Any) -> str:
    """把任意字段安全转换为去空白字符串。

    Args:
        value: 原始字段值。

    Returns:
        str: 字符串形式；``None`` 返回空串。
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def normalize_text(text: Any) -> str:
    """规范化消息文本，用于去重比较。

    Args:
        text: 原始文本。

    Returns:
        str: NFKC 归一化并折叠空白后的文本。
    """

    raw = unicodedata.normalize("NFKC", _as_text(text))
    return _WHITESPACE_PATTERN.sub(" ", raw).strip()


def text_hash(text: Any) -> str:
    """计算规范化文本的稳定哈希（不存原文）。

    Args:
        text: 原始文本。

    Returns:
        str: 规范化文本的 sha256 十六进制摘要；空文本返回空串。
    """

    normalized = normalize_text(text)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def redact_text(text: Any, *, max_length: int = 60) -> str:
    """生成可选的脱敏诊断文本。

    Args:
        text: 原始文本。
        max_length: 截断长度。

    Returns:
        str: 去掉长数字串并截断后的文本。
    """

    normalized = normalize_text(text)
    if not normalized:
        return ""
    return _LONG_DIGITS_PATTERN.sub("#", normalized)[:max_length]


def build_bot_key(platform: str, bot_account_id: str) -> str:
    """构造机器人心情状态键。

    Args:
        platform: 平台名，例如 ``qq``。
        bot_account_id: 机器人账号 ID。

    Returns:
        str: 稳定键。
    """

    return "\x1f".join(("bot", _as_text(platform), _as_text(bot_account_id)))


def build_bot_key_from_message(message: NormalizedMessage) -> str:
    """由消息推导机器人状态键。

    Args:
        message: 归一化消息。

    Returns:
        str: 稳定键。
    """

    return build_bot_key(message.platform, message.bot_account_id)


def normalize_inbound(
    message: Mapping[str, Any],
    *,
    default_platform: str = "qq",
) -> Optional[NormalizedMessage]:
    """把宿主入站消息字典转换为 :class:`NormalizedMessage`。

    Args:
        message: ``chat.receive.after_process`` 的 ``message`` 参数。
        default_platform: 字典缺少 ``platform`` 时使用的平台名。

    Returns:
        Optional[NormalizedMessage]: 归一化消息；缺少必要身份字段时返回 ``None``。
    """

    if not isinstance(message, Mapping):
        return None

    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        message_info = {}
    user_info = message_info.get("user_info")
    if not isinstance(user_info, Mapping):
        user_info = {}
    group_info = message_info.get("group_info")
    if not isinstance(group_info, Mapping):
        group_info = {}

    user_id = _as_text(user_info.get("user_id"))
    bot_account_id = _as_text(message.get("account_id")) or _as_text(message.get("self_id"))
    message_id = _as_text(message.get("message_id"))
    platform = _as_text(message.get("platform")) or default_platform

    if not user_id or not message_id or not bot_account_id:
        return None

    timestamp_raw = _as_text(message.get("timestamp"))
    try:
        timestamp = int(float(timestamp_raw))
    except (TypeError, ValueError):
        timestamp = 0

    text = _as_text(message.get("processed_plain_text"))
    if not text:
        raw_message = message.get("raw_message")
        if isinstance(raw_message, str):
            text = raw_message

    mentions_other = False
    reply_to = _as_text(message.get("reply_to"))
    raw = message.get("raw_message")
    components = raw.get("components", []) if isinstance(raw, Mapping) else raw
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, Mapping):
                continue
            data = component.get("data")
            if not isinstance(data, Mapping):
                continue
            if component.get("type") == "at":
                target = _as_text(data.get("target_user_id"))
                mentions_other |= bool(target and target != bot_account_id)
            if component.get("type") == "reply" and not reply_to:
                reply_to = _as_text(data.get("target_message_id"))

    return NormalizedMessage(
        platform=platform,
        bot_account_id=bot_account_id,
        scope=_as_text(message.get("scope")),
        user_id=user_id,
        group_id=_as_text(group_info.get("group_id")),
        session_id=_as_text(message.get("session_id")),
        message_id=message_id,
        text=text,
        timestamp=timestamp,
        is_at=bool(message.get("is_at")),
        is_mentioned=bool(message.get("is_mentioned")),
        is_notify=bool(message.get("is_notify")),
        is_command=bool(message.get("is_command")),
        is_picture=bool(message.get("is_picture")),
        is_emoji=bool(message.get("is_emoji")),
        reply_to=reply_to,
        raw_fields={
            "user_nickname": _as_text(user_info.get("user_nickname")),
            "group_name": _as_text(group_info.get("group_name")),
            "mentions_other_user": mentions_other,
        },
    )


def is_self_message(message: NormalizedMessage) -> bool:
    """判断消息是否由机器人自己发出。

    Args:
        message: 归一化消息。

    Returns:
        bool: 来自机器人账号本身时返回 ``True``。
    """

    return bool(message.bot_account_id) and message.user_id == message.bot_account_id


def is_excluded(message: NormalizedMessage) -> bool:
    """判断消息是否属于必须排除的类型（通知、命令、自身回声）。

    Args:
        message: 归一化消息。

    Returns:
        bool: 需要排除时返回 ``True``。
    """

    if is_self_message(message):
        return True
    return bool(message.is_notify or message.is_command)


def describes(message: NormalizedMessage) -> Dict[str, Any]:
    """生成不含敏感原文的诊断摘要。

    Args:
        message: 归一化消息。

    Returns:
        Dict[str, Any]: 可安全写入日志的字段。
    """

    return {
        "platform": message.platform,
        "bot_account_id": message.bot_account_id,
        "scope": message.scope,
        "user_id": message.user_id,
        "group_id": message.group_id,
        "session_id": message.session_id,
        "message_id": message.message_id,
        "timestamp": message.timestamp,
        "text_hash": text_hash(message.text),
        "length": len(normalize_text(message.text)),
        "is_group": message.is_group,
        "is_at": message.is_at,
        "is_command": message.is_command,
        "is_notify": message.is_notify,
        "has_reply_to": bool(message.reply_to),
    }
