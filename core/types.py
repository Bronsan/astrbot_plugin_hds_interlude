"""HDS Interlude 的数据协议层。

对应上游 `src/types.ts`。上游用 TypeScript 接口描述跨模块数据协议；
这里用 dataclass / 类型别名保持同一套字段名（camelCase 原样保留，
便于与上游剧本数据、文档和排障日志逐字对照）。

所有从 SQLite 读出的行都会经过 `normalize_*` 系列函数，
把 JSON 列里可能缺失/类型漂移的字段补回默认值——与上游
`normalizeDatabaseRow` / `decodeStoryState` 的兼容思路一致。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# 枚举与基础类型（对应 types.d.ts 开头的 type 别名）
# --------------------------------------------------------------------------- #

StoryStatus = str  # 'active' | 'paused' | 'archived'
SceneStatus = str  # 'active' | 'closed'
FactScope = str  # 'character' | 'world' | 'relationship' | 'event' | 'promise'
StatePatchTarget = str  # 'character' | 'perspective' | 'world' | 'relationship'
StatePatchStatus = str  # 'proposed' | 'applied' | 'compacted' | 'rejected' | 'cleared'
IntentStatus = str  # 'pending' | 'completed' | 'cancelled'
NarrativePhase = str  # 'advance' | 'conversation-follow-up' | 'user-message' | 'intent-due'
InteractionReplyMode = str  # 'none' | 'immediate' | 'delayed'
LifeRhythmAttention = str  # 'phone-focused' | 'available' | 'occupied' | 'resting' | 'asleep'

FACT_SCOPES = ("character", "world", "relationship", "event", "promise")
STATE_PATCH_TARGETS = ("character", "perspective", "relationship", "world")
OVERLAY_CLEAR_TARGETS = ("character", "perspective", "relationship", "world", "all")

#: 分段消息标记的默认值（上游 runtime.messageSeparator）。
DEFAULT_MESSAGE_SEPARATOR = "<sep/>"


def now_utc() -> datetime:
    """当前 UTC 时间。项目中一切持久化时间都用 aware UTC datetime。"""
    return datetime.now(timezone.utc)


def ensure_utc(value: Any) -> Optional[datetime]:
    """把数据库里读出的时间值归一为 aware UTC datetime；无效值返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(value: Any) -> str:
    """datetime → ISO-8601 字符串（UTC，带 Z）；非时间值原样返回字符串。"""
    parsed = ensure_utc(value)
    if parsed is None:
        return str(value or "")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def json_dumps(value: Any) -> str:
    """稳定 JSON 序列化（用于 JSON 列）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: Any, default: Any = None) -> Any:
    """宽容 JSON 反序列化；失败或空值返回 default。"""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 故事设定与状态
# --------------------------------------------------------------------------- #


def empty_story_setting() -> Dict[str, Any]:
    """对应上游 `emptyStorySetting()`。"""
    return {
        "character": {"name": "Unnamed character", "profile": ""},
        "user": {"displayName": "", "profile": ""},
        "relationship": "",
        "world": "",
        "perspective": "",
        "perspectives": [],
        "supplementaryFacts": [],
        "supportingCast": "",
        "location": "",
        "style": "现实主义日常叙事，情绪克制，关系变化缓慢而具体。",
        "timezone": "Asia/Shanghai",
    }


def empty_story_state() -> Dict[str, Any]:
    """对应上游 `emptyStoryState()`（schemaVersion 与 extensions 保兼容）。"""
    return {
        "schemaVersion": 1,
        "extensions": {},
        "settingOverlay": {},
        "narrativeUpdateCount": 0,
        "continuityDirty": True,
        "automation": {},
        "lifeStatus": None,
        "lifeRhythm": None,
        "timelineCarry": [],
        "workingDetails": [],
        "workingDetailResolutions": {},
        "automaticDeliverySummaries": [],
        "scenePresence": [],
        "proactiveContactLog": [],
        "activeConsequencePromptedAt": {},
    }


def empty_participant_state() -> Dict[str, Any]:
    """对应上游 `emptyParticipantState()`。"""
    return {
        "openThreads": [],
        "relationshipNotes": [],
        "relationshipOverlay": "",
        "unreadMessageCount": 0,
        "pendingReplyCount": 0,
        "lastUserMessageAt": None,
        "lastCharacterMessageAt": None,
    }


def normalize_story_state(raw: Any) -> Dict[str, Any]:
    """宽容读取 story.state 列（对应 `decodeStoryState` 的核心行为）。"""
    base = empty_story_state()
    data = json_loads(raw, {})
    if not isinstance(data, dict):
        return base
    merged = {**base, **data}
    if not isinstance(merged.get("settingOverlay"), dict):
        merged["settingOverlay"] = {}
    if not isinstance(merged.get("automation"), dict):
        merged["automation"] = {}
    if not isinstance(merged.get("extensions"), dict):
        merged["extensions"] = {}
    return merged


def encode_story_state(state: Dict[str, Any]) -> str:
    """写出 story.state 列。"""
    return json_dumps(state)


# --------------------------------------------------------------------------- #
# 数据库行 dataclass（字段名与上游 21 张表一致）
# --------------------------------------------------------------------------- #


@dataclass
class Story:
    """interlude_story 行。"""

    id: str
    platform: str
    selfId: str
    userId: str
    channelId: str
    status: str
    setting: Dict[str, Any]
    state: Dict[str, Any]
    cursorAt: datetime
    createdAt: datetime
    updatedAt: datetime


@dataclass
class Participant:
    """interlude_participant 行：一个私聊关系分支。"""

    id: str
    storyId: str
    platform: str
    selfId: str
    userId: str
    channelId: str
    personId: str
    displayName: str
    profile: str
    relationship: str
    state: Dict[str, Any]
    status: str
    createdAt: datetime
    updatedAt: datetime


@dataclass
class ScriptEntry:
    """interlude_script_entry 行：原始剧本条目。"""

    id: int
    storyId: str
    participantId: str
    kind: str
    actor: str
    content: str
    occurredAt: datetime
    metadata: Dict[str, Any]
    createdAt: datetime
    embedding: Optional[List[float]] = None


@dataclass
class Memory:
    """interlude_memory 行：场景记忆摘要。"""

    id: int
    storyId: str
    participantId: str
    category: str
    content: str
    importance: float
    status: str
    sourceEntryId: Optional[int]
    createdAt: datetime
    updatedAt: datetime


@dataclass
class Scene:
    """interlude_scene 行：活跃/已关闭场景。"""

    id: int
    storyId: str
    status: str
    startedAt: datetime
    endedAt: Optional[datetime]
    hook: str
    summary: str
    entryCount: int
    lastEntryId: Optional[int]
    createdAt: datetime
    updatedAt: datetime


@dataclass
class Arc:
    """interlude_arc 行：剧情弧线。"""

    id: int
    storyId: str
    status: str
    title: str
    summary: str
    sceneCount: int
    createdAt: datetime
    updatedAt: datetime


@dataclass
class Fact:
    """interlude_fact 行：长期事实。"""

    id: int
    storyId: str
    participantId: str
    scope: str
    content: str
    importance: float
    confidence: float
    unresolved: bool
    status: str
    sourceEntryIds: List[int]
    lastSeenAt: datetime
    createdAt: datetime
    updatedAt: datetime
    embedding: Optional[List[float]] = None
    knowledge: Optional[Dict[str, Any]] = None


@dataclass
class Intent:
    """interlude_intent 行：延迟回复 / 提醒 / 承诺 / 剧情余波 / 分段消息。"""

    id: int
    storyId: str
    participantId: str
    type: str
    summary: str
    notBefore: datetime
    status: str
    payload: Dict[str, Any]
    createdAt: datetime
    updatedAt: datetime


@dataclass
class StatePatch:
    """interlude_state_patch 行：设定演化提案。"""

    id: int
    storyId: str
    participantId: str
    target: str
    path: str
    proposedValue: str
    evidence: str
    confidence: float
    impact: str
    status: str
    sourceEntryIds: List[int]
    createdAt: datetime
    appliedAt: Optional[datetime]


@dataclass
class OverlaySnapshot:
    """interlude_overlay_snapshot 行：overlay 压缩快照。"""

    id: int
    storyId: str
    participantId: str
    target: str
    tier: str
    periodStart: datetime
    periodEnd: datetime
    summary: str
    majorEvents: List[str]
    sourcePatchIds: List[int]
    status: str
    createdAt: datetime
    updatedAt: datetime


@dataclass
class Endpoint:
    """interlude_endpoint 行：角色端点（账号迁移的显式通道）。"""

    id: str
    ownerKind: str  # 'story' | 'participant'
    ownerId: str
    channelKind: str  # 'qq' | 'wechat'
    platform: str
    accountKey: str
    selfId: str
    userId: str
    channelId: str
    groupId: str
    conversationKind: str  # 'private' | 'group'
    enabled: bool
    createdAt: datetime
    updatedAt: datetime


@dataclass
class SchedulePreplan:
    """interlude_schedule_preplan 行：近期日程层。"""

    storyId: str
    revision: int
    timezone: str
    validFrom: str
    validThrough: str
    lastReviewedLocalDate: str
    lastEvidenceEntryId: int
    reviewReason: str
    regimes: List[Dict[str, Any]]
    exceptions: List[Dict[str, Any]]
    materializedDays: List[Dict[str, Any]]
    createdAt: datetime
    updatedAt: datetime


# --------------------------------------------------------------------------- #
# 叙事请求/决策协议（对应 types.d.ts 的 NarrativeRequest / NarrativeDecision）
# --------------------------------------------------------------------------- #


@dataclass
class NarrativeImage:
    """当前私聊回合的图片附件（不落库）。"""

    id: str
    mimeType: str
    dataUri: str


@dataclass
class NarrativeAudio:
    """当前私聊回合的语音附件（不落库）。"""

    id: str
    format: str
    base64: str


@dataclass
class TimelinePlan:
    """时间导演产出的相对时间账本。"""

    beats: List[Dict[str, Any]] = field(default_factory=list)
    carry: List[str] = field(default_factory=list)


@dataclass
class NarrativeRequest:
    """主叙事请求包（对应上游 NarrativeRequest 的移植子集）。"""

    phase: NarrativePhase
    story: Story
    from_at: datetime
    now: datetime
    participant: Optional[Participant] = None
    participants: List[Participant] = field(default_factory=list)
    userMessage: str = ""
    images: List[NarrativeImage] = field(default_factory=list)
    audio: List[NarrativeAudio] = field(default_factory=list)
    visualObservations: List[str] = field(default_factory=list)
    timelinePlan: Optional[TimelinePlan] = None
    timelineCarry: List[str] = field(default_factory=list)
    shareParticipantDetails: bool = False
    dueIntents: List[Intent] = field(default_factory=list)
    upcomingIntents: List[Intent] = field(default_factory=list)
    activeConsequences: List[Intent] = field(default_factory=list)
    supersededIntents: List[Intent] = field(default_factory=list)
    recentEntries: List[ScriptEntry] = field(default_factory=list)
    memories: List[Memory] = field(default_factory=list)
    facts: List[Fact] = field(default_factory=list)
    scene: Optional[Scene] = None
    arc: Optional[Arc] = None
    previousScenes: List[Dict[str, Any]] = field(default_factory=list)
    overlaySnapshots: List[OverlaySnapshot] = field(default_factory=list)
    groupContext: Optional[Dict[str, Any]] = None
    quotedMessages: List[Dict[str, Any]] = field(default_factory=list)
    alterEnabled: bool = False
    emotionalOffset: Optional[Dict[str, Any]] = None
    agencyEnabled: bool = False
    agencyWindow: Optional[Dict[str, Any]] = None
    automaticDeliverySummaries: List[Dict[str, Any]] = field(default_factory=list)
    followUpCommitments: List[Intent] = field(default_factory=list)
    schedulePreplan: Optional[Dict[str, Any]] = None
    workingDetails: List[Dict[str, Any]] = field(default_factory=list)
    scenePresence: List[Dict[str, Any]] = field(default_factory=list)
    deliveryReality: List[Dict[str, Any]] = field(default_factory=list)
    continuation: Optional[Dict[str, Any]] = None
    refreshContinuity: bool = False
    outputRecovery: bool = False
    messageSeparator: str = DEFAULT_MESSAGE_SEPARATOR
    splitReplyMessages: bool = True
    stickyChatNotice: Optional[str] = None


# --------------------------------------------------------------------------- #
# 昵称哈希（对应上游 scopeKey 的脱敏思路：共享状态里不出现原始私聊标识）
# --------------------------------------------------------------------------- #


def stable_suffix(value: str, length: int = 8) -> str:
    """取稳定短哈希后缀，用于生成可读但不泄漏原文的标识。"""
    import hashlib

    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:length]


def deep_copy(value: Any) -> Any:
    """深拷贝（快照用）。"""
    return copy.deepcopy(value)
