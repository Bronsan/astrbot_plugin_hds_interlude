"""剧本引擎：正文解析、可见回复归一化、提交构建与投递账本。

对应上游 `src/script/` 目录下的：
`authored-actions.ts`（say 标记解析）、`commit-builder.ts`（提交构建）、
`delivery-ledger.ts`（投递账本）、`intent-lifecycle.ts`（意图生命周期）、
`delivery-reality.ts`（投递现实投影）与 `validator` 的可见回复校验部分。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .types import DEFAULT_MESSAGE_SEPARATOR, ensure_utc, to_iso
from .timeutil import format_story_clock


def clip(value: Any, limit: int) -> str:
    """按字符数截断（对应上游 clip 工具）。"""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text[:limit] if limit and limit > 0 else text


def is_record(value: Any) -> bool:
    return isinstance(value, dict)


# --------------------------------------------------------------------------- #
# say 标记（authored-actions.ts）
# --------------------------------------------------------------------------- #

_SAY_PATTERN = re.compile(r'<say id="([\w-]{1,64})">([\s\S]*?)</say>')


def read_authored_actions(script: str) -> Dict[str, Any]:
    """解析正文中的 `<say id="...">...</say>` 标记。

    返回 {prose, actions}：prose 是去标记后的连续正文；actions 里每个
    动作记录它在 prose 中的 [start, end) 与原文片段。重复 id 的动作会
    被丢弃（对应上游 filter：同 id 只允许出现一次）。
    """
    actions: List[Dict[str, Any]] = []
    prose = ""
    cursor = 0
    for match in _SAY_PATTERN.finditer(script or ""):
        prose += script[cursor:match.start()]
        start = len(prose)
        prose += match.group(2)
        actions.append({
            "id": match.group(1),
            "start": start,
            "end": len(prose),
            "content": match.group(2),
        })
        cursor = match.end()
    prose += script[cursor:]
    counts: Dict[str, int] = {}
    for action in actions:
        counts[action["id"]] = counts.get(action["id"], 0) + 1
    unique = [action for action in actions if counts[action["id"]] == 1]
    return {"prose": prose, "actions": unique}


def reconcile_transport_references(decision: Dict[str, Any], separator: str = DEFAULT_MESSAGE_SEPARATOR) -> Dict[str, Any]:
    """把 say 标记与结构化 transport 字段互相对齐（对应 reconcileTransportReferences 的核心行为）。

    规则：
    - `interaction.reply.actionId` 指向的动作被解析为可见内容；
    - 指向不存在的动作时该回复降级为 none（不凭空发送）；
    - `mode=none` 但带有合法 actionId 时，抢救为 immediate；
    - 无 actionId 但正文只有一个动作、且回复为 immediate 时，
      把该动作补回为回复内容。
    """
    if not isinstance(decision.get("script"), str):
        return decision
    result = dict(decision)
    parsed = read_authored_actions(result["script"])
    leading = len(parsed["prose"]) - len(parsed["prose"].lstrip())
    prose = parsed["prose"].strip()
    actions: List[Dict[str, Any]] = []
    for action in parsed["actions"]:
        action = dict(action)
        action["start"] -= leading
        action["end"] -= leading
        if 0 <= action["start"] < action["end"] <= len(prose) and prose[action["start"]:action["end"]] == action["content"]:
            actions.append(action)
            continue
        start = max(0, action["start"])
        end = min(max(action["end"], start), len(prose))
        slice_text = prose[start:end]
        if slice_text and slice_text.strip() == action["content"].strip():
            action.update({"start": start, "end": end, "content": slice_text})
            actions.append(action)
    result["script"] = prose
    result["authoredActions"] = actions

    interaction = result.get("interaction")
    if isinstance(interaction, dict) and isinstance(interaction.get("reply"), dict):
        reply = dict(interaction["reply"])
        action_id = reply.get("actionId")
        if action_id and reply.get("mode") == "immediate":
            matched = next(
                (
                    action
                    for action in actions
                    if action["id"] == action_id
                    and prose[action["start"]:action["end"]] == action["content"]
                    and action["content"].strip()
                ),
                None,
            )
            if matched:
                reply["content"] = matched["content"]
            else:
                reply["mode"] = "none"
                reply.pop("content", None)
        elif (
            reply.get("mode") == "none"
            and isinstance(action_id, str)
            and action_id.strip()
        ):
            matched = next(
                (
                    action
                    for action in actions
                    if action["id"] == action_id
                    and prose[action["start"]:action["end"]] == action["content"]
                    and action["content"].strip()
                ),
                None,
            )
            if matched:
                reply["mode"] = "immediate"
                reply["content"] = matched["content"]
        if len(actions) == 1 and reply.get("mode") == "immediate" and not reply.get("content"):
            reply["content"] = actions[0]["content"]
        reply.pop("actionId", None)
        result["interaction"] = {**interaction, "reply": reply}
    return result


# --------------------------------------------------------------------------- #
# 可见回复归一化与分段（service.ts 的 sanitize/split 部分）
# --------------------------------------------------------------------------- #

_SEP_LOOSE = re.compile(r"[<＜]\s*sep\s*/?\s*[>＞]", re.IGNORECASE)
_SMART_SINGLE_QUOTES = re.compile(r"‘([^’]+)’")
_MEDIA_TAGS = re.compile(r"</?(?:file|img|image|audio|record|video|flash|mface)\b[^>]*/?>", re.IGNORECASE)
_CQ_MEDIA = re.compile(r"\[CQ:(?:file|image|record|video|flash|mface),[^\]]*\]", re.IGNORECASE)
_PLACEHOLDER_STICKER = re.compile(r"[\[【](?:表情包?|图片|动图|GIF)[\]】]", re.IGNORECASE)
_PLACEHOLDER_FACE = re.compile(r"[\[【](?:流汗|微笑|笑哭|尴尬|爱心|惊讶|流泪|委屈)[\]】]")


def sanitize_and_clamp_visible_content(
    value: Any, max_characters: int, separator: str = DEFAULT_MESSAGE_SEPARATOR
) -> str:
    """清洗并截断可见消息（对应 sanitizeAndClampVisibleContent）。"""
    text = "" if value is None else str(value)
    text = _SEP_LOOSE.sub((separator or DEFAULT_MESSAGE_SEPARATOR).strip() or DEFAULT_MESSAGE_SEPARATOR, text)
    text = _SMART_SINGLE_QUOTES.sub(lambda match: f"“{match.group(1)}”", text)
    text = _MEDIA_TAGS.sub("", text)
    text = _CQ_MEDIA.sub("", text)
    text = _PLACEHOLDER_STICKER.sub("", text)
    text = _PLACEHOLDER_FACE.sub("", text)
    return text.strip()[: max(1, int(max_characters))]


def split_visible_reply_bubbles(
    content: str, separator: str = DEFAULT_MESSAGE_SEPARATOR, split_enabled: bool = True, newline_as_separator: bool = False
) -> List[str]:
    """按 `<sep/>` 拆分气泡（对应 splitVisibleReplyBubbles）。"""
    if not split_enabled:
        return [content]
    canonical = (separator or "").strip() or DEFAULT_MESSAGE_SEPARATOR
    if not canonical:
        return [content]
    normalized = content
    if newline_as_separator and canonical not in normalized and re.search(r"\r?\n", normalized):
        normalized = re.sub(r"\r?\n+", canonical, normalized)
    if canonical not in normalized:
        return [content]
    parts = [part.strip() for part in normalized.split(canonical)]
    parts = [part for part in parts if part]
    return parts if parts else [content]


def normalize_script_field(raw: Any) -> str:
    """宽容读取模型返回的 script 字段（对应 normalizeScriptField）。"""
    if isinstance(raw, str):
        return raw

    def prose_of(entry: Any) -> str:
        if isinstance(entry, dict):
            for key in ("text", "content"):
                if isinstance(entry.get(key), str):
                    return entry[key].strip()
        return ""

    def join_entries(entries: Sequence[Any]) -> str:
        return "\n".join(filter(None, (prose_of(entry) for entry in entries)))

    if isinstance(raw, list):
        return join_entries(raw)
    if not isinstance(raw, dict):
        return ""
    for key in ("content", "text", "prose", "narration", "body", "script"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            return raw[key]
    if isinstance(raw.get("entries"), list):
        return join_entries(raw["entries"])
    return ""


def normalize_interaction(value: Any, now: datetime, runtime: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """归一化 interaction（对应 normalizeInteraction，含延迟范围校验）。"""
    if not isinstance(value, dict) or not isinstance(value.get("reply"), dict):
        return None
    reply = value["reply"]
    mode = reply.get("mode") if isinstance(reply.get("mode"), str) else None
    if mode not in ("none", "immediate", "delayed"):
        has_content = isinstance(reply.get("content"), str) and bool(reply["content"].strip())
        if has_content and mode in (None, "text", "send", "reply", "message"):
            mode = "immediate"
        elif mode is None and not has_content:
            mode = "none"
        else:
            return None
    separator = (runtime.get("messageSeparator") or "").strip() or DEFAULT_MESSAGE_SEPARATOR
    content = None
    if isinstance(reply.get("content"), str):
        content = sanitize_and_clamp_visible_content(
            reply["content"], int(runtime.get("maxMessageCharacters") or 1500), separator
        )
    if content and runtime.get("splitReplyMessages") is not False and separator and separator not in content:
        content = re.sub(r"\s*\r?\n\s*\r?\n+", separator, content)
    send_at = ensure_utc(reply.get("sendAt"))
    seen = value.get("seen") if isinstance(value.get("seen"), bool) else True
    if mode == "none" or not content:
        return {"seen": seen, "reply": {"mode": "none"}}
    if mode == "immediate":
        return {"seen": seen, "reply": {"mode": "immediate", "content": content}}
    delay_ms = (send_at.timestamp() - now.timestamp()) * 1000 if send_at else None
    min_ms = float(runtime.get("minimumDelayedReplySeconds") if runtime.get("minimumDelayedReplySeconds") is not None else 10) * 1000
    max_ms = float(runtime.get("maximumDelayedReplyMinutes") if runtime.get("maximumDelayedReplyMinutes") is not None else 1440) * 60 * 1000
    if not send_at or delay_ms is None or delay_ms < min_ms or delay_ms > max_ms:
        return {"seen": seen, "reply": {"mode": "none"}}
    return {"seen": seen, "reply": {"mode": "delayed", "content": content, "sendAt": to_iso(send_at)}}


def has_structured_interaction(value: Any) -> bool:
    """判断结构化 interaction 是否合法（对应 hasStructuredInteraction）。"""
    if not isinstance(value, dict) or not isinstance(value.get("seen"), bool) or not isinstance(value.get("reply"), dict):
        return False
    mode = value["reply"].get("mode")
    if mode not in ("none", "immediate", "delayed"):
        return False
    if mode == "none":
        return True
    content = value["reply"].get("content")
    return isinstance(content, str) and bool(content.strip())


def has_structured_group_reply_field(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("mode") not in ("none", "immediate"):
        return False
    if value.get("mode") == "none":
        return True
    return isinstance(value.get("content"), str) and bool(value["content"].strip())


def visible_reply_mode(decision: Dict[str, Any], phase: str, group_context: Optional[Dict[str, Any]]) -> str:
    """给日志用的"回复模式"标签（对应 visibleReplyMode）。"""
    cross = decision.get("crossConversationActions") or []
    if phase == "advance":
        if any(action.get("mode") == "immediate" for action in cross):
            return "主动联系"
        if any(action.get("mode") == "delayed" for action in cross):
            return "计划联系"
        return "无可见投递"
    if phase in ("conversation-follow-up", "intent-due"):
        if has_structured_interaction(decision.get("interaction")):
            return decision["interaction"]["reply"]["mode"]
        if any(action.get("mode") == "immediate" for action in cross):
            return "主动联系"
        return "无可见投递"
    if not group_context:
        return decision["interaction"]["reply"]["mode"] if has_structured_interaction(decision.get("interaction")) else "未提供或无效"
    if has_structured_group_reply_field(decision.get("groupReply")):
        return f"group:{decision['groupReply']['mode']}"
    if has_structured_interaction(decision.get("interaction")):
        return f"group-fallback:{decision['interaction']['reply']['mode']}"
    return "未提供或无效"


def requires_visible_reply_recovery(phase: str, group_context: Optional[Dict[str, Any]], decision: Dict[str, Any]) -> bool:
    """用户回合缺少可见回复结构时要求一次恢复重写（对应 requiresVisibleReplyRecovery）。"""
    if phase != "user-message":
        return False
    if group_context:
        return not (has_structured_group_reply_field(decision.get("groupReply")) or has_structured_interaction(decision.get("interaction")))
    if decision.get("localMedia") or decision.get("nativeFace") or decision.get("messageReactions"):
        return False
    return not has_structured_interaction(decision.get("interaction"))


# --------------------------------------------------------------------------- #
# 决策归一化（service.ts 的 normalizeDecision / 相关校验）
# --------------------------------------------------------------------------- #


def normalize_intent_updates(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("id"), int) or item["id"] <= 0:
            continue
        if item.get("status") not in ("completed", "cancelled"):
            continue
        entry = {"id": int(item["id"]), "status": item["status"]}
        if isinstance(item.get("resolution"), str) and item["resolution"].strip():
            entry["resolution"] = clip(item["resolution"], 1000)
        result.append(entry)
    return result[:8]


def _to_dt(value: Any) -> Optional[datetime]:
    return ensure_utc(value)


def is_active_consequence_draft(intent: Dict[str, Any]) -> bool:
    return (
        intent.get("type") == "active-consequence"
        and isinstance(intent.get("payload"), dict)
        and intent["payload"].get("lifecycle") == "active"
    )


def valid_memory(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("category"), str)
        and isinstance(value.get("content"), str)
        and bool(value["content"].strip())
    )


def valid_intent(value: Any, from_at: datetime, now: datetime, memory_config: Dict[str, Any]) -> bool:
    """意图草案校验（对应 validIntent，含剧情余波的独立证据要求）。"""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not isinstance(value.get("summary"), str):
        return False
    not_before = _to_dt(value.get("notBefore"))
    if not not_before:
        return False
    if not is_active_consequence_draft(value):
        return not_before > now
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
    effect = payload.get("effect").strip() if isinstance(payload.get("effect"), str) else ""
    strength = payload.get("strength")
    max_lifetime_days = max(1, int(memory_config.get("activeConsequenceMaxDays") or 7))
    expires_at = _to_dt(payload.get("expiresAt"))
    strength_ok = strength is None or (
        isinstance(strength, (int, float)) and not isinstance(strength, bool) and 0 <= float(strength) <= 1
    )
    return bool(
        memory_config.get("activeConsequencesEnabled", True)
        and effect
        and strength_ok
        and not_before <= now
        and not_before >= from_at
        and expires_at
        and expires_at > now
        and (expires_at - now).total_seconds() <= max_lifetime_days * 86400
    )


def normalize_conversation_action(
    value: Any,
    runtime: Dict[str, Any],
    permitted_participant_ids: Iterable[str],
    permitted_group_ids: Iterable[str],
    current_participant_id: str,
    now: datetime,
    proactive: bool = False,
) -> Optional[Dict[str, Any]]:
    """跨账号动作校验（对应 normalizeConversationAction）。"""
    if not isinstance(value, dict):
        return None
    participant_id = value.get("participantId")
    if not isinstance(participant_id, str) or not participant_id or participant_id == current_participant_id:
        return None
    group_target = participant_id.startswith("group:")
    permitted_participants = set(permitted_participant_ids)
    permitted_groups = {str(item).split(":")[-1] for item in permitted_group_ids}
    target_id = participant_id[len("group:"):] if group_target else participant_id
    if group_target:
        if target_id not in permitted_groups:
            return None
    elif target_id not in permitted_participants:
        return None
    if value.get("mode") not in ("immediate", "delayed"):
        return None
    if group_target and value.get("mode") != "immediate":
        return None
    content = (
        sanitize_and_clamp_visible_content(value.get("content"), int(runtime.get("maxMessageCharacters") or 1500), runtime.get("messageSeparator") or DEFAULT_MESSAGE_SEPARATOR)
        if isinstance(value.get("content"), str)
        else ""
    )
    if not content:
        return None
    willingness = None
    if isinstance(value.get("willingness"), (int, float)) and not isinstance(value.get("willingness"), bool):
        willingness = max(0.0, min(1.0, float(value["willingness"])))
    if proactive and (willingness is None or willingness < float(runtime.get("proactiveWillingnessThreshold") or 0.65)):
        return None
    result: Dict[str, Any] = {
        "participantId": f"group:{target_id}" if group_target else target_id,
        "mode": value["mode"],
        "content": content,
    }
    if isinstance(value.get("endpointId"), str) and value["endpointId"].strip():
        result["endpointId"] = value["endpointId"].strip()
    if willingness is not None:
        result["willingness"] = willingness
    if isinstance(value.get("reason"), str):
        result["reason"] = clip(value["reason"], 300)
    if value["mode"] == "delayed":
        send_at = _to_dt(value.get("sendAt"))
        delay_ms = (send_at.timestamp() - now.timestamp()) * 1000 if send_at else None
        min_ms = float(runtime.get("minimumDelayedReplySeconds") if runtime.get("minimumDelayedReplySeconds") is not None else 10) * 1000
        max_ms = float(runtime.get("maximumDelayedReplyMinutes") if runtime.get("maximumDelayedReplyMinutes") is not None else 1440) * 60 * 1000
        if not send_at or delay_ms is None or delay_ms < min_ms or delay_ms > max_ms:
            return None
        result["sendAt"] = to_iso(send_at)
    return result


def dropped_proactive_action_info(
    value: Any, runtime: Dict[str, Any], permitted_participant_ids: Iterable[str], current_participant_id: str
) -> Optional[Dict[str, Any]]:
    """解析层被意愿门丢弃的主动行动（对应 droppedProactiveActionInfo，留痕补偿）。"""
    if not isinstance(value, dict):
        return None
    participant_id = value.get("participantId")
    if not isinstance(participant_id, str) or not participant_id:
        return None
    if participant_id == current_participant_id or participant_id.startswith("group:"):
        return None
    if participant_id not in set(permitted_participant_ids):
        return None
    if not isinstance(value.get("content"), str) or not value["content"].strip():
        return None
    content = sanitize_and_clamp_visible_content(
        value["content"], int(runtime.get("maxMessageCharacters") or 1500), runtime.get("messageSeparator") or DEFAULT_MESSAGE_SEPARATOR
    )
    if not content:
        return None
    mode = "delayed" if value.get("mode") == "delayed" else "immediate"
    threshold = float(runtime.get("proactiveWillingnessThreshold") or 0.65)
    willingness = None
    if isinstance(value.get("willingness"), (int, float)) and not isinstance(value.get("willingness"), bool):
        willingness = max(0.0, min(1.0, float(value["willingness"])))
    if willingness is None:
        return {"participantId": participant_id, "mode": mode, "content": content, "reason": "proactive-willingness-missing"}
    if willingness < threshold:
        return {"participantId": participant_id, "mode": mode, "content": content, "reason": "proactive-willingness-below-threshold"}
    if mode == "delayed":
        return {"participantId": participant_id, "mode": mode, "content": content, "reason": "delayed-send-at-out-of-range"}
    return None


def pick_participant_state_patch(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    patch: Dict[str, Any] = {}
    if isinstance(value.get("openThreads"), list) and all(isinstance(item, str) for item in value["openThreads"]):
        patch["openThreads"] = [clip(item, 500) for item in value["openThreads"]][:50]
    if isinstance(value.get("relationshipNotes"), list) and all(isinstance(item, str) for item in value["relationshipNotes"]):
        patch["relationshipNotes"] = [clip(item, 500) for item in value["relationshipNotes"]][:50]
    return patch or None


def normalize_browser_intents(value: Any) -> List[Dict[str, Any]]:
    """归一网页观察意图（只允许 search/visit，最多 1 条）。"""
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        query = item.get("query") if isinstance(item.get("query"), str) else ""
        url = item.get("url") if isinstance(item.get("url"), str) else ""
        mode = item.get("mode") if item.get("mode") in ("search", "visit") else ("search" if query.strip() else "visit")
        if mode == "search" and not query.strip():
            continue
        if mode == "visit" and not url.strip():
            continue
        result.append({
            "mode": mode,
            "timing": "immediate" if item.get("timing") == "immediate" else "deferred",
            "purpose": clip(item.get("purpose"), 300) if isinstance(item.get("purpose"), str) else "",
            "query": clip(query, 300),
            "url": clip(url, 2000),
        })
    return result[:1]


def normalize_decision(
    raw: Dict[str, Any],
    from_at: datetime,
    now: datetime,
    permit_messages: bool,
    runtime: Dict[str, Any],
    shared: Dict[str, Any],
    current_participant_id: str,
    permitted_participant_ids: Iterable[str],
    permitted_group_ids: Iterable[str],
    phase: str = "advance",
    memory_config: Optional[Dict[str, Any]] = None,
    refresh_continuity: bool = False,
) -> Dict[str, Any]:
    """把模型原始 JSON 归一为可信的决策对象（对应 normalizeDecision）。

    关键边界：脚本/消息截断、跨账号目标与意愿校验、意图与记忆的证据
    校验、延迟时间范围校验——模型说的一切都要在这里过一遍。
    """
    memory_config = memory_config or {}
    permitted_participants = set(permitted_participant_ids)
    raw = reconcile_transport_references(raw, runtime.get("messageSeparator") or DEFAULT_MESSAGE_SEPARATOR)
    script = normalize_script_field(raw.get("script")).strip()[: int(runtime.get("maxScriptCharacters") or 1400)]
    interaction = None if phase == "advance" else normalize_interaction(raw.get("interaction"), now, runtime)
    memories: List[Dict[str, Any]] = []
    if isinstance(raw.get("memories"), list):
        for memory in raw["memories"]:
            if not valid_memory(memory):
                continue
            candidate = memory.get("participantId")
            participant_id = candidate if isinstance(candidate, str) and candidate.strip() in permitted_participants else (
                current_participant_id if current_participant_id in permitted_participants else ""
            )
            memories.append({**memory, "participantId": participant_id})
    intents: List[Dict[str, Any]] = []
    if isinstance(raw.get("intents"), list):
        for intent in raw["intents"]:
            if not isinstance(intent, dict) or intent.get("type") == "follow-up-commitment":
                continue
            if not valid_intent(intent, from_at, now, memory_config):
                continue
            candidate = intent.get("participantId")
            participant_id = candidate if isinstance(candidate, str) and candidate.strip() in permitted_participants else (
                current_participant_id if current_participant_id in permitted_participants else ""
            )
            intents.append({**intent, "participantId": participant_id})
    intents = intents[:8]
    intent_updates = normalize_intent_updates(raw.get("intentUpdates"))
    proactive_gated = phase == "advance" and not isinstance(raw.get("proactiveContact"), dict)
    dropped: List[Dict[str, Any]] = []
    cross_actions: List[Dict[str, Any]] = []
    if permit_messages and shared.get("allowCrossConversationMessages") and isinstance(raw.get("crossConversationActions"), list):
        for action in raw["crossConversationActions"]:
            normalized = normalize_conversation_action(
                action, runtime, permitted_participants, permitted_group_ids, current_participant_id, now, proactive_gated
            )
            if not normalized:
                if proactive_gated:
                    info = dropped_proactive_action_info(action, runtime, permitted_participants, current_participant_id)
                    if info:
                        dropped.append(info)
                continue
            if (
                normalized["participantId"].startswith("group:")
                and isinstance(interaction, dict)
                and interaction.get("reply", {}).get("content")
                and is_group_echo_of_private_reply(normalized["content"], interaction["reply"]["content"])
            ):
                dropped.append({
                    "participantId": normalized["participantId"],
                    "mode": "immediate",
                    "content": normalized["content"],
                    "reason": "group-echo-of-private-reply",
                })
                continue
            cross_actions.append(normalized)
    max_cross = max(0, int(shared.get("maxCrossConversationActions") or 0))
    cross_actions = cross_actions[:max_cross]
    state_patch = pick_participant_state_patch(raw.get("statePatch")) if isinstance(raw.get("statePatch"), dict) else None
    result: Dict[str, Any] = {
        "script": script,
        "authoredActions": raw.get("authoredActions"),
        "alter": raw.get("alter"),
        "agencyWindow": raw.get("agencyWindow") if isinstance(raw.get("agencyWindow"), dict) else None,
        "proactiveContact": raw.get("proactiveContact") if isinstance(raw.get("proactiveContact"), dict) else None,
        "interaction": interaction,
        "automaticDeliverySummary": raw.get("automaticDeliverySummary") if phase in ("advance", "conversation-follow-up") else None,
        "followUpCommitment": raw.get("followUpCommitment") if phase == "user-message" else None,
        "followUpResolutions": raw.get("followUpResolutions") if phase in ("user-message", "intent-due") else [],
        "memories": memories,
        "intents": intents,
        "intentUpdates": intent_updates,
        "statePatch": state_patch,
        "crossConversationActions": cross_actions,
        "groupReply": None,
        "localMedia": raw.get("localMedia") if isinstance(raw.get("localMedia"), dict) else None,
        "nativeFace": raw.get("nativeFace") if isinstance(raw.get("nativeFace"), dict) else None,
        "messageReactions": raw.get("messageReactions") if isinstance(raw.get("messageReactions"), list) else None,
        "browserIntents": normalize_browser_intents(raw.get("browserIntents")),
        "droppedProactiveActions": dropped,
    }
    return result


def normalize_group_reply(raw: Any, interaction: Optional[Dict[str, Any]], max_characters: int, separator: str) -> Optional[str]:
    """群聊可见回复（groupReply 优先，其次从 interaction 提升）。"""
    if isinstance(raw, dict) and raw.get("mode") == "immediate" and isinstance(raw.get("content"), str):
        content = sanitize_and_clamp_visible_content(raw["content"], max_characters, separator)
        if content:
            return content
    if not isinstance(raw, dict) or raw.get("mode") != "none":
        if isinstance(interaction, dict):
            reply = interaction.get("reply") or {}
            if reply.get("mode") == "immediate" and isinstance(reply.get("content"), str):
                content = sanitize_and_clamp_visible_content(reply["content"], max_characters, separator)
                if content:
                    return content
    return None


def is_group_echo_of_private_reply(action_content: str, reply_content: str) -> bool:
    """群消息与私聊回复近似判重（对应 isGroupEchoOfPrivateReply）。"""
    if not reply_content.strip() or not action_content.strip():
        return False

    def normalize(text: str) -> str:
        return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()

    a, b = normalize(action_content), normalize(reply_content)
    if not a or not b:
        return False
    min_echo = 6
    if a == b:
        return True
    if len(b) >= min_echo and b in a:
        return True
    if len(a) >= min_echo and a in b:
        return True
    grams_a = {a[i:i + 2] for i in range(len(a) - 1)}
    grams_b = {b[i:i + 2] for i in range(len(b) - 1)}
    if not grams_a or not grams_b:
        return False
    shared = len(grams_a & grams_b)
    return shared / (len(grams_a) + len(grams_b) - shared) > 0.55


# --------------------------------------------------------------------------- #
# 提交构建（commit-builder.ts）
# --------------------------------------------------------------------------- #


def _binding_for_bubbles(prose: str, bubbles: Sequence[str]) -> Dict[str, Any]:
    spans: List[Dict[str, Any]] = []
    cursor = 0
    for bubble_index, raw_bubble in enumerate(bubbles):
        bubble = raw_bubble.strip()
        if not bubble:
            return {"status": "unbound"}
        start = prose.find(bubble, cursor)
        if start < 0 or prose.find(bubble, start + 1) >= 0:
            return {"status": "unbound"}
        end = start + len(bubble)
        spans.append({"bubbleIndex": bubble_index, "start": start, "end": end})
        cursor = end
    return {"status": "bound", "spans": spans}


def _bind_immediate_message_action(prose: str, bubbles: Sequence[str], actions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    matching = [
        action
        for action in actions or []
        if prose[action["start"]:action["end"]] == action["content"]
        and all(bubble.strip() in action["content"] for bubble in bubbles)
    ]
    if len(matching) == 1:
        action = matching[0]
        binding = _binding_for_bubbles(action["content"], bubbles)
        if binding["status"] == "bound":
            return {
                **binding,
                "spans": [
                    {**span, "start": span["start"] + action["start"], "end": span["end"] + action["start"]}
                    for span in binding["spans"]
                ],
            }
    return _binding_for_bubbles(prose, bubbles)


def decision_to_script_commit(
    story_id: str,
    phase: str,
    from_at: datetime,
    now: datetime,
    participant_id: str,
    decision: Dict[str, Any],
    message_separator: str = DEFAULT_MESSAGE_SEPARATOR,
    split_reply_messages: bool = True,
    frame_id: str = "",
    burst_id: str = "",
) -> Dict[str, Any]:
    """把归一化决策编译为 ScriptCommit（对应 decisionToScriptCommit）。

    生成稳定 commitId（语义哈希）与事件序列：message-perceived /
    narrative / outgoing-message / group-message / follow-up-promise /
    future-intent / platform-action。
    """
    participant_id = (participant_id or "").strip()
    prose = (decision.get("script") or "").strip()
    semantic_payload = {
        "participantId": participant_id,
        "prose": prose,
        "interaction": decision.get("interaction"),
        "groupReplyContent": decision.get("groupReply") or "",
        "crossConversationActions": decision.get("crossConversationActions") or [],
        "intents": decision.get("intents") or [],
        "followUpCommitment": decision.get("followUpCommitment"),
        "localMedia": decision.get("localMedia"),
        "nativeFace": decision.get("nativeFace"),
    }
    digest = hashlib.sha256(
        "\x1f".join([
            story_id,
            phase,
            to_iso(from_at),
            to_iso(now),
            json.dumps(semantic_payload, ensure_ascii=False, sort_keys=True, default=str),
        ]).encode("utf-8")
    ).hexdigest()[:20]
    commit_id = f"commit:{digest}"
    events: List[Dict[str, Any]] = []

    def add(event: Dict[str, Any]) -> Dict[str, Any]:
        item = {**event, "commitId": commit_id, "eventId": f"{commit_id}:e{len(events) + 1}"}
        events.append(item)
        return item

    def add_message_event(
        target_participant: str, content: str, mode: str, occurred_at: datetime, caused_by: List[str], endpoint_id: str = ""
    ) -> None:
        bubbles = split_visible_reply_bubbles(content, message_separator, split_reply_messages)
        canonical = message_separator.join(bubbles) if split_reply_messages else content
        event: Dict[str, Any] = {
            "kind": "outgoing-message",
            "actor": "protagonist",
            "occurredAt": to_iso(occurred_at),
            "causedByEventIds": caused_by,
            "participantId": target_participant,
            "content": canonical,
            "bubbles": bubbles,
            "deliveryMode": mode,
            "scriptBinding": (
                {"status": "future"} if mode == "delayed"
                else _bind_immediate_message_action(prose, bubbles, decision.get("authoredActions") or [])
            ),
        }
        if endpoint_id:
            event["endpointId"] = endpoint_id
        add(event)

    interaction = decision.get("interaction")
    perceived = None
    if phase == "user-message" and isinstance(interaction, dict) and interaction.get("seen") and participant_id:
        perceived = add({
            "kind": "message-perceived",
            "actor": "protagonist",
            "occurredAt": to_iso(now),
            "causedByEventIds": [],
            "participantId": participant_id,
        })
    narrative = add({
        "kind": "narrative",
        "actor": "protagonist",
        "occurredAt": to_iso(now),
        "causedByEventIds": [perceived["eventId"]] if perceived else [],
        "content": prose,
    })
    reply = interaction.get("reply") if isinstance(interaction, dict) else None
    if reply and isinstance(reply.get("content"), str) and reply["content"].strip() and reply.get("mode") in ("immediate", "delayed"):
        occurred = ensure_utc(reply.get("sendAt")) if reply["mode"] == "delayed" and reply.get("sendAt") else now
        add_message_event(
            participant_id, reply["content"], reply["mode"], occurred or now,
            [perceived["eventId"] if perceived else narrative["eventId"]],
        )
    for action in decision.get("crossConversationActions") or []:
        if not action.get("content") or action.get("mode") not in ("immediate", "delayed"):
            continue
        occurred = ensure_utc(action.get("sendAt")) if action["mode"] == "delayed" and action.get("sendAt") else now
        add_message_event(
            action["participantId"], action["content"], action["mode"], occurred or now,
            [narrative["eventId"]], action.get("endpointId") or "",
        )
    if decision.get("groupReply"):
        bubbles = split_visible_reply_bubbles(decision["groupReply"], message_separator, split_reply_messages)
        add({
            "kind": "group-message",
            "actor": "protagonist",
            "occurredAt": to_iso(now),
            "causedByEventIds": [narrative["eventId"]],
            "content": message_separator.join(bubbles) if split_reply_messages else decision["groupReply"],
            "bubbles": bubbles,
            "deliveryMode": "immediate",
            "scriptBinding": _bind_immediate_message_action(prose, bubbles, decision.get("authoredActions") or []),
        })
    commitment = decision.get("followUpCommitment")
    if isinstance(commitment, dict) and isinstance(commitment.get("summary"), str):
        add({
            "kind": "follow-up-promise",
            "actor": "protagonist",
            "occurredAt": to_iso(now),
            "causedByEventIds": [narrative["eventId"]],
            "participantId": participant_id,
            "content": commitment["summary"],
            "metadata": {"notBefore": commitment.get("notBefore"), "kind": commitment.get("kind")},
        })
    for intent in decision.get("intents") or []:
        add({
            "kind": "future-intent",
            "actor": "protagonist",
            "occurredAt": to_iso(now),
            "causedByEventIds": [narrative["eventId"]],
            "participantId": intent.get("participantId") or participant_id,
            "content": intent.get("summary"),
            "metadata": {"type": intent.get("type"), "notBefore": intent.get("notBefore")},
        })
    for browser in decision.get("browserIntents") or []:
        if not isinstance(browser, dict):
            continue
        purpose = browser.get("purpose") or browser.get("query") or browser.get("url")
        if not isinstance(purpose, str) or not purpose.strip():
            continue
        add({
            "kind": "browser-intent",
            "actor": "protagonist",
            "occurredAt": to_iso(now),
            "causedByEventIds": [narrative["eventId"]],
            "participantId": participant_id,
            "content": clip(purpose, 300),
            "metadata": {
                "mode": browser.get("mode") if browser.get("mode") in ("search", "visit") else "search",
                "timing": "immediate" if browser.get("timing") == "immediate" else "deferred",
                "query": clip(browser.get("query"), 300) if isinstance(browser.get("query"), str) else "",
                "url": clip(browser.get("url"), 2000) if isinstance(browser.get("url"), str) else "",
            },
        })
    if decision.get("localMedia") or decision.get("nativeFace") or decision.get("messageReactions"):
        metadata: Dict[str, Any] = {}
        if decision.get("localMedia"):
            metadata["localMedia"] = decision["localMedia"]
        if decision.get("nativeFace"):
            metadata["nativeFace"] = decision["nativeFace"]
        if decision.get("messageReactions"):
            metadata["messageReactions"] = decision["messageReactions"]
        add({
            "kind": "platform-action",
            "actor": "protagonist",
            "occurredAt": to_iso(now),
            "causedByEventIds": [narrative["eventId"]],
            "participantId": participant_id,
            "metadata": metadata,
        })
    return {
        "commitId": commit_id,
        "storyId": story_id,
        "participantId": participant_id,
        "phase": phase,
        "window": {"from": to_iso(from_at), "to": to_iso(now)},
        "prose": prose,
        "events": events,
        "sceneDelta": {
            "frameId": frame_id,
            "burstId": burst_id,
            "proseAppend": prose,
            "eventIds": [event["eventId"] for event in events],
        },
        "sourceFormat": "script-first-v1",
    }


def find_private_outgoing_message_event(
    commit: Dict[str, Any], participant_id: str, mode: Optional[str] = None, content: Optional[str] = None,
    separator: str = DEFAULT_MESSAGE_SEPARATOR,
) -> Optional[Dict[str, Any]]:
    for event in commit.get("events") or []:
        if event.get("kind") != "outgoing-message" or event.get("participantId") != participant_id:
            continue
        if mode and event.get("deliveryMode") != mode:
            continue
        if content is not None:
            same = event.get("content") == content or (
                separator.join(event.get("bubbles") or []) == separator.join(split_visible_reply_bubbles(content, separator, True))
            )
            if not same:
                continue
        return event
    return None


def find_group_script_event(commit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return next((event for event in commit.get("events") or [] if event.get("kind") == "group-message"), None)


# --------------------------------------------------------------------------- #
# 投递账本（delivery-ledger.ts）
# --------------------------------------------------------------------------- #


def delivery_segments(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把事件展开为投递 segment 列表（对应 deliverySegments）。"""
    if event.get("kind") == "outgoing-message" or event.get("deliveryMode"):
        return [
            {"index": index, "kind": "message", "content": content, "status": "pending"}
            for index, content in enumerate(event.get("bubbles") or [])
        ]
    if event.get("kind") != "platform-action":
        return []
    segments: List[Dict[str, Any]] = []
    metadata = event.get("metadata") or {}
    local_media = metadata.get("localMedia")
    if isinstance(local_media, dict) and isinstance(local_media.get("assetId"), str):
        segments.append({"index": len(segments), "kind": "local-media", "content": local_media["assetId"], "status": "pending"})
    native_face = metadata.get("nativeFace")
    if isinstance(native_face, dict) and isinstance(native_face.get("semantic"), str):
        segments.append({"index": len(segments), "kind": "native-face", "content": native_face["semantic"], "status": "pending"})
    for reaction in metadata.get("messageReactions") or []:
        if not isinstance(reaction, dict):
            continue
        if isinstance(reaction.get("messageRef"), str) and isinstance(reaction.get("reaction"), str):
            segments.append({
                "index": len(segments),
                "kind": "message-reaction",
                "content": f"{reaction['messageRef']}:{reaction['reaction']}",
                "targetId": str(reaction.get("messageId") or ""),
                "status": "pending",
            })
    return segments


def create_script_delivery_actions(commit: Dict[str, Any]) -> List[Dict[str, Any]]:
    """为提交创建投递账本（对应 createScriptDeliveryActions）。"""
    actions: List[Dict[str, Any]] = []
    for event in commit.get("events") or []:
        segments = delivery_segments(event)
        if not segments:
            continue
        action: Dict[str, Any] = {
            "commitId": commit.get("commitId"),
            "eventId": event.get("eventId"),
            "eventKind": event.get("kind"),
            "status": "pending",
            "segments": segments,
            "updatedAt": commit.get("window", {}).get("to"),
        }
        if event.get("participantId"):
            action["participantId"] = event["participantId"]
        if event.get("endpointId"):
            action["endpointId"] = event["endpointId"]
        actions.append(action)
    return actions


def aggregate_delivery_status(segments: Sequence[Dict[str, Any]]) -> str:
    """聚合 segment 状态（对应 aggregateDeliveryStatus）。"""
    if not segments or all(segment.get("status") == "pending" for segment in segments):
        return "pending"
    if all(segment.get("status") == "delivered" for segment in segments):
        return "delivered"
    if any(segment.get("status") == "delivered" for segment in segments):
        return "partial"
    if any(segment.get("status") == "pending" for segment in segments):
        return "pending"
    if any(segment.get("status") == "failed" for segment in segments):
        return "failed"
    return "cancelled"


def update_script_delivery_actions(
    current: Any, reference: Dict[str, Any], status: str, at: datetime, reason: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """按 segment 回写投递账本（对应 updateScriptDeliveryActions）。

    已 delivered / cancelled 的 segment 不可被覆盖；返回 None 表示无变化。
    """
    if not isinstance(current, list):
        return None
    changed = False
    timestamp = to_iso(at)
    actions: List[Dict[str, Any]] = []
    for raw in current:
        if not (
            isinstance(raw, dict)
            and raw.get("commitId") == reference.get("commitId")
            and raw.get("eventId") == reference.get("eventId")
        ):
            actions.append(raw)
            continue
        action_changed = False
        segments: List[Dict[str, Any]] = []
        for segment in raw.get("segments") or []:
            if segment.get("index") != reference.get("segmentIndex"):
                segments.append(segment)
                continue
            if segment.get("status") in ("delivered", "cancelled"):
                segments.append(segment)
                continue
            if segment.get("status") == status and segment.get("reason") == reason:
                segments.append(segment)
                continue
            changed = True
            action_changed = True
            updated = {
                **segment,
                "status": status,
                "attemptedAt": segment.get("attemptedAt") or timestamp,
            }
            if status == "pending":
                updated.pop("completedAt", None)
            else:
                updated["completedAt"] = timestamp
            if reason:
                updated["reason"] = reason
            else:
                updated.pop("reason", None)
            segments.append(updated)
        if action_changed:
            actions.append({**raw, "segments": segments, "status": aggregate_delivery_status(segments), "updatedAt": timestamp})
        else:
            actions.append(raw)
    return actions if changed else None


def delivery_reference(event: Optional[Dict[str, Any]], script_entry_id: Optional[int], segment_index: int = 0) -> Optional[Dict[str, Any]]:
    if not event or not isinstance(script_entry_id, int):
        return None
    return {
        "commitId": event.get("commitId"),
        "eventId": event.get("eventId"),
        "scriptEntryId": script_entry_id,
        "segmentIndex": segment_index,
    }


def platform_action_reference(
    commit: Optional[Dict[str, Any]], script_entry_id: Optional[int], kind: str, content: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    if not commit:
        return None
    event = next((item for item in commit.get("events") or [] if item.get("kind") == "platform-action"), None)
    if not event or not isinstance(script_entry_id, int):
        return None
    segment = next(
        (
            item
            for item in delivery_segments(event)
            if item.get("kind") == kind and (content is None or item.get("content") == content)
        ),
        None,
    )
    return delivery_reference(event, script_entry_id, segment["index"]) if segment else None


# --------------------------------------------------------------------------- #
# 投递现实投影与延续书签（delivery-reality / continuation）
# --------------------------------------------------------------------------- #


def _message_head(content: str, limit: int = 12) -> str:
    """消息类片段只保留开头（对应 rc57 `messageHead`，按 Unicode 码点计）。"""
    points = list(content)
    if len(points) <= limit:
        return content
    return "".join(points[:limit]) + "…"


def _receipt_clock(segment: Dict[str, Any], timezone_name: str) -> str:
    """投递回执时间 HH:mm（优先 completedAt，其次 attemptedAt）。"""
    moment = ensure_utc(segment.get("completedAt")) or ensure_utc(segment.get("attemptedAt"))
    return format_story_clock(moment, timezone_name) if moment else ""


def delivery_reality_projection(
    entries: Sequence[Any],
    participant_id: str,
    share_participant_details: bool = False,
    limit: int = 8,
    character_budget: int = 2000,
    timezone_name: str = "",
) -> List[Dict[str, Any]]:
    """投影未确认送达的可见动作（对应 rc57 `deliveryReality`）。

    rc57 起：消息片段只保留 12 个码点的 `head`，附带回执时钟 `at`、
    `eventKind` 与 `index`，并按字符预算（默认 2000）从新到旧裁剪。
    """
    projected: List[Dict[str, Any]] = []
    for entry in entries:
        kind = getattr(entry, "kind", None)
        metadata = getattr(entry, "metadata", None) or {}
        entry_id = getattr(entry, "id", None)
        if kind != "script":
            continue
        actions = metadata.get("deliveryActions")
        if not isinstance(actions, list):
            continue
        if not actions and metadata.get("commitId"):
            projected.append({
                "sourceEntryId": entry_id,
                "eventId": "",
                "segments": [],
                "communicationOutcome": "no-outgoing-action-recorded",
            })
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            if action.get("commitId") != metadata.get("commitId") or not isinstance(action.get("segments"), list):
                continue
            if action.get("participantId") and action["participantId"] != participant_id and not share_participant_details:
                continue
            segments = [
                item
                for item in action["segments"]
                if isinstance(item, dict)
                and isinstance(item.get("content"), str)
                and item.get("status") in ("pending", "delivered", "failed", "cancelled")
            ]
            if not segments or not any(item.get("status") != "delivered" for item in segments):
                continue
            view_segments = []
            for position, item in enumerate(segments):
                segment_kind = str(item.get("kind") or "message")
                at = _receipt_clock(item, timezone_name)
                view_segments.append({
                    "index": item.get("index") if isinstance(item.get("index"), int) else position,
                    "kind": segment_kind,
                    "head": _message_head(item["content"]) if segment_kind == "message" else item["content"],
                    "outcome": (
                        "not-confirmed" if item.get("status") == "pending"
                        else "delivery-not-confirmed-after-error" if item.get("status") == "failed"
                        else item.get("status")
                    ),
                    **({"at": at} if at else {}),
                })
            projected.append({
                "sourceEntryId": entry_id,
                "eventId": action.get("eventId"),
                "eventKind": action.get("eventKind") if isinstance(action.get("eventKind"), str) else "outgoing-message",
                "segments": view_segments,
            })
    limited = projected[-max(1, int(limit)):]
    kept: List[Dict[str, Any]] = []
    remaining = max(200, int(character_budget))
    for item in reversed(limited):
        size = len(json.dumps(item, ensure_ascii=False))
        if kept and size > remaining:
            break
        kept.insert(0, item)
        remaining -= size
    return kept


def continuation_bookmark(entries: Sequence[Any], from_at: datetime, now: datetime) -> Dict[str, Any]:
    """续写书签：定位最后一段已完成的正文与最近通信（对应 continuationBookmark）。"""
    visible = [entry for entry in entries if getattr(entry, "occurredAt", None) and entry.occurredAt <= now]
    scripts = [entry for entry in visible if getattr(entry, "kind", "") == "script" and entry.occurredAt <= from_at]
    last_script = scripts[-1] if scripts else None

    def reference(entry: Any) -> Dict[str, Any]:
        return {
            "entryId": getattr(entry, "id", None),
            "kind": getattr(entry, "kind", ""),
            "participantId": getattr(entry, "participantId", ""),
            "occurredAt": to_iso(getattr(entry, "occurredAt", None)),
        }

    recent_kinds = ("user-message", "character-message", "character-group-message", "character-platform-action")
    recent = [
        reference(entry)
        for entry in visible
        if (getattr(entry, "kind", "") != "user-message" or entry.occurredAt <= from_at) and getattr(entry, "kind", "") in recent_kinds
    ][-4:]
    bookmark: Dict[str, Any] = {
        "establishedThrough": to_iso(from_at),
        "writingStart": "after-last-completed-passage",
        "newEventEntryIds": [
            getattr(entry, "id", None)
            for entry in visible
            if getattr(entry, "kind", "") == "user-message" and entry.occurredAt > from_at
        ],
        "recentCommunications": recent,
    }
    if last_script is not None:
        bookmark["lastScript"] = reference(last_script)
        bookmark["originalEndpoint"] = {
            "entryId": getattr(last_script, "id", None),
            "characterOffset": len(getattr(last_script, "content", "")),
        }
    return bookmark


# --------------------------------------------------------------------------- #
# 意图生命周期（intent-lifecycle.ts）
# --------------------------------------------------------------------------- #

_NON_LIVE_INTENT_TYPES = ("split-message", "browser-research", "narrative-retry", "proactive-check", "active-consequence")


def live_narrative_intents(intents: Iterable[Any]) -> List[Any]:
    """主叙事可见的到期意图（排除分段/浏览/重试/主动重查/余波）。"""
    return [intent for intent in intents if getattr(intent, "type", None) not in _NON_LIVE_INTENT_TYPES]


def consumed_live_intent_ids(intents: Iterable[Any]) -> List[int]:
    """写作成功后被消费的到期意图 id（承诺回访除外）。"""
    return [
        intent.id
        for intent in live_narrative_intents(intents)
        if getattr(intent, "type", None) != "follow-up-commitment"
    ]


def group_due_intents(intents: Iterable[Any]) -> List[List[Any]]:
    """按 参与者 + 家族 分批到期意图（对应 groupDueIntents）。"""
    batches: Dict[str, List[Any]] = {}
    ordered = sorted(
        intents,
        key=lambda intent: (getattr(intent, "notBefore", None) or datetime.min.replace(tzinfo=timezone_utc()), getattr(intent, "id", 0)),
    )
    for intent in ordered:
        family = "agency" if getattr(intent, "type", None) == "proactive-check" else "normal"
        key = f"{getattr(intent, 'participantId', '') or '__global__'}|{family}"
        batches.setdefault(key, []).append(intent)
    return list(batches.values())


def timezone_utc():
    from datetime import timezone

    return timezone.utc


def normalize_scene_presence_drafts(value: Any, entries: Sequence[Any], now: datetime) -> List[Dict[str, Any]]:
    """场景在场表校验：每条状态必须有引用条目里的显式证据（对应 normalizeScenePresenceDrafts）。"""
    if not isinstance(value, list):
        return []
    by_id = {getattr(entry, "id", None): entry for entry in entries}
    next_items: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = clip(item.get("name"), 80).strip() if isinstance(item.get("name"), str) else ""
        status = item.get("status") if item.get("status") in ("present", "off-scene", "expected") else None
        basis = clip(item.get("basis"), 300).strip() if isinstance(item.get("basis"), str) else ""
        source_entry_ids = (
            [entry_id for entry_id in item.get("sourceEntryIds", []) if entry_id in by_id][:8]
            if isinstance(item.get("sourceEntryIds"), list)
            else []
        )
        evidence = [by_id[entry_id] for entry_id in source_entry_ids if name in getattr(by_id[entry_id], "content", "")]
        if not name or not status or not basis or not evidence or not has_explicit_presence_evidence(status, evidence):
            continue
        next_items.append({
            "name": name,
            "status": status,
            "basis": basis,
            "sourceEntryIds": source_entry_ids,
            "updatedAt": to_iso(now),
        })
    return next_items


_PRESENCE_PATTERNS = {
    "off-scene": re.compile("告别|道别|分别|先走|离开|离去|回家|回去了|独自|分开|告辞"),
    "expected": re.compile("约好|约在|等会|稍后|会来|准备来|约见"),
    "present": re.compile("一起|同行|身边|来到|抵达|进入|走进|拉着|坐在|站在|陪着"),
}


def has_explicit_presence_evidence(status: str, entries: Sequence[Any]) -> bool:
    text = "\n".join(getattr(entry, "content", "") for entry in entries)
    pattern = _PRESENCE_PATTERNS.get(status)
    return bool(pattern and pattern.search(text))


def merge_setting(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并故事设定（对应 mergeSetting，character/user 做浅合并）。"""
    merged = {**base, **patch}
    if isinstance(patch.get("character"), dict):
        merged["character"] = {**(base.get("character") or {}), **patch["character"]}
    if isinstance(patch.get("user"), dict):
        merged["user"] = {**(base.get("user") or {}), **patch["user"]}
    return merged


def merge_participant_state(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    merged = {**base, **patch}
    if not isinstance(patch.get("openThreads"), list):
        merged["openThreads"] = base.get("openThreads") or []
    if not isinstance(patch.get("relationshipNotes"), list):
        merged["relationshipNotes"] = base.get("relationshipNotes") or []
    return merged


def normalize_participant_state(value: Any) -> Dict[str, Any]:
    record = value if isinstance(value, dict) else {}

    def string_list(key: str) -> List[str]:
        items = record.get(key)
        if not isinstance(items, list):
            return []
        return [clip(item, 500) for item in items if isinstance(item, str)][:50]

    def non_negative(key: str) -> int:
        number = record.get(key)
        return max(0, int(number)) if isinstance(number, (int, float)) and not isinstance(number, bool) else 0

    return {
        "openThreads": string_list("openThreads"),
        "relationshipNotes": string_list("relationshipNotes"),
        "relationshipOverlay": clip(record.get("relationshipOverlay"), 4000) if isinstance(record.get("relationshipOverlay"), str) else "",
        "unreadMessageCount": non_negative("unreadMessageCount"),
        "pendingReplyCount": non_negative("pendingReplyCount"),
        "lastUserMessageAt": record.get("lastUserMessageAt") if isinstance(record.get("lastUserMessageAt"), str) else None,
        "lastCharacterMessageAt": record.get("lastCharacterMessageAt") if isinstance(record.get("lastCharacterMessageAt"), str) else None,
    }


def participant_relevance(participant: Any) -> float:
    """参与者排序权重（对应 participantRelevance）。"""
    state = normalize_participant_state(getattr(participant, "state", None))
    pending = state["pendingReplyCount"] * 2 + state["unreadMessageCount"]
    last = ensure_utc(state.get("lastUserMessageAt")) or getattr(participant, "updatedAt", None)
    return pending * 1e9 + (last.timestamp() if last else 0)
