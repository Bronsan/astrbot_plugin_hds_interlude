"""服务编排层：故事、参与者、队列、持久化、投递与后台任务。

对应上游 `src/service.ts`（11k 行的编排层）的核心闭环：
私聊/群聊接入 → 短时合并 → 上下文装配 → 主叙事写作 → 落库与投递 →
自动生活推进 → 记忆压缩 → 到期意图与主动联系。

模块分三段组织（本文件按顺序包含全部内容）：
1) 基础：会话视图、存储绑定、权限与查询；
2) 回合管线：接收、合并、写作、落库、投递；
3) 后台与管理：推进、压缩、调度扫描、全部管理命令支撑。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import random
import re
import socket
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import agency as agency_mod
from . import alter as alter_mod
from . import group_willingness as willingness_mod
from . import schedule_preplan as preplan_mod
from . import urge as urge_mod
from .database import Database
from .narrator import (
    ModelCallError,
    NarrativeResult,
    Narrator,
)
from .scheduler import Scheduler
from .script_engine import (
    clip,
    consumed_live_intent_ids,
    continuation_bookmark,
    create_script_delivery_actions,
    decision_to_script_commit,
    delivery_reality_projection,
    delivery_segments,
    live_narrative_intents,
    merge_participant_state,
    merge_setting,
    normalize_participant_state,
    normalize_scene_presence_drafts,
    participant_relevance,
    sanitize_and_clamp_visible_content,
    split_visible_reply_bubbles,
    update_script_delivery_actions,
    visible_reply_mode,
)
from .timeutil import (
    calendar_day_key,
    in_clock_window,
    local_clock_minutes,
    parse_hhmm,
    resolve_timezone,
)
from .turn_engine import TurnEngine, format_buffered_user_messages
from .types import (
    DEFAULT_MESSAGE_SEPARATOR,
    Arc,
    DeliveryUnknown,
    Fact,
    Intent,
    Memory,
    NarrativeAudio,
    NarrativeImage,
    NarrativeRequest,
    OverlaySnapshot,
    Participant,
    Scene,
    ScriptEntry,
    StatePatch,
    Story,
    TimelinePlan,
    empty_participant_state,
    empty_story_setting,
    empty_story_state,
    encode_story_state,
    ensure_utc,
    json_loads,
    normalize_story_state,
    now_utc,
    stable_suffix,
    to_iso,
)

#: 私有会话视图：把平台事件抽象成服务层需要的字段（对应 Koishi session 的最小面）。
TRIGGER_PREFIX = re.compile(r"^[!/.]?interlude(?:\.|\s|$)", re.IGNORECASE)


def _pick(value: Any, default: Any) -> Any:
    """None 才用默认值：保留 0 / False 等合法配置（避免 `or` 误伤）。"""
    return default if value is None else value


class _HTMLTextExtractor(HTMLParser):
    """从公开网页提取标题与正文（跳过脚本/样式，保留段落分隔）。"""

    _BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: List[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def first_search_result(source: str, base_url: str) -> str:
    """从搜索结果页取第一个外链（兼容 DuckDuckGo 的 uddg 重定向）。"""
    for raw in re.findall(r'<a[^>]+href="([^"]+)"', source or "", flags=re.I):
        target = unescape(raw)
        if target.startswith("//"):
            target = "https:" + target
        elif target.startswith("/"):
            target = urljoin(base_url, target)
        if not target.startswith("http"):
            continue
        parsed = urlparse(target)
        host = (parsed.hostname or "").lower()
        if "duckduckgo.com" in host or "bing.com" in host or "google.com" in host:
            uddg = parse_qs(parsed.query).get("uddg")
            if uddg:
                target = unquote(uddg[0])
            else:
                continue
        return target
    return ""


def parse_html_page(source: str) -> Dict[str, str]:
    """解析网页为 `{title, text}`（纯标准库，失败时退化为空）。"""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(source or "")
    except Exception:  # noqa: BLE001 - 异常 HTML 也要尽量给出正文
        pass
    return {"title": unescape(parser.title).strip(), "text": parser.text()}


def _meta_text(metadata: Any, key: str) -> str:
    """从条目 metadata（可能是 JSON 文本）里取一个字符串字段。"""
    if isinstance(metadata, str):
        metadata = json_loads(metadata, {})
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get(key)
    return str(value) if value else ""


@dataclass
class SessionView:
    """一次入站事件的平台无关视图。"""

    platform: str
    selfId: str
    userId: str
    channelId: str
    isDirect: bool
    content: str
    senderName: str = ""
    groupId: str = ""
    messageId: str = ""
    umo: str = ""
    imageSources: List[str] = field(default_factory=list)
    audioSources: List[str] = field(default_factory=list)
    quote: Optional[Dict[str, str]] = None
    atBot: bool = False
    rawEvent: Any = None

    def key(self) -> str:
        """入站去重键（对应 seenIncomingMessages 的 key）。"""
        return "|".join([
            self.platform, self.selfId, self.userId, self.channelId, self.messageId or self.content[:64]
        ])


def is_interlude_command_like(content: str) -> bool:
    """消息看起来像 HDSI 管理命令（对应 looksLikeInterludeCommand）。"""
    return bool(TRIGGER_PREFIX.match((content or "").strip()))


def normalize_interlude_spacing(content: str, is_registered: Callable[[str], bool]) -> Optional[str]:
    """把 `interlude memory facts` 空格写法重写为点分形式（对应 normalizeInterludeSpacing）。"""
    trimmed = (content or "").strip()
    match = re.match(r"^([/!.]?)(interlude)(\s+)(\S[\s\S]*)$", trimmed)
    if not match:
        return None
    tokens = ("interlude " + match.group(4)).split()
    for size in range(min(len(tokens), 4), 1, -1):
        name = ".".join(tokens[:size])
        if not is_registered(name):
            continue
        rest = " ".join(tokens[size:])
        return f"{match.group(1)}{name}{(' ' + rest) if rest else ''}"
    return None


def narrative_turn_count(rows: Sequence[Dict[str, Any]]) -> int:
    """独立叙事回合数（对应 rc57 `developmentTurns`）。

    只有 `script` 条目（或 actor=narrator）算叙事回合；同一回合内的多条
    用户消息/意图不会重复计数。上游用 `occurredAt` 毫秒时间戳去重，
    这里保持一致。
    """
    keys = set()
    for row in rows:
        if row.get("kind") != "script" and row.get("actor") != "narrator":
            continue
        moment = ensure_utc(row.get("occurredAt"))
        if moment is not None:
            keys.add(moment.timestamp())
    return len(keys)


def state_patch_evidence(rows: Sequence[Dict[str, Any]], timezone: str) -> Dict[str, int]:
    """状态演化证据（对应 rc57 `statePatchEvidence`）。

    - turns：独立叙事回合数（不是参与者数）；
    - days：故事本地日历日数；
    - scenes：来源条目所属场景数（本移植在 script.metadata.sceneId 记录；
      旧数据没有 sceneId 时按 1 个场景保守处理）。
    """
    narrative = [
        row for row in rows
        if row.get("kind") == "script" or row.get("actor") == "narrator"
    ]
    days = set()
    scenes = set()
    for row in narrative:
        moment = ensure_utc(row.get("occurredAt"))
        if moment is not None:
            days.add(calendar_day_key(moment, timezone))
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            metadata = json_loads(metadata, {})
        if isinstance(metadata, dict) and metadata.get("sceneId") is not None:
            scenes.add(str(metadata["sceneId"]))
    return {
        "turns": narrative_turn_count(narrative),
        "days": len(days),
        "scenes": max(1, len(scenes)) if narrative else 0,
    }


class InterludeService:
    """HDSI 运行时服务（对应上游 InterludeService）。"""

    def __init__(
        self,
        config_getter: Callable[[], Dict[str, Any]],
        data_dir: str,
        logger: Callable[[str, str], None],
        transport: Any,
        content_store: Any = None,
    ) -> None:
        self._config_getter = config_getter
        self._log = logger
        self._transport = transport
        self.content_store = content_store
        self.db = Database(f"{data_dir}/hdsi.db")
        self.data_dir = data_dir
        self.scheduler = Scheduler(
            on_wake_error=lambda error: self._log("warn", f"唤醒任务失败：{error}"),
            on_task_error=lambda error: self._log("warn", f"后台任务失败：{error}"),
        )
        self.turn_engine = TurnEngine(
            user_message_debounce_seconds=lambda: float(self.runtime.get("userMessageDebounceSeconds") or 2),
            schedule_flush=lambda key, revision: asyncio.ensure_future(self.flush_buffered_narrative(key, revision)),
            report=lambda *args, **kwargs: self._log("debug", "turn-engine"),
        )
        self.narrator = Narrator(model_config={}, on_log=lambda channel, message: self._log("info", message))
        self._story_locks: Dict[str, asyncio.Lock] = {}
        self._seen_incoming: Dict[str, float] = {}
        self._buffered_group_turns: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._group_flush_locks: Dict[str, asyncio.Lock] = {}
        self._runtime_generation = 0
        self._paused = False
        self._pending_retries: Dict[str, Dict[str, Any]] = {}
        self._interrupted_typing: Set[str] = set()
        self._background_task: Optional[asyncio.Task] = None
        self._last_sweep_at: float = 0.0
        self._last_compaction_at: float = 0.0
        self._last_weather_at: float = 0.0
        self._last_seeder_at: float = 0.0
        self._story_state_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._group_willingness_memory: Dict[str, Dict[str, Any]] = {}
        self._closed = False
        self._health = {
            "sinceAt": to_iso(now_utc()),
            "narrativeTotal": 0,
            "narrativeFailed": 0,
            "structureMissing": 0,
            "recoverySaved": 0,
            "replyModes": {"immediate": 0, "none": 0, "delayed": 0, "noDelivery": 0},
            "inputTokens": 0,
            "cachedTokens": 0,
            "latencies": [],
        }

    # ------------------------------------------------------------------ #
    # 配置视图
    # ------------------------------------------------------------------ #

    @property
    def config(self) -> Dict[str, Any]:
        return self._config_getter() or {}

    @property
    def runtime(self) -> Dict[str, Any]:
        return self.config.get("runtime") or {}

    @property
    def story_defaults(self) -> Dict[str, Any]:
        return self.config.get("storyDefaults") or {}

    @property
    def onebot(self) -> Dict[str, Any]:
        return self.config.get("onebot") or {}

    @property
    def shared_story(self) -> Dict[str, Any]:
        return self.config.get("sharedStory") or {}

    @property
    def memory_config(self) -> Dict[str, Any]:
        return self.config.get("memory") or {}

    def refresh_runtime(self) -> None:
        """配置变化后刷新派生物（模型连接等）。"""
        self.narrator = Narrator(model_config=self.config.get("model") or {}, on_log=self._log)
        self.runtime_generation_bump()

    def runtime_generation_bump(self) -> None:
        self._runtime_generation += 1

    @property
    def health(self) -> Dict[str, Any]:
        """健康指标快照（对应 interlude.status 的健康段）。"""
        data = dict(self._health)
        total = data["narrativeTotal"] + data["narrativeFailed"]
        data["successRate"] = (data["narrativeTotal"] / total) if total else 1.0
        latencies = sorted(data.pop("latencies") or [])
        data["medianLatencyMs"] = latencies[len(latencies) // 2] if latencies else 0
        data["cacheHitRate"] = (data["cachedTokens"] / data["inputTokens"]) if data["inputTokens"] else 0.0
        return data

    # ------------------------------------------------------------------ #
    # 存储辅助
    # ------------------------------------------------------------------ #

    def _lock(self, story_id: str) -> asyncio.Lock:
        if story_id not in self._story_locks:
            self._story_locks[story_id] = asyncio.Lock()
        return self._story_locks[story_id]

    def in_serial(self, story_id: str) -> bool:
        lock = self._story_locks.get(story_id)
        return bool(lock and lock.locked())

    async def serial(self, story_id: str, task: Callable[[], Awaitable[Any]]) -> Any:
        """故事级串行化（对应上游 serial）。"""
        async with self._lock(story_id):
            return await task()

    # ---- 行 → dataclass ----

    @staticmethod
    def _row_to_story(row: Dict[str, Any]) -> Story:
        return Story(
            id=row["id"],
            platform=row.get("platform") or "",
            selfId=row.get("selfId") or "",
            userId=row.get("userId") or "",
            channelId=row.get("channelId") or "",
            status=row.get("status") or "active",
            setting=json_loads(row.get("setting"), empty_story_setting()),
            state=normalize_story_state(row.get("state")),
            cursorAt=ensure_utc(row.get("cursorAt")) or now_utc(),
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
        )

    @staticmethod
    def _row_to_participant(row: Dict[str, Any]) -> Participant:
        return Participant(
            id=row["id"],
            storyId=row.get("storyId") or "",
            platform=row.get("platform") or "",
            selfId=row.get("selfId") or "",
            userId=row.get("userId") or "",
            channelId=row.get("channelId") or "",
            personId=row.get("personId") or "",
            displayName=row.get("displayName") or "",
            profile=row.get("profile") or "",
            relationship=row.get("relationship") or "",
            state=normalize_participant_state(row.get("state")),
            status=row.get("status") or "active",
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
        )

    @staticmethod
    def _row_to_entry(row: Dict[str, Any]) -> ScriptEntry:
        return ScriptEntry(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            participantId=row.get("participantId") or "",
            kind=row.get("kind") or "",
            actor=row.get("actor") or "",
            content=row.get("content") or "",
            occurredAt=ensure_utc(row.get("occurredAt")) or now_utc(),
            metadata=json_loads(row.get("metadata"), {}) or {},
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            embedding=row.get("embedding") if isinstance(row.get("embedding"), list) else None,
        )

    @staticmethod
    def _row_to_fact(row: Dict[str, Any]) -> Fact:
        return Fact(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            participantId=row.get("participantId") or "",
            scope=row.get("scope") or "event",
            content=row.get("content") or "",
            importance=float(row.get("importance") or 0.5),
            confidence=float(row.get("confidence") or 0.8),
            unresolved=bool(row.get("unresolved")),
            status=row.get("status") or "active",
            sourceEntryIds=[int(item) for item in (json_loads(row.get("sourceEntryIds"), []) or []) if isinstance(item, (int, float))],
            lastSeenAt=ensure_utc(row.get("lastSeenAt")) or now_utc(),
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
            embedding=row.get("embedding") if isinstance(row.get("embedding"), list) else None,
            knowledge=json_loads(row.get("knowledge"), None),
        )

    @staticmethod
    def _row_to_memory(row: Dict[str, Any]) -> Memory:
        return Memory(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            participantId=row.get("participantId") or "",
            category=row.get("category") or "",
            content=row.get("content") or "",
            importance=float(row.get("importance") or 0.5),
            status=row.get("status") or "active",
            sourceEntryId=int(row["sourceEntryId"]) if row.get("sourceEntryId") is not None else None,
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
        )

    @staticmethod
    def _row_to_intent(row: Dict[str, Any]) -> Intent:
        return Intent(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            participantId=row.get("participantId") or "",
            type=row.get("type") or "",
            summary=row.get("summary") or "",
            notBefore=ensure_utc(row.get("notBefore")) or now_utc(),
            status=row.get("status") or "pending",
            payload=json_loads(row.get("payload"), {}) or {},
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
        )

    @staticmethod
    def _row_to_patch(row: Dict[str, Any]) -> StatePatch:
        return StatePatch(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            participantId=row.get("participantId") or "",
            target=row.get("target") or "",
            path=row.get("path") or "",
            proposedValue=row.get("proposedValue") or "",
            evidence=row.get("evidence") or "",
            confidence=float(row.get("confidence") or 0.0),
            impact=row.get("impact") or "minor",
            status=row.get("status") or "proposed",
            sourceEntryIds=[int(item) for item in (json_loads(row.get("sourceEntryIds"), []) or []) if isinstance(item, (int, float))],
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            appliedAt=ensure_utc(row.get("appliedAt")),
        )

    @staticmethod
    def _row_to_snapshot(row: Dict[str, Any]) -> OverlaySnapshot:
        return OverlaySnapshot(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            participantId=row.get("participantId") or "",
            target=row.get("target") or "",
            tier=row.get("tier") or "weekly",
            periodStart=ensure_utc(row.get("periodStart")) or now_utc(),
            periodEnd=ensure_utc(row.get("periodEnd")) or now_utc(),
            summary=row.get("summary") or "",
            majorEvents=json_loads(row.get("majorEvents"), []) or [],
            sourcePatchIds=[int(item) for item in (json_loads(row.get("sourcePatchIds"), []) or []) if isinstance(item, (int, float))],
            status=row.get("status") or "active",
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
        )

    @staticmethod
    def _row_to_scene(row: Dict[str, Any]) -> Scene:
        return Scene(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            status=row.get("status") or "active",
            startedAt=ensure_utc(row.get("startedAt")) or now_utc(),
            endedAt=ensure_utc(row.get("endedAt")),
            hook=row.get("hook") or "",
            summary=row.get("summary") or "",
            entryCount=int(row.get("entryCount") or 0),
            lastEntryId=int(row["lastEntryId"]) if row.get("lastEntryId") is not None else None,
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
        )

    @staticmethod
    def _row_to_arc(row: Dict[str, Any]) -> Arc:
        return Arc(
            id=int(row["id"]),
            storyId=row.get("storyId") or "",
            status=row.get("status") or "active",
            title=row.get("title") or "",
            summary=row.get("summary") or "",
            sceneCount=int(row.get("sceneCount") or 0),
            createdAt=ensure_utc(row.get("createdAt")) or now_utc(),
            updatedAt=ensure_utc(row.get("updatedAt")) or now_utc(),
        )

    # ---- 故事状态读写 ----

    async def get_story(self, story_id: str) -> Optional[Story]:
        row = self.db.first_row("interlude_story", {"id": story_id})
        return self._row_to_story(row) if row else None

    async def list_story_summaries(self, limit: int = 30) -> List[Dict[str, Any]]:
        """WebUI 绑定页用的故事列表（只读摘要，不含剧本正文）。"""
        rows = self.db.list_rows(
            "interlude_story",
            {"status": {"$ne": "archived"}},
            order_by="updatedAt DESC",
            limit=max(1, min(int(limit or 30), 100)),
        )
        summaries: List[Dict[str, Any]] = []
        for row in rows:
            story = self._row_to_story(row)
            character = (story.setting.get("character") or {}) if isinstance(story.setting, dict) else {}
            summaries.append(
                {
                    "id": story.id,
                    "label": str(character.get("name") or "未命名故事"),
                    "platform": story.platform,
                    "status": story.status,
                    "updatedAt": to_iso(story.updatedAt),
                }
            )
        return summaries

    async def get_participant(self, participant_id: str) -> Optional[Participant]:
        row = self.db.first_row("interlude_participant", {"id": participant_id})
        return self._row_to_participant(row) if row else None

    async def participants(self, story_id: str) -> List[Participant]:
        rows = self.db.list_rows("interlude_participant", {"storyId": story_id}, order_by="updatedAt DESC")
        return [self._row_to_participant(row) for row in rows]

    async def _update_story_state(self, story_id: str, mutate: Callable[[Dict[str, Any]], Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
        """读-改-写故事状态（故事队列内调用）。"""
        story = await self.get_story(story_id)
        if story is None:
            return {}
        state = mutate(story.state)
        self.db.update_rows(
            "interlude_story",
            {"id": story_id},
            {"state": encode_story_state(state), "updatedAt": now or now_utc()},
        )
        return state

    def _story_state(self, story: Story) -> Dict[str, Any]:
        return story.state

    # ------------------------------------------------------------------ #
    # 白名单与权限
    # ------------------------------------------------------------------ #

    def _object_list(self, section: str, key: str, json_key: str) -> List[Dict[str, Any]]:
        """读取"对象行列表"配置。

        AstrBot 配置页把对象行列表退化为字符串数组，这里兼容：
        - dict 行直接使用；
        - JSON 字符串行解析后使用；
        - <json_key> 文本域（整段 JSON 数组）优先级最高。
        """
        source = self.config.get(section) or {}
        raw_json = source.get(json_key)
        if isinstance(raw_json, str) and raw_json.strip():
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, list):
                    rows = [row for row in parsed if isinstance(row, dict)]
                    if rows:
                        return rows
            except ValueError:
                pass
        rows = source.get(key)
        if not isinstance(rows, list):
            return []
        result: List[Dict[str, Any]] = []
        for row in rows:
            if isinstance(row, str):
                text = row.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except ValueError:
                    continue
            if isinstance(row, dict):
                result.append(row)
        return result

    def _user_accounts(self) -> List[Dict[str, Any]]:
        return self._object_list("onebot", "userAccounts", "userAccountsJson")

    def _group_chats(self) -> List[Dict[str, Any]]:
        return self._object_list("onebot", "groupChats", "groupChatsJson")

    def _rest_windows(self) -> List[Dict[str, Any]]:
        """休息窗口列表（含上游默认值：夜间 23:00-07:00）。"""
        windows = self._object_list("runtime", "restWindows", "restWindowsJson")
        if windows:
            return windows
        return [{
            "enabled": True,
            "label": "night sleep",
            "start": "23:00",
            "end": "07:00",
            "minIntervalMinutes": 120,
            "maxIntervalMinutes": 240,
        }]

    def can_handle_session(self, session: SessionView) -> bool:
        """互动白名单检查（对应 canHandleSession）。

        onebot 白名单只约束 OneBot 平台会话（按上游语义：启用后空白名单
        拒绝对应账号）；其它平台适配器不受此名单影响。
        """
        onebot = self.onebot
        if not onebot.get("enabled") or session.platform != "onebot":
            return True
        bots = self._object_list("onebot", "botAccounts", "botAccountsJson")
        if session.selfId:
            if not any(
                account.get("enabled") is not False
                and str(account.get("qq") or "") == session.selfId
                for account in bots
            ):
                return False
        if session.isDirect:
            return any(
                account.get("enabled") is not False
                and str(account.get("qq") or "") == session.userId
                for account in self._user_accounts()
            )
        group = self._group_config(session.groupId)
        return bool(group and group.get("enabled") is not False)

    def can_manage_session(self, session: SessionView) -> bool:
        """管理权限（对应 canManageSession：先过白名单，再看 managerAccounts）。"""
        if not self.can_handle_session(session):
            return False
        managers = self.shared_story.get("managerAccounts") or []
        if not managers:
            return True
        return session.userId in {str(item) for item in managers}

    def manage_session_denial(self, session: SessionView) -> Optional[str]:
        """返回没有管理权限的原因（对应 manageSessionDenialReason）。"""
        if not self.can_handle_session(session):
            return "不在 HDSI 互动白名单中。"
        managers = self.shared_story.get("managerAccounts") or []
        if managers and session.userId not in {str(item) for item in managers}:
            return "不在共享主剧本管理员名单 sharedStory.managerAccounts 中。"
        return None

    def can_handle_participant(self, participant: Participant) -> bool:
        """参与者当前是否可投递（对应 canHandleParticipant）。"""
        onebot = self.onebot
        if not onebot.get("enabled"):
            return True
        return any(
            account.get("enabled") is not False and str(account.get("qq") or "") == participant.userId
            for account in self._user_accounts()
        )

    def can_handle_story(self, story: Story) -> bool:
        return story.status == "active" and not self._paused

    # ------------------------------------------------------------------ #
    # 故事与参与者
    # ------------------------------------------------------------------ #

    async def get_canonical_story(self) -> Optional[Story]:
        """当前唯一的主剧本（对应 getCanonicalStory）。"""
        row = self.db.first_row("interlude_story", {"status": {"$ne": "archived"}}, order_by="updatedAt DESC")
        return self._row_to_story(row) if row else None

    async def find_story(self, session: SessionView) -> Optional[Story]:
        """按会话查找故事（对应 findStory：主剧本 + 用户端点归属）。"""
        story = await self.get_canonical_story()
        if story is None:
            return None
        row = self.db.first_row(
            "interlude_participant",
            {"storyId": story.id, "userId": session.userId, "selfId": session.selfId},
        )
        if row is None:
            row = self.db.first_row("interlude_participant", {"storyId": story.id, "userId": session.userId})
        if row is None and self.shared_story.get("autoEnrollParticipants", True) and self.can_handle_session(session) and session.isDirect:
            participant = await self.create_participant(story, session)
            return story if participant else None
        return story if row else None

    async def find_participant(self, session: SessionView, story: Story) -> Optional[Participant]:
        row = self.db.first_row(
            "interlude_participant", {"storyId": story.id, "userId": session.userId, "selfId": session.selfId}
        ) or self.db.first_row("interlude_participant", {"storyId": story.id, "userId": session.userId})
        return self._row_to_participant(row) if row else None

    def _user_account_config(self, user_id: str) -> Dict[str, Any]:
        for account in self._user_accounts():
            if str(account.get("qq") or "") == user_id:
                return account
        return {}

    async def create_participant(self, story: Story, session: SessionView) -> Optional[Participant]:
        """创建参与者（对应 createParticipant / 白名单自动加入）。"""
        account = self._user_account_config(session.userId)
        display_name = str(account.get("label") or session.senderName or session.userId)
        person_id = str(account.get("personId") or session.userId)
        profile = str(account.get("profile") or story.setting.get("user", {}).get("profile") or "")
        relationship = str(account.get("relationship") or story.setting.get("relationship") or "")
        participant_id = f"participant:{stable_suffix(f'{story.id}|{session.selfId}|{session.userId}', 16)}"
        now = now_utc()
        self.db.insert_row("interlude_participant", {
            "id": participant_id,
            "storyId": story.id,
            "platform": session.platform,
            "selfId": session.selfId,
            "userId": session.userId,
            "channelId": session.umo or session.channelId,
            "personId": person_id,
            "displayName": display_name,
            "profile": profile,
            "relationship": relationship,
            "state": empty_participant_state(),
            "status": "active",
            "createdAt": now,
            "updatedAt": now,
        })
        self._log("info", f"新参与者加入主剧本：{display_name}（{session.userId}）")
        return await self.get_participant(participant_id)

    async def create_story(self, session: SessionView, legacy_name: str = "") -> Story:
        """从当前配置档案创建主剧本（对应 createStory）。"""
        defaults = self.story_defaults
        setting = empty_story_setting()
        setting["character"] = {
            "name": str(defaults.get("characterName") or "Unnamed character"),
            "profile": str(defaults.get("characterProfile") or ""),
        }
        setting["perspective"] = str(defaults.get("perspective") or "")
        setting["perspectives"] = [str(item) for item in (defaults.get("perspectives") or []) if str(item).strip()]
        setting["supplementaryFacts"] = [str(item) for item in (defaults.get("supplementaryFacts") or []) if str(item).strip()]
        setting["user"] = {"displayName": "", "profile": str(defaults.get("userProfile") or "")}
        setting["relationship"] = str(defaults.get("relationship") or "")
        setting["world"] = str(defaults.get("world") or "")
        setting["supportingCast"] = str(defaults.get("supportingCast") or "")
        setting["location"] = str(defaults.get("location") or "")
        setting["style"] = str(defaults.get("style") or setting["style"])
        setting["timezone"] = str(defaults.get("timezone") or "Asia/Shanghai")
        story_id = f"story:{stable_suffix(f'{session.platform}|{session.selfId}|{session.userId}', 16)}"
        now = now_utc()
        state = empty_story_state()
        state["automation"] = {"nextAdvanceAt": to_iso(now)}
        self.db.insert_row("interlude_story", {
            "id": story_id,
            "platform": session.platform,
            "selfId": session.selfId,
            "userId": session.userId,
            "channelId": session.umo or session.channelId,
            "status": "active",
            "setting": setting,
            "state": encode_story_state(state),
            "cursorAt": now,
            "createdAt": now,
            "updatedAt": now,
        })
        story = await self.get_story(story_id)
        assert story is not None
        # 初始补充事实作为长期事实注入
        for fact_text in setting.get("supplementaryFacts") or []:
            await self._insert_fact(story, "world", fact_text, importance=0.7, confidence=0.9)
        await self._ensure_active_scene(story)
        await self.create_participant(story, session)
        if legacy_name.strip():
            self._log("info", f"已忽略旧 init 的名称参数“{legacy_name.strip()}”；角色名称以配置为准。")
        return story

    async def _ensure_active_scene(self, story: Story, now: Optional[datetime] = None) -> Scene:
        """确保存在活跃场景与剧情弧线（对应 ensureActiveScene）。"""
        scene_row = self.db.first_row("interlude_scene", {"storyId": story.id, "status": "active"}, order_by="startedAt DESC")
        if scene_row:
            return self._row_to_scene(scene_row)
        moment = now or now_utc()
        scene_id = self.db.insert_row("interlude_scene", {
            "storyId": story.id,
            "status": "active",
            "startedAt": moment,
            "endedAt": None,
            "hook": "",
            "summary": "",
            "entryCount": 0,
            "lastEntryId": None,
            "createdAt": moment,
            "updatedAt": moment,
        })
        arc_row = self.db.first_row("interlude_arc", {"storyId": story.id, "status": "active"}, order_by="updatedAt DESC")
        arc_id = int(arc_row["id"]) if arc_row else self.db.insert_row("interlude_arc", {
            "storyId": story.id,
            "status": "active",
            "title": "开场",
            "summary": "",
            "sceneCount": 0,
            "createdAt": moment,
            "updatedAt": moment,
        })
        state = story.state
        self.db.update_rows("interlude_story", {"id": story.id}, {
            "state": encode_story_state({**state, "activeSceneId": scene_id, "activeArcId": arc_id}),
            "updatedAt": moment,
        })
        return self._row_to_scene(self.db.first_row("interlude_scene", {"id": scene_id}) or {})

    async def active_scene(self, story_id: str) -> Optional[Scene]:
        row = self.db.first_row("interlude_scene", {"storyId": story_id, "status": "active"}, order_by="startedAt DESC")
        return self._row_to_scene(row) if row else None

    async def active_arc(self, story_id: str) -> Optional[Arc]:
        row = self.db.first_row("interlude_arc", {"storyId": story_id, "status": "active"}, order_by="updatedAt DESC")
        return self._row_to_arc(row) if row else None

    def _group_config(self, group_id: str) -> Optional[Dict[str, Any]]:
        for group in self._group_chats():
            if str(group.get("groupId") or "") == str(group_id):
                return group
        return None

    # ------------------------------------------------------------------ #
    # 剧本条目
    # ------------------------------------------------------------------ #

    async def append_entry(
        self,
        story_id: str,
        draft: Dict[str, Any],
        now: Optional[datetime] = None,
        participant_id: str = "",
    ) -> int:
        """写入一条剧本条目并维护活跃场景计数（对应 appendEntry）。"""
        moment = ensure_utc(draft.get("occurredAt")) or now or now_utc()
        entry_id = self.db.append_script_entry({
            "storyId": story_id,
            "participantId": draft.get("participantId", participant_id) or "",
            "kind": draft.get("kind") or "system",
            "actor": draft.get("actor") or "system",
            "content": draft.get("content") or "",
            "occurredAt": moment,
            "metadata": draft.get("metadata") or {},
            "createdAt": now or now_utc(),
        })
        scene_row = self.db.first_row("interlude_scene", {"storyId": story_id, "status": "active"}, order_by="startedAt DESC")
        if scene_row:
            self.db.update_rows("interlude_scene", {"id": scene_row["id"]}, {
                "entryCount": int(scene_row.get("entryCount") or 0) + 1,
                "lastEntryId": entry_id,
                "updatedAt": now or now_utc(),
            })
        return entry_id

    async def recent_entries(self, story_id: str, limit: int = 35) -> List[ScriptEntry]:
        """最近原始条目（对应 recentEntries）。"""
        rows = self.db.list_rows(
            "interlude_script_entry", {"storyId": story_id}, order_by="id DESC", limit=int(limit)
        )
        entries = [self._row_to_entry(row) for row in rows]
        entries.reverse()
        return entries

    async def entries_between(self, story_id: str, from_at: datetime, to_at: datetime, limit: int = 200) -> List[ScriptEntry]:
        rows = self.db.list_rows(
            "interlude_script_entry",
            {"storyId": story_id, "occurredAt": {"$gt": to_iso(from_at), "$lte": to_iso(to_at)}},
            order_by="id ASC",
            limit=limit,
        )
        return [self._row_to_entry(row) for row in rows]

    # ------------------------------------------------------------------ #
    # 私聊接入（receive）
    # ------------------------------------------------------------------ #

    async def receive(self, session: SessionView) -> bool:
        """处理一条私聊事件；返回是否消费（True 表示不再交给其它处理器）。"""
        if not self.runtime.get("captureDirectMessages", True):
            return False
        if not self.can_handle_session(session):
            return False
        story = await self.find_story(session)
        if story is None:
            if not self.runtime.get("autoCreate", False):
                return False
            story = await self.create_story(session)
        participant = await self.find_participant(session, story)
        if participant is None:
            participant = await self.create_participant(story, session)
        if participant is None or participant.status != "active":
            return False
        if self._is_duplicate_incoming(session.key()):
            return True

        now = now_utc()
        content = sanitize_and_clamp_visible_content(
            session.content, int(self.runtime.get("maxMessageCharacters") or 1500) * 4, DEFAULT_MESSAGE_SEPARATOR
        )
        # 图片 / 语音占位；合并转发已由入口层展开（main._extract_forward_text）。
        image_sources = list(session.imageSources)
        audio_sources = list(session.audioSources)
        entry_kind = "user-message"
        entry_content = content or ("（用户发来图片）" if image_sources else "（用户发来语音）" if audio_sources else "")
        if not entry_content:
            return False
        await self.serial(story.id, lambda: self._record_incoming(story, participant, session, entry_kind, entry_content, now, image_sources, audio_sources))
        # 新消息：中断在途请求（首条回复未提交时会废弃旧请求）
        self.turn_engine.signal_incoming_interruption(story.id, participant.id)
        # 取消普通延迟回复（可配置）
        if self.runtime.get("cancelDelayedRepliesOnUserMessage", True):
            await self._cancel_delayed_replies(story.id, participant.id, now, "user-message-cancel")
        self.turn_engine.buffer_user_narrative(
            story.id,
            participant.id,
            {
                "content": entry_content,
                "occurredAt": to_iso(now),
                "supersededIntents": [],
                "imageSources": image_sources,
                "audioSources": audio_sources,
                "quote": session.quote,
            },
            event=session,
        )
        await self._note_participant_incoming(story, participant, now)
        return True

    async def _record_incoming(
        self,
        story: Story,
        participant: Participant,
        session: SessionView,
        kind: str,
        content: str,
        now: datetime,
        image_sources: List[str],
        audio_sources: List[str],
    ) -> None:
        metadata: Dict[str, Any] = {}
        if session.messageId:
            # typ-0 replyTo 的等价物：保留触发消息 ID，供引用回复透传。
            metadata["messageId"] = session.messageId
        if image_sources:
            metadata["imageSources"] = image_sources
        if audio_sources:
            metadata["audioSources"] = audio_sources
        if session.quote:
            metadata["quote"] = session.quote
        await self.append_entry(story.id, {
            "kind": kind,
            "actor": "user",
            "participantId": participant.id,
            "content": content,
            "occurredAt": to_iso(now),
            "metadata": metadata,
        }, now, participant.id)
        # 刷新参与者会话地址（主动投递 / 延迟消息依赖它找回会话）
        if session.umo and session.umo != participant.channelId:
            self.db.update_rows("interlude_participant", {"id": participant.id}, {
                "channelId": session.umo, "updatedAt": now,
            })
        endpoint_id = self._remember_participant_endpoint(participant, session, now)
        if endpoint_id:
            endpoint_state = dict(participant.state or {})
            endpoint_state["preferredEndpointId"] = endpoint_id
            self.db.update_rows("interlude_participant", {"id": participant.id}, {
                "state": endpoint_state, "updatedAt": now,
            })
        state = story.state
        automation = dict(state.get("automation") or {})
        automation["lastUserMessageAt"] = to_iso(now)
        automation["quietUntil"] = to_iso(now + timedelta(minutes=10))
        # Urge：用户消息只改变"下一次推进"的密度信号，不直接触发回复。
        urge_config = urge_mod.resolve_urge_config(self.config.get("urge"))
        if urge_config["enabled"]:
            urge_state = urge_mod.normalize_urge_state(state.get("urge"), now.timestamp())
            state = {**state, "urge": urge_mod.urge_user_event(urge_state, now.timestamp())}
        self.db.update_rows("interlude_story", {"id": story.id}, {
            "state": encode_story_state({**state, "automation": automation}),
            "updatedAt": now,
        })

    async def _note_participant_incoming(self, story: Story, participant: Participant, now: datetime) -> None:
        # 重新读取行，避免用进场时的旧快照覆盖端点/未读等并发更新。
        row = self.db.first_row("interlude_participant", {"id": participant.id})
        state = dict(json_loads((row or {}).get("state"), {}) or {})
        state["unreadMessageCount"] = int(state.get("unreadMessageCount") or 0) + 1
        state["lastUserMessageAt"] = to_iso(now)
        changes: Dict[str, Any] = {"state": state, "updatedAt": now}
        # 参与者会话地址随事件刷新（主动投递与延迟消息依赖它找到会话）。
        if row and row.get("channelId"):
            changes["channelId"] = row["channelId"]
        elif participant.channelId:
            changes["channelId"] = participant.channelId
        self.db.update_rows("interlude_participant", {"id": participant.id}, changes)

    def _remember_participant_endpoint(self, participant: Participant, session: SessionView, now: datetime) -> str:
        """把本次入站会话登记为参与者的可用端点（对应 endpoint-state 的刷新）。

        端点必须携带真实的 `umo`（平台会话地址）才能参与投递；没有 `umo`
        的入站事件（例如某些适配器只给 user_id）不会覆盖已有端点。
        """
        if not session.umo or not session.userId:
            return ""
        platform = session.platform or "onebot"
        endpoint_id = f"ep:{stable_suffix(f'{participant.id}|{platform}|{session.userId}', 12)}"
        values = {
            "ownerKind": "participant",
            "ownerId": participant.id,
            "channelKind": "qq",
            "platform": platform,
            "accountKey": f"{platform}:{session.selfId}",
            "selfId": session.selfId,
            "userId": session.userId,
            "channelId": session.umo,
            "conversationKind": "private",
            "enabled": True,
            "updatedAt": now,
        }
        existing = self.db.first_row("interlude_endpoint", {"id": endpoint_id})
        if existing:
            self.db.update_rows("interlude_endpoint", {"id": endpoint_id}, values)
        else:
            self.db.insert_row("interlude_endpoint", {"id": endpoint_id, **values, "createdAt": now})
        return endpoint_id

    def _delivery_umo(self, participant: Participant) -> str:
        """选择投递地址：优先参与者端点，其次 `participant.channelId`。

        端点路由让同一参与者在多个平台/账号之间切换时，消息从"最近一次
        真实会话"对应的账号发出，而不是永远使用首次建档时的地址。
        """
        state = participant.state or {}
        preferred = state.get("preferredEndpointId")
        if isinstance(preferred, str) and preferred:
            row = self.db.first_row("interlude_endpoint", {"id": preferred, "enabled": True})
            if row and row.get("channelId"):
                return str(row["channelId"])
        rows = self.db.list_rows("interlude_endpoint", {
            "ownerKind": "participant", "ownerId": participant.id, "enabled": True,
        }, order_by="updatedAt DESC")
        for row in rows:
            if row.get("channelId"):
                return str(row["channelId"])
        return participant.channelId

    def _is_duplicate_incoming(self, key: str) -> bool:
        now = time.time()
        if key in self._seen_incoming and now - self._seen_incoming[key] < 120:
            return True
        self._seen_incoming[key] = now
        if len(self._seen_incoming) > 600:
            oldest = min(self._seen_incoming, key=self._seen_incoming.get)
            self._seen_incoming.pop(oldest, None)
        return False

    async def _cancel_delayed_replies(self, story_id: str, participant_id: str, now: datetime, reason: str) -> None:
        rows = self.db.list_rows("interlude_intent", {"storyId": story_id, "status": "pending"})
        for row in rows:
            intent = self._row_to_intent(row)
            if intent.type not in ("delayed-message", "split-message"):
                continue
            if intent.participantId not in ("", participant_id):
                continue
            self.db.update_rows("interlude_intent", {"id": intent.id, "status": "pending"}, {
                "status": "cancelled", "updatedAt": now,
            })
            reference = intent.payload.get("scriptEvent") if isinstance(intent.payload, dict) else None
            if isinstance(reference, dict):
                await self.update_script_delivery_outcome(story_id, reference, "cancelled", now, reason)

    # ------------------------------------------------------------------ #
    # 群聊接入（receiveGroup）
    # ------------------------------------------------------------------ #

    async def receive_group(self, session: SessionView) -> bool:
        """处理一条群聊事件（对应 receiveGroup：白名单/意愿门/冷却/@ 规则）。"""
        group = self._group_config(session.groupId)
        if group is None or group.get("enabled") is False:
            return False
        if not session.content.strip() and not session.imageSources and not session.audioSources:
            return False
        if self._is_duplicate_incoming(session.key()):
            return True
        story = await self.get_canonical_story()
        if story is None:
            return False
        now = now_utc()
        content = session.content.strip()
        mentioned = session.atBot
        entry_content = content or ("（群成员发来图片）" if session.imageSources else "（群成员发来语音）")
        await self.serial(story.id, lambda: self.append_entry(story.id, {
            "kind": "group-message",
            "actor": "user",
            "participantId": "",
            "content": f"[群{group.get('label') or session.groupId}] {session.senderName or session.userId}：{entry_content}",
            "occurredAt": to_iso(now),
            "metadata": {
                "groupId": session.groupId,
                "senderId": session.userId,
                "senderName": session.senderName,
                **({"messageId": session.messageId} if session.messageId else {}),
            },
        }, now))

        response_mode = group.get("responseMode") or "mention-only"
        include = mentioned or response_mode == "always"
        if not include:
            return False
        # 冷却检查
        cooldown_key = f"group-cooldown:{session.groupId}"
        last_reply = self._group_willingness_memory.get(cooldown_key, {}).get("at", 0)
        cooldown_seconds = float(_pick(group.get("cooldownSeconds"), 60))
        if not mentioned and time.time() - last_reply < cooldown_seconds:
            return False
        # 意愿门（@ 直接通过、睡眠态例外）
        preset = group.get("willingnessPreset") or "off"
        willingness_input = group.get("willingness") or {}
        gate_state_key = f"group-willingness:{session.groupId}"
        previous = self._group_willingness_memory.get(gate_state_key)
        life_status = story.state.get("lifeStatus")
        decision = willingness_mod.evaluate_willingness_gate(
            previous,
            preset,
            group.get("willingnessAuto"),
            life_status,
            willingness_input,
            message_count=1,
            mentioned_bot=mentioned,
            quoted_bot=False,
            content=content,
            now_seconds=now.timestamp(),
        )
        self._group_willingness_memory[gate_state_key] = decision["state"]
        if not decision["shouldCall"]:
            self._log(
                "debug",
                f"群 {session.groupId} 意愿门拦截：{decision['reason']} "
                f"({willingness_mod.format_willingness_gate_diagnosis(decision['diagnosis'])})",
            )
            return True
        # 进入群回合缓冲：按 (故事, 群) 隔离，多群同时说话不会串批。
        buffer_key = (story.id, session.groupId)
        turn = self._buffered_group_turns.get(buffer_key)
        if turn is None:
            turn = {"messages": [], "timer": None, "groupId": session.groupId, "umo": session.umo, "mention": mentioned}
            self._buffered_group_turns[buffer_key] = turn
        turn["messages"].append({"content": entry_content, "senderName": session.senderName, "senderId": session.userId})
        turn["groupId"] = session.groupId
        turn["umo"] = session.umo or turn.get("umo") or ""
        turn["mention"] = turn.get("mention") or mentioned
        if turn.get("timer"):
            turn["timer"].cancel()
        debounce = max(0.0, float(_pick(group.get("debounceSeconds"), 1)))
        loop = asyncio.get_event_loop()
        turn["timer"] = loop.call_later(
            debounce,
            lambda sid=story.id, gid=session.groupId: asyncio.ensure_future(self._flush_group_turn(sid, gid)),
        )
        return True

    async def _flush_group_turn(self, story_id: str, group_id: str) -> None:
        """群回合刷写入口：同一故事同一时刻只允许一个群批次写作。

        群批次之间互不阻塞（否则两个群会互相等待），但同一故事的落库与
        游标推进必须串行，避免并发写作把时间线写乱。
        """
        buffer_key = (story_id, group_id)
        turn = self._buffered_group_turns.pop(buffer_key, None)
        if not turn or not turn.get("messages"):
            return
        lock = self._group_flush_locks.setdefault(story_id, asyncio.Lock())
        if lock.locked():
            self._buffered_group_turns[buffer_key] = turn
            loop = asyncio.get_event_loop()
            turn["timer"] = loop.call_later(
                0.5,
                lambda sid=story_id, gid=group_id: asyncio.ensure_future(self._flush_group_turn(sid, gid)),
            )
            return
        async with lock:
            await self._flush_group_turn_locked(story_id, group_id, turn)

    async def _flush_group_turn_locked(
        self, story_id: str, group_id: str, turn: Dict[str, Any]
    ) -> None:
        """群回合刷写主体：把合并后的群消息交给主叙事（对应 flushBufferedGroupTurn）。"""
        buffer_key = (story_id, group_id)
        if self._private_narrative_pending(story_id):
            self._buffered_group_turns[buffer_key] = turn
            loop = asyncio.get_event_loop()
            turn["timer"] = loop.call_later(
                1.0,
                lambda sid=story_id, gid=group_id: asyncio.ensure_future(self._flush_group_turn(sid, gid)),
            )
            return
        story = await self.get_story(story_id)
        if story is None or not self.can_handle_story(story):
            return
        group = self._group_config(turn.get("groupId") or "") or {}
        context_limit = int(group.get("contextLimit") or 20)
        recent = await self.recent_entries(story_id, max(context_limit, 10))
        group_messages = [
            entry for entry in recent
            if entry.kind == "group-message" and (entry.metadata or {}).get("groupId") == turn.get("groupId")
        ][-context_limit:]
        group_context = {
            "groupId": turn.get("groupId"),
            "label": group.get("label") or "",
            "purpose": group.get("purpose") or "",
            "characterRole": group.get("characterRole") or "",
            "messages": [
                {
                    # messageRef 是给模型看的稳定引用；messageId 是平台真实消息 ID，
                    # 用于引用回复与消息回应（对应上游 groupContext.messages）。
                    "messageRef": f"msg-{entry.id}",
                    "messageId": _meta_text(entry.metadata, "messageId"),
                    "senderId": (entry.metadata or {}).get("senderId") or "",
                    "senderName": (entry.metadata or {}).get("senderName") or "",
                    "content": entry.content,
                    "occurredAt": to_iso(entry.occurredAt),
                }
                for entry in group_messages
            ],
        }
        now = now_utc()
        from_at = self.narrative_cursor(story, now)
        request = await self._build_request(story, None, "user-message", from_at, now)
        request.groupContext = group_context
        result = await self._run_narrative(story, request, group_turn=True)
        if result.get("success"):
            decision = result.get("decision") or {}
            # 对应上游 hoistParticipantlessInteraction：群回合模型把话写进 interaction 时，
            # 提升为 groupReply，保证群发言仍能投递。
            if not decision.get("groupReply"):
                reply = (decision.get("interaction") or {}).get("reply") or {}
                if reply.get("mode") == "immediate" and isinstance(reply.get("content"), str) and reply["content"].strip():
                    decision["groupReply"] = reply["content"]
            # 消息回应只允许指向本轮 groupContext 中真实存在的消息。
            normalized_reactions = self._normalize_group_reactions(
                decision.get("messageReactions"), group_context.get("messages") or []
            )
            if normalized_reactions:
                decision["messageReactions"] = normalized_reactions
            else:
                decision.pop("messageReactions", None)
            # 群回合的正文同样落库（对应群的 script 条目；群发言另行登记）
            group_entry_id = 0
            if decision.get("script"):
                scene = await self.active_scene(story_id)
                group_entry_id = await self.serial(story_id, lambda: self.append_entry(story_id, {
                    "kind": "script",
                    "actor": "protagonist",
                    "participantId": "",
                    "content": decision.get("script") or "",
                    "occurredAt": to_iso(now),
                    "metadata": {
                        "phase": "user-message", "groupTurn": True,
                        "groupId": turn.get("groupId"),
                        "sceneId": scene.id if scene else None,
                    },
                }, now))
            self.db.update_rows("interlude_story", {"id": story_id}, {"cursorAt": now, "updatedAt": now_utc()})
            if decision.get("groupReply"):
                umo = turn.get("umo") or ""
                group_id = turn.get("groupId") or ""
                delivery_status = "failed"
                delivery_reason = ""
                if umo:
                    raw_status = await self._transport_send(umo, decision["groupReply"])
                    if raw_status == "unknown":
                        # 群消息同样区分“结果未知”：可能已发出，只留痕、不重发。
                        delivery_status = "pending"
                        delivery_reason = "transport-timeout: 回执超时，结果未知"
                    else:
                        delivery_status = raw_status
                if delivery_status == "pending":
                    self._log("warn", f"群消息投递超时（结果未知，不自动重试） 群频道={group_id}")
                elif delivery_status == "failed":
                    self._log("error", f"群消息投递失败 群频道={group_id}")
                self._group_willingness_memory[f"group-cooldown:{turn.get('groupId')}"] = {"at": time.time()}
                # 成功群发言扣减意愿（同时生成静默批次）
                gate_state_key = f"group-willingness:{turn.get('groupId')}"
                consumed = willingness_mod.consume_willingness_gate(
                    self._group_willingness_memory.get(gate_state_key),
                    group.get("willingnessPreset") or "off",
                    group.get("willingnessAuto"),
                    story.state.get("lifeStatus"),
                    group.get("willingness"),
                    now.timestamp(),
                )
                self._group_willingness_memory[gate_state_key] = consumed
                await self.serial(story_id, lambda: self.append_entry(story_id, {
                    "kind": "character-group-message",
                    "actor": "protagonist",
                    "content": decision["groupReply"],
                    "occurredAt": to_iso(now),
                    "metadata": {
                        "groupId": turn.get("groupId"),
                        "deliveryStatus": delivery_status,
                        **({"deliveryReason": delivery_reason} if delivery_reason else {}),
                    },
                }, now))
            # 群回合的平台动作（原生表情/表情包/回应）落账并投递到群会话。
            await self._deliver_group_platform_actions(
                story, group_entry_id, decision, turn.get("umo") or "", now
            )
            self._schedule_next_automatic_advance(story_id, now)

    def _normalize_group_reactions(
        self, raw: Any, messages: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """把模型给的 messageRef 解析成平台 messageId（对应 normalizeGroupChatActions）。"""
        chat = self.config.get("chatActions") or {}
        if not chat.get("enabled") or not chat.get("messageReactions", True):
            return []
        allowed = [str(item) for item in _pick(chat.get("allowedReactions"), list(self.QQ_REACTION_IDS))]
        targets = {
            str(message.get("messageRef") or ""): str(message.get("messageId") or "")
            for message in messages
            if message.get("messageRef") and message.get("messageId")
        }
        result: List[Dict[str, Any]] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            reference = str(item.get("messageRef") or "")
            reaction = str(item.get("reaction") or "")
            target_id = targets.get(reference) or ""
            if target_id and reaction in allowed:
                result.append({"messageRef": reference, "reaction": reaction, "messageId": target_id})
        return result[:1]

    async def _deliver_group_platform_actions(
        self,
        story: Story,
        script_entry_id: int,
        decision: Dict[str, Any],
        umo: str,
        now: datetime,
    ) -> None:
        """群回合的平台动作：把决策里的动作写成账本并投递到群会话。"""
        if not script_entry_id or not umo:
            return
        metadata: Dict[str, Any] = {}
        if decision.get("localMedia"):
            metadata["localMedia"] = decision["localMedia"]
        if decision.get("nativeFace"):
            metadata["nativeFace"] = decision["nativeFace"]
        if decision.get("messageReactions"):
            metadata["messageReactions"] = decision["messageReactions"]
        if not metadata:
            return
        commit_id = f"group-action:{script_entry_id}"
        event = {
            "kind": "platform-action",
            "actor": "protagonist",
            "participantId": "",
            "eventId": f"{commit_id}:e1",
            "metadata": metadata,
        }
        actions = create_script_delivery_actions(
            {"commitId": commit_id, "events": [event], "window": {"to": to_iso(now)}}
        )
        if not actions:
            return
        row = self.db.first_row("interlude_script_entry", {"id": script_entry_id, "storyId": story.id})
        if row:
            entry_metadata = json_loads(row.get("metadata"), {}) or {}
            entry_metadata["deliveryActions"] = actions
            self.db.update_rows(
                "interlude_script_entry", {"id": script_entry_id}, {"metadata": entry_metadata}
            )
        await self._deliver_platform_actions(
            story,
            None,
            [{**event, "commitId": commit_id, "scriptEntryId": script_entry_id}],
            now,
            umo_override=umo,
        )

    def has_pending_narrative(self, story_id: str) -> bool:
        """故事是否有未完成写作（对应 hasPendingNarrative）。

        群缓冲键为 `(storyId, groupId)`：只要该故事下任一未刷写的群批次
        存在，就算有未完成写作。群刷写自身改用 `_private_narrative_pending`，
        否则会把自己算成"未完成"而永远排不上。
        """
        return self.turn_engine.has_pending_narrative(
            story_id,
            lambda target: any(
                key[0] == target and bool(turn.get("messages"))
                for key, turn in self._buffered_group_turns.items()
            ),
        )

    def _private_narrative_pending(self, story_id: str) -> bool:
        """只看私聊回合是否在写（群批次之间互不阻塞）。"""
        return self.turn_engine.has_pending_narrative(story_id, lambda _target: False)

    # ------------------------------------------------------------------ #
    # 私聊回合刷写（核心管线）
    # ------------------------------------------------------------------ #

    async def flush_buffered_narrative(self, key: str, revision: int) -> None:
        """合并缓冲到期：执行一次完整写作（对应 flushBufferedNarrative）。"""
        if self._paused:
            turn = self.turn_engine.get_turn(key)
            if turn:
                turn.timer = None
            return
        turn = self.turn_engine.begin_flush(key, revision)
        if turn is None:
            return
        request_id = revision
        turn.inFlightRequestId = request_id
        try:
            # 批次取出与开场诊断都放在 try 内：任何前段异常都由 catch 消化，
            # finally 的 end_flush 必达（对应上游 rc55 的回合死锁修复）。
            batch = list(turn.messages)
            turn.messages.clear()
            self._log(
                "debug",
                f"写请求开始 故事={turn.storyId} 参与者={turn.participantId} "
                f"请求={request_id} 批次={len(batch)}",
            )
            if not batch:
                return
            snapshot = await self.serial(turn.storyId, lambda: self._turn_snapshot(turn.storyId, turn.participantId))
            if snapshot is None:
                await self.schedule_narrative_retry(turn.storyId, turn.participantId)
                return
            story, participant, from_at, now, due = snapshot
            user_message = format_buffered_user_messages(batch)
            request_started = time.time()
            request = await self._build_request(story, participant, "user-message", from_at, now)
            request.userMessage = user_message
            request.dueIntents = live_narrative_intents(due)
            request.supersededIntents = []
            # 图片/语音证据（数据 URI 由平台层收集）
            image_sources: List[str] = []
            for message in batch:
                image_sources.extend(message.get("imageSources") or [])
            request.images = await self._load_native_images(story, image_sources[:3])
            vision_config = (self.config.get("model") or {}).get("vision") or {}
            if (
                request.images
                and vision_config.get("enabled")
                and str(vision_config.get("mode") or "native") == "sidecar"
            ):
                # 侧端识图：把图片转成事实观察，主叙事只读文字（对应 describeVisual）。
                observation = await self.narrator.describe_visual(request.images)
                if observation:
                    request.visualObservations = [observation]
                    request.images = []
            audio_sources: List[str] = []
            for message in batch:
                audio_sources.extend(message.get("audioSources") or [])
            request.audio = await self._load_native_audio(story, audio_sources[:1])
            request.quotedMessages = [
                message["quote"] for message in batch if isinstance(message.get("quote"), dict)
            ]
            result = await self._run_narrative(story, request)
            effective_now = now
            if not result.get("success"):
                if result.get("decision") and result["decision"].get("script"):
                    result["decision"]["outputRecovery"] = True
                    retry_request = await self._build_request(story, participant, "user-message", from_at, now)
                    retry_request.userMessage = user_message
                    retry_request.outputRecovery = True
                    retry_request.dueIntents = request.dueIntents
                    result = await self._run_narrative(story, retry_request)
            if turn.nextRevision != revision:
                turn.messages = batch + turn.messages
                return
            decision = result.get("decision") or {}
            persisted = await self.serial(
                turn.storyId,
                lambda: self._persist_turn(
                    turn.storyId,
                    participant.id,
                    decision,
                    from_at,
                    effective_now,
                    "user-message",
                    success=bool(result.get("success")),
                ),
            )
            self._schedule_browser_intents(story, decision, participant.id, effective_now)
            if result.get("success") and decision.get("interaction", {}).get("reply", {}).get("mode") == "immediate":
                turn.firstMessageCommittedRequestId = request_id
            if decision.get("interaction"):
                mode = visible_reply_mode(decision, "user-message", None)
                self._health["replyModes"][mode if mode in self._health["replyModes"] else "noDelivery"] = (
                    self._health["replyModes"].get(mode if mode in self._health["replyModes"] else "noDelivery", 0) + 1
                )
            # 投递可见消息（首条打字下限按请求起始时间计算）
            await self._deliver_messages_for_participant(
                story, participant, persisted.get("messages") or [], request_started, result.get("succeeded", False) and False
            )
            await self._deliver_platform_actions(
                story, participant, persisted.get("platformActions") or [], effective_now
            )
            if result.get("success"):
                await self.serial(turn.storyId, lambda: self._finish_turn(turn.storyId, participant.id, effective_now, request.dueIntents))
                await self.schedule_compaction(turn.storyId)
                self._schedule_next_automatic_advance(turn.storyId, effective_now)
            else:
                await self.schedule_narrative_retry(turn.storyId, turn.participantId)
        except Exception as error:  # noqa: BLE001 - 回合失败不能拖垮整个插件
            self._log("warn", f"合并写作任务失败：参与者={turn.participantId} 错误={error}")
            await self.schedule_narrative_retry(turn.storyId, turn.participantId)
        finally:
            try:
                self.turn_engine.end_flush(key, request_id)
            except Exception as error:  # noqa: BLE001 - 收尾异常不得再抛出
                self._log("warn", f"回合收尾失败：{error}")

    async def _turn_snapshot(
        self, story_id: str, participant_id: str
    ) -> Optional[Tuple[Story, Participant, datetime, datetime, List[Intent]]]:
        story = await self.get_story(story_id)
        participant = await self.get_participant(participant_id)
        if story is None or participant is None:
            return None
        if participant.status != "active" or story.status != "active":
            return None
        now = now_utc()
        due = live_narrative_intents(await self.due_intents(story.id, now))
        due = [intent for intent in due if not intent.participantId or intent.participantId == participant.id]
        return story, participant, self.narrative_cursor(story, now), now, due

    def narrative_cursor(self, story: Story, now: datetime) -> datetime:
        """写作窗口起点（对应 narrativeCursor）。"""
        cursor = story.cursorAt
        if cursor > now:
            return now
        return cursor

    # ------------------------------------------------------------------ #
    # 请求装配与模型调用
    # ------------------------------------------------------------------ #

    async def _build_request(
        self, story: Story, participant: Optional[Participant], phase: str, from_at: datetime, now: datetime
    ) -> NarrativeRequest:
        """装配一次叙事请求的上下文（对应 tryDecide 的上下文部分）。"""
        runtime = self.runtime
        entry_limit = int(runtime.get("contextEntryLimit") or 17)
        window_minutes = int(_pick(runtime.get("contextTimeWindowMinutes"), 6))
        recent = await self.recent_entries(story.id, max(entry_limit * 2, 60))
        cutoff = now - timedelta(minutes=window_minutes) if window_minutes > 0 else None
        selected = recent[-entry_limit:]
        if cutoff:
            visible = [entry for entry in recent if entry.occurredAt >= cutoff][-500:]
            ids = {entry.id for entry in selected}
            for entry in visible:
                if entry.id not in ids:
                    selected.append(entry)
            selected.sort(key=lambda entry: entry.id)
        participants = await self.participants(story.id)
        # 参与者按相关性排序（未读/待回复权重），当前参与者置后（对应 participantRelevance）
        participants.sort(key=participant_relevance, reverse=True)
        if participant:
            participants = [item for item in participants if item.id != participant.id] + [participant]
        query = (
            next((entry.content for entry in reversed(selected) if entry.kind == "user-message"), "")
            if phase == "user-message"
            else ""
        )
        facts = await self._facts_for_prompt(story, participant, query)
        memories = await self.memories(story.id, int(self.memory_config.get("recentEntryLimit") or 10), participant.id if participant else "")
        scene = await self.active_scene(story.id)
        arc = await self.active_arc(story.id)
        previous_scenes = await self._previous_scene_summaries(story.id, scene)
        overlay_snapshots = await self._overlay_snapshots(story)
        alter_state = story.state.get("alterSystem")
        config = self.config
        alter_config = alter_mod.resolve_alter_system_config(config.get("alterSystem"))
        agency_config = agency_mod.resolve_agency_config(config.get("agency"))
        schedule_record = self.admin_schedule_preplan_sync(story.id)
        schedule_window = preplan_mod.schedule_preplan_window(
            schedule_record, now, story.setting.get("timezone") or "UTC", 12,
            preplan_mod.resolve_schedule_preplan_config(config.get("schedulePreplan")),
        )
        active_consequences = [
            intent for intent in await self.due_intents(story.id, now, include_active=True)
            if intent.type == "active-consequence" and intent.status == "pending"
        ]
        commitments = [
            intent for intent in await self._pending_intents(story.id)
            if intent.type == "follow-up-commitment" and intent.participantId == (participant.id if participant else "")
        ][:2]
        request = NarrativeRequest(
            phase=phase,
            story=story,
            from_at=from_at,
            now=now,
            participant=participant,
            participants=participants,
            recentEntries=selected,
            facts=facts,
            memories=memories,
            scene=scene,
            arc=arc,
            previousScenes=previous_scenes,
            overlaySnapshots=overlay_snapshots,
            alterEnabled=bool(alter_config.get("enabled")),
            emotionalOffset=alter_mod.emotional_offset_for_prompt(alter_state, alter_config),
            agencyEnabled=bool(agency_config.get("enabled")) and phase in ("advance", "intent-due"),
            agencyWindow=agency_mod.active_agency_window(story.state.get("agencyWindow"), now),
            schedulePreplan=schedule_window,
            activeConsequences=active_consequences,
            automaticDeliverySummaries=story.state.get("automaticDeliverySummaries") or [],
            followUpCommitments=commitments,
            workingDetails=self._active_working_details(story, now),
            scenePresence=story.state.get("scenePresence") or [],
            deliveryReality=delivery_reality_projection(
                selected,
                participant.id if participant else "",
                bool(self.shared_story.get("shareParticipantDetails")),
                timezone_name=str(story.setting.get("timezone") or ""),
            ),
            mediaCatalog=self._media_catalog(),
            webContext=self._browser_observations(story.id),
            continuation=(
                continuation_bookmark(recent, from_at, now) if phase == "user-message" else None
            ),
            messageSeparator=str(runtime.get("messageSeparator") or DEFAULT_MESSAGE_SEPARATOR),
            splitReplyMessages=runtime.get("splitReplyMessages") is not False,
        )
        if self.content_store is not None:
            try:
                activation_text = "\n".join(
                    entry.content for entry in selected[-8:] if getattr(entry, "content", "")
                )[-12000:]
                request.characterCard = self.content_store.character_for_story(story.id)
                request.worldBookEntries = self.content_store.activated_entries(
                    story.id,
                    activation_text,
                )
            except Exception as error:  # noqa: BLE001 - 内容层失败降级为无角色卡
                self._log("warn", f"装配 WebUI 角色/世界书失败：{error}")
        return request

    async def _plan_timeline(
        self, story: Story, request: NarrativeRequest, now: datetime
    ) -> Optional[TimelinePlan]:
        """自动回合的时间导演（对应 planTimeline）。

        只给自动回合（advance / intent-due）提供"这一段生活怎么分配"的相对
        账本；失败、关闭或输出不合法时返回 None，主叙事按无账本推进。
        """
        director = self.config.get("timelineDirector") or {}
        if not director.get("enabled", True) or request.phase not in ("advance", "intent-due"):
            return None
        timezone = story.setting.get("timezone") or "UTC"
        payload = {
            "storyIdentity": {"setting": story.setting},
            "interval": {
                "from": to_iso(request.from_at),
                "now": to_iso(now),
                "timezone": timezone,
            },
            "lifeStatus": story.state.get("lifeStatus"),
            "schedulePreplan": request.schedulePreplan,
            "timelineCarry": story.state.get("timelineCarry") or [],
            "recentScript": [
                {"occurredAt": to_iso(entry.occurredAt), "content": clip(entry.content, 400)}
                for entry in request.recentEntries[-6:]
            ],
        }
        try:
            plan = await self.narrator.plan_timeline(payload, self.config)
        except Exception as error:  # noqa: BLE001 - 时间导演失败不能阻断推进
            self._log("debug", f"时间导演失败，已降级为无账本推进：{error}")
            return None
        if not plan:
            return None
        return TimelinePlan(beats=plan.get("beats") or [], carry=plan.get("carry") or [])

    def _active_working_details(self, story: Story, now: datetime) -> List[Dict[str, Any]]:
        details = story.state.get("workingDetails") or []
        active = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            expires = ensure_utc(detail.get("expiresAt"))
            if expires and expires <= now:
                continue
            active.append(detail)
        return active[-12:]

    async def _previous_scene_summaries(self, story_id: str, scene: Optional[Scene]) -> List[Dict[str, Any]]:
        count = int(self.memory_config.get("previousSceneSummaries") or 0)
        if count <= 0:
            return []
        rows = self.db.list_rows(
            "interlude_scene",
            {"storyId": story_id, "status": "closed"},
            order_by="endedAt DESC",
            limit=count,
        )
        return [
            {
                "startedAt": to_iso(ensure_utc(row.get("startedAt"))),
                "endedAt": to_iso(ensure_utc(row.get("endedAt"))),
                "summary": clip(row.get("summary") or "", 2000),
            }
            for row in reversed(rows)
        ]

    async def _facts_for_prompt(
        self, story: Story, participant: Optional[Participant], query: str = ""
    ) -> List[Fact]:
        """事实检索：重要度/置信度/时间衰减/未解决加权（对应 12.2 事实排序）。

        `model.embedding.enabled` 且 `liveQuery` 开启时，额外按当前用户文本
        做一次语义相似度加权；没有向量的旧事实仍走规则排序。
        """
        rows = self.db.list_rows("interlude_fact", {"storyId": story.id, "status": "active"})
        facts = [self._row_to_fact(row) for row in rows]
        if not facts:
            return []
        memory = self.memory_config
        w_importance = float(memory.get("factImportanceWeight") or 0.5)
        w_confidence = float(memory.get("factConfidenceWeight") or 0.35)
        w_recency = float(memory.get("factRecencyWeight") or 0.15)
        w_unresolved = float(memory.get("unresolvedWeight") or 0.2)
        now_ts = now_utc().timestamp()
        # 语义相似度（仅在显式开启时请求，避免每轮多一次网络调用）
        semantic: Dict[int, float] = {}
        embedding_config = (self.config.get("model") or {}).get("embedding") or {}
        if embedding_config.get("enabled") and embedding_config.get("liveQuery") and query.strip():
            query_vector = await self.embed_text(query)
            if query_vector:
                for fact in facts:
                    if fact.embedding:
                        semantic[id(fact)] = self._cosine(query_vector, fact.embedding)

        def score(fact: Fact) -> float:
            age_days = max(0.0, (now_ts - fact.lastSeenAt.timestamp()) / 86400)
            recency = 1.0 / (1.0 + age_days / 7.0)
            unresolved_bonus = w_unresolved if (fact.unresolved and fact.scope == "promise") else 0.0
            return (
                fact.importance * w_importance
                + fact.confidence * w_confidence
                + recency * w_recency
                + unresolved_bonus
                + semantic.get(id(fact), 0.0) * 0.35
            )

        facts.sort(key=score, reverse=True)
        limit = int(self.runtime.get("memoryLimit") or 20)
        selected = facts[:limit]
        # 兜底通道：最近已完成事件与未完成承诺各保留（对应固定通道）
        recent_events = sorted(
            [fact for fact in facts if fact.scope == "event" and not fact.unresolved],
            key=lambda fact: fact.lastSeenAt.timestamp(),
            reverse=True,
        )[:3]
        unresolved = [fact for fact in facts if fact.unresolved][:4]
        for fact in recent_events + unresolved:
            if fact not in selected:
                selected.append(fact)
        return selected[: limit + 5]

    async def _run_narrative(self, story: Story, request: NarrativeRequest, group_turn: bool = False) -> Dict[str, Any]:
        """调用主叙事（对应 tryDecide 的调用与统计部分）。"""
        started = time.time()
        result: NarrativeResult = await self.narrator.decide(request, self.config)
        latency_ms = int((time.time() - started) * 1000)
        self._health["latencies"].append(latency_ms)
        if len(self._health["latencies"]) > 50:
            self._health["latencies"] = self._health["latencies"][-50:]
        if result.usage:
            self._health["inputTokens"] += result.usage.promptTokens
            self._health["cachedTokens"] += result.usage.cachedTokens
        if result.succeeded:
            self._health["narrativeTotal"] += 1
        else:
            self._health["narrativeFailed"] += 1
            if result.hadStructureIssue:
                self._health["structureMissing"] += 1
        decision = result.decision or {}
        output: Dict[str, Any] = {
            "success": result.succeeded,
            "decision": decision,
            "error": result.error,
            "latencyMs": latency_ms,
        }
        if result.succeeded:
            effective_now = now_utc()
            decision = await self._validate_decision_side_effects(story, request, decision)
            output["decision"] = decision
            output["effectiveNow"] = effective_now
            if self._log:
                self._log(
                    "info",
                    f"写作完成 阶段={request.phase} 回复模式={visible_reply_mode(decision, request.phase, request.groupContext)} "
                    f"耗时={latency_ms}ms",
                )
        return output

    async def _validate_decision_side_effects(
        self, story: Story, request: NarrativeRequest, decision: Dict[str, Any]
    ) -> Dict[str, Any]:
        """对决策中的外部副作用做宿主侧校验（对应 normalizeAgencyWindowDraft 等）。"""
        now = request.now
        if request.participant and isinstance(decision.get("agencyWindow"), dict):
            recent_ids = {entry.id for entry in request.recentEntries}
            window = agency_mod.normalize_agency_window_draft(
                decision["agencyWindow"], now,
                agency_mod.resolve_agency_config(self.config.get("agency")),
                recent_ids,
                request.recentEntries[-1].id if request.recentEntries else None,
            )
            decision["agencyWindow"] = window
        alter_value = alter_mod.normalize_alter_value(decision.get("alter"))
        decision["alter"] = alter_value
        if isinstance(decision.get("followUpResolutions"), list):
            decision["followUpResolutions"] = [
                item for item in decision["followUpResolutions"]
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            ][:8]
        return decision

    # ------------------------------------------------------------------ #
    # 落库（对应 persistDecision / turn-persistence.ts）
    # ------------------------------------------------------------------ #

    async def _persist_turn(
        self,
        story_id: str,
        participant_id: str,
        decision: Dict[str, Any],
        from_at: datetime,
        now: datetime,
        phase: str,
        success: bool = True,
    ) -> Dict[str, Any]:
        """把一次决策落库：提交、剧本条目、投递账本、记忆/意图/演化。

        返回 {messages, commit, scriptEntryId}；messages 是待投递的可见
        消息草稿（投递成功后回写账本）。
        """
        story = await self.get_story(story_id)
        if story is None:
            return {"messages": [], "commit": None, "scriptEntryId": None, "platformActions": []}
        # 消息回应只属于群聊通道；私聊回合即使模型返回也丢弃。
        decision = {**decision}
        decision.pop("messageReactions", None)
        commit = decision_to_script_commit(
            story_id, phase, from_at, now, participant_id, decision,
            message_separator=str(self.runtime.get("messageSeparator") or DEFAULT_MESSAGE_SEPARATOR),
            split_reply_messages=self.runtime.get("splitReplyMessages") is not False,
        )
        delivery_actions = create_script_delivery_actions(commit)
        scene = await self.active_scene(story_id)
        script_entry_id = await self.append_entry(story_id, {
            "kind": "script",
            "actor": "protagonist",
            "participantId": participant_id,
            "content": commit["prose"],
            "occurredAt": to_iso(now),
            "metadata": {
                "commitId": commit["commitId"],
                "deliveryActions": delivery_actions,
                "phase": phase,
                # rc57：状态演化按来源场景数判断 major 证据；旧条目没有该字段时按 1 个场景处理。
                "sceneId": scene.id if scene else None,
                # 网页观察意图留痕（真正执行由 _schedule_browser_intents 负责）。
                "browserIntents": [
                    event for event in commit["events"] if event.get("kind") == "browser-intent"
                ] or None,
            },
        }, now, participant_id)
        logging_config = self.config.get("logging") or {}
        if logging_config.get("logScriptPreview"):
            preview_length = int(_pick(logging_config.get("previewLength"), 500))
            self._log("info", f"剧本预览 故事={story_id}：{clip(commit['prose'], preview_length)}")
        messages: List[Dict[str, Any]] = []
        # 引用回复：把触发这条回复的消息 ID 透传到投递层（对应 typ-0 replyTo）。
        reply_to = ""
        if phase == "user-message" and (self.config.get("chatActions") or {}).get("quoteReply", True):
            incoming = self.db.first_row(
                "interlude_script_entry",
                {"storyId": story_id, "kind": "user-message", "participantId": participant_id},
                order_by="id DESC",
            )
            if incoming:
                reply_to = str((json_loads(incoming.get("metadata"), {}) or {}).get("messageId") or "")
        for event in commit["events"]:
            if event["kind"] == "outgoing-message":
                reference = {
                    "commitId": commit["commitId"],
                    "eventId": event["eventId"],
                    "scriptEntryId": script_entry_id,
                    "segmentIndex": 0,
                }
                messages.append({
                    "participantId": event.get("participantId") or participant_id,
                    "content": event.get("content") or "",
                    "bubbles": event.get("bubbles") or [],
                    "mode": event.get("deliveryMode") or "immediate",
                    "sendAt": event.get("occurredAt") if event.get("deliveryMode") == "delayed" else None,
                    "scriptEvent": reference,
                    "phase": phase,
                    "replyTo": reply_to,
                })
        # 记忆与意图落库
        for memory in decision.get("memories") or []:
            self.db.insert_row("interlude_memory", {
                "storyId": story_id,
                "participantId": memory.get("participantId") or participant_id,
                "category": clip(memory.get("category"), 32),
                "content": clip(memory.get("content"), 4000),
                "importance": float(memory.get("importance") or 0.5),
                "status": "active",
                "sourceEntryId": script_entry_id,
                "createdAt": now,
                "updatedAt": now,
            })
        for intent in decision.get("intents") or []:
            not_before = ensure_utc(intent.get("notBefore")) or now
            self.db.insert_row("interlude_intent", {
                "storyId": story_id,
                "participantId": intent.get("participantId") or participant_id,
                "type": clip(intent.get("type"), 32),
                "summary": clip(intent.get("summary"), 1000),
                "notBefore": not_before,
                "status": "pending",
                "payload": intent.get("payload") or {},
                "createdAt": now,
                "updatedAt": now,
            })
        for update in decision.get("intentUpdates") or []:
            self.db.update_rows("interlude_intent", {"id": update["id"], "storyId": story_id, "status": "pending"}, {
                "status": update["status"], "updatedAt": now,
                "payload": {"resolution": update.get("resolution")} if update.get("resolution") else {},
            })
        # 承诺回访（用户回合说出的"回头答复"）
        commitment = decision.get("followUpCommitment")
        if phase == "user-message" and isinstance(commitment, dict) and isinstance(commitment.get("summary"), str):
            not_before = ensure_utc(commitment.get("notBefore")) or (now + timedelta(minutes=30))
            expires_at = ensure_utc(commitment.get("expiresAt")) or (now + timedelta(hours=24))
            self.db.insert_row("interlude_intent", {
                "storyId": story_id,
                "participantId": participant_id,
                "type": "follow-up-commitment",
                "summary": clip(commitment.get("summary"), 1000),
                "notBefore": not_before,
                "status": "pending",
                "payload": {
                    "kind": commitment.get("kind") or "thinking",
                    "expiresAt": to_iso(expires_at),
                    "sourceEntryIds": commitment.get("sourceEntryIds") or [],
                },
                "createdAt": now,
                "updatedAt": now,
            })
        for resolution in decision.get("followUpResolutions") or []:
            outcome_map = {"fulfilled": "completed", "rescheduled": "pending", "cancelled": "cancelled"}
            status = outcome_map.get(resolution.get("outcome") or "", "pending")
            changes: Dict[str, Any] = {"status": status, "updatedAt": now}
            if resolution.get("outcome") == "rescheduled":
                changes["notBefore"] = ensure_utc(resolution.get("notBefore")) or (now + timedelta(hours=1))
            self.db.update_rows("interlude_intent", {"id": resolution["id"], "storyId": story_id}, changes)
        # 参与者状态补丁（openThreads / relationshipNotes）
        state_patch = decision.get("statePatch")
        if state_patch and participant_id:
            participant = await self.get_participant(participant_id)
            if participant:
                merged = merge_participant_state(participant.state, state_patch)
                self.db.update_rows("interlude_participant", {"id": participant_id}, {"state": merged, "updatedAt": now})
        # Alter 累计与 emotionalOffset（本轮先累计，后台再触发侧端分析）
        alter_value = decision.get("alter")
        if alter_mod.normalize_alter_value(alter_value) is not None:
            await self._accumulate_alter(story, participant_id or "", int(alter_value), phase, now)
        # agencyWindow 更新
        if decision.get("agencyWindow"):
            await self._update_story_state(story_id, lambda state: {**state, "agencyWindow": decision["agencyWindow"]}, now)
        # 自动投递摘要（后台回合）
        if phase in ("advance", "conversation-follow-up") and isinstance(decision.get("automaticDeliverySummary"), str):
            summary = clip(decision["automaticDeliverySummary"], 240).strip()
            if summary:
                def add_summary(state: Dict[str, Any]) -> Dict[str, Any]:
                    prior = list(state.get("automaticDeliverySummaries") or [])
                    prior.append({
                        "participantId": participant_id,
                        "summary": summary,
                        "sourceEntryId": script_entry_id,
                        "deliveredAt": to_iso(now),
                    })
                    return {**state, "automaticDeliverySummaries": prior[-6:]}

                await self._update_story_state(story_id, add_summary, now)
        # Urge：把本轮草案并入调度状态（basisQuote 必须逐字命中正文才采纳）
        urge_config = urge_mod.resolve_urge_config(self.config.get("urge"))
        if urge_config["enabled"] and script_entry_id and isinstance(decision.get("urge"), dict):
            raw_urge = decision["urge"]

            def apply_urge(state: Dict[str, Any]) -> Dict[str, Any]:
                current = urge_mod.normalize_urge_state(state.get("urge"), now.timestamp())
                committed = urge_mod.commit_urge(
                    current, raw_urge, commit["prose"], script_entry_id,
                    participant_id, now.timestamp(), urge_config,
                )
                return {**state, "urge": committed}

            await self._update_story_state(story_id, apply_urge, now)
        # 平台动作（原生表情 / 本地表情包 / 消息回应）单独成账，交给投递层消费
        platform_actions = [
            {**event, "scriptEntryId": script_entry_id}
            for event in commit["events"]
            if event.get("kind") == "platform-action"
        ]
        # 剧情余波（active-consequence）在验证阶段已过 validIntent，这里直接作为意图写入
        return {
            "messages": messages,
            "commit": commit,
            "scriptEntryId": script_entry_id,
            "platformActions": platform_actions,
        }

    async def _finish_turn(self, story_id: str, participant_id: str, now: datetime, due: Sequence[Intent]) -> None:
        """写作成功后的收尾：游标推进、到期意图结算、关系游标更新。"""
        story = await self.get_story(story_id)
        if story is None:
            return
        self.db.update_rows("interlude_story", {"id": story_id}, {"cursorAt": now, "updatedAt": now_utc()})
        consumed = consumed_live_intent_ids(due)
        if consumed:
            self.db.update_rows("interlude_intent", {"id": {"$in": consumed}, "status": "pending"}, {
                "status": "completed", "updatedAt": now,
            })
        await self._schedule_conversation_follow_ups(story_id, now, participant_id)
        await self._cancel_pending_retries(story_id, participant_id)

    async def _accumulate_alter(self, story: Story, participant_id: str, alter_value: int, phase: str, now: datetime) -> None:
        """Alter 累计；达到阈值时安排后台侧端分析（对应锁内快照的准备部分）。"""
        config = alter_mod.resolve_alter_system_config(self.config.get("alterSystem"))
        if not config.get("enabled"):
            return
        state = story.state
        current = alter_mod.normalize_alter_system_state(state.get("alterSystem"))
        advance = alter_mod.advance_alter_system(current, alter_value, phase, now, config, participant_id)
        new_state = advance["state"]
        if advance["offsetExpired"]:
            self._log("info", "Alter 旧偏移权重过低，已清除")
        await self._update_story_state(story.id, lambda s: {**s, "alterSystem": new_state}, now)
        if advance["thresholdReached"] and not alter_mod.alter_scope_cooling_down(new_state, participant_id, now):
            marked = alter_mod.mark_alter_scope_analysis_attempt(new_state, participant_id, now)
            await self._update_story_state(story.id, lambda s: {**s, "alterSystem": marked}, now)
            asyncio.ensure_future(self._run_alter_analysis(story.id, participant_id, advance["triggerValue"], advance["threshold"], now))

    async def _run_alter_analysis(self, story_id: str, participant_id: str, trigger_value: float, threshold: float, now: datetime) -> None:
        """Alter 侧端分析：锁外模型请求，返回后复查再合并（对应三阶段）。"""
        story = await self.get_story(story_id)
        if story is None:
            return
        config = self.config
        alter_config = alter_mod.resolve_alter_system_config(config.get("alterSystem"))
        direction = "serious" if trigger_value > 0 else "relaxed"
        entries = await self.recent_entries(story_id, 8)
        recent_scripts = [
            {"content": clip(entry.content, 500), "occurredAt": to_iso(entry.occurredAt)}
            for entry in entries if entry.kind == "script"
        ][-4:]
        request = {
            "characterName": story.setting.get("character", {}).get("name") or "",
            "triggerValue": trigger_value,
            "threshold": threshold,
            "direction": direction,
            "recentScripts": recent_scripts,
            "settingOverlay": story.state.get("settingOverlay") or {},
        }
        description = await self.narrator.analyze_alter(request, config)
        if not description:
            return
        fresh = await self.get_story(story_id)
        if fresh is None or fresh.status != "active":
            return
        state = alter_mod.normalize_alter_system_state(fresh.state.get("alterSystem"))
        if state is None:
            return

        def commit(state_data: Dict[str, Any]) -> Dict[str, Any]:
            current = alter_mod.normalize_alter_system_state(state_data.get("alterSystem")) or state
            completed = alter_mod.complete_alter_analysis(
                current, description, threshold, now_utc(), alter_config, participant_id, trigger_value
            )
            return {**state_data, "alterSystem": completed}

        await self._update_story_state(story_id, commit)

    # ------------------------------------------------------------------ #
    # 投递（delivery）
    # ------------------------------------------------------------------ #

    def split_outgoing_message(self, content: str) -> List[str]:
        return split_visible_reply_bubbles(
            content,
            separator=str(self.runtime.get("messageSeparator") or DEFAULT_MESSAGE_SEPARATOR),
            split_enabled=self.runtime.get("splitReplyMessages") is not False,
            newline_as_separator=self.runtime.get("convertNewlineToSeparator") is True,
        )

    def typing_delay_milliseconds(self, next_segment: str) -> int:
        """模拟下一段气泡的输入时间（对应 typingDelayMilliseconds，含抖动）。

        注意：0 是合法配置值（如 typingBaseDelaySeconds=0 关闭基础等待），
        必须用 None 判断而不是 `or` 兜底，否则 0 会被替换成默认值。
        """
        base_seconds = max(0.0, float(_pick(self.runtime.get("typingBaseDelaySeconds"), 1)))
        chars_per_second = max(1.0, float(_pick(self.runtime.get("typingCharactersPerSecond"), 8)))
        maximum_seconds = max(base_seconds, float(_pick(self.runtime.get("typingMaxDelaySeconds"), 12)))
        import math

        nominal = min(maximum_seconds, base_seconds + math.ceil(len(next_segment) / chars_per_second))
        jitter = max(0.0, min(0.5, float(_pick(self.runtime.get("typingJitterRatio"), 0.3))))
        factor = 1 + (random.random() * 2 - 1) * jitter if jitter else 1.0
        return max(250, min(int(maximum_seconds * 1000), int(round(nominal * factor * 1000))))

    def first_message_typing_hold_ms(self, content: str, request_started: float) -> int:
        """首条消息的打字下限补足（对应 firstMessageTypingHoldMs）。"""
        floor = self.typing_delay_milliseconds(content)
        elapsed = max(0.0, (time.time() - request_started) * 1000)
        return max(0, floor - int(elapsed))

    async def _deliver_messages_for_participant(
        self,
        story: Story,
        participant: Participant,
        messages: Sequence[Dict[str, Any]],
        request_started: float,
        is_automatic: bool = False,
    ) -> List[Dict[str, Any]]:
        """投递一次写作产生的可见消息（即时/延迟/分段）。

        即时消息经打字模拟分段投递；延迟与后续分段保存为 split-message
        意图，由后台扫描按到期时间投递（对应 split-message + 账本回写）。
        """
        delivered: List[Dict[str, Any]] = []
        first_bubble = True
        for message in messages:
            target_participant_id = message.get("participantId") or participant.id
            target = participant if target_participant_id == participant.id else await self.get_participant(target_participant_id)
            if target is None:
                continue
            if message.get("mode") == "delayed":
                self.db.insert_row("interlude_intent", {
                    "storyId": story.id,
                    "participantId": target.id,
                    "type": "delayed-message",
                    "summary": clip(message.get("content"), 500),
                    "notBefore": ensure_utc(message.get("sendAt")) or now_utc(),
                    "status": "pending",
                    "payload": {
                        "content": message.get("content"),
                        "scriptEvent": message.get("scriptEvent"),
                        "replyTo": message.get("replyTo") or "",
                        "automaticDelivery": {"summary": clip(message.get("content"), 240)} if is_automatic else None,
                    },
                    "createdAt": now_utc(),
                    "updatedAt": now_utc(),
                })
                self._schedule_due_intent_wake(story.id)
                continue
            bubbles = message.get("bubbles") or self.split_outgoing_message(message.get("content") or "")
            for index, bubble in enumerate(bubbles):
                if index > 0:
                    await asyncio.sleep(self.typing_delay_milliseconds(bubble) / 1000.0)
                elif first_bubble and request_started:
                    hold = self.first_message_typing_hold_ms(bubble, request_started)
                    if hold > 0:
                        await asyncio.sleep(hold / 1000.0)
                first_bubble = False
                logging_config = self.config.get("logging") or {}
                if logging_config.get("logMessageContent"):
                    preview_length = int(_pick(logging_config.get("previewLength"), 500))
                    self._log("info", f"发送消息 目标={target.id}：{clip(bubble, preview_length)}")
                outcome = await self._transport_send(
                    self._delivery_umo(target), bubble,
                    reply_to=str(message.get("replyTo") or "") if index == 0 else "",
                )
                reference = dict(message.get("scriptEvent") or {})
                if reference:
                    reference["segmentIndex"] = index
                if outcome == "delivered":
                    delivered.append({"participantId": target.id, "content": bubble})
                    await self._mark_segment_delivered(story.id, reference, "delivered", "delivery-ok")
                elif outcome == "unknown":
                    # 回执超时：消息可能已送达，只记 pending，不排补偿（避免重复发送）。
                    await self._mark_segment_delivered(
                        story.id, reference, "pending", "transport-timeout: 回执超时，结果未知"
                    )
                else:
                    await self._mark_segment_delivered(story.id, reference, "failed", "transport-error")
                    await self._schedule_narrative_delivery_retry(story, target, bubble)
                if index < len(bubbles) - 1:
                    # 后续气泡持久化为分段意图（重启恢复只补发未确认分段）
                    self.db.insert_row("interlude_intent", {
                        "storyId": story.id,
                        "participantId": target.id,
                        "type": "split-message",
                        "summary": clip(bubbles[index + 1], 500),
                        "notBefore": now_utc(),
                        "status": "completed",
                        "payload": {
                            "content": bubbles[index + 1],
                            "deliveredInline": True,
                            "scriptEvent": {**reference, "segmentIndex": index + 1},
                        },
                        "createdAt": now_utc(),
                        "updatedAt": now_utc(),
                    })
        # 更新参与者投递时间与未读/待回复计数
        fresh = await self.get_participant(participant.id)
        if fresh and delivered:
            state = dict(fresh.state or {})
            state["lastCharacterMessageAt"] = to_iso(now_utc())
            self.db.update_rows("interlude_participant", {"id": fresh.id}, {"state": state, "updatedAt": now_utc()})
            await self._acknowledge_urge(story, participant, now_utc())
        return delivered

    async def _acknowledge_urge(self, story: Story, participant: Participant, now: datetime) -> None:
        """一次真实发生的主动投递把 urge 的"上膛"转成 burst 窗口。"""
        config = urge_mod.resolve_urge_config(self.config.get("urge"))
        if not config["enabled"]:
            return
        state = urge_mod.normalize_urge_state(story.state.get("urge"), now.timestamp())
        armed = state.get("armed")
        if not armed:
            return
        updated = urge_mod.acknowledge_urge(
            state, participant.id, int(armed.get("entryId") or 0), now.timestamp()
        )
        if updated is state:
            return
        await self._update_story_state(story.id, lambda current: {**current, "urge": updated}, now)

    #: QQ 原生表情语义 → 官方 face id。只用于 `chatActions.allowedNativeFaces`
    #: 允许的语义；未登记的语义会以 `cancelled` 落账，而不是永久 pending。
    NATIVE_FACE_IDS: Dict[str, str] = {
        "smile": "14",
        "laugh": "182",
        "sweat": "27",
        "awkward": "111",
        "heart": "66",
        "surprised": "0",
        "sad": "5",
        "angry": "106",
        "cry": "5",
        "shy": "6",
        "cool": "16",
        "confused": "32",
        "sleepy": "8",
    }

    #: QQ 消息回应 → OneBot `set_msg_emoji_like` 的 emoji_id（与上游一致）。
    QQ_REACTION_IDS: Dict[str, str] = {
        "like": "76",
        "smile": "14",
        "laugh": "182",
        "heart": "66",
        "surprised": "0",
        "sad": "5",
        "angry": "106",
    }

    _STICKER_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    _STICKER_AUDIO_EXT = (".mp3", ".wav", ".ogg", ".silk", ".amr", ".m4a")

    async def _deliver_platform_actions(
        self,
        story: Story,
        default_participant: Optional[Participant],
        actions: Sequence[Dict[str, Any]],
        now: datetime,
        umo_override: str = "",
    ) -> None:
        """投递平台动作（原生表情 / 本地表情包 / 消息回应）。

        每条 segment 独立回写账本：
        - 成功 → `delivered`；
        - 回执超时 → `pending`（可能已送达，不重发）；
        - 确定失败 → `failed`；
        - 未启用、素材不存在、平台无此能力 → `cancelled` 并写原因。
        """
        if not actions:
            return
        chat = self.config.get("chatActions") or {}
        stickers = self.config.get("stickers") or {}
        for action in actions:
            participant_id = action.get("participantId") or (
                default_participant.id if default_participant else ""
            )
            if default_participant is not None and participant_id == default_participant.id:
                target = default_participant
            elif participant_id:
                target = await self.get_participant(participant_id)
            else:
                target = None
            if target is None and not umo_override:
                continue
            umo = umo_override or self._delivery_umo(target)
            for segment in delivery_segments(action):
                reference = {
                    "commitId": action.get("commitId"),
                    "eventId": action.get("eventId"),
                    "scriptEntryId": action.get("scriptEntryId"),
                    "segmentIndex": segment.get("index"),
                }
                kind = segment.get("kind")
                content = str(segment.get("content") or "")
                outcome = "cancelled"
                reason = ""
                if not umo:
                    reason = "no-delivery-endpoint"
                elif kind == "native-face":
                    allowed = [str(item) for item in (chat.get("allowedNativeFaces") or [])]
                    face_id = self.NATIVE_FACE_IDS.get(content)
                    if not chat.get("enabled"):
                        reason = "chatActions 未启用"
                    elif not chat.get("nativeFaces", True):
                        reason = "chatActions.nativeFaces 已关闭"
                    elif content not in allowed:
                        reason = f"表情语义不在 allowedNativeFaces：{content}"
                    elif face_id is None:
                        reason = f"未登记的 QQ 表情语义：{content}"
                    else:
                        outcome = await self._transport_call("send_face", umo, face_id)
                elif kind == "local-media":
                    if not stickers.get("enabled"):
                        reason = "stickers 未启用"
                    else:
                        path = self._resolve_sticker(content)
                        if path is None:
                            reason = f"素材不存在：{content}"
                        else:
                            media_kind = "audio" if path.lower().endswith(self._STICKER_AUDIO_EXT) else "image"
                            outcome = await self._transport_call("send_media", umo, path, media_kind)
                elif kind == "message-reaction":
                    _, _, reaction = content.partition(":")
                    allowed = [str(item) for item in _pick(chat.get("allowedReactions"), list(self.QQ_REACTION_IDS))]
                    emoji_id = self.QQ_REACTION_IDS.get(reaction)
                    target_id = str(segment.get("targetId") or "")
                    if not chat.get("enabled"):
                        reason = "chatActions 未启用"
                    elif not chat.get("messageReactions", True):
                        reason = "chatActions.messageReactions 已关闭"
                    elif reaction not in allowed:
                        reason = f"回应不在 allowedReactions：{reaction}"
                    elif emoji_id is None:
                        reason = f"未登记的回应类型：{reaction}"
                    elif not target_id:
                        reason = "回应目标缺少平台消息 ID"
                    elif not hasattr(self._transport, "send_reaction"):
                        reason = "unsupported: 当前适配器没有 send_reaction"
                    else:
                        outcome = await self._transport_call("send_reaction", umo, target_id, emoji_id)
                else:
                    reason = f"未知动作类型：{kind}"
                if outcome == "delivered":
                    await self._mark_segment_delivered(story.id, reference, "delivered", "delivery-ok")
                elif outcome == "unknown":
                    await self._mark_segment_delivered(
                        story.id, reference, "pending", "transport-timeout: 回执超时，结果未知"
                    )
                elif outcome == "failed":
                    await self._mark_segment_delivered(story.id, reference, "failed", "transport-error")
                    self._log("warn", f"平台动作投递失败 类型={kind} 目标={participant_id}")
                else:
                    # cancelled 不可被后续覆盖：这是对"本移植做不到"的明确记录。
                    await self._mark_segment_delivered(story.id, reference, "cancelled", reason or "cancelled")
                    self._log("debug", f"平台动作跳过 类型={kind} 原因={reason}")

    async def _transport_call(self, method: str, *args: Any) -> str:
        """调用平台适配器的可选方法，统一三态返回（缺失方法视为确定失败）。"""
        handler = getattr(self._transport, method, None)
        if not callable(handler):
            return "failed"
        try:
            result = await handler(*args)
        except DeliveryUnknown as error:
            self._log("warn", f"{method} 结果未知（可能已送达，不自动重试）：{error.reason}")
            return "unknown"
        except Exception as error:  # noqa: BLE001 - 动作投递失败不能影响回合
            self._log("warn", f"{method} 失败：{error}")
            return "failed"
        if isinstance(result, str) and result in ("delivered", "failed", "unknown"):
            return result
        return "delivered" if bool(result) else "failed"

    def sticker_root(self) -> str:
        """本地表情包根目录。

        相对路径优先按插件数据目录解析；若该目录不存在但当前工作目录下
        存在同名目录，则使用工作目录（兼容上游默认的 `data/hds-interlude/stickers`）。
        """
        raw = str((self.config.get("stickers") or {}).get("directory") or "data/hds-interlude/stickers")
        if not os.path.isabs(raw):
            candidate = os.path.join(self.data_dir, raw)
            if not os.path.isdir(candidate):
                cwd_candidate = os.path.abspath(raw)
                if os.path.isdir(cwd_candidate):
                    return cwd_candidate
            raw = candidate
        return os.path.abspath(raw)

    @staticmethod
    def _inside_root(root_real: str, candidate: str) -> bool:
        """判断真实路径是否仍在素材根目录内（挡符号链接/junction 越界）。"""
        try:
            return os.path.commonpath([root_real, candidate]) == root_real
        except ValueError:  # Windows 跨盘符
            return False

    def _sticker_files(self) -> List[str]:
        """扫描素材目录（带 60 秒缓存），只返回图片/语音且体积合规的文件。

        使用 `realpath` 双侧边界判定：root 内的符号链接/junction 指向目录外时
        直接跳过（对应上游 rc56 的表情包回源 realpath 加固）。
        """
        cached = getattr(self, "_sticker_cache", None)
        now = time.time()
        if cached and now - cached[0] < 60:
            return cached[1]
        root = self.sticker_root()
        root_real = os.path.realpath(root)
        limit = int(_pick((self.config.get("stickers") or {}).get("maxFileSizeMB"), 10)) * 1024 * 1024
        files: List[str] = []
        if os.path.isdir(root):
            for folder, _dirs, names in os.walk(root):
                for name in names:
                    if not name.lower().endswith(self._STICKER_IMAGE_EXT + self._STICKER_AUDIO_EXT):
                        continue
                    path = os.path.join(folder, name)
                    real = os.path.realpath(path)
                    if not self._inside_root(root_real, real):
                        self._log("warn", f"表情包越界，已跳过：{path}")
                        continue
                    try:
                        if os.path.getsize(real) > limit:
                            continue
                    except OSError:
                        continue
                    files.append(real)
        files.sort()
        self._sticker_cache = (now, files)
        return files

    def _resolve_sticker(self, asset_id: str) -> Optional[str]:
        """把 `localMedia.assetId` 解析为真实文件路径。

        支持三种写法：`相对路径（不含扩展名）`、`文件名（不含扩展名）`、
        `一级分组/文件名`。拒绝 `..` 与绝对路径，防止越权读取。
        """
        asset = (asset_id or "").strip().replace("\\", "/")
        if not asset or asset.startswith("/") or ".." in asset.split("/"):
            return None
        root = os.path.realpath(self.sticker_root())
        target = asset.lower()
        for path in self._sticker_files():
            relative = os.path.relpath(path, root).replace("\\", "/")
            without_ext = os.path.splitext(relative)[0]
            stem = os.path.splitext(os.path.basename(relative))[0]
            if target in (without_ext.lower(), stem.lower()) or without_ext.lower().endswith("/" + target):
                return path
        return None

    def _media_catalog(self) -> List[Dict[str, Any]]:
        """提供给主模型的可用素材清单（只含真实存在的 assetId）。"""
        stickers = self.config.get("stickers") or {}
        if not stickers.get("enabled"):
            return []
        limit = int(_pick(stickers.get("catalogLimit"), 40))
        root = os.path.realpath(self.sticker_root())
        catalog: List[Dict[str, Any]] = []
        for path in self._sticker_files()[:limit]:
            relative = os.path.relpath(path, root).replace("\\", "/")
            without_ext = os.path.splitext(relative)[0]
            catalog.append({
                "assetId": without_ext,
                "kind": "audio" if path.lower().endswith(self._STICKER_AUDIO_EXT) else "image",
                "group": without_ext.split("/")[0] if "/" in without_ext else "",
            })
        return catalog

    async def _transport_send(self, umo: str, text: str, reply_to: str = "") -> str:
        """经平台层发送一条文本消息，返回投递状态。

        返回 `"delivered"` / `"failed"` / `"unknown"`：
        - `unknown` 表示请求已写出但回执超时（`DeliveryUnknown`）——消息可能
          已经到达，账本必须记 `pending`，且不得自动重试（对应上游 rc53/rc56）；
        - 其余异常一律按确定失败处理，允许补偿重试。

        `reply_to` 是触发消息 ID（对应 typ-0 的 replyTo 透传）；由平台层组装
        `Reply` 组件，服务层不关心平台消息段格式。
        """
        if not umo or not text:
            return "failed"
        try:
            return "delivered" if bool(await self._transport.send(umo, text, reply_to)) else "failed"
        except DeliveryUnknown as error:
            self._log("warn", f"消息投递结果未知（可能已送达，不自动重试）：{error.reason}")
            return "unknown"
        except Exception as error:  # noqa: BLE001 - 投递失败按账本记录，不能影响回合
            self._log("warn", f"消息投递失败：{error}")
            return "failed"

    async def _mark_segment_delivered(
        self, story_id: str, reference: Dict[str, Any], status: str, reason: str = ""
    ) -> None:
        """按 segment 回写源剧本条目的投递账本（对应 updateScriptDeliveryOutcome）。"""
        if not reference or not isinstance(reference.get("scriptEntryId"), int):
            return
        row = self.db.first_row("interlude_script_entry", {"id": reference["scriptEntryId"], "storyId": story_id})
        if row is None:
            return
        metadata = json_loads(row.get("metadata"), {}) or {}
        actions = metadata.get("deliveryActions")
        updated = update_script_delivery_actions(actions, reference, status, now_utc(), reason or None)
        if updated is None:
            return
        metadata["deliveryActions"] = updated
        self.db.update_rows("interlude_script_entry", {"id": reference["scriptEntryId"]}, {"metadata": metadata})

    async def update_script_delivery_outcome(
        self, story_id: str, reference: Dict[str, Any], status: str, at: datetime, reason: str = ""
    ) -> None:
        """给外部调用（命令/后台）的账本回写入口。"""
        await self._mark_segment_delivered(story_id, reference, status, reason)

    async def _schedule_narrative_delivery_retry(self, story: Story, participant: Participant, content: str) -> None:
        """投递失败：进入 30 秒补偿重试（对应 failed → 待补偿重试）。"""
        self.db.insert_row("interlude_intent", {
            "storyId": story.id,
            "participantId": participant.id,
            "type": "delayed-message",
            "summary": clip(content, 500),
            "notBefore": now_utc() + timedelta(seconds=30),
            "status": "pending",
            "payload": {"content": content, "deliveryRetry": True},
            "createdAt": now_utc(),
            "updatedAt": now_utc(),
        })
        self._schedule_due_intent_wake(story.id)

    # ------------------------------------------------------------------ #
    # 叙事失败重试（narrative-retry）
    # ------------------------------------------------------------------ #

    async def schedule_narrative_retry(self, story_id: str, participant_id: str) -> None:
        """叙事失败后的自动重试（对应 scheduleNarrativeRetry）。"""
        max_attempts = int(_pick(self.runtime.get("narrativeRetryMaxAttempts"), 6))
        if max_attempts <= 0:
            return
        key = f"{story_id}|{participant_id}"
        entry = self._pending_retries.get(key) or {"attempts": 0}
        if entry["attempts"] >= max_attempts:
            self._log("warn", f"叙事重试达到上限 {max_attempts} 次，停止重试 参与者={participant_id}")
            return
        entry["attempts"] += 1
        delay = max(5, int(self.runtime.get("narrativeRetryDelaySeconds") or 15))
        self._pending_retries[key] = entry
        self.db.insert_row("interlude_intent", {
            "storyId": story_id,
            "participantId": participant_id,
            "type": "narrative-retry",
            "summary": "叙事失败自动重试",
            "notBefore": now_utc() + timedelta(seconds=delay),
            "status": "pending",
            "payload": {"attempt": entry["attempts"]},
            "createdAt": now_utc(),
            "updatedAt": now_utc(),
        })
        self._schedule_due_intent_wake(story_id)

    async def _cancel_pending_retries(self, story_id: str, participant_id: str) -> None:
        key = f"{story_id}|{participant_id}"
        self._pending_retries.pop(key, None)
        self.db.update_rows(
            "interlude_intent",
            {"storyId": story_id, "participantId": participant_id, "type": "narrative-retry", "status": "pending"},
            {"status": "cancelled", "updatedAt": now_utc()},
        )

    # ------------------------------------------------------------------ #
    # 到期意图与调度唤醒
    # ------------------------------------------------------------------ #

    async def due_intents(self, story_id: str, now: datetime, include_active: bool = False) -> List[Intent]:
        """到期意图（对应 dueIntents）；active-consequence 默认不列入到期表。"""
        rows = self.db.list_rows(
            "interlude_intent",
            {"storyId": story_id, "status": "pending", "notBefore": {"$lte": to_iso(now)}},
            order_by="notBefore ASC, id ASC",
            limit=50,
        )
        intents = [self._row_to_intent(row) for row in rows]
        if not include_active:
            intents = [intent for intent in intents if intent.type != "active-consequence"]
        return intents

    async def _pending_intents(self, story_id: str) -> List[Intent]:
        rows = self.db.list_rows("interlude_intent", {"storyId": story_id, "status": "pending"}, order_by="notBefore ASC, id ASC")
        return [self._row_to_intent(row) for row in rows]

    def _schedule_due_intent_wake(self, story_id: str) -> None:
        """把每个故事的到期唤醒时间重新校准（keep-earliest 语义在调度器内）。"""
        rows = self.db.list_rows(
            "interlude_intent", {"storyId": story_id, "status": "pending"}, order_by="notBefore ASC", limit=1
        )
        next_at = ensure_utc(rows[0]["notBefore"]) if rows else None
        automation = self._automation_of(story_id)
        advance_at = ensure_utc(automation.get("nextAdvanceAt"))
        candidates = [moment for moment in (next_at, advance_at) if moment]
        if not candidates:
            return
        earliest = min(candidates)
        self.scheduler.schedule_wake(story_id, earliest.timestamp(), lambda: asyncio.ensure_future(self.sweep()))

    def _automation_of(self, story_id: str) -> Dict[str, Any]:
        row = self.db.first_row("interlude_story", {"id": story_id})
        if not row:
            return {}
        state = normalize_story_state(row.get("state"))
        return dict(state.get("automation") or {})

    async def _set_automation(self, story_id: str, changes: Dict[str, Any]) -> None:
        if self._closed:
            return

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            automation = dict(state.get("automation") or {})
            automation.update(changes)
            return {**state, "automation": automation}

        try:
            await self._update_story_state(story_id, mutate)
        except Exception:  # noqa: BLE001 - 卸载竞态：数据库关闭后放弃写回
            if not self._closed:
                self._log("debug", "自动推进时间写回失败（数据库关闭竞态）")

    # ------------------------------------------------------------------ #
    # 自动生活推进（advance）
    # ------------------------------------------------------------------ #

    async def advance_story(self, story: Story, force: bool = True) -> List[Dict[str, Any]]:
        """把剧本补写到现在（对应 advanceStory）。"""
        if self._paused:
            return []
        if not self.can_handle_story(story):
            return []
        messages = await self.serial(story.id, lambda: self._advance_unlocked(story.id, now_utc(), force))
        await self.schedule_compaction(story.id)
        return messages

    async def _advance_unlocked(self, story_id: str, now: datetime, force: bool) -> List[Dict[str, Any]]:
        story = await self.get_story(story_id)
        if story is None:
            return []
        automation = dict(story.state.get("automation") or {})
        from_at = self.narrative_cursor(story, now)
        # 手动推进间隔限制（minimumAdvanceMinutes）
        if force:
            minimum = int(self.runtime.get("minimumAdvanceMinutes") or 30)
            last_auto = ensure_utc(automation.get("lastAutoAdvanceAt"))
            if last_auto and not self._due_any(story_id, now) and (now - from_at) < timedelta(minutes=minimum):
                self._log("info", "手动推进被最小间隔限制跳过（没有到期任务）")
        # 处理到期意图（split-message 补投 / 提醒 / 承诺）
        handled = await self._process_due_intents(story, now)
        due = live_narrative_intents(await self.due_intents(story_id, now))
        due = [intent for intent in due if intent.type in ("delayed-message", "follow-up-commitment", "proactive-check")]
        phase = "advance"
        if due:
            has_commitment = any(intent.type == "follow-up-commitment" for intent in due)
            has_proactive = any(intent.type == "proactive-check" for intent in due)
            if has_commitment:
                phase = "intent-due"
            elif has_proactive:
                phase = "intent-due"
        if not force and not due and not self._advance_due(story, now):
            return []
        request = await self._build_request(story, None, phase, from_at, now)
        request.dueIntents = due
        request.timelinePlan = await self._plan_timeline(story, request, now)
        result = await self._run_narrative(story, request)
        if not result.get("success"):
            # 自动回合失败：记录重试门（timelineRetryAt），不冻结推进
            await self._set_automation(story_id, {
                "timelineRetryAt": to_iso(now + timedelta(minutes=int(self.runtime.get("sweepIntervalMinutes") or 5))),
                "lastAutoAdvanceAt": to_iso(now),
                "nextAdvanceAt": to_iso(now + timedelta(minutes=max(5, int(self.runtime.get("autoAdvanceIntervalMinutes") or 40)))),
            })
            return []
        decision = result["decision"]
        # 时间导演的 carry 作为下一段生活的连续性提示（主叙事自带的优先）。
        if request.timelinePlan and request.timelinePlan.carry and not decision.get("timelineCarry"):
            decision["timelineCarry"] = list(request.timelinePlan.carry)
        persisted = await self._persist_turn(story_id, "", decision, from_at, now, phase)
        self._schedule_browser_intents(story, decision, "", now)
        # 自动回合的主动联系：交叉行动投递 + agency 目标
        messages = list(persisted.get("messages") or [])
        immediate = [message for message in messages if message.get("mode") == "immediate"]
        if immediate:
            for message in immediate:
                target = await self.get_participant(message.get("participantId") or "")
                if target:
                    await self._deliver_messages_for_participant(story, target, [message], 0.0, is_automatic=True)
        # 自动回合的平台动作：按参与者分别投递（原生表情/表情包/回应）
        for action in persisted.get("platformActions") or []:
            target = await self.get_participant(action.get("participantId") or "")
            if target:
                await self._deliver_platform_actions(story, target, [action], now)
        await self._finish_turn(story_id, "", now, due)
        await self._apply_proactive_contact(story, decision, now)
        await self._apply_auto_decision_side_effects(story, decision, now)
        self._schedule_next_automatic_advance(story_id, now)
        return messages

    async def _apply_auto_decision_side_effects(self, story: Story, decision: Dict[str, Any], now: datetime) -> None:
        """自动回合的附加副作用：剧情余波 / 生活节奏 / 时间账本 carry。"""
        timeline_carry = []
        if isinstance(decision.get("timelineCarry"), list):
            timeline_carry = [clip(item, 200) for item in decision["timelineCarry"] if isinstance(item, str)][:6]
        updates: Dict[str, Any] = {}
        if timeline_carry:
            updates["timelineCarry"] = timeline_carry
        if updates:
            await self._update_story_state(story.id, lambda state: {**state, **updates}, now)

    async def _apply_proactive_contact(self, story: Story, decision: Dict[str, Any], now: datetime) -> None:
        """主动联系候选的容量判断（对应 Agency 容量门与 proactive-check 重查）。"""
        candidate = decision.get("proactiveContact")
        if not isinstance(candidate, dict):
            return
        config = agency_mod.resolve_agency_config(self.config.get("agency"))
        if not config.get("enabled"):
            return
        if not self.runtime.get("allowProactiveMessages", False):
            return
        participant_id = candidate.get("participantId") or ""
        participant = await self.get_participant(participant_id)
        if participant is None:
            return
        normalized = agency_mod.normalize_proactive_contact(
            candidate, now, config, {participant_id},
            {entry.id for entry in await self.recent_entries(story.id, 50)},
            None,
        )
        if normalized is None:
            self._log("debug", "主动联系候选未通过证据校验，已丢弃")
            return
        state = story.state
        window = agency_mod.active_agency_window(state.get("agencyWindow"), now)
        log = list(state.get("proactiveContactLog") or [])
        daily_cap = int(config.get("proactiveDailyCap") or 3)
        if daily_cap > 0 and agency_mod.count_recent_proactive_contacts(log, participant_id, now, daily_cap) >= daily_cap:
            self._log("info", f"主动联系达到每日上限（{daily_cap}），候选放下")
            return
        capacity = agency_mod.evaluate_agency_capacity(
            window, normalized, now, config, participant.state.get("lastCharacterMessageAt")
        )
        if normalized["outcome"] == "let-go":
            return
        if normalized["outcome"] == "recheck-later" or not capacity["allowed"]:
            recheck_at = agency_mod.proactive_recheck_at(normalized, capacity, window, now)
            self.db.insert_row("interlude_intent", {
                "storyId": story.id,
                "participantId": participant_id,
                "type": "proactive-check",
                "summary": clip(normalized["motive"], 500),
                "notBefore": recheck_at,
                "status": "pending",
                "payload": normalized,
                "createdAt": now,
                "updatedAt": now,
            })
            self._schedule_due_intent_wake(story.id)
            self._log("info", f"主动联系未放行（{capacity['reason']}），已安排重查 {to_iso(recheck_at)}")
            return
        # send-now：由同回合的 crossConversationActions 投递，这里只记配额
        log.append({
            "participantId": participant_id,
            "at": to_iso(now),
            "channelReason": normalized["origin"],
            "ref": normalized.get("motive", "")[:64],
        })
        await self._update_story_state(story.id, lambda s: {**s, "proactiveContactLog": log[-20:]}, now)

    def _advance_due(self, story: Story, now: datetime) -> bool:
        """判断自动推进是否到期（对应调度三元组：quietUntil / nextAdvanceAt / restWindows）。"""
        automation = dict(story.state.get("automation") or {})
        quiet_until = ensure_utc(automation.get("quietUntil"))
        if quiet_until and now < quiet_until:
            return False
        next_advance = ensure_utc(automation.get("nextAdvanceAt"))
        if next_advance is None:
            return True
        return now >= next_advance

    def _schedule_next_automatic_advance(self, story_id: str, now: datetime) -> None:
        """计算下一次自动推进时间（对应 scheduleNextAutomaticAdvance + restWindows）。"""
        story_row = self.db.first_row("interlude_story", {"id": story_id})
        if not story_row:
            return
        story = self._row_to_story(story_row)
        runtime = self.runtime
        if not runtime.get("autoAdvanceEnabled", True):
            self.scheduler.cancel_wake(story_id)
            return
        interval = int(runtime.get("autoAdvanceIntervalMinutes") or 40)
        jitter = int(runtime.get("autoAdvanceJitterMinutes") or 5)
        timezone_name = story.setting.get("timezone") or "UTC"
        # Urge 开启时用弹性算法替代固定间隔（对应 planUrge）：
        # 生活节奏 / 对话密度 / 休息窗口 / burst 窗口共同决定下一次推进。
        urge_config = urge_mod.resolve_urge_config(self.config.get("urge"))
        if urge_config["enabled"]:
            stamp = now.timestamp()
            urge_state = urge_mod.normalize_urge_state(story.state.get("urge"), stamp)
            rest = urge_mod.rest_minutes_for(now, timezone_name, self._rest_windows())
            participant_rows = self.db.list_rows(
                "interlude_participant", {"storyId": story_id, "status": "active"}
            )
            unavailable = not any(row.get("channelId") for row in participant_rows)
            planned = urge_mod.plan_urge(
                urge_state, stamp, urge_config, rest, unavailable,
                rhythm=urge_mod.rhythm_from_state(urge_state, stamp),
            )
            next_at = planned["nextAdvanceAt"]

            async def save_urge() -> None:
                await self._update_story_state(
                    story_id, lambda state: {**state, "urge": planned["state"]}, now
                )
                await self._set_automation(story_id, {
                    "nextAdvanceAt": to_iso(next_at),
                    "urgeReason": planned["reason"],
                })

            asyncio.ensure_future(save_urge())
            self._log(
                "debug",
                f"Urge 排程 原因={planned['reason']} 间隔={planned['minutes']:.1f}分钟",
            )
            self.scheduler.schedule_wake(story_id, next_at.timestamp(), lambda: asyncio.ensure_future(self.sweep()))
            return
        # 休息窗口内使用其自己的推进节奏
        for window in self._rest_windows():
            if window.get("enabled") is False:
                continue
            start = parse_hhmm(str(window.get("start") or "23:00"))
            end = parse_hhmm(str(window.get("end") or "07:00"))
            if start is None or end is None:
                continue
            minutes_now = local_clock_minutes(now, timezone_name)
            if in_clock_window(minutes_now, start, end):
                interval = int(window.get("minIntervalMinutes") or 120)
                jitter = max(0, int(window.get("maxIntervalMinutes") or 240) - interval)
                break
        delay_minutes = interval + (random.randint(0, jitter) if jitter > 0 else 0)
        next_at = now + timedelta(minutes=delay_minutes)
        asyncio.ensure_future(self._set_automation(story_id, {"nextAdvanceAt": to_iso(next_at)}))
        self.scheduler.schedule_wake(story_id, next_at.timestamp(), lambda: asyncio.ensure_future(self.sweep()))

    async def _schedule_conversation_follow_ups(self, story_id: str, now: datetime, participant_id: str) -> None:
        """对话后的短期补写时间点（对应 conversationFollowUpMinutes，默认 10/20 分钟）。"""
        runtime = self.runtime
        follow_ups = runtime.get("conversationFollowUpMinutes") or [10, 20]
        jitter = int(runtime.get("conversationFollowUpJitterMinutes") or 1)
        times = []
        for minutes in follow_ups:
            try:
                base = int(minutes)
            except (TypeError, ValueError):
                continue
            offset = base + (random.randint(0, jitter) if jitter > 0 else 0)
            times.append(to_iso(now + timedelta(minutes=offset)))
        if times:
            await self._set_automation(story_id, {
                "conversationFollowUpAt": times,
                "conversationFollowUpParticipantId": participant_id,
                "quietUntil": to_iso(now + timedelta(minutes=int(follow_ups[0]) if follow_ups else 10)),
            })

    async def _process_due_intents(self, story: Story, now: datetime) -> int:
        """处理到期意图：分段补投、延迟消息、重试、提醒、承诺。返回处理数量。"""
        handled = 0
        due = await self.due_intents(story.id, now)
        for intent in due:
            if intent.type == "split-message":
                payload = intent.payload or {}
                if payload.get("deliveredInline"):
                    self.db.update_rows("interlude_intent", {"id": intent.id, "status": "pending"}, {"status": "completed", "updatedAt": now})
                    continue
                participant = await self.get_participant(intent.participantId) if intent.participantId else None
                content = clip(payload.get("content"), int(self.runtime.get("maxMessageCharacters") or 1500))
                if participant and content:
                    outcome = await self._transport_send(
                        self._delivery_umo(participant), content,
                        reply_to=str(payload.get("replyTo") or ""),
                    )
                    reference = payload.get("scriptEvent") or {}
                    await self._mark_segment_delivered(
                        story.id,
                        reference,
                        "delivered" if outcome == "delivered" else ("pending" if outcome == "unknown" else "failed"),
                        "split-due" if outcome != "unknown" else "transport-timeout: 回执超时，结果未知",
                    )
                    if outcome == "unknown":
                        # 结果未知：不自动重发（可能已送达），意图就地收口并留痕。
                        self.db.update_rows(
                            "interlude_intent", {"id": intent.id, "status": "pending"},
                            {
                                "status": "completed",
                                "payload": {**payload, "deliveryUnknown": True},
                                "updatedAt": now,
                            },
                        )
                    else:
                        self.db.update_rows(
                            "interlude_intent", {"id": intent.id, "status": "pending"},
                            {"status": "completed" if outcome == "delivered" else "pending", "updatedAt": now},
                        )
                        if outcome == "failed":
                            self.db.update_rows("interlude_intent", {"id": intent.id}, {"notBefore": now + timedelta(seconds=30)})
                    handled += 1
                else:
                    self.db.update_rows("interlude_intent", {"id": intent.id, "status": "pending"}, {"status": "cancelled", "updatedAt": now})
                    reference = payload.get("scriptEvent") or {}
                    await self._mark_segment_delivered(story.id, reference, "cancelled", "delivery-target-unavailable")
            elif intent.type == "delayed-message":
                participant = await self.get_participant(intent.participantId) if intent.participantId else None
                content = clip((intent.payload or {}).get("content"), int(self.runtime.get("maxMessageCharacters") or 1500))
                if participant and participant.status == "active" and content:
                    outcome = await self._transport_send(
                        self._delivery_umo(participant), content,
                        reply_to=str((intent.payload or {}).get("replyTo") or ""),
                    )
                    reference = (intent.payload or {}).get("scriptEvent") or {}
                    if reference:
                        await self._mark_segment_delivered(
                            story.id,
                            reference,
                            "delivered" if outcome == "delivered" else ("pending" if outcome == "unknown" else "failed"),
                            "delayed-due" if outcome != "unknown" else "transport-timeout: 回执超时，结果未知",
                        )
                    if outcome == "unknown":
                        # 结果未知：不自动重发（可能已送达），意图就地收口并留痕。
                        self.db.update_rows(
                            "interlude_intent", {"id": intent.id, "status": "pending"},
                            {
                                "status": "completed",
                                "payload": {**(intent.payload or {}), "deliveryUnknown": True},
                                "updatedAt": now,
                            },
                        )
                    else:
                        self.db.update_rows(
                            "interlude_intent", {"id": intent.id, "status": "pending"},
                            {"status": "completed" if outcome == "delivered" else "pending", "updatedAt": now},
                        )
                        if outcome == "failed":
                            self.db.update_rows("interlude_intent", {"id": intent.id}, {"notBefore": now + timedelta(seconds=30)})
                    handled += 1
                else:
                    self.db.update_rows("interlude_intent", {"id": intent.id, "status": "pending"}, {"status": "cancelled", "updatedAt": now})
                    reference = (intent.payload or {}).get("scriptEvent") or {}
                    if reference:
                        await self._mark_segment_delivered(story.id, reference, "cancelled", "delivery-target-unavailable")
            elif intent.type == "narrative-retry":
                key = f"{story.id}|{intent.participantId}"
                self.db.update_rows("interlude_intent", {"id": intent.id, "status": "pending"}, {"status": "completed", "updatedAt": now})
                participant = await self.get_participant(intent.participantId) if intent.participantId else None
                if participant and participant.status == "active":
                    turn_key = participant.id
                    pending = self.turn_engine.get_turn(turn_key)
                    if pending and pending.messages:
                        await self.flush_buffered_narrative(turn_key, pending.nextRevision)
                    else:
                        # 没有待处理消息：仅尝试续写生活
                        handled += 0
                handled += 1
            elif intent.type == "proactive-check":
                handled += 0  # 交由主动联系回合重查（下一轮 advance 的 dueIntents 携带）
            elif intent.type == "follow-up-commitment":
                handled += 0  # 交由 intent-due 回合处理
        return handled

    def _due_any(self, story_id: str, now: datetime) -> bool:
        return self.db.count_rows("interlude_intent", {
            "storyId": story_id, "status": "pending", "notBefore": {"$lte": to_iso(now)}
        }) > 0

    # ------------------------------------------------------------------ #
    # 后台扫描（sweep）
    # ------------------------------------------------------------------ #

    async def sweep(self) -> None:
        """后台扫描：发现到期任务（对应 sweep，不做重复工作）。

        每轮最多处理 `runtime.maxStoriesPerSweep` 个活跃故事（默认 20），
        按最近更新排序；单个故事失败不影响其余故事。
        """
        if self._paused:
            return
        limit = max(1, int(_pick(self.runtime.get("maxStoriesPerSweep"), 20)))
        rows = self.db.list_rows(
            "interlude_story", {"status": "active"}, order_by="updatedAt DESC", limit=limit
        )
        if not rows:
            return
        for row in rows:
            story = self._row_to_story(row)
            if not self.can_handle_story(story):
                continue
            now = now_utc()
            try:
                await self._process_due_intents(story, now)
                if self.has_pending_narrative(story.id):
                    # 前台回合中：仅独立检查到期分段投递
                    continue
                if self._advance_due(story, now):
                    messages = await self.advance_story(story, False)
                    if messages:
                        self._log("debug", f"后台推进完成 故事={story.id} 可见消息={len(messages)}")
                else:
                    self._schedule_next_automatic_advance(story.id, now)
            except Exception as error:  # noqa: BLE001 - 背景任务容错
                self._log("warn", f"后台扫描失败 故事={story.id}：{error}")

    async def background_loop(self) -> None:
        """常驻后台循环：扫描 / 整理 / 天气 / 播种（由插件层启动）。"""
        while True:
            try:
                self._schedule_due_intent_wake_of_canonical()
                await self.sweep()
                await self._maybe_background_compaction()
                await self.weather_sweep()
                await self.world_seeder_sweep()
                await self.backfill_fact_embeddings()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self._log("warn", f"后台循环异常：{error}")
            interval = max(30, int(self.runtime.get("sweepIntervalMinutes") or 5) * 60)
            await asyncio.sleep(interval)

    def _schedule_due_intent_wake_of_canonical(self) -> None:
        row = self.db.first_row("interlude_story", {"status": {"$ne": "archived"}}, order_by="updatedAt DESC")
        if row:
            self._schedule_due_intent_wake(row["id"])
            self._schedule_next_automatic_advance(row["id"], now_utc())

    # ------------------------------------------------------------------ #
    # 记忆压缩（compactStory / 后台整理）
    # ------------------------------------------------------------------ #

    async def schedule_compaction(self, story_id: str) -> None:
        """写作结束后检查是否需要整理（对应 scheduleCompaction，去重门）。"""
        if not self.memory_config.get("enabled", True):
            return
        story_row = self.db.first_row("interlude_story", {"id": story_id})
        if not story_row:
            return
        story = self._row_to_story(story_row)
        scene = await self.active_scene(story_id)
        if scene is None:
            return
        pending_count, pending_chars = await self._uncompacted_volume(story_id, scene)
        entry_threshold = int(self.memory_config.get("sceneEntryThreshold") or 16)
        char_threshold = int(self.memory_config.get("sceneCharacterThreshold") or 10000)
        if pending_count < entry_threshold and pending_chars < char_threshold:
            return
        if self.scheduler.has_exclusive(f"compaction:{story_id}"):
            return
        fingerprint = f"{scene.id}:{pending_count}:{pending_chars}"
        cooldown_key = f"compaction-backoff:{story_id}"
        if self.scheduler.is_backed_off(cooldown_key, fingerprint):
            return

        async def task() -> None:
            fresh = await self.get_story(story_id)
            if fresh is None:
                return
            ok = await self.compact_story(fresh, force=False)
            if not ok:
                self.scheduler.note_backoff(cooldown_key, fingerprint, time.time() + 10 * 60)

        self.scheduler.schedule_exclusive(f"compaction:{story_id}", task)

    async def _uncompacted_volume(self, story_id: str, scene: Scene) -> Tuple[int, int]:
        last_entry_id = scene.lastEntryId
        where: Dict[str, Any] = {"storyId": story_id}
        if last_entry_id:
            where["id"] = {"$gt": int(last_entry_id)}
        rows = self.db.list_rows("interlude_script_entry", where, order_by="id ASC", limit=500)
        count = len(rows)
        chars = sum(len(row.get("content") or "") for row in rows)
        return count, chars

    async def compact_story(self, story: Story, force: bool = True) -> bool:
        """执行一次完整整理：场景摘要、长期事实、状态提案、日程审查。

        对应 compactStory + compaction 链路。压缩失败不回滚主剧本。
        """
        if not self.memory_config.get("enabled", True) and not force:
            return False
        scene = await self.active_scene(story.id)
        if scene is None:
            return False
        last_entry_id = scene.lastEntryId
        where: Dict[str, Any] = {"storyId": story.id}
        if last_entry_id and not force:
            where["id"] = {"$gt": int(last_entry_id)}
        limit = int(self.memory_config.get("compactionEntryLimit") or 80)
        rows = self.db.list_rows("interlude_script_entry", where, order_by="id ASC", limit=limit)
        entries = [self._row_to_entry(row) for row in rows]
        if not entries:
            if force:
                await self._maybe_review_schedule(story, scene, [])
            return False
        # 字符预算裁剪（从尾部保留）
        budget = int(self.memory_config.get("compactionCharacterLimit") or 32000)
        selected: List[ScriptEntry] = []
        used = 0
        for entry in reversed(entries):
            size = len(entry.content) + 60
            if used + size > budget and selected:
                break
            selected.append(entry)
            used += size
        selected.reverse()
        scene_view = {
            "id": scene.id,
            "hook": clip(scene.hook, int(self.memory_config.get("sceneHookCharacters") or 2000)),
            "summary": clip(scene.summary, int(self.memory_config.get("sceneSummaryCharacters") or 8000)),
        }
        arc = await self.active_arc(story.id)
        participants = await self.participants(story.id)
        facts = [self._row_to_fact(row) for row in self.db.list_rows("interlude_fact", {"storyId": story.id, "status": "active"}, limit=50)]
        request = {
            "storyIdentity": {"setting": story.setting},
            "interval": {
                "from": to_iso(selected[0].occurredAt),
                "to": to_iso(selected[-1].occurredAt),
            },
            "scene": scene_view,
            "arc": {"title": arc.title, "summary": clip(arc.summary, 12000)} if arc else None,
            "entries": [
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "actor": entry.actor,
                    "participantId": entry.participantId or None,
                    "occurredAtLocal": format_story_display_time(entry.occurredAt, story.setting.get("timezone") or "UTC"),
                    "content": entry.content,
                }
                for entry in selected
            ],
            "participants": [
                {"id": item.id, "displayName": item.displayName, "relationship": item.relationship}
                for item in participants
            ],
            "facts": [
                {"id": fact.id, "scope": fact.scope, "content": clip(fact.content, 300), "unresolved": fact.unresolved}
                for fact in facts
            ],
            "schedulePreplan": self._schedule_review_request(story, selected),
            "workingDetails": self._active_working_details(story, now_utc()),
        }
        decision = await self.narrator.compact(request, self.config)
        if not decision:
            self._log("info", "压缩模型未返回有效结果；保留原剧本，稍后再试")
            return False
        now = now_utc()
        await self.serial(story.id, lambda: self._apply_compaction(story, scene, selected, decision, now))
        await self._maybe_review_schedule(story, scene, selected)
        self._log("info", f"记忆整理完成 条目={len(selected)} 事实={len(decision.get('facts') or [])}")
        return True

    async def _apply_compaction(
        self,
        story: Story,
        scene: Scene,
        entries: List[ScriptEntry],
        decision: Dict[str, Any],
        now: datetime,
    ) -> None:
        """应用一次压缩结果（对应 CompactionDecision 的分发）。"""
        entry_ids = {entry.id for entry in entries}
        scene_data = decision.get("scene") if isinstance(decision.get("scene"), dict) else None
        if scene_data:
            hook = clip(scene_data.get("hook"), int(self.memory_config.get("sceneHookCharacters") or 2000)) if isinstance(scene_data.get("hook"), str) else scene.hook
            summary = clip(scene_data.get("summary"), int(self.memory_config.get("sceneSummaryCharacters") or 8000)) if isinstance(scene_data.get("summary"), str) else scene.summary
            changes: Dict[str, Any] = {
                "hook": hook, "summary": summary, "lastEntryId": entries[-1].id, "updatedAt": now,
            }
            close = scene_data.get("close") is True
            if close:
                changes["status"] = "closed"
                changes["endedAt"] = now
            self.db.update_rows("interlude_scene", {"id": scene.id}, changes)
            if close:
                new_scene_id = self.db.insert_row("interlude_scene", {
                    "storyId": story.id, "status": "active", "startedAt": now, "endedAt": None,
                    "hook": "", "summary": "", "entryCount": 0, "lastEntryId": None,
                    "createdAt": now, "updatedAt": now,
                })
                await self._update_story_state(story.id, lambda state: {**state, "activeSceneId": new_scene_id}, now)
                self._log("info", f"场景 #${scene.id} 已关闭，创建新场景 #{new_scene_id}")
            presence = scene_data.get("presence")
            normalized_presence = normalize_scene_presence_drafts(presence, entries, now)
            if normalized_presence:
                await self._update_story_state(story.id, lambda state: {**state, "scenePresence": normalized_presence}, now)
        else:
            self.db.update_rows("interlude_scene", {"id": scene.id}, {"lastEntryId": entries[-1].id, "updatedAt": now})
        arc_data = decision.get("arc") if isinstance(decision.get("arc"), dict) else None
        if arc_data:
            arc = await self.active_arc(story.id)
            if arc:
                changes = {}
                if isinstance(arc_data.get("title"), str) and arc_data["title"].strip():
                    changes["title"] = clip(arc_data["title"], 255)
                if isinstance(arc_data.get("summary"), str) and arc_data["summary"].strip():
                    changes["summary"] = clip(arc_data["summary"], int(self.memory_config.get("arcSummaryCharacters") or 12000))
                if changes:
                    changes["updatedAt"] = now
                    self.db.update_rows("interlude_arc", {"id": arc.id}, changes)
        # 长期事实
        for draft in decision.get("facts") or []:
            if not isinstance(draft, dict):
                continue
            scope = draft.get("scope")
            content = draft.get("content")
            if scope not in ("character", "world", "relationship", "event", "promise"):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            source_ids = [item for item in (draft.get("sourceEntryIds") or []) if item in entry_ids]
            if not source_ids and entry_ids:
                continue
            fact_id = await self._insert_fact(
                story, scope, clip(content, int(self.memory_config.get("factContentCharacters") or 4000)),
                importance=float(draft.get("importance") or 0.5),
                confidence=float(draft.get("confidence") or 0.8),
                unresolved=bool(draft.get("unresolved")),
                source_entry_ids=source_ids,
            )
            for resolve_id in draft.get("resolvesFactIds") or []:
                self.db.update_rows("interlude_fact", {"id": resolve_id, "storyId": story.id, "status": "active"}, {
                    "status": "superseded", "updatedAt": now,
                })
            if fact_id:
                self._log("debug", f"长期事实入库 #{fact_id} scope={scope}")
        # 状态演化提案（阈值评估在 _maintain_overlays 完成）
        for draft in decision.get("statePatches") or []:
            if not isinstance(draft, dict):
                continue
            target = draft.get("target")
            if target not in ("character", "perspective", "world", "relationship"):
                continue
            proposed = draft.get("proposedValue")
            if not isinstance(proposed, str) or not proposed.strip():
                continue
            source_ids = [item for item in (draft.get("sourceEntryIds") or []) if item in entry_ids]
            if not source_ids and entry_ids:
                continue
            self.db.insert_row("interlude_state_patch", {
                "storyId": story.id,
                "participantId": draft.get("participantId") or "",
                "target": target,
                "path": clip(draft.get("path"), 255),
                "proposedValue": clip(proposed, 4000),
                "evidence": clip(draft.get("evidence"), 4000),
                "confidence": float(draft.get("confidence") or 0.6),
                "impact": "major" if draft.get("impact") == "major" else "minor",
                "status": "proposed",
                "sourceEntryIds": source_ids,
                "createdAt": now,
                "appliedAt": None,
            })
        # workingDetails（在途小细节）
        details: Optional[List[Dict[str, Any]]] = None
        for draft in decision.get("workingDetails") or []:
            if not isinstance(draft, dict):
                continue
            label = draft.get("label")
            value = draft.get("value")
            if not isinstance(label, str) or not isinstance(value, str) or not label.strip():
                continue
            if details is None:
                details = list(story.state.get("workingDetails") or [])
            replaces = draft.get("replacesLabel")
            if isinstance(replaces, str) and replaces.strip():
                details = [item for item in details if item.get("label") != replaces.strip()]
            details = [item for item in details if item.get("label") != label.strip()]
            if draft.get("resolved") is True:
                continue
            details.append({
                "label": label.strip()[:200],
                "value": value.strip()[:500],
                "expiresAt": draft.get("expiresAt"),
                "createdAt": to_iso(now),
                "sourceEntryIds": [item for item in (draft.get("sourceEntryIds") or []) if item in entry_ids],
            })
        if details is not None:
            await self._update_story_state(story.id, lambda state: {**state, "workingDetails": details[-20:]}, now)
        # 生活状态（群聊意愿 auto 档输入）
        life_status = decision.get("lifeStatus")
        if life_status in ("busy", "asleep", "idle"):
            await self._update_story_state(story.id, lambda state: {
                **state, "lifeStatus": {"status": life_status, "updatedAt": to_iso(now)},
            }, now)
        rhythm = decision.get("lifeRhythm")
        if isinstance(rhythm, dict) and rhythm.get("attention") in (
            "phone-focused", "available", "occupied", "resting", "asleep"
        ):
            await self._update_story_state(story.id, lambda state: {
                **state,
                "lifeRhythm": {
                    "attention": rhythm["attention"],
                    "basis": clip(rhythm.get("basis"), 300),
                    "assessedAt": to_iso(now),
                    "source": "compactor",
                },
            }, now)
        # 状态提案的自动应用评估
        await self._maintain_overlays(story, now)

    async def _insert_fact(
        self,
        story: Story,
        scope: str,
        content: str,
        importance: float = 0.5,
        confidence: float = 0.8,
        unresolved: bool = False,
        source_entry_ids: Optional[List[int]] = None,
        knowledge: Optional[Dict[str, Any]] = None,
    ) -> int:
        """写入一条长期事实并执行容量上限（对应 maxFactsPerStory）。"""
        now = now_utc()
        fact_id = self.db.insert_row("interlude_fact", {
            "storyId": story.id,
            "participantId": "",
            "scope": scope,
            "content": content,
            "importance": max(0.0, min(1.0, importance)),
            "confidence": max(0.0, min(1.0, confidence)),
            "unresolved": unresolved,
            "embedding": None,
            "knowledge": knowledge,
            "status": "active",
            "sourceEntryIds": source_entry_ids or [],
            "lastSeenAt": now,
            "createdAt": now,
            "updatedAt": now,
        })
        max_facts = int(self.memory_config.get("maxFactsPerStory") or 200)
        count = self.db.count_rows("interlude_fact", {"storyId": story.id, "status": "active"})
        if count > max_facts:
            rows = self.db.list_rows(
                "interlude_fact", {"storyId": story.id, "status": "active"},
                order_by="importance ASC, lastSeenAt ASC", limit=count - max_facts,
            )
            for row in rows:
                self.db.update_rows("interlude_fact", {"id": row["id"]}, {"status": "superseded", "updatedAt": now})
        return fact_id

    # ---- Agent 语义检索（Embedding 基础版） ----

    async def embed_text(self, text: str) -> Optional[List[float]]:
        """调用 Embedding 连接（不可用时返回 None，调用方降级规则排序）。"""
        providers = self.narrator.providers_for("useForEmbedding")
        if not providers or not text.strip():
            return None
        provider = providers[0]
        max_chars = int((self.config.get("model") or {}).get("embedding", {}).get("maxInputCharacters") or 4000)
        try:
            return await self.narrator.client.embed(provider, text.strip()[:max_chars])
        except ModelCallError:
            return None

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        import math

        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        norm_a = math.sqrt(sum(a * a for a in left))
        norm_b = math.sqrt(sum(b * b for b in right))
        return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

    async def backfill_fact_embeddings(self) -> None:
        """后台补齐长期事实的向量（对应 embedding.backfillBatchSize）。

        只在 `model.embedding.enabled` 且存在 `useForEmbedding` 连接时运行；
        每轮最多补 `backfillBatchSize` 条，失败静默跳过（不影响叙事）。
        """
        config = (self.config.get("model") or {}).get("embedding") or {}
        if not config.get("enabled"):
            return
        if not self.narrator.providers_for("useForEmbedding"):
            return
        story = await self.get_canonical_story()
        if story is None:
            return
        batch = max(1, int(_pick(config.get("backfillBatchSize"), 5)))
        rows = self.db.list_rows(
            "interlude_fact", {"storyId": story.id, "status": "active"},
            order_by="lastSeenAt DESC", limit=max(60, batch * 4),
        )
        missing = [row for row in rows if not row.get("embedding")][:batch]
        for row in missing:
            vector = await self.embed_text(row.get("content") or "")
            if vector:
                self.db.update_rows("interlude_fact", {"id": row["id"]}, {"embedding": vector})

    # ------------------------------------------------------------------ #
    # 场景记忆查询（命令用）
    # ------------------------------------------------------------------ #

    async def memories(self, story_id: str, limit: int = 10, participant_id: str = "") -> List[Memory]:
        rows = self.db.list_rows(
            "interlude_memory",
            {"storyId": story_id, "status": "active"},
            order_by="createdAt DESC",
            limit=max(limit * 3, 30),
        )
        memories = [self._row_to_memory(row) for row in rows]
        if participant_id:
            filtered = [memory for memory in memories if not memory.participantId or memory.participantId == participant_id]
            memories = filtered or memories
        memories.sort(key=lambda memory: (memory.importance, memory.createdAt.timestamp()), reverse=True)
        return memories[: max(1, min(limit, 30))]

    async def facts(self, story_id: str, limit: int = 8) -> List[Fact]:
        """命令展示用的长期事实（按重要度排序）。"""
        rows = self.db.list_rows("interlude_fact", {"storyId": story_id, "status": "active"}, order_by="importance DESC, id DESC", limit=limit)
        return [self._row_to_fact(row) for row in rows]

    async def admin_facts(self, story_id: str, limit: int = 20) -> List[Fact]:
        rows = self.db.list_rows("interlude_fact", {"storyId": story_id, "status": "active"}, order_by="id ASC", limit=max(1, min(limit, 100)))
        return [self._row_to_fact(row) for row in rows]

    async def admin_pending_intents(self, story_id: str, limit: int = 20) -> List[Intent]:
        rows = self.db.list_rows("interlude_intent", {"storyId": story_id, "status": "pending"}, order_by="notBefore ASC", limit=max(1, min(limit, 100)))
        return [self._row_to_intent(row) for row in rows]

    async def add_admin_fact(self, story: Story, scope: str, content: str) -> bool:
        """人工添加高置信度长期事实（对应 addAdminFact，含指代不清检查）。"""
        text = (content or "").strip()
        if not text:
            return False
        if scope == "relationship" and not _mentions_person(text):
            return False
        if _vague_pronouns_only(text):
            return False
        await self._insert_fact(story, scope, clip(text, int(self.memory_config.get("factContentCharacters") or 4000)), importance=0.8, confidence=1.0)
        return True

    async def forget_admin_fact(self, story_id: str, fact_id: int) -> bool:
        changed = self.db.update_rows(
            "interlude_fact", {"id": fact_id, "storyId": story_id, "status": "active"},
            {"status": "superseded", "updatedAt": now_utc()},
        )
        return changed > 0

    async def cancel_admin_intent(self, story_id: str, intent_id: int) -> bool:
        row = self.db.first_row("interlude_intent", {"id": intent_id, "storyId": story_id, "status": "pending"})
        if row is None:
            return False
        intent = self._row_to_intent(row)
        self.db.update_rows("interlude_intent", {"id": intent_id}, {"status": "cancelled", "updatedAt": now_utc()})
        reference = (intent.payload or {}).get("scriptEvent")
        if isinstance(reference, dict):
            await self._mark_segment_delivered(story_id, reference, "cancelled", "admin-cancel")
        return True

    async def add_admin_script_note(self, story: Story, content: str) -> bool:
        """人工剧本注记（对应 addAdminScriptNote，来源标记 admin-note）。"""
        text = (content or "").strip()
        if not text:
            return False
        now = now_utc()
        await self.serial(story.id, lambda: self.append_entry(story.id, {
            "kind": "admin-note",
            "actor": "admin",
            "content": f"[管理员注记] {clip(text, 4000)}",
            "occurredAt": to_iso(now),
            "metadata": {"source": "admin-note"},
        }, now))
        return True

    # ------------------------------------------------------------------ #
    # Overlay 演化与状态提案（memory 12.4）
    # ------------------------------------------------------------------ #

    async def admin_state_patches(self, story_id: str, limit: int = 20) -> List[StatePatch]:
        rows = self.db.list_rows(
            "interlude_state_patch", {"storyId": story_id}, order_by="id DESC", limit=max(1, min(limit, 100))
        )
        return [self._row_to_patch(row) for row in rows]

    async def apply_admin_state_patch(self, story: Story, patch_id: int) -> bool:
        """手动批准提案：绕过阈值直接写入 overlay（对应 applyAdminStatePatch）。"""
        row = self.db.first_row("interlude_state_patch", {"id": patch_id, "storyId": story.id})
        if row is None:
            return False
        patch = self._row_to_patch(row)
        if patch.status not in ("proposed",):
            return False
        now = now_utc()
        await self.serial(story.id, lambda: self._apply_patch_to_overlay(story, patch, now, "admin"))
        self.db.update_rows("interlude_state_patch", {"id": patch_id}, {"status": "applied", "appliedAt": now})
        return True

    async def reject_admin_state_patch(self, story_id: str, patch_id: int) -> bool:
        changed = self.db.update_rows(
            "interlude_state_patch", {"id": patch_id, "storyId": story_id, "status": "proposed"},
            {"status": "rejected"},
        )
        return changed > 0

    async def _apply_patch_to_overlay(self, story: Story, patch: StatePatch, now: datetime, source: str) -> None:
        """把一条提案写入 overlay（characterTraits 追加、其余字段覆盖累积）。

        `development.*` 路径不进 overlay（对应上游对 development 类提案的限制）。
        """
        if patch.path.startswith("development."):
            self._log("debug", f"开发类提案 #{patch.id} 不进 overlay（{patch.path}）")
            return

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            overlay = dict(state.get("settingOverlay") or {})
            if patch.target == "character":
                if patch.path.startswith("characterTraits"):
                    traits = list(overlay.get("characterTraits") or [])
                    if patch.proposedValue not in traits:
                        traits.append(patch.proposedValue)
                    overlay["characterTraits"] = traits[-20:]
                else:
                    overlay["characterProfile"] = _append_overlay_text(overlay.get("characterProfile"), patch.proposedValue)
            elif patch.target == "perspective":
                overlay["perspective"] = _append_overlay_text(overlay.get("perspective"), patch.proposedValue)
            elif patch.target == "world":
                overlay["world"] = _append_overlay_text(overlay.get("world"), patch.proposedValue)
            elif patch.target == "relationship":
                if patch.participantId:
                    # 参与者级关系 overlay 单独写入参与者记录
                    pass
                else:
                    overlay["relationship"] = _append_overlay_text(overlay.get("relationship"), patch.proposedValue)
            return {**state, "settingOverlay": overlay}

        await self._update_story_state(story.id, mutate, now)
        if patch.target == "relationship" and patch.participantId:
            participant = await self.get_participant(patch.participantId)
            if participant:
                state = dict(participant.state or {})
                state["relationshipOverlay"] = _append_overlay_text(state.get("relationshipOverlay"), patch.proposedValue)
                self.db.update_rows("interlude_participant", {"id": participant.id}, {"state": state, "updatedAt": now})
        self._log("info", f"设定演化已写入 overlay（{patch.target}/{patch.path}，来源={source}）")

    async def _maintain_overlays(self, story: Story, now: datetime) -> None:
        """提案的自动应用评估：置信度 / 回合数 / 跨日 / 冷却（对应 12.4 规则）。"""
        memory = self.memory_config
        auto_apply = memory.get("autoApplyStatePatches", True)
        allow_major = memory.get("allowMajorStateChanges", True)
        confidence_threshold = float(memory.get("statePatchConfidenceThreshold") or 0.82)
        major_threshold = float(memory.get("majorStatePatchConfidenceThreshold") or 0.95)
        min_turns = int(memory.get("statePatchMinTurns") or 3)
        major_min_scenes = int(memory.get("statePatchMajorMinScenes") or 2)
        min_days = int(memory.get("statePatchMinDays") or 2)
        cooldown_hours = int(memory.get("statePatchCooldownHours") or 72)
        fresh = await self.get_story(story.id)
        if fresh is None:
            return
        proposed = [
            self._row_to_patch(row)
            for row in self.db.list_rows("interlude_state_patch", {"storyId": story.id, "status": "proposed"}, order_by="id ASC", limit=50)
        ]
        for patch in proposed:
            if patch.confidence < (major_threshold if patch.impact == "major" else confidence_threshold):
                continue
            entry_ids = [item for item in patch.sourceEntryIds if item]
            if not entry_ids:
                continue
            rows = self.db.list_rows("interlude_script_entry", {"id": {"$in": entry_ids}, "storyId": story.id})
            evidence = state_patch_evidence(rows, story.setting.get("timezone") or "UTC")
            if patch.impact == "major":
                # 重大变化仍要求独立场景证据（对应 rc57 保留的 major 约束）。
                if evidence["scenes"] < major_min_scenes:
                    self._log(
                        "debug",
                        f"Overlay 候选继续累计 目标={patch.target}/{patch.path} 影响=major "
                        f"场景={evidence['scenes']}/{major_min_scenes} 回合={evidence['turns']} "
                        f"日期={evidence['days']} 置信度={patch.confidence:.2f}/{major_threshold:.2f}",
                    )
                    continue
            else:
                # rc57 Issue #8：普通提案按独立叙事回合、而不是参与者/场景数判断，
                # 否则单场景长故事会永远停在 1 个"回合"。
                if evidence["turns"] < min_turns or evidence["days"] < min_days:
                    self._log(
                        "debug",
                        f"Overlay 候选继续累计 目标={patch.target}/{patch.path} 影响=minor "
                        f"回合={evidence['turns']}/{min_turns} 场景={evidence['scenes']} "
                        f"日期={evidence['days']}/{min_days} 置信度={patch.confidence:.2f}/{confidence_threshold:.2f}",
                    )
                    continue
            # 同路径冷却
            applied_rows = self.db.list_rows(
                "interlude_state_patch",
                {"storyId": story.id, "target": patch.target, "path": patch.path, "status": {"$in": ["applied", "compacted"]}},
                order_by="appliedAt DESC", limit=1,
            )
            if applied_rows:
                applied_at = ensure_utc(applied_rows[0].get("appliedAt"))
                if applied_at and (now - applied_at) < timedelta(hours=cooldown_hours):
                    continue
            if not auto_apply:
                continue
            if patch.impact == "major" and not allow_major:
                continue
            await self._apply_patch_to_overlay(story, patch, now, "auto")
            self.db.update_rows("interlude_state_patch", {"id": patch.id}, {"status": "applied", "appliedAt": now})

    async def admin_overlay_status(self, story_id: str) -> Dict[str, Any]:
        """overlay 状态统计（对应 adminOverlayStatus）。"""
        story = await self.get_story(story_id)
        state = story.state if story else {}
        rows = self.db.list_rows("interlude_state_patch", {"storyId": story_id})
        proposed = [self._row_to_patch(row) for row in rows if row.get("status") == "proposed"]
        applied = [row for row in rows if row.get("status") in ("applied", "compacted")]
        cleared = [row for row in rows if row.get("status") == "cleared"]
        snapshots = self.db.list_rows("interlude_overlay_snapshot", {"storyId": story_id, "status": "active"})
        participant_overlays = [
            self._row_to_participant(row) for row in self.db.list_rows("interlude_participant", {"storyId": story_id})
            if (normalize_participant_state(row.get("state")).get("relationshipOverlay") or "").strip()
        ]
        return {
            "state": state.get("settingOverlay") or {},
            "proposed": proposed,
            "applied": applied,
            "cleared": cleared,
            "snapshots": snapshots,
            "participantOverlays": participant_overlays,
        }

    async def clear_setting_overlay(self, story: Story, target: str) -> Dict[str, Any]:
        """清理指定 overlay 部分（对应 clearSettingOverlay，含提案与快照清理）。"""
        now = now_utc()
        participant_count = 0

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            overlay = dict(state.get("settingOverlay") or {})
            if target in ("character", "all"):
                overlay.pop("characterProfile", None)
                overlay.pop("characterTraits", None)
            if target in ("perspective", "all"):
                overlay.pop("perspective", None)
            if target in ("world", "all"):
                overlay.pop("world", None)
            if target in ("relationship", "all"):
                overlay.pop("relationship", None)
            return {**state, "settingOverlay": overlay}

        await self._update_story_state(story.id, mutate, now)
        targets = ["character", "perspective", "relationship", "world"] if target == "all" else [target]
        for item in targets:
            self.db.update_rows(
                "interlude_state_patch",
                {"storyId": story.id, "target": item, "status": "proposed"},
                {"status": "cleared"},
            )
            self.db.update_rows(
                "interlude_overlay_snapshot",
                {"storyId": story.id, "target": item, "status": "active"},
                {"status": "superseded", "updatedAt": now},
            )
        if target in ("relationship", "all"):
            for row in self.db.list_rows("interlude_participant", {"storyId": story.id}):
                participant = self._row_to_participant(row)
                state = dict(participant.state or {})
                if (state.get("relationshipOverlay") or "").strip():
                    state["relationshipOverlay"] = ""
                    self.db.update_rows("interlude_participant", {"id": participant.id}, {"state": state, "updatedAt": now})
                    participant_count += 1
        return {"participantCount": participant_count}

    async def compact_overlay(self, story: Story) -> bool:
        """只压缩已应用的 overlay（对应 compactOverlay；移植版做快照归档）。"""
        now = now_utc()
        applied = [
            self._row_to_patch(row)
            for row in self.db.list_rows("interlude_state_patch", {"storyId": story.id, "status": "applied"}, order_by="appliedAt ASC", limit=200)
        ]
        if not applied:
            return False
        by_target: Dict[str, List[StatePatch]] = {}
        for patch in applied:
            by_target.setdefault(patch.target, []).append(patch)
        for target, patches in by_target.items():
            summary = "；".join(clip(patch.proposedValue, 200) for patch in patches[-8:])
            period_start = min(patch.appliedAt or patch.createdAt for patch in patches)
            period_end = max(patch.appliedAt or patch.createdAt for patch in patches)
            snapshot_id = self.db.insert_row("interlude_overlay_snapshot", {
                "storyId": story.id,
                "participantId": "",
                "target": target,
                "tier": "weekly",
                "periodStart": period_start,
                "periodEnd": period_end,
                "summary": clip(summary, int(self.memory_config.get("overlayWeeklySummaryCharacters") or 1600)),
                "majorEvents": [],
                "sourcePatchIds": [patch.id for patch in patches],
                "status": "active",
                "createdAt": now,
                "updatedAt": now,
            })
            self.db.update_rows("interlude_state_patch", {"id": {"$in": [patch.id for patch in patches]}}, {"status": "compacted"})
            self._log("info", f"overlay 快照 #{snapshot_id} 已归档（{target}，{len(patches)} 条提案）")
        return True

    async def _overlay_snapshots(self, story: Story) -> List[OverlaySnapshot]:
        rows = self.db.list_rows("interlude_overlay_snapshot", {"storyId": story.id, "status": "active"}, order_by="periodEnd DESC", limit=8)
        return [self._row_to_snapshot(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Schedule Preplan 管理
    # ------------------------------------------------------------------ #

    def admin_schedule_preplan_sync(self, story_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.first_row("interlude_schedule_preplan", {"storyId": story_id})
        if not row:
            return None
        return {
            "storyId": row.get("storyId"),
            "revision": int(row.get("revision") or 0),
            "timezone": row.get("timezone") or "UTC",
            "validFrom": row.get("validFrom") or "",
            "validThrough": row.get("validThrough") or "",
            "lastReviewedLocalDate": row.get("lastReviewedLocalDate") or "",
            "lastEvidenceEntryId": int(row.get("lastEvidenceEntryId") or 0),
            "reviewReason": row.get("reviewReason") or "",
            "regimes": json_loads(row.get("regimes"), []) or [],
            "exceptions": json_loads(row.get("exceptions"), []) or [],
            "materializedDays": json_loads(row.get("materializedDays"), []) or [],
        }

    async def admin_schedule_preplan(self, story_id: str) -> Optional[Dict[str, Any]]:
        return self.admin_schedule_preplan_sync(story_id)

    async def request_schedule_preplan_rebuild(self, story_id: str) -> bool:
        """标记日程重新审查（对应 requestSchedulePreplanRebuild）。"""
        row = self.db.first_row("interlude_schedule_preplan", {"storyId": story_id})
        if not row:
            return False
        self.db.update_rows("interlude_schedule_preplan", {"storyId": story_id}, {
            "lastReviewedLocalDate": "", "reviewReason": "manual-refresh", "updatedAt": now_utc(),
        })
        return True

    def _schedule_review_request(self, story: Story, entries: Sequence[ScriptEntry]) -> Optional[Dict[str, Any]]:
        config = preplan_mod.resolve_schedule_preplan_config(self.config.get("schedulePreplan"))
        if not config.get("enabled"):
            return None
        now = now_utc()
        timezone_name = story.setting.get("timezone") or "UTC"
        current = self.admin_schedule_preplan_sync(story.id)
        return {
            "localDate": calendar_day_key(now, timezone_name),
            "horizonDays": config["horizonDays"],
            "variationLevel": config["variationLevel"],
            "current": current,
            "evidenceEntries": [
                {"id": entry.id, "content": clip(entry.content, 400), "occurredAt": to_iso(entry.occurredAt)}
                for entry in entries[-40:]
            ],
        }

    async def _maybe_review_schedule(self, story: Story, scene: Scene, entries: Sequence[ScriptEntry]) -> None:
        """每日日程审查：无新证据时程序刷新不调模型（对应 schedulePreplanReviewDue/NeedsModel）。"""
        config = preplan_mod.resolve_schedule_preplan_config(self.config.get("schedulePreplan"))
        if not config.get("enabled"):
            return
        now = now_utc()
        timezone_name = story.setting.get("timezone") or "UTC"
        today = calendar_day_key(now, timezone_name)
        current = self.admin_schedule_preplan_sync(story.id)
        if not preplan_mod.schedule_preplan_review_due(current, now, timezone_name, config):
            return
        fresh_story = await self.get_story(story.id)
        if fresh_story is None:
            return
        evidence = await self.recent_entries(story.id, 60)
        if not preplan_mod.schedule_preplan_needs_model(current, evidence, today, timezone_name, config):
            if current is None:
                return
            refreshed = preplan_mod.refresh_schedule_preplan(current, today, timezone_name, config, now)
            self._save_schedule_record(refreshed)
            return
        request = self._schedule_review_request(fresh_story, evidence) or {}
        proposal = await self.narrator.plan_schedule(request, self.config)
        if proposal is None:
            return
        updated = preplan_mod.apply_schedule_preplan_proposal(
            current, proposal, evidence, today, timezone_name, config, now, config["variationLevel"]
        )
        if updated:
            updated["storyId"] = story.id
            self._save_schedule_record(updated)
            self._log("info", f"Schedule Preplan 已更新 revision={updated.get('revision')} 原因={clip(updated.get('reviewReason'), 120)}")

    def _save_schedule_record(self, record: Dict[str, Any]) -> None:
        existing = self.db.first_row("interlude_schedule_preplan", {"storyId": record.get("storyId")})
        now = now_utc()
        values = {
            "storyId": record.get("storyId"),
            "revision": int(record.get("revision") or 0),
            "timezone": record.get("timezone") or "UTC",
            "validFrom": record.get("validFrom") or "",
            "validThrough": record.get("validThrough") or "",
            "lastReviewedLocalDate": record.get("lastReviewedLocalDate") or "",
            "lastEvidenceEntryId": int(record.get("lastEvidenceEntryId") or 0),
            "reviewReason": record.get("reviewReason") or "",
            "regimes": record.get("regimes") or [],
            "exceptions": record.get("exceptions") or [],
            "materializedDays": record.get("materializedDays") or [],
            "createdAt": record.get("createdAt") or now,
            "updatedAt": now,
        }
        if existing:
            self.db.update_rows("interlude_schedule_preplan", {"storyId": record.get("storyId")}, values)
        else:
            self.db.insert_row("interlude_schedule_preplan", values)

    # ------------------------------------------------------------------ #
    # 天气与世界事件播种（扩展 15 / 19）
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 网页观察（browser，自包含 httpx 实现）
    # ------------------------------------------------------------------ #

    def _browser_config(self) -> Dict[str, Any]:
        return self.config.get("browser") or {}

    async def _browser_domain_allowed(self, url: str, config: Dict[str, Any]) -> bool:
        """域名白/黑名单 + 私网地址拦截（避免 SSRF）。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower().strip(".")
        if not host:
            return False
        blocked = [str(item).lower().strip(".") for item in config.get("blockedDomains") or []]
        allowed = [str(item).lower().strip(".") for item in config.get("allowedDomains") or []]

        def matches(domain: str) -> bool:
            return bool(domain) and (host == domain or host.endswith("." + domain))

        if any(matches(domain) for domain in blocked):
            return False
        if allowed and not any(matches(domain) for domain in allowed):
            return False
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except Exception:  # noqa: BLE001 - 解析失败视为不可访问
            return False
        for info in infos:
            address = str(info[4][0]).split("%")[0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True

    def _browser_cache_hit(self, story_id: str, query: str, url: str, minutes: int) -> bool:
        if minutes <= 0:
            return False
        since = now_utc() - timedelta(minutes=minutes)
        rows = self.db.list_rows(
            "interlude_web_observation", {"storyId": story_id, "status": "ready"},
            order_by="accessedAt DESC", limit=50,
        )
        for row in rows:
            accessed = ensure_utc(row.get("accessedAt"))
            if accessed is not None and accessed < since:
                continue
            if url and row.get("url") == url:
                return True
            if query and row.get("query") == query:
                return True
        return False

    async def _browser_observe(
        self, mode: str, query: str, url: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """执行一次搜索或访问，返回 `{status,url,title,excerpt,summary}`。"""
        import httpx

        max_text = max(500, int(config.get("maxTextCharacters") or 12000))
        max_excerpt = max(100, int(config.get("maxExcerptCharacters") or 3000))
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; HDSInterlude/1.0; +https://gitee.com/MomoiCore/HDS-Interlude)",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        target = url
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
                if mode == "search":
                    template = str(config.get("searchUrlTemplate") or "https://html.duckduckgo.com/html/?q={query}")
                    if "{query}" not in template:
                        template = "https://html.duckduckgo.com/html/?q={query}"
                    search_url = template.replace("{query}", quote_plus(query))
                    if not await self._browser_domain_allowed(search_url, config):
                        return {"status": "blocked", "url": search_url, "title": "", "excerpt": "", "summary": "搜索地址被域名策略拒绝"}
                    response = await client.get(search_url)
                    if response.status_code >= 400:
                        return {"status": "failed", "url": search_url, "title": "", "excerpt": "", "summary": f"搜索 HTTP {response.status_code}"}
                    target = first_search_result(response.text, search_url)
                    if not target:
                        return {"status": "empty", "url": search_url, "title": "", "excerpt": "", "summary": "没有找到可用结果"}
                if not await self._browser_domain_allowed(target, config):
                    return {"status": "blocked", "url": target, "title": "", "excerpt": "", "summary": "目标地址被域名策略拒绝"}
                response = await client.get(target)
                if response.status_code >= 400:
                    return {"status": "failed", "url": target, "title": "", "excerpt": "", "summary": f"HTTP {response.status_code}"}
                page = parse_html_page(response.text)
        except Exception as error:  # noqa: BLE001 - 观察失败只记状态
            return {"status": "failed", "url": target, "title": "", "excerpt": "", "summary": f"抓取失败：{error}"}
        text = (page.get("text") or "")[:max_text]
        excerpt = text[:max_excerpt]
        return {
            "status": "ready" if excerpt else "empty",
            "url": target,
            "title": (page.get("title") or target)[:300],
            "excerpt": excerpt,
            "summary": excerpt[:500],
        }

    async def _execute_browser_intents(
        self, story: Story, decision: Dict[str, Any], participant_id: str, now: datetime
    ) -> None:
        """执行延迟网页观察（deferred-only；immediate 会被跳过并留日志）。"""
        config = self._browser_config()
        if not config.get("enabled"):
            return
        intents = decision.get("browserIntents") or []
        for intent in intents[:1]:
            if not isinstance(intent, dict):
                continue
            timing = str(intent.get("timing") or "deferred")
            if timing == "immediate" and str(config.get("mode") or "deferred-only") == "deferred-only":
                self._log("debug", "网页观察 immediate 请求在 deferred-only 模式下已跳过")
                continue
            mode = str(intent.get("mode") or "search")
            query = str(intent.get("query") or "").strip()
            url = str(intent.get("url") or "").strip()
            if mode == "search" and not config.get("allowSearch", True):
                continue
            if mode == "visit" and not config.get("allowVisit", True):
                continue
            if self._browser_cache_hit(
                story.id, query, url, int(config.get("cacheMinutes") or 30)
            ):
                self._log("debug", f"网页观察命中缓存：{query or url}")
                continue
            result = await self._browser_observe(mode, query, url, config)
            self.db.insert_row("interlude_web_observation", {
                "storyId": story.id,
                "participantId": participant_id,
                "intentId": 0,
                "mode": mode,
                "query": query,
                "url": result.get("url") or url,
                "title": clip(result.get("title") or "", 300),
                "excerpt": clip(result.get("excerpt") or "", 12000),
                "summary": clip(result.get("summary") or "", 1000),
                "status": result.get("status") or "failed",
                "accessedAt": now,
                "createdAt": now,
            })
            self._log(
                "info" if result.get("status") == "ready" else "debug",
                f"网页观察 {result.get('status')}：{result.get('title') or query or url}",
            )

    def _schedule_browser_intents(
        self, story: Story, decision: Dict[str, Any], participant_id: str, now: datetime
    ) -> None:
        if not decision.get("browserIntents") or not self._browser_config().get("enabled"):
            return

        async def run() -> None:
            try:
                await self._execute_browser_intents(story, decision, participant_id, now)
            except Exception as error:  # noqa: BLE001 - 观察失败不影响叙事
                self._log("debug", f"网页观察任务失败：{error}")

        asyncio.ensure_future(run())

    def _browser_observations(self, story_id: str) -> List[Dict[str, Any]]:
        """最近 24 小时内可用的网页观察（进入下一轮 webContext）。"""
        cutoff = now_utc() - timedelta(hours=24)
        rows = self.db.list_rows(
            "interlude_web_observation", {"storyId": story_id, "status": "ready"},
            order_by="accessedAt DESC", limit=5,
        )
        observations: List[Dict[str, Any]] = []
        for row in rows:
            accessed = ensure_utc(row.get("accessedAt"))
            if accessed is not None and accessed < cutoff:
                continue
            observations.append({
                "title": row.get("title") or "",
                "url": row.get("url") or "",
                "excerpt": clip(row.get("excerpt") or "", 3000),
                "accessedAt": to_iso(accessed) if accessed else "",
            })
        return observations

    async def weather_sweep(self) -> None:
        """运行时天气：Open-Meteo 拉取并以 [世界事件] 注入（滚动替换）。"""
        weather = self.config.get("weather") or {}
        if not weather.get("enabled"):
            return
        story = await self.get_canonical_story()
        if story is None or not self.can_handle_story(story):
            return
        now = now_utc()
        cadence = max(5, int(weather.get("cadenceMinutes") or 30)) * 60
        if time.time() - self._last_weather_at < cadence:
            return
        self._last_weather_at = time.time()
        if weather.get("skipFantasyWorld") and self._is_fantasy_world(story):
            self._log("debug", "架空世界已跳过天气注入（weather.skipFantasyWorld）")
            return
        latitude, longitude = await self._weather_coordinates(story, weather)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": latitude,
                        "longitude": longitude,
                        "current": "temperature_2m,weather_code",
                        "timezone": "UTC",
                    },
                )
            data = response.json()
        except Exception as error:  # noqa: BLE001
            self._log("debug", f"天气拉取失败：{error}")
            return
        current = data.get("current") if isinstance(data, dict) else None
        if not isinstance(current, dict):
            return
        temperature = current.get("temperature_2m")
        code = current.get("weather_code")
        text = f"环境：{_weather_text(code)}，气温约 {round(float(temperature)) if temperature is not None else '未知'}°C。"
        # 滚动替换：时间线上只保留最新一条天气事件
        existing = self.db.list_rows("interlude_script_entry", {"storyId": story.id, "kind": "world-event"}, order_by="id DESC", limit=20)
        weather_rows = [row for row in existing if "[世界事件] 天气：" in (row.get("content") or "")]
        if weather_rows and (weather_rows[0].get("content") or "").endswith(text):
            return
        for row in weather_rows:
            self.db.delete_rows("interlude_script_entry", {"id": row["id"]})
        await self.append_entry(story.id, {
            "kind": "world-event",
            "actor": "system",
            "content": f"[世界事件] 天气：{text}",
            "occurredAt": to_iso(now),
            "metadata": {"source": "weather"},
        }, now)

    #: 判定"架空世界"的关键词。只在 weather.skipFantasyWorld 开启时生效。
    _FANTASY_MARKERS = (
        "异世界", "魔法", "精灵", "龙族", "幻想", "架空", "修仙", "仙侠",
        "星际", "赛博", "末世", "奇幻", "深渊", "神域", "魔物",
    )

    def _is_fantasy_world(self, story: Story) -> bool:
        """按世界/地点/角色描述判定是否架空世界（保守：命中任一关键词即跳过）。"""
        setting = story.setting or {}
        text = " ".join(
            str(setting.get(key) or "")
            for key in ("world", "location", "character", "supportingCast", "relationship")
        )
        return any(marker in text for marker in self._FANTASY_MARKERS)

    async def _weather_coordinates(self, story: Story, weather: Dict[str, Any]) -> Tuple[float, float]:
        """解析观测点：`autoFromLocation` 开启时按故事地点做地理编码。

        结果缓存在 `story.state["weatherLocation"]`；编码失败回退到配置的
        经纬度，不阻断天气注入。
        """
        default = (
            float(weather.get("latitude") or 39.14),
            float(weather.get("longitude") or 117.18),
        )
        if not weather.get("autoFromLocation", True):
            return default
        cached = (story.state or {}).get("weatherLocation")
        if isinstance(cached, dict):
            try:
                return float(cached["latitude"]), float(cached["longitude"])
            except (KeyError, TypeError, ValueError):
                pass
        place = str(story.setting.get("location") or "").strip()
        if not place:
            return default
        try:
            import httpx

            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": place[:80], "count": 1, "language": "zh", "format": "json"},
                )
            results = (response.json() or {}).get("results") or []
            first = results[0] if results else None
            if not isinstance(first, dict):
                return default
            latitude = float(first["latitude"])
            longitude = float(first["longitude"])
        except Exception as error:  # noqa: BLE001 - 地理编码失败按配置回退
            self._log("debug", f"天气观测点解析失败（{place}）：{error}")
            return default
        now = now_utc()
        record = {
            "name": str(first.get("name") or place),
            "latitude": latitude,
            "longitude": longitude,
            "resolvedAt": to_iso(now),
        }
        await self._update_story_state(
            story.id, lambda state: {**state, "weatherLocation": record}, now
        )
        return latitude, longitude

    async def world_seeder_sweep(self) -> None:
        """世界事件播种器：低频检查，多数输出空（对应 worldSeeder）。"""
        seeder = self.config.get("worldSeeder") or {}
        if not seeder.get("enabled"):
            return
        if not self.narrator.providers_for("useForWorldSeeding"):
            return
        story = await self.get_canonical_story()
        if story is None or not self.can_handle_story(story):
            return
        now = now_utc()
        cadence = max(5, int(seeder.get("cadenceMinutes") or 30)) * 60
        if time.time() - self._last_seeder_at < cadence:
            return
        self._last_seeder_at = time.time()
        # 每轮约 10% 概率整轮跳过
        if random.random() < 0.1:
            return
        pending = self.db.count_rows("interlude_seeded_event", {"storyId": story.id, "status": "scheduled"})
        if pending >= int(seeder.get("maxPending") or 6):
            return
        daily_cap = int(seeder.get("dailyCap") or 8)
        injected_24h = self.db.count_rows("interlude_script_entry", {
            "storyId": story.id, "kind": "world-event", "occurredAt": {"$gte": to_iso(now - timedelta(hours=24))},
        })
        if injected_24h >= daily_cap:
            return
        request = {
            "storyIdentity": {"setting": story.setting},
            "interval": {"now": to_iso(now)},
        }
        event = await self.narrator.seed_world_event(request, self.config)
        if not event:
            return
        occurs_at = ensure_utc(event.get("occursAt")) or (now + timedelta(hours=1))
        max_horizon = now + timedelta(hours=int(seeder.get("maxHorizonHours") or 72))
        occurs_at = min(occurs_at, max_horizon)
        self.db.insert_row("interlude_seeded_event", {
            "storyId": story.id,
            "summary": event["summary"],
            "importance": event.get("importance") or "low",
            "occursAt": occurs_at,
            "expiresAt": occurs_at + timedelta(hours=6),
            "status": "scheduled",
            "subjects": event.get("subjects") or [],
            "sourcePayload": {},
            "injectedEntryId": None,
            "createdAt": now,
            "updatedAt": now,
        })
        self._log("info", f"世界事件已排期：{clip(event['summary'], 80)}")

    async def _inject_due_seeded_events(self, story: Story, now: datetime) -> None:
        """到点注入 [世界事件] 条目（对应播种器的注入步）。"""
        rows = self.db.list_rows(
            "interlude_seeded_event",
            {"storyId": story.id, "status": "scheduled", "occursAt": {"$lte": to_iso(now)}},
            order_by="occursAt ASC", limit=3,
        )
        for row in rows:
            entry_id = await self.append_entry(story.id, {
                "kind": "world-event",
                "actor": "system",
                "content": f"[世界事件] {row.get('summary')}",
                "occurredAt": to_iso(ensure_utc(row.get("occursAt")) or now),
                "metadata": {"source": "world-seeder", "subjects": json_loads(row.get("subjects"), [])},
            }, now)
            self.db.update_rows("interlude_seeded_event", {"id": row["id"]}, {
                "status": "injected", "injectedEntryId": entry_id, "updatedAt": now,
            })

    # ------------------------------------------------------------------ #
    # 档案检查与设定修改（doctor / setup / story.start）
    # ------------------------------------------------------------------ #

    async def story_start_readiness(self, session: SessionView) -> Dict[str, Any]:
        """启动前检查（对应 storyStartReadiness / interlude.doctor）。"""
        defaults = self.story_defaults
        existing = await self.get_canonical_story()
        main_providers = self.narrator.providers_for("useForMain")
        blockers: List[str] = []
        warnings: List[str] = []
        if not defaults.get("characterName"):
            blockers.append("storyDefaults.characterName 未填写。")
        if not main_providers:
            blockers.append("模型中心没有勾选「用于主叙事」的连接。")
        elif not main_providers[0].model or not resolve_connection_endpoint_safe(main_providers[0]):
            blockers.append("主叙事连接缺少 endpoint 或模型名。")
        if self.onebot.get("enabled") and not self._user_accounts():
            blockers.append("QQ 接入已启用但用户白名单为空，所有私聊都会被拒绝。")
        timezone_name = str(defaults.get("timezone") or "Asia/Shanghai")
        if str(resolve_timezone(timezone_name)) == "UTC" and timezone_name.upper() != "UTC":
            warnings.append(f"时区 “{timezone_name}” 无效，运行时已回退 UTC。")
        preview = {
            "characterName": defaults.get("characterName") or "",
            "characterProfile": (defaults.get("characterProfile") or "").strip(),
            "perspective": (defaults.get("perspective") or "").strip(),
            "world": (defaults.get("world") or "").strip(),
            "timezone": timezone_name,
            "model": main_providers[0].label if main_providers else "未指定",
            "autoCreate": bool(self.runtime.get("autoCreate")),
        }
        return {
            "title": "HDS Interlude 档案检查",
            "preview": preview,
            "blockers": blockers,
            "warnings": warnings,
            "ready": not blockers,
            "existing": existing,
        }

    async def update_setting(self, story: Story, patch: Dict[str, Any]) -> Story:
        """合并修改当前故事设定（对应 updateSetting：interlude.setup）。"""
        allowed = {}
        for key in (
            "character", "user", "relationship", "world", "perspective", "perspectives",
            "supplementaryFacts", "supportingCast", "location", "style", "timezone",
        ):
            if key in patch:
                allowed[key] = patch[key]
        merged = merge_setting(story.setting, allowed)
        if "timezone" in allowed and str(resolve_timezone(str(allowed["timezone"]))) == "UTC" and str(allowed["timezone"]).upper() != "UTC":
            self._log("warn", f"时区 “{allowed['timezone']}” 无效，运行时将回退 UTC")
        now = now_utc()
        self.db.update_rows("interlude_story", {"id": story.id}, {"setting": merged, "updatedAt": now})
        fresh = await self.get_story(story.id)
        assert fresh is not None
        return fresh

    async def set_status(self, story: Story, status: str) -> None:
        """pause / resume（对应 setStatus）。"""
        self.db.update_rows("interlude_story", {"id": story.id}, {"status": status, "updatedAt": now_utc()})
        self._paused = status != "active"
        if status != "active":
            self.turn_engine.invalidate_narratives(story.id)
            self.scheduler.cancel_wakes(story.id)
        else:
            self._schedule_next_automatic_advance(story.id, now_utc())
            self._schedule_due_intent_wake(story.id)

    # ------------------------------------------------------------------ #
    # 重置 / 清库 / 范围删除（reset / database.clear / purge.*）
    # ------------------------------------------------------------------ #

    async def rebase_timeline(self, story: Story) -> Dict[str, Any]:
        """以当前真实时间重建短期时间状态（对应 rebaseTimeline）。"""
        now = now_utc()

        def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            return {
                **state,
                "continuitySnapshot": None,
                "workingDetails": [],
                "workingDetailResolutions": {},
                "timelineCarry": [],
                "extensions": {**(state.get("extensions") or {}), "interruptedOutgoingDrafts": []},
            }

        await self._update_story_state(story.id, mutate, now)
        scene = await self.active_scene(story.id)
        scene_reset = False
        if scene and not scene.summary and scene.entryCount == 0:
            scene_reset = True
        self.db.update_rows("interlude_story", {"id": story.id}, {"cursorAt": now, "updatedAt": now})
        self.turn_engine.invalidate_narratives(story.id)
        self._schedule_next_automatic_advance(story.id, now)
        return {"at": now, "sceneReset": scene_reset}

    async def clear_database(self) -> Dict[str, Any]:
        """清空 HDSI 自有表（对应 clearDatabase）。"""
        self.turn_engine.invalidate_narratives()
        self.scheduler.cancel_wakes()
        removed = self.db.clear_all_tables()
        self.runtime_generation_bump()
        return {"removed": removed, "logicallyCleared": 0}

    async def purge_all_story_data(self, story_id: str) -> None:
        """reset 命令的第二步：删除该故事全部派生数据并重建空白主剧本。"""
        await self.purge_all_data(story_id)

    async def purge_all_data(self, story_id: str) -> None:
        """彻底重置所有平台的剧本与派生数据，保留空白主剧本（对应 purgeAllData）。"""
        story = await self.get_story(story_id)
        if story is None:
            return
        self.turn_engine.invalidate_narratives(story_id)
        for table in (
            "interlude_script_entry", "interlude_memory", "interlude_intent",
            "interlude_scene", "interlude_arc", "interlude_fact", "interlude_state_patch",
            "interlude_overlay_snapshot", "interlude_seeded_event", "interlude_web_observation",
            "interlude_qzone_post", "interlude_schedule_preplan",
        ):
            self.db.delete_rows(table, {"storyId": story_id})
        defaults = self.story_defaults
        setting = empty_story_setting()
        setting["character"] = {"name": str(defaults.get("characterName") or "Unnamed character"), "profile": str(defaults.get("characterProfile") or "")}
        setting["perspective"] = str(defaults.get("perspective") or "")
        setting["perspectives"] = [str(item) for item in (defaults.get("perspectives") or []) if str(item).strip()]
        setting["supplementaryFacts"] = [str(item) for item in (defaults.get("supplementaryFacts") or []) if str(item).strip()]
        setting["user"] = {"displayName": "", "profile": str(defaults.get("userProfile") or "")}
        setting["relationship"] = str(defaults.get("relationship") or "")
        setting["world"] = str(defaults.get("world") or "")
        setting["supportingCast"] = str(defaults.get("supportingCast") or "")
        setting["location"] = str(defaults.get("location") or "")
        setting["style"] = str(defaults.get("style") or setting["style"])
        setting["timezone"] = str(defaults.get("timezone") or "Asia/Shanghai")
        now = now_utc()
        state = empty_story_state()
        state["automation"] = {"nextAdvanceAt": to_iso(now)}
        self.db.update_rows("interlude_story", {"id": story_id}, {
            "setting": setting, "state": encode_story_state(state), "cursorAt": now, "updatedAt": now,
        })
        # 参与者资料按白名单重建
        for row in self.db.list_rows("interlude_participant", {"storyId": story_id}):
            participant = self._row_to_participant(row)
            account = self._user_account_config(participant.userId)
            state_p = empty_participant_state()
            self.db.update_rows("interlude_participant", {"id": participant.id}, {
                "state": state_p,
                "displayName": str(account.get("label") or participant.displayName),
                "profile": str(account.get("profile") or participant.profile),
                "relationship": str(account.get("relationship") or participant.relationship),
                "updatedAt": now,
            })
        for fact_text in setting.get("supplementaryFacts") or []:
            await self._insert_fact(story, "world", fact_text, importance=0.7, confidence=0.9)
        fresh = await self.get_story(story_id)
        if fresh:
            await self._ensure_active_scene(fresh, now)

    async def purge_platform_data(self, platform: str) -> int:
        """清空指定平台的剧本（对应 purgePlatformData；当前单平台下等于归档该剧本）。"""
        normalized = (platform or "").strip().lower()
        if not normalized:
            return 0
        rows = self.db.list_rows("interlude_story")
        count = 0
        for row in rows:
            story = self._row_to_story(row)
            if story.platform.lower() == normalized or (normalized == "onebot" and story.platform.lower() in ("aiocqhttp", "onebot")):
                self.db.update_rows("interlude_story", {"id": story.id}, {"status": "archived", "updatedAt": now_utc()})
                count += 1
        return count

    async def purge_story_range(self, story_id: str, from_at: datetime, to_at: datetime) -> None:
        """删除时间范围内的剧本与关联记忆（对应 purgeStoryRange）。"""
        rng = {"$gte": to_iso(from_at), "$lte": to_iso(to_at)}
        entry_rows = self.db.list_rows("interlude_script_entry", {"storyId": story_id, "occurredAt": rng})
        entry_ids = {int(row["id"]) for row in entry_rows}
        self.db.delete_rows("interlude_script_entry", {"storyId": story_id, "occurredAt": rng})
        # 关联记忆/事实/意图/提案按创建或更新时间清理
        for table in ("interlude_memory", "interlude_intent", "interlude_fact", "interlude_state_patch"):
            self.db.delete_rows(table, {"storyId": story_id, "createdAt": rng})
            self.db.delete_rows(table, {"storyId": story_id, "updatedAt": rng})
        # 与时间范围重叠的场景摘要
        scene_rows = self.db.list_rows("interlude_scene", {"storyId": story_id})
        for row in scene_rows:
            started = ensure_utc(row.get("startedAt"))
            ended = ensure_utc(row.get("endedAt"))
            if started and ended and not (ended < from_at or started > to_at):
                self.db.delete_rows("interlude_scene", {"id": row["id"]})
        self._log("info", f"范围删除完成：条目 {len(entry_ids)} 条")

    # ------------------------------------------------------------------ #
    # 端点与别名（单剧本多通道的简化实现）
    # ------------------------------------------------------------------ #

    def list_story_endpoints(self, story_id: str) -> List[Dict[str, Any]]:
        rows = self.db.list_rows("interlude_endpoint", {"ownerKind": "story", "ownerId": story_id})
        return [
            {
                "endpointId": row.get("id"),
                "platform": row.get("platform"),
                "selfId": row.get("selfId"),
                "channelKind": row.get("channelKind"),
                "enabled": bool(row.get("enabled")),
                "online": True,
            }
            for row in rows
        ]

    async def add_story_endpoint(self, story: Story, platform: str, self_id: str, channel_kind: str) -> Dict[str, Any]:
        if not platform or not self_id:
            return {"ok": False, "error": "平台与账号不能为空"}
        endpoint_id = f"ep:{stable_suffix(f'{story.id}|{platform}|{self_id}|{channel_kind}', 12)}"
        existing = self.db.first_row("interlude_endpoint", {"id": endpoint_id})
        now = now_utc()
        if existing:
            self.db.update_rows("interlude_endpoint", {"id": endpoint_id}, {"enabled": True, "updatedAt": now})
        else:
            self.db.insert_row("interlude_endpoint", {
                "id": endpoint_id, "ownerKind": "story", "ownerId": story.id,
                "channelKind": channel_kind or "qq", "platform": platform, "accountKey": f"{platform}:{self_id}",
                "selfId": self_id, "userId": "", "channelId": "", "groupId": "",
                "conversationKind": "private", "enabled": True, "createdAt": now, "updatedAt": now,
            })
        return {"ok": True, "endpointId": endpoint_id}

    async def disable_story_endpoint(self, endpoint_id: str) -> Dict[str, Any]:
        changed = self.db.update_rows("interlude_endpoint", {"id": endpoint_id}, {"enabled": False, "updatedAt": now_utc()})
        return {"ok": changed > 0, "error": "" if changed else "端点不存在"}

    def list_story_aliases(self) -> List[Dict[str, Any]]:
        rows = self.db.list_rows("interlude_story_alias")
        return [
            {
                "aliasStoryId": row.get("aliasStoryId"),
                "canonicalStoryId": row.get("canonicalStoryId"),
                "reason": row.get("reason"),
                "createdAt": ensure_utc(row.get("createdAt")) or now_utc(),
            }
            for row in rows
        ]

    async def remove_story_alias(self, alias_id: str, audit: str = "") -> Dict[str, Any]:
        row = self.db.first_row("interlude_story_alias", {"aliasStoryId": alias_id})
        if row is None:
            return {"ok": False, "error": "别名不存在"}
        self.db.delete_rows("interlude_story_alias", {"aliasStoryId": alias_id})
        story = await self.get_canonical_story()
        if story:
            await self.serial(story.id, lambda: self.append_entry(story.id, {
                "kind": "system",
                "actor": "admin",
                "content": f"[系统] 剧本别名 {alias_id} 已回滚（{audit}）",
                "occurredAt": to_iso(now_utc()),
                "metadata": {"source": "story-alias-rollback"},
            }, now_utc()))
        return {"ok": True, "error": ""}

    def list_participant_endpoints(self, participant_id: str) -> List[Dict[str, Any]]:
        rows = self.db.list_rows("interlude_endpoint", {"ownerKind": "participant", "ownerId": participant_id})
        return [
            {
                "endpointId": row.get("id"),
                "platform": row.get("platform"),
                "userId": row.get("userId"),
                "enabled": bool(row.get("enabled")),
            }
            for row in rows
        ]

    async def link_participant_endpoint(self, participant: Participant, platform: str, user_id: str) -> Dict[str, Any]:
        if not user_id:
            return {"ok": False, "error": "用户 ID 不能为空"}
        endpoint_id = f"ep:{stable_suffix(f'{participant.id}|{platform}|{user_id}', 12)}"
        now = now_utc()
        existing = self.db.first_row("interlude_endpoint", {"id": endpoint_id})
        if existing:
            self.db.update_rows("interlude_endpoint", {"id": endpoint_id}, {"enabled": True, "updatedAt": now})
        else:
            self.db.insert_row("interlude_endpoint", {
                "id": endpoint_id, "ownerKind": "participant", "ownerId": participant.id,
                "channelKind": "qq", "platform": platform or "onebot", "accountKey": f"{platform}:{user_id}",
                "selfId": participant.selfId, "userId": user_id, "channelId": "", "groupId": "",
                "conversationKind": "private", "enabled": True, "createdAt": now, "updatedAt": now,
            })
        return {"ok": True, "endpointId": endpoint_id}

    async def unlink_participant_endpoint(self, endpoint_id: str) -> Dict[str, Any]:
        changed = self.db.update_rows("interlude_endpoint", {"id": endpoint_id}, {"enabled": False, "updatedAt": now_utc()})
        return {"ok": changed > 0, "error": "" if changed else "端点不存在"}

    # ------------------------------------------------------------------ #
    # 图片与语音加载（平台层提供数据 URI 或本地路径）
    # ------------------------------------------------------------------ #

    async def _load_native_images(self, story: Story, sources: Sequence[str]) -> List[NarrativeImage]:
        vision = (self.config.get("model") or {}).get("vision") or {}
        if not vision.get("enabled"):
            return []
        images: List[NarrativeImage] = []
        for source in sources:
            data = await self._transport.fetch_image_data_uri(source) if hasattr(self._transport, "fetch_image_data_uri") else None
            if not data:
                continue
            mime = "image/jpeg"
            if isinstance(data, tuple):
                data, mime = data
            images.append(NarrativeImage(id=stable_suffix(source, 10), mimeType=mime, dataUri=data))
        return images

    async def _load_native_audio(self, story: Story, sources: Sequence[str]) -> List[NarrativeAudio]:
        audio_config = (self.config.get("model") or {}).get("audio") or {}
        if not audio_config.get("enabled"):
            return []
        result: List[NarrativeAudio] = []
        max_per = int(audio_config.get("maxPerMessage") or 1)
        for source in sources[:max_per]:
            payload = await self._transport.fetch_audio_base64(source) if hasattr(self._transport, "fetch_audio_base64") else None
            if not payload:
                continue
            base64_text = payload[0] if isinstance(payload, tuple) else payload
            fmt = payload[1] if isinstance(payload, tuple) and len(payload) > 1 else (audio_config.get("outFormat") or "mp3")
            result.append(NarrativeAudio(id=stable_suffix(source, 10), format=fmt, base64=base64_text))
        return result

    # ------------------------------------------------------------------ #
    # QZone 通道（依赖 SnowLuma 扩展动作；移植环境不可用时明确拒绝）
    # ------------------------------------------------------------------ #

    async def qzone_available(self) -> bool:
        """QQ 空间通道可用性（对应 qzoneAvailable）。"""
        qzone = self.config.get("qzone") or {}
        if not qzone.get("enabled"):
            return False
        return bool(getattr(self._transport, "qzone_available", lambda: False)())

    async def qzone_execute(self, story: Story, action: str, payload: Dict[str, Any], self_id: str = "") -> Dict[str, Any]:
        """执行空间动作（对应 qzoneExecute）。"""
        if not await self.qzone_available():
            return {"ok": False, "error": "QQ 空间通道不可用（需要支持 qzone 扩展动作的连接）"}
        runner = getattr(self._transport, "qzone_execute", None)
        if runner is None:
            return {"ok": False, "error": "当前平台层未实现空间动作"}
        return await runner(story, action, payload, self_id)

    # ------------------------------------------------------------------ #
    # 后台压缩节流
    # ------------------------------------------------------------------ #

    async def _maybe_background_compaction(self) -> None:
        interval = max(1, int(self.memory_config.get("backgroundIntervalMinutes") or 10)) * 60
        if time.time() - self._last_compaction_at < interval:
            return
        self._last_compaction_at = time.time()
        story = await self.get_canonical_story()
        if story is None or not self.can_handle_story(story):
            return
        if self.has_pending_narrative(story.id):
            return
        now = now_utc()
        await self._inject_due_seeded_events(story, now)
        await self.schedule_compaction(story.id)

    # ------------------------------------------------------------------ #
    # 供命令层使用的杂项
    # ------------------------------------------------------------------ #

    def load_preset_sync_snapshot(self) -> Optional[Dict[str, Any]]:
        """读取分享码应用快照（供 preset.status / rollback）。"""
        path = f"{self.data_dir}/share-snapshot.json"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def save_preset_sync_snapshot(self, snapshot: Dict[str, Any]) -> None:
        path = f"{self.data_dir}/share-snapshot.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)

    def clear_preset_sync_state(self) -> None:
        for name in ("share-snapshot.json", "preset-sync-state.json"):
            try:
                import os

                os.remove(f"{self.data_dir}/{name}")
            except OSError:
                continue

    def close(self) -> None:
        self._closed = True
        self.turn_engine.invalidate_narratives()
        self.scheduler.cancel_wakes()
        self.db.close()


# --------------------------------------------------------------------------- #
# 模块级工具
# --------------------------------------------------------------------------- #

_PERSON_HINTS = re.compile(r"[\u4e00-\u9fff]{2,4}(?:说|告诉|答应|准备|提到)|妈妈|爸爸|母亲|父亲|老师|同事|朋友|同学|室友|哥哥|姐姐|弟弟|妹妹")
_VAGUE_ONLY = re.compile(r"^(他|她|对方|那个人|有人|某人)(说|告诉|答应|觉得|准备|提到)")


def _mentions_person(text: str) -> bool:
    """弱校验：关系事实正文里是否明确写了人物（对应指代不清检查）。"""
    return bool(_PERSON_HINTS.search(text)) or bool(re.search(r"[\u4e00-\u9fff]{2,3}(?=[说他她答准提])", text))


def _vague_pronouns_only(text: str) -> bool:
    return bool(_VAGUE_ONLY.match(text.strip()))


def _append_overlay_text(current: Any, addition: str) -> str:
    base = current.strip() if isinstance(current, str) else ""
    piece = (addition or "").strip()
    if not piece:
        return base
    if not base:
        return piece
    if piece in base:
        return base
    return f"{base}；{piece}"


def resolve_connection_endpoint_safe(provider: Any) -> str:
    from .narrator import resolve_connection_endpoint

    try:
        return resolve_connection_endpoint(provider)
    except Exception:  # noqa: BLE001
        return ""


def _weather_text(code: Any) -> str:
    mapping = {
        0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
        51: "毛毛雨", 53: "小雨", 55: "中雨", 61: "小雨", 63: "中雨", 65: "大雨",
        71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 95: "雷阵雨", 99: "雷暴",
    }
    try:
        return mapping.get(int(code), "未知天况")
    except (TypeError, ValueError):
        return "未知天况"


