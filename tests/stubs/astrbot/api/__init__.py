"""astrbot.api 包桩：提供 logger。"""
from __future__ import annotations

import logging

_logger = logging.getLogger("hdsi-stub")


class _Logger:
    def info(self, message: str, *args) -> None:
        _logger.info(message, *args)

    def warning(self, message: str, *args) -> None:
        _logger.warning(message, *args)

    def error(self, message: str, *args) -> None:
        _logger.error(message, *args)

    def debug(self, message: str, *args) -> None:
        _logger.debug(message, *args)


logger = _Logger()
