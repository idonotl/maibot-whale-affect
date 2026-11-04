"""管理操作层：状态查询、调整、重置、撤销称呼、备份与审计。

设计要点（技术路线 §12 / §13 P4）：

- 本模块不感知 Host、Hook 或 WebUI 协议，只做纯逻辑，因此可以脱离宿主单测。
- 鉴权由调用方（``plugin.py`` 的 ``@API`` 方法）依据 ``config.admin`` 完成；
  本模块额外提供 :func:`check_admin` 便于复用同一条判定规则。
- 所有写操作都会追加一行 ``admin_audit``，且**不抛异常表达业务失败**：
  统一返回 ``{"ok": False, "error": "..."}``，避免网关把异常吞成 502。
- 维护者只有管理权，不预设 F=100；调整必须显式给值并留审计。
"""

from __future__ import annotations

import logging

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import WhaleAffectConfig
from .engine import AffectEngine, from_centi, to_centi
from .host_adapter import build_bot_key
from .models import Reaction, UserRelationState, affinity_level, mood_level
from .repository import AffectRepository, SqliteStateStorage

logger = logging.getLogger("plugin.local.whale_affect.admin_api")

# 单次管理查询返回的最大条数，避免一次拉爆网关 512 KiB 结果上限。
MAX_LIST_LIMIT = 200


def _clamp_count(value: Any, default: int, lower: int, upper: int) -> int:
    """把外部传入的计数参数夹到安全区间。

    注意不能用 ``value or default``：WebUI 的数字输入框会真的传 ``0``，
    那种情况应该夹到下界而不是悄悄换成默认值。

    Args:
        value: 外部传入的值。
        default: 无法解析为整数时的兜底值。
        lower: 下界。
        upper: 上界。

    Returns:
        int: 夹取后的整数。
    """

    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = int(default)
    return max(lower, min(parsed, upper))

# 审计里 before/after 只保留可读标量，避免整行 18 列噪声。
_AUDIT_FIELDS: Tuple[str, ...] = (
    "affinity_centi",
    "offset_centi",
    "active_days",
    "valid_interactions",
    "preferred_title",
    "title_opt_in",
    "title_enabled",
    "reaction",
)


@dataclass(frozen=True)
class AdminContext:
    """管理操作所需的依赖集合。

    Attributes:
        repository: 插件仓储，提供查询与写事务。
        storage: 引擎存储适配层。
        engine: 状态引擎，用于只读投影。
        config: 当前插件配置。
    """

    repository: AffectRepository
    storage: SqliteStateStorage
    engine: AffectEngine
    config: WhaleAffectConfig


def check_admin(context: AdminContext, actor_user_id: str) -> bool:
    """判断给定 QQ 号是否为配置中的维护者。

    Args:
        context: 管理上下文。
        actor_user_id: 操作者 QQ 号（必须来自真实入站 user_id，不能由消息自报）。

    Returns:
        bool: 是否为维护者。
    """

    target = str(actor_user_id or "").strip()
    if not target:
        return False
    return target in {str(item).strip() for item in context.config.admin.user_ids}


def authorize_actor(
    context: AdminContext,
    *,
    via_channel: str,
    actor_user_id: str = "",
) -> Optional[Dict[str, Any]]:
    """判定一次管理操作是否被允许。

    技术路线 §12：管理动作必须通过**已认证的本机管理通道**；不能仅因 API 名叫
    admin 就认为有权限。本插件的 WebUI 网关已经用 ``maibot_session`` Cookie
    完成本机认证，因此 ``via_channel="webui"`` 视为已授权。未来的 QQ 管理命令
    必须传 ``via_channel="qq"`` 并提供真实入站 ``actor_user_id``。

    Args:
        context: 管理上下文。
        via_channel: 调用通道，``webui`` 或 ``qq``。
        actor_user_id: QQ 通道下的操作者 QQ 号。

    Returns:
        Optional[Dict[str, Any]]: 拒绝响应；允许时返回 ``None``。
    """

    channel = str(via_channel or "").strip().lower()
    if channel == "webui":
        return None
    if channel == "qq" and check_admin(context, actor_user_id):
        return None
    return {
        "ok": False,
        "error": "该管理操作未通过已认证的通道或维护者身份校验",
        "via_channel": channel,
        "actor_user_id": str(actor_user_id or ""),
    }


def audit_actor_key(*, via_channel: str, actor_user_id: str = "") -> str:
    """构造审计里的操作者标识。

    Args:
        via_channel: 调用通道。
        actor_user_id: QQ 通道下的操作者 QQ 号。

    Returns:
        str: 审计用操作者标识。
    """

    channel = str(via_channel or "").strip().lower() or "unknown"
    actor = str(actor_user_id or "").strip()
    if channel == "webui":
        return f"webui:local:{actor}".rstrip(":")
    return f"{channel}:{actor}" if actor else f"{channel}:"


# ──── 身份解析 ────────────────────────────────────────────


def describe_user_key(user_key: str) -> Dict[str, str]:
    """把身份键拆成人类可读的四段；格式非法时原样返回。

    Args:
        user_key: ``\\x1f`` 连接的身份键。

    Returns:
        Dict[str, str]: 平台、机器人账号、作用域与用户 ID。
    """

    parts = str(user_key).split("\x1f")
    if len(parts) != 4:
        return {"platform": "", "bot_account_id": "", "scope": "", "user_id": str(user_key)}
    return {
        "platform": parts[0],
        "bot_account_id": parts[1],
        "scope": parts[2],
        "user_id": parts[3],
    }


def resolve_user_keys(context: AdminContext, identifier: str) -> List[str]:
    """把管理员输入的标识解析为一组身份键。

    支持两种输入：完整身份键（含 ``\\x1f``）或纯 QQ 号。

    Args:
        context: 管理上下文。
        identifier: 用户输入的标识。

    Returns:
        List[str]: 匹配到的身份键列表。
    """

    raw = str(identifier or "").strip()
    if not raw:
        return []
    if "\x1f" in raw:
        return [raw]
    return context.storage.find_user_keys(raw)


# ──── 只读查询 ────────────────────────────────────────────


def read_global_state(context: AdminContext) -> Dict[str, Any]:
    """读取全局状态：心情 M、当日统计、审计数与错误计数。

    Args:
        context: 管理上下文。

    Returns:
        Dict[str, Any]: 全局状态。
    """

    engine = context.engine
    now = engine.clock.now_utc()
    local_date = engine.clock.local_date(now)
    bot_key = _bot_key_from_config(context.config)
    mood = engine.project_global(bot_key, now)
    aggregate = context.storage.daily_aggregate(local_date)
    status_counts = context.storage.count_events_by_status(local_date)
    suppressed = context.storage.count_suppressed_today(local_date)
    stored_mood = context.storage.load_mood(bot_key)

    return {
        "ok": True,
        "local_date": local_date,
        "timezone": context.config.mood.timezone,
        "bot_key": bot_key,
        "mood": from_centi(mood.mood_centi),
        "mood_initial": float(context.config.mood.daily_initial),
        "stored_mood": None if stored_mood is None else from_centi(stored_mood.mood_centi),
        "stored_mood_date": "" if stored_mood is None else stored_mood.local_date,
        "enabled": bool(context.config.plugin.enabled),
        "shadow_mode": bool(context.config.plugin.shadow_mode),
        "aggregate": aggregate,
        "events_by_status": status_counts,
        "suppressed_today": suppressed,
        "db_error_count": context.repository.error_count,
        "db_path": str(context.repository.db_path),
    }


def read_user_state(context: AdminContext, identifier: str) -> Dict[str, Any]:
    """读取单用户的完整状态。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。

    Returns:
        Dict[str, Any]: 用户状态；找不到时返回 ``ok=False``。
    """

    keys = resolve_user_keys(context, identifier)
    if not keys:
        return {"ok": False, "error": f"找不到用户: {identifier}", "candidates": []}
    if len(keys) > 1:
        return {
            "ok": False,
            "error": f"标识匹配到 {len(keys)} 个身份键，请填写完整身份键",
            "candidates": [describe_user_key(key) for key in keys],
        }

    user_key = keys[0]
    engine = context.engine
    now = engine.clock.now_utc()
    local_date = engine.clock.local_date(now)
    bot_key = _bot_key_from_config(context.config)
    user = context.storage.load_user(user_key, engine.config)
    snapshot = engine.build_snapshot(user_key, bot_key, "private", now)
    daily = context.storage.load_daily(user_key, local_date)
    recent = context.storage.recent_events(user_key=user_key, limit=10)
    identity = describe_user_key(user_key)

    return {
        "ok": True,
        "user_key": user_key,
        "identity": identity,
        "local_date": local_date,
        "affinity": from_centi(snapshot.affinity_centi),
        "mood": from_centi(snapshot.mood_centi),
        "offset": from_centi(snapshot.offset_centi),
        "effective": from_centi(snapshot.effective_centi),
        "mood_level": _mood_level(snapshot.effective_centi),
        "affinity_level": _affinity_level(snapshot.affinity_centi),
        "reaction": snapshot.reaction,
        "reaction_expires_utc": snapshot.reaction_expires_utc,
        "title_allowed": snapshot.title_allowed,
        "title_should_use": snapshot.title_should_use,
        "preferred_title": user.preferred_title,
        "title_opt_in": bool(user.title_opt_in),
        "title_enabled": bool(user.title_enabled),
        "active_days": int(user.active_days),
        "valid_interactions": int(user.valid_interactions),
        "successful_reply_count": int(user.successful_reply_count),
        "last_attack_utc": int(user.last_attack_utc),
        "daily": {
            "valid_count": int(daily.valid_count),
            "positive_f_used": from_centi(daily.positive_f_used),
            "negative_f_used": from_centi(daily.negative_f_used),
            "positive_m_used": from_centi(daily.positive_m_used),
            "negative_m_used": from_centi(daily.negative_m_used),
            "semantic_positive_count": int(daily.semantic_positive_count),
            "active_bonus_granted": bool(daily.active_bonus_granted),
            "apology_repair_granted": bool(daily.apology_repair_granted),
        },
        "recent_events": recent,
    }


def list_users(context: AdminContext, *, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    """分页列出已建立关系的用户。

    Args:
        context: 管理上下文。
        page: 页码，从 1 开始。
        page_size: 每页条数。

    Returns:
        Dict[str, Any]: 分页结果。
    """

    size = _clamp_count(page_size, 20, 1, 50)
    index = _clamp_count(page, 1, 1, 1_000_000)
    local_date = context.engine.clock.local_date(context.engine.clock.now_utc())
    total, rows = context.storage.list_users(
        limit=size,
        offset=(index - 1) * size,
        local_date=local_date,
    )
    # 前端分页组件要求 page <= pages；翻页后总数变小时回落到最后一页。
    pages = max(1, (total + size - 1) // size)
    if index > pages:
        index = pages
        total, rows = context.storage.list_users(
            limit=size,
            offset=(index - 1) * size,
            local_date=local_date,
        )
    items = []
    for row in rows:
        identity = describe_user_key(str(row["user_key"]))
        items.append(
            {
                "user_id": identity["user_id"],
                "user_key": row["user_key"],
                "scope": identity["scope"],
                "affinity": from_centi(row["affinity_centi"]),
                "affinity_level": _affinity_level(row["affinity_centi"]),
                "offset": from_centi(row["offset_centi"]),
                "active_days": row["active_days"],
                "valid_interactions": row["valid_interactions"],
                "preferred_title": row["preferred_title"],
                "title_enabled": row["title_enabled"],
                "reaction": row["reaction"],
                "today_valid_count": row["today_valid_count"],
                "today_positive_f": from_centi(row["today_positive_f"]),
                "today_negative_f": from_centi(row["today_negative_f"]),
                "last_event_utc": row["last_event_utc"],
            }
        )
    return {
        "ok": True,
        "total": total,
        "page": index,
        "page_size": size,
        "pages": pages,
        "local_date": local_date,
        "items": items,
    }


def recent_events(context: AdminContext, *, identifier: str = "", limit: int = 20) -> Dict[str, Any]:
    """读取最近事件；``identifier`` 为空时跨用户读取。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号；为空表示不筛选。
        limit: 最大条数。

    Returns:
        Dict[str, Any]: 事件列表。
    """

    size = _clamp_count(limit, 20, 1, MAX_LIST_LIMIT)
    user_key = ""
    if str(identifier or "").strip():
        keys = resolve_user_keys(context, identifier)
        if not keys:
            return {"ok": False, "error": f"找不到用户: {identifier}", "user_key": "", "items": []}
        if len(keys) > 1:
            return {
                "ok": False,
                "error": f"标识匹配到 {len(keys)} 个身份键，请填写完整身份键",
                "candidates": [describe_user_key(key) for key in keys],
                "user_key": "",
                "items": [],
            }
        user_key = keys[0]
    events = context.storage.recent_events(user_key=user_key, limit=size)
    for event in events:
        event["user_id"] = describe_user_key(str(event["user_key"]))["user_id"]
    return {"ok": True, "user_key": user_key, "items": events}


def list_audit(context: AdminContext, *, limit: int = 20) -> Dict[str, Any]:
    """读取最近的管理审计记录。

    Args:
        context: 管理上下文。
        limit: 最大条数。

    Returns:
        Dict[str, Any]: 审计列表。
    """

    size = _clamp_count(limit, 20, 1, MAX_LIST_LIMIT)
    return {"ok": True, "items": context.storage.list_audit(limit=size)}


def export_user(context: AdminContext, identifier: str) -> Dict[str, Any]:
    """导出单用户的插件状态（不含任何消息原文）。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。

    Returns:
        Dict[str, Any]: 导出数据。
    """

    state = read_user_state(context, identifier)
    if not state.get("ok"):
        return state
    keys = resolve_user_keys(context, identifier)
    user_key = keys[0]
    daily = context.storage.load_daily_for_date(state["local_date"]).get(user_key, {})
    return {
        "ok": True,
        "exported_at_utc": context.engine.clock.now_utc(),
        "state": state,
        "daily": daily,
        "events": context.storage.recent_events(user_key=user_key, limit=MAX_LIST_LIMIT),
    }


# ──── 写操作 ──────────────────────────────────────────────


def adjust_affinity(
    context: AdminContext,
    *,
    identifier: str,
    value: float,
    mode: str = "set",
    reason: str = "",
    actor_user_id: str = "",
    via_channel: str = "webui",
) -> Dict[str, Any]:
    """调整指定用户的长期好感 F，并写入审计。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。
        value: 目标好感（``mode="set"``）或增量（``mode="delta"``），单位分。
        mode: ``set`` 或 ``delta``。
        reason: 审计备注。
        actor_user_id: 操作者 QQ 号（仅 QQ 通道需要）。
        via_channel: 调用通道，``webui`` 表示已认证的本机管理通道。

    Returns:
        Dict[str, Any]: 调整结果。
    """

    denied = _deny_if_not_admin(context, actor_user_id, via_channel)
    if denied is not None:
        return denied

    normalized_mode = str(mode or "set").strip().lower()
    if normalized_mode not in {"set", "delta"}:
        return {"ok": False, "error": f"不支持的调整模式: {mode}"}
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"好感值必须是数字: {value!r}"}
    if not -100.0 <= amount <= 100.0:
        return {"ok": False, "error": "好感值必须落在 -100..100 之间"}

    resolved = _require_single_key(context, identifier)
    if isinstance(resolved, dict):
        return resolved
    user_key = resolved

    engine = context.engine
    now = engine.clock.now_utc()
    local_date = engine.clock.local_date(now)
    baseline = context.storage.load_user(user_key, engine.config)
    delta_centi = to_centi(amount)

    def _mutator(before: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        current = before or {}
        base_centi = int(current.get("affinity_centi", baseline.affinity_centi))
        target_centi = base_centi + delta_centi if normalized_mode == "delta" else delta_centi
        target_centi = max(0, min(10000, target_centi))
        merged = dict(current) if current else _default_user_row(baseline, local_date, now)
        merged["affinity_centi"] = target_centi
        merged["version"] = int(current.get("version", 0)) + 1
        return merged

    result = context.storage.mutate_user(user_key, _mutator)
    before, after = result["before"], result["after"]
    _audit(
        context,
        actor_key=audit_actor_key(via_channel=via_channel, actor_user_id=actor_user_id),
        operation=f"adjust_affinity:{normalized_mode}",
        target_key=user_key,
        before=_audit_view(before),
        after={**_audit_view(after), "reason": str(reason or "")},
    )
    return {
        "ok": True,
        "user_key": user_key,
        "before_affinity": from_centi(int(before.get("affinity_centi", baseline.affinity_centi))),
        "after_affinity": from_centi(int(after.get("affinity_centi", 0))),
    }


def reset_user(
    context: AdminContext,
    *,
    identifier: str,
    affinity: Optional[float] = None,
    reason: str = "",
    actor_user_id: str = "",
    via_channel: str = "webui",
) -> Dict[str, Any]:
    """把指定用户重置为「刚认识」状态，保留事件历史。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。
        affinity: 重置后的好感；``None`` 表示用配置里的初始值。
        reason: 审计备注。
        actor_user_id: 操作者 QQ 号（仅 QQ 通道需要）。
        via_channel: 调用通道。

    Returns:
        Dict[str, Any]: 重置结果。
    """

    denied = _deny_if_not_admin(context, actor_user_id, via_channel)
    if denied is not None:
        return denied

    engine = context.engine
    try:
        initial_centi = (
            engine.config.initial_affinity_centi if affinity is None else to_centi(float(affinity))
        )
    except (TypeError, ValueError):
        return {"ok": False, "error": f"好感值必须是数字: {affinity!r}"}
    if not 0 <= initial_centi <= 10000:
        return {"ok": False, "error": "重置后的好感必须落在 0..100"}

    resolved = _require_single_key(context, identifier)
    if isinstance(resolved, dict):
        return resolved
    user_key = resolved

    now = engine.clock.now_utc()
    local_date = engine.clock.local_date(now)
    baseline = context.storage.load_user(user_key, engine.config)

    def _mutator(before: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged = _default_user_row(baseline, local_date, now)
        merged["affinity_centi"] = initial_centi
        merged["version"] = int((before or {}).get("version", 0)) + 1
        return merged

    result = context.storage.mutate_user(user_key, _mutator)
    _audit(
        context,
        actor_key=audit_actor_key(via_channel=via_channel, actor_user_id=actor_user_id),
        operation="reset_user",
        target_key=user_key,
        before=_audit_view(result["before"]),
        after={**_audit_view(result["after"]), "reason": str(reason or "")},
    )
    return {
        "ok": True,
        "user_key": user_key,
        "affinity": from_centi(initial_centi),
        "note": "已清零短期偏移、活跃日与交流计数，事件历史保留",
    }


def revoke_title(
    context: AdminContext,
    *,
    identifier: str,
    reason: str = "",
    actor_user_id: str = "",
    via_channel: str = "webui",
) -> Dict[str, Any]:
    """撤销指定用户的称呼许可，但不扣分。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。
        reason: 审计备注。
        actor_user_id: 操作者 QQ 号（仅 QQ 通道需要）。
        via_channel: 调用通道。

    Returns:
        Dict[str, Any]: 撤销结果。
    """

    denied = _deny_if_not_admin(context, actor_user_id, via_channel)
    if denied is not None:
        return denied

    resolved = _require_single_key(context, identifier)
    if isinstance(resolved, dict):
        return resolved
    user_key = resolved

    engine = context.engine
    now = engine.clock.now_utc()
    local_date = engine.clock.local_date(now)
    baseline = context.storage.load_user(user_key, engine.config)

    def _mutator(before: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        current = before or _default_user_row(baseline, local_date, now)
        merged = dict(current)
        merged["preferred_title"] = ""
        merged["title_opt_in"] = 0
        merged["title_enabled"] = 0
        merged["version"] = int(current.get("version", 0)) + 1
        return merged

    result = context.storage.mutate_user(user_key, _mutator)
    _audit(
        context,
        actor_key=audit_actor_key(via_channel=via_channel, actor_user_id=actor_user_id),
        operation="revoke_title",
        target_key=user_key,
        before=_audit_view(result["before"]),
        after={**_audit_view(result["after"]), "reason": str(reason or "")},
    )
    return {"ok": True, "user_key": user_key, "title_enabled": False}


def delete_user(
    context: AdminContext,
    *,
    identifier: str,
    reason: str = "",
    actor_user_id: str = "",
    via_channel: str = "webui",
) -> Dict[str, Any]:
    """删除指定用户的全部插件状态（不可撤销）。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。
        reason: 审计备注。
        actor_user_id: 操作者 QQ 号（仅 QQ 通道需要）。
        via_channel: 调用通道。

    Returns:
        Dict[str, Any]: 删除结果与各表删除行数。
    """

    denied = _deny_if_not_admin(context, actor_user_id, via_channel)
    if denied is not None:
        return denied

    resolved = _require_single_key(context, identifier)
    if isinstance(resolved, dict):
        return resolved
    user_key = resolved

    before = context.storage.recent_events(user_key=user_key, limit=1)
    removed = context.storage.delete_user(user_key)
    _audit(
        context,
        actor_key=audit_actor_key(via_channel=via_channel, actor_user_id=actor_user_id),
        operation="delete_user",
        target_key=user_key,
        before={"removed": removed, "last_event": before[:1]},
        after={"reason": str(reason or "")},
    )
    return {"ok": True, "user_key": user_key, "removed": removed}


def set_title_opt_in(
    context: AdminContext,
    *,
    identifier: str,
    enabled: bool,
    title: str = "",
    reason: str = "",
    actor_user_id: str = "",
    via_channel: str = "webui",
) -> Dict[str, Any]:
    """设置指定用户是否愿意被称呼，以及偏好的称呼词。

    技术路线 §5：称呼必须用户自愿（``title_opt_in``），维护者不得代替用户开启。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。
        enabled: 是否允许使用称呼。
        title: 偏好称呼词；空串表示回到默认「主人」。
        reason: 审计备注。
        actor_user_id: 操作者 QQ 号（仅 QQ 通道需要）。
        via_channel: 调用通道。

    Returns:
        Dict[str, Any]: 设置结果。
    """

    denied = _deny_if_not_admin(context, actor_user_id, via_channel)
    if denied is not None:
        return denied

    resolved = _require_single_key(context, identifier)
    if isinstance(resolved, dict):
        return resolved
    user_key = resolved

    preferred = str(title or "").strip()
    if len(preferred) > 16:
        return {"ok": False, "error": "称呼词最多 16 个字符"}

    engine = context.engine
    now = engine.clock.now_utc()
    local_date = engine.clock.local_date(now)
    baseline = context.storage.load_user(user_key, engine.config)

    def _mutator(before: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        current = before or _default_user_row(baseline, local_date, now)
        merged = dict(current)
        merged["title_opt_in"] = 1 if enabled else 0
        merged["preferred_title"] = preferred
        merged["title_enabled"] = 0
        merged["version"] = int(current.get("version", 0)) + 1
        return merged

    result = context.storage.mutate_user(user_key, _mutator)
    _audit(
        context,
        actor_key=audit_actor_key(via_channel=via_channel, actor_user_id=actor_user_id),
        operation="set_title_opt_in",
        target_key=user_key,
        before=_audit_view(result["before"]),
        after={**_audit_view(result["after"]), "reason": str(reason or "")},
    )
    return {
        "ok": True,
        "user_key": user_key,
        "title_opt_in": bool(enabled),
        "preferred_title": preferred,
    }


def backup_database(
    context: AdminContext,
    *,
    target_dir: str = "",
    reason: str = "",
    actor_user_id: str = "",
    via_channel: str = "webui",
) -> Dict[str, Any]:
    """使用 SQLite 在线备份 API 生成数据库副本。

    Args:
        context: 管理上下文。
        target_dir: 备份目录；为空时放在数据库同目录的 ``backups`` 子目录。
        reason: 审计备注。
        actor_user_id: 操作者 QQ 号（仅 QQ 通道需要）。
        via_channel: 调用通道。

    Returns:
        Dict[str, Any]: 备份文件路径与大小。
    """

    denied = _deny_if_not_admin(context, actor_user_id, via_channel)
    if denied is not None:
        return denied

    db_path = Path(context.repository.db_path)
    directory = Path(target_dir).expanduser() if str(target_dir or "").strip() else db_path.parent / "backups"
    if not directory.is_absolute():
        directory = db_path.parent / directory
    stamp = context.engine.clock.now_utc()
    target = directory / f"affect-{stamp}.sqlite3"
    try:
        written = context.repository.backup_to(target)
    except Exception as exc:  # noqa: BLE001 - 备份失败必须回报而不是抛出
        return {"ok": False, "error": f"备份失败: {exc}"}

    size = written.stat().st_size if written.exists() else 0
    _audit(
        context,
        actor_key=audit_actor_key(via_channel=via_channel, actor_user_id=actor_user_id),
        operation="backup_database",
        target_key=str(written),
        before={},
        after={"size_bytes": size, "reason": str(reason or "")},
    )
    return {"ok": True, "path": str(written), "size_bytes": size}


# ──── 内部辅助 ────────────────────────────────────────────


def _bot_key_from_config(config: WhaleAffectConfig) -> str:
    """用配置里的平台与机器人账号构造 bot_key。

    Args:
        config: 插件配置。

    Returns:
        str: 与 ``host_adapter.build_bot_key`` 一致的机器人身份键。
    """

    return build_bot_key(str(config.admin.platform), str(config.admin.bot_account_id))


def _mood_level(effective_centi: int) -> str:
    """返回心情分段标识。

    Args:
        effective_centi: 有效心情 E 的 centi 整数。

    Returns:
        str: 分段标识。
    """

    return mood_level(effective_centi)


def _affinity_level(affinity_centi: int) -> str:
    """返回关系分段标识。

    Args:
        affinity_centi: 长期好感 F 的 centi 整数。

    Returns:
        str: 分段标识。
    """

    return affinity_level(affinity_centi)


def _default_user_row(baseline: UserRelationState, local_date: str, now_utc: int) -> Dict[str, Any]:
    """构造一行「刚认识」的用户状态（未落库）。

    Args:
        baseline: ``UserRelationState`` 实例。
        local_date: 当前本地日期。
        now_utc: 当前时间戳。

    Returns:
        Dict[str, Any]: 列名到取值的字典。
    """

    return {
        "affinity_centi": int(baseline.affinity_centi),
        "offset_centi": 0,
        "offset_local_date": local_date,
        "offset_update_utc": int(now_utc),
        "active_days": 0,
        "valid_interactions": 0,
        "last_valid_utc": 0,
        "preferred_title": "",
        "title_opt_in": 0,
        "title_enabled": 0,
        "successful_reply_count": int(baseline.successful_reply_count),
        "last_title_reply_count": int(baseline.successful_reply_count),
        "last_attack_utc": 0,
        "reaction": Reaction.NONE.value,
        "reaction_expires_utc": 0,
        "reaction_replies_left": 0,
        "version": 0,
    }


def _audit_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """把一行关系状态压缩为审计可读的标量字典。

    Args:
        row: 数据库行字典。

    Returns:
        Dict[str, Any]: 仅含关键字段的字典。
    """

    view: Dict[str, Any] = {}
    for field in _AUDIT_FIELDS:
        if field not in row:
            continue
        value = row[field]
        if field.endswith("_centi") and isinstance(value, int):
            view[field] = from_centi(value)
        elif field in {"title_opt_in", "title_enabled"}:
            view[field] = bool(value)
        elif field in {"active_days", "valid_interactions"}:
            view[field] = int(value)
        else:
            view[field] = value
    return view


def _audit(
    context: AdminContext,
    *,
    actor_key: str,
    operation: str,
    target_key: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> None:
    """追加一行审计；审计写失败不影响主操作结果。

    Args:
        context: 管理上下文。
        actor_key: 操作者身份。
        operation: 操作名。
        target_key: 操作对象身份键。
        before: 修改前状态。
        after: 修改后状态。
    """

    try:
        context.storage.write_audit(
            actor_key=str(actor_key),
            operation=str(operation),
            target_key=str(target_key),
            before=before,
            after=after,
        )
    except Exception:  # noqa: BLE001 - 审计失败不能回滚已完成的管理操作
        logger.warning("whale-affect: 管理审计写入失败", exc_info=True)


def _deny_if_not_admin(
    context: AdminContext,
    actor_user_id: str,
    via_channel: str = "webui",
) -> Optional[Dict[str, Any]]:
    """未通过鉴权时的统一拒绝响应。

    Args:
        context: 管理上下文。
        actor_user_id: 操作者 QQ 号。
        via_channel: 调用通道。

    Returns:
        Optional[Dict[str, Any]]: 拒绝响应；允许时返回 ``None``。
    """

    return authorize_actor(context, via_channel=via_channel, actor_user_id=actor_user_id)


def _require_single_key(context: AdminContext, identifier: str) -> Any:
    """解析标识并要求唯一匹配。

    Args:
        context: 管理上下文。
        identifier: 完整身份键或 QQ 号。

    Returns:
        Any: 唯一身份键字符串；失败时返回错误响应字典。
    """

    keys = resolve_user_keys(context, identifier)
    if not keys:
        return {"ok": False, "error": f"找不到用户: {identifier}"}
    if len(keys) > 1:
        return {
            "ok": False,
            "error": f"标识匹配到 {len(keys)} 个身份键，请填写完整身份键",
            "candidates": [describe_user_key(key) for key in keys],
        }
    return keys[0]
