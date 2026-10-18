"""WebUI 内容层与 Studio API 的离线测试。

运行：python tests/webui_test.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.content_store import (  # noqa: E402
    ContentStore,
    activate_lorebook_entries,
    card_from_type0,
    card_png,
    card_to_sillytavern_v2,
    card_to_story_defaults,
    embed_png_card,
    lorebook_to_world_info,
    normalize_character_card,
    normalize_lorebook,
    read_png_card,
    story_setting_patch_from_card,
)
from core.studio_api import StudioAPI, StudioError  # noqa: E402

TYPE0_INFO: dict[str, Any] = {
    "code": "ABCD-1234",
    "title": "港城夜雨",
    "revision": 7,
    "sections": ["storyDefaults", "runtime"],
    "payload": {
        "storyDefaults": {
            "characterName": "林晚",
            "characterProfile": "夜班花店店员，作息不规律，习惯把话咽回去。",
            "perspective": "世界不会主动解释自己，所以她习惯先观察再行动。",
            "perspectives": ["对陌生人保持礼貌的距离。"],
            "supplementaryFacts": ["花店在旧港区，雨天几乎没有客人。"],
            "userProfile": "常来买花的熟客。",
            "relationship": "认识三个月，仍未交换联系方式。",
            "world": "临海旧城，常年潮湿，夜里只有便利店和花店亮着。",
            "supportingCast": "隔壁便利店老板老周，爱管闲事。",
            "location": "旧港区",
            "style": "克制的现实主义日常。",
            "timezone": "Asia/Shanghai",
        },
        "runtime": {"sweepIntervalMinutes": 5},
    },
}


def _chunk(kind: str, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind.encode() + payload + struct.pack(">I", zlib.crc32(kind.encode() + payload) & 0xFFFFFFFF)


def make_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk("IHDR", header)
        + _chunk("IDAT", zlib.compress(raw))
        + _chunk("IEND", b"")
    )


async def fake_fetch(base_url: str, code: str) -> dict[str, Any]:
    if code.upper() != "ABCD-1234":
        raise ValueError("分享码不存在")
    return TYPE0_INFO


def test_character_card_v2() -> None:
    raw = {
        "spec": "chara_card_v2",
        "data": {
            "name": "测试角色",
            "description": "描述",
            "personality": "冷静",
            "tags": ["测试", " 测试 "],
            "first_mes": "你好",
            "alternate_greetings": ["嗨"],
            "extensions": {"hds": {"world": "测试世界", "perspective": "视角"}},
        },
    }
    card = normalize_character_card(raw)
    assert card["name"] == "测试角色"
    assert card["tags"] == ["测试"], card["tags"]
    assert card["hds"]["world"] == "测试世界"
    assert card["hds"]["timezone"] == "Asia/Shanghai"
    exported = card_to_sillytavern_v2(card)
    assert exported["spec"] == "chara_card_v2"
    assert exported["data"]["name"] == "测试角色"
    assert exported["data"]["extensions"]["hds"]["world"] == "测试世界"
    print("character card v2 ok")


def test_lorebook_normalize_and_activation() -> None:
    raw = {
        "name": "港城设定",
        "token_budget": 500,
        "entries": {
            "0": {
                "key": ["旧港区", "港区"],
                "keysecondary": ["雨"],
                "comment": "旧港区",
                "content": "旧港区靠海，雨天路灯很暗。",
                "order": 10,
                "selective": True,
                "selectiveLogic": 3,
                "disable": False,
            },
            "1": {
                "key": [],
                "comment": "世界规则",
                "content": "这是一个没有超自然力量的世界。",
                "constant": True,
                "order": 1,
            },
            "2": {
                "key": ["不存在的词"],
                "comment": "不会命中",
                "content": "不应出现。",
                "disable": True,
            },
        },
    }
    book = normalize_lorebook(raw)
    assert len(book["entries"]) == 3
    assert book["entries"][0]["constant"] is True  # 按 order 排序
    assert book["entries"][2]["enabled"] is False
    active = activate_lorebook_entries([book], "今天旧港区下着雨，她关了花店的门。", max_characters=1000)
    names = [entry["comment"] for entry in active]
    assert "世界规则" in names and "旧港区" in names, names
    assert "不会命中" not in names
    active_without_secondary = activate_lorebook_entries(
        [book], "今天旧港区很晴。", max_characters=1000
    )
    names = [entry["comment"] for entry in active_without_secondary]
    assert "旧港区" not in names, names  # AND ALL 要求次要关键词也命中
    exported = lorebook_to_world_info(book)
    assert isinstance(exported["entries"], list) and len(exported["entries"]) == 3
    print("lorebook normalize/activation ok")


def test_type0_mapping() -> None:
    card = card_from_type0(TYPE0_INFO, base_url="https://typ0.space")
    assert card["name"] == "林晚"
    assert "夜班花店" in card["description"]
    assert card["hds"]["world"].startswith("临海旧城")
    assert card["hds"]["perspectives"][0].startswith("世界不会")
    assert card["source"]["type"] == "type0"
    assert card["source"]["revision"] == 7
    defaults = card_to_story_defaults(card)
    assert defaults["characterName"] == "林晚"
    assert defaults["world"].startswith("临海旧城")
    assert defaults["timezone"] == "Asia/Shanghai"
    patch = story_setting_patch_from_card(card)
    assert patch["character"]["name"] == "林晚"
    assert patch["user"]["profile"] == "常来买花的熟客。"
    print("type0 mapping ok")


def test_png_card_roundtrip() -> None:
    png = make_png()
    raw = {
        "spec": "chara_card_v2",
        "data": {"name": "PNG 角色", "description": "来自 PNG 数据块"},
    }
    card = normalize_character_card(raw)
    embedded = embed_png_card(png, card)
    loaded = read_png_card(embedded)
    assert loaded is not None
    assert loaded["data"]["name"] == "PNG 角色"
    with_avatar = dict(card)
    with_avatar["avatar"] = "data:image/png;base64," + base64.b64encode(png).decode()
    exported = card_png(with_avatar)
    assert exported.startswith(b"\x89PNG")
    assert read_png_card(exported)["data"]["name"] == "PNG 角色"
    print("png card roundtrip ok")


async def test_store_and_api() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ContentStore(tmp)
        api = StudioAPI(
            store,
            share_fetcher=fake_fetch,
            story_lister=None,
            story_applier=None,
            default_base="https://typ0.space",
        )
        # Type0 预览
        preview = await api.dispatch("type0.preview", {"code": "ABCD-1234"})
        assert preview["card"]["name"] == "林晚"
        assert preview["mapping"]
        assert any("世界书" in item for item in preview["warnings"])

        # Type0 导入并激活
        imported = await api.dispatch("type0.import", {"code": "ABCD-1234", "activate": True})
        assert imported["character"]["name"] == "林晚"
        assert store.stats()["characters"] == 1
        assert store.active()["characterId"] == imported["character"]["id"]

        # 手动角色 + 提示词 + 世界书
        manual = await api.dispatch(
            "character.save",
            {"character": {"name": "手动角色", "description": "手动描述"}},
        )
        assert manual["character"]["source"]["type"] == "manual"
        prompts = await api.dispatch(
            "prompts.save",
            {"prompts": {"stylePrompt": "更克制的文风", "mainPrompt": ""}},
        )
        assert prompts["prompts"]["stylePrompt"] == "更克制的文风"
        assert "mainPrompt" not in prompts["prompts"]

        book = await api.dispatch(
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
        book_id = book["lorebook"]["id"]

        # 绑定与预览
        bound = await api.dispatch("character.activate", {
            "characterId": imported["character"]["id"],
            "lorebookIds": [book_id],
        })
        assert book_id in bound["state"]["stats"]["activeLorebookIds"]
        preview_prompt = await api.dispatch(
            "preview.prompt",
            {"characterId": imported["character"]["id"], "lorebookIds": [book_id], "query": "下雨了"},
        )
        assert "WORLD BOOK" in preview_prompt["worldBlock"]
        assert preview_prompt["storyDefaults"]["characterName"] == "林晚"

        # 配置叠加
        base_config = {
            "storyDefaults": {"characterName": "配置回退", "timezone": "UTC"},
            "model": {"stylePrompt": "默认文风", "mainPrompt": "默认方向"},
        }
        effective = store.apply_config_overrides(base_config)
        assert effective["storyDefaults"]["characterName"] == "林晚"
        assert effective["storyDefaults"]["timezone"] == "Asia/Shanghai"
        assert effective["model"]["stylePrompt"] == "更克制的文风"

        # 导出
        exported = await api.dispatch("export.character", {"id": imported["character"]["id"]})
        assert json.loads(exported["content"])["data"]["name"] == "林晚"
        exported_book = await api.dispatch("export.lorebook", {"id": book_id})
        assert json.loads(exported_book["content"])["name"] == "测试世界书"

        # 文件导入（世界书 JSON）
        payload = base64.b64encode(json.dumps({
            "name": "导入世界书",
            "entries": [{"keys": ["码头"], "content": "码头在城东。"}],
        }).encode()).decode()
        imported_book = await api.dispatch(
            "import.lorebook",
            {"filename": "world.json", "contentBase64": payload},
        )
        assert imported_book["lorebook"]["name"] == "导入世界书"

        # 未知操作
        try:
            await api.dispatch("nope", {})
        except StudioError as error:
            assert error.status == 404
        else:
            raise AssertionError("unknown action should raise")

        # 重新加载持久化
        reloaded = ContentStore(tmp)
        assert len(reloaded.characters()) == 2
        assert reloaded.prompts()["stylePrompt"] == "更克制的文风"
        print("store/api ok")


async def main() -> None:
    test_character_card_v2()
    test_lorebook_normalize_and_activation()
    test_type0_mapping()
    test_png_card_roundtrip()
    await test_store_and_api()
    print("webui tests passed")


if __name__ == "__main__":
    asyncio.run(main())
