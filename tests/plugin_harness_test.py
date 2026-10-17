"""插件入口层冒烟测试（用桩模拟 AstrBot API）。

验证：命令派发（点分/空格/唤醒前缀）、确认问答、私聊消息中间件、
失明模式静默、叙事回合接入。运行：python tests/plugin_harness_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tests", "stubs"))
sys.path.insert(0, os.path.dirname(BASE))  # 以包形式导入插件（相对导入需要）
sys.path.insert(0, BASE)  # tests 包与 core 包

from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.star import Context  # noqa: E402

import importlib  # noqa: E402

plugin_main = importlib.import_module(f"{os.path.basename(BASE)}.main")
from tests.smoke_test import build_config  # noqa: E402
from core.narrator import NarrativeResult  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")


async def collect(agen) -> list[str]:
    """收集事件监听器 async generator 产出的文本。"""
    results: list[str] = []
    async for item in agen:
        if isinstance(item, dict) and item.get("type") == "plain":
            results.append(item["text"])
    return results


async def run_dispatch(plugin, event, text):
    return await plugin._dispatch_command(event, text)


def story_id_of(plugin) -> str:
    row = plugin.service.db.first_row("interlude_story", order_by="updatedAt DESC")
    return row["id"] if row else ""


def sent_texts(context) -> list[str]:
    """从宿主发送记录里提取纯文本（桩 MessageChain.chain 组件）。"""
    texts: list[str] = []
    for _, chain in context.sent:
        for component in getattr(chain, "chain", []):
            text = getattr(component, "text", None)
            if isinstance(text, str):
                texts.append(text)
    return texts


async def main() -> None:
    config = build_config()
    context = Context()
    plugin = plugin_main.HDSInterludePlugin(context=context, config=config)
    try:
        # 1) doctor：无故事 + 无模型连接 → 有阻断项
        event = AstrMessageEvent(message_str="interlude.doctor")
        out = await run_dispatch(plugin, event, "interlude.doctor")
        check("doctor 输出档案检查", out is not None and "配置档案检查" in out, str(out))
        check("doctor 报告阻断项", "阻断" in out and "主叙事" in out, str(out))

        # 2) status：无故事提示
        out = await run_dispatch(plugin, event, "interlude.status")
        check("无故事提示", out is not None and "还没有故事" in out, str(out))

        # 3) 空格写法归一（interlude memory facts → interlude.memory.facts）
        #    重写成功的证据：进入了该命令的处理器（无故事提示），而不是退回 interlude 入口帮助。
        out = await run_dispatch(plugin, event, "interlude memory facts 5")
        check("空格写法重写", out is not None and "还没有故事" in out, str(out))

        # 4) 唤醒前缀写法（成员命令，无确认需求）
        out = await run_dispatch(plugin, event, "/interlude.memory 5")
        check("唤醒前缀识别", out is not None and "还没有故事" in out, str(out))

        # 5) 补上模型连接后 story.start：y/n 确认流
        config["model"]["providers"] = [{
            "label": "Stub", "mode": "openai-compatible",
            "endpoint": "https://example.com/v1/chat/completions", "apiKey": "x",
            "model": "stub-model", "enabled": True,
            "useForMain": True, "useForCompaction": True, "useForAlter": True,
        }]
        plugin.service.refresh_runtime()
        event5 = AstrMessageEvent(message_str="interlude.story.start")
        task = asyncio.ensure_future(run_dispatch(plugin, event5, "interlude.story.start"))
        await asyncio.sleep(0.1)  # 等提问发出
        check("确认提问已发送", any("确认从此档案启动吗" in str(item) for item in event5.sent), str(event5.sent))
        answer = AstrMessageEvent(message_str="y")
        resolved = plugin._resolve_confirmation(answer)
        check("确认被消费", resolved is True and answer._stopped, str(resolved))
        out = await asyncio.wait_for(task, 5)
        check("故事启动成功", out is not None and "已从配置档案启动" in out, str(out))

        # 6) 启动后 status 展示
        out = await run_dispatch(plugin, event, "interlude.status")
        check("status 展示主角", out is not None and "主角：林知遥" in out, str(out))
        check("status 展示状态", "故事状态：active" in out, str(out))

        # 7) 私聊叙事接入：消息被 consume，事件被 stop
        setattr(sys.modules[__name__], "MODEL_CALLS", 0)

        async def fake_decide(request, cfg):
            globals()["MODEL_CALLS"] = globals().get("MODEL_CALLS", 0) + 1
            return NarrativeResult(succeeded=True, decision={
                "script": "她抬头看了一眼窗外的雨。",
                "interaction": {"seen": True, "reply": {"mode": "immediate", "content": "在的"}},
                "authoredActions": [], "intents": [], "intentUpdates": [], "crossConversationActions": [],
                "memories": [], "alter": None, "agencyWindow": None, "proactiveContact": None,
                "statePatch": None, "localMedia": None, "nativeFace": None, "messageReactions": None,
                "droppedProactiveActions": [], "followUpResolutions": [],
            }, providerLabel="stub")

        plugin.service.narrator.decide = fake_decide  # type: ignore[assignment]
        narrative_event = AstrMessageEvent(message_str="今天路过花店，看到你换班了")
        await collect(plugin.on_private_message(narrative_event))
        check("叙事消息被消费", narrative_event._stopped, "not stopped")
        await asyncio.sleep(1.0)
        check("模型被调用", globals().get("MODEL_CALLS", 0) >= 1, str(globals().get("MODEL_CALLS")))
        sent = sent_texts(context)
        check("回复经宿主发送", any("在的" in item for item in sent), str(sent))

        # 8) 命令消息不进入剧本（ignoreCommandMessages）
        before = globals().get("MODEL_CALLS", 0)
        command_event = AstrMessageEvent(message_str="interlude.status")
        await collect(plugin.on_private_message(command_event))
        check("命令消息被消费", command_event._stopped, "not stopped")
        await asyncio.sleep(0.3)
        check("命令不触发写作", globals().get("MODEL_CALLS", 0) == before, str(globals().get("MODEL_CALLS")))

        # 9) 未知命令写法：识别为 command-like，不进入叙事
        unknown = AstrMessageEvent(message_str="interlude.unknown.thing")
        await collect(plugin.on_private_message(unknown))
        await asyncio.sleep(0.3)
        check("未知命令不触发写作", globals().get("MODEL_CALLS", 0) == before, str(globals().get("MODEL_CALLS")))

        # 12) 群聊链路：白名单群捕获 → 群回合缓冲 → 落库
        config["onebot"]["groupChatsJson"] = (
            '[{"groupId":"555","label":"同学群","enabled":true,'
            '"responseMode":"always","willingnessPreset":"off","cooldownSeconds":0,'
            '"debounceSeconds":0.1,"contextLimit":10}]'
        )
        group_event = AstrMessageEvent(
            message_str="大家晚上好", sender_id="20002", sender_name="周宁",
            group_id="555", umo="onebot:GroupMessage:555",
        )
        await collect(plugin.on_group_message(group_event))
        check("群消息被消费", group_event._stopped, "not stopped")
        await asyncio.sleep(1.6)
        group_entries = [
            entry for entry in await plugin.service.recent_entries(story_id_of(plugin), 30)
            if entry.kind == "group-message"
        ]
        check("群消息落库", bool(group_entries), "no group entry")

        # 13) 未授权群：不消费
        denied_group = AstrMessageEvent(
            message_str="路过", group_id="999", umo="onebot:GroupMessage:999",
        )
        await collect(plugin.on_group_message(denied_group))
        check("未授权群不消费", denied_group._stopped is False, "was stopped")

        # 14) 失明模式：命令静默
        config["blindMode"] = {"enabled": True}
        blind_event = AstrMessageEvent(message_str="interlude.status")
        out = await run_dispatch(plugin, blind_event, "interlude.status")
        check("失明模式命令静默", out == "", str(out))
        config["blindMode"] = {"enabled": False}

        # 11) purge.range 参数校验
        out = await run_dispatch(plugin, event, "interlude.purge.range bad-date 2026-01-01T00:00:00+08:00")
        check("purge.range 校验 ISO 时间", out == "时间范围无效，请使用 ISO-8601，例如 2026-08-01T00:00:00+08:00。", str(out))

        # 12) 确认超时后按取消（用 0.01s 快速模拟）
        async def quick_cancel():
            event_x = AstrMessageEvent(message_str="interlude.pause")
            task_x = asyncio.ensure_future(run_dispatch(plugin, event_x, "interlude.pause"))
            await asyncio.sleep(0.05)
            # 不回答 → 直接取消 future 模拟超时
            key = event_x.unified_msg_origin
            future = plugin._confirmations.get(key)
            if future:
                future.set_result("n")
            out_x = await asyncio.wait_for(task_x, 5)
            return out_x

        # pause 不需要确认（管理员指令直接执行）；这里用一个需要确认的命令
        event_y = AstrMessageEvent(message_str="interlude.timeline.rebase")
        task_y = asyncio.ensure_future(run_dispatch(plugin, event_y, "interlude.timeline.rebase"))
        await asyncio.sleep(0.1)
        future = plugin._confirmations.get(event_y.unified_msg_origin)
        check("rebase 等待确认", future is not None and not future.done(), "no pending confirmation")
        if future:
            future.set_result("n")
        out_y = await asyncio.wait_for(task_y, 5)
        check("回答 n 后取消", out_y == "操作已取消。", str(out_y))
    finally:
        await plugin.terminate()

    print(f"\n通过 {PASSED} 项")
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for item in FAILED:
            print(f"  ✗ {item}")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
