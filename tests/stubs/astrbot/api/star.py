"""astrbot.api.star 桩：Context / Star / StarTools / register。"""
from __future__ import annotations

import os
import tempfile
from typing import Any


class Context:
    """Context 桩：send_message 记录发送，可注入平台实例。"""

    def __init__(self):
        self.sent: list[tuple[str, Any]] = []
        self.platform_inst = None

    async def send_message(self, umo: str, chain: Any) -> bool:
        self.sent.append((umo, chain))
        return True

    def get_platform_inst(self, platform_id: str):
        return self.platform_inst


class Star:
    def __init__(self, context: Context):
        self.context = context


class StarTools:
    _data_root = tempfile.mkdtemp(prefix="hdsi-star-tools-")

    @classmethod
    def get_data_dir(cls, plugin_name: str = "") -> str:
        path = os.path.join(cls._data_root, plugin_name)
        os.makedirs(path, exist_ok=True)
        return path


def register(*args, **kwargs):
    def decorator(cls):
        return cls

    return decorator
