"""私聊回合状态机：短时合并、请求取代与首条提交边界。

对应上游 `src/turn-engine.ts`（逐行移植）。

- 同一关系分支的连续消息在 `userMessageDebounceSeconds` 内合并；
- 主模型尚未提交第一条可见回复时，新消息会废弃在途请求，并把
  旧、新消息合并后重新写作；
- 第一条回复已经提交后，新消息只取消尚未发送的 `<sep/>` 分段
  （由服务层处理），请求本身不再作废；
- `narrating` 保证同一故事同一时间只有一个写作回合。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional, Set


class Turn:
    """一个参与者的缓冲回合（对应 turn-engine 里的 turn 对象）。"""

    __slots__ = (
        "storyId",
        "participantId",
        "messages",
        "nextRevision",
        "obsoleteRequestIds",
        "discardedRequestIds",
        "sources",
        "latestEvent",
        "timer",
        "inFlightRequestId",
        "firstMessageCommittedRequestId",
        "activeSources",
        "activeBatchEndpointIds",
        "activeBatchMessageCount",
    )

    def __init__(self, story_id: str, participant_id: str) -> None:
        self.storyId = story_id
        self.participantId = participant_id
        self.messages: List[Dict[str, Any]] = []
        self.nextRevision = 0
        self.obsoleteRequestIds: Set[int] = set()
        self.discardedRequestIds: Set[int] = set()
        self.sources: List[Dict[str, Any]] = []
        self.latestEvent: Any = None
        self.timer: Optional[asyncio.TimerHandle] = None
        self.inFlightRequestId: Optional[int] = None
        self.firstMessageCommittedRequestId: Optional[int] = None
        self.activeSources: Optional[List[Dict[str, Any]]] = None
        self.activeBatchEndpointIds: Optional[List[str]] = None
        self.activeBatchMessageCount: Optional[int] = None


def should_supersede_request(
    in_flight_request_id: Optional[int],
    first_message_committed_request_id: Optional[int],
    obsolete_request_ids: Set[int],
) -> bool:
    """对应 shouldSupersedeRequest：在途请求存在、首条未提交、且尚未作废。"""
    return (
        in_flight_request_id is not None
        and in_flight_request_id != first_message_committed_request_id
        and in_flight_request_id not in obsolete_request_ids
    )


class TurnEngine:
    """回合容器集合（对应 createTurnEngine 的对外接口）。"""

    def __init__(
        self,
        user_message_debounce_seconds: Callable[[], float],
        schedule_flush: Callable[[str, int], None],
        report: Callable[..., None],
    ) -> None:
        self._turns: Dict[str, Turn] = {}
        self._narrating: Set[str] = set()
        self._debounce = user_message_debounce_seconds
        self._schedule_flush = schedule_flush
        self._report = report

    # ------------------------------------------------------------------ #
    # 缓冲与取代
    # ------------------------------------------------------------------ #

    def buffer_user_narrative(
        self,
        story_id: str,
        participant_id: str,
        message: Dict[str, Any],
        event: Any = None,
        endpoint_id: str = "",
    ) -> None:
        """把一条入站用户消息放进合并缓冲，并重置合并计时器。"""
        key = participant_id
        turn = self._turns.get(key)
        if turn is None:
            turn = Turn(story_id, participant_id)
            self._turns[key] = turn
        if should_supersede_request(
            turn.inFlightRequestId, turn.firstMessageCommittedRequestId, turn.obsoleteRequestIds
        ):
            turn.obsoleteRequestIds.add(turn.inFlightRequestId)  # type: ignore[arg-type]
            self._report(
                "standard",
                "info",
                story_id,
                "新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%s",
                participant_id,
                turn.inFlightRequestId,
            )
        message = dict(message)
        if endpoint_id:
            message.setdefault("endpointId", endpoint_id)
        turn.messages.append(message)
        turn.latestEvent = event
        if turn.timer is not None:
            turn.timer.cancel()
        turn.nextRevision += 1
        revision = turn.nextRevision
        delay = max(0.0, float(self._debounce() or 0)) * 1000

        def _fire() -> None:
            self._schedule_flush(key, revision)

        loop = asyncio.get_event_loop()
        turn.timer = loop.call_later(delay / 1000.0, _fire)
        self._report("diagnostic", "debug", story_id, "短时消息合并 参与者=%s 待处理=%d 等待=%dms", participant_id, len(turn.messages), delay)

    def signal_incoming_interruption(self, story_id: str, participant_id: str) -> None:
        """入站消息对在途请求发出中断信号（不动缓冲，仅作废请求）。"""
        turn = self._turns.get(participant_id)
        if turn is None:
            return
        if not should_supersede_request(
            turn.inFlightRequestId, turn.firstMessageCommittedRequestId, turn.obsoleteRequestIds
        ):
            return
        turn.obsoleteRequestIds.add(turn.inFlightRequestId)  # type: ignore[arg-type]
        self._report(
            "standard",
            "info",
            story_id,
            "新消息到达且首条回复尚未提交，放弃旧请求 参与者=%s 请求=%s",
            participant_id,
            turn.inFlightRequestId,
        )

    def has_pending_narrative(self, story_id: str, has_pending_group_turn: Callable[[str], bool]) -> bool:
        """故事是否有未完成的写作（对应 hasPendingNarrative）。"""
        if story_id in self._narrating:
            return True
        for turn in self._turns.values():
            if turn.storyId == story_id and (turn.messages or turn.timer or turn.inFlightRequestId):
                return True
        return bool(has_pending_group_turn(story_id))

    # ------------------------------------------------------------------ #
    # 刷写（flush）
    # ------------------------------------------------------------------ #

    def begin_flush(self, key: str, revision: int) -> Optional[Turn]:
        """申请开始写作；narrating 冲突时返回 None（调用方稍后重试）。"""
        turn = self._turns.get(key)
        if turn is None or turn.nextRevision != revision:
            return None
        if turn.storyId in self._narrating:
            # 同一故事已有写作在途：250ms 后按同一 revision 重试（对应上游 retryDelay）。
            if turn.timer is not None:
                turn.timer.cancel()
            loop = asyncio.get_event_loop()
            turn.timer = loop.call_later(
                0.25, lambda: self._schedule_flush(key, revision)
            )
            return None
        self._narrating.add(turn.storyId)
        if turn.timer is not None:
            turn.timer.cancel()
        turn.timer = None
        turn.activeSources = turn.sources
        turn.sources = []
        return turn

    def end_flush(self, key: str, request_id: int) -> None:
        """写作结束：释放 narrating；若缓冲里又进了新消息，重新排一次刷写。"""
        turn = self._turns.get(key)
        if turn is None:
            return
        if turn.inFlightRequestId == request_id:
            turn.inFlightRequestId = None
            turn.firstMessageCommittedRequestId = None
            turn.activeSources = None
            turn.activeBatchEndpointIds = None
            turn.activeBatchMessageCount = None
            self._narrating.discard(turn.storyId)
        turn.obsoleteRequestIds.discard(request_id)
        turn.discardedRequestIds.discard(request_id)
        if not turn.messages and not turn.timer and turn.inFlightRequestId is None:
            self._turns.pop(key, None)
        elif turn.messages and not turn.timer and turn.inFlightRequestId is None:
            turn.nextRevision += 1
            revision = turn.nextRevision
            delay = max(0.0, float(self._debounce() or 0))
            loop = asyncio.get_event_loop()
            turn.timer = loop.call_later(delay, lambda: self._schedule_flush(key, revision))

    def reschedule_timers(self) -> None:
        """重载/恢复后把滞留缓冲重新排上（对应 rescheduleTimers）。"""
        loop = asyncio.get_event_loop()
        for key, turn in self._turns.items():
            if turn.timer is None and turn.inFlightRequestId is None and turn.messages:
                turn.nextRevision += 1
                revision = turn.nextRevision
                turn.timer = loop.call_later(0.0, lambda k=key, r=revision: self._schedule_flush(k, r))

    def invalidate_narratives(self, story_id: Optional[str] = None) -> None:
        """管理操作（purge/reset）清空缓冲：在途请求标记为丢弃，不会再入队。"""
        for key in list(self._turns.keys()):
            turn = self._turns[key]
            if story_id and turn.storyId != story_id:
                continue
            if turn.timer is not None:
                turn.timer.cancel()
            if turn.inFlightRequestId is not None:
                turn.obsoleteRequestIds.add(turn.inFlightRequestId)
                turn.discardedRequestIds.add(turn.inFlightRequestId)
                turn.messages.clear()
                turn.timer = None
            else:
                self._turns.pop(key, None)

    def record_source(self, key: str, endpoint_id: str, received_seq: int) -> None:
        """记录本回合来源端点（多端点标注用）。"""
        turn = self._turns.get(key)
        if turn is None or not endpoint_id:
            return
        if any(source.get("endpointId") == endpoint_id for source in turn.sources):
            return
        turn.sources.append({"endpointId": endpoint_id, "receivedSeq": received_seq})
        turn.sources.sort(key=lambda source: source.get("receivedSeq", 0))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def is_narrating(self, story_id: str) -> bool:
        return story_id in self._narrating

    def get_turn(self, key: str) -> Optional[Turn]:
        return self._turns.get(key)

    def pending_turn_count(self) -> int:
        return len(self._turns)


def format_buffered_user_messages(batch: List[Dict[str, Any]], separator: str = "\n") -> str:
    """把合并批次拼成模型可读的当前事件文本（对应 formatBufferedUserMessages）。

    上游对多消息批次会保留原文顺序；这里保持同样行为，图片与语音
    以占位事实出现（上游同样以 imageCount/audioCount 传递）。
    """
    parts: List[str] = []
    for message in batch:
        content = str(message.get("content") or "").strip()
        images = message.get("imageSources") or []
        audios = message.get("audioSources") or []
        suffix = ""
        if images:
            suffix += f"（附图 {len(images)} 张）"
        if audios:
            suffix += "（语音）"
        if content or suffix:
            parts.append((content + suffix).strip())
    return separator.join(part for part in parts if part)


__all__ = [
    "Turn",
    "TurnEngine",
    "should_supersede_request",
    "format_buffered_user_messages",
]
