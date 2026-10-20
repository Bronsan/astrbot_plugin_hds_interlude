"""WebUI 测试接口的 HTTP 端到端测试。

启动 `tests/studio_test_server.py` 的真实 HTTP 服务，验证：

1. 静态资源（index/style/app.js）与 bridge shim 注入；
2. 前端 app.js 里的每个 action 都有后端 handler；
3. state / 角色 / 世界书 / 绑定 / 提示词 / 预览 / 导入导出 / 故事 / Type0 全链路；
4. AstrBot 实际路由别名；
5. 错误信封（404/400）；
6. `content.json` 落盘与重启后可读。

运行：python tests/webui_http_test.py
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core.content_store import read_png_card  # noqa: E402
from studio_test_server import StudioTestServer  # noqa: E402
from webui_test import TYPE0_INFO, make_png  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")


def request(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str, str]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")


def action(
    base: str,
    name: str,
    payload: dict | None = None,
    path: str = "/studio/action",
) -> tuple[int, dict]:
    body = json.dumps({"action": name, "payload": payload or {}}, ensure_ascii=False).encode("utf-8")
    status, _, text = request(base + path, "POST", body)
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, {"raw": text}


def main() -> None:
    global PASSED
    avatar = "data:image/png;base64," + base64.b64encode(make_png()).decode("ascii")
    data_dir = tempfile.mkdtemp(prefix="hds-webui-http-")
    server: StudioTestServer | None = StudioTestServer(data_dir=data_dir, type0_fixture=TYPE0_INFO).start()
    server2: StudioTestServer | None = None
    try:
        base = server.url.rstrip("/")

        # ---------------------------------------------------------------- #
        # 1. 静态资源与 bridge
        # ---------------------------------------------------------------- #
        status, content_type, html = request(base + "/")
        check("GET / 返回 200", status == 200, str(status))
        check("index 为 HTML", "text/html" in content_type, content_type)
        check("index 注入 bridge", "AstrBotPluginView" in html and "apiPost" in html, "")
        check("index 引用 app.js", './app.js' in html, "")
        check("index 引用 style.css", './style.css' in html, "")

        status, content_type, js = request(base + "/app.js")
        check("GET /app.js 返回 200", status == 200, str(status))
        check("app.js 为 JS", "javascript" in content_type, content_type)
        check("app.js 使用 bridge", "AstrBotPluginView" in js and "apiPost" in js, "")

        status, content_type, css = request(base + "/style.css")
        check("GET /style.css 返回 200", status == 200, str(status))
        check("style.css 为 CSS", "text/css" in content_type, content_type)

        status, _, health = request(base + "/healthz")
        check("GET /healthz 正常", status == 200 and json.loads(health)["status"] == "ok", health[:120])

        # ---------------------------------------------------------------- #
        # 2. 前端 action 与后端 handler 覆盖一致性
        # ---------------------------------------------------------------- #
        front_actions = set(re.findall(r"api\('([^']+)'", js))
        # importFile(kind) 动态选择，字面量只在三元表达式里
        front_actions.update({"import.character", "import.lorebook"})
        missing = sorted(
            name for name in front_actions if not hasattr(server.api, "action_" + name.replace(".", "_"))
        )
        check("前端 action 全部有后端 handler", not missing, str(missing))
        check("前端 action 数量合理", len(front_actions) >= 19, str(sorted(front_actions)))

        # ---------------------------------------------------------------- #
        # 3. state.get 初始态
        # ---------------------------------------------------------------- #
        status, obj = action(base, "state.get")
        check("state.get 200/ok", status == 200 and obj.get("status") == "ok", str(obj)[:200])
        state = obj.get("data") or {}
        check("初始角色数为 0", (state.get("stats") or {}).get("characters") == 0, str(state.get("stats")))
        check("state 含字段目录", bool((state.get("catalog") or {}).get("character")), "")
        check("state 含故事列表", any(item.get("id") == "story-test-1" for item in state.get("stories") or []), "")

        # ---------------------------------------------------------------- #
        # 4. 角色 CRUD / 激活
        # ---------------------------------------------------------------- #
        card = {
            "name": "测试角色",
            "description": "用于 HTTP 端到端测试的角色。",
            "personality": "克制、先观察再行动。",
            "scenario": "临海旧城，常年潮湿。",
            "avatar": avatar,
            "hds": {
                "perspective": "世界不会主动解释自己。",
                "world": "临海旧城，夜里只有便利店和花店亮着。",
                "supportingCast": "隔壁便利店老板老周。",
                "location": "旧港区",
                "relationship": "认识三个月的熟客。",
                "userProfile": "常来买花的熟客。",
                "style": "克制的现实主义日常。",
                "timezone": "Asia/Shanghai",
                "supplementaryFacts": ["花店在旧港区。"],
            },
        }
        status, obj = action(base, "character.save", {"character": card})
        check("character.save 200/ok", status == 200 and obj.get("status") == "ok", str(obj)[:200])
        saved = (obj.get("data") or {}).get("character") or {}
        char_id = saved.get("id") or ""
        check("character.save 返回 id", bool(char_id), str(saved)[:200])
        check("character.save 保留名称", saved.get("name") == "测试角色", str(saved.get("name")))
        check("character.save 记录来源", (saved.get("source") or {}).get("type") == "manual", str(saved.get("source")))

        status, obj = action(base, "character.get", {"id": char_id})
        check("character.get 命中", status == 200 and obj["data"]["character"]["name"] == "测试角色", str(obj)[:200])

        status, obj = action(base, "character.duplicate", {"id": char_id})
        duplicate = (obj.get("data") or {}).get("character") or {}
        duplicate_id = duplicate.get("id") or ""
        check("character.duplicate 产生新 id", bool(duplicate_id) and duplicate_id != char_id, str(duplicate)[:160])

        status, obj = action(base, "character.activate", {"characterId": char_id})
        active = (obj.get("data") or {}).get("active") or {}
        check("character.activate 生效", active.get("characterId") == char_id, str(active)[:160])

        # ---------------------------------------------------------------- #
        # 5. 世界书 CRUD
        # ---------------------------------------------------------------- #
        status, obj = action(
            base,
            "lorebook.save",
            {
                "lorebook": {
                    "name": "测试世界书",
                    "entries": [
                        {"comment": "常驻", "content": "世界是现实的。", "constant": True},
                        {"comment": "关键词", "keys": ["雨"], "content": "雨天的街道很安静。"},
                    ],
                }
            },
        )
        book = (obj.get("data") or {}).get("lorebook") or {}
        book_id = book.get("id") or ""
        check("lorebook.save 200/ok", status == 200 and bool(book_id), str(obj)[:200])
        check("lorebook.save 条目数", len(book.get("entries") or []) == 2, str(len(book.get("entries") or [])))

        status, obj = action(base, "lorebook.get", {"id": book_id})
        check("lorebook.get 命中", status == 200 and obj["data"]["lorebook"]["name"] == "测试世界书", str(obj)[:160])

        status, obj = action(base, "lorebook.duplicate", {"id": book_id})
        duplicate_book_id = ((obj.get("data") or {}).get("lorebook") or {}).get("id") or ""
        check("lorebook.duplicate 产生新 id", bool(duplicate_book_id) and duplicate_book_id != book_id, "")

        # ---------------------------------------------------------------- #
        # 6. 绑定 / 提示词 / 预览
        # ---------------------------------------------------------------- #
        status, obj = action(
            base,
            "binding.save",
            {"storyId": "story-test-1", "characterId": char_id, "lorebookIds": [book_id]},
        )
        binding = (obj.get("data") or {}).get("binding") or {}
        bound_state = (obj.get("data") or {}).get("state") or {}
        story_binding = (bound_state.get("bindings") or {}).get("story-test-1") or {}
        check("binding.save 200/ok", status == 200 and binding.get("characterId") == char_id, str(obj)[:200])
        check("binding.save 写入世界书", book_id in (story_binding.get("lorebookIds") or []), str(story_binding))

        status, obj = action(base, "prompts.save", {"prompts": {"stylePrompt": "更克制的文风", "mainPrompt": ""}})
        prompts = (obj.get("data") or {}).get("prompts") or {}
        check("prompts.save 生效", prompts.get("stylePrompt") == "更克制的文风", str(prompts))
        check("prompts.save 跳过空值", "mainPrompt" not in prompts, str(prompts))

        status, obj = action(
            base,
            "preview.prompt",
            {"characterId": char_id, "lorebookIds": [book_id], "query": "下雨了"},
        )
        preview = obj.get("data") or {}
        check("preview.prompt 200/ok", status == 200 and obj.get("status") == "ok", str(obj)[:200])
        check("preview.prompt 注入世界书", "WORLD BOOK" in (preview.get("worldBlock") or ""), "")
        defaults = preview.get("storyDefaults") or {}
        check("preview.prompt 映射角色名", defaults.get("characterName") == "测试角色", str(defaults)[:160])

        # ---------------------------------------------------------------- #
        # 7. 导出 / 导入
        # ---------------------------------------------------------------- #
        status, obj = action(base, "export.character", {"id": char_id})
        exported = json.loads(obj["data"]["content"])
        check("export.character 为酒馆 V2", exported.get("spec") == "chara_card_v2", str(exported.get("spec")))
        check("export.character 名称正确", exported["data"]["name"] == "测试角色", "")

        status, obj = action(base, "export.character.png", {"id": char_id})
        png_base64 = (obj.get("data") or {}).get("contentBase64") or ""
        png_bytes = base64.b64decode(png_base64) if png_base64 else b""
        check("export.character.png 返回 PNG", png_bytes.startswith(b"\x89PNG"), png_bytes[:8].hex())
        png_card = read_png_card(png_bytes) if png_bytes else None
        check("PNG 卡包含角色数据", bool(png_card) and png_card["data"]["name"] == "测试角色", "")

        status, obj = action(base, "import.character", {"filename": "card.png", "contentBase64": png_base64})
        imported = (obj.get("data") or {}).get("character") or {}
        imported_id = imported.get("id") or ""
        check("import.character(PNG) 成功", status == 200 and bool(imported_id) and imported_id != char_id, str(obj)[:200])
        check("import.character 名称一致", imported.get("name") == "测试角色", str(imported.get("name")))

        status, obj = action(base, "export.lorebook", {"id": book_id})
        exported_book = json.loads(obj["data"]["content"])
        check("export.lorebook 成功", exported_book.get("name") == "测试世界书", str(exported_book)[:120])

        world_json = json.dumps(
            {"name": "导入世界书", "entries": [{"keys": ["码头"], "content": "码头在城东。"}]},
            ensure_ascii=False,
        ).encode("utf-8")
        status, obj = action(
            base,
            "import.lorebook",
            {"filename": "world.json", "contentBase64": base64.b64encode(world_json).decode("ascii")},
        )
        imported_book = (obj.get("data") or {}).get("lorebook") or {}
        check("import.lorebook 成功", status == 200 and imported_book.get("name") == "导入世界书", str(obj)[:200])

        # ---------------------------------------------------------------- #
        # 8. 故事列表 / 应用到故事
        # ---------------------------------------------------------------- #
        status, obj = action(base, "story.list")
        stories = (obj.get("data") or {}).get("stories") or []
        check("story.list 返回测试故事", any(item.get("id") == "story-test-1" for item in stories), str(stories)[:160])

        status, obj = action(
            base,
            "story.apply",
            {"storyId": "story-test-1", "characterId": char_id, "lorebookIds": [book_id]},
        )
        applied = (obj.get("data") or {}).get("applied")
        check("story.apply 200/ok", status == 200 and applied is True, str(obj)[:200])
        check(
            "story.apply 调用宿主写回",
            ((server.applied_story_patches.get("story-test-1") or {}).get("character") or {}).get("name")
            == "测试角色",
            str(server.applied_story_patches)[:200],
        )

        # ---------------------------------------------------------------- #
        # 9. Type0 预览 / 导入（fixture）
        # ---------------------------------------------------------------- #
        status, obj = action(base, "type0.preview", {"code": "ABCD-1234"})
        type0 = obj.get("data") or {}
        check("type0.preview 200/ok", status == 200 and obj.get("status") == "ok", str(obj)[:200])
        check("type0.preview 映射角色名", (type0.get("card") or {}).get("name") == "林晚", str(type0.get("card"))[:160])
        check("type0.preview 返回映射表", bool(type0.get("mapping")), "")

        status, obj = action(base, "type0.import", {"code": "ABCD-1234", "activate": True})
        type0_imported = (obj.get("data") or {}).get("character") or {}
        check("type0.import 成功", status == 200 and type0_imported.get("name") == "林晚", str(obj)[:200])
        type0_state = (obj.get("data") or {}).get("state") or {}
        check("type0.import 完成激活", (type0_state.get("active") or {}).get("characterId") == type0_imported.get("id"), "")

        # ---------------------------------------------------------------- #
        # 10. 删除
        # ---------------------------------------------------------------- #
        status, obj = action(base, "character.delete", {"id": duplicate_id})
        check("character.delete 成功", status == 200 and obj["data"].get("deleted") is True, str(obj)[:160])
        status, obj = action(base, "lorebook.delete", {"id": duplicate_book_id})
        check("lorebook.delete 成功", status == 200 and obj["data"].get("deleted") is True, str(obj)[:160])
        status, obj = action(base, "binding.delete", {"storyId": "story-test-1"})
        check("binding.delete 成功", status == 200 and obj["data"].get("deleted") is True, str(obj)[:160])

        # ---------------------------------------------------------------- #
        # 11. AstrBot 实际路由别名
        # ---------------------------------------------------------------- #
        status, obj = action(base, "state.get", path="/astrbot_plugin_hds_interlude/studio/action")
        check("AstrBot 路由别名可用", status == 200 and obj.get("status") == "ok", str(obj)[:160])

        # ---------------------------------------------------------------- #
        # 12. 错误信封
        # ---------------------------------------------------------------- #
        status, obj = action(base, "nope", {})
        check("未知 action 返回 404", status == 404, str(status))
        check("未知 action 为 error 信封", obj.get("status") == "error" and "未知操作" in (obj.get("message") or ""), str(obj)[:160])

        status, obj = action(base, "character.get", {"id": "missing"})
        check("缺失角色返回 404", status == 404 and obj.get("status") == "error", str(obj)[:160])

        status, _, text = request(base + "/studio/action", "POST", b"{not-json")
        bad = json.loads(text) if text else {}
        check("非法 JSON 返回 400", status == 400, str(status))
        check("非法 JSON 为 error 信封", bad.get("status") == "error", str(bad)[:160])

        # ---------------------------------------------------------------- #
        # 13. 落盘与重启
        # ---------------------------------------------------------------- #
        content_path = Path(data_dir) / "content.json"
        check("content.json 已写入", content_path.exists(), str(content_path))
        persisted = json.loads(content_path.read_text(encoding="utf-8"))
        check("content.json 含角色", len(persisted.get("characters") or []) >= 2, str(len(persisted.get("characters") or [])))

        server.stop()
        server = None
        server2 = StudioTestServer(data_dir=data_dir).start()
        status, obj = action(server2.url.rstrip("/"), "state.get")
        restarted = obj.get("data") or {}
        check("重启后内容仍可读", status == 200 and (restarted.get("stats") or {}).get("characters") == len(persisted.get("characters") or []), str(restarted.get("stats")))
    finally:
        if server is not None:
            server.stop()
        if server2 is not None:
            server2.stop()
        shutil.rmtree(data_dir, ignore_errors=True)

    print(f"\nwebui http e2e: {PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for item in FAILED:
            print("  FAIL", item)
        raise SystemExit(1)
    print("webui http e2e passed")


if __name__ == "__main__":
    main()
