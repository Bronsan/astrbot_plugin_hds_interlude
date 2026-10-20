"""astrbot.api.web 桩：插件页面请求上下文与响应信封。"""
from __future__ import annotations

from typing import Any, Dict


class _Request:
    """模拟 AstrBot 的请求上下文。

    测试通过 `request.payload` 注入请求体；生产环境由宿主提供真实实现。
    """

    def __init__(self) -> None:
        self.payload: Any = {}

    async def json(self, default: Any = None) -> Any:
        return self.payload if self.payload is not None else default


request = _Request()


def json_response(data: Any = None, **_: Any) -> Dict[str, Any]:
    return {"status": "ok", "data": data}


def error_response(message: str = "", status_code: int = 400, **_: Any) -> Dict[str, Any]:
    return {"status": "error", "message": message, "statusCode": status_code}
