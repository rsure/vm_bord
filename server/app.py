"""vm_bord 本地服务：标准库 HTTP 服务 + JSON API + 静态页面。

只监听 127.0.0.1，给本机浏览器用。端口默认 8787，被占用就往后顺延。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import esxi
import store

PORT_BASE = 8787
PORT_SPAN = 12
WEB_DIR = store.ROOT / "web"
VERSION = "0.1.0"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = f"vm_bord/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ---- 基础应答 -------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, no_store: bool = True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _fail(self, status: int, kind: str, message: str):
        self._json(status, {"ok": False, "error": {"kind": kind, "message": message}})

    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖基类
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---- 路由 -----------------------------------------------------------
    def do_HEAD(self):  # noqa: N802
        self._route("GET")

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def _route(self, method: str):
        path = unquote(urlparse(self.path).path)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._serve_file(WEB_DIR / "index.html")
            if method == "GET" and path.startswith("/static/"):
                return self._serve_file(WEB_DIR / path[len("/static/"):], scoped=True)
            if method == "GET" and path == "/api/health":
                return self._json(200, {"ok": True, "version": VERSION, "port": self.server.server_port})
            if method == "GET" and path == "/api/hosts":
                return self._json(200, {"ok": True, "hosts": store.list_hosts()})
            if method == "GET" and path == "/api/inventory":
                return self._inventory()
            if method == "POST" and path == "/api/hosts":
                return self._add_host()
            return self._fail(404, "not_found", f"没有这个接口：{path}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 兜底，别让服务被一个请求带死
            self.log_message("处理 %s %s 出错: %s", method, path, exc)
            try:
                self._fail(500, "internal", f"服务内部错误：{exc}")
            except Exception:  # noqa: BLE001
                pass

    # ---- 静态文件 -------------------------------------------------------
    def _serve_file(self, target: Path, scoped: bool = False):
        try:
            resolved = target.resolve()
        except OSError:
            return self._fail(404, "not_found", "页面文件不存在")
        if scoped and not str(resolved).startswith(str(WEB_DIR.resolve())):
            return self._fail(403, "forbidden", "越界访问")
        if not resolved.is_file():
            return self._fail(404, "not_found", f"页面文件不存在：{target.name}")
        ctype = CONTENT_TYPES.get(resolved.suffix.lower(), "application/octet-stream")
        self._send(200, resolved.read_bytes(), ctype, no_store=resolved.suffix == ".html")

    # ---- API ------------------------------------------------------------
    def _inventory(self):
        entries = []
        for public in store.list_hosts():
            full = store.get_host(public["id"], with_secret=True)
            if not full:
                continue
            item = dict(public)
            item["stats"] = None
            item["error"] = None
            try:
                item["_password"] = store.secret_of(full)
                item["_cfg"] = full
            except OSError as exc:
                item["error"] = {"kind": "secret", "message": f"密码解密失败：{exc}"}
            entries.append(item)

        todo = [e for e in entries if e.get("_cfg")]
        if todo:
            with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
                list(pool.map(_collect_one, todo))

        for item in entries:
            item.pop("_cfg", None)
            item.pop("_password", None)
        self._json(200, {
            "ok": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "items": entries,
        })

    def _add_host(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._fail(400, "bad_json", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            return self._fail(400, "bad_json", "请求体必须是对象")

        host = str(payload.get("host") or "").strip()
        user = str(payload.get("user") or "").strip()
        password = str(payload.get("password") or "")
        try:
            port = int(payload.get("port") or 443)
        except (TypeError, ValueError):
            return self._fail(400, "bad_port", "端口必须是数字")
        name = str(payload.get("name") or "").strip() or host

        if not host:
            return self._fail(400, "missing_host", "请填写主机地址")
        if not user:
            return self._fail(400, "missing_user", "请填写登录账号")
        if not password:
            return self._fail(400, "missing_password", "请填写登录密码")
        if not (1 <= port <= 65535):
            return self._fail(400, "bad_port", "端口必须在 1-65535 之间")

        try:
            info = esxi.probe(host, port, user, password)
        except esxi.EsxiError as exc:
            return self._fail(400, exc.kind, f"连不上 {host}:{port} —— {exc.message}")

        try:
            entry = store.add_host(
                {"name": name, "host": host, "port": port, "user": user, "password": password},
                info.get("fingerprint"),
            )
        except ValueError as exc:
            return self._fail(409, "duplicate", str(exc))

        self._json(201, {"ok": True, "host": entry, "info": info})


def _collect_one(item: dict) -> dict:
    try:
        item["stats"] = esxi.collect_host(item["_cfg"], item["_password"])
    except esxi.EsxiError as exc:
        item["error"] = {"kind": exc.kind, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        item["error"] = {"kind": "other", "message": str(exc)}
    return item


class LocalServer(ThreadingHTTPServer):
    """独占绑定的本地服务。

    Windows 上 SO_REUSEADDR 不是"复用"而是"允许抢占"：两个进程能同时绑同一个
    端口，连接落到谁那儿说不清。这里关掉它，让重绑直接失败，好走端口顺延。
    """

    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 浏览器提前断开很常见，不该把一整段 traceback 打到控制台。
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def _bind(port: int) -> tuple[LocalServer, int]:
    last_result = None
    for candidate in range(port, port + PORT_SPAN):
        try:
            return LocalServer(("127.0.0.1", candidate), Handler), candidate
        except OSError as exc:
            last_result = exc
    raise SystemExit(f"127.0.0.1:{port}-{port + PORT_SPAN - 1} 都起不来：{last_result}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="vm_bord 本地虚拟机看板")
    parser.add_argument("--port", type=int, default=PORT_BASE, help=f"监听端口，默认 {PORT_BASE}")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    server, port = _bind(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"vm_bord 已启动：{url}")
    print(f"清单文件：{store.HOSTS_FILE}")
    print("按 Ctrl+C 停止。")

    if args.open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
