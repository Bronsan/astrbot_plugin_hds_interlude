"""HDS Interlude / 幕间系统 · AstrBot 插件入口。

上游为 Koishi 插件 `koishi-plugin-hds-interlude`（MomoiCore，AGPL-3.0）。
本文件对应上游 `src/index.ts`：命令注册、消息中间件、权限判定、失明模式
与生命周期管理；叙事核心全部经 `self.service`（`core/service.py`）转发。

命令名保持上游点分形式（`interlude.status` 等）。AstrBot 命令系统对
点号命令名原生支持，同时也支持带唤醒前缀的写法（`/interlude.status`）；
两种写法共用同一批处理器，缺省前缀时经消息中间件派发。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Face, Image, Plain, Record, Reply

try:  # pragma: no cover - 旧版 AstrBot 可能没有合并转发组件
    from astrbot.api.message_components import Forward, Node, Nodes
except ImportError:  # pragma: no cover
    Forward = Node = Nodes = ()  # type: ignore[assignment]
from astrbot.api.star import Context, Star, StarTools

try:  # pragma: no cover - 兼容不同版本的注册装饰器
    from astrbot.api.star import register
except ImportError:  # pragma: no cover
    def register(*args: Any, **kwargs: Any):  # type: ignore[misc]
        def decorator(cls):
            return cls

        return decorator

from .core.content_store import ContentStore
from .core.service import (
    InterludeService,
    SessionView,
    is_interlude_command_like,
    normalize_interlude_spacing,
)
from .core.studio_api import StudioAPI, StudioError
from .core.share_sync import (
    build_config_patch,
    default_share_base,
    extract_share_code,
    fetch_share,
    format_diff_lines,
    summarize_share,
)
from .core.timeutil import format_log_time, format_story_display_time
from .core.types import DeliveryUnknown, now_utc, to_iso

PLUGIN_NAME = "astrbot_plugin_hds_interlude"
PLUGIN_VERSION = "0.3.2"

#: 单次平台投递的回执等待上限（对应上游 withTransportTimeout 的 30 秒）。
TRANSPORT_TIMEOUT_SECONDS = 30

#: 回执超时类错误的判定关键字（平台异常类名或错误文本）。
_UNKNOWN_DELIVERY_HINTS = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "超时",
    "bridge closed",
    "bridge 已关闭",
)


def _is_unknown_delivery_error(error: BaseException) -> bool:
    """判断异常是否表示“请求已写出但结果未知”（对应 transportTimeout）。"""
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return True
    if "timeout" in type(error).__name__.lower():
        return True
    text = str(error).lower()
    return any(hint in text for hint in _UNKNOWN_DELIVERY_HINTS)

#: 确认等待时长（对应上游 askConfirmation 的 60 秒）。
CONFIRMATION_TIMEOUT_SECONDS = 60
_CONFIRMATION_YES = re.compile(r"^(?:y|yes)$", re.IGNORECASE)

#: 上游逐字回执文案。
NO_MANAGER = "当前 QQ 没有共享主剧本的管理权限。"
NO_MANAGER_DETAIL = (
    "当前 QQ 没有共享主剧本的管理权限。"
    "请在「插件配置 → 共享主剧本 → 管理员账号」中添加此 QQ，或留空允许所有获授权账号。"
)
NO_ADMIN = "无权限：当前账号不是 HDSI 管理员。"
CANCELLED = "操作已取消。"


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def _decode_silk_to_wav(data: bytes) -> Optional[bytes]:
    """SILK → WAV（AstrBot 自带 silk-python；不可用时返回 None）。"""
    if not data:
        return None
    try:
        import wave

        import pysilk
    except ImportError:
        return None
    try:
        pcm = io.BytesIO()
        pysilk.decode(io.BytesIO(data), pcm, 24000)
        pcm.seek(0)
        wav = io.BytesIO()
        with wave.open(wav, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(pcm.read())
        return wav.getvalue()
    except Exception:  # noqa: BLE001 - 解码失败交给调用方降级
        return None


def _is_silk(data: bytes, extension: str = "") -> bool:
    if extension.lower().lstrip(".") == "silk":
        return True
    return bool(data[:10].startswith(b"#!SILK"))


def _resize_image_bytes(data: bytes, mime: str, max_dimension: int) -> Tuple[bytes, str]:
    """按 `vision.maxImageDimension` 缩放图片（Pillow 缺失时原样返回）。"""
    if max_dimension <= 0 or not data:
        return data, mime
    try:
        from PIL import Image as PILImage
    except ImportError:  # pragma: no cover - AstrBot 默认自带 Pillow
        return data, mime
    try:
        image = PILImage.open(io.BytesIO(data))
        image.load()
        if max(image.size) <= max_dimension:
            return data, mime
        image.thumbnail((max_dimension, max_dimension))
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            background = PILImage.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - 缩放失败不影响原图送入模型
        return data, mime


def _component_text(component: Any) -> str:
    """把单个消息组件压成可读文本（合并转发展开用）。"""
    if isinstance(component, Plain):
        return _text(getattr(component, "text", ""))
    if isinstance(component, Image):
        return "[图片]"
    if isinstance(component, Record):
        return "[语音]"
    if isinstance(component, At):
        return f"@{_text(getattr(component, 'qq', ''))}"
    return ""


def _render_forward_nodes(
    nodes: List[Any], depth: int, max_depth: int, counter: List[int], max_nodes: int
) -> List[str]:
    """递归展开合并转发节点（限制层数、节点数；图片/语音只留占位）。"""
    lines: List[str] = []
    for node in nodes:
        if counter[0] >= max_nodes:
            break
        counter[0] += 1
        name = _text(getattr(node, "name", "") or getattr(node, "uin", ""))
        parts: List[str] = []
        for child in getattr(node, "content", None) or []:
            if Node and isinstance(child, Node):
                if depth + 1 <= max_depth:
                    parts.extend(_render_forward_nodes([child], depth + 1, max_depth, counter, max_nodes))
                continue
            if Nodes and isinstance(child, Nodes):
                if depth + 1 <= max_depth:
                    parts.extend(_render_forward_nodes(
                        list(getattr(child, "nodes", None) or []), depth + 1, max_depth, counter, max_nodes
                    ))
                continue
            parts.append(_component_text(child))
        text = "".join(parts).strip()
        if text:
            lines.append(f"{name}: {text}" if name else text)
    return lines


def _onebot_segment_text(segment: Any) -> str:
    """把 OneBot 消息段压成文本（`get_forward_msg` 返回值用）。"""
    if not isinstance(segment, dict):
        return ""
    kind = str(segment.get("type") or "")
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    if kind == "text":
        return _text(data.get("text"))
    if kind == "image":
        return "[图片]"
    if kind == "record":
        return "[语音]"
    if kind == "face":
        return "[表情]"
    if kind == "at":
        return f"@{_text(data.get('qq'))}"
    if kind in ("forward", "node"):
        return "[嵌套转发]"
    return ""


def _render_forward_payload(payload: Any, max_nodes: int, max_characters: int) -> str:
    """解析 OneBot `get_forward_msg` 返回（兼容 data/messages 两种包装）。"""
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    messages = data.get("messages") or data.get("message") or []
    if not isinstance(messages, list):
        return ""
    lines: List[str] = []
    for item in messages[:max_nodes]:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        name = _text(sender.get("card") or sender.get("nickname") or sender.get("user_id") or "")
        content = item.get("message") or item.get("content") or []
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(_onebot_segment_text(segment) for segment in content)
        else:
            text = ""
        text = text.strip()
        if text:
            lines.append(f"{name}: {text}" if name else text)
    return "\n".join(lines)[:max_characters]


async def _extract_forward_text(
    messages: List[Any], config: Dict[str, Any], platform_client: Any = None
) -> str:
    """读取 QQ 合并转发的正文（对应 runtime.forwardMessage）。

    - 适配器已展开为 `Nodes`/`Node`：递归展开并遵守节点/层数预算；
    - 只有 `Forward` 引用：通过平台客户端 `get_forward_msg` 二次拉取；
    - 平台不支持或拉取失败：保留可读占位，不丢事件。
    """
    if not config.get("enabled", True):
        return ""
    max_nodes = max(1, int(config.get("maxNodes") or 30))
    max_depth = max(1, int(config.get("maxDepth") or 3))
    max_characters = max(1, int(config.get("maxCharacters") or 2000))
    counter = [0]
    lines: List[str] = []
    for component in messages:
        if Nodes and isinstance(component, Nodes):
            lines.extend(_render_forward_nodes(
                list(getattr(component, "nodes", None) or []), 0, max_depth, counter, max_nodes
            ))
        elif Node and isinstance(component, Node):
            lines.extend(_render_forward_nodes([component], 0, max_depth, counter, max_nodes))
        elif Forward and isinstance(component, Forward):
            rendered = ""
            if platform_client is not None and hasattr(platform_client, "call_action"):
                try:
                    payload = await asyncio.wait_for(
                        platform_client.call_action(
                            "get_forward_msg", message_id=_text(getattr(component, "id", ""))
                        ),
                        timeout=10,
                    )
                    rendered = _render_forward_payload(
                        payload, max_nodes - counter[0], max_characters
                    )
                except Exception as error:  # noqa: BLE001 - 拉取失败保留占位
                    astrbot_logger.debug(f"{PLUGIN_NAME}: 合并转发拉取失败: {error}")
            lines.append(rendered or "[合并转发：当前适配器未展开内容]")
    return "\n".join(lines)[:max_characters]


class PluginTransport:
    """平台层投递适配：把服务层的发送/取媒体需求映射到 AstrBot API。"""

    def __init__(self, plugin: "HDSInterludePlugin") -> None:
        self._plugin = plugin

    async def send(self, umo: str, text: str, reply_to: str = "") -> bool:
        """主动/延迟投递一条文本消息。

        返回 False 表示确定失败；回执超时/桥接关闭抛 `DeliveryUnknown`，
        由服务层把账本记为 pending 且不自动重试（对应上游 rc53/rc56）。
        `reply_to` 非空时附带 `Reply` 组件（对应 typ-0 的 replyTo 透传）。
        """
        if not umo or not text:
            return False
        components: List[Any] = []
        if reply_to:
            try:
                components.append(Reply(id=reply_to))
            except (TypeError, ValueError):
                # 旧版组件只接受 sender/qq 字段：宁可退回纯文本，也不丢消息。
                components = []
        components.append(Plain(text))
        return await self._send_chain(umo, MessageChain(components))

    async def send_face(self, umo: str, face_id: str) -> bool:
        """发送 QQ 原生小表情（`chatActions.nativeFaces`）。

        `face_id` 由服务层的语义映射表给出；组件要求整数 id。
        """
        if not umo or not str(face_id).strip():
            return False
        try:
            component = Face(id=int(face_id))
        except (TypeError, ValueError):
            return False
        return await self._send_chain(umo, MessageChain([component]))

    async def send_media(self, umo: str, path: str, kind: str = "image") -> bool:
        """发送本地表情包/语音（`stickers.directory` 中的真实文件）。"""
        if not umo or not path or not os.path.isfile(path):
            return False
        if kind == "audio":
            try:
                component = Record.fromFileSystem(path)
            except AttributeError:  # pragma: no cover - 旧版 AstrBot 无该工厂方法
                component = Record(file=path)
        else:
            try:
                component = Image.fromFileSystem(path)
            except AttributeError:  # pragma: no cover
                component = Image(file=path)
        return await self._send_chain(umo, MessageChain([component]))

    def _client_for_umo(self, umo: str) -> Any:
        """按 `unified_msg_origin` 找平台客户端（OneBot 的 CQHttp 实例）。"""
        try:
            platform_id = str(umo).split(":", 1)[0]
            platform = self._plugin.context.get_platform_inst(platform_id)
        except Exception:  # noqa: BLE001 - 非 OneBot 平台没有该接口
            return None
        if platform is None:
            return None
        getter = getattr(platform, "get_client", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001
                return None
        return getattr(platform, "bot", None)

    async def send_reaction(self, umo: str, message_id: str, emoji_id: str) -> bool:
        """给指定消息加 QQ 表情回应（OneBot `set_msg_emoji_like`）。"""
        if not umo or not message_id or not emoji_id:
            return False
        client = self._client_for_umo(umo)
        if client is None or not hasattr(client, "call_action"):
            return False
        try:
            result = await asyncio.wait_for(
                client.call_action(
                    "set_msg_emoji_like",
                    message_id=str(message_id),
                    emoji_id=str(emoji_id),
                    set=True,
                ),
                timeout=TRANSPORT_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 动作失败由账本记录
            if _is_unknown_delivery_error(error):
                raise DeliveryUnknown(f"transport-timeout: {error}") from error
            astrbot_logger.debug(f"{PLUGIN_NAME}: 消息回应失败: {error}")
            return False
        if isinstance(result, dict) and result.get("retcode") not in (None, 0):
            astrbot_logger.debug(f"{PLUGIN_NAME}: 消息回应被平台拒绝: {result}")
            return False
        return True

    async def _send_chain(self, umo: str, chain: MessageChain) -> bool:
        """统一的平台发送：30 秒回执上限 + unknown 语义。"""
        try:
            return bool(await asyncio.wait_for(
                self._plugin.context.send_message(umo, chain),
                timeout=TRANSPORT_TIMEOUT_SECONDS,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 投递失败由账本记录
            if _is_unknown_delivery_error(error):
                astrbot_logger.warning(f"{PLUGIN_NAME}: 发送消息结果未知（可能已送达）: {error}")
                raise DeliveryUnknown(f"transport-timeout: {error}") from error
            astrbot_logger.warning(f"{PLUGIN_NAME}: 发送消息失败: {error}")
            return False

    def _vision_max_dimension(self) -> int:
        try:
            vision = (self._plugin._config_section("model") or {}).get("vision") or {}
            return int(vision.get("maxImageDimension") or 0)
        except Exception:  # noqa: BLE001
            return 0

    async def fetch_image_data_uri(self, source: str) -> Optional[Tuple[str, str]]:
        """把图片引用（URL / 本地路径 / base64）转换为 data URI。

        图片二进制只用于本次多模态请求，绝不写入数据库（与上游一致）。
        """
        if not source:
            return None
        try:
            if source.startswith("data:"):
                header, _, payload = source.partition(",")
                mime = header[5:].split(";")[0] or "image/jpeg"
                data, mime = _resize_image_bytes(base64.b64decode(payload), mime, self._vision_max_dimension())
                return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", mime
            if source.startswith("http://") or source.startswith("https://"):
                import httpx

                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    response = await client.get(source)
                if response.status_code >= 400 or not response.content:
                    return None
                mime = response.headers.get("content-type", "image/jpeg").split(";")[0]
                data, mime = _resize_image_bytes(response.content, mime, self._vision_max_dimension())
                return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", mime
            if source.startswith("file://"):
                source = source[len("file://"):]
            if os.path.isfile(source):
                mime = mimetypes.guess_type(source)[0] or "image/jpeg"
                with open(source, "rb") as handle:
                    data, mime = _resize_image_bytes(handle.read(), mime, self._vision_max_dimension())
                return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", mime
        except Exception as error:  # noqa: BLE001
            astrbot_logger.debug(f"{PLUGIN_NAME}: 图片读取失败 {source}: {error}")
        return None

    async def fetch_audio_base64(self, source: str) -> Optional[Tuple[str, str]]:
        """读取语音文件为 base64（本地路径或 http 均可；失败返回 None）。"""
        if not source:
            return None
        try:
            if source.startswith("http://") or source.startswith("https://"):
                import httpx

                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    response = await client.get(source)
                if response.status_code >= 400 or not response.content:
                    return None
                mime = response.headers.get("content-type", "audio/mpeg").split(";")[0]
                data = response.content
                if _is_silk(data):
                    wav = _decode_silk_to_wav(data)
                    if wav:
                        return base64.b64encode(wav).decode("ascii"), "wav"
                fmt = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg"}.get(mime, "mp3")
                return base64.b64encode(data).decode("ascii"), fmt
            if source.startswith("file://"):
                source = source[len("file://"):]
            if os.path.isfile(source):
                extension = os.path.splitext(source)[1].lower().lstrip(".") or "mp3"
                with open(source, "rb") as handle:
                    data = handle.read()
                if _is_silk(data, extension):
                    wav = _decode_silk_to_wav(data)
                    if wav:
                        return base64.b64encode(wav).decode("ascii"), "wav"
                fmt = extension if extension in ("mp3", "wav", "ogg", "m4a", "flac", "amr") else "mp3"
                return base64.b64encode(data).decode("ascii"), fmt
        except Exception as error:  # noqa: BLE001
            astrbot_logger.debug(f"{PLUGIN_NAME}: 语音读取失败 {source}: {error}")
        return None

    def qzone_available(self) -> bool:
        """QQ 空间通道需要上游 SnowLuma 扩展动作；移植环境默认不可用。"""
        return False

    async def qzone_execute(self, story: Any, action: str, payload: Dict[str, Any], self_id: str = "") -> Dict[str, Any]:
        return {"ok": False, "error": "当前平台层未实现 QQ 空间动作（需要 SnowLuma 兼容连接）"}


@register(PLUGIN_NAME, "hdsi-port", "时间感知的持续叙事聊天框架（HDS Interlude）AstrBot 移植版", PLUGIN_VERSION, "https://gitee.com/MomoiCore/HDS-Interlude")
class HDSInterludePlugin(Star):
    """HDS Interlude 的 AstrBot 插件（对应上游 src/index.ts 的 apply 入口）。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._blind = bool((self._config_section("blindMode") or {}).get("enabled"))
        data_dir = self._resolve_data_dir()
        self.content_store = ContentStore(data_dir, logger=self._log)
        self.transport = PluginTransport(self)
        self.service = InterludeService(
            config_getter=self._effective_config,
            data_dir=data_dir,
            logger=self._log,
            transport=self.transport,
            content_store=self.content_store,
        )
        self.studio_api = StudioAPI(
            self.content_store,
            story_lister=self._studio_story_list,
            story_applier=self._studio_apply_story,
            default_base=self._type0_share_base(),
            logger=self._log,
        )
        self._confirmations: Dict[str, asyncio.Future] = {}
        self._background_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._started_at = time.time()
        self._type0_task: Optional[asyncio.Task] = None
        if not self._blind:
            astrbot_logger.info(f"{PLUGIN_NAME}: 插件加载完成 {PLUGIN_VERSION}")
        self._register_commands()
        self._register_studio_web_api()
        self._background_task = asyncio.get_event_loop().create_task(self._background())
        self._type0_task = asyncio.get_event_loop().create_task(self._auto_import_type0())
        if self._blind:
            self._heartbeat_task = asyncio.get_event_loop().create_task(self._blind_heartbeat())

    # ------------------------------------------------------------------ #
    # 配置与生命周期
    # ------------------------------------------------------------------ #

    def _config_view(self) -> Dict[str, Any]:
        """每次读取时构造普通 dict 视图（AstrBotConfig 兼容 dict）。"""
        try:
            return dict(self.config)
        except Exception:  # noqa: BLE001
            return {}

    def _config_section(self, name: str) -> Dict[str, Any]:
        section = self._config_view().get(name)
        return section if isinstance(section, dict) else {}

    def _effective_config(self) -> Dict[str, Any]:
        """WebUI 内容叠加后的配置视图（仅服务层使用）。

        角色身份、世界书、叙事提示词保存在 `content_store`；这里按每次读取
        叠加到 AstrBot 配置之上，保证 WebUI 修改无需重载插件即可生效。
        配置页原字段保留为「回退默认值」，也便于回滚。
        """
        raw = self._config_view()
        try:
            return self.content_store.apply_config_overrides(raw)
        except Exception as error:  # noqa: BLE001 - 内容层异常不能拖垮叙事
            self._log("warn", f"应用 WebUI 内容失败，回退配置值：{error}")
            return raw

    def _type0_share_base(self) -> str:
        sync = self._config_section("presetSync")
        base = str(sync.get("baseUrl") or "").strip()
        return base or "https://typ0.space"

    async def _studio_story_list(self) -> List[Dict[str, Any]]:
        try:
            return await self.service.list_story_summaries()
        except Exception as error:  # noqa: BLE001
            self._log("warn", f"WebUI 读取故事列表失败：{error}")
            return []

    async def _studio_apply_story(self, story_id: str, patch: Dict[str, Any]) -> None:
        story = await self.service.get_story(story_id)
        if story is None:
            raise StudioError("故事不存在", 404)
        await self.service.update_setting(story, patch)

    def _register_studio_web_api(self) -> None:
        """注册 WebUI「角色工作室」后端动作端点。

        使用 AstrBot 4.14+ 的插件页面 bridge；旧版本没有
        `register_web_api` 时只记录日志，不影响插件主体。
        """
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            self._log("info", "当前 AstrBot 不支持插件可视化视图，WebUI 未注册（插件功能不受影响）。")
            return
        try:
            from astrbot.api.web import error_response, json_response, request
        except Exception as error:  # noqa: BLE001 - 旧版没有 astrbot.api.web
            self._log("warn", f"插件页面 API 不可用：{error}")
            return

        async def studio_action():
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)
            action = str(payload.get("action") or "").strip()
            arguments = payload.get("payload")
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                data = await self.studio_api.dispatch(action, arguments)
            except StudioError as error:
                return error_response(error.message, status_code=error.status)
            except Exception as error:  # noqa: BLE001 - 统一转成可读错误
                self._log("error", f"WebUI 操作失败 action={action}：{error}")
                return error_response(f"操作失败：{error}", status_code=500)
            return json_response(data)

        try:
            register(
                f"/{PLUGIN_NAME}/studio/action",
                studio_action,
                ["POST"],
                "HDS Interlude Studio action",
            )
        except Exception as error:  # noqa: BLE001 - 路由冲突等
            self._log("warn", f"注册 WebUI 端点失败：{error}")

    def _resolve_data_dir(self) -> str:
        try:
            path = StarTools.get_data_dir(PLUGIN_NAME)
            return str(path)
        except Exception:  # noqa: BLE001 - 兜底到 data 目录约定
            fallback = os.path.join("data", "plugin_data", PLUGIN_NAME)
            os.makedirs(fallback, exist_ok=True)
            return fallback

    def _blind_now(self) -> bool:
        """失明模式当前开关：初始化快照 OR 实时配置（配置页改动即时生效）。"""
        return self._blind or bool((self._config_section("blindMode") or {}).get("enabled"))

    def _log(self, level: str, message: str) -> None:
        """日志出口：任何情况下都不得抛出（对应上游 rc55 的日志层防御）。

        `logging.level` 在这里生效：低于阈值的事件不会进入 AstrBot 日志。
        内容预览由 `logging.logScriptPreview/logMessageContent` 在服务层控制。
        """
        try:
            if self._blind_now() and level not in ("error",):
                return
            threshold = str(self._config_section("logging").get("level") or "info").lower()
            order = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40}
            if order.get(level, 20) < order.get(threshold, 20):
                return
            line = f"{PLUGIN_NAME}: {message}"
            if level == "error":
                astrbot_logger.error(line)
            elif level == "warn":
                astrbot_logger.warning(line)
            elif level == "debug":
                astrbot_logger.debug(line)
            else:
                astrbot_logger.info(line)
        except Exception:  # noqa: BLE001 - 日志失败不能拖垮回合
            return

    async def _background(self) -> None:
        try:
            await self.service.background_loop()
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001
            self._log("warn", f"后台循环退出：{error}")

    async def _auto_import_type0(self) -> None:
        """Type0 内置导入：插件加载时直连分享站拉取并应用。

        不再依赖 typ-0 桌面端或任何外部平台：只读 `presetSync` 配置，
        通过 HTTP 直接访问 `baseUrl`，按 revision 去重后写入本地配置。
        """
        sync = self._config_section("presetSync")
        if not sync.get("enabled") or not sync.get("autoUpdate"):
            return
        code = extract_share_code(sync.get("shareUrl") or sync.get("shareCode"))
        if not code:
            self._log("warn", "Type0 自动导入已开启，但分享码/链接为空")
            return
        try:
            # 给宿主留出加载余量，避免与其它插件的启动请求抢占。
            await asyncio.sleep(2)
            applied, reason = await self._run_preset_sync(None, None, force=False)
            self._log("info" if applied else "debug", f"Type0 自动导入：{reason}")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 导入失败不能拖垮插件
            self._log("warn", f"Type0 自动导入失败：{error}")

    async def _blind_heartbeat(self) -> None:
        """失明模式健康心跳（不包含故事与账户内容）。"""
        interval = max(1, int(self._config_section("blindMode").get("healthReportMinutes") or 10)) * 60
        while True:
            await asyncio.sleep(interval)
            uptime = int(time.time() - self._started_at)
            astrbot_logger.info(f"{PLUGIN_NAME}: 运行中 uptime={uptime}s（失明模式）")

    async def terminate(self):
        """插件卸载/停用时收尾（对应上游 dispose 钩子）。"""
        for task in (self._background_task, self._heartbeat_task, self._type0_task):
            if task:
                task.cancel()
        try:
            self.service.close()
        except Exception:  # noqa: BLE001
            pass
        self._log("info", "插件已卸载")

    # ------------------------------------------------------------------ #
    # 会话与事件工具
    # ------------------------------------------------------------------ #

    def _platform_name(self, event: AstrMessageEvent) -> str:
        raw = _text(event.get_platform_name()).lower()
        if raw in ("aiocqhttp", "napcat", "lagrange", "onebot"):
            return "onebot"
        return raw or "unknown"

    def _platform_client(self, event: AstrMessageEvent) -> Any:
        """取当前事件所对应平台适配器的 OneBot 客户端（不可用时返回 None）。"""
        try:
            platform_id = event.get_platform_id()
            platform = self.context.get_platform_inst(platform_id)
        except Exception:  # noqa: BLE001 - 旧版/非 OneBot 适配器没有该接口
            return None
        if platform is None:
            return None
        getter = getattr(platform, "get_client", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001
                return None
        return getattr(platform, "bot", None)

    async def _session(self, event: AstrMessageEvent) -> SessionView:
        images: List[str] = []
        audios: List[str] = []
        at_bot = False
        quote: Optional[Dict[str, str]] = None
        self_id = _text(event.get_self_id())
        messages = list(event.get_messages())
        for component in messages:
            if isinstance(component, Image):
                source = getattr(component, "url", None) or getattr(component, "file", None) or getattr(component, "path", None)
                if source:
                    images.append(str(source))
            elif isinstance(component, Record):
                source = getattr(component, "file", None) or getattr(component, "url", None) or getattr(component, "path", None)
                if source:
                    audios.append(str(source))
            elif isinstance(component, At):
                qq = str(getattr(component, "qq", "") or "")
                if qq in (self_id, "all"):
                    at_bot = qq == self_id or at_bot
            elif isinstance(component, Reply):
                sender = _text(getattr(component, "sender_id", "") or getattr(component, "qq", ""))
                content = _text(getattr(component, "message_str", "") or getattr(component, "text", ""))
                if content:
                    quote = {"senderId": sender, "senderName": sender, "content": content[:2000]}
        content = _text(event.message_str)
        forward_text = await _extract_forward_text(
            messages,
            self._config_section("runtime").get("forwardMessage") or {},
            platform_client=self._platform_client(event),
        )
        if forward_text:
            content = (
                f"{content}\n[合并转发]\n{forward_text}".strip()
                if content.strip()
                else f"[合并转发]\n{forward_text}"
            )
        return SessionView(
            platform=self._platform_name(event),
            selfId=self_id,
            userId=_text(event.get_sender_id()),
            channelId=_text(getattr(event, "unified_msg_origin", "")),
            isDirect=bool(event.is_private_chat()),
            content=content,
            senderName=_text(event.get_sender_name()),
            groupId=_text(event.get_group_id()),
            messageId=_text(getattr(event.message_obj, "message_id", "")),
            umo=_text(getattr(event, "unified_msg_origin", "")),
            imageSources=images,
            audioSources=audios,
            quote=quote,
            atBot=at_bot,
            rawEvent=event,
        )

    def _looks_like_interlude_command(self, content: str) -> bool:
        return is_interlude_command_like(content)

    # ---- y/n 确认（对应 askConfirmation） ----

    async def _ask_confirmation(self, event: AstrMessageEvent, message: str) -> bool:
        """先发提问，再用会话级 future 等下一个 y/n；60 秒无回复按取消处理。"""
        key = _text(getattr(event, "unified_msg_origin", ""))
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._confirmations[key] = future
        try:
            await event.send(event.plain_result(f"{message}\n请在 60 秒内回复 y 或 n。"))
            answer = await asyncio.wait_for(future, CONFIRMATION_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        finally:
            self._confirmations.pop(key, None)
        return bool(_CONFIRMATION_YES.match(_text(answer).strip()))

    def _resolve_confirmation(self, event: AstrMessageEvent) -> bool:
        """若该会话在等 y/n，消费这条文本并返回 True（只有真实文本才算回答）。"""
        key = _text(getattr(event, "unified_msg_origin", ""))
        future = self._confirmations.get(key)
        if future is None or future.done():
            return False
        answer = _text(event.message_str).strip()
        if not answer:
            return False
        future.set_result(answer)
        event.stop_event()
        return True

    # ------------------------------------------------------------------ #
    # 消息中间件（对应上游 ctx.middleware）
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊入站：确认问答 → 管理命令 → 叙事捕获。"""
        if self._resolve_confirmation(event):
            return
        raw = _text(event.message_str)
        if raw.strip() and self._looks_like_interlude_command(raw):
            result = await self._dispatch_command(event, raw)
            if result is not None:
                event.stop_event()
                if result:
                    yield event.plain_result(result)
                return
            if self._config_section("runtime").get("ignoreCommandMessages", True):
                return
        capture = self._config_section("runtime").get("captureDirectMessages", True)
        if not capture:
            return
        try:
            consumed = await self.service.receive(await self._session(event))
        except Exception as error:  # noqa: BLE001 - 私聊归属不能因异常漏给其它 Agent
            self._log("error", f"私聊事件处理失败，已吞掉事件：{error}")
            event.stop_event()
            return
        if consumed:
            # 叙事吞掉事件（沉默回合同样不交给其它处理器）
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊入站：命令派发 → 白名单与意愿门（对应 receiveGroup）。"""
        raw = _text(event.message_str)
        stripped = raw.strip()
        # 群内命令：@ 机器人后的 interlude.*（消息前缀里可能带 @ 片段）
        command_text = self._strip_group_command_prefix(stripped)
        if command_text is not None and self._looks_like_interlude_command(command_text):
            result = await self._dispatch_command(event, command_text)
            if result is not None:
                event.stop_event()
                yield event.plain_result(result)
                return
        try:
            consumed = await self.service.receive_group(await self._session(event))
        except Exception as error:  # noqa: BLE001
            self._log("warn", f"群聊事件处理失败：{error}")
            return
        if consumed:
            event.stop_event()

    @staticmethod
    def _strip_group_command_prefix(content: str) -> Optional[str]:
        """去掉群消息里的 @片段，留下可能的命令文本。"""
        if not content:
            return None
        text = re.sub(r"^(\[[^\]]*\]\s*|@\S+\s*)+", "", content).strip()
        if not text:
            return None
        return text

    # ------------------------------------------------------------------ #
    # 命令派发（46 条管理命令，名称与上游点分形式一致）
    # ------------------------------------------------------------------ #

    def _register_commands(self) -> None:
        """命令表：名称与上游点分形式一致；缺省唤醒前缀时经消息中间件派发。

        上游命令由 Koishi 命令解析器处理；移植版在监听器里做等价派发，
        并保留空格写法归一（`interlude memory facts` → `interlude.memory.facts`）。
        """
        self._commands: Dict[str, Any] = {
            "interlude": self._cmd_entry,
            "interlude.doctor": self._cmd_doctor,
            "interlude.story.start": self._cmd_story_start,
            "interlude.init": self._cmd_init,
            "interlude.setup": self._cmd_setup,
            "interlude.qzone": self._cmd_qzone,
            "interlude.story.alias": self._cmd_story_alias,
            "interlude.story.endpoint": self._cmd_story_endpoint,
            "interlude.reset": self._cmd_reset,
            "interlude.participant.link": self._cmd_participant_link,
            "interlude.participant.unlink": self._cmd_participant_unlink,
            "interlude.participant.endpoints": self._cmd_participant_endpoints,
            "interlude.status": self._cmd_status,
            "interlude.pause": self._cmd_pause,
            "interlude.resume": self._cmd_resume,
            "interlude.advance": self._cmd_advance,
            "interlude.timeline.rebase": self._cmd_timeline_rebase,
            "interlude.timeline": self._cmd_timeline,
            "interlude.memory": self._cmd_memory,
            "interlude.context": self._cmd_context,
            "interlude.compact": self._cmd_compact,
            "interlude.script": self._cmd_script,
            "interlude.script.note": self._cmd_script_note,
            "interlude.memory.facts": self._cmd_memory_facts,
            "interlude.memory.add": self._cmd_memory_add,
            "interlude.memory.forget": self._cmd_memory_forget,
            "interlude.memory.intents": self._cmd_memory_intents,
            "interlude.memory.cancel": self._cmd_memory_cancel,
            "interlude.memory.patches": self._cmd_memory_patches,
            "interlude.memory.apply": self._cmd_memory_apply,
            "interlude.memory.reject": self._cmd_memory_reject,
            "interlude.overlay.clear": self._cmd_overlay_clear,
            "interlude.overlay.status": self._cmd_overlay_status,
            "interlude.overlay.compact": self._cmd_overlay_compact,
            "interlude.schedule": self._cmd_schedule,
            "interlude.schedule.refresh": self._cmd_schedule_refresh,
            "interlude.schedule.rebuild": self._cmd_schedule_rebuild,
            "interlude.database.clear": self._cmd_database_clear,
            "interlude.purge.all": self._cmd_purge_all,
            "interlude.purge.platform": self._cmd_purge_platform,
            "interlude.purge.range": self._cmd_purge_range,
            "interlude.preset.check": self._cmd_preset_check,
            "interlude.preset.apply": self._cmd_preset_apply,
            "interlude.preset.status": self._cmd_preset_status,
            "interlude.preset.sync": self._cmd_preset_sync,
            "interlude.preset.rollback": self._cmd_preset_rollback,
        }

    async def _dispatch_command(self, event: AstrMessageEvent, content: str) -> Optional[str]:
        """解析并执行一条命令；返回输出文本（None = 未识别）。"""
        if self._blind_now():
            return ""  # 失明模式：命令静默屏蔽
        text = content.strip()
        # 归一：去掉唤醒前缀
        text = re.sub(r"^[!/.]?(?=[iI]nterlude)", "", text, count=1)
        # 空格写法 → 点分写法
        rewritten = normalize_interlude_spacing(text, lambda name: name in self._commands)
        if rewritten is not None:
            text = rewritten
        # 匹配最长命令名（interlude.memory.add 优先于 interlude.memory）
        candidates = sorted(self._commands.keys(), key=len, reverse=True)
        for name in candidates:
            if text == name or text.startswith(name + " "):
                args = text[len(name):].strip()
                handler = self._commands[name]
                session = await self._session(event)
                try:
                    return await handler(event, session, args)
                except Exception as error:  # noqa: BLE001 - 命令异常回显而不崩溃
                    self._log("error", f"命令 {name} 执行失败：{error}")
                    return f"命令执行失败：{error}"
        return None

    # ---- 权限与前置检查 ----

    def _require_manager(self, session: SessionView) -> bool:
        return self.service.can_manage_session(session)

    async def _require_story(self, session: SessionView):
        if not self.service.can_handle_session(session):
            return (
                "当前 QQ 账号未获 HDSI 互动授权。请在「插件配置 → QQ 接入」中"
                "检查机器人 QQ 号、用户 QQ 白名单和启用状态。"
            )
        story = await self.service.find_story(session)
        if story is None:
            return (
                "当前私聊还没有故事。请先完成插件配置，然后执行 interlude.doctor；"
                "手动启动请使用 interlude.story.start，或开启 runtime.autoCreate 后直接发送第一条私聊。"
            )
        return story

    # ------------------------------------------------------------------ #
    # 命令实现（与上游 registerCommands 逐条对应）
    # ------------------------------------------------------------------ #

    async def _cmd_entry(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        return "\n".join([
            "HDS Interlude 命令入口。子命令用点分书写,例如:",
            "· interlude.status —— 查看当前故事与模型状态",
            "· interlude.timeline 10 —— 查看近期剧本",
            "· interlude.memory 10 —— 查看相关记忆",
            "· interlude.context —— 查看运行上下文摘要",
            "管理命令: interlude.script.note / interlude.memory.apply / interlude.overlay.clear / interlude.preset.sync 等,",
            '完整清单见 README。(空格写法如 "interlude script note" 也能自动识别,但推荐点分)',
        ])

    async def _start_story_from_console(
        self, event: AstrMessageEvent, session: SessionView, legacy_name: str = ""
    ) -> str:
        """对应 startStoryFromConsole：预览 → y/n 确认 → 创建主剧本。"""
        if not self._require_manager(session):
            denial = self.service.manage_session_denial(session)
            detail = f"（{denial}）" if denial else ""
            return f"无权限：手动启动共享主剧本需要 HDSI 管理员权限。{detail}"
        readiness = await self.service.story_start_readiness(session)
        existing = readiness.get("existing")
        if existing is not None:
            name = existing.setting.get("character", {}).get("name") or ""
            if existing.status == "paused":
                return f"当前已有 {name} 的主剧本（暂停中）；请使用 interlude.resume 恢复，不要重复启动。"
            return f"当前已有 {name} 的活动主剧本；请使用 interlude.status 查看状态。"
        if not readiness.get("ready"):
            return self._format_story_start_readiness(readiness, "配置档案尚未适合启动")
        preview = readiness["preview"]
        legacy_note = ""
        if legacy_name.strip():
            legacy_note = f"\n已忽略旧 init 的名称参数“{legacy_name.strip()}”；角色名称以插件配置为准。"
        # 注意：`filter` 在本模块是 AstrBot 的过滤器对象，不能用内置 filter()。
        lines = [
            "即将从当前配置档案启动故事：",
            f"主角：{preview['characterName']}",
            f"角色设定：{'已填写' if preview['characterProfile'] else '未填写'}",
            f"Perspective：{'已填写' if preview['perspective'] else '未填写'}",
            f"世界与地点：{'已填写' if preview['world'] else '未填写'}",
            f"时区：{preview['timezone']}",
            f"主模型：{preview['model']}",
            f"自动创建：{'开启（首次私聊通常无需手动启动）' if preview['autoCreate'] else '关闭'}",
            *[f"提示：{warning}" for warning in readiness.get("warnings") or []],
        ]
        if legacy_note.strip():
            lines.append(legacy_note.strip())
        message = "\n".join(line for line in lines if line)
        if not await self._ask_confirmation(event, f"{message}\n确认从此档案启动吗？(y/n)"):
            return CANCELLED
        story = await self.service.create_story(session)
        participant = await self.service.find_participant(session, story)
        name = participant.displayName if participant else session.userId
        return f"已从配置档案启动 {story.setting.get('character', {}).get('name')} 的共享主剧本，并加入 {name}。"

    def _format_story_start_readiness(self, readiness: Dict[str, Any], title: str = "配置档案检查") -> str:
        preview = readiness["preview"]
        lines = [
            title,
            f"主角：{preview.get('characterName') or '未填写'}",
            f"角色设定：{'已填写' if preview.get('characterProfile') else '未填写'}",
            f"Perspective：{'已填写' if preview.get('perspective') else '未填写'}",
            f"世界：{'已填写' if preview.get('world') else '未填写'}",
            f"时区：{preview.get('timezone')}",
            f"主模型：{preview.get('model')}",
            f"自动创建：{'开启' if preview.get('autoCreate') else '关闭'}",
        ]
        existing = readiness.get("existing")
        if existing is not None:
            lines.append(f"运行中故事：{existing.setting.get('character', {}).get('name')}（{existing.status}）")
        else:
            lines.append("运行中故事：尚未创建")
        lines.extend(f"阻断：{item}" for item in readiness.get("blockers") or [])
        lines.extend(f"提示：{item}" for item in readiness.get("warnings") or [])
        if existing is not None:
            lines.append("结果：已有运行中故事，无需再次启动。")
        elif readiness.get("ready"):
            lines.append("结果：可以启动。")
        else:
            lines.append("结果：请先完成阻断项。")
        return "\n".join(lines)

    async def _cmd_doctor(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        readiness = await self.service.story_start_readiness(session)
        return self._format_story_start_readiness(readiness)

    async def _cmd_story_start(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        return await self._start_story_from_console(event, session)

    async def _cmd_init(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        return await self._start_story_from_console(event, session, legacy_name=args)

    async def _cmd_setup(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER_DETAIL
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        try:
            patch = json.loads(args)
            if not isinstance(patch, dict):
                raise ValueError("设定必须是 JSON 对象。普通测试无需使用此命令。")
        except ValueError as error:
            return f"JSON 格式不正确：{error}"
        updated = await self.service.update_setting(story, patch)
        return f"已保存 {updated.setting.get('character', {}).get('name')} 的当前故事设定。"

    async def _cmd_qzone(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        content = args.strip()
        if not content:
            return "说说内容不能为空。"
        if not await self.service.qzone_available():
            return "QQ 空间通道不可用：请确认配置已启用 QQ 空间（qzone.enabled，【扩展 17】）且平台连接支持空间动作。"
        result = await self.service.qzone_execute(story, "post", {"content": content, "ugcRight": 4}, session.selfId)
        if result.get("ok"):
            return f"已发表说说（tid={result.get('tid')}，好友可见）。"
        return f"发表失败：{result.get('error')}"

    async def _cmd_story_alias(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        text = args.strip()
        if text.startswith("remove "):
            alias_id = text[len("remove "):].strip()
            if not alias_id:
                return "用法：interlude.story.alias remove <别名ID>"
            result = await self.service.remove_story_alias(alias_id, f"by {session.userId}")
            if result.get("ok"):
                return f"已回滚别名 {alias_id}（审计已写入剧本条目）。"
            return f"回滚失败：{result.get('error')}"
        if text:
            return "用法：interlude.story.alias [remove <别名ID>]"
        aliases = self.service.list_story_aliases()
        if not aliases:
            return "当前没有剧本别名。"
        lines = [f"剧本别名（{len(aliases)} 条）："]
        lines.extend(
            f"{row['aliasStoryId']} → {row['canonicalStoryId']}（{row['reason']}，{format_story_display_time(row['createdAt'], 'UTC')}）"
            for row in aliases
        )
        return "\n".join(lines)

    async def _cmd_story_endpoint(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        parts = args.split()
        if parts and parts[0] == "add":
            if len(parts) < 3:
                return "用法：interlude.story.endpoint add <平台> <账号> [qq|wechat]"
            platform, account = parts[1], parts[2]
            kind = "wechat" if (len(parts) > 3 and parts[3] == "wechat") else "qq"
            result = await self.service.add_story_endpoint(story, platform, account, kind)
            if result.get("ok"):
                return f"角色端点已注册（{platform} {account}，{kind}，端点 {result.get('endpointId')}）。"
            return f"注册失败：{result.get('error')}"
        if parts and parts[0] == "disable":
            if len(parts) < 2:
                return "用法：interlude.story.endpoint disable <端点ID>"
            result = await self.service.disable_story_endpoint(parts[1])
            if result.get("ok"):
                return f"端点已停用（{parts[1]}）。"
            return f"停用失败：{result.get('error')}"
        if parts:
            return "用法：interlude.story.endpoint [add <平台> <账号> [qq|wechat] | disable <端点ID>]"
        endpoints = self.service.list_story_endpoints(story.id)
        if not endpoints:
            return "当前故事没有登记任何角色端点。"
        lines = [f"角色端点（{len(endpoints)} 个）："]
        lines.extend(
            f"{'●' if item['enabled'] else '○'} {item['platform']} {item['selfId']}"
            f"（{item['channelKind']}{'·在线' if item.get('online') else ''}）端点={item['endpointId']}"
            for item in endpoints
        )
        return "\n".join(lines)

    async def _cmd_reset(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return "无权限。"
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        confirmed = await self._ask_confirmation(
            event, "这将删除所有剧本、记忆、事实，并将角色设定重置为配置档案当前值。确定吗？(y/n)"
        )
        if not confirmed:
            return "已取消。"
        await self.service.clear_database()
        await self.service.purge_all_story_data(story.id)
        self.service.clear_preset_sync_state()
        return (
            "已完全重置。数据库已清空，角色设定已回到配置的故事档案（storyDefaults）模板。\n"
            "如需更换角色身份，请在插件配置中修改故事档案后重新开始。"
        )

    async def _cmd_participant_link(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        parts = args.split()
        if len(parts) < 2:
            return "用法：interlude.participant.link <参与者ID> <用户ID>"
        participant = await self.service.get_participant(parts[0])
        if participant is None:
            return f"参与者不存在：{parts[0]}"
        result = await self.service.link_participant_endpoint(participant, session.platform, parts[1])
        if result.get("ok"):
            return f"用户端点已链接（{parts[1]} → {participant.displayName or participant.id}）。"
        return f"链接失败：{result.get('error')}"

    async def _cmd_participant_unlink(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        endpoint_id = args.strip()
        if not endpoint_id:
            return "用法：interlude.participant.unlink <端点ID>"
        result = await self.service.unlink_participant_endpoint(endpoint_id)
        if result.get("ok"):
            return f"端点已解除链接（{endpoint_id}）。"
        return f"解除失败：{result.get('error')}"

    async def _cmd_participant_endpoints(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        participant_id = args.strip()
        if not participant_id:
            return "用法：interlude.participant.endpoints <参与者ID>"
        endpoints = self.service.list_participant_endpoints(participant_id)
        if not endpoints:
            return f"参与者 {participant_id} 没有用户端点。"
        lines = [f"用户端点（{len(endpoints)} 个）："]
        lines.extend(
            f"{'●' if item['enabled'] else '○'} {item['platform']} {item['userId']} 端点={item['endpointId']}"
            for item in endpoints
        )
        return "\n".join(lines)

    async def _cmd_status(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        """故事与模型状态（对应上游 interlude.status，含健康指标段）。"""
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        health = self.service.health
        participants = await self.service.participants(story.id)
        agency = self.service.config.get("agency") or {}
        agency_window = story.state.get("agencyWindow") or {}
        main_label = self.service.narrator.main_connection_label()
        total = health["narrativeTotal"] + health["narrativeFailed"]
        reply_modes = health["replyModes"]
        lines = [
            f"主角：{story.setting.get('character', {}).get('name')}",
            f"关系人数：{len(participants)}",
            f"故事状态：{story.status}",
            f"已写到：{to_iso(story.cursorAt)}",
            f"主模型连接：{main_label or '未指定（按模型配置回退）'}",
            f"允许主动可见消息：{'开启' if self.service.runtime.get('allowProactiveMessages') else '关闭'}",
            f"Agency Window：{'开启' if agency.get('enabled') else '关闭'}（{agency_window.get('activityLoad') or '尚未建立'}）",
            f"—— 健康指标（自 {health.get('sinceAt') or '-'}）——",
            f"主叙事成功：{health['narrativeTotal']}/{total}（{health['successRate'] * 100:.1f}%）",
            f"结构化回复缺失：{health['structureMissing']} 次（挽回 {health['recoverySaved']} 次）",
            "回复模式：immediate %d / none %d / delayed %d / 无投递 %d"
            % (reply_modes.get("immediate", 0), reply_modes.get("none", 0), reply_modes.get("delayed", 0), reply_modes.get("noDelivery", 0)),
            f"前缀缓存命中：{health['cacheHitRate'] * 100:.1f}%（输入 {health['inputTokens']:,} tokens）",
            f"中位延迟：{health['medianLatencyMs'] / 1000:.1f}s",
        ]
        return "\n".join(lines)

    async def _change_status(self, session: SessionView, status: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        await self.service.set_status(story, status)
        onebot = self._config_section("onebot")
        if onebot.get("enabled") and not (onebot.get("userAccounts") or []):
            return "已切换状态；注意：QQ 接入已启用但用户白名单为空，所有私聊都会被拒绝。"
        return "故事已恢复自动处理。" if status == "active" else "故事已暂停自动处理；已有记录不会删除。"

    async def _cmd_pause(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        return await self._change_status(session, "paused")

    async def _cmd_resume(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        return await self._change_status(session, "active")

    async def _cmd_advance(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        messages = await self.service.advance_story(story)
        delivered = []
        if messages:
            participant = await self.service.find_participant(session, story)
            if participant:
                delivered = await self.service._deliver_messages_for_participant(story, participant, messages, 0.0)  # noqa: SLF001
        if delivered:
            return "剧本已补写到现在，并已发送其中已经发生的可见角色消息。"
        if messages:
            return "剧本已补写到现在；可见消息投递未完成，请查看日志。"
        return "剧本已补写到现在；这次没有发生可见角色消息。"

    async def _cmd_timeline_rebase(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        confirmed = await self._ask_confirmation(
            event,
            "将清空当前场景摘要、连续性快照和工作暂存，并从现在重新建立宿主时间线；历史剧本和长期事实会保留。确认执行吗？(y/n)",
        )
        if not confirmed:
            return CANCELLED
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        result = await self.service.rebase_timeline(story)
        at = format_log_time(result.get("at"), story.setting.get("timezone") or "UTC")
        scene_note = "，活跃场景摘要已重置" if result.get("sceneReset") else ""
        return f"已在 {at} 重建宿主时间线{scene_note}。"

    @staticmethod
    def _parse_limit(args: str, default: int, maximum: int) -> int:
        try:
            value = int(args.strip().split()[0])
        except (ValueError, IndexError):
            return default
        return max(1, min(value, maximum))

    async def _cmd_timeline(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        limit = self._parse_limit(args, 10, 30)
        participant = await self.service.find_participant(session, story)
        entries = await self.service.recent_entries(story.id, max(1, min(limit * 3, 90)))
        filtered = [
            entry for entry in entries
            if not entry.participantId or (participant and entry.participantId == participant.id)
        ][-limit:]
        if not filtered:
            return "当前故事还没有剧本记录。"
        timezone_name = story.setting.get("timezone") or "UTC"
        return "\n".join(
            f"[{format_story_display_time(entry.occurredAt, timezone_name)}] {entry.actor}/{entry.kind}: {entry.content}"
            for entry in filtered
        )

    async def _cmd_memory(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        limit = self._parse_limit(args, 10, 30)
        participant = await self.service.find_participant(session, story)
        memories = await self.service.memories(story.id, limit, participant.id if participant else "")
        if not memories:
            return "暂时还没有提取出耐久记忆；多进行一些对话并等待后台整理后再看。"
        return "\n".join(f"[{memory.category}/{memory.importance:.2f}] {memory.content}" for memory in memories)

    async def _cmd_context(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        participant = await self.service.find_participant(session, story)
        scene = await self.service.active_scene(story.id)
        arc = await self.service.active_arc(story.id)
        facts = await self.service.facts(story.id, 8)
        state_overlay = story.state.get("settingOverlay") or {}
        agency_window = story.state.get("agencyWindow")
        lines = [
            f"场景引子：{(scene.hook if scene else '') or '尚未整理'}",
            f"场景摘要：{(scene.summary if scene else '') or '尚未整理'}",
            f"剧情弧线：{(arc.title if arc else '开场')} — {((arc.summary if arc else '') or '尚未整理')}",
            f"当前关系：{(participant.displayName if participant else session.userId)}（{(participant.relationship if participant else '未填写')}）",
            f"当前关系状态：{json.dumps(participant.state if participant else {}, ensure_ascii=False)}",
            f"主角个体价值观 / 看待世界的方式：{story.setting.get('perspective') or '未填写'}（当前 overlay：{state_overlay.get('perspective') or '未形成'}）",
            f"主角全局变化：{json.dumps(state_overlay, ensure_ascii=False)}",
            f"主体行动窗口：{json.dumps(agency_window, ensure_ascii=False) if agency_window else 'null'}",
            "长期事实：" + (
                " | ".join(f"[{fact.scope}/{fact.importance:.2f}] {fact.content}" for fact in facts) if facts else "暂无"
            ),
        ]
        return "\n".join(lines)

    async def _cmd_compact(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        compacted = await self.service.compact_story(story)
        return "已完成一次连续性记忆整理。" if compacted else "当前还没有达到需要整理的剧本量。"

    async def _cmd_script(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        limit = self._parse_limit(args, 20, 50)
        entries = await self.service.recent_entries(story.id, limit)
        if not entries:
            return "当前主剧本还没有原始条目。"
        return "\n\n".join(
            f"#{entry.id} [{to_iso(entry.occurredAt)}] {entry.actor}/{entry.kind}"
            f"{('/' + entry.participantId) if entry.participantId else ''}\n{entry.content}"
            for entry in entries
        )

    async def _cmd_script_note(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        ok = await self.service.add_admin_script_note(story, args)
        return "已写入管理员注记，后续压缩会将其纳入连续性。" if ok else "注记为空，未写入。"

    async def _cmd_memory_facts(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        limit = self._parse_limit(args, 20, 100)
        facts = await self.service.admin_facts(story.id, limit)
        if not facts:
            return "当前没有有效的长期事实。"
        return "\n\n".join(
            f"#{fact.id} [{fact.scope}] 重要度={fact.importance:.2f} 置信度={fact.confidence:.2f} 未解决={fact.unresolved}\n{fact.content}"
            for fact in facts
        )

    async def _cmd_memory_add(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return "用法：interlude.memory.add <范围> <内容>（范围：character/world/relationship/event/promise）"
        scope, content = parts[0], parts[1]
        if scope not in ("character", "world", "relationship", "event", "promise"):
            return "scope 必须是 character、world、relationship、event 或 promise。"
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        ok = await self.service.add_admin_fact(story, scope, content)
        if ok:
            return "已添加高置信度全局长期事实。"
        return "事实内容为空或主体不明确，未添加；relationship 事实请在正文中写出具体人物，例如“小林说他准备出门”。"

    async def _cmd_memory_forget(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        try:
            fact_id = int(args.strip().split()[0])
        except (ValueError, IndexError):
            return "用法：interlude.memory.forget <编号>"
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        ok = await self.service.forget_admin_fact(story.id, fact_id)
        return f"长期事实 #{fact_id} 已标记为失效。" if ok else f"未找到有效的长期事实 #{fact_id}。"

    async def _cmd_memory_intents(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        limit = self._parse_limit(args, 20, 100)
        intents = await self.service.admin_pending_intents(story.id, limit)
        if not intents:
            return "当前没有等待中的计划、提醒、承诺或剧情余波。"
        lines = []
        for intent in intents:
            payload = intent.payload or {}
            active = intent.type == "active-consequence" and payload.get("lifecycle") == "active"
            if active:
                timing = f"持续影响至={payload.get('expiresAt') or '未设置'}"
            else:
                timing = f"最早执行={to_iso(intent.notBefore)}"
            lines.append(f"#{intent.id} [{intent.type}] 参与者={intent.participantId or '全局'} {timing}\n{intent.summary}")
        return "\n\n".join(lines)

    async def _cmd_memory_cancel(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        try:
            intent_id = int(args.strip().split()[0])
        except (ValueError, IndexError):
            return "用法：interlude.memory.cancel <编号>"
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        ok = await self.service.cancel_admin_intent(story.id, intent_id)
        return f"意图 #{intent_id} 已取消。" if ok else f"未找到等待中的意图 #{intent_id}。"

    async def _cmd_memory_patches(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        limit = self._parse_limit(args, 20, 100)
        patches = await self.service.admin_state_patches(story.id, limit)
        if not patches:
            return "当前没有设定演化提案。"
        lines = []
        for patch in patches:
            proposed = patch.proposedValue
            if len(proposed) > 220:
                proposed = f"{proposed[:220]}…(共{len(patch.proposedValue)}字,详情可查数据库)"
            evidence = patch.evidence if len(patch.evidence) <= 160 else f"{patch.evidence[:160]}…"
            lines.append(
                f"#{patch.id} [{patch.status}/{patch.target}/{patch.impact}] 置信度={patch.confidence:.2f}\n"
                f"提出：{format_story_display_time(patch.createdAt, story.setting.get('timezone') or 'UTC')}"
                f"{(' · 关系对象：' + patch.participantId) if patch.participantId else ''}\n"
                f"提案：{proposed}\n证据：{evidence}"
            )
        return "\n\n".join(lines)

    async def _cmd_memory_apply(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        try:
            patch_id = int(args.strip().split()[0])
        except (ValueError, IndexError):
            return "用法：interlude.memory.apply <编号>"
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        ok = await self.service.apply_admin_state_patch(story, patch_id)
        return f"设定演化提案 #{patch_id} 已批准并写入 overlay。" if ok else f"未找到待批准的设定演化提案 #{patch_id}。"

    async def _cmd_memory_reject(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        try:
            patch_id = int(args.strip().split()[0])
        except (ValueError, IndexError):
            return "用法：interlude.memory.reject <编号>"
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        ok = await self.service.reject_admin_state_patch(story.id, patch_id)
        return f"设定演化提案 #{patch_id} 已拒绝。" if ok else f"未找到待审核的设定演化提案 #{patch_id}。"

    async def _cmd_overlay_clear(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_ADMIN
        target = args.strip().lower()
        if target not in ("character", "perspective", "relationship", "world", "all"):
            return "target 必须是 character、perspective、relationship、world 或 all。"
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        status = await self.service.admin_overlay_status(story.id)
        overlay = status.get("state") or {}
        parts = []
        if target in ("character", "all") and (overlay.get("characterProfile") or overlay.get("characterTraits")):
            parts.append("角色人设/特质演化")
        if target in ("perspective", "all") and overlay.get("perspective"):
            parts.append("视角演化")
        if target in ("relationship", "all") and overlay.get("relationship"):
            parts.append("全局关系演化")
        if target in ("world", "all") and overlay.get("world"):
            parts.append("世界演化")
        overlay_note = f"overlay 现有内容:{'、'.join(parts)}" if parts else "overlay 现有内容:(所选范围为空)"
        preview = (
            f"{overlay_note};待积累提案 {len(status.get('proposed') or [])} 条、"
            f"压缩快照 {len(status.get('snapshots') or [])} 条将按范围标记清理。"
        )
        if not await self._ask_confirmation(
            event, f"{preview}\n\n即将清理 {target} overlay；剧本和记忆不会删除。确认执行吗？(y/n)"
        ):
            return CANCELLED
        result = await self.service.clear_setting_overlay(story, target)
        note = ""
        if target in ("relationship", "all"):
            note = f"，已清理 {result.get('participantCount', 0)} 个参与者关系 overlay"
        return f"已清理 {target} overlay{note}；剧本、长期事实和普通记忆均未删除。"

    async def _cmd_overlay_status(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        status = await self.service.admin_overlay_status(story.id)
        overlay = status.get("state") or {}
        lines = ["当前全局 overlay："]
        state_lines = []
        if overlay.get("characterProfile"):
            state_lines.append(f"· 角色人设({len(overlay['characterProfile'])}字)：{overlay['characterProfile'][:80]}")
        traits = overlay.get("characterTraits") or []
        if traits:
            state_lines.append(f"· 角色特质({len(traits)}条)：{' / '.join(traits[-3:])[:80]}")
        if overlay.get("perspective"):
            state_lines.append(f"· 视角({len(overlay['perspective'])}字)：{overlay['perspective'][:80]}")
        if overlay.get("world"):
            state_lines.append(f"· 世界({len(overlay['world'])}字)：{overlay['world'][:80]}")
        if overlay.get("relationship"):
            state_lines.append(f"· 全局关系({len(overlay['relationship'])}字)：{overlay['relationship'][:80]}")
        lines.extend(state_lines or ["(空——设定尚未产生稳定演化)"])
        lines.extend([
            "",
            f"待积累提案：{len(status.get('proposed') or [])} 条（可 interlude.memory.apply 手动批准,或等证据跨回合跨日后自动应用）",
            f"已应用/已归档提案：{len(status.get('applied') or [])} 条",
            f"已清理提案：{len(status.get('cleared') or [])} 条",
            f"overlay 压缩快照：{len(status.get('snapshots') or [])} 条",
            f"参与者关系 overlay：{len(status.get('participantOverlays') or [])} 个",
        ])
        return "\n".join(lines)

    async def _cmd_overlay_compact(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_ADMIN
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        changed = await self.service.compact_overlay(story)
        return "overlay 合并和压缩完成。" if changed else "没有需要合并或压缩的 overlay。"

    async def _cmd_schedule(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        record = await self.service.admin_schedule_preplan(story.id)
        if not record:
            return "Schedule Preplan 尚未生成；后台会在空闲整理时建立。"
        from .core import schedule_preplan as preplan_mod

        window = preplan_mod.schedule_preplan_window(
            record, now_utc(), story.setting.get("timezone") or "UTC", 12,
            preplan_mod.resolve_schedule_preplan_config(self._config_section("schedulePreplan")),
        )
        lines = [
            f"Schedule Preplan：版本 {record.get('revision')}，覆盖 {record.get('validFrom')} → {record.get('validThrough')}",
            f"最后审查：{record.get('lastReviewedLocalDate') or '尚未'}；原因：{record.get('reviewReason') or '无'}",
        ]
        if window and window.get("blocks"):
            lines.extend(
                f"{block['date']} {block['start']}-{block['end']} [{block['kind']}] {block['label']}"
                f"{(' @ ' + str(block.get('location'))) if block.get('location') else ''}"
                for block in window["blocks"]
            )
        else:
            lines.append("未来约半天没有已确定的日程块。")
        return "\n".join(lines)

    async def _request_schedule_refresh(self, session: SessionView) -> str:
        if not self._require_manager(session):
            return NO_ADMIN
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        marked = await self.service.request_schedule_preplan_rebuild(story.id)
        if marked:
            return "Schedule Preplan 已标记为重新审查；会在当前前台回合结束后的空闲队列中处理。"
        return "当前还没有 Schedule Preplan；后台会自动建立。"

    async def _cmd_schedule_refresh(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        return await self._request_schedule_refresh(session)

    async def _cmd_schedule_rebuild(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        return await self._request_schedule_refresh(session)

    async def _cmd_database_clear(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_ADMIN
        if not await self._ask_confirmation(event, "即将清空 HDSI 自有数据库，剧本、记忆和状态记录都会删除。确认执行吗？(y/n)"):
            return CANCELLED
        result = await self.service.clear_database()
        self.service.clear_preset_sync_state()
        return (
            f"HDSI 数据库清空完成：处理 {result.get('removed', 0)} 条记录。"
            "分享码同步状态已重置：下次启动/手动 sync 会重新拉取并应用预设。"
        )

    async def _cmd_purge_all(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        if not await self._ask_confirmation(event, "即将删除所有平台的剧本、记忆、事实、意图和状态。确认执行吗？(y/n)"):
            return CANCELLED
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        await self.service.purge_all_data(story.id)
        return (
            "已彻底重置所有平台：旧剧本、场景摘要、剧情弧线、长期事实、记忆、意图、状态演化和参与者关系状态均已清除；"
            "当前故事保留为空白的全局主剧本，Canon 已按当前配置重建。"
        )

    async def _cmd_purge_platform(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        platform = args.strip().lower()
        if not platform:
            return "请填写平台名，例如 onebot。"
        if not await self._ask_confirmation(event, f"即将删除平台 {platform} 的全部剧本和记忆。确认执行吗？(y/n)"):
            return CANCELLED
        count = await self.service.purge_platform_data(platform)
        if count:
            return f"已清空并归档平台 {platform} 的 {count} 部剧本；其它平台不受影响。"
        return f"没有找到平台 {platform} 的 HDSI 剧本。"

    async def _cmd_purge_range(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        parts = args.split()
        if len(parts) < 2:
            return "用法：interlude.purge.range <开始> <结束>（ISO-8601，例如 2026-08-01T00:00:00+08:00）"
        from .core.types import ensure_utc

        from_at = ensure_utc(parts[0])
        to_at = ensure_utc(parts[1])
        if from_at is None or to_at is None or from_at > to_at:
            return "时间范围无效，请使用 ISO-8601，例如 2026-08-01T00:00:00+08:00。"
        if not await self._ask_confirmation(
            event, f"即将删除 {to_iso(from_at)} 至 {to_iso(to_at)} 范围内的剧本和关联记忆。确认执行吗？(y/n)"
        ):
            return CANCELLED
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        await self.service.purge_story_range(story.id, from_at, to_at)
        return f"已删除 {to_iso(from_at)} 至 {to_iso(to_at)} 范围内的剧本和关联记忆；Canon 与参与者身份未删除。"

    # ---- 分享码预设（typ0-site 联动） ----

    @staticmethod
    def _parse_preset_args(text: str) -> Dict[str, Any]:
        args = text.split()
        value_of = lambda flag: (  # noqa: E731
            args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
        )
        code = None
        for index, item in enumerate(args):
            if item.startswith("--"):
                continue
            if index > 0 and args[index - 1].lower() in ("--sections", "--base"):
                continue
            code = item
            break
        sections_value = value_of("--sections")
        return {
            "code": code,
            "baseUrl": value_of("--base") or default_share_base(),
            "story": "--story" in args,
            "sectionFilter": (
                [item.strip() for item in re.split(r"[,，]", sections_value) if item.strip()]
                if sections_value
                else None
            ),
        }

    async def _cmd_preset_check(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        parsed = self._parse_preset_args(args)
        if not parsed["code"]:
            return "用法:interlude.preset.check <分享码> [--base http://…] [--sections runtime,urge] [--story]"
        try:
            info = await fetch_share(parsed["baseUrl"], parsed["code"])
        except ValueError as error:
            return f"分享码检查失败:{error}"
        sections = (
            [section for section in info["sections"] if section in parsed["sectionFilter"]]
            if parsed["sectionFilter"]
            else info["sections"]
        )
        if not sections:
            return "指定的分区在该预设中不存在。"
        summary = summarize_share(self._config_view(), info, sections)
        lines = format_diff_lines(summary)
        if not summary["diffs"] and not summary["storyPatch"]:
            lines.append("(与本地配置无差异)")
        base_arg = f" --base {parsed['baseUrl']}" if parsed["baseUrl"] != default_share_base() else ""
        lines.extend([
            "",
            f"确认无误后执行:interlude.preset.apply {info['code']}{base_arg}"
            f"{' --story' if parsed['story'] else ''}"
            f"{' --sections ' + ','.join(parsed['sectionFilter']) if parsed['sectionFilter'] else ''}",
        ])
        return "\n".join(lines)

    async def _cmd_preset_apply(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        parsed = self._parse_preset_args(args)
        if not parsed["code"]:
            return "用法:interlude.preset.apply <分享码> [--base http://…] [--sections runtime,urge] [--story]"
        try:
            first = await fetch_share(parsed["baseUrl"], parsed["code"])
        except ValueError as error:
            return f"分享码应用失败:{error}"
        sections = (
            [section for section in first["sections"] if section in parsed["sectionFilter"]]
            if parsed["sectionFilter"]
            else first["sections"]
        )
        if not sections:
            return "指定的分区在该预设中不存在。"
        summary = summarize_share(self._config_view(), first, sections)
        lines = format_diff_lines(summary)
        preview_revision = first["revision"]
        extra_note = ""
        if "storyDefaults" in sections:
            extra_note += ",storyDefaults 同时写入配置模板"
        if parsed["story"]:
            extra_note += ",并应用到当前故事"
        if not await self._ask_confirmation(
            event, "\n".join([*lines, "", f"即将应用 revision {preview_revision}(配置分区整段替换{extra_note})。继续吗?"])
        ):
            return CANCELLED
        try:
            second = await fetch_share(parsed["baseUrl"], parsed["code"])
        except ValueError as error:
            return f"分享码应用失败:{error}"
        if second["revision"] != preview_revision:
            return (
                f"远端已更新(revision {preview_revision} → {second['revision']}),预览内容可能已过期。"
                f"请重新执行 interlude.preset.check {second['code']}。"
            )
        patch_sections = list(summary["configSections"])
        if "storyDefaults" in sections and isinstance((second.get("payload") or {}).get("storyDefaults"), dict):
            patch_sections.append("storyDefaults")
        snapshot_sections = {section: self._config_view().get(section) for section in patch_sections}
        results: List[str] = []
        if patch_sections:
            patch = build_config_patch(self._config_view(), second["payload"], patch_sections, include_story_defaults=True)
            self._apply_config_patch(patch)
            results.append(f"配置已写入:{'、'.join(patch_sections)}(storyDefaults 模板即时生效于新故事;部分运行时参数需插件重载后生效)")
        story_setting_snapshot = None
        story_id = None
        if parsed["story"]:
            story = await self.service.find_story(session)
            if story is not None:
                patch = summary["storyPatch"]
                if isinstance(patch, dict) and patch:
                    await self.service.update_setting(story, patch)
                    results.append("storyDefaults 已应用到当前故事设定。")
                    story_setting_snapshot = story.setting
                    story_id = story.id
                else:
                    results.append("当前会话没有可应用的故事(或预设无 storyDefaults 内容),已跳过故事设定。")
        try:
            from .core.types import deep_copy

            self.service.save_preset_sync_snapshot({
                "code": second["code"],
                "revision": second["revision"],
                "title": second["title"],
                "appliedAt": to_iso(now_utc()),
                "configSections": deep_copy(snapshot_sections),
                "storySetting": deep_copy(story_setting_snapshot),
                "storyId": story_id,
            })
            results.append("(已保存应用前快照:interlude.preset.rollback 可恢复)")
        except Exception:  # noqa: BLE001
            results.append("(快照保存失败,不影响本次应用结果)")
        return "\n".join([f"已应用「{second['title']}」revision {second['revision']}。", *results])

    async def _cmd_preset_status(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        snapshot = self.service.load_preset_sync_snapshot()
        if not snapshot:
            return "尚无应用记录。"
        lines = [
            f"最近应用:{snapshot.get('title')}(分享码 {snapshot.get('code')},revision {snapshot.get('revision')})",
            f"时间:{snapshot.get('appliedAt')}",
            f"分区:{'、'.join((snapshot.get('configSections') or {}).keys()) or '(无)'}",
        ]
        if snapshot.get("storySetting"):
            lines.append("含当前故事设定快照")
        return "\n".join(lines)

    async def _cmd_preset_sync(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        sync = self._config_section("presetSync")
        code = extract_share_code(sync.get("shareUrl") or sync.get("shareCode"))
        if not sync.get("enabled") or not code:
            return "\n".join([
                "Type0 预设导入未启用。",
                "请在「插件配置 → 【预设入口】Type0 预设」中配置:",
                "  1. 启用预设同步",
                "  2. 填写分享码或完整分享链接",
                f"  3.{' 加载时自动导入已开启' if sync.get('autoUpdate') else ' 加载时自动导入未开启（仅手动触发）'}",
                "",
                "填好后保存并重载，插件会直连分享站自动拉取；也可执行 interlude.preset.sync 手动触发。",
            ])
        status_lines = [
            f"同步配置:{sync.get('baseUrl') or default_share_base()} · 分享码 {code}",
            f"加载时自动导入:{'开启' if sync.get('autoUpdate') else '关闭(仅手动)'}",
            f"应用分区:{('、'.join(sync.get('sections') or [])) if sync.get('sections') else '预设里有什么就同步什么'}",
            f"应用到当前故事:{'是' if sync.get('applyToCurrentStory') else '否'}",
        ]
        if not await self._ask_confirmation(event, "\n".join([*status_lines, "", "现在触发一次同步吗?"])):
            return "\n".join(status_lines)
        applied, reason = await self._run_preset_sync(event, session, force=True)
        return "\n".join([*status_lines, "", f"同步结果:{'✅ 已应用' if applied else '⏭ 跳过'} — {reason}"])

    async def _run_preset_sync(
        self, event: Optional[AstrMessageEvent], session: Optional[SessionView], force: bool = False
    ) -> Tuple[bool, str]:
        """自动/手动同步共用路径（对应 runAutoSync）。

        `session=None` 表示插件加载时的内置导入：此时不依赖任何消息事件，
        故事级应用回落到 canonical story。
        """
        sync = self._config_section("presetSync")
        code = extract_share_code(sync.get("shareUrl") or sync.get("shareCode"))
        base_url = str(sync.get("baseUrl") or "").strip() or default_share_base()
        if not sync.get("enabled") or not code:
            return False, "同步未启用或未填写分享码"
        try:
            info = await fetch_share(base_url, code)
        except ValueError as error:
            return False, f"拉取失败：{error}"
        state = self.service.load_preset_sync_snapshot() or {}
        if not force and state.get("revision") == info["revision"] and state.get("code") == info["code"]:
            return False, "revision 未变化，跳过"
        sections = sync.get("sections") or []
        selected = [section for section in info["sections"] if not sections or section in sections]
        # `storyDefaults` 属于内容型数据，已迁至 WebUI 内容库，不再写回配置页；
        # 需要落到当前故事时仍走下方 applyToCurrentStory 分支。
        patch = build_config_patch(
            self._config_view(),
            info["payload"],
            [section for section in selected if section != "storyDefaults"],
            include_story_defaults=False,
        )
        if patch:
            self._apply_config_patch(patch)
        if sync.get("applyToCurrentStory") and isinstance((info["payload"] or {}).get("storyDefaults"), dict):
            story = (
                await self.service.find_story(session)
                if session is not None
                else await self.service.get_canonical_story()
            )
            if story is not None:
                await self.service.update_setting(story, info["payload"]["storyDefaults"])
        self.service.save_preset_sync_snapshot({
            "code": info["code"],
            "revision": info["revision"],
            "title": info["title"],
            "appliedAt": to_iso(now_utc()),
            "configSections": {section: self._config_view().get(section) for section in patch.keys()},
            "storySetting": None,
            "storyId": None,
        })
        return True, f"已应用「{info['title']}」revision {info['revision']}"

    async def _cmd_preset_rollback(self, event: AstrMessageEvent, session: SessionView, args: str) -> str:
        if not self._require_manager(session):
            return NO_MANAGER
        snapshot = self.service.load_preset_sync_snapshot()
        if not snapshot:
            return "没有可回滚的应用记录。"
        sections_note = "、".join((snapshot.get("configSections") or {}).keys())
        extra = "\n以及该故事的角色设定" if snapshot.get("storySetting") else ""
        if not await self._ask_confirmation(event, f"将把以下分区恢复到应用「{snapshot.get('title')}」之前的状态:\n{sections_note}{extra}\n继续吗?"):
            return CANCELLED
        restore = self._config_view()
        for section, value in (snapshot.get("configSections") or {}).items():
            restore[section] = value
        self._apply_config_patch(restore)
        if snapshot.get("storySetting") and snapshot.get("storyId"):
            story = await self.service.get_story(snapshot["storyId"])
            if story is not None:
                self.service.db.update_rows(
                    "interlude_story", {"id": story.id},
                    {"setting": snapshot["storySetting"], "updatedAt": now_utc()},
                )
        self.service.clear_preset_sync_state()
        return f"已回滚到「{snapshot.get('title')}」应用之前的状态。"

    def _apply_config_patch(self, patch: Dict[str, Any]) -> None:
        """把补丁写回插件配置（对应 ctx.scope.update）。"""
        try:
            for key, value in patch.items():
                self.config[key] = value
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
        except Exception as error:  # noqa: BLE001
            self._log("warn", f"配置写回失败：{error}")
        self.service.refresh_runtime()
