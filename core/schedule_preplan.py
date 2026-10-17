"""Schedule Preplan：近期日程层。

对应上游 `src/schedule-preplan.ts`（逐行移植）。后台保存多日计划，
主叙事只读取未来约半天（12 小时、最多 8 块），且永远标记为
"计划而非已发生事实"；固定日程可成为自动推进锚点。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from .timeutil import calendar_day_key, local_clock_minutes, story_local_time_context

#: 默认配置（对应 DEFAULT_SCHEDULE_PREPLAN_CONFIG）。
DEFAULT_SCHEDULE_PREPLAN_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "horizonDays": 14,
    "reviewAfterLocalHour": 3,
    "anchorAutoAdvance": True,
    "variationLevel": "stable",
    "candidateActivationProbability": 0.25,
    "candidateRevealMinutes": 120,
}

WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
KINDS = ("fixed", "routine", "flexible", "open")
_PRIORITY = {"fixed": 4, "routine": 3, "flexible": 2, "open": 1}

#: 证据条目中命中"改期"语义的触发词（对应 DATE_CHANGE_TRIGGER）。
DATE_CHANGE_TRIGGER = re.compile(
    "取消|改期|改成|推迟|提前到|延到|改约|另约|延期|定在|敲定|约好|约了|确认了?(时间|地点|日子)?"
)

SCHEDULE_PREPLAN_FOLLOWUP_COOLDOWN_SECONDS = 2 * 60 * 60


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _clamp_number(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return max(minimum, min(maximum, number))


def resolve_schedule_preplan_config(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    value = value or {}
    return {
        "enabled": value.get("enabled") is not False,
        "horizonDays": _clamp_int(value.get("horizonDays"), 3, 30, 14),
        "reviewAfterLocalHour": _clamp_int(value.get("reviewAfterLocalHour"), 0, 23, 3),
        "anchorAutoAdvance": value.get("anchorAutoAdvance") is not False,
        "variationLevel": value.get("variationLevel") if value.get("variationLevel") in ("stable", "contextual", "granular") else "stable",
        "candidateActivationProbability": _clamp_number(value.get("candidateActivationProbability"), 0.05, 0.5, 0.25),
        "candidateRevealMinutes": _clamp_int(value.get("candidateRevealMinutes"), 15, 360, 120),
    }


# --------------------------------------------------------------------------- #
# 基础日期/时间工具
# --------------------------------------------------------------------------- #


def date_key(value: Any) -> Optional[str]:
    raw = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.isoformat()


def add_date(value: str, days: int) -> str:
    parsed = date.fromisoformat(value)
    return (parsed + timedelta(days=days)).isoformat()


def date_difference(left: str, right: str) -> int:
    return (date.fromisoformat(right) - date.fromisoformat(left)).days


def time_key(value: Any) -> Optional[str]:
    raw = value.strip() if isinstance(value, str) else ""
    match = re.fullmatch(r"(\d{2}):(\d{2})", raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return raw


def time_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def clock_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _slug(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[^\w\-\u4e00-\u9fff\U00010000-\U0010ffff]", "-", value.strip(), flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:limit]


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\r\n]+", " ", value.strip())[:limit]


def _finite01(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def _evidence_ids(value: Any, valid: Optional[Set[int]]) -> List[int]:
    if not isinstance(value, list):
        return []
    seen: List[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.append(number)
    seen = seen[:30]
    return [item for item in seen if item in valid] if valid else seen


# --------------------------------------------------------------------------- #
# 模型提案归一化
# --------------------------------------------------------------------------- #


def _normalize_block(value: Any, valid_evidence_ids: Optional[Set[int]], allow_tentative: bool = True) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    block_id = _slug(value.get("id"), 80)
    start = time_key(value.get("start"))
    end = time_key(value.get("end"))
    label = _clean_text(value.get("label"), 160)
    kind = value.get("kind") if value.get("kind") in KINDS else None
    if not block_id or not start or not end or start == end or not label or not kind:
        return None
    location = _clean_text(value.get("location"), 120)
    if value.get("tentative") is True and (not allow_tentative or kind in ("fixed", "routine")):
        return None
    tentative = allow_tentative and value.get("tentative") is True and kind in ("flexible", "open")
    block: Dict[str, Any] = {"id": block_id, "start": start, "end": end, "label": label, "kind": kind}
    if location:
        block["location"] = location
    if tentative:
        block["tentative"] = True
    source_ids = _evidence_ids(value.get("sourceEntryIds"), valid_evidence_ids)
    if source_ids:
        block["sourceEntryIds"] = source_ids
    return block


def _normalize_blocks(value: Any, valid_evidence_ids: Optional[Set[int]] = None, allow_tentative: bool = True) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = [
        block
        for block in (_normalize_block(item, valid_evidence_ids, allow_tentative) for item in value)
        if block
    ]
    return result[:20]


def normalize_regimes(value: Any, valid_evidence_ids: Optional[Set[int]] = None, allow_tentative: bool = True, limit: int = 64) -> List[Dict[str, Any]]:
    """校验 regimes 数组（对应 normalizeRegimes）。"""
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("weekly"), dict):
            continue
        regime_id = _slug(item.get("id"), 80)
        label = _clean_text(item.get("label"), 120)
        start = date_key(item.get("from"))
        end = date_key(item.get("to"))
        if not regime_id or not label or not start or (end and end < start):
            continue
        weekly: Dict[str, Any] = {}
        for weekday in WEEKDAYS:
            blocks = _normalize_blocks(item["weekly"].get(weekday), valid_evidence_ids, allow_tentative)
            if blocks:
                weekly[weekday] = blocks
        regime: Dict[str, Any] = {"id": regime_id, "label": label, "from": start, "weekly": weekly}
        if end:
            regime["to"] = end
        source_ids = _evidence_ids(item.get("sourceEntryIds"), valid_evidence_ids)
        if source_ids:
            regime["sourceEntryIds"] = source_ids
        result.append(regime)
    return result[:limit]


def normalize_exceptions(value: Any, valid_evidence_ids: Optional[Set[int]] = None, allow_tentative: bool = True, limit: int = 64) -> List[Dict[str, Any]]:
    """校验 exceptions 数组（对应 normalizeExceptions）。"""
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        day = date_key(item.get("date"))
        mode = "replace" if item.get("mode") == "replace" else ("patch" if item.get("mode") == "patch" else None)
        reason = _clean_text(item.get("reason"), 300)
        if not day or not mode or not reason:
            continue
        remove_ids = []
        if isinstance(item.get("removeBlockIds"), list):
            remove_ids = [sid for sid in (_slug(entry, 80) for entry in item["removeBlockIds"]) if sid][:20]
        exception: Dict[str, Any] = {
            "date": day,
            "mode": mode,
            "reason": reason,
            "removeBlockIds": remove_ids,
            "blocks": _normalize_blocks(item.get("blocks"), valid_evidence_ids, allow_tentative),
        }
        source_ids = _evidence_ids(item.get("sourceEntryIds"), valid_evidence_ids)
        if source_ids:
            exception["sourceEntryIds"] = source_ids
        result.append(exception)
    return result[:limit]


def normalize_days(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict) and date_key(item.get("date")):
            result.append({"date": date_key(item.get("date")), "blocks": _normalize_blocks(item.get("blocks"))})
    return result[:31]


def normalize_schedule_preplan_record(value: Any) -> Optional[Dict[str, Any]]:
    """宽容读取日程记录（对应 normalizeSchedulePreplanRecord）。"""
    if not isinstance(value, dict) or not isinstance(value.get("storyId"), str):
        return None
    valid_from = date_key(value.get("validFrom"))
    valid_through = date_key(value.get("validThrough"))
    if not valid_from or not valid_through:
        return None
    return {
        "storyId": value.get("storyId"),
        "revision": _clamp_int(value.get("revision"), 0, 1000000, 0),
        "timezone": _clean_text(value.get("timezone"), 127) or "UTC",
        "validFrom": valid_from,
        "validThrough": valid_through,
        "lastReviewedLocalDate": date_key(value.get("lastReviewedLocalDate")) or "",
        "lastEvidenceEntryId": _clamp_int(value.get("lastEvidenceEntryId"), 0, 2**53 - 1, 0),
        "reviewReason": _clean_text(value.get("reviewReason"), 500),
        "regimes": normalize_regimes(value.get("regimes")),
        "exceptions": normalize_exceptions(value.get("exceptions")),
        "materializedDays": normalize_days(value.get("materializedDays")),
        "createdAt": value.get("createdAt") or datetime(1970, 1, 1),
        "updatedAt": value.get("updatedAt") or datetime(1970, 1, 1),
    }


# --------------------------------------------------------------------------- #
# 审查触发与提案应用
# --------------------------------------------------------------------------- #


def schedule_preplan_review_due(record: Optional[Dict[str, Any]], now: datetime, timezone: str, config: Dict[str, Any]) -> bool:
    """每日审查是否到期（对应 schedulePreplanReviewDue）。"""
    if not config.get("enabled"):
        return False
    today = calendar_day_key(now, timezone)
    if not record:
        return True
    if record.get("timezone") != timezone:
        return True
    return record.get("lastReviewedLocalDate") != today and local_clock_minutes(now, timezone) >= int(config.get("reviewAfterLocalHour") or 3) * 60


def schedule_preplan_evidence_mentions_date_change(entries: Iterable[Any]) -> List[int]:
    """证据条目中是否出现"改期"语义（对应 schedulePreplanEvidenceMentionsDateChange）。"""
    matched: List[int] = []
    for entry in entries:
        entry_id = getattr(entry, "id", None)
        content = getattr(entry, "content", "") or ""
        if isinstance(entry, dict):
            entry_id = entry.get("id")
            content = entry.get("content") or ""
        if isinstance(entry_id, int) and entry_id > 0 and DATE_CHANGE_TRIGGER.search(str(content)):
            matched.append(entry_id)
    seen: List[int] = []
    for item in matched:
        if item not in seen:
            seen.append(item)
    return seen[:20]


def schedule_preplan_needs_model(
    record: Optional[Dict[str, Any]], evidence: Iterable[Any], today: str, timezone: str, config: Dict[str, Any]
) -> bool:
    """没有新证据且覆盖充足时直接程序刷新、不调用模型（对应 schedulePreplanNeedsModel）。"""
    if not record or record.get("timezone") != timezone:
        return True
    if any((getattr(entry, "id", 0) or 0) > record.get("lastEvidenceEntryId", 0) for entry in evidence):
        return True
    if not record.get("regimes"):
        return False
    coverage_target = add_date(today, max(1, int(config.get("horizonDays") or 14) - 3))
    regimes = record.get("regimes") or []
    if not any(
        regime.get("from") <= coverage_target and (not regime.get("to") or regime.get("to") >= coverage_target)
        for regime in regimes
    ):
        return True
    return str(record.get("validThrough") or "") < coverage_target


def _resolve_overlaps(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """冲突消解：kind 优先级 + 开始时间（对应 resolveOverlaps）。"""
    chosen: List[Dict[str, Any]] = []
    for candidate in sorted(
        blocks, key=lambda block: (-_PRIORITY.get(block.get("kind"), 0), time_minutes(block["start"]))
    ):
        start = time_minutes(candidate["start"])
        end = time_minutes(candidate["end"])
        if end <= start:
            end += 1440
        overlaps = False
        for block in chosen:
            other_start = time_minutes(block["start"])
            other_end = time_minutes(block["end"])
            if other_end <= other_start:
                other_end += 1440
            if start < other_end and end > other_start:
                overlaps = True
                break
        if not overlaps and not any(block.get("id") == candidate.get("id") for block in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda block: time_minutes(block["start"]))


def materialize_schedule_preplan(
    regimes: List[Dict[str, Any]], exceptions: List[Dict[str, Any]], start_date: str, horizon_days: int
) -> List[Dict[str, Any]]:
    """把周规律 + 例外展开为逐日计划（对应 materializeSchedulePreplan）。"""
    days: List[Dict[str, Any]] = []
    for offset in range(-1, max(1, horizon_days)):
        day = add_date(start_date, offset)
        matching = sorted(
            [
                regime
                for regime in regimes
                if regime.get("from") <= day and (not regime.get("to") or regime.get("to") >= day)
            ],
            key=lambda regime: regime.get("from", ""),
            reverse=True,
        )
        weekday = WEEKDAYS[date.fromisoformat(day).isoweekday() % 7]
        blocks: List[Dict[str, Any]] = []
        if matching:
            weekly = matching[0].get("weekly") or {}
            blocks = [dict(block) for block in weekly.get(weekday, [])]
        exception = next((item for item in exceptions if item.get("date") == day), None)
        if exception and exception.get("mode") == "replace":
            blocks = [dict(block) for block in exception.get("blocks", [])]
        elif exception:
            removed = set(exception.get("removeBlockIds") or [])
            blocks = [block for block in blocks if block.get("id") not in removed] + [
                dict(block) for block in exception.get("blocks", [])
            ]
        days.append({"date": day, "blocks": _resolve_overlaps(blocks)[:12]})
    return days


def refresh_schedule_preplan(
    record: Dict[str, Any], today: str, timezone: str, config: Dict[str, Any], now: datetime,
    reason: str = "Daily review found no schedule-changing evidence.",
) -> Dict[str, Any]:
    valid_through = add_date(today, int(config.get("horizonDays") or 14) - 1)
    return {
        **record,
        "timezone": timezone,
        "validFrom": today,
        "validThrough": valid_through,
        "lastReviewedLocalDate": today,
        "reviewReason": reason,
        "materializedDays": materialize_schedule_preplan(
            record.get("regimes") or [], record.get("exceptions") or [], today, int(config.get("horizonDays") or 14)
        ),
        "updatedAt": now,
    }


def _normalize_proposal(value: Any, valid_evidence_ids: Set[int], variation_level: str) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or value.get("outcome") not in ("unchanged", "extend", "patch", "replace"):
        return None
    outcome = value.get("outcome")
    reason = _clean_text(value.get("reason"), 500)
    if not reason:
        return None
    source_entry_ids = [item for item in _evidence_ids(value.get("sourceEntryIds"), None) if item in valid_evidence_ids]
    allow_tentative = variation_level == "granular"
    regimes = normalize_regimes(value.get("regimes"), valid_evidence_ids, allow_tentative, 6)
    exceptions = normalize_exceptions(value.get("exceptions"), valid_evidence_ids, allow_tentative, 30)
    if (
        outcome in ("patch", "replace")
        and valid_evidence_ids
        and not source_entry_ids
        and not any(regime.get("sourceEntryIds") for regime in regimes)
        and not any(exception.get("sourceEntryIds") for exception in exceptions)
    ):
        return None
    return {
        "outcome": outcome,
        "reason": reason,
        "confidence": _finite01(value.get("confidence")),
        "sourceEntryIds": source_entry_ids,
        "regimes": regimes,
        "exceptions": exceptions,
    }


def _merge_by(current: List[Dict[str, Any]], changes: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    merged: Dict[Any, Dict[str, Any]] = {item.get(key): item for item in current}
    for item in changes:
        merged[item.get(key)] = item
    return list(merged.values())


def _retain_regimes(regimes: List[Dict[str, Any]], today: str, horizon_days: int) -> List[Dict[str, Any]]:
    retained: List[Dict[str, Any]] = []
    for offset in range(-1, max(1, horizon_days)):
        day = add_date(today, offset)
        matching = sorted(
            [regime for regime in regimes if regime.get("from") <= day and (not regime.get("to") or regime.get("to") >= day)],
            key=lambda regime: regime.get("from", ""),
            reverse=True,
        )
        if matching and matching[0] not in retained:
            retained.append(matching[0])
    valid_through = add_date(today, horizon_days - 1)
    upcoming = sorted(
        [regime for regime in regimes if regime.get("from") > valid_through],
        key=lambda regime: regime.get("from", ""),
    )[:6]
    for regime in upcoming:
        if regime not in retained:
            retained.append(regime)
    return [regime for regime in regimes if regime in retained]


def _retain_exceptions(exceptions: List[Dict[str, Any]], today: str, valid_through: str) -> List[Dict[str, Any]]:
    yesterday = add_date(today, -1)
    upcoming = sorted(
        [item for item in exceptions if item.get("date") >= yesterday],
        key=lambda item: item.get("date", ""),
    )
    within = [item for item in upcoming if item.get("date") <= valid_through]
    beyond = [item for item in upcoming if item.get("date") > valid_through][:6]
    return within + beyond


def apply_schedule_preplan_proposal(
    current: Optional[Dict[str, Any]],
    proposal_value: Any,
    evidence: Iterable[Any],
    today: str,
    timezone: str,
    config: Dict[str, Any],
    now: datetime,
    variation_level: str = "stable",
) -> Optional[Dict[str, Any]]:
    """应用审查提案（对应 applySchedulePreplanProposal）。"""
    evidence_ids = {getattr(entry, "id", 0) for entry in evidence}
    evidence_ids = {entry_id for entry_id in evidence_ids if isinstance(entry_id, int) and entry_id > 0}
    proposal = _normalize_proposal(proposal_value, evidence_ids, variation_level)
    if not proposal:
        return (
            refresh_schedule_preplan(current, today, timezone, config, now, "Invalid proposal ignored; existing Schedule Preplan retained.")
            if current
            else None
        )
    max_evidence = max([entry_id for entry_id in evidence_ids] + [0])
    if proposal["outcome"] == "unchanged" and current:
        refreshed = refresh_schedule_preplan(current, today, timezone, config, now, proposal["reason"])
        refreshed["lastEvidenceEntryId"] = max(current.get("lastEvidenceEntryId", 0), max_evidence)
        return refreshed
    regimes = list(current.get("regimes") or []) if current else []
    exceptions = list(current.get("exceptions") or []) if current else []
    if proposal["outcome"] == "replace" or not current:
        regimes = proposal["regimes"] or []
        exceptions = proposal["exceptions"] or []
    else:
        regimes = _merge_by(regimes, proposal["regimes"] or [], "id")
        exceptions = _merge_by(exceptions, proposal["exceptions"] or [], "date")
    if not regimes and not exceptions:
        if not current:
            return {
                "storyId": "",
                "revision": 1,
                "timezone": timezone,
                "validFrom": today,
                "validThrough": add_date(today, int(config.get("horizonDays") or 14) - 1),
                "lastReviewedLocalDate": today,
                "lastEvidenceEntryId": max_evidence,
                "reviewReason": proposal["reason"],
                "regimes": [],
                "exceptions": [],
                "materializedDays": [],
                "createdAt": now,
                "updatedAt": now,
            }
        return refresh_schedule_preplan(current, today, timezone, config, now, "Empty proposal ignored; existing Schedule Preplan retained.")
    valid_through = add_date(today, int(config.get("horizonDays") or 14) - 1)
    retained_regimes = _retain_regimes(regimes, today, int(config.get("horizonDays") or 14))
    retained_exceptions = _retain_exceptions(exceptions, today, valid_through)
    return {
        "storyId": current.get("storyId") if current else "",
        "revision": (current.get("revision", 0) if current else 0) + 1,
        "timezone": timezone,
        "validFrom": today,
        "validThrough": valid_through,
        "lastReviewedLocalDate": today,
        "lastEvidenceEntryId": max(current.get("lastEvidenceEntryId", 0) if current else 0, max_evidence),
        "reviewReason": proposal["reason"],
        "regimes": retained_regimes,
        "exceptions": retained_exceptions,
        "materializedDays": materialize_schedule_preplan(
            retained_regimes, retained_exceptions, today, int(config.get("horizonDays") or 14)
        ),
        "createdAt": (current.get("createdAt") if current else None) or now,
        "updatedAt": now,
    }


# --------------------------------------------------------------------------- #
# 半日投影与推进锚点
# --------------------------------------------------------------------------- #


def is_tentative_block_active(story_id: str, day: str, block_id: str, probability: float) -> bool:
    """候选块按 故事+日期+块ID 的确定性哈希激活（对应 isTentativeBlockActive）。"""
    digest = hashlib.sha256(f"{story_id}|{day}|{block_id}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big") / 4294967296
    return value < probability


def schedule_preplan_window(
    record: Optional[Dict[str, Any]],
    now: datetime,
    timezone: str,
    hours: int = 12,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """未来约半天的计划投影（对应 schedulePreplanWindow，最多 8 块）。"""
    config = config or DEFAULT_SCHEDULE_PREPLAN_CONFIG
    if not record or record.get("timezone") != timezone:
        return None
    local = story_local_time_context(now, timezone)
    today = str(local["date"])
    start_minute = int(local["hour"]) * 60 + int(str(local["time"])[3:5])
    end_minute = start_minute + max(1, hours) * 60
    blocks: List[Dict[str, Any]] = []
    for day_entry in record.get("materializedDays") or []:
        day = day_entry.get("date")
        if not day:
            continue
        day_offset = date_difference(today, day)
        if day_offset < -1 or day_offset > 1:
            continue
        for block in day_entry.get("blocks") or []:
            start = day_offset * 1440 + time_minutes(block["start"])
            end = day_offset * 1440 + time_minutes(block["end"])
            if end <= start:
                end += 1440
            if end <= start_minute or start >= end_minute:
                continue
            if block.get("tentative"):
                if not is_tentative_block_active(
                    record.get("storyId", ""), day, block.get("id", ""),
                    float(config.get("candidateActivationProbability") or 0.25),
                ):
                    continue
                minutes_until = start - start_minute
                if minutes_until > int(config.get("candidateRevealMinutes") or 120):
                    blocks.append({**block, "label": "可能的个人安排", "location": None, "date": day, "tentative": True})
                    continue
            blocks.append({**block, "date": day})
    to_total = end_minute
    to_date = add_date(today, to_total // 1440)
    to_clock = clock_text(to_total % 1440)
    return {
        "name": "Schedule Preplan",
        "timezone": timezone,
        "from": f"{today} {clock_text(start_minute)}",
        "to": f"{to_date} {to_clock}",
        "plannedNotObserved": True,
        "revision": record.get("revision", 0),
        "blocks": blocks[:8],
    }


def next_schedule_preplan_transition(
    record: Optional[Dict[str, Any]], now: datetime, timezone: str, max_hours: int = 12
) -> Optional[datetime]:
    """固定日程的下一个开始/结束时刻（对应 nextSchedulePreplanTransition，推进锚点）。"""
    window = schedule_preplan_window(record, now, timezone, max_hours)
    if not window:
        return None
    local = story_local_time_context(now, timezone)
    current = int(local["hour"]) * 60 + int(str(local["time"])[3:5])
    candidates: List[int] = []
    for block in [item for item in window["blocks"] if item.get("kind") == "fixed"]:
        offset = date_difference(str(local["date"]), block["date"]) * 1440
        start = offset + time_minutes(block["start"])
        end = offset + time_minutes(block["end"])
        if end <= start:
            end += 1440
        if start > current:
            candidates.append(start)
        if end > current:
            candidates.append(end)
    if not candidates:
        return None
    next_minute = min(candidates)
    return now + timedelta(minutes=next_minute - current)
