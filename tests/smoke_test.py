"""HDS Interlude 移植版冒烟测试。

覆盖：
1. 纯逻辑模块（时间锚 / 群意愿门 / Alter / 日程预排 / 剧本引擎）；
2. 回合引擎与调度器状态机；
3. 服务层端到端小回合（存根模型 + 存根投递）：接收 → 合并写作 →
   落库 → 投递账本 → 自动推进 → 管理命令支撑。

运行：python tests/smoke_test.py
（不依赖 AstrBot 本体；核心模块不含 astrbot 导入。）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import alter as alter_mod  # noqa: E402
from core import agency as agency_mod  # noqa: E402
from core import group_willingness as gw  # noqa: E402
from core import schedule_preplan as preplan  # noqa: E402
from core import script_engine as se  # noqa: E402
from core import urge as urge_mod  # noqa: E402
from core.narrator import (  # noqa: E402
    DEFAULT_STYLE_PROMPT,
    NarrativeResult,
    build_system_prompt,
    phase_instruction,
    transport_instruction,
)
from core.service import InterludeService, SessionView  # noqa: E402
from core.share_sync import SHARE_SECTIONS  # noqa: E402
from core.timeutil import format_story_clock, story_local_time_context  # noqa: E402
from core.turn_engine import TurnEngine, should_supersede_request  # noqa: E402
from core.types import (  # noqa: E402
    DeliveryUnknown,
    NarrativeImage,
    NarrativeRequest,
    Story,
    empty_story_setting,
    empty_story_state,
    json_loads,
    now_utc,
    to_iso,
)

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")


def test_timeutil() -> None:
    ctx = story_local_time_context(datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc), "Asia/Shanghai")
    check("时区本地时间", ctx["local"].startswith("2026-10-15 16:00"), ctx["local"])
    check("时段 afternoon", ctx["period"] == "afternoon", str(ctx))
    check("GMT 偏移", ctx["offset"] == "GMT+8", str(ctx["offset"]))
    invalid = story_local_time_context(datetime(2026, 1, 1, tzinfo=timezone.utc), "Not/AZone")
    check("无效时区回退 UTC", invalid["timezone"] == "UTC", invalid["timezone"])


def test_group_willingness() -> None:
    config = {"enabled": True, "maxScore": 1, "threshold": 0.24, "probabilityAmplifier": 1.3,
              "decayHalfLifeSeconds": 180, "replyCost": 0.55, "baseGain": 0.12,
              "quoteGain": 0.12, "keywordGain": 0.18, "keywords": ["在吗"]}
    now = 1_000_000.0
    d1 = gw.evaluate_group_willingness(None, config, 1, False, False, "普通一句", now_seconds=now, roll=0.9)
    check("低于阈值不叫", d1["shouldCall"] is False and d1["reason"] == "below-threshold", str(d1))
    state = d1["state"]
    # 累积到超过阈值
    state = gw.evaluate_group_willingness(state, config, 3, False, False, "在吗在吗", now_seconds=now + 1, roll=0.0)["state"]
    d2 = gw.evaluate_group_willingness(state, config, 1, False, True, "说到你了", now_seconds=now + 2, roll=0.0)
    check("超过阈值且命中概率发言", d2["shouldCall"] is True, str(d2))
    d3 = gw.evaluate_group_willingness(state, config, 1, True, False, "hi", now_seconds=now + 2, roll=0.99)
    check("@ 直接通过", d3["reason"] == "forced-mention" and d3["shouldCall"], str(d3))
    consumed = gw.consume_group_willingness(state, config, now + 5)
    check("发言扣减成本", consumed["score"] < state["score"], str(consumed))
    # 睡眠态 ×0.2 与 @ 阻断
    sleep_state = gw.evaluate_willingness_gate(None, "auto", {"busy": "quiet", "idle": "active", "asleep": "quiet"},
                                               {"status": "asleep", "updatedAt": to_iso(now_utc())}, None,
                                               3, mentioned_bot=True, quoted_bot=False, content="x",
                                               now_seconds=now_utc().timestamp(), roll=0.99)
    check("睡眠态 @ 不直通", sleep_state["reason"] == "asleep", str(sleep_state))
    # 过期 lifeStatus 回退 idle
    stale = gw.resolve_willingness_layers("auto", {"busy": "quiet", "idle": "active", "asleep": "quiet"},
                                          {"status": "busy", "updatedAt": "1970-01-01T00:00:00Z"}, now, None)
    check("6 小时未刷新按 idle 档", stale["diagnosis"]["tier"] == "active", str(stale["diagnosis"]))


def test_alter() -> None:
    config = alter_mod.resolve_alter_system_config({"enabled": True, "baseThreshold": 3})
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    state = alter_mod.create_alter_system_state(now)
    advance = None
    for index in range(3):
        advance = alter_mod.advance_alter_system(state, 2, "user-message", now + timedelta(minutes=index), config, "p1")
        state = advance["state"]
    check("Alter 达到阈值", advance["thresholdReached"] is True, str(advance["triggerValue"]))
    completed = alter_mod.complete_alter_analysis(state, "氛围严肃了一点", advance["threshold"], now, config, "p1", advance["triggerValue"])
    offset = completed.get("emotionalOffset")
    check("侧端分析写入 offset", offset is not None and offset["direction"] == "serious", str(offset))
    prompt_view = alter_mod.emotional_offset_for_prompt(completed, config)
    check("offset 注入提示词", prompt_view is not None and prompt_view["weight"] == 1.0, str(prompt_view))
    # 反向衰减到清除（oppositeDecay=0.15/点 → 7 步后权重低于 minWeight）
    state2 = completed
    for index in range(7):
        state2 = alter_mod.advance_alter_system(state2, -1, "user-message", now + timedelta(minutes=10 + index), config, "p1")["state"]
    check("反向衰减清除 offset", state2.get("emotionalOffset") is None, str(state2.get("emotionalOffset")))


def test_script_engine() -> None:
    parsed = se.read_authored_actions('前面。<say id="a1">第一句话</say>中间。<say id="a1">重复</say>结尾。')
    check("重复 say 丢弃", len(parsed["actions"]) == 0, str(parsed["actions"]))
    parsed = se.read_authored_actions('她打字。<say id="s1">在忙，晚点说</say>然后放下手机。')
    check("say 解析数量", len(parsed["actions"]) == 1 and parsed["actions"][0]["content"] == "在忙，晚点说", str(parsed))
    # reconcile 接收的是含 say 标记的原始正文（与主叙事输出一致）
    decision = {"script": '她打字。<say id="s1">在忙，晚点说</say>然后放下手机。',
                "interaction": {"seen": True, "reply": {"mode": "immediate", "actionId": "s1", "content": "x"}}}
    reconciled = se.reconcile_transport_references(decision)
    check("say 与 transport 对齐", reconciled["interaction"]["reply"].get("content") == "在忙，晚点说",
          str(reconciled["interaction"]))
    check("reconcile 去标记", "<say" not in reconciled["script"], reconciled["script"])
    bad = {"script": "正文", "interaction": {"seen": True, "reply": {"mode": "immediate", "actionId": "nope", "content": "x"}}}
    reconciled_bad = se.reconcile_transport_references(bad)
    check("无效 actionId 降级 none", reconciled_bad["interaction"]["reply"]["mode"] == "none", str(reconciled_bad["interaction"]))
    bubbles = se.split_visible_reply_bubbles("一<sep/>二<sep/>三")
    check("分段拆分", bubbles == ["一", "二", "三"], str(bubbles))
    newline = se.split_visible_reply_bubbles("甲\n\n乙", newline_as_separator=True)
    check("换行分段（小模型适配）", newline == ["甲", "乙"], str(newline))
    runtime = {"messageSeparator": "<sep/>", "maxMessageCharacters": 1500, "splitReplyMessages": True,
               "minimumDelayedReplySeconds": 10, "maximumDelayedReplyMinutes": 1440}
    now = now_utc()
    immediate = se.normalize_interaction({"seen": True, "reply": {"mode": "immediate", "content": "在的"}}, now, runtime)
    check("immediate 归一", immediate["reply"]["mode"] == "immediate", str(immediate))
    too_soon = se.normalize_interaction(
        {"seen": True, "reply": {"mode": "delayed", "content": "晚点回", "sendAt": to_iso(now + timedelta(seconds=1))}},
        now, runtime,
    )
    check("延迟过短降级 none", too_soon["reply"]["mode"] == "none", str(too_soon))
    delayed = se.normalize_interaction(
        {"seen": True, "reply": {"mode": "delayed", "content": "晚点回", "sendAt": to_iso(now + timedelta(minutes=10))}},
        now, runtime,
    )
    check("延迟合法保留", delayed["reply"]["mode"] == "delayed" and delayed["reply"].get("sendAt"), str(delayed))
    # commit 构建 + 账本
    full_decision = {
        "script": "她想了想，还是回了。",
        "interaction": immediate,
        "intents": [{"type": "reminder", "summary": "晚点问他面试结果", "notBefore": to_iso(now + timedelta(hours=1)), "participantId": ""}],
        "crossConversationActions": [],
        "memories": [],
        "intentUpdates": [],
        "authoredActions": [],
    }
    commit = se.decision_to_script_commit("story:1", "user-message", now - timedelta(minutes=5), now, "p1", full_decision)
    kinds = [event["kind"] for event in commit["events"]]
    check("提交事件序列", kinds[:3] == ["message-perceived", "narrative", "outgoing-message"], str(kinds))
    actions = se.create_script_delivery_actions(commit)
    check("账本创建", len(actions) == 1 and actions[0]["segments"][0]["status"] == "pending", str(actions))
    reference = {"commitId": commit["commitId"], "eventId": actions[0]["eventId"], "segmentIndex": 0}
    updated = se.update_script_delivery_actions(actions, reference, "delivered", now)
    check("账本回写 delivered", updated is not None and updated[0]["status"] == "delivered", str(updated))
    frozen = se.update_script_delivery_actions(updated, reference, "failed", now)
    check("终态不可覆盖", frozen is None, str(frozen))


def test_schedule_preplan() -> None:
    regimes = [{
        "id": "work", "label": "工作周", "from": "2026-10-01",
        "weekly": {"thursday": [{"id": "shift", "start": "14:00", "end": "22:00", "label": "晚班", "kind": "fixed"}]},
    }]
    days = preplan.materialize_schedule_preplan(regimes, [], "2026-10-15", 7)
    day = next(item for item in days if item["date"] == "2026-10-15")
    check("周规律展开（周四）", day["blocks"] and day["blocks"][0]["label"] == "晚班", str(day))
    record = {
        "storyId": "s1", "revision": 1, "timezone": "Asia/Shanghai",
        "validFrom": "2026-10-15", "validThrough": "2026-10-28", "lastReviewedLocalDate": "",
        "lastEvidenceEntryId": 0, "reviewReason": "", "regimes": regimes, "exceptions": [],
        "materializedDays": days, "createdAt": now_utc(), "updatedAt": now_utc(),
    }
    window = preplan.schedule_preplan_window(record, datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc), "Asia/Shanghai")
    check("半日投影包含晚班", window and any(block["label"] == "晚班" for block in window["blocks"]), str(window))
    transition = preplan.next_schedule_preplan_transition(record, datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc), "Asia/Shanghai")
    check("固定日程锚点", transition is not None, str(transition))
    proposal = {"outcome": "patch", "reason": "新增周末团建", "sourceEntryIds": [7], "regimes": [{
        "id": "work", "label": "工作周", "from": "2026-10-01",
        "weekly": {"thursday": [{"id": "shift", "start": "14:00", "end": "22:00", "label": "晚班", "kind": "fixed"}],
                   "saturday": [{"id": "outing", "start": "10:00", "end": "16:00", "label": "团建", "kind": "routine"}]},
    }]}
    evidence = [type("E", (), {"id": 7})()]
    updated = preplan.apply_schedule_preplan_proposal(record, proposal, evidence, "2026-10-15", "Asia/Shanghai",
                                                      preplan.resolve_schedule_preplan_config({}), now_utc())
    check("提案合并并升级 revision", updated and updated["revision"] == 2, str(updated and updated.get("revision")))


def test_agency() -> None:
    config = agency_mod.resolve_agency_config({"enabled": True, "contactMode": "natural"})
    check("contactMode 归一", config["contactMode"] == "natural", str(config))
    window = {
        "activityLoad": "free", "privacy": "private", "deviceAccess": "available",
        "validUntil": to_iso(now_utc() + timedelta(hours=1)), "updatedAt": to_iso(now_utc()),
        "basis": "在家中独处", "sourceEntryIds": [1],
    }
    candidate = {"participantId": "p1", "origin": "life-event", "disclosure": "personal", "sourceEntryIds": [1]}
    now = now_utc()
    capacity = agency_mod.evaluate_agency_capacity(window, candidate, now, config, to_iso(now - timedelta(minutes=90)))
    check("容量门放行", capacity["allowed"] is True, str(capacity))
    too_soon = agency_mod.evaluate_agency_capacity(window, candidate, now, config, to_iso(now - timedelta(minutes=10)))
    check("最小间隔拦截", too_soon["allowed"] is False and too_soon["reason"] == "minimum-proactive-interval", str(too_soon))
    promise = dict(candidate, origin="promise")
    bypass = agency_mod.evaluate_agency_capacity(window, promise, now, config, to_iso(now - timedelta(minutes=10)))
    check("承诺型绕过间隔", bypass["allowed"] is True, str(bypass))


async def test_turn_engine() -> None:
    flushed: list[tuple[str, int]] = []
    engine = TurnEngine(
        user_message_debounce_seconds=lambda: 0.05,
        schedule_flush=lambda key, revision: flushed.append((key, revision)),
        report=lambda *args, **kwargs: None,
    )
    engine.buffer_user_narrative("s1", "p1", {"content": "在吗", "occurredAt": to_iso(now_utc())})
    await asyncio.sleep(0.15)
    check("合并计时器触发", len(flushed) == 1, str(flushed))
    key, revision = flushed[0]
    turn = engine.begin_flush(key, revision)
    check("开始刷写", turn is not None and turn.messages, str(turn))
    # 在途请求 + 首条未提交 → 新消息使旧请求作废
    turn.inFlightRequestId = revision
    engine.buffer_user_narrative("s1", "p1", {"content": "还在吗", "occurredAt": to_iso(now_utc())})
    check("旧请求作废", revision in turn.obsoleteRequestIds, str(turn.obsoleteRequestIds))
    check("作废判定函数", should_supersede_request(revision, None, turn.obsoleteRequestIds) is False, "already obsolete")
    engine.end_flush(key, revision)
    check("narrating 释放", engine.is_narrating("s1") is False, "still narrating")


class StubTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.faces: list[tuple[str, str]] = []
        self.media: list[tuple[str, str, str]] = []
        self.reactions: list[tuple[str, str, str]] = []

    async def send(self, umo: str, text: str, reply_to: str = "") -> bool:
        self.sent.append((umo, text, reply_to))
        return True

    async def send_face(self, umo: str, face_id: str) -> bool:
        self.faces.append((umo, face_id))
        return True

    async def send_media(self, umo: str, path: str, kind: str = "image") -> bool:
        self.media.append((umo, path, kind))
        return True

    async def send_reaction(self, umo: str, message_id: str, emoji_id: str) -> bool:
        self.reactions.append((umo, message_id, emoji_id))
        return True


def build_config() -> dict:
    return {
        "storyDefaults": {
            "characterName": "林知遥",
            "characterProfile": "夜班花店店员，作息不规律。",
            "perspective": "把安静当成一种能力。",
            "relationship": "大学旧友",
            "world": "当代上海，现实世界。",
            "supportingCast": "周宁：同事。",
            "location": "上海静安区",
            "style": "现实主义日常叙事。",
            "timezone": "Asia/Shanghai",
        },
        "model": {
            "providers": [],
            "mainTemperature": 0.8,
            "mainMaxTokens": 2048,
            "mainTimeout": 60000,
            "mainResponseFormat": "prompt-only",
            "failover": {"enabled": True, "strategy": "priority", "maxAttemptsPerProvider": 1, "cooldownMinutes": 5},
            "compaction": {"enabled": False},
        },
        "onebot": {"enabled": True, "userAccounts": [{"qq": "10001", "label": "小林", "personId": "lin", "relationships": "", "enabled": True}],
                   "botAccounts": [{"qq": "90001", "enabled": True}], "groupChats": [], "ignoreSelfMessages": True},
        "sharedStory": {"autoEnrollParticipants": True, "allowCrossConversationMessages": True,
                        "shareParticipantDetails": False, "maxCrossConversationActions": 1,
                        "participantContextLimit": 6, "managerAccounts": ["10001"]},
        "runtime": {
            "captureDirectMessages": True, "autoCreate": False, "ignoreCommandMessages": True,
            "userMessageDebounceSeconds": 0.05, "splitReplyMessages": True, "messageSeparator": "<sep/>",
            "typingBaseDelaySeconds": 0, "typingCharactersPerSecond": 100, "typingMaxDelaySeconds": 0.3,
            "typingJitterRatio": 0.3, "autoAdvanceEnabled": True, "autoAdvanceIntervalMinutes": 40,
            "autoAdvanceJitterMinutes": 5, "conversationFollowUpMinutes": [10, 20],
            "conversationFollowUpJitterMinutes": 1, "sweepIntervalMinutes": 5,
            "contextEntryLimit": 17, "contextTimeWindowMinutes": 6, "memoryLimit": 20,
            "maxMessageCharacters": 1500, "maxScriptCharacters": 1400,
            "minimumDelayedReplySeconds": 10, "maximumDelayedReplyMinutes": 1440,
            "allowProactiveMessages": True, "proactiveWillingnessThreshold": 0.65,
            "narrativeRetryDelaySeconds": 15, "narrativeRetryMaxAttempts": 0,
        },
        "memory": {"enabled": True, "sceneEntryThreshold": 16, "sceneCharacterThreshold": 10000},
        "alterSystem": {"enabled": True, "baseThreshold": 3},
        "agency": {"enabled": False},
        "schedulePreplan": {"enabled": True, "horizonDays": 14},
        "blindMode": {"enabled": False},
        "presetSync": {"enabled": False},
        "weather": {"enabled": False},
        "worldSeeder": {"enabled": False},
    }


async def test_rc56_alignment() -> None:
    """rc54 / rc56 对齐项：默认文风与分享分区白名单。"""
    check("默认文风为完整中文指引", "你正在持续创作一部以主角为中心的现实主义生活剧本" in DEFAULT_STYLE_PROMPT, DEFAULT_STYLE_PROMPT[:60])
    check("默认文风覆盖生活质感段落", "让剧本从这些真实而具体的生活质感中展开" in DEFAULT_STYLE_PROMPT, "")
    now = now_utc()
    story = Story(
        id="style-story", platform="onebot", selfId="1", userId="2", channelId="c",
        status="active",
        setting={**empty_story_setting(), "style": ""},
        state=empty_story_state(),
        cursorAt=now, createdAt=now, updatedAt=now,
    )
    request = NarrativeRequest(phase="user-message", story=story, from_at=now, now=now)
    prompt = build_system_prompt(request, "", "", "", "", False, False, "off")
    check("空文风回落默认指引", DEFAULT_STYLE_PROMPT in prompt, prompt[-400:])
    overridden = build_system_prompt(request, "自定义文风：冷静克制。", "", "", "", False, False, "off")
    check("显式文风覆盖默认", "自定义文风：冷静克制。" in overridden and DEFAULT_STYLE_PROMPT not in overridden, overridden[-200:])
    check("分享分区包含 weather", "weather" in SHARE_SECTIONS, str(SHARE_SECTIONS))
    check("分享分区 18 项", len(SHARE_SECTIONS) == 18, str(len(SHARE_SECTIONS)))


async def test_rc57_alignment() -> None:
    """rc57 对齐：默认值下调、投影压缩、单场景 Overlay 演化。"""
    now = now_utc()
    check("故事时钟 HH:mm", format_story_clock(datetime(2026, 10, 17, 8, 30, tzinfo=timezone.utc), "Asia/Shanghai") == "16:30", "")
    check("空时区时钟为空", format_story_clock(now, "") == "", "")

    # 组回合提示词：群发言与私聊通道分流（rc57）
    group_transport = transport_instruction("user-message", True)
    check("群回合保留 groupReply", "groupReply" in group_transport, group_transport[-300:])
    check("群回合提示私聊 crossConversationActions", "crossConversationActions" in group_transport, group_transport[-300:])
    check("群回合私聊知识说明", "private conversations" in phase_instruction("user-message", True), "")

    # rc57 默认上限：script 1400 / 可见消息 1500
    normalized = se.normalize_decision(
        {
            "script": "x" * 1800,
            "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "y" * 1800}},
        },
        now - timedelta(minutes=1),
        now,
        True,
        {},
        {},
        "p1",
        {"p1"},
        set(),
        phase="user-message",
    )
    check("rc57 剧本默认上限 1400", len(normalized["script"]) == 1400, str(len(normalized["script"])))
    check(
        "rc57 可见消息默认上限 1500",
        len(normalized["interaction"]["reply"]["content"]) == 1500,
        str(len(normalized["interaction"]["reply"]["content"])),
    )

    # rc57 投递现实投影：head 截断 + 回执时钟
    entry = SimpleNamespace(
        kind="script",
        id=7,
        metadata={
            "commitId": "commit-1",
            "deliveryActions": [{
                "commitId": "commit-1",
                "eventId": "event-1",
                "eventKind": "outgoing-message",
                "participantId": "p1",
                "segments": [{
                    "index": 0,
                    "kind": "message",
                    "content": "这是一条超过十二个字符的消息内容",
                    "status": "pending",
                    "attemptedAt": to_iso(now),
                }],
            }],
        },
    )
    projected = se.delivery_reality_projection([entry], "p1", False, timezone_name="Asia/Shanghai")
    segment = projected[0]["segments"][0]
    check("rc57 投影 head 截断", segment["head"].endswith("…") and len(segment["head"]) == 13, str(segment))
    check("rc57 投影不携带全文", "content" not in segment, str(segment))
    check("rc57 投影回执时钟", segment.get("at") == format_story_clock(now, "Asia/Shanghai"), str(segment))
    check("rc57 投影 eventKind", projected[0].get("eventKind") == "outgoing-message", str(projected))

    # rc57 Issue #8：单场景、多独立回合也允许普通设定演化
    tmp = tempfile.mkdtemp(prefix="hdsi-rc57-")
    try:
        service = InterludeService(
            config_getter=lambda: build_config(),
            data_dir=tmp,
            logger=lambda level, message: None,
            transport=StubTransport(),
        )
        session = SessionView(
            platform="onebot", selfId="90001", userId="10001", channelId="onebot:FriendMessage:10001",
            isDirect=True, content="在吗", senderName="小林", umo="onebot:FriendMessage:10001",
        )
        story = await service.create_story(session)
        scene = await service.active_scene(story.id)
        entry_ids = []
        for offset in (2, 1, 0):
            entry_ids.append(await service.append_entry(story.id, {
                "kind": "script",
                "actor": "protagonist",
                "participantId": "",
                "content": f"第 {offset} 天的独立回合",
                "occurredAt": to_iso(now - timedelta(days=offset)),
                "metadata": {"sceneId": scene.id if scene else None},
            }, now))
        patch_id = service.db.insert_row("interlude_state_patch", {
            "storyId": story.id, "participantId": "", "target": "character",
            "path": "characterTraits", "proposedValue": "更谨慎",
            "evidence": "连续三个独立回合", "confidence": 0.9, "impact": "minor",
            "status": "proposed", "sourceEntryIds": entry_ids,
            "createdAt": now, "appliedAt": None,
        })
        await service._maintain_overlays(story, now)
        applied = service.db.first_row("interlude_state_patch", {"id": patch_id})
        check("rc57 单场景多回合可演化", applied is not None and applied.get("status") == "applied", str(applied))
        fresh_story = await service.get_story(story.id)
        traits = ((fresh_story.state or {}).get("settingOverlay") or {}).get("characterTraits") or []
        check("rc57 演化写入 overlay", "更谨慎" in traits, str(traits))

        single_id = await service.append_entry(story.id, {
            "kind": "script",
            "actor": "protagonist",
            "participantId": "",
            "content": "只有一个回合",
            "occurredAt": to_iso(now),
            "metadata": {"sceneId": scene.id if scene else None},
        }, now)
        patch_two = service.db.insert_row("interlude_state_patch", {
            "storyId": story.id, "participantId": "", "target": "perspective",
            "path": "outlook", "proposedValue": "更悲观",
            "evidence": "只有一个回合", "confidence": 0.9, "impact": "minor",
            "status": "proposed", "sourceEntryIds": [single_id],
            "createdAt": now, "appliedAt": None,
        })
        await service._maintain_overlays(story, now)
        blocked = service.db.first_row("interlude_state_patch", {"id": patch_two})
        check("rc57 单回合不演化", blocked is not None and blocked.get("status") == "proposed", str(blocked))

        # 重大变化仍要求两个独立场景：同一场景的 3 个回合不足以放行 major
        patch_major = service.db.insert_row("interlude_state_patch", {
            "storyId": story.id, "participantId": "", "target": "world",
            "path": "era", "proposedValue": "时代改变",
            "evidence": "同一场景的三个回合", "confidence": 0.99, "impact": "major",
            "status": "proposed", "sourceEntryIds": entry_ids,
            "createdAt": now, "appliedAt": None,
        })
        await service._maintain_overlays(story, now)
        major_row = service.db.first_row("interlude_state_patch", {"id": patch_major})
        check("rc57 单场景不放行 major", major_row is not None and major_row.get("status") == "proposed", str(major_row))
        service.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class UnknownTransport:
    """模拟回执超时：请求已写出但结果不可知。"""

    async def send(self, umo: str, text: str, reply_to: str = "") -> bool:
        raise DeliveryUnknown("transport-timeout: 回执超时")


async def test_delivery_unknown() -> None:
    """rc53 / rc56 投递语义：结果未知记 pending，且不自动重试。"""
    tmp = tempfile.mkdtemp(prefix="hdsi-unknown-")
    try:
        config = build_config()
        service = InterludeService(
            config_getter=lambda: config,
            data_dir=tmp,
            logger=lambda level, message: None,
            transport=UnknownTransport(),
        )
        session = SessionView(
            platform="onebot", selfId="90001", userId="10001", channelId="onebot:FriendMessage:10001",
            isDirect=True, content="在吗", senderName="小林", umo="onebot:FriendMessage:10001",
        )
        story = await service.create_story(session)
        participant = await service.find_participant(session, story)
        now = now_utc()
        decision = {
            "script": "她看了一眼手机。",
            "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "在的"}},
            "authoredActions": [],
            "intents": [],
            "intentUpdates": [],
            "crossConversationActions": [],
            "memories": [],
            "alter": None,
            "agencyWindow": None,
            "proactiveContact": None,
            "statePatch": None,
            "localMedia": None,
            "nativeFace": None,
            "messageReactions": None,
            "droppedProactiveActions": [],
            "followUpResolutions": [],
        }
        persisted = await service._persist_turn(story.id, participant.id, decision, now, now, "user-message")
        delivery = await service._deliver_messages_for_participant(story, participant, persisted["messages"], 0.0)
        check("unknown 不计入已投递", delivery == [], str(delivery))
        entries = await service.recent_entries(story.id, 10)
        script_row = next(entry for entry in entries if entry.kind == "script")
        actions = (script_row.metadata or {}).get("deliveryActions") or []
        statuses = [segment["status"] for action in actions for segment in action.get("segments", [])]
        check("unknown 账本记 pending", statuses == ["pending"], str(statuses))
        retry = service.db.first_row("interlude_intent", {"storyId": story.id, "type": "delayed-message"})
        check("unknown 不排补偿重试", retry is None, str(retry))
        check("unknown 状态直读", await service._transport_send("umo", "你好") == "unknown", "")
        service.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_service_end_to_end() -> None:
    tmp = tempfile.mkdtemp(prefix="hdsi-test-")
    try:
        config = build_config()
        transport = StubTransport()
        logs: list[str] = []
        service = InterludeService(
            config_getter=lambda: config,
            data_dir=tmp,
            logger=lambda level, message: logs.append(f"{level}:{message}"),
            transport=transport,
        )

        calls = {"count": 0}

        async def fake_decide(request, cfg):
            calls["count"] += 1
            decision = {
                "script": "她看了一眼手机，把刚洗好的花枝放下，才回了两个字。",
                "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "在的<sep/>怎么了"}},
                "authoredActions": [],
                "intents": [],
                "intentUpdates": [],
                "crossConversationActions": [],
                "memories": [{"category": "scene", "content": "小林晚上来问她在不在。", "importance": 0.4}],
                "alter": -1,
                "agencyWindow": None,
                "proactiveContact": None,
                "statePatch": None,
                "localMedia": None,
                "nativeFace": None,
                "messageReactions": None,
                "droppedProactiveActions": [],
                "followUpResolutions": [],
            }
            return NarrativeResult(succeeded=True, decision=decision, providerLabel="stub")

        service.narrator.decide = fake_decide  # type: ignore[assignment]

        session = SessionView(
            platform="onebot", selfId="90001", userId="10001", channelId="onebot:FriendMessage:10001",
            isDirect=True, content="在吗", senderName="小林", umo="onebot:FriendMessage:10001",
        )
        story = await service.create_story(session)
        check("故事创建", story is not None and story.setting["character"]["name"] == "林知遥", str(story and story.id))
        participant = await service.find_participant(session, story)
        check("参与者创建", participant is not None and participant.userId == "10001", str(participant))

        consumed = await service.receive(session)
        check("私聊接收消费", consumed is True, str(consumed))
        await asyncio.sleep(1.2)  # 等合并计时器 + 写作回合 + 分段投递完成
        check("模型被调用", calls["count"] >= 1, str(calls))
        sent = [item[1] for item in transport.sent]
        check("回复已投递（分段）", sent[:2] == ["在的", "怎么了"], str(sent))
        entries = await service.recent_entries(story.id, 10)
        kinds = [entry.kind for entry in entries]
        check("剧本条目落库", "user-message" in kinds and "script" in kinds, str(kinds))
        # 投递账本应已回写 delivered
        script_row = next(row for row in entries if row.kind == "script")
        actions = (script_row.metadata or {}).get("deliveryActions") or []
        statuses = [segment["status"] for action in actions for segment in action.get("segments", [])]
        check("账本 delivered", statuses and all(status == "delivered" for status in statuses), str(statuses))
        # 事实/记忆
        memories = await service.memories(story.id, 10)
        check("记忆落库", any("小林" in memory.content for memory in memories), str(memories))
        # 去重
        consumed_again = await service.receive(session)
        check("重复入站去重", consumed_again is True and calls["count"] == 1, str(calls))
        # 手动推进（存根模型）
        await service.advance_story(story, force=True)
        check("自动推进调用模型", calls["count"] >= 2, str(calls))
        # 管理员操作
        ok_fact = await service.add_admin_fact(story, "promise", "主角答应周三告诉小林面试结果。")
        check("人工事实入库", ok_fact, "")
        facts = await service.admin_facts(story.id, 10)
        check("事实可查询", any("面试结果" in fact.content for fact in facts), str(facts))
        await service.add_admin_script_note(story, "主角今晚临时换班。")
        entries = await service.recent_entries(story.id, 10)
        check("管理员注记带来源标记", any(entry.kind == "admin-note" and "管理员注记" in entry.content for entry in entries), str([e.kind for e in entries]))
        # 清库（上游语义：HDSI 自有表全部清空，故事行也随之消失，下一条消息会按档案重建）
        result = await service.clear_database()
        check("清库返回删除数", result["removed"] > 0, str(result))
        story_after = await service.get_canonical_story()
        check("清库后故事行被移除", story_after is None, str(story_after))
        recreated = await service.create_story(session)
        check("清库后可按档案重建", recreated is not None and recreated.setting["character"]["name"] == "林知遥", str(recreated and recreated.id))
        service.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_urge_alignment() -> None:
    """Urge 算法：区间接线、上膛/确认/爆发、引用校验。"""
    cfg = urge_mod.resolve_urge_config({"enabled": True, "frequency": "medium"})
    check("urge 预设 medium", cfg["idle"] == [35, 55] and cfg["burst"] == [3, 7], str(cfg))
    custom = urge_mod.resolve_urge_config({"frequency": "custom", "advanced": {"idleMin": 100, "idleMax": 50}})
    check("urge 区间下限不超过上限", custom["idle"] == [100, 100], str(custom["idle"]))
    now = 1_000_000.0
    state = urge_mod.normalize_urge_state(
        {"version": 1, "buckets": [now - 60],
         "armed": {"participantId": "p1", "entryId": 3, "at": now - 60}}, now
    )
    check("urge 状态恢复上膛", state["armed"] is not None, str(state))
    reset = urge_mod.urge_user_event(state, now)
    check("用户消息清空上膛", reset["armed"] is None and reset["buckets"][-1] >= now - 120, str(reset))
    ignored = urge_mod.commit_urge(
        state, {"value": 0.9, "basisQuote": "不存在的句子"}, "正文", 5, "p1", now, cfg, random_fn=lambda: 0.5
    )
    check("urge 引用不命中不采纳", ignored.get("armed") is None and ignored.get("value") is None, str(ignored))
    committed = urge_mod.commit_urge(
        state, {"value": 0.9, "basisQuote": "看了一眼手机", "pace": "normal"},
        "她看了一眼手机。", 5, "p1", now, cfg, random_fn=lambda: 0.5
    )
    check("urge 高值上膛", (committed.get("armed") or {}).get("participantId") == "p1", str(committed))
    acked = urge_mod.acknowledge_urge(committed, "p1", 5, now + 10)
    check("urge 确认后进入 burst", (acked.get("burst") or {}).get("used") == 0, str(acked))
    plan = urge_mod.plan_urge(
        acked, now + 10, cfg, 0, False, random_fn=lambda: 0.5,
        rhythm={"attention": "phone-focused", "stale": False},
    )
    check("urge burst 计划", plan["reason"] == "confirmed-contact-burst" and plan["minutes"] <= cfg["ttl"], str(plan))
    slow = urge_mod.plan_urge({**state, "pace": "slow", "suggested": 120}, now, cfg, 0, False, random_fn=lambda: 0.5)
    check("urge slow 尊重建议间隔", slow["reason"] == "script-slow" and slow["minutes"] >= cfg["slow"][0], str(slow))


def test_type0_share_codes() -> None:
    """Type0 分享码/链接解析（内置导入入口）。"""
    from core.share_sync import extract_share_code

    check("裸分享码", extract_share_code(" abcd-1234 ") == "ABCD-1234", "")
    check(
        "typ0 链接",
        extract_share_code("https://typ0.space/p/ABCD-1234") == "ABCD-1234",
        "",
    )
    check(
        "查询参数链接",
        extract_share_code("https://typ0.space/?code=WXYZ-99") == "WXYZ-99",
        "",
    )
    check("无效链接", extract_share_code("https://typ0.space/about") == "", "")


def test_browser_helpers() -> None:
    """网页观察的纯解析逻辑（正文提取 / 搜索结果解析 / 提示词）。"""
    from core.narrator import browser_instruction
    from core.service import first_search_result, parse_html_page

    page = parse_html_page(
        "<html><head><title>示例标题</title><script>bad()</script></head>"
        "<body><p>第一段</p><p>第二段</p></body></html>"
    )
    check("网页标题提取", page["title"] == "示例标题", page["title"])
    check("网页正文提取", "第一段" in page["text"] and "第二段" in page["text"], page["text"])
    check("网页脚本剔除", "bad()" not in page["text"], page["text"])
    link = first_search_result(
        '<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">x</a>',
        "https://html.duckduckgo.com/html/?q=x",
    )
    check("搜索结果解包", link == "https://example.com/a", link)
    check("网页观察提示词", "WEB OBSERVATION" in browser_instruction(True), "")
    check("群回合不注入网页意图", browser_instruction(True, True) == "", "")


def test_schema_defaults() -> None:
    """schema 与代码默认值单一来源（防止 rc54 的双源漂移复发）。"""
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_conf_schema.json"), encoding="utf-8") as handle:
        schema = json.load(handle)
    runtime = schema["runtime"]["items"]
    removed_paths = [
        ("storyDefaults",),
        ("mainPromptOverride",),
        ("model", "items", "mainPrompt"),
        ("model", "items", "formatPrompt"),
        ("model", "items", "fixedPrompt"),
        ("model", "items", "stylePrompt"),
        ("model", "items", "compaction", "items", "mainPrompt"),
        ("model", "items", "compaction", "items", "fixedPrompt"),
        ("model", "items", "compaction", "items", "stylePrompt"),
        ("alterSystem", "items", "prompt"),
    ]

    def has_path(node, path):
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return False
            node = node[key]
        return True

    check("配置页已移除全部内容字段", not any(has_path(schema, path) for path in removed_paths), "")
    check("配置页无 WebUI 措辞", "WebUI" not in json.dumps(schema, ensure_ascii=False), "")
    check("内置默认文风仍可用", bool(DEFAULT_STYLE_PROMPT.strip()), "")
    check("schema contextEntryLimit=17", runtime["contextEntryLimit"]["default"] == 17, str(runtime["contextEntryLimit"]["default"]))
    check("schema contextTimeWindowMinutes=6", runtime["contextTimeWindowMinutes"]["default"] == 6, "")
    check("schema maxScriptCharacters=1400", runtime["maxScriptCharacters"]["default"] == 1400, "")
    check("schema maxMessageCharacters=1500", runtime["maxMessageCharacters"]["default"] == 1500, "")
    check("schema urge advanced 齐全", len(schema["urge"]["items"]["advanced"]["items"]) == 17, "")
    check("schema 预设置顶", next(iter(schema)) == "presetSync", str(next(iter(schema))))


def test_metadata_update_contract() -> None:
    """metadata 必须满足 AstrBot 更新检查约定（repo / 版本 / 作者 / 市场源）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "metadata.yaml"), encoding="utf-8") as handle:
        text = handle.read()

    def field(name: str) -> str:
        match = re.search(rf"^{name}:\s*(.+)$", text, re.MULTILINE)
        return match.group(1).strip().strip('"').strip("'") if match else ""

    name = field("name")
    version = field("version")
    repo = field("repo")
    author = field("author")
    check("metadata 名称与目录一致", name == "astrbot_plugin_hds_interlude", name)
    check(
        "metadata 仓库指向本插件 GitHub",
        repo == "https://github.com/Bronsan/astrbot_plugin_hds_interlude",
        repo,
    )
    check("metadata 版本为语义化（无 v 前缀）", bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), version)
    check("metadata 作者用于市场标识", author == "Bronsan", author)
    check("metadata 声明 astrbot_version", bool(field("astrbot_version")), field("astrbot_version"))

    with open(os.path.join(root, "plugin_registry.json"), encoding="utf-8") as handle:
        registry = json.load(handle)
    entry = registry.get("Bronsan/astrbot_plugin_hds_interlude") or {}
    check("自托管市场源条目存在", entry.get("name") == name, str(entry)[:120])
    check(
        "自托管市场源版本与 metadata 一致",
        entry.get("version") == version,
        f"{entry.get('version')} != {version}",
    )
    check("自托管市场源 repo 与 metadata 一致", entry.get("repo") == repo, str(entry.get("repo")))

    with open(os.path.join(root, "plugin_registry-md5.json"), encoding="utf-8") as handle:
        md5_payload = json.load(handle)
    with open(os.path.join(root, "plugin_registry.json"), "rb") as handle:
        expected_md5 = hashlib.md5(handle.read()).hexdigest()
    check("自托管市场源 MD5 与文件一致", md5_payload.get("md5") == expected_md5, str(md5_payload))


async def test_wiring_fixes() -> None:
    """本轮修复：提示词覆盖、群缓冲隔离、端点路由、动作投递、时间导演、扫描上限。"""
    tmp = tempfile.mkdtemp()
    try:
        config = build_config()
        config["mainPromptOverride"] = "覆盖提示词：保持克制，不写戏剧化转折。"
        config["urge"] = {"enabled": False}
        config["timelineDirector"] = {"enabled": True}
        config["chatActions"] = {
            "enabled": True, "nativeFaces": True, "messageReactions": True,
            "allowedNativeFaces": ["smile", "laugh", "sweat", "awkward"],
        }
        config["stickers"] = {"enabled": True, "directory": "stickers", "maxFileSizeMB": 1, "catalogLimit": 10}
        config["logging"] = {"level": "debug", "logScriptPreview": False, "logMessageContent": False, "previewLength": 80}
        config["browser"] = {
            "enabled": True, "mode": "deferred-only", "allowSearch": True, "allowVisit": True,
            "searchUrlTemplate": "https://html.duckduckgo.com/html/?q={query}",
            "allowedDomains": [], "blockedDomains": [],
            "maxTextCharacters": 4000, "maxExcerptCharacters": 1000, "cacheMinutes": 30,
        }
        config["onebot"]["groupChats"] = [
            {"groupId": "g1", "label": "一群", "enabled": True, "responseMode": "always",
             "debounceSeconds": 0.02, "willingnessPreset": "off", "contextLimit": 10},
            {"groupId": "g2", "label": "二群", "enabled": True, "responseMode": "always",
             "debounceSeconds": 0.02, "willingnessPreset": "off", "contextLimit": 10},
        ]
        os.makedirs(os.path.join(tmp, "stickers", "happy"), exist_ok=True)
        with open(os.path.join(tmp, "stickers", "happy", "smile.png"), "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        transport = StubTransport()
        service = InterludeService(
            config_getter=lambda: config, data_dir=tmp, logger=lambda level, message: None, transport=transport
        )
        session = SessionView(
            platform="onebot", selfId="90001", userId="10001", channelId="umo1",
            isDirect=True, content="在吗", senderName="小林", umo="umo1", messageId="m-100",
        )
        story = await service.create_story(session)
        participant = await service.find_participant(session, story)

        # 1) mainPromptOverride：进入系统提示词且优先于 model.mainPrompt
        captured: dict = {}

        async def fake_call(kind, messages, *args, **kwargs):
            captured["system"] = messages[0]["content"]
            return json.dumps(
                {"script": "她抬头看了一会儿窗外。", "interaction": {"seen": True, "reply": {"mode": "none"}}},
                ensure_ascii=False,
            ), None, "stub"

        service.narrator._call_with_failover = fake_call
        now = now_utc()
        request = await service._build_request(story, participant, "user-message", now, now)
        result = await service.narrator.decide(request, config)
        check("mainPromptOverride 进入提示词", "覆盖提示词" in captured.get("system", ""), captured.get("system", "")[-200:])
        check("覆盖后仍可解析决策", result.succeeded, str(result.error))

        # native 视觉：图片作为 image_url 部分进入主模型（而不是只报 imageCount）
        vision_capture: dict = {}

        async def fake_vision_call(kind, messages, *args, **kwargs):
            vision_capture["messages"] = messages
            return json.dumps(
                {"script": "她看了一眼照片。", "interaction": {"seen": True, "reply": {"mode": "none"}}},
                ensure_ascii=False,
            ), None, "stub"

        service.narrator._call_with_failover = fake_vision_call
        vision_config = json.loads(json.dumps(config))
        vision_config["model"]["vision"] = {
            "enabled": True, "mode": "native", "detail": "low", "maxImageDimension": 40,
        }
        vision_request = await service._build_request(story, participant, "user-message", now, now)
        vision_request.images = [
            NarrativeImage(id="i1", mimeType="image/png", dataUri="data:image/png;base64,AAAA")
        ]
        await service.narrator.decide(vision_request, vision_config)
        vision_content = vision_capture["messages"][1]["content"]
        check(
            "native 视觉送入主模型",
            isinstance(vision_content, list)
            and any(
                part.get("type") == "image_url" and part.get("image_url", {}).get("detail") == "low"
                for part in vision_content
            ),
            str(vision_content)[:240],
        )

        # 2) 群缓冲按 (故事, 群) 隔离；群消息 unknown → pending（rc56 typ-0 等价语义）
        seen_groups: list[str] = []

        class GroupUnknownTransport(StubTransport):
            async def send(self, umo: str, text: str, reply_to: str = "") -> bool:
                if umo.startswith("gumo"):
                    raise DeliveryUnknown("transport-timeout: 回执超时")
                return await super().send(umo, text, reply_to)

        group_transport = GroupUnknownTransport()
        service._transport = group_transport

        async def fake_group_run(story_obj, request_obj, group_turn=False):
            seen_groups.append(request_obj.groupContext["groupId"])
            messages = request_obj.groupContext.get("messages") or []
            reaction = []
            if messages and messages[-1].get("messageId"):
                reaction = [{"messageRef": messages[-1]["messageRef"], "reaction": "like"}]
            return {
                "success": True,
                "decision": {
                    "script": "她看了一眼群聊。",
                    "groupReply": "我在。",
                    "messageReactions": reaction,
                },
                "error": "",
            }

        service._run_narrative = fake_group_run
        for group_id, umo in (("g1", "gumo1"), ("g2", "gumo2")):
            await service.receive_group(SessionView(
                platform="onebot", selfId="90001", userId="10001", channelId=umo,
                isDirect=False, content="群里说话", senderName="小林", umo=umo,
                groupId=group_id, atBot=True, messageId=f"gmsg-{group_id}",
            ))
        await asyncio.sleep(0.8)
        check("群缓冲按群隔离", sorted(seen_groups) == ["g1", "g2"], str(seen_groups))
        group_row = service.db.first_row(
            "interlude_script_entry", {"kind": "character-group-message"}, order_by="id DESC"
        )
        group_meta = json_loads(group_row.get("metadata"), {}) if group_row else {}
        check("群消息 unknown 记 pending", group_meta.get("deliveryStatus") == "pending", str(group_meta))
        group_script_row = service.db.first_row(
            "interlude_script_entry", {"kind": "script"}, order_by="id DESC"
        )
        group_script_meta = json_loads(group_script_row.get("metadata"), {}) if group_script_row else {}
        group_actions = group_script_meta.get("deliveryActions") or []
        group_segments = [segment for action in group_actions for segment in action.get("segments", [])]
        check(
            "群回应投递 delivered",
            bool(group_transport.reactions)
            and any(segment.get("kind") == "message-reaction" and segment.get("status") == "delivered" for segment in group_segments),
            str(group_segments),
        )
        check(
            "群消息 pending 带原因",
            str(group_meta.get("deliveryReason") or "").startswith("transport-timeout"),
            str(group_meta),
        )
        service._transport = transport

        async def fake_private_run(story_obj, request_obj, group_turn=False):
            return {
                "success": True,
                "decision": {
                    "script": "她回了消息。",
                    "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "在的"}},
                },
                "error": "",
            }

        service._run_narrative = fake_private_run

        # 3) 端点登记与切换
        await service.receive(session)
        await asyncio.sleep(0.6)
        fresh = await service.get_participant(participant.id)
        check("端点登记首条会话", service._delivery_umo(fresh) == "umo1", service._delivery_umo(fresh))
        check(
            "replyTo 透传到投递层",
            any(item[2] == "m-100" for item in transport.sent),
            str(transport.sent[-3:]),
        )
        session_two = SessionView(
            platform="onebot", selfId="90001", userId="10001", channelId="umo2",
            isDirect=True, content="还在吗", senderName="小林", umo="umo2",
        )
        await service.receive(session_two)
        await asyncio.sleep(0.6)
        fresh = await service.get_participant(participant.id)
        check("端点切换到最近会话", service._delivery_umo(fresh) == "umo2", service._delivery_umo(fresh))
        check("同一用户只保留一个端点", len(service.list_participant_endpoints(participant.id)) == 1, str(service.list_participant_endpoints(participant.id)))

        # 4) 平台动作投递：原生表情 / 表情包 / 回应
        decision = {
            "script": "她把手机翻过来，笑了一下。",
            "interaction": {"seen": True, "reply": {"mode": "none"}},
            "localMedia": {"assetId": "happy/smile", "intent": "发个笑脸"},
            "nativeFace": {"semantic": "smile", "intent": "轻轻笑"},
            "messageReactions": [{"messageRef": "m1", "reaction": "like"}],
        }
        stamp = now_utc()
        persisted = await service._persist_turn(story.id, participant.id, decision, stamp, stamp, "user-message")
        check("平台动作单独成账", len(persisted["platformActions"]) == 1, str(persisted["platformActions"]))
        await service._deliver_platform_actions(story, participant, persisted["platformActions"], stamp)
        row = service.db.first_row("interlude_script_entry", {"id": persisted["scriptEntryId"]})
        segments = [
            segment
            for action in (json_loads(row["metadata"], {}).get("deliveryActions") or [])
            for segment in action.get("segments", [])
        ]
        statuses = {segment["kind"]: segment["status"] for segment in segments}
        check("原生表情投递 delivered", bool(transport.faces) and statuses.get("native-face") == "delivered", str(statuses))
        check("本地表情包投递 delivered", bool(transport.media) and statuses.get("local-media") == "delivered", str(statuses))
        check("私聊不生成消息回应", "message-reaction" not in statuses, str(statuses))
        check("素材目录进入提示词", any(item["assetId"] == "happy/smile" for item in service._media_catalog()), str(service._media_catalog()))
        # 符号链接越界：目录内链接指向 root 外时必须跳过（rc56 realpath 加固）
        outside = os.path.join(tmp, "outside.png")
        with open(outside, "wb") as handle:
            handle.write(b"outside")
        link_path = os.path.join(tmp, "stickers", "happy", "evil.png")
        try:
            os.symlink(outside, link_path)
            service._sticker_cache = (0, [])
            check("越界符号链接不参与投递", service._resolve_sticker("happy/evil") is None, "")
            check(
                "越界符号链接不进入提示词目录",
                all(item["assetId"] != "happy/evil" for item in service._media_catalog()),
                str(service._media_catalog()),
            )
            os.unlink(link_path)
        except (OSError, NotImplementedError):
            pass
        # 未登记语义 / 不存在素材 → cancelled（不再永久 pending）
        bad = {
            "script": "她想发点什么，又停住了。",
            "interaction": {"seen": True, "reply": {"mode": "none"}},
            "localMedia": {"assetId": "missing/asset", "intent": "发个表情"},
            "nativeFace": {"semantic": "not-a-face", "intent": "做个表情"},
        }
        persisted_bad = await service._persist_turn(story.id, participant.id, bad, stamp, stamp, "user-message")
        await service._deliver_platform_actions(story, participant, persisted_bad["platformActions"], stamp)
        row_bad = service.db.first_row("interlude_script_entry", {"id": persisted_bad["scriptEntryId"]})
        bad_segments = [
            segment
            for action in (json_loads(row_bad["metadata"], {}).get("deliveryActions") or [])
            for segment in action.get("segments", [])
        ]
        check("非法动作全部 cancelled", bad_segments and all(segment["status"] == "cancelled" for segment in bad_segments), str(bad_segments))

        # 4b) 网页观察：域名策略、执行与 webContext
        check(
            "网页观察拒绝私网",
            not await service._browser_domain_allowed("http://127.0.0.1/", config["browser"]),
            "",
        )
        check(
            "网页观察拒绝黑名单",
            not await service._browser_domain_allowed(
                "https://example.com/", {"blockedDomains": ["example.com"]}
            ),
            "",
        )

        async def fake_observe(mode, query, url, cfg):
            return {
                "status": "ready", "url": "https://example.com/a",
                "title": "示例页面", "excerpt": "页面正文内容", "summary": "页面正文内容",
            }

        service._browser_observe = fake_observe
        await service._execute_browser_intents(
            story,
            {"browserIntents": [{"mode": "visit", "timing": "deferred", "url": "https://example.com/a"}]},
            participant.id,
            now_utc(),
        )
        observations = service._browser_observations(story.id)
        check(
            "网页观察落库并进入 webContext",
            any(item.get("title") == "示例页面" for item in observations),
            str(observations),
        )
        browser_request = await service._build_request(story, participant, "user-message", now, now)
        check("webContext 进入请求", bool(browser_request.webContext), str(browser_request.webContext))

        # 5) 时间导演接线
        captured_plan: dict = {}

        async def fake_plan(payload, cfg):
            captured_plan["payload"] = payload
            return {"beats": [{"at": 0.5, "kind": "activity", "summary": "整理花材"}], "carry": ["答应回电"]}

        async def fake_advance_run(story_obj, request_obj, group_turn=False):
            captured_plan["request"] = request_obj
            return {"success": False, "decision": {}, "error": "stub"}

        service.narrator.plan_timeline = fake_plan
        service._run_narrative = fake_advance_run
        await service._advance_unlocked(story.id, now_utc(), True)
        planned = captured_plan.get("request")
        check("时间账本进入请求", planned is not None and planned.timelinePlan is not None and bool(planned.timelinePlan.beats), str(planned and planned.timelinePlan))

        # 6) 架空世界识别（weather.skipFantasyWorld）
        fantasy = Story(
            id="fantasy", platform="onebot", selfId="1", userId="2", channelId="c", status="active",
            setting={**story.setting, "world": "异世界魔法学院，精灵与龙族共存。"},
            state=empty_story_state(), cursorAt=now, createdAt=now, updatedAt=now,
        )
        check("架空世界命中", service._is_fantasy_world(fantasy), "")
        check("现实世界不误判", not service._is_fantasy_world(story), "")

        # 7) maxStoriesPerSweep：每轮只处理配置数量的故事
        config["runtime"]["maxStoriesPerSweep"] = 1
        scanned: list[str] = []

        async def fake_due(story_obj, now_obj):
            scanned.append(story_obj.id)
            return 0

        service._process_due_intents = fake_due
        service.db.insert_row("interlude_story", {
            "id": "story-extra", "platform": "onebot", "selfId": "90001", "userId": "10002",
            "channelId": "umo-extra", "status": "active",
            "setting": story.setting, "state": empty_story_state(),
            "cursorAt": now, "createdAt": now, "updatedAt": now,
        })
        await service.sweep()
        check("扫描上限生效", len(scanned) == 1, str(scanned))
        service.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main() -> None:
    test_timeutil()
    test_group_willingness()
    test_alter()
    test_script_engine()
    test_schedule_preplan()
    test_agency()
    test_urge_alignment()
    test_type0_share_codes()
    test_browser_helpers()
    test_schema_defaults()
    test_metadata_update_contract()
    await test_turn_engine()
    await test_rc56_alignment()
    await test_rc57_alignment()
    await test_delivery_unknown()
    await test_service_end_to_end()
    await test_wiring_fixes()
    print(f"\n通过 {PASSED} 项")
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for item in FAILED:
            print(f"  ✗ {item}")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
