"""时间锚层：故事时区、本地时间端点、日照时段。

对应上游 `src/time.ts`。上游用 Intl.DateTimeFormat 拿到故事本地时间的
权威端点（日期、时分秒、星期、GMT 偏移、时段与日照预期），本轮提示词
以它为唯一时钟来源——重载或长间隔后不沿用旧剧本里的钟点描述。

Python 使用 zoneinfo 实现等价能力；无效时区回退 UTC（与上游一致）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

_tz_cache: Dict[str, Optional[ZoneInfo]] = {}

_PERIOD_ZH = {
    "morning": "上午",
    "afternoon": "下午",
    "evening": "傍晚/晚上",
    "night": "夜间",
}


def resolve_timezone(name: str) -> ZoneInfo:
    """把配置里的 IANA 时区名解析为 ZoneInfo；无效时回退 UTC。"""
    candidate = (name or "").strip() or "UTC"
    if candidate in _tz_cache:
        cached = _tz_cache[candidate]
        return cached if cached is not None else timezone.utc
    try:
        zone = ZoneInfo(candidate)
        _tz_cache[candidate] = zone
        return zone
    except Exception:  # noqa: BLE001 - 无效时区一律回退 UTC（对应上游 resolveTimezone）
        _tz_cache[candidate] = None
        return timezone.utc


def _offset_text(value: datetime) -> str:
    """GMT±X 文本，对应上游 timeZoneName: 'shortOffset'。"""
    offset = value.utcoffset()
    total_minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    if minutes:
        return f"GMT{sign}{hours}:{minutes:02d}"
    return f"GMT{sign}{hours}"


def story_local_time_context(value: datetime, timezone_name: str) -> Dict[str, object]:
    """对应上游 `storyLocalTimeContext`：一次算清故事本地时间的所有端点。"""
    zone = resolve_timezone(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(zone)
    hour = local.hour
    if 5 <= hour < 12:
        period = "morning"
    elif 12 <= hour < 18:
        period = "afternoon"
    elif 18 <= hour < 22:
        period = "evening"
    else:
        period = "night"
    if period in ("morning", "afternoon"):
        daylight = "normally daylight unless current weather, season, or setting explicitly says otherwise"
    elif period == "evening":
        daylight = "transitioning toward darkness; use the established season and setting"
    else:
        daylight = "normally dark outside unless the setting explicitly says otherwise"
    date = local.strftime("%Y-%m-%d")
    time_text = local.strftime("%H:%M:%S")
    return {
        "timezone": str(zone),
        "utc": value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "local": f"{date} {time_text}",
        "date": date,
        "time": time_text,
        "hour": hour,
        "weekday": local.strftime("%A"),
        "offset": _offset_text(local),
        "period": period,
        "periodZh": _PERIOD_ZH[period],
        "daylightExpectation": daylight,
    }


def format_log_time(value: Optional[datetime], timezone_name: str) -> str:
    """日志用时间（MM-DD HH:mm:ss，故事本地时区）。"""
    if value is None:
        return "-"
    zone = resolve_timezone(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone).strftime("%m-%d %H:%M:%S")


def format_story_display_time(value: Optional[datetime], timezone_name: str) -> str:
    """剧本展示时间：本地时间 + GMT 偏移（对应 formatStoryDisplayTime）。"""
    if value is None:
        return "-"
    context = story_local_time_context(value, timezone_name)
    return f"{context['local']} {context['offset'] or 'GMT+0'}"


def format_story_clock(value: Optional[datetime], timezone_name: str) -> str:
    """故事本地时钟 HH:mm（对应 rc57 `formatStoryClock`）；无时区时返回空串。"""
    if value is None or not str(timezone_name or "").strip():
        return ""
    zone = resolve_timezone(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone).strftime("%H:%M")


def local_clock_minutes(value: datetime, timezone_name: str) -> int:
    """故事本地时间的分钟数（0..1439），对应 `localClockMinutes`。"""
    zone = resolve_timezone(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(zone)
    return local.hour * 60 + local.minute


def calendar_day_key(value: datetime, timezone_name: str) -> str:
    """故事本地日历日（YYYY-MM-DD），对应 `calendarDayKey`。"""
    zone = resolve_timezone(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone).strftime("%Y-%m-%d")


def parse_hhmm(text: str) -> Optional[int]:
    """解析 'HH:mm' 为分钟数；无效返回 None。"""
    try:
        hour_text, minute_text = str(text).strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def in_clock_window(
    minutes: int, start_minutes: int, end_minutes: int
) -> bool:
    """判断分钟数是否落在 [start, end) 的窗口内；支持跨午夜。"""
    if start_minutes <= end_minutes:
        return start_minutes <= minutes < end_minutes
    return minutes >= start_minutes or minutes < end_minutes
