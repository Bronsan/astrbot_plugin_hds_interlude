"""astrbot.api.message_components 桩。"""
from __future__ import annotations


class Plain:
    def __init__(self, text: str = ""):
        self.text = text


class At:
    def __init__(self, qq: str = ""):
        self.qq = str(qq)


class Image:
    def __init__(self, file: str = "", url: str = ""):
        self.file = file
        self.url = url


class Record:
    def __init__(self, file: str = ""):
        self.file = file


class Reply:
    def __init__(self, sender_id: str = "", message_str: str = "", qq: str = ""):
        self.sender_id = sender_id
        self.message_str = message_str
        self.qq = qq


class Face:
    def __init__(self, id: int = 0):
        self.id = id


class Node:
    def __init__(self, uin: str = "", name: str = "", content=None):
        self.uin = uin
        self.name = name
        self.content = content or []
