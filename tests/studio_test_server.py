"""HDS Studio WebUI 独立测试接口（仅标准库）。

作用：

1. 不依赖 AstrBot，直接托管 `views/studio/` 的 HTML/CSS/JS；
2. 向 `index.html` 注入 AstrBot 插件页 bridge shim，使 SPA 在普通浏览器中可运行；
3. 提供 `POST /studio/action`（以及 AstrBot 实际路径），转发给真实的
   `ContentStore` + `StudioAPI`，请求/响应信封与插件页保持一致；
4. 供 `tests/webui_http_test.py` 做端到端调用链验证。

手动运行：

    python tests/studio_test_server.py --port 8787
    # 打开 http://127.0.0.1:8787/

带 Type0 fixture：

    python tests/studio_test_server.py --port 8787 --type0-fixture fixture.json

代码内启动：

    server = StudioTestServer().start()
    print(server.url)
    ...
    server.stop()
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.content_store import ContentStore  # noqa: E402
from core.studio_api import StudioAPI, StudioError  # noqa: E402

STATIC_DIR = ROOT / "views" / "studio"
ACTION_PATHS = (
    "/studio/action",
    "/astrbot_plugin_hds_interlude/studio/action",
)

# AstrBot 插件页由宿主注入 window.AstrBotPluginView；独立测试接口用同样的
# apiPost(path, body) 契约补齐，app.js 无需任何改动。
BRIDGE_SHIM = """<script>
(function () {
  window.AstrBotPluginView = {
    apiPost: async function (path, body) {
      const response = await fetch('/studio/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
      });
      const payload = await response.json().catch(function () { return {}; });
      if (!response.ok) {
        throw new Error(payload.message || ('HTTP ' + response.status));
      }
      return payload;
    }
  };
})();
</script>
"""


def inject_bridge(html: str) -> str:
    """在 app.js 之前注入 bridge shim（必须在模块读取 window 之前执行）。"""

    marker = '<script type="module" src="./app.js"></script>'
    if marker not in html:
        raise RuntimeError("views/studio/index.html 缺少 app.js 模块入口")
    return html.replace(marker, BRIDGE_SHIM + "    " + marker, 1)


class StudioTestServer:
    """把 Studio WebUI 与真实动作层包装成可独立运行的 HTTP 测试接口。"""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        *,
        type0_fixture: Optional[Any] = None,
        host: str = "127.0.0.1",
        port: int = 0,
        logger: Any = None,
    ) -> None:
        self._owns_data_dir = data_dir is None
        self.data_dir = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="hds-studio-"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ContentStore(str(self.data_dir), logger=logger)
        self.stories = [
            {"id": "story-test-1", "title": "测试故事", "updatedAt": "2026-10-20T00:00:00Z"},
        ]
        self.applied_story_patches: Dict[str, Dict[str, Any]] = {}

        async def list_stories() -> list:
            return [dict(item) for item in self.stories]

        async def apply_story(story_id: str, patch: Dict[str, Any]) -> None:
            self.applied_story_patches[story_id] = dict(patch)

        fixture = self._load_fixture(type0_fixture)

        async def fake_fetch(base_url: str, code: str) -> Dict[str, Any]:
            if fixture is None:
                raise ValueError("测试接口未配置 Type0 fixture")
            return json.loads(json.dumps(fixture, ensure_ascii=False))

        extra: Dict[str, Any] = {}
        if fixture is not None:
            extra["share_fetcher"] = fake_fetch

        self.api = StudioAPI(
            self.store,
            story_lister=list_stories,
            story_applier=apply_story,
            default_base="https://typ0.space",
            logger=logger,
            **extra,
        )

        self.host = host
        self.port = port
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="hds-studio-loop", daemon=True
        )
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._started = False

    # -- 生命周期 -------------------------------------------------------- #

    def start(self) -> "StudioTestServer":
        if self._started:
            return self
        self._loop_thread.start()
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        except Exception:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=5)
            raise
        self._httpd.daemon_threads = True
        self.host, self.port = self._httpd.server_address[:2]
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, name="hds-studio-http", daemon=True
        )
        self._http_thread.start()
        self._started = True
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._loop_thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=5)
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
            self._http_thread = None
        self._started = False
        if self._owns_data_dir:
            shutil.rmtree(self.data_dir, ignore_errors=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    # -- 内部 ------------------------------------------------------------ #

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        """从 HTTP 线程调用真实异步动作层。"""

        future = asyncio.run_coroutine_threadsafe(
            self.api.dispatch(action, payload or {}), self._loop
        )
        return future.result(timeout=30)

    @staticmethod
    def _load_fixture(value: Optional[Any]) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        text = Path(str(value)).read_text(encoding="utf-8")
        return json.loads(text)

    def _make_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "HDSStudioTest/1.0"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                return

            def _write(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._write(status, body, "application/json; charset=utf-8")

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
                path = self.path.split("?", 1)[0]
                try:
                    if path in ("/", "/index.html"):
                        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
                        self._write(
                            200,
                            inject_bridge(html).encode("utf-8"),
                            "text/html; charset=utf-8",
                        )
                    elif path == "/app.js":
                        self._write(
                            200,
                            (STATIC_DIR / "app.js").read_bytes(),
                            "text/javascript; charset=utf-8",
                        )
                    elif path == "/style.css":
                        self._write(
                            200,
                            (STATIC_DIR / "style.css").read_bytes(),
                            "text/css; charset=utf-8",
                        )
                    elif path == "/healthz":
                        self._json(
                            200,
                            {"status": "ok", "data": {"dataDir": str(owner.data_dir)}},
                        )
                    else:
                        self._json(404, {"status": "error", "message": "not found"})
                except Exception as error:  # noqa: BLE001 - 测试接口统一返回可读错误
                    self._json(500, {"status": "error", "message": f"静态资源读取失败：{error}"})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path not in ACTION_PATHS:
                    self._json(404, {"status": "error", "message": "not found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._json(
                        400,
                        {"status": "error", "message": f"请求体不是合法 JSON：{error}"},
                    )
                    return
                if not isinstance(payload, dict):
                    self._json(400, {"status": "error", "message": "请求体必须是 JSON 对象"})
                    return
                action = str(payload.get("action") or "").strip()
                arguments = payload.get("payload")
                if not isinstance(arguments, dict):
                    arguments = {}
                try:
                    data = owner.call(action, arguments)
                except StudioError as error:
                    self._json(error.status, {"status": "error", "message": error.message})
                    return
                except Exception as error:  # noqa: BLE001
                    self._json(500, {"status": "error", "message": f"操作失败：{error}"})
                    return
                self._json(200, {"status": "ok", "data": data})

        return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="HDS Studio WebUI 独立测试接口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", default="", help="内容库目录，留空使用临时目录")
    parser.add_argument("--type0-fixture", default="", help="Type0 预览/导入的 JSON fixture")
    args = parser.parse_args()

    server = StudioTestServer(
        data_dir=args.data_dir or None,
        type0_fixture=args.type0_fixture or None,
        host=args.host,
        port=args.port,
    ).start()
    print(f"HDS Studio 测试接口已启动：{server.url}")
    print(f"数据目录：{server.data_dir}")
    print("按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
