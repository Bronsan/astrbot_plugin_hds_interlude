"""持久化层：HDSI 自有 SQLite 数据库（21 张表）。

对应上游 `src/database.ts` 的 `registerTables`。上游建在 Koishi 的
minato/SQLite 服务上；移植版使用 Python 标准库 sqlite3，数据库文件放在
AstrBot 的插件数据目录（`data/plugin_data/astrbot_plugin_hds_interlude/`），
绝不与 AstrBot 自身或其它插件的数据混写——对应上游
`interlude.database.clear` 只清 HDSI 自有表的语义。

表结构与上游逐字段一致（`knowledge` / `embedding` 用 JSON 列，
时间统一存 ISO-8601 文本）。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .types import json_dumps, json_loads, to_iso

#: 全部 HDSI 自有表名（顺序与上游 registerTables 一致）。
TABLE_NAMES: Tuple[str, ...] = (
    "interlude_story",
    "interlude_participant",
    "interlude_script_entry",
    "interlude_memory",
    "interlude_intent",
    "interlude_scene",
    "interlude_arc",
    "interlude_fact",
    "interlude_state_patch",
    "interlude_web_observation",
    "interlude_overlay_snapshot",
    "interlude_sticker",
    "interlude_seeded_event",
    "interlude_qzone_post",
    "interlude_endpoint",
    "interlude_endpoint_state",
    "interlude_story_alias",
    "interlude_schedule_preplan",
    "interlude_long_arc_guidance",
    "interlude_long_arc_progress",
    "interlude_work",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interlude_story (
  id TEXT PRIMARY KEY,
  platform TEXT, selfId TEXT, userId TEXT, channelId TEXT,
  status TEXT, setting TEXT, state TEXT,
  cursorAt TEXT, createdAt TEXT, updatedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_story_platform ON interlude_story(platform);

CREATE TABLE IF NOT EXISTS interlude_participant (
  id TEXT PRIMARY KEY,
  storyId TEXT, platform TEXT, selfId TEXT, userId TEXT, channelId TEXT,
  personId TEXT, displayName TEXT, profile TEXT, relationship TEXT,
  state TEXT, status TEXT, createdAt TEXT, updatedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_participant_story ON interlude_participant(storyId, status);

CREATE TABLE IF NOT EXISTS interlude_script_entry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, participantId TEXT, kind TEXT, actor TEXT, content TEXT,
  occurredAt TEXT, metadata TEXT, embedding TEXT, createdAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_entry_story_time ON interlude_script_entry(storyId, occurredAt);

CREATE TABLE IF NOT EXISTS interlude_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, participantId TEXT, category TEXT, content TEXT,
  importance REAL, status TEXT, sourceEntryId INTEGER,
  createdAt TEXT, updatedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_story ON interlude_memory(storyId, importance);

CREATE TABLE IF NOT EXISTS interlude_intent (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, participantId TEXT, type TEXT, summary TEXT,
  notBefore TEXT, status TEXT, payload TEXT, createdAt TEXT, updatedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_intent_story ON interlude_intent(storyId, status, notBefore);

CREATE TABLE IF NOT EXISTS interlude_scene (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, status TEXT, startedAt TEXT, endedAt TEXT,
  hook TEXT, summary TEXT, entryCount INTEGER, lastEntryId INTEGER,
  createdAt TEXT, updatedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_scene_story ON interlude_scene(storyId, status);

CREATE TABLE IF NOT EXISTS interlude_arc (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, status TEXT, title TEXT, summary TEXT, sceneCount INTEGER,
  createdAt TEXT, updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_fact (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, participantId TEXT, scope TEXT, content TEXT,
  importance REAL, confidence REAL, unresolved INTEGER,
  embedding TEXT, knowledge TEXT, status TEXT, sourceEntryIds TEXT,
  lastSeenAt TEXT, createdAt TEXT, updatedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_fact_story ON interlude_fact(storyId, status, importance);

CREATE TABLE IF NOT EXISTS interlude_state_patch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, participantId TEXT, target TEXT, path TEXT,
  proposedValue TEXT, evidence TEXT, confidence REAL, impact TEXT,
  status TEXT, sourceEntryIds TEXT, createdAt TEXT, appliedAt TEXT
);
CREATE INDEX IF NOT EXISTS idx_patch_story ON interlude_state_patch(storyId, status, confidence);

CREATE TABLE IF NOT EXISTS interlude_web_observation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, participantId TEXT, intentId INTEGER, mode TEXT,
  query TEXT, url TEXT, title TEXT, excerpt TEXT, summary TEXT,
  status TEXT, accessedAt TEXT, createdAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_overlay_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, participantId TEXT, target TEXT, tier TEXT,
  periodStart TEXT, periodEnd TEXT, summary TEXT, majorEvents TEXT,
  sourcePatchIds TEXT, status TEXT, createdAt TEXT, updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_sticker (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  assetId TEXT UNIQUE, filePath TEXT, group_name TEXT, mimeType TEXT,
  animated INTEGER, size INTEGER, hash TEXT, description TEXT,
  aliases TEXT, status TEXT, embedding TEXT, createdAt TEXT, updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_seeded_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, summary TEXT, importance TEXT, occursAt TEXT, expiresAt TEXT,
  status TEXT, subjects TEXT, sourcePayload TEXT, injectedEntryId INTEGER,
  createdAt TEXT, updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_qzone_post (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, kind TEXT, tid TEXT, targetUin TEXT, content TEXT,
  ugcRight INTEGER, endpointId TEXT, commentNum INTEGER, status TEXT, error TEXT,
  createdAt TEXT, postedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_endpoint (
  id TEXT PRIMARY KEY,
  ownerKind TEXT, ownerId TEXT, channelKind TEXT, platform TEXT,
  accountKey TEXT, selfId TEXT, userId TEXT, channelId TEXT, groupId TEXT,
  conversationKind TEXT, enabled INTEGER, createdAt TEXT, updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_endpoint_state (
  endpointId TEXT PRIMARY KEY, state TEXT, updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_story_alias (
  aliasStoryId TEXT PRIMARY KEY, canonicalStoryId TEXT, reason TEXT, createdAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_schedule_preplan (
  storyId TEXT PRIMARY KEY,
  revision INTEGER, timezone TEXT, validFrom TEXT, validThrough TEXT,
  lastReviewedLocalDate TEXT, lastEvidenceEntryId INTEGER, reviewReason TEXT,
  regimes TEXT, exceptions TEXT, materializedDays TEXT,
  createdAt TEXT, updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_long_arc_guidance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  storyId TEXT, version INTEGER, status TEXT, title TEXT, premise TEXT,
  direction TEXT, payload TEXT, currentStage TEXT, intensity TEXT,
  confidence REAL, triggerEntryId INTEGER, evidenceEntryIds TEXT,
  supersedesId INTEGER, createdAt TEXT, updatedAt TEXT, completedAt TEXT, expiresAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_long_arc_progress (
  storyId TEXT PRIMARY KEY,
  lastCountedEntryId INTEGER, totalScore REAL,
  privateCount INTEGER, privateScore REAL, groupCount INTEGER, groupScore REAL,
  unknownCount INTEGER, lastGenerationScore REAL, lastGenerationEntryId INTEGER,
  updatedAt TEXT
);

CREATE TABLE IF NOT EXISTS interlude_work (
  id TEXT PRIMARY KEY, storyId TEXT, participantId TEXT,
  generation INTEGER, state TEXT
);
"""

#: JSON 列（读出时反序列化、写入时序列化）。
_JSON_COLUMNS = {
    "setting", "state", "metadata", "embedding", "payload", "knowledge",
    "sourceEntryIds", "majorEvents", "sourcePatchIds", "aliases",
    "subjects", "sourcePayload", "regimes", "exceptions", "materializedDays",
    "evidenceEntryIds", "payload", "state",
}

#: 时间列（读出时保持 ISO 字符串，由上层 ensure_utc 解析）。
_TIME_COLUMNS = {
    "cursorAt", "createdAt", "updatedAt", "occurredAt", "notBefore",
    "startedAt", "endedAt", "lastSeenAt", "appliedAt", "periodStart",
    "periodEnd", "accessedAt", "occursAt", "expiresAt", "postedAt",
    "completedAt",
}


class Database:
    """HDSI 自有数据库封装。

    使用单连接 + 写锁 + WAL；所有公开方法都可在事件循环里直接同步调用
    （单次读写微秒级），重量级批处理由上层放进 `asyncio.to_thread`。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 底层行操作
    # ------------------------------------------------------------------ #

    @staticmethod
    def _where_clause(where: Dict[str, Any]) -> Tuple[str, List[Any]]:
        """把 {col: value} / {col: {'$in': [...]}} / {'$lte'/'$gte'/'$lt'/'$gt'} 转 SQL。"""
        parts: List[str] = []
        params: List[Any] = []
        for column, value in where.items():
            if isinstance(value, dict):
                for op, operand in value.items():
                    sql_op = {
                        "$in": "IN",
                        "$lte": "<=",
                        "$gte": ">=",
                        "$lt": "<",
                        "$gt": ">",
                        "$ne": "!=",
                    }.get(op)
                    if sql_op is None:
                        continue
                    if sql_op == "IN":
                        items = list(operand or [])
                        if not items:
                            parts.append("1=0")
                            continue
                        placeholders = ",".join("?" for _ in items)
                        parts.append(f"{column} IN ({placeholders})")
                        params.extend(items)
                    else:
                        if op in ("$lte", "$gte", "$lt", "$gt") and column in _TIME_COLUMNS:
                            parts.append(f"{column} {sql_op} ?")
                            params.append(to_iso(operand))
                        else:
                            parts.append(f"{column} {sql_op} ?")
                            params.append(operand)
            else:
                if value is None:
                    parts.append(f"{column} IS NULL")
                else:
                    parts.append(f"{column} = ?")
                    params.append(value)
        clause = " AND ".join(parts) if parts else "1=1"
        return clause, params

    @staticmethod
    def _normalize_row(row: sqlite3.Row) -> Dict[str, Any]:
        """行 → dict，JSON 列反序列化。"""
        data = dict(row)
        for column in list(data.keys()):
            if column in _JSON_COLUMNS and isinstance(data[column], str):
                data[column] = json_loads(data[column], None)
        # sticker 表的 group 列在 SQLite 里叫 group_name（group 是保留字）
        if "group_name" in data and "group" not in data:
            data["group"] = data.pop("group_name")
        return data

    @staticmethod
    def _serialize(changes: Dict[str, Any], table: str) -> Tuple[List[str], List[Any]]:
        columns: List[str] = []
        values: List[Any] = []
        for column, value in changes.items():
            if table == "interlude_sticker" and column == "group":
                column = "group_name"
            if column in _JSON_COLUMNS:
                value = json_dumps(value)
            elif column in _TIME_COLUMNS and value is not None:
                value = to_iso(value)
            elif isinstance(value, bool):
                value = 1 if value else 0
            columns.append(column)
            values.append(value)
        return columns, values

    def list_rows(
        self,
        table: str,
        where: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """通用查询。"""
        clause, params = self._where_clause(where or {})
        sql = f"SELECT * FROM {table} WHERE {clause}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
            if offset:
                sql += f" OFFSET {int(offset)}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._normalize_row(row) for row in rows]

    def first_row(
        self, table: str, where: Optional[Dict[str, Any]] = None, order_by: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        rows = self.list_rows(table, where, order_by=order_by, limit=1)
        return rows[0] if rows else None

    def insert_row(self, table: str, values: Dict[str, Any]) -> int:
        """插入一行，返回自增 id（无自增列时返回 0）。"""
        columns, params = self._serialize(values, table)
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def update_rows(self, table: str, where: Dict[str, Any], changes: Dict[str, Any]) -> int:
        """按条件更新，返回影响行数。"""
        if not changes:
            return 0
        set_parts: List[str] = []
        params: List[Any] = []
        for column, value in changes.items():
            if table == "interlude_sticker" and column == "group":
                column = "group_name"
            if column in _JSON_COLUMNS:
                value = json_dumps(value)
            elif column in _TIME_COLUMNS and value is not None:
                value = to_iso(value)
            elif isinstance(value, bool):
                value = 1 if value else 0
            set_parts.append(f"{column} = ?")
            params.append(value)
        clause, where_params = self._where_clause(where)
        sql = f"UPDATE {table} SET {','.join(set_parts)} WHERE {clause}"
        with self._lock:
            cursor = self._conn.execute(sql, params + where_params)
            self._conn.commit()
            return cursor.rowcount

    def delete_rows(self, table: str, where: Optional[Dict[str, Any]] = None) -> int:
        """删除行；where 为空表示清空全表（仅限内部维护调用）。"""
        if where is None:
            sql, params = f"DELETE FROM {table}", []
        else:
            clause, params = self._where_clause(where)
            sql = f"DELETE FROM {table} WHERE {clause}"
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.rowcount

    def count_rows(self, table: str, where: Optional[Dict[str, Any]] = None) -> int:
        clause, params = self._where_clause(where or {})
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {clause}", params
            ).fetchone()
        return int(row["n"]) if row else 0

    def clear_all_tables(self) -> int:
        """清空全部 HDSI 表，返回删除的记录总数（interlude.database.clear）。"""
        total = 0
        with self._lock:
            for table in TABLE_NAMES:
                try:
                    total += self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                    self._conn.execute(f"DELETE FROM {table}")
                except sqlite3.Error:
                    continue
            self._conn.commit()
            try:
                self._conn.execute("VACUUM")  # 事务外回收空间；失败不影响清库结果
                self._conn.commit()
            except sqlite3.Error:
                pass
        return total

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    # ------------------------------------------------------------------ #
    # 高层便捷方法（服务层绑定业务语义）
    # ------------------------------------------------------------------ #

    def append_script_entry(self, entry: Dict[str, Any]) -> int:
        """写入剧本条目，返回条目 id（id 为自增）。"""
        entry = dict(entry)
        entry.pop("id", None)
        return self.insert_row("interlude_script_entry", entry)

    async def run_in_thread(self, fn, *args, **kwargs):  # pragma: no cover - 便捷入口
        """在写线程池里执行阻塞操作（用于 VACUUM 等重操作）。"""
        return await asyncio.to_thread(fn, *args, **kwargs)


def parse_entry_ids(value: Any) -> List[int]:
    """把 JSON 列读出的 sourceEntryIds 归一为 int 列表。"""
    if not isinstance(value, (list, tuple)):
        return []
    result: List[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def quote_ident(name: str) -> str:
    """转义 SQL 标识符（内部只用于表名白名单场景）。"""
    if name not in TABLE_NAMES:
        raise ValueError(f"unknown table: {name}")
    return f'"{name}"'


def chunked(items: Sequence[Any], size: int = 500) -> Iterable[Sequence[Any]]:
    """把序列切成小块（用于 IN 子句规模控制）。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]
