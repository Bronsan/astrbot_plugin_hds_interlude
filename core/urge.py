"""Urge 弹性推进（对应上游 `src/urge.ts`，rc57 算法逐条移植）。

设计要点（与上游一致）：

- 状态只有一份，存 `story.state["urge"]`，随故事一起持久化；
- 用户消息只做"降低间隔"的密度信号（`urge_user_event`），不直接触发回复；
- 主叙事回合可以返回 `urge` 草案；只有 `basisQuote` 能在正文中逐字命中时才采纳；
- 采纳后的高值会"上膛"（armed），下一次确认发生的主动联系把它转成 burst 窗口；
- 调度只决定"下一次推进"，不决定"发不发消息"。

本模块是纯函数集合，不导入 astrbot，也不触碰数据库，便于单测。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

#: 上游以毫秒为时间单位，这里统一用秒。
MINUTE = 60.0

#: 生活节奏对推进速度的乘数（sleep 时几乎停摆）。
ATTENTION_MULTIPLIERS: Dict[str, float] = {
    "phone-focused": 1.4,
    "available": 1.0,
    "occupied": 0.7,
    "resting": 0.5,
    "asleep": 0.15,
}

#: `plan_urge` 使用的节奏判定新鲜度（上游 rhythm.stale 由生活节奏刷新时置位）。
RHYTHM_STALE_SECONDS = 90 * MINUTE


def _record(value: Any) -> Dict[str, Any]:
    """仅接受普通对象，其余一律视为空（对应上游 `record()`）。"""
    return dict(value) if isinstance(value, Mapping) else {}


def _finite(value: Any, fallback: float, low: float, high: float) -> float:
    """数值收敛：非有限数回退默认值，并夹到区间内（对应上游 `finite()`）。"""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        number = float(value)
        if number == number and number not in (float("inf"), float("-inf")):
            return max(low, min(high, number))
    return fallback


def _option(value: Any, allowed: Sequence[str], fallback: str) -> str:
    text = str(value) if isinstance(value, str) else ""
    return text if text in allowed else fallback


def resolve_urge_config(raw: Any) -> Dict[str, Any]:
    """解析 `urge` 配置（对应 `resolveUrgeConfig`）。

    `frequency` 的三档预设与上游逐字一致；`advanced.*` 可以逐项覆盖区间，
    下限会被抬到上限以下，避免出现反向区间。
    """
    config = _record(raw)
    advanced = _record(config.get("advanced"))
    frequency = _option(config.get("frequency"), ("low", "medium", "high", "custom"), "medium")
    defaults = {
        "low": (20, 30, 60, 90, 5, 10, 120, 180),
        "medium": (10, 20, 35, 55, 3, 7, 110, 130),
        "high": (6, 12, 20, 35, 2, 5, 90, 120),
    }[frequency if frequency != "custom" else "medium"]

    def pair(name: str, index: int) -> list:
        low = _finite(advanced.get(f"{name}Min"), defaults[index], 1, 1440)
        high = _finite(advanced.get(f"{name}Max"), defaults[index + 1], 1, 1440)
        return [low, max(low, high)]

    return {
        "enabled": config.get("enabled") is True,
        "frequency": frequency,
        "willingness": _finite(config.get("proactiveWillingnessThreshold"), 0.4, 0, 1),
        "hot": pair("hot", 0),
        "idle": pair("idle", 2),
        "burst": pair("burst", 4),
        "slow": pair("slow", 6),
        "halfLife": _finite(advanced.get("halfLifeMinutes"), 90, 5, 240),
        "threshold": _finite(advanced.get("burstThreshold"), 0.75, 0, 1),
        "jitter": _finite(advanced.get("jitter"), 0.15, 0, 1),
        "extremeChance": _finite(advanced.get("extremeChance"), 0.03, 0, 1),
        "ttl": _finite(advanced.get("burstTtlMinutes"), 35, 5, 120),
        "budget": int(_finite(advanced.get("burstBudget"), 3, 0, 10)),
        "contactMin": _finite(advanced.get("burstContactMinMinutes"), 5, 1, 60),
        "activeMultiplier": _finite(advanced.get("activeMultiplier"), 1.4, 1, 3),
        "restMultiplier": _finite(advanced.get("restMultiplier"), 0.6, 0.05, 1),
    }


def normalize_urge_state(raw: Any, now: float) -> Dict[str, Any]:
    """清洗持久化状态（对应 `normalizeUrgeState`）。版本不是 1 时整体丢弃。"""
    record = _record(raw)
    if record.get("version") != 1:
        return {"version": 1, "buckets": []}

    def timestamp(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if number <= 0 or number > now or number != number:
            return None
        return number

    buckets = []
    for item in record.get("buckets") or []:
        moment = timestamp(item)
        if moment is not None and now - moment < 240 * MINUTE:
            buckets.append(moment)
    buckets = sorted(set(buckets))[-120:]

    armed_raw = _record(record.get("armed"))
    armed = None
    armed_at = timestamp(armed_raw.get("at"))
    if (
        isinstance(armed_raw.get("participantId"), str)
        and isinstance(armed_raw.get("entryId"), int)
        and armed_at is not None
        and now - armed_at < 10 * MINUTE
    ):
        armed = {
            "participantId": armed_raw["participantId"],
            "entryId": armed_raw["entryId"],
            "at": armed_at,
        }

    burst_raw = _record(record.get("burst"))
    burst = None
    burst_started = timestamp(burst_raw.get("started"))
    if isinstance(burst_raw.get("participantId"), str) and burst_started is not None:
        burst = {
            "participantId": burst_raw["participantId"],
            "started": burst_started,
            "used": int(_finite(burst_raw.get("used"), 0, 0, 10)),
        }

    attention = record.get("rhythmAttention")
    rhythm_at = timestamp(record.get("rhythmAt"))
    return {
        "version": 1,
        "mode": record.get("mode") if isinstance(record.get("mode"), str) else None,
        "buckets": buckets,
        "value": _finite(record.get("value"), 0, 0, 1) if isinstance(record.get("value"), (int, float)) else None,
        "pace": "slow" if record.get("pace") == "slow" else "normal",
        "suggested": (
            _finite(record.get("suggested"), 120, 1, 1440)
            if isinstance(record.get("suggested"), (int, float))
            else None
        ),
        "sourceEntryId": record.get("sourceEntryId") if isinstance(record.get("sourceEntryId"), int) else None,
        "spent": record.get("spent") is True,
        "reason": record.get("reason")[:160] if isinstance(record.get("reason"), str) else None,
        "rhythmAttention": attention if attention in ATTENTION_MULTIPLIERS else None,
        "rhythmAt": rhythm_at,
        "armed": armed,
        "burst": burst,
    }


def urge_user_event(state: Dict[str, Any], now: float) -> Dict[str, Any]:
    """用户消息到达：记一个 2 分钟桶，清空上膛/爆发与建议间隔。"""
    bucket = int(now // (2 * MINUTE)) * 2 * MINUTE
    buckets = sorted(set(list(state.get("buckets") or []) + [float(bucket)]))[-120:]
    return {
        **state,
        "buckets": buckets,
        "pace": "normal",
        "suggested": None,
        "armed": None,
        "burst": None,
        "spent": False,
    }


def urge_density(state: Dict[str, Any], now: float, config: Dict[str, Any]) -> float:
    """对话密度（0..1）：近 15 分钟桶数占 6 的上限，再按半衰期衰减。"""
    buckets = list(state.get("buckets") or [])
    if not buckets:
        return 0.0
    last = buckets[-1]
    recent = len([item for item in buckets if item >= last - 15 * MINUTE])
    decay = 0.5 ** (max(0.0, now - last) / (config["halfLife"] * MINUTE))
    return min(1.0, recent / 6.0) * decay


def commit_urge(
    state: Dict[str, Any],
    raw: Any,
    script: str,
    entry_id: int,
    target: str,
    now: float,
    config: Dict[str, Any],
    random_fn: Any = random.random,
) -> Dict[str, Any]:
    """采纳主叙事返回的 `urge` 草案（对应 `commitUrge`）。

    只有 `basisQuote` 能在本轮正文里逐字命中时才采纳；否则保留原状态。
    """
    record = _record(raw)
    quote = record.get("basisQuote").strip() if isinstance(record.get("basisQuote"), str) else ""
    value = record.get("value")
    if (
        not quote
        or not isinstance(script, str)
        or quote not in script
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        return {**state, "pace": "normal", "suggested": None, "armed": None, "burst": None}

    number = float(value)
    if number != number:
        return {**state, "pace": "normal", "suggested": None, "armed": None, "burst": None}
    if random_fn() < config["extremeChance"]:
        number = random_fn()
    else:
        number = number + (random_fn() * 2 - 1) * config["jitter"]
    number = max(0.0, min(1.0, number))
    pace = "slow" if record.get("pace") == "slow" else "normal"
    high = number >= config["threshold"] and pace != "slow"
    attention = record.get("rhythmAttention")
    attention = attention if attention in ATTENTION_MULTIPLIERS else None
    suggested = record.get("suggestedDelayMinutes")
    return {
        **state,
        "value": number,
        "pace": pace,
        "sourceEntryId": entry_id,
        "rhythmAttention": attention,
        "rhythmAt": now if attention else state.get("rhythmAt"),
        "suggested": _finite(suggested, 120, 1, 1440) if isinstance(suggested, (int, float)) else None,
        "burst": state.get("burst") if high else None,
        "armed": (
            {"participantId": target, "entryId": entry_id, "at": now}
            if high and target and not state.get("spent") and not state.get("burst") and config["budget"] > 0
            else None
        ),
    }


def acknowledge_urge(state: Dict[str, Any], participant_id: str, entry_id: int, now: float) -> Dict[str, Any]:
    """确认主动联系已经发生：上膛转为 burst 窗口（对应 `acknowledgeUrge`）。"""
    armed = state.get("armed")
    if not armed or armed.get("participantId") != participant_id or armed.get("entryId") != entry_id or state.get("spent"):
        return state
    return {
        **state,
        "armed": None,
        "spent": True,
        "burst": {"participantId": participant_id, "started": now, "used": 0},
    }


def urge_burst_active(
    state: Dict[str, Any], now: float, config: Dict[str, Any], participant_id: str = ""
) -> bool:
    burst = state.get("burst")
    if not burst:
        return False
    if participant_id and participant_id != burst.get("participantId"):
        return False
    return (
        now - float(burst.get("started") or 0) < config["ttl"] * MINUTE
        and int(burst.get("used") or 0) <= config["budget"]
        and state.get("pace") != "slow"
    )


def plan_urge(
    state: Dict[str, Any],
    now: float,
    config: Dict[str, Any],
    rest_minutes: int = 0,
    unavailable: bool = False,
    random_fn: Any = random.random,
    rhythm: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """计算下一次自动推进（对应 `planUrge`）。

    返回 `{state, nextAdvanceAt, minutes, reason}`；`state` 需要写回故事状态，
    `nextAdvanceAt` 是 UTC `datetime`。
    """

    def sample(range_pair: Sequence[float]) -> float:
        return range_pair[0] + random_fn() * (range_pair[1] - range_pair[0])

    next_state = dict(state)
    if state.get("pace") == "slow":
        suggested = state.get("suggested")
        if suggested is None:
            minutes = sample(config["slow"])
        else:
            minutes = max(
                config["slow"][0],
                min(config["slow"][1], float(suggested) * (0.9 + random_fn() * 0.2)),
            )
        reason = "script-slow"
        next_state["burst"] = None
        next_state["armed"] = None
    elif not unavailable and urge_burst_active(state, now, config) and int(state["burst"]["used"]) < config["budget"]:
        minutes = sample(config["burst"]) * (2 ** int(state["burst"]["used"]))
        remaining = max(1.0, (float(state["burst"]["started"]) + config["ttl"] * MINUTE - now) / MINUTE)
        minutes = min(minutes, remaining)
        reason = "confirmed-contact-burst"
        next_state["burst"] = {**state["burst"], "used": int(state["burst"]["used"]) + 1}
    else:
        base = sample(config["idle"])
        density = urge_density(state, now, config)
        density_factor = 1 + density * (config["activeMultiplier"] - 1)
        attention = (rhythm or {}).get("attention")
        stale = bool((rhythm or {}).get("stale"))
        if attention in ATTENTION_MULTIPLIERS and not stale:
            rhythm_factor = ATTENTION_MULTIPLIERS[attention]
            reason = f"rhythm:{attention}"
        else:
            rhythm_factor = config["restMultiplier"] if rest_minutes > 0 else 1.0
            reason = "rest-window-fallback" if rest_minutes > 0 else "conversation-density-decay"
        if unavailable:
            rhythm_factor *= 0.5
        minutes = max(1.0, base / (density_factor * rhythm_factor))
        next_state["burst"] = None
        if unavailable:
            next_state["armed"] = None
    next_state["reason"] = reason
    next_at = datetime.fromtimestamp(now + minutes * MINUTE, tz=timezone.utc)
    return {"state": next_state, "nextAdvanceAt": next_at, "minutes": minutes, "reason": reason}


def urgency_hold(state: Dict[str, Any], config: Dict[str, Any], now: float) -> Optional[str]:
    """把当前 urge 状态压成一行诊断文本（给 `interlude.status` 用）。"""
    if not config.get("enabled"):
        return None
    parts = [f"pace={state.get('pace') or 'normal'}"]
    if state.get("value") is not None:
        parts.append(f"value={float(state['value']):.2f}")
    if state.get("armed"):
        parts.append(f"armed={state['armed'].get('participantId')}")
    burst = state.get("burst")
    if burst:
        used = int(burst.get("used") or 0)
        parts.append(f"burst={used}/{config['budget']}")
    if state.get("reason"):
        parts.append(f"reason={state['reason']}")
    return " ".join(parts)


def rhythm_from_state(state: Dict[str, Any], now: float) -> Dict[str, Any]:
    """从持久化状态恢复节奏判定，超时视为 stale（对应上游 rhythm.stale）。"""
    attention = state.get("rhythmAttention")
    moment = state.get("rhythmAt")
    if not isinstance(moment, (int, float)) or attention not in ATTENTION_MULTIPLIERS:
        return {"attention": None, "stale": True}
    return {"attention": attention, "stale": now - float(moment) > RHYTHM_STALE_SECONDS}


def rest_minutes_for(now: datetime, timezone_name: str, windows: Sequence[Mapping[str, Any]]) -> int:
    """当前是否落在休息窗口内；返回窗口剩余分钟（对应上游 restMinutes）。"""
    from .timeutil import in_clock_window, local_clock_minutes, parse_hhmm

    for window in windows:
        if not isinstance(window, Mapping) or window.get("enabled") is False:
            continue
        start = parse_hhmm(str(window.get("start") or "23:00"))
        end = parse_hhmm(str(window.get("end") or "07:00"))
        if start is None or end is None:
            continue
        minutes_now = local_clock_minutes(now, timezone_name)
        if not in_clock_window(minutes_now, start, end):
            continue
        span = (end - start) % 1440 or 1440
        elapsed = (minutes_now - start) % 1440
        return max(1, span - elapsed)
    return 0


def next_advance_delay(config: Dict[str, Any], state: Dict[str, Any], now: datetime) -> Optional[timedelta]:
    """给 `interlude.status` 展示用的剩余时间（不改变状态）。"""
    if not config.get("enabled"):
        return None
    target = state.get("suggested")
    if state.get("pace") == "slow" and isinstance(target, (int, float)):
        return timedelta(minutes=max(1.0, float(target)))
    return None
