"""astrbot.api.event 桩：filter、AstrMessageEvent、MessageChain。"""
from __future__ import annotations

from enum import Enum
from typing import Any


class EventMessageType(Enum):
    ALL = "all"
    PRIVATE_MESSAGE = "private"
    GROUP_MESSAGE = "group"


class PlatformAdapterType(Enum):
    ALL = "all"
    AIOCQHTTP = "aiocqhttp"


class PermissionType(Enum):
    ADMIN = "admin"


class MessageChain:
    """消息链桩：保存组件列表。"""

    def __init__(self, components: Any = None):
        self.chain = list(components or [])

    def message(self, text: str) -> "MessageChain":
        from .message_components import Plain

        self.chain.append(Plain(text))
        return self


class _FilterDecorators:
    """装饰器桩：直接返回原函数（本地测试不经过真实派发）。"""

    @staticmethod
    def event_message_type(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def command(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def command_group(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def platform_adapter_type(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def permission_type(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def __getattr__(self, name: str):
        def decorator_holder(*args, **kwargs):
            def decorator(func=None):
                return func

            return decorator

        return decorator_holder


filter = _FilterDecorators()
filter.EventMessageType = EventMessageType
filter.PlatformAdapterType = PlatformAdapterType
filter.PermissionType = PermissionType


class _MessageObject:
    def __init__(self, message_id: str = "m1", group_id: str = ""):
        self.message_id = message_id
        self.group_id = group_id
        self.self_id = ""
        self.message = []


class AstrMessageEvent:
    """AstrMessageEvent 桩：接口与宿主一致的最小子集。"""

    def __init__(
        self,
        message_str: str = "",
        sender_id: str = "10001",
        sender_name: str = "小林",
        self_id: str = "90001",
        group_id: str = "",
        umo: str = "onebot:FriendMessage:10001",
        platform: str = "aiocqhttp",
        components: Any = None,
    ):
        self.message_str = message_str
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self.unified_msg_origin = umo
        self._platform = platform
        self.message_obj = _MessageObject(group_id=group_id)
        self._components = list(components or [])
        self._stopped = False
        self.is_at_or_wake_command = False
        self.sent: list[Any] = []

    def get_platform_name(self) -> str:
        return self._platform

    def get_self_id(self) -> str:
        return self._self_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_group_id(self) -> str:
        return self.message_obj.group_id

    def is_private_chat(self) -> bool:
        return not self.message_obj.group_id

    def get_messages(self):
        return self._components

    def plain_result(self, text: str) -> Any:
        return {"type": "plain", "text": text}

    def stop_event(self) -> None:
        self._stopped = True

    async def send(self, result: Any) -> None:
        self.sent.append(result)
