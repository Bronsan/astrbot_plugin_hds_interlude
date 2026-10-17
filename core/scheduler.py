"""调度状态机：每故事最早到期唤醒、独占任务门与指纹冷却表。

对应上游 `src/scheduler.ts`（逐行移植）。

- `schedule_wake`：keep-earliest——新唤醒不晚于已有唤醒时替换；
  执行返回 "busy" 时按固定延迟重排（不丢失到期事件）；
- `schedule_exclusive`：同一 key 的独占任务去重；支持 halt（任务代次
  失效，直接放弃）与 defer（暂停/换库中，稍后重试）；
- 指纹冷却：同一未变化输入（例如同一场景压缩失败）在冷却期内不重试。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional, Set


class _WakeToken:
    __slots__ = ("dueAt", "handle")

    def __init__(self, due_at: float) -> None:
        self.dueAt = due_at
        self.handle: Optional[asyncio.TimerHandle] = None

    def cancel(self) -> None:
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None


class Scheduler:
    """对应 createScheduler 的对外接口。"""

    def __init__(
        self,
        retry_delay_ms: int = 1500,
        defer_delay_ms: int = 1000,
        on_wake_error: Optional[Callable[[BaseException], None]] = None,
        on_task_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._wake_timers: Dict[str, _WakeToken] = {}
        self._exclusive: Set[str] = set()
        self._backoffs: Dict[str, Dict[str, float]] = {}
        self._retry_delay_ms = retry_delay_ms
        self._defer_delay_ms = defer_delay_ms
        self._on_wake_error = on_wake_error
        self._on_task_error = on_task_error
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.get_event_loop()
        return self._loop

    # ------------------------------------------------------------------ #
    # 唤醒（keep-earliest）
    # ------------------------------------------------------------------ #

    def schedule_wake(self, story_id: str, not_before: float, fire: Callable[[], Any]) -> Optional[float]:
        """为一个故事设置最早唤醒时刻；返回实际等待秒数（None = 不需要新定时器）。

        `fire` 是同步可调用对象，返回 "busy" 时表示目标被占用，
        调度器会按 retry_delay 重排。
        """
        due_at = float(not_before)
        existing = self._wake_timers.get(story_id)
        if existing and existing.dueAt <= due_at and existing.dueAt != float("inf"):
            return None
        if existing:
            existing.cancel()
        token = _WakeToken(due_at)

        def wake() -> None:
            if self._wake_timers.get(story_id) is not token:
                return
            token.dueAt = float("inf")
            result = fire()
            if self._wake_timers.get(story_id) is not token:
                return
            if result != "busy":
                self._wake_timers.pop(story_id, None)
                return
            token.dueAt = time.time() + self._retry_delay_ms / 1000.0
            token.handle = self._event_loop().call_later(self._retry_delay_ms / 1000.0, wake)

        try:
            delay = max(0.0, due_at - time.time())
            token.handle = self._event_loop().call_later(delay, wake)
        except BaseException as error:  # noqa: BLE001 - 调度失败由上层记录
            if self._on_wake_error:
                self._on_wake_error(error)
            return None
        self._wake_timers[story_id] = token
        return delay

    def cancel_wake(self, story_id: str) -> None:
        token = self._wake_timers.pop(story_id, None)
        if token:
            token.cancel()

    def cancel_wakes(self, story_id: Optional[str] = None) -> None:
        for key in list(self._wake_timers.keys()):
            if story_id and key != story_id:
                continue
            self.cancel_wake(key)

    def pending_wake_count(self) -> int:
        return len(self._wake_timers)

    # ------------------------------------------------------------------ #
    # 独占任务
    # ------------------------------------------------------------------ #

    def schedule_exclusive(
        self,
        key: str,
        task: Callable[[], Any],
        halted: Optional[Callable[[], bool]] = None,
        defer: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """同一 key 的独占任务去重；返回是否成功排队。"""
        if key in self._exclusive:
            return False
        self._exclusive.add(key)

        def run() -> None:
            if halted and halted():
                self._exclusive.discard(key)
                return
            if defer and defer():
                self._event_loop().call_later(self._defer_delay_ms / 1000.0, run)
                return
            future = asyncio.ensure_future(task())

            def _done(_future: "asyncio.Future[Any]") -> None:
                self._exclusive.discard(key)
                error = _future.exception()
                if error and self._on_task_error:
                    self._on_task_error(error)

            future.add_done_callback(_done)

        run()
        return True

    def has_exclusive(self, key: str) -> bool:
        return key in self._exclusive

    # ------------------------------------------------------------------ #
    # 指纹冷却
    # ------------------------------------------------------------------ #

    def is_backed_off(self, key: str, fingerprint: str, at_now: Optional[float] = None) -> bool:
        """同一指纹在冷却期内返回 True（对应 isBackedOff）。"""
        current = at_now if at_now is not None else time.time()
        entry = self._backoffs.get(key)
        if not entry or entry.get("fingerprint") != fingerprint or current >= entry.get("until", 0):
            if entry and current >= entry.get("until", 0):
                self._backoffs.pop(key, None)
            return False
        return True

    def note_backoff(self, key: str, fingerprint: str, until: float) -> None:
        """记录冷却（对应 noteBackoff）。"""
        self._backoffs[key] = {"fingerprint": fingerprint, "until": float(until)}

    def clear_backoff(self, key: Optional[str] = None) -> None:
        if key:
            self._backoffs.pop(key, None)
        else:
            self._backoffs.clear()
