"""WebUI「角色工作室」后端动作层。

纯 Python，不导入 astrbot：`main.py` 只负责把 Dashboard bridge 的请求
转换成 `(action, payload)`，再调用 `StudioAPI.dispatch()`。这样既能脱离
AstrBot 单测，也避免把框架对象泄漏进业务代码。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .content_store import (
    ContentError,
    ContentStore,
    DEFAULT_WORLD_BOOK_BUDGET,
    PROMPT_KEYS,
    _type0_story_defaults,
    card_from_type0,
    card_png,
    card_to_sillytavern_v2,
    card_to_story_defaults,
    lorebook_from_type0,
    lorebook_to_world_info,
    normalize_character_card,
    normalize_lorebook,
    read_png_card,
    story_setting_patch_from_card,
    type0_mapping_rows,
)
from .share_sync import default_share_base, fetch_share

MAX_IMPORT_BYTES = 8 * 1024 * 1024

FIELD_CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "character": [
        {"key": "name", "label": "角色名", "group": "identity", "type": "text", "required": True, "hint": "叙事中的主角名，也是角色卡的唯一标识。"},
        {"key": "description", "label": "角色描述", "group": "identity", "type": "textarea", "rows": 6, "hint": "身份、经历、作息、习惯与行为边界；对应 storyDefaults.characterProfile。"},
        {"key": "personality", "label": "性格", "group": "identity", "type": "textarea", "rows": 3, "hint": "独立于描述的简短性格摘要；导入时若与描述重复会自动去重。"},
        {"key": "scenario", "label": "场景", "group": "identity", "type": "textarea", "rows": 4, "hint": "故事发生的时代、地点与处境；未单独填写世界时会作为 world 回退。"},
        {"key": "tags", "label": "标签", "group": "identity", "type": "tags", "hint": "用于筛选，随酒馆角色卡导出。"},
        {"key": "creator", "label": "作者", "group": "meta", "type": "text"},
        {"key": "character_version", "label": "角色版本", "group": "meta", "type": "text"},
        {"key": "creator_notes", "label": "作者注记", "group": "meta", "type": "textarea", "rows": 2},
        {"key": "first_mes", "label": "开场白", "group": "dialogue", "type": "textarea", "rows": 4, "hint": "酒馆格式字段；仅作开场氛围参考，不会作为已发生剧情注入。"},
        {"key": "alternate_greetings", "label": "备用开场", "group": "dialogue", "type": "lines"},
        {"key": "mes_example", "label": "对话示例", "group": "dialogue", "type": "textarea", "rows": 5, "hint": "作为语气参考注入系统提示词，明确要求不得照抄。"},
        {"key": "system_prompt", "label": "角色系统提示", "group": "prompting", "type": "textarea", "rows": 5, "hint": "角色级权威指令，优先级高但不能覆盖 JSON 输出合约。"},
        {"key": "post_history_instructions", "label": "历史后置指令", "group": "prompting", "type": "textarea", "rows": 4},
    ],
    "hds": [
        {"key": "perspective", "label": "个体视角", "group": "hds", "type": "textarea", "rows": 3, "hint": "主角看待世界的方式，对应 storyDefaults.perspective。"},
        {"key": "perspectives", "label": "多条视角", "group": "hds", "type": "lines", "hint": "每条独立注入；与单条视角合并去重。"},
        {"key": "userProfile", "label": "用户默认背景", "group": "hds", "type": "textarea", "rows": 3},
        {"key": "relationship", "label": "初始关系", "group": "hds", "type": "textarea", "rows": 2},
        {"key": "world", "label": "世界设定", "group": "hds", "type": "textarea", "rows": 4, "hint": "优先于 scenario 映射到 storyDefaults.world。"},
        {"key": "supportingCast", "label": "配角", "group": "hds", "type": "textarea", "rows": 3},
        {"key": "location", "label": "主要地点", "group": "hds", "type": "text"},
        {"key": "style", "label": "故事级文风", "group": "hds", "type": "textarea", "rows": 3},
        {"key": "timezone", "label": "时区", "group": "hds", "type": "text", "placeholder": "Asia/Shanghai"},
    ],
    "lorebook": [
        {"key": "name", "label": "世界书名称", "group": "book", "type": "text", "required": True},
        {"key": "description", "label": "说明", "group": "book", "type": "textarea", "rows": 2},
        {"key": "scan_depth", "label": "扫描深度", "group": "book", "type": "int", "hint": "向后扫描多少条上下文用于关键词匹配。"},
        {"key": "token_budget", "label": "注入字符预算", "group": "book", "type": "int", "hint": "激活条目注入提示词的最大字符数。"},
    ],
    "entry": [
        {"key": "comment", "label": "条目名", "group": "entry", "type": "text"},
        {"key": "keys", "label": "关键词", "group": "entry", "type": "tags", "hint": "命中任一关键词即触发（常量条目忽略）。"},
        {"key": "secondary_keys", "label": "次要关键词", "group": "entry", "type": "tags"},
        {"key": "content", "label": "内容", "group": "entry", "type": "textarea", "rows": 8, "required": True},
        {"key": "constant", "label": "常驻", "group": "entry", "type": "bool", "hint": "每回合都注入，不依赖关键词。"},
        {"key": "selective", "label": "启用次要关键词", "group": "entry", "type": "bool"},
        {"key": "selectiveLogic", "label": "次要关键词逻辑", "group": "entry", "type": "select", "options": [
            {"value": 0, "label": "任一命中 (AND ANY)"},
            {"value": 1, "label": "并非全部命中 (NOT ALL)"},
            {"value": 2, "label": "全不命中 (NOT ANY)"},
            {"value": 3, "label": "全部命中 (AND ALL)"},
        ]},
        {"key": "insertion_order", "label": "插入顺序", "group": "entry", "type": "int"},
        {"key": "position", "label": "注入位置", "group": "entry", "type": "select", "options": [
            {"value": "before_char", "label": "角色设定之前"},
            {"value": "after_char", "label": "角色设定之后"},
        ]},
        {"key": "probability", "label": "触发概率 %", "group": "entry", "type": "int"},
        {"key": "case_sensitive", "label": "区分大小写", "group": "entry", "type": "bool"},
        {"key": "match_whole_words", "label": "全词匹配", "group": "entry", "type": "bool"},
        {"key": "use_regex", "label": "正则匹配", "group": "entry", "type": "bool"},
        {"key": "depth", "label": "深度", "group": "entry", "type": "int"},
        {"key": "group", "label": "分组", "group": "entry", "type": "text"},
    ],
    "prompts": [
        {"key": "stylePrompt", "label": "全局文风", "group": "narrative", "type": "textarea", "rows": 5, "hint": "对应 model.stylePrompt；留空回退配置默认。"},
        {"key": "mainPrompt", "label": "主叙事方向", "group": "narrative", "type": "textarea", "rows": 5, "hint": "对应 model.mainPrompt。"},
        {"key": "fixedPrompt", "label": "长期固定约束", "group": "narrative", "type": "textarea", "rows": 4, "hint": "对应 model.fixedPrompt。"},
        {"key": "formatPrompt", "label": "输出格式补充", "group": "narrative", "type": "textarea", "rows": 3, "hint": "对应 model.formatPrompt，不能移除 JSON 合约。"},
        {"key": "mainPromptOverride", "label": "顶层主提示词覆盖", "group": "advanced", "type": "textarea", "rows": 5, "hint": "优先级高于主叙事方向；留空使用内置默认。"},
        {"key": "compactionMainPrompt", "label": "压缩指令", "group": "advanced", "type": "textarea", "rows": 4},
        {"key": "compactionFixedPrompt", "label": "压缩固定规则", "group": "advanced", "type": "textarea", "rows": 3},
        {"key": "compactionStylePrompt", "label": "压缩文风", "group": "advanced", "type": "textarea", "rows": 3},
        {"key": "alterPrompt", "label": "情绪侧端附加要求", "group": "advanced", "type": "textarea", "rows": 3},
    ],
}


class StudioError(Exception):
    """Action 失败，带可读消息与 HTTP 语义状态。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


class StudioAPI:
    def __init__(
        self,
        store: ContentStore,
        *,
        story_lister: Optional[Callable[[], Awaitable[List[Dict[str, Any]]]]] = None,
        story_applier: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        share_fetcher: Callable[[str, str], Awaitable[Dict[str, Any]]] = fetch_share,
        default_base: Optional[str] = None,
        logger: Any = None,
    ) -> None:
        self.store = store
        self._story_lister = story_lister
        self._story_applier = story_applier
        self._share_fetcher = share_fetcher
        self._default_base = default_base or default_share_base()
        self._logger = logger

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def _log(self, level: str, message: str) -> None:
        if callable(self._logger):
            try:
                self._logger(level, message)
            except Exception:
                pass

    def _character_summary(self, card: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": card.get("id") or "",
            "name": card.get("name") or "",
            "description": (card.get("description") or "")[:160],
            "tags": list(card.get("tags") or []),
            "avatar": card.get("avatar") or "",
            "source": card.get("source") or {},
            "updatedAt": card.get("updatedAt") or "",
            "hasWorld": bool((card.get("hds") or {}).get("world") or card.get("scenario")),
        }

    def _lorebook_summary(self, book: Dict[str, Any]) -> Dict[str, Any]:
        entries = book.get("entries") or []
        return {
            "id": book.get("id") or "",
            "name": book.get("name") or "",
            "description": (book.get("description") or "")[:160],
            "entryCount": len(entries),
            "enabledCount": len([item for item in entries if item.get("enabled", True)]),
            "constantCount": len([item for item in entries if item.get("constant")]),
            "budget": book.get("token_budget") or DEFAULT_WORLD_BOOK_BUDGET,
            "updatedAt": book.get("updatedAt") or "",
            "source": book.get("source") or {},
        }

    async def state(self) -> Dict[str, Any]:
        characters = [self._character_summary(item) for item in self.store.characters()]
        lorebooks = [self._lorebook_summary(item) for item in self.store.lorebooks()]
        stories: List[Dict[str, Any]] = []
        if self._story_lister:
            try:
                stories = await self._story_lister()
            except Exception as error:  # noqa: BLE001
                self._log("warn", f"读取故事列表失败：{error}")
        return {
            "characters": characters,
            "lorebooks": lorebooks,
            "active": self.store.active(),
            "bindings": self.store.bindings(),
            "prompts": self.store.prompts(),
            "stories": stories,
            "stats": self.store.stats(),
            "catalog": FIELD_CATALOG,
            "meta": {
                "defaultShareBase": self._default_base,
                "promptKeys": list(PROMPT_KEYS),
                "worldBookBudget": DEFAULT_WORLD_BOOK_BUDGET,
                "schemaVersion": 1,
            },
        }

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    async def dispatch(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        payload = payload if isinstance(payload, dict) else {}
        handler = getattr(self, f"action_{action.replace('.', '_')}", None)
        if handler is None:
            raise StudioError(f"未知操作：{action}", 404)
        try:
            return await handler(payload)
        except StudioError:
            raise
        except ContentError as error:
            raise StudioError(str(error), 400) from error
        except ValueError as error:
            raise StudioError(str(error), 400) from error

    # -- 角色 ------------------------------------------------------------ #
    async def action_state_get(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.state()

    async def action_character_get(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        card = self.store.character(_text(payload.get("id")))
        if not card:
            raise StudioError("角色不存在", 404)
        return {"character": card}

    async def action_character_save(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload.get("character")
        if not isinstance(raw, dict):
            raise StudioError("缺少角色卡数据")
        card = self.store.save_character(raw)
        self._log("info", f"WebUI 保存角色：{card.get('name')}（{card.get('id')}）")
        return {"character": card, "state": await self.state()}

    async def action_character_delete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        character_id = _text(payload.get("id"))
        if not self.store.delete_character(character_id):
            raise StudioError("角色不存在", 404)
        return {"deleted": True, "state": await self.state()}

    async def action_character_duplicate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        card = self.store.duplicate_character(_text(payload.get("id")))
        if not card:
            raise StudioError("角色不存在", 404)
        return {"character": card, "state": await self.state()}

    async def action_character_activate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        active = self.store.set_active(
            _text(payload.get("characterId")),
            payload.get("lorebookIds") if isinstance(payload.get("lorebookIds"), list) else None,
        )
        return {"active": active, "state": await self.state()}

    # -- 世界书 ---------------------------------------------------------- #
    async def action_lorebook_get(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        book = self.store.lorebook(_text(payload.get("id")))
        if not book:
            raise StudioError("世界书不存在", 404)
        return {"lorebook": book}

    async def action_lorebook_save(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload.get("lorebook")
        if not isinstance(raw, dict):
            raise StudioError("缺少世界书数据")
        book = self.store.save_lorebook(raw)
        self._log("info", f"WebUI 保存世界书：{book.get('name')}（{len(book.get('entries') or [])} 条）")
        return {"lorebook": book, "state": await self.state()}

    async def action_lorebook_delete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.store.delete_lorebook(_text(payload.get("id"))):
            raise StudioError("世界书不存在", 404)
        return {"deleted": True, "state": await self.state()}

    async def action_lorebook_duplicate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        book = self.store.lorebook(_text(payload.get("id")))
        if not book:
            raise StudioError("世界书不存在", 404)
        book["id"] = ""
        book["name"] = f"{book.get('name') or '未命名世界书'} 副本"
        saved = self.store.save_lorebook(book)
        return {"lorebook": saved, "state": await self.state()}

    # -- 绑定 ------------------------------------------------------------ #
    async def action_binding_save(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        story_id = _text(payload.get("storyId"))
        if not story_id:
            raise StudioError("缺少 storyId")
        binding = self.store.bind_story(
            story_id,
            _text(payload.get("characterId")),
            payload.get("lorebookIds") if isinstance(payload.get("lorebookIds"), list) else None,
        )
        return {"binding": binding, "state": await self.state()}

    async def action_binding_delete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.store.unbind_story(_text(payload.get("storyId"))):
            raise StudioError("绑定不存在", 404)
        return {"deleted": True, "state": await self.state()}

    async def action_prompts_save(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prompts = payload.get("prompts")
        if not isinstance(prompts, dict):
            raise StudioError("缺少 prompts 数据")
        saved = self.store.save_prompts(prompts)
        return {"prompts": saved, "state": await self.state()}

    # -- 预览与故事 ------------------------------------------------------ #
    async def action_preview_prompt(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_card = payload.get("character")
        card = normalize_character_card(raw_card) if isinstance(raw_card, dict) else None
        return self.store.prompt_preview(
            character_id=_text(payload.get("characterId")),
            card=card,
            lorebook_ids=payload.get("lorebookIds") if isinstance(payload.get("lorebookIds"), list) else [],
            query=_text(payload.get("query")),
            setting=payload.get("setting") if isinstance(payload.get("setting"), dict) else {},
        )

    async def action_story_list(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._story_lister:
            return {"stories": []}
        return {"stories": await self._story_lister()}

    async def action_story_apply(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._story_applier:
            raise StudioError("当前环境不支持写入故事设定", 501)
        story_id = _text(payload.get("storyId"))
        card = self.store.character(_text(payload.get("characterId"))) or self.store.active_character()
        if not card:
            raise StudioError("请先选择角色")
        if not story_id:
            raise StudioError("缺少 storyId")
        patch = story_setting_patch_from_card(card)
        await self._story_applier(story_id, patch)
        self.store.bind_story(
            story_id,
            card.get("id") or "",
            payload.get("lorebookIds") if isinstance(payload.get("lorebookIds"), list) else None,
        )
        self._log("info", f"WebUI 将角色「{card.get('name')}」应用到故事 {story_id}")
        return {"applied": True, "patch": patch, "state": await self.state()}

    # -- Type0 ----------------------------------------------------------- #
    async def _fetch_type0(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        code = _text(payload.get("code"))
        base = _text(payload.get("baseUrl")) or self._default_base
        if not code:
            raise StudioError("请填写 Type0 分享码或链接")
        try:
            info = await self._share_fetcher(base, code)
        except ValueError as error:
            raise StudioError(f"Type0 获取失败：{error}", 502) from error
        except Exception as error:  # noqa: BLE001 - 网络层异常统一转可读错误
            raise StudioError(f"Type0 获取失败：{error}", 502) from error
        if not isinstance(info, dict) or not isinstance(info.get("payload"), dict):
            raise StudioError("Type0 返回结构无效：缺少 payload", 502)
        return info

    def _type0_preview(self, info: Dict[str, Any], base_url: str) -> Dict[str, Any]:
        card = card_from_type0(info, base_url=base_url)
        lorebook = lorebook_from_type0(info)
        warnings: List[str] = []
        payload = info.get("payload") or {}
        defaults = _type0_story_defaults(payload if isinstance(payload, dict) else {})
        if not defaults:
            warnings.append("Type0 预设没有 storyDefaults 分区，已仅用标题与作者信息生成角色卡。")
        if not card.get("description"):
            warnings.append("预设未提供 characterProfile，角色描述为空，建议导入后补充。")
        if not (card.get("hds") or {}).get("world") and not card.get("scenario"):
            warnings.append("预设未提供 world，世界设定为空。")
        if lorebook is None:
            warnings.append("该 Type0 预设不含世界书分区；导入后可手动创建世界书并绑定。")
        return {
            "title": info.get("title") or "未命名预设",
            "code": info.get("code") or "",
            "revision": int(info.get("revision") or 0),
            "sections": info.get("sections") or [],
            "card": card,
            "lorebook": lorebook,
            "mapping": type0_mapping_rows(card, info),
            "warnings": warnings,
            "summary": {
                "characterName": card.get("name") or "",
                "descriptionLength": len(card.get("description") or ""),
                "perspectives": len((card.get("hds") or {}).get("perspectives") or []),
                "supplementaryFacts": len((card.get("hds") or {}).get("supplementaryFacts") or []),
                "worldLength": len((card.get("hds") or {}).get("world") or ""),
                "lorebookEntries": len((lorebook or {}).get("entries") or []),
            },
        }

    async def action_type0_preview(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        info = await self._fetch_type0(payload)
        preview = self._type0_preview(info, _text(payload.get("baseUrl")) or self._default_base)
        preview["rawSections"] = list((info.get("payload") or {}).keys())
        return preview

    async def action_type0_import(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        info = await self._fetch_type0(payload)
        base = _text(payload.get("baseUrl")) or self._default_base
        preview = self._type0_preview(info, base)
        card = preview["card"]
        saved = self.store.save_character(card)
        book = preview.get("lorebook")
        saved_book = self.store.save_lorebook(book) if isinstance(book, dict) else None
        if payload.get("activate") is not False:
            lorebook_ids = [saved_book["id"]] if saved_book else []
            self.store.set_active(saved["id"], lorebook_ids)
        self._log(
            "info",
            f"Type0 导入角色「{saved.get('name')}」：code={info.get('code')} revision={info.get('revision')}",
        )
        return {
            "character": saved,
            "lorebook": saved_book,
            "warnings": preview.get("warnings") or [],
            "state": await self.state(),
        }

    # -- 导入 / 导出 ----------------------------------------------------- #
    async def action_import_character(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        filename = _text(payload.get("filename")) or "character.json"
        encoded = _text(payload.get("contentBase64"))
        if not encoded:
            raise StudioError("缺少文件内容")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise StudioError("文件不是有效的 Base64 数据") from error
        if len(raw) > MAX_IMPORT_BYTES:
            raise StudioError("文件超过 8 MB 上限", 413)
        card_data: Optional[Dict[str, Any]] = None
        if raw.startswith(b"\x89PNG"):
            card_data = read_png_card(raw)
            if card_data is None:
                raise StudioError("PNG 中未找到 chara/ccv3 角色卡数据块")
        else:
            try:
                card_data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise StudioError("无法解析角色卡：需要 JSON 或酒馆 PNG") from error
            if isinstance(card_data, dict) and isinstance(card_data.get("data"), dict):
                card_data = card_data["data"]
        card = normalize_character_card(card_data, source_type="sillytavern")
        saved = self.store.save_character(card)
        self._log("info", f"导入角色卡：{filename} → {saved.get('name')}")
        return {"character": saved, "filename": filename, "state": await self.state()}

    async def action_import_lorebook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        filename = _text(payload.get("filename")) or "worldbook.json"
        encoded = _text(payload.get("contentBase64"))
        if not encoded:
            raise StudioError("缺少文件内容")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise StudioError("文件不是有效的 Base64 数据") from error
        if len(raw) > MAX_IMPORT_BYTES:
            raise StudioError("文件超过 8 MB 上限", 413)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise StudioError("无法解析世界书 JSON") from error
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        book = normalize_lorebook(data, name_fallback=filename.rsplit(".", 1)[0])
        saved = self.store.save_lorebook(book)
        self._log("info", f"导入世界书：{filename} → {saved.get('name')}")
        return {"lorebook": saved, "filename": filename, "state": await self.state()}

    async def action_export_character(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        card = self.store.character(_text(payload.get("id")))
        if not card:
            raise StudioError("角色不存在", 404)
        content = json.dumps(card_to_sillytavern_v2(card), ensure_ascii=False, indent=2)
        return {
            "filename": f"{_safe_filename(card.get('name'))}.json",
            "content": content,
        }

    async def action_export_character_png(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        card = self.store.character(_text(payload.get("id")))
        if not card:
            raise StudioError("角色不存在", 404)
        try:
            png = card_png(card)
        except ContentError as error:
            raise StudioError(str(error), 400) from error
        return {
            "filename": f"{_safe_filename(card.get('name'))}.png",
            "contentBase64": base64.b64encode(png).decode("ascii"),
        }

    async def action_export_lorebook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        book = self.store.lorebook(_text(payload.get("id")))
        if not book:
            raise StudioError("世界书不存在", 404)
        content = json.dumps(lorebook_to_world_info(book), ensure_ascii=False, indent=2)
        return {
            "filename": f"{_safe_filename(book.get('name'))}.worldbook.json",
            "content": content,
        }


def _safe_filename(value: Any) -> str:
    text = str(value or "character").strip() or "character"
    text = re.sub(r'[\\/:*?"<>|\s]+', "_", text)
    return text[:60].strip("_") or "character"
