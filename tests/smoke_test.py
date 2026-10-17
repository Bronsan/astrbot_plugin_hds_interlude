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
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import alter as alter_mod  # noqa: E402
from core import agency as agency_mod  # noqa: E402
from core import group_willingness as gw  # noqa: E402
from core import schedule_preplan as preplan  # noqa: E402
from core import script_engine as se  # noqa: E402
from core.narrator import NarrativeResult  # noqa: E402
from core.service import InterludeService, SessionView  # noqa: E402
from core.timeutil import story_local_time_context  # noqa: E402
from core.turn_engine import TurnEngine, should_supersede_request  # noqa: E402
from core.types import now_utc, to_iso  # noqa: E402

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
    runtime = {"messageSeparator": "<sep/>", "maxMessageCharacters": 2000, "splitReplyMessages": True,
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

    async def send(self, umo: str, text: str) -> bool:
        self.sent.append((umo, text))
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
            "contextEntryLimit": 35, "contextTimeWindowMinutes": 45, "memoryLimit": 20,
            "maxMessageCharacters": 2000, "maxScriptCharacters": 8000,
            "minimumDelayedReplySeconds": 10, "maximumDelayedReplyMinutes": 1440,
            "allowProactiveMessages": True, "proactiveWillingnessThreshold": 0.65,
            "narrativeRetryMaxAttempts": 0,
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
        sent = [text for _, text in transport.sent]
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


async def main() -> None:
    test_timeutil()
    test_group_willingness()
    test_alter()
    test_script_engine()
    test_schedule_preplan()
    test_agency()
    await test_turn_engine()
    await test_service_end_to_end()
    print(f"\n通过 {PASSED} 项")
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for item in FAILED:
            print(f"  ✗ {item}")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
