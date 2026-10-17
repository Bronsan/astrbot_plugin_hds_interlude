"""分享码预设同步（typ0-site 联动）。

对应上游 `src/share-sync.ts`：拉取分享站预设、预览差异、应用写入、
revision 二次校验与快照回滚。同步分区不包含模型连接凭据（连接类
字段会被清洗剔除）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

#: 可同步的配置分区（对应 rc56 起的 SHARE_SECTIONS，18 项；weather 与站点白名单对齐）。
SHARE_SECTIONS = (
    "storyDefaults", "runtime", "urge", "agency", "memory", "alterSystem",
    "schedulePreplan", "timelineDirector", "chatActions", "stickers", "sharedStory",
    "browser", "worldSeeder", "weather", "longHorizon", "qzone", "blindMode", "logging",
)

#: 连接类字段清洗（不进入同步分区）。
_SENSITIVE_KEYS = {"apiKey", "api_key", "token", "extraHeaders", "proxyAgent"}


def normalize_share_code(value: Any) -> str:
    """分享码归一（8 位，字母数字与连字符，大小写不敏感）。"""
    text = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{4,16}", text):
        return ""
    return text


def sanitize_share_payload(payload: Any) -> Any:
    """递归剔除连接类敏感字段（对应 sanitizeSharePayload）。"""
    if isinstance(payload, dict):
        return {
            key: sanitize_share_payload(value)
            for key, value in payload.items()
            if key not in _SENSITIVE_KEYS
        }
    if isinstance(payload, list):
        return [sanitize_share_payload(item) for item in payload]
    return payload


async def fetch_share(base_url: str, code: str, timeout: float = 15.0) -> Dict[str, Any]:
    """拉取分享码预设（对应 fetchShare）。

    返回 { code, title, revision, sections, payload }；远端不可达或结构
    无效时抛出 ValueError，由命令层转为用户可读的失败信息。
    """
    base = (base_url or "").strip().rstrip("/")
    normalized = normalize_share_code(code)
    if not base or not normalized:
        raise ValueError("分享站地址或分享码无效")
    candidates = [
        f"{base}/api/preset/{normalized}",
        f"{base}/api/presets/{normalized}",
        f"{base}/api/share/{normalized}",
    ]
    last_error: Optional[str] = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in candidates:
            try:
                response = await client.get(url)
            except httpx.HTTPError as error:
                last_error = str(error)
                continue
            if response.status_code == 404:
                last_error = "分享码不存在"
                continue
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                continue
            try:
                data = response.json()
            except ValueError:
                last_error = "返回不是 JSON"
                continue
            if not isinstance(data, dict):
                last_error = "返回结构无效"
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                inner = data.get("preset")
                payload = inner.get("payload") if isinstance(inner, dict) else None
            if not isinstance(payload, dict):
                last_error = "预设缺少 payload"
                continue
            sections = data.get("sections")
            if not isinstance(sections, list):
                sections = [key for key in payload.keys() if key in SHARE_SECTIONS]
            sections = [str(item) for item in sections if str(item) in SHARE_SECTIONS]
            return {
                "code": str(data.get("code") or normalized),
                "title": str(data.get("title") or "未命名预设"),
                "revision": int(data.get("revision") or 0),
                "sections": sections,
                "payload": sanitize_share_payload(payload),
            }
    raise ValueError(last_error or "分享站不可达")


def diff_section(current: Any, incoming: Any, limit: int = 30) -> List[str]:
    """两个配置分区的差异行（对应 diffSection，简化为关键字段级差异）。"""
    lines: List[str] = []
    if not isinstance(incoming, dict):
        return lines
    current_dict = current if isinstance(current, dict) else {}
    for key, value in incoming.items():
        old = current_dict.get(key)
        if old == value:
            continue
        old_preview = _preview(old, 60)
        new_preview = _preview(value, 60)
        lines.append(f"  {key}: {old_preview} → {new_preview}")
        if len(lines) >= limit:
            break
    return lines


def _preview(value: Any, limit: int) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def summarize_share(current_config: Dict[str, Any], info: Dict[str, Any], sections: List[str]) -> Dict[str, Any]:
    """预览摘要（对应 summarizeShare）。"""
    diffs: List[Tuple[str, List[str]]] = []
    story_patch: Dict[str, Any] = {}
    for section in sections:
        incoming = (info.get("payload") or {}).get(section)
        if incoming is None:
            continue
        current = current_config.get(section)
        lines = diff_section(current, incoming)
        if section == "storyDefaults" and isinstance(incoming, dict):
            story_patch = incoming
        if lines:
            diffs.append((section, lines))
    return {
        "diffs": diffs,
        "storyPatch": story_patch,
        "configSections": [section for section in sections if section != "storyDefaults"],
    }


def format_diff_lines(summary: Dict[str, Any]) -> List[str]:
    """把预览摘要格式化为输出文本（对应 formatDiffLines）。"""
    lines: List[str] = []
    for section, entries in summary.get("diffs") or []:
        lines.append(f"[{section}]")
        lines.extend(entries)
    return lines


def build_config_patch(
    current_config: Dict[str, Any], payload: Dict[str, Any], sections: List[str], include_story_defaults: bool = False
) -> Dict[str, Any]:
    """构建写回配置的补丁（对应 buildConfigPatch：整段替换）。"""
    patch: Dict[str, Any] = {}
    for section in sections:
        if section == "storyDefaults" and not include_story_defaults:
            continue
        if section in payload:
            patch[section] = payload[section]
    return patch


def default_share_base() -> str:
    import os

    return (os.environ.get("HDSI_SHARE_BASE") or "").strip() or "https://typ0.space"
