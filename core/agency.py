"""主体行动窗口（Agency Window）。

对应上游 `src/agency.ts`。只描述角色能否采取外部联系行动的现实条件
（日程负荷、隐私、设备、生活来源的联系理由），不描述情绪、关系阶段
或联系风格——这是与 Alter 系统互不读取的硬边界。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from .types import ensure_utc

MINUTE_SECONDS = 60
HOUR_SECONDS = 3600

#: 默认配置（对应 DEFAULT_AGENCY_CONFIG）。
DEFAULT_AGENCY_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "maxWindowMinutes": 240,
    "minimumProactiveIntervalMinutes": 60,
    "maxCandidateHours": 24,
    "contactMode": "strict",
    "naturalWillingnessThreshold": 0.25,
    "naturalMinimumIntervalMinutes": 30,
    "proactiveDailyCap": 3,
}

_ACTIVITY_LOADS = ("free", "occupied", "overloaded")
_PRIVACY_VALUES = ("private", "shared", "public")
_DEVICE_VALUES = ("available", "limited", "unavailable")
_ORIGINS = ("life-event", "promise", "practical-update", "relationship-follow-up")
_DISCLOSURES = ("ordinary", "personal")
_OUTCOMES = ("send-now", "recheck-later", "let-go")


def _clamp_number(candidate: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        parsed = float(candidate)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed):
        return fallback
    return max(minimum, min(maximum, parsed))


def resolve_agency_config(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """合并默认值并钳制数值（对应 resolveAgencyConfig）。"""
    merged = dict(DEFAULT_AGENCY_CONFIG)
    if isinstance(value, dict):
        merged.update(value)
    mode = merged.get("contactMode")
    merged["contactMode"] = mode if mode in ("natural", "balanced") else "strict"
    merged["naturalWillingnessThreshold"] = _clamp_number(merged.get("naturalWillingnessThreshold"), 0, 1, 0.25)
    merged["naturalMinimumIntervalMinutes"] = int(
        math.floor(_clamp_number(merged.get("naturalMinimumIntervalMinutes"), 0, 10080, 30))
    )
    merged["proactiveDailyCap"] = int(math.floor(_clamp_number(merged.get("proactiveDailyCap"), 0, 20, 3)))
    return merged


def _positive_ids(value: Any) -> List[int]:
    if not isinstance(value, list):
        return []
    result: List[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.append(number)
    return result


def _grounded_ids(value: Any, valid: Set[int], fallback: Optional[int]) -> List[int]:
    ids = [entry for entry in _positive_ids(value) if entry in valid]
    if not ids and fallback and fallback > 0:
        ids.append(fallback)
    seen: List[int] = []
    for entry in ids:
        if entry not in seen:
            seen.append(entry)
    return seen[-20:]


def _future_date(value: Any, now: datetime) -> Optional[datetime]:
    parsed = ensure_utc(value)
    return parsed if parsed and parsed > now else None


def normalize_agency_window_state(value: Any) -> Optional[Dict[str, Any]]:
    """读取已持久化的 Agency Window（对应 normalizeAgencyWindowState）。"""
    if not isinstance(value, dict):
        return None
    if value.get("activityLoad") not in _ACTIVITY_LOADS:
        return None
    if value.get("privacy") not in _PRIVACY_VALUES:
        return None
    if value.get("deviceAccess") not in _DEVICE_VALUES:
        return None
    valid_until = ensure_utc(value.get("validUntil"))
    updated_at = ensure_utc(value.get("updatedAt"))
    if not valid_until or not updated_at:
        return None
    next_opportunity = ensure_utc(value.get("nextOpportunityAt"))
    return {
        "activityLoad": value.get("activityLoad"),
        "privacy": value.get("privacy"),
        "deviceAccess": value.get("deviceAccess"),
        "nextOpportunityAt": next_opportunity.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if next_opportunity else None,
        "validUntil": valid_until.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "basis": str(value.get("basis") or "").strip()[:500],
        "sourceEntryIds": _positive_ids(value.get("sourceEntryIds"))[-20:],
        "updatedAt": updated_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }


def normalize_agency_window_draft(
    value: Any,
    now: datetime,
    config: Dict[str, Any],
    valid_source_entry_ids: Iterable[int],
    fallback_source_entry_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    """校验模型返回的 agencyWindow 草稿（对应 normalizeAgencyWindowDraft）。"""
    if not isinstance(value, dict):
        return None
    if value.get("activityLoad") not in _ACTIVITY_LOADS:
        return None
    if value.get("privacy") not in _PRIVACY_VALUES:
        return None
    if value.get("deviceAccess") not in _DEVICE_VALUES:
        return None
    maximum = now + timedelta(minutes=max(5, int(config.get("maxWindowMinutes") or 240)))
    requested_until = ensure_utc(value.get("validUntil"))
    if requested_until and requested_until > now:
        valid_until = min(requested_until, maximum)
    else:
        valid_until = maximum
    requested_opportunity = ensure_utc(value.get("nextOpportunityAt"))
    next_opportunity = (
        min(requested_opportunity, valid_until)
        if requested_opportunity and now < requested_opportunity
        else None
    )
    source_entry_ids = _grounded_ids(value.get("sourceEntryIds"), set(valid_source_entry_ids), fallback_source_entry_id)
    basis = str(value.get("basis") or "").strip()[:500]
    if not basis or not source_entry_ids:
        return None
    iso = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # noqa: E731
    return {
        "activityLoad": value.get("activityLoad"),
        "privacy": value.get("privacy"),
        "deviceAccess": value.get("deviceAccess"),
        "nextOpportunityAt": iso(next_opportunity) if next_opportunity else None,
        "validUntil": iso(valid_until),
        "basis": basis,
        "sourceEntryIds": source_entry_ids,
        "updatedAt": iso(now),
    }


def active_agency_window(value: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """取出仍在有效期内的窗口（对应 activeAgencyWindow）。"""
    moment = now or datetime.now(timezone.utc)
    state = normalize_agency_window_state(value)
    if not state:
        return None
    valid_until = ensure_utc(state.get("validUntil"))
    return state if valid_until and valid_until > moment else None


def normalize_proactive_contact(
    value: Any,
    now: datetime,
    config: Dict[str, Any],
    permitted_participant_ids: Set[str],
    valid_source_entry_ids: Iterable[int],
    fallback_source_entry_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    """校验模型返回的 proactiveContact 草稿（对应 normalizeProactiveContact）。"""
    if not isinstance(value, dict):
        return None
    participant_id = str(value.get("participantId") or "")
    if participant_id not in permitted_participant_ids:
        return None
    if value.get("origin") not in _ORIGINS:
        return None
    if value.get("disclosure") not in _DISCLOSURES:
        return None
    if value.get("outcome") not in _OUTCOMES:
        return None
    motive = str(value.get("motive") or "").strip()[:600]
    source_entry_ids = _grounded_ids(value.get("sourceEntryIds"), set(valid_source_entry_ids), fallback_source_entry_id)
    if not motive or not source_entry_ids:
        return None
    maximum_expiry = now + timedelta(hours=max(1, int(config.get("maxCandidateHours") or 24)))
    requested_expiry = ensure_utc(value.get("expiresAt"))
    expires_at = min(requested_expiry, maximum_expiry) if requested_expiry and requested_expiry > now else maximum_expiry
    requested_not_before = ensure_utc(value.get("notBefore"))
    not_before = (
        requested_not_before
        if requested_not_before and now < requested_not_before < expires_at
        else None
    )
    willingness = None
    raw_willingness = value.get("willingness")
    if not isinstance(raw_willingness, bool) and isinstance(raw_willingness, (int, float)) and math.isfinite(float(raw_willingness)):
        willingness = max(0.0, min(1.0, float(raw_willingness)))
    iso = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # noqa: E731
    return {
        "participantId": participant_id,
        "origin": value.get("origin"),
        "motive": motive,
        "disclosure": value.get("disclosure"),
        "sourceEntryIds": source_entry_ids,
        "willingness": willingness,
        "outcome": value.get("outcome"),
        "notBefore": iso(not_before) if not_before else None,
        "expiresAt": iso(expires_at),
    }


def evaluate_agency_capacity(
    window: Optional[Dict[str, Any]],
    candidate: Dict[str, Any],
    now: datetime,
    config: Dict[str, Any],
    last_character_message_at: Any,
) -> Dict[str, Any]:
    """容量门：设备/隐私/负荷/最小间隔（对应 evaluateAgencyCapacity）。

    承诺型联系（origin=promise）可绕过最小间隔与 occupied 限制；
    设备、隐私、overloaded 三门任何档位都不放宽。
    """
    valid_until = ensure_utc((window or {}).get("validUntil"))
    if not window or not valid_until or valid_until <= now:
        return {"allowed": False, "reason": "agency-window-missing-or-expired", "nextOpportunityAt": None}
    next_opportunity_at = _future_date(window.get("nextOpportunityAt"), now)
    if window.get("deviceAccess") == "unavailable":
        return {"allowed": False, "reason": "device-unavailable", "nextOpportunityAt": next_opportunity_at}
    if window.get("deviceAccess") == "limited":
        return {"allowed": False, "reason": "device-limited", "nextOpportunityAt": next_opportunity_at}
    if window.get("activityLoad") == "overloaded":
        return {"allowed": False, "reason": "schedule-overloaded", "nextOpportunityAt": next_opportunity_at}
    if candidate.get("disclosure") == "personal" and window.get("privacy") != "private":
        return {"allowed": False, "reason": "privacy-insufficient", "nextOpportunityAt": next_opportunity_at}
    last_contact = ensure_utc(last_character_message_at)
    minimum_interval = max(0.0, float(config.get("minimumProactiveIntervalMinutes") or 60)) * MINUTE_SECONDS
    if candidate.get("origin") != "promise" and last_contact:
        elapsed = (now - last_contact).total_seconds()
        if elapsed < minimum_interval:
            return {
                "allowed": False,
                "reason": "minimum-proactive-interval",
                "nextOpportunityAt": last_contact + timedelta(seconds=minimum_interval),
            }
    if (
        window.get("activityLoad") == "occupied"
        and candidate.get("origin") not in ("promise", "practical-update")
    ):
        return {"allowed": False, "reason": "schedule-occupied", "nextOpportunityAt": next_opportunity_at}
    return {"allowed": True, "reason": "capacity-available", "nextOpportunityAt": None}


def proactive_candidate_fingerprint(candidate: Dict[str, Any]) -> str:
    """候选去重指纹：参与者 + 来源 + 证据条目（对应 proactiveCandidateFingerprint）。"""
    ids = sorted(int(item) for item in (candidate.get("sourceEntryIds") or []))
    return "|".join([str(candidate.get("participantId") or ""), str(candidate.get("origin") or ""), ",".join(str(i) for i in ids)])


def proactive_recheck_at(
    candidate: Dict[str, Any], capacity: Dict[str, Any], window: Optional[Dict[str, Any]], now: datetime
) -> datetime:
    """下次重查时间（对应 proactiveRecheckAt，取最早可行时刻并受 expiresAt 钳制）。"""
    requested = ensure_utc(candidate.get("notBefore"))
    capacity_time = ensure_utc(capacity.get("nextOpportunityAt"))
    window_time = ensure_utc((window or {}).get("nextOpportunityAt"))
    fallback = now + timedelta(minutes=30)
    candidates = sorted(
        [moment for moment in (requested, capacity_time, window_time) if moment and moment > now],
        key=lambda moment: moment.timestamp(),
    )
    selected = candidates[0] if candidates else fallback
    expiry = ensure_utc(candidate.get("expiresAt")) or (now + timedelta(hours=1))
    return min(selected, expiry)


def count_recent_proactive_contacts(
    log: Any, participant_id: str, now: datetime, cap: int
) -> int:
    """统计 24 小时内未被作废的主动联系次数（对应 proactiveDailyCap 计数）。"""
    if cap <= 0 or not isinstance(log, list):
        return 0
    window_start = now.timestamp() - 24 * HOUR_SECONDS
    count = 0
    for entry in log[-20:]:
        if not isinstance(entry, dict):
            continue
        if entry.get("participantId") != participant_id:
            continue
        if entry.get("voided"):
            continue
        at = ensure_utc(entry.get("at"))
        if at and at.timestamp() >= window_start:
            count += 1
    return count
