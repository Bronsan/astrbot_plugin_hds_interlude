"""群聊本地意愿门。

对应上游 `src/group-willingness.ts`（逐行移植：阈值、半衰减、
边际递减、阈值概率、成功发言成本、五档预设、auto 生活状态映射、
睡眠态 ×0.2、skipCount 抖动）。纯算法，不写数据库、不调用模型。
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, Optional, Tuple

#: 默认数值门（对应 DEFAULT_GROUP_WILLINGNESS）。
DEFAULT_GROUP_WILLINGNESS: Dict[str, Any] = {
    "enabled": False,
    "maxScore": 1,
    "threshold": 0.24,
    "probabilityAmplifier": 1.3,
    "decayHalfLifeSeconds": 180,
    "replyCost": 0.55,
    "baseGain": 0.12,
    "quoteGain": 0.12,
    "keywordGain": 0.18,
    "keywords": [],
}

#: 五档意愿预设（对应 WILLINGNESS_TIERS，数值与上游逐字一致）。
WILLINGNESS_TIERS: Dict[str, Dict[str, Any]] = {
    "quiet": {"enabled": True, "maxScore": 2, "threshold": 0.8, "probabilityAmplifier": 1.1,
              "decayHalfLifeSeconds": 150, "replyCost": 0.85, "baseGain": 0.12, "quoteGain": 0.08,
              "keywordGain": 0.12, "keywords": []},
    "reserved": {"enabled": True, "maxScore": 2, "threshold": 0.75, "probabilityAmplifier": 1.25,
                 "decayHalfLifeSeconds": 200, "replyCost": 0.85, "baseGain": 0.17, "quoteGain": 0.12,
                 "keywordGain": 0.16, "keywords": []},
    "normal": {"enabled": True, "maxScore": 2, "threshold": 0.62, "probabilityAmplifier": 1.4,
               "decayHalfLifeSeconds": 240, "replyCost": 0.8, "baseGain": 0.25, "quoteGain": 0.15,
               "keywordGain": 0.2, "keywords": []},
    "active": {"enabled": True, "maxScore": 2, "threshold": 0.3, "probabilityAmplifier": 1.6,
               "decayHalfLifeSeconds": 300, "replyCost": 0.6, "baseGain": 0.34, "quoteGain": 0.2,
               "keywordGain": 0.25, "keywords": []},
    "eager": {"enabled": True, "maxScore": 2, "threshold": 0.1, "probabilityAmplifier": 1.8,
              "decayHalfLifeSeconds": 360, "replyCost": 0.5, "baseGain": 0.45, "quoteGain": 0.28,
              "keywordGain": 0.3, "keywords": []},
}

DEFAULT_AUTO_WILLINGNESS = {"busy": "quiet", "idle": "active", "asleep": "quiet"}

#: 成功发言后的静默批次抖动（对应 WILLINGNESS_JITTER：[最小, 最大]）。
WILLINGNESS_JITTER: Dict[str, Tuple[int, int]] = {
    "quiet": (1, 9),
    "reserved": (1, 7),
    "normal": (0, 6),
    "active": (0, 4),
    "eager": (0, 3),
}

#: 生活节奏对静默批次的修正（对应 RHYTHM_SKIP_MODIFIER）。
RHYTHM_SKIP_MODIFIER = {
    "phone-focused": -1,
    "available": 0,
    "occupied": 1,
    "resting": 2,
    "asleep": 3,
}

ASLEEP_PROBABILITY_MULTIPLIER = 0.2
LIFE_STATUS_STALE_SECONDS = 6 * 60 * 60

WILLINGNESS_TIER_NAMES = ("quiet", "reserved", "normal", "active", "eager")


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def resolve_group_willingness(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """合并默认值（对应 resolveGroupWillingness，keywords 去空截断 30 条）。"""
    merged = dict(DEFAULT_GROUP_WILLINGNESS)
    if isinstance(config, dict):
        merged.update(config)
    keywords = merged.get("keywords") or []
    merged["keywords"] = [str(item).strip() for item in keywords if str(item).strip()][:30]
    return merged


def _decay(previous: Optional[Dict[str, Any]], config: Dict[str, Any], now_seconds: float) -> Dict[str, Any]:
    """半衰期衰减（对应 decay）。score 低于 1e-3 归零。"""
    score = float((previous or {}).get("score") or 0.0)
    updated_at = float((previous or {}).get("updatedAt") or now_seconds)
    elapsed = max(0.0, now_seconds - updated_at)
    factor = 0.5 ** (elapsed / max(1.0, float(config.get("decayHalfLifeSeconds") or 180)))
    decayed = score * factor
    state: Dict[str, Any] = {
        "score": 0.0 if decayed < 1e-3 else decayed,
        "updatedAt": now_seconds,
    }
    skip_count = (previous or {}).get("skipCount")
    if skip_count:
        state["skipCount"] = skip_count
    return state


def evaluate_group_willingness(
    previous: Optional[Dict[str, Any]],
    config_input: Optional[Dict[str, Any]],
    message_count: int,
    mentioned_bot: bool,
    quoted_bot: bool,
    content: str,
    now_seconds: Optional[float] = None,
    roll: Optional[float] = None,
) -> Dict[str, Any]:
    """普通群消息的意愿判定（对应 evaluateGroupWillingness）。

    返回 {state, shouldCall, probability, reason}。`roll` 供测试注入随机值。
    """
    now = now_seconds if now_seconds is not None else time.time()
    config = resolve_group_willingness(config_input)
    state = _decay(previous, config, now)
    if not config.get("enabled"):
        return {"state": state, "shouldCall": True, "probability": 1.0, "reason": "disabled"}
    skip_count = int(state.get("skipCount") or 0)
    if skip_count > 0 and not mentioned_bot:
        state["skipCount"] = max(0, skip_count - max(1, int(message_count or 1)))
        state["updatedAt"] = now
        return {"state": state, "shouldCall": False, "probability": 0.0, "reason": "skip-jitter"}
    keyword_hit = any(str(keyword) in (content or "") for keyword in config.get("keywords") or [])
    raw_gain = (
        float(config.get("baseGain") or 0.12) * max(1, min(3, int(message_count or 1)))
        + (float(config.get("quoteGain") or 0.12) if quoted_bot else 0.0)
        + (float(config.get("keywordGain") or 0.18) if keyword_hit else 0.0)
    )
    marginal = 1 - min(1.0, float(state["score"]) / max(0.0001, float(config.get("maxScore") or 1))) ** 2
    state["score"] = _clamp(
        float(state["score"]) + raw_gain * max(0.0, marginal), 0.0, float(config.get("maxScore") or 1)
    )
    if mentioned_bot:
        return {"state": state, "shouldCall": True, "probability": 1.0, "reason": "forced-mention"}
    if float(state["score"]) <= float(config.get("threshold") or 0.24):
        return {"state": state, "shouldCall": False, "probability": 0.0, "reason": "below-threshold"}
    probability = _clamp(
        (float(state["score"]) - float(config.get("threshold") or 0.24)) * float(config.get("probabilityAmplifier") or 1.3),
        0.0,
        1.0,
    )
    outcome = roll if roll is not None else random.random()
    return {
        "state": state,
        "shouldCall": outcome < probability,
        "probability": probability,
        "reason": "probability-roll",
    }


def consume_group_willingness(
    previous: Optional[Dict[str, Any]], config_input: Optional[Dict[str, Any]], now_seconds: float
) -> Dict[str, Any]:
    """成功发言后扣减意愿（对应 consumeGroupWillingness）。"""
    config = resolve_group_willingness(config_input)
    state = _decay(previous, config, now_seconds)
    return {
        "score": max(0.0, float(state["score"]) - float(config.get("replyCost") or 0.55)),
        "updatedAt": now_seconds,
    }


def _normalize_tier(value: Any) -> Optional[str]:
    return value if value in WILLINGNESS_TIER_NAMES else None


def _resolve_auto_willingness(value: Any) -> Dict[str, str]:
    record = value if isinstance(value, dict) else {}
    return {
        "busy": _normalize_tier(record.get("busy")) or DEFAULT_AUTO_WILLINGNESS["busy"],
        "idle": _normalize_tier(record.get("idle")) or DEFAULT_AUTO_WILLINGNESS["idle"],
        "asleep": _normalize_tier(record.get("asleep")) or DEFAULT_AUTO_WILLINGNESS["asleep"],
    }


def resolve_willingness_layers(
    preset: Any,
    auto_input: Any,
    life_status: Optional[Dict[str, Any]],
    now_seconds: float,
    legacy_input: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """解析 preset/auto/custom/off 四层配置（对应 resolveWillingnessLayers）。

    auto 档：lifeStatus 缺失/无效/超过 6 小时未刷新时按 idle 档位；
    返回 diagnosis（preset/tier/lifeStatus/fallback/asleep）供日志与调试。
    """
    raw_preset = preset if isinstance(preset, str) else ""
    legacy_enabled = bool((legacy_input or {}).get("enabled") is True)
    if legacy_enabled and raw_preset in ("", "off"):
        resolved = "custom"
    elif raw_preset in ("quiet", "reserved", "normal", "active", "eager", "auto", "custom"):
        resolved = raw_preset
    else:
        resolved = "off"
    diagnosis: Dict[str, Any] = {"preset": resolved, "asleep": False}
    if resolved in ("off", "custom"):
        if resolved == "off":
            config = dict(legacy_input or {})
            config["enabled"] = False
        else:
            config = dict(legacy_input or {})
        return {"preset": resolved, "config": config, "diagnosis": diagnosis}
    if resolved == "auto":
        status = (life_status or {}).get("status") if isinstance(life_status, dict) else None
        updated_text = (life_status or {}).get("updatedAt") if isinstance(life_status, dict) else None
        updated_at = None
        if isinstance(updated_text, str) and updated_text.strip():
            try:
                from datetime import datetime

                text = updated_text.strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                updated_at = datetime.fromisoformat(text).timestamp()
            except ValueError:
                updated_at = None
        if status in ("busy", "idle", "asleep") and updated_at is not None:
            stale = now_seconds - updated_at > LIFE_STATUS_STALE_SECONDS
        else:
            stale = None
        if status not in ("busy", "idle", "asleep") or updated_at is None or stale:
            tier = _resolve_auto_willingness(auto_input)["idle"]
            diagnosis["lifeStatus"] = "idle"
            if not life_status:
                diagnosis["fallback"] = "missing"
            elif status not in ("busy", "idle", "asleep") or updated_at is None:
                diagnosis["fallback"] = "invalid"
            else:
                diagnosis["fallback"] = "stale"
                diagnosis["stale"] = True
        else:
            tier = _resolve_auto_willingness(auto_input)[status]
            diagnosis["lifeStatus"] = status
            diagnosis["asleep"] = status == "asleep"
    else:
        tier = resolved
    diagnosis["tier"] = tier
    tier_config = dict(WILLINGNESS_TIERS[tier])
    tier_config["keywords"] = (legacy_input or {}).get("keywords") or []
    return {"preset": resolved, "config": tier_config, "diagnosis": diagnosis}


def format_willingness_gate_diagnosis(diagnosis: Dict[str, Any]) -> str:
    """把诊断对象格式化为一行日志文本（对应 formatWillingnessGateDiagnosis）。"""
    if diagnosis.get("preset") != "auto":
        return str(diagnosis.get("tier") or diagnosis.get("preset") or "")
    prefix = {
        "missing": "无状态→",
        "invalid": "无效→",
        "stale": "过期→",
    }.get(diagnosis.get("fallback") or "", "")
    asleep_note = "×0.2" if diagnosis.get("asleep") else ""
    return f"auto({prefix}{diagnosis.get('lifeStatus') or 'idle'}→{diagnosis.get('tier')}{asleep_note})"


def evaluate_willingness_gate(
    previous: Optional[Dict[str, Any]],
    preset: Any,
    auto_input: Any,
    life_status: Optional[Dict[str, Any]],
    legacy_input: Optional[Dict[str, Any]],
    message_count: int,
    mentioned_bot: bool,
    quoted_bot: bool,
    content: str,
    now_seconds: Optional[float] = None,
    roll: Optional[float] = None,
) -> Dict[str, Any]:
    """完整意愿门（对应 evaluateWillingnessGate，含睡眠态处理）。"""
    now = now_seconds if now_seconds is not None else time.time()
    layers = resolve_willingness_layers(preset, auto_input, life_status, now, legacy_input)
    if layers["diagnosis"].get("asleep"):
        base = evaluate_group_willingness(
            previous,
            layers["config"],
            message_count,
            mentioned_bot=False,
            quoted_bot=quoted_bot,
            content=content,
            now_seconds=now,
            roll=1.0,
        )
        probability = _clamp(base["probability"] * ASLEEP_PROBABILITY_MULTIPLIER, 0.0, 1.0)
        outcome = roll if roll is not None else random.random()
        should_call = outcome < probability
        return {
            "state": base["state"],
            "shouldCall": should_call,
            "probability": probability,
            "reason": "probability-roll" if should_call else "asleep",
            "diagnosis": layers["diagnosis"],
        }
    decision = evaluate_group_willingness(
        previous,
        layers["config"],
        message_count,
        mentioned_bot=mentioned_bot,
        quoted_bot=quoted_bot,
        content=content,
        now_seconds=now,
        roll=roll,
    )
    decision["diagnosis"] = layers["diagnosis"]
    return decision


def consume_willingness_gate(
    previous: Optional[Dict[str, Any]],
    preset: Any,
    auto_input: Any,
    life_status: Optional[Dict[str, Any]],
    legacy_input: Optional[Dict[str, Any]],
    now_seconds: float,
    rhythm_attention: Optional[str] = None,
    roll: Optional[float] = None,
) -> Dict[str, Any]:
    """成功发言后消耗意愿并生成静默批次（对应 consumeWillingnessGate）。"""
    layers = resolve_willingness_layers(preset, auto_input, life_status, now_seconds, legacy_input)
    consumed = consume_group_willingness(previous, layers["config"], now_seconds)
    tier = layers["diagnosis"].get("tier")
    jitter_min, jitter_max = WILLINGNESS_JITTER.get(tier or "", (0, 3))
    rhythm_mod = RHYTHM_SKIP_MODIFIER.get(rhythm_attention or "", 0)
    outcome = roll if roll is not None else random.random()
    raw = jitter_min + int(outcome * (jitter_max - jitter_min + 1)) + rhythm_mod
    consumed["skipCount"] = max(0, raw)
    return consumed
