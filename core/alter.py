"""Alter 情绪偏移系统（对应上游 `src/alter.ts`，逐行移植）。

只衡量本轮新事件对整体氛围造成的净变化：正数偏严肃，负数偏轻松，
范围 -5..5。按参与者/全局独立累计，达到动态阈值后由侧端模型写一句
氛围描述，作为临时参考注入后续提示词；同向强化、反向消退、低于
最小权重自动清除。纯状态转移，全部可测。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HOUR_MS = 60 * 60 * 1000
HISTORY_LIMIT = 50
SCOPE_LIMIT = 32
ALTER_ANALYSIS_COOLDOWN_MS = 5 * 60 * 1000

#: 对应 DEFAULT_ALTER_SYSTEM_CONFIG（默认值与上游一致）。
DEFAULT_ALTER_SYSTEM_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "baseThreshold": 10,
    "densityFactor": 0.3,
    "sameDirectionBoost": 0.05,
    "oppositeDecay": 0.15,
    "minWeight": 0.2,
    "maxIntensity": 2,
    "temperature": 0.3,
    "topP": 1,
    "maxTokens": 400,
    "timeout": 30000,
    "prompt": "",
}


def resolve_alter_system_config(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(DEFAULT_ALTER_SYSTEM_CONFIG)
    if isinstance(value, dict):
        merged.update(value)
    return merged


def normalize_alter_value(value: Any) -> Optional[int]:
    """把模型输出的 alter 归一为 -5..5 整数；非法返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return max(-5, min(5, int(round(float(value)))))


def _finite(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    numeric = float(value)
    return numeric if math.isfinite(numeric) else fallback


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _date_value(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _normalized_iso(value: Any) -> Optional[str]:
    parsed = _date_value(value)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if parsed else None


def _normalize_phase(value: Any) -> str:
    text = str(value)
    return text if text in ("advance", "conversation-follow-up", "user-message", "intent-due") else "user-message"


def _normalize_participant_id(value: Any) -> str:
    return str(value).strip()[:255] if isinstance(value, str) else ""


def normalize_pending_scopes(value: Any) -> List[Dict[str, Any]]:
    """合并同名 scope、截断上限（对应 normalizePendingScopes）。"""
    if not isinstance(value, list):
        return []
    by_participant: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        participant_id = _normalize_participant_id(item.get("participantId"))
        if participant_id not in by_participant:
            order.append(participant_id)
            by_participant[participant_id] = {"participantId": participant_id, "alterValue": 0.0}
        existing = by_participant[participant_id]
        existing["alterValue"] = _clamp(
            _finite(existing.get("alterValue"), 0.0) + _clamp(_finite(item.get("alterValue"), 0.0), -1000, 1000),
            -1000,
            1000,
        )
        attempt = _normalized_iso(item.get("lastAnalysisAttemptAt"))
        if attempt:
            existing["lastAnalysisAttemptAt"] = attempt
    scopes = [by_participant[key] for key in order]
    active = [scope for scope in scopes if scope["alterValue"] != 0]
    if len(active) >= SCOPE_LIMIT:
        return active
    idle = [scope for scope in scopes if scope["alterValue"] == 0]
    return (active + idle)[:SCOPE_LIMIT]


def create_alter_system_state(now: Optional[datetime] = None) -> Dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    return {
        "alterValue": 0,
        "alterWeight": 0,
        "lastTriggerDirection": 0,
        "emotionalOffset": None,
        "history": [],
        "pendingScopes": [],
        "lastUpdatedAt": _normalized_iso(moment),
    }


def normalize_alter_system_state(value: Any) -> Optional[Dict[str, Any]]:
    """宽容读取 state.alterSystem（对应 normalizeAlterSystemState）。"""
    if not isinstance(value, dict):
        return None
    history: List[Dict[str, Any]] = []
    raw_history = value.get("history")
    if isinstance(raw_history, list):
        for index, entry in enumerate(raw_history):
            if not isinstance(entry, dict):
                continue
            item: Dict[str, Any] = {
                "turn": max(1, int(_finite(entry.get("turn"), index + 1))),
                "phase": _normalize_phase(entry.get("phase")),
                "alter": normalize_alter_value(entry.get("alter")) or 0,
                "alterValue": _clamp(_finite(entry.get("alterValue"), 0.0), -1000, 1000),
                "timestamp": _normalized_iso(entry.get("timestamp")) or "1970-01-01T00:00:00.000Z",
            }
            participant_id = _normalize_participant_id(entry.get("participantId"))
            if participant_id:
                item["participantId"] = participant_id
            history.append(item)
    emotional_offset = None
    raw_offset = value.get("emotionalOffset")
    if isinstance(raw_offset, dict) and isinstance(raw_offset.get("description"), str):
        emotional_offset = {
            "direction": "relaxed" if raw_offset.get("direction") == "relaxed" else "serious",
            "description": str(raw_offset.get("description")).strip()[:800],
            "intensity": _clamp(_finite(raw_offset.get("intensity"), 1.0), 0.0, 3.0),
            "generatedAt": _normalized_iso(raw_offset.get("generatedAt")) or "1970-01-01T00:00:00.000Z",
        }
    legacy_direction = int(math.copysign(1, _finite(value.get("lastTriggerAlter"), 0))) if _finite(value.get("lastTriggerAlter"), 0) else 0
    direction = int(math.copysign(1, _finite(value.get("lastTriggerDirection"), legacy_direction))) if _finite(value.get("lastTriggerDirection"), legacy_direction) else 0
    pending_scopes = normalize_pending_scopes(value.get("pendingScopes"))
    alter_value = _clamp(_finite(value.get("alterValue"), 0.0), -1000, 1000)
    if not pending_scopes and abs(alter_value) > 0:
        pending_scopes.append({"participantId": "", "alterValue": alter_value})
    return {
        "alterValue": alter_value,
        "alterWeight": _clamp(_finite(value.get("alterWeight"), 0.0), 0.0, 1.0),
        "lastTriggerDirection": direction,
        "emotionalOffset": emotional_offset,
        "history": history[-HISTORY_LIMIT:],
        "pendingScopes": pending_scopes,
        "lastUpdatedAt": _normalized_iso(value.get("lastUpdatedAt")) or "1970-01-01T00:00:00.000Z",
        "lastAnalysisAttemptAt": _normalized_iso(value.get("lastAnalysisAttemptAt")),
    }


def calculate_alter_threshold(
    history: List[Dict[str, Any]], config: Dict[str, Any], now: Optional[datetime] = None
) -> float:
    """动态阈值：最近一小时写作密度越高，阈值越低（不低于基础值一半）。"""
    moment = now or datetime.now(timezone.utc)
    one_hour_ago = moment.timestamp() * 1000 - HOUR_MS
    turns = 0
    for entry in history:
        parsed = _date_value(entry.get("timestamp"))
        if parsed and parsed.timestamp() * 1000 >= one_hour_ago:
            turns += 1
    density = min(turns / 10.0, 1.0)
    base = max(1.0, _finite(config.get("baseThreshold"), 10))
    factor = _clamp(_finite(config.get("densityFactor"), 0.3), 0.0, 1.0)
    return max(base * 0.5, base * (1 - density * factor))


def _adjust_alter_weight(weight: float, same_direction: bool, magnitude: float, config: Dict[str, Any]) -> float:
    rate = _finite(config.get("sameDirectionBoost"), 0.05) if same_direction else -_finite(config.get("oppositeDecay"), 0.15)
    return _clamp(weight + max(0.0, magnitude) * rate, 0.0, 1.0)


def _find_scope(scopes: List[Dict[str, Any]], participant_id: str) -> Optional[Dict[str, Any]]:
    for scope in scopes:
        if scope.get("participantId") == participant_id:
            return scope
    return None


def _ensure_scope(state: Dict[str, Any], participant_id: str) -> Dict[str, Any]:
    scopes = state.setdefault("pendingScopes", [])
    scope = _find_scope(scopes, participant_id)
    if scope is None:
        scope = {"participantId": participant_id, "alterValue": 0.0}
        scopes.append(scope)
    return scope


def _total_pending(scopes: List[Dict[str, Any]]) -> float:
    return _clamp(sum(_finite(scope.get("alterValue"), 0.0) for scope in scopes or []), -1000, 1000)


def alter_history_for_scope(history: List[Dict[str, Any]], participant_id: str = "") -> List[Dict[str, Any]]:
    source = _normalize_participant_id(participant_id)
    return [entry for entry in history if _normalize_participant_id(entry.get("participantId")) == source]


def advance_alter_system(
    current: Optional[Dict[str, Any]],
    alter: int,
    phase: str,
    now: datetime,
    config: Dict[str, Any],
    participant_id: str = "",
) -> Dict[str, Any]:
    """一轮写作产生的 alter 计入 scope；返回阈值与是否触发。"""
    state = (
        {**current, "history": list(current.get("history") or []), "pendingScopes": normalize_pending_scopes(current.get("pendingScopes"))}
        if current
        else create_alter_system_state(now)
    )
    if not state.get("pendingScopes") and abs(_finite(state.get("alterValue"), 0.0)) > 0:
        state["pendingScopes"] = [{"participantId": "", "alterValue": state["alterValue"]}]
    source_id = _normalize_participant_id(participant_id)
    scope = _ensure_scope(state, source_id)
    scope["alterValue"] = _clamp(_finite(scope.get("alterValue"), 0.0) + alter, -1000, 1000)
    state["alterValue"] = _total_pending(state["pendingScopes"])
    direction = int(math.copysign(1, alter)) if alter else 0
    offset_expired = False
    if state.get("emotionalOffset") and direction:
        state["alterWeight"] = _adjust_alter_weight(
            _finite(state.get("alterWeight"), 0.0),
            direction == int(state.get("lastTriggerDirection") or 0),
            abs(alter),
            config,
        )
        if state["alterWeight"] < _finite(config.get("minWeight"), 0.2):
            state["emotionalOffset"] = None
            state["alterWeight"] = 0.0
            offset_expired = True
    last_turn = state["history"][-1]["turn"] if state.get("history") else 0
    history_entry: Dict[str, Any] = {
        "turn": last_turn + 1,
        "phase": _normalize_phase(phase),
        "alter": alter,
        "alterValue": state["alterValue"],
        "timestamp": _normalized_iso(now),
    }
    if source_id:
        history_entry["participantId"] = source_id
    state["history"].append(history_entry)
    state["history"] = state["history"][-HISTORY_LIMIT:]
    state["lastUpdatedAt"] = _normalized_iso(now)
    scope_history = alter_history_for_scope(state["history"], source_id)
    threshold = calculate_alter_threshold(scope_history, config, now)
    return {
        "state": state,
        "threshold": threshold,
        "offsetExpired": offset_expired,
        "thresholdReached": abs(_finite(scope.get("alterValue"), 0.0)) >= threshold,
        "sourceParticipantId": source_id,
        "triggerValue": _finite(scope.get("alterValue"), 0.0),
    }


def complete_alter_analysis(
    state: Dict[str, Any],
    description: str,
    threshold: float,
    now: datetime,
    config: Dict[str, Any],
    participant_id: str = "",
    analyzed_value: Optional[float] = None,
) -> Dict[str, Any]:
    """侧端分析返回后落库：抵消触发值、写入 emotionalOffset。"""
    source_id = _normalize_participant_id(participant_id)
    scopes = normalize_pending_scopes(state.get("pendingScopes"))
    if not scopes and abs(_finite(state.get("alterValue"), 0.0)) > 0:
        scopes.append({"participantId": "", "alterValue": _finite(state.get("alterValue"), 0.0)})
    holder = {**state, "pendingScopes": scopes}
    scope = _ensure_scope(holder, source_id)
    trigger_value = analyzed_value if analyzed_value is not None else _finite(scope.get("alterValue"), 0.0)
    if not math.isfinite(trigger_value) or not trigger_value:
        return state
    if analyzed_value is not None:
        if (
            (trigger_value > 0) != (_finite(scope.get("alterValue"), 0.0) > 0)
            or abs(_finite(scope.get("alterValue"), 0.0)) < threshold
        ):
            return state
    direction = int(math.copysign(1, trigger_value))
    scope["alterValue"] = _clamp(_finite(scope.get("alterValue"), 0.0) - trigger_value, -1000, 1000)
    scope.pop("lastAnalysisAttemptAt", None)
    return {
        **state,
        "alterValue": _total_pending(scopes),
        "pendingScopes": scopes,
        "alterWeight": 1.0,
        "lastTriggerDirection": direction,
        "emotionalOffset": {
            "direction": "serious" if direction > 0 else "relaxed",
            "description": str(description or "").strip()[:800],
            "intensity": min(abs(trigger_value) / max(1.0, threshold), _finite(config.get("maxIntensity"), 2)),
            "generatedAt": _normalized_iso(now),
        },
        "lastUpdatedAt": _normalized_iso(now),
    }


def emotional_offset_for_prompt(state: Optional[Dict[str, Any]], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 emotionalOffset 投影进提示词（对应 emotionalOffsetForPrompt）。"""
    if not config.get("enabled") or not state:
        return None
    offset = state.get("emotionalOffset")
    if not offset:
        return None
    if _finite(state.get("alterWeight"), 0.0) < _finite(config.get("minWeight"), 0.2):
        return None
    return {**offset, "weight": _finite(state.get("alterWeight"), 0.0)}


def alter_scope_cooling_down(
    state: Optional[Dict[str, Any]], participant_id: str = "", now: Optional[datetime] = None,
    cooldown_ms: int = ALTER_ANALYSIS_COOLDOWN_MS,
) -> bool:
    """失败冷却：同一 scope 5 分钟内不重复请求分析。"""
    moment = now or datetime.now(timezone.utc)
    source_id = _normalize_participant_id(participant_id)
    scope = _find_scope(normalize_pending_scopes((state or {}).get("pendingScopes")), source_id)
    last_attempt = _date_value(
        scope.get("lastAnalysisAttemptAt") if scope else (state or {}).get("lastAnalysisAttemptAt")
    )
    return bool(last_attempt and (moment.timestamp() - last_attempt.timestamp()) * 1000 < cooldown_ms)


def mark_alter_scope_analysis_attempt(
    state: Dict[str, Any], participant_id: str = "", now: Optional[datetime] = None
) -> Dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    scopes = normalize_pending_scopes(state.get("pendingScopes"))
    scope = _ensure_scope({**state, "pendingScopes": scopes}, _normalize_participant_id(participant_id))
    scope["lastAnalysisAttemptAt"] = _normalized_iso(moment)
    return {**state, "pendingScopes": scopes}


def clear_alter_scope_analysis_attempt(state: Dict[str, Any], participant_id: str = "") -> Dict[str, Any]:
    source_id = _normalize_participant_id(participant_id)
    scopes = normalize_pending_scopes(state.get("pendingScopes"))
    scope = _find_scope(scopes, source_id)
    if scope:
        scope.pop("lastAnalysisAttemptAt", None)
    return {**state, "pendingScopes": scopes}


def alter_scope_value(state: Optional[Dict[str, Any]], participant_id: str = "") -> float:
    scope = _find_scope(normalize_pending_scopes((state or {}).get("pendingScopes")), _normalize_participant_id(participant_id))
    return _finite(scope.get("alterValue"), 0.0) if scope else 0.0
