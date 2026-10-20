"""插件入口层冒烟测试（用桩模拟 AstrBot API）。

验证：命令派发（点分/空格/唤醒前缀）、确认问答、私聊消息中间件、
失明模式静默、叙事回合接入。运行：python tests/plugin_harness_test.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile

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

# 必须使用插件包内的类型：`core.types`（顶层导入）与
# `astrbot_plugin_hds_interlude.core.types` 是两个不同的模块对象。
DeliveryUnknown = plugin_main.DeliveryUnknown

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

        # 11) 回执超时 → DeliveryUnknown（结果未知语义，对应上游 rc53/rc56）
        config["blindMode"] = {"enabled": False}
        plugin._blind = False

        async def _timeout_send(umo, chain):
            raise asyncio.TimeoutError("platform timeout")

        original_send = plugin.context.send_message
        plugin.context.send_message = _timeout_send  # type: ignore[assignment]
        try:
            await plugin.transport.send("umo", "文本")
            timeout_raised = False
        except DeliveryUnknown:
            timeout_raised = True
        finally:
            plugin.context.send_message = original_send  # type: ignore[assignment]
        check("回执超时抛 DeliveryUnknown", timeout_raised, "")

        # 12) 日志层防御：logger 抛错不影响调用（对应上游 rc55 writeReport 防御）
        original_info = plugin_main.astrbot_logger.info

        def _broken_info(*args, **kwargs):
            raise RuntimeError("logger down")

        plugin_main.astrbot_logger.info = _broken_info  # type: ignore[assignment]
        try:
            plugin._log("info", "日志防御检查")
            log_safe = True
        except Exception:  # noqa: BLE001
            log_safe = False
        finally:
            plugin_main.astrbot_logger.info = original_info  # type: ignore[assignment]
        check("日志失败不抛出", log_safe, "")

        # 13) rc57 配置面：预设入口置顶 + 默认预算下调
        schema = json.load(open(os.path.join(BASE, "_conf_schema.json"), encoding="utf-8"))
        check("rc57 模型中心置顶", next(iter(schema)) == "model", str(list(schema)[:3]))
        runtime = schema["runtime"]["items"]
        check("rc57 上下文默认 17 条", runtime["contextEntryLimit"]["default"] == 17, str(runtime["contextEntryLimit"]))
        check("rc57 时间窗默认 6 分钟", runtime["contextTimeWindowMinutes"]["default"] == 6, str(runtime["contextTimeWindowMinutes"]))
        check("rc57 剧本默认 1400", runtime["maxScriptCharacters"]["default"] == 1400, str(runtime["maxScriptCharacters"]))
        check("rc57 消息默认 1500", runtime["maxMessageCharacters"]["default"] == 1500, str(runtime["maxMessageCharacters"]))
        check("rc57 重试默认 15 秒", runtime["narrativeRetryDelaySeconds"]["default"] == 15, str(runtime["narrativeRetryDelaySeconds"]))

        # 14) 原生表情 / 本地素材投递组件
        plugin.context.sent.clear()
        face_ok = await plugin.transport.send_face("umo-face", "14")
        media_ok = await plugin.transport.send_media("umo-media", __file__, "image")
        missing = await plugin.transport.send_media("umo-media", "/nonexistent/file.png", "image")
        face_chain = plugin.context.sent[0][1].chain[0] if plugin.context.sent else None
        media_chain = plugin.context.sent[1][1].chain[0] if len(plugin.context.sent) > 1 else None
        check("send_face 组件正确", bool(face_ok) and getattr(face_chain, "id", None) == 14, str(face_chain))
        check("send_media 组件正确", bool(media_ok) and str(getattr(media_chain, "file", "")).endswith("plugin_harness_test.py"), str(media_chain))
        check("缺失素材确定失败", missing is False, str(missing))

        # 14b) replyTo 透传：Reply 组件 + 文本
        plugin.context.sent.clear()
        reply_ok = await plugin.transport.send("umo-reply", "文本", "m-42")
        reply_chain = plugin.context.sent[0][1].chain if plugin.context.sent else []
        check(
            "replyTo 生成 Reply 组件",
            bool(reply_ok) and len(reply_chain) == 2 and getattr(reply_chain[0], "id", None) == "m-42",
            str(reply_chain),
        )

        # 14c) 图片按 vision.maxImageDimension 缩放（AstrBot 自带 Pillow）
        try:
            from PIL import Image as PILImage

            config["model"]["vision"] = {
                "enabled": True, "mode": "native", "detail": "auto", "maxImageDimension": 40,
            }
            image_path = os.path.join(tempfile.gettempdir(), "hdsi-vision-resize.png")
            PILImage.new("RGB", (120, 60), (200, 20, 20)).save(image_path)
            data_uri, _mime = await plugin.transport.fetch_image_data_uri(image_path)
            raw = base64.b64decode(data_uri.split(",", 1)[1])
            with PILImage.open(io.BytesIO(raw)) as resized:
                check("图片按 maxImageDimension 缩放", max(resized.size) <= 40, str(resized.size))
        except ImportError:
            pass

        # 14d) SILK 识别与缺库降级（AstrBot 默认自带 silk-python）
        check("SILK 魔数识别", plugin_main._is_silk(b"#!SILK_V3xxxx"), "")
        check("非 SILK 不误判", not plugin_main._is_silk(b"ID3xxxx", "mp3"), "")
        check("SILK 解码失败可降级", plugin_main._decode_silk_to_wav(b"#!SILK_V3garbage") is None, "")

        # 15) 日志级别过滤（logging.level）
        records: list[tuple[str, str]] = []

        class _Recorder:
            def info(self, message, *args):
                records.append(("info", message))

            def warning(self, message, *args):
                records.append(("warn", message))

            def error(self, message, *args):
                records.append(("error", message))

            def debug(self, message, *args):
                records.append(("debug", message))

        original_logger = plugin_main.astrbot_logger
        plugin_main.astrbot_logger = _Recorder()  # type: ignore[assignment]
        config["logging"] = {"level": "error", "previewLength": 50}
        try:
            plugin._log("info", "应被过滤")
            plugin._log("error", "应被保留")
        finally:
            plugin_main.astrbot_logger = original_logger  # type: ignore[assignment]
        check("日志级别过滤生效", [item[0] for item in records] == ["error"], str(records))

        # 16) 合并转发展开（runtime.forwardMessage）
        from astrbot.api.message_components import Node, Nodes, Plain

        config["runtime"]["forwardMessage"] = {
            "enabled": True, "maxNodes": 30, "maxCharacters": 2000, "maxDepth": 3,
        }

        node_one = Node(content=[Plain("第一句")], name="阿宁", uin="1")
        node_two = Node(content=[Plain("第二句")], name="小林", uin="2")
        forward_event = AstrMessageEvent(message_str="", components=[Nodes(nodes=[node_one, node_two])])
        forward_session = await plugin._session(forward_event)
        check(
            "合并转发展开",
            "阿宁: 第一句" in forward_session.content and "小林: 第二句" in forward_session.content,
            forward_session.content,
        )
        config["runtime"]["forwardMessage"]["maxNodes"] = 1
        limited = await plugin._session(forward_event)
        check(
            "合并转发节点预算",
            "阿宁: 第一句" in limited.content and "第二句" not in limited.content,
            limited.content,
        )

        # 17) 平台 API：get_forward_msg 与 set_msg_emoji_like
        from astrbot.api.message_components import Forward

        calls: list[tuple[str, dict]] = []

        class _Client:
            async def call_action(self, action, **kwargs):
                calls.append((action, kwargs))
                if action == "get_forward_msg":
                    return {
                        "messages": [
                            {
                                "sender": {"nickname": "阿宁"},
                                "message": [{"type": "text", "data": {"text": "平台展开的转发"}}],
                            }
                        ]
                    }
                return {"retcode": 0}

        class _Platform:
            def get_client(self):
                return _Client()

        plugin.context.platform_inst = _Platform()
        ref_event = AstrMessageEvent(message_str="", components=[Forward(id="f-1")])
        ref_session = await plugin._session(ref_event)
        check("Forward 经平台 API 展开", "平台展开的转发" in ref_session.content, ref_session.content)
        reaction_ok = await plugin.transport.send_reaction("p1:GroupMessage:g1", "m-1", "76")
        check(
            "回应走 set_msg_emoji_like",
            bool(reaction_ok) and any(item[0] == "set_msg_emoji_like" for item in calls),
            str(calls),
        )
        plugin.context.platform_inst = None

        # 18) Type0 内置导入：没有消息事件也能拉取并应用
        config["presetSync"] = {
            "enabled": True,
            "baseUrl": "https://example.test",
            "shareCode": "TEST-1234",
            "autoUpdate": True,
            "sections": ["runtime"],
            "applyToCurrentStory": False,
        }
        preset_payload = {
            "code": "TEST-1234",
            "title": "测试预设",
            "revision": "r1",
            "sections": ["runtime"],
            "payload": {"runtime": {"userMessageDebounceSeconds": 1.5}},
        }
        original_fetch = plugin_main.fetch_share

        async def _fake_fetch(base, code, timeout=15.0):
            return preset_payload

        plugin_main.fetch_share = _fake_fetch  # type: ignore[assignment]
        try:
            applied, reason = await plugin._run_preset_sync(None, None, force=True)
        finally:
            plugin_main.fetch_share = original_fetch  # type: ignore[assignment]
        check(
            "Type0 无事件导入",
            bool(applied) and config["runtime"]["userMessageDebounceSeconds"] == 1.5,
            reason,
        )
        check(
            "Type0 记录 revision",
            (plugin.service.load_preset_sync_snapshot() or {}).get("revision") == "r1",
            str(plugin.service.load_preset_sync_snapshot()),
        )

        # 19) 插件页路由注册与动作分发（WebUI 真实入口）
        from astrbot.api import web as web_stub

        route = plugin.context.web_apis.get("/astrbot_plugin_hds_interlude/studio/action")
        check("WebUI 路由已注册", route is not None, str(list(plugin.context.web_apis)))
        if route is not None:
            try:
                web_stub.request.payload = {"action": "state.get", "payload": {}}
                response = await route["handler"]()
                check(
                    "WebUI 路由 state.get",
                    isinstance(response, dict)
                    and response.get("status") == "ok"
                    and "characters" in (response.get("data") or {}),
                    str(response)[:200],
                )

                web_stub.request.payload = {
                    "action": "character.save",
                    "payload": {"character": {"name": "路由角色"}},
                }
                saved_response = await route["handler"]()
                check(
                    "WebUI 路由 character.save",
                    ((saved_response.get("data") or {}).get("character") or {}).get("name")
                    == "路由角色",
                    str(saved_response)[:200],
                )

                web_stub.request.payload = {"action": "nope", "payload": {}}
                bad_action = await route["handler"]()
                check(
                    "WebUI 路由错误信封",
                    bad_action.get("status") == "error" and bad_action.get("statusCode") == 404,
                    str(bad_action)[:160],
                )

                web_stub.request.payload = "not-a-dict"
                bad_body = await route["handler"]()
                check(
                    "WebUI 路由拒绝非对象请求",
                    bad_body.get("status") == "error" and bad_body.get("statusCode") == 400,
                    str(bad_body)[:160],
                )
            finally:
                web_stub.request.payload = {}
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
