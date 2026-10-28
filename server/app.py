"""vm_bord 本地服务：标准库 HTTP 服务 + JSON API + 静态页面。

只监听 127.0.0.1，给本机浏览器用。端口默认 8787，被占用就往后顺延。
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import esxi
import store

PORT_BASE = 8787
PORT_SPAN = 12
WEB_DIR = store.ROOT / "web"
VERSION = "0.6.0"

# 采集一台主机的墙钟上限：ESXi 卡住时不能把整个页面拖死。
COLLECT_TIMEOUT = 10.0
# 10 秒内重复刷新直接回缓存，避免连点把三台主机刷爆。
CACHE_TTL = 10.0
_cache = {"data": None, "ts": 0.0}
_cache_lock = threading.Lock()

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


# DSH 面板跑在 127.0.0.1:<另一个端口> 上，浏览器里跨端口就是跨域。
# 只对"本机来源"放行，互联网上的页面拿不到这个头，也就没法驱动本机电源操作。
_LOCAL_ORIGIN = re.compile(r"^http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$")

_SERVER: "ThreadingHTTPServer | None" = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"vm_bord/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ---- 基础应答 -------------------------------------------------------
    def _cors(self):
        """只给本机来源的 /api/* 请求放行跨域。"""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin or not _LOCAL_ORIGIN.match(origin):
            return
        if not urlparse(self.path).path.startswith("/api/"):
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, status: int, body: bytes, ctype: str, no_store: bool = True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                self.close_connection = True

    def _json(self, status: int, obj):
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _fail(self, status: int, kind: str, message: str):
        self._json(status, {"ok": False, "error": {"kind": kind, "message": message}})

    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖基类
        # 控制台可能已经没了（被宿主回收、管道关闭）。写日志失败绝不能连累响应：
        # send_response 内部就会调到这里，一旦抛出，客户端只会看到连接被重置。
        try:
            sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
            sys.stderr.flush()
        except (OSError, ValueError, AttributeError):
            pass

    # ---- 路由 -----------------------------------------------------------
    def do_HEAD(self):  # noqa: N802
        self._route("GET")

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def do_PUT(self):  # noqa: N802
        self._route("PUT")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

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
            if method == "PUT" and path.startswith("/api/hosts/"):
                return self._edit_host(path[len("/api/hosts/"):])
            if method == "POST" and path.startswith("/api/hosts/") and path.endswith("/delete"):
                return self._delete_host(path[len("/api/hosts/"):-len("/delete")])
            if method == "POST" and path == "/api/vms/power":
                return self._power()
            if method == "GET" and path.startswith("/api/tasks/"):
                return self._task(path[len("/api/tasks/"):])
            if method == "GET" and path == "/api/audit":
                return self._audit()
            if method == "POST" and path == "/api/shutdown":
                return self._shutdown()
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
        now = time.time()
        with _cache_lock:
            cached = _cache["data"]
            age = now - _cache["ts"]
        if cached is not None and age < CACHE_TTL:
            return self._json(200, {**cached, "cached": True, "cache_age": round(age, 1)})

        data = _build_inventory()
        with _cache_lock:
            _cache["data"] = data
            _cache["ts"] = time.time()
        self._json(200, {**data, "cached": False, "cache_age": 0})

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

    def _edit_host(self, host_id: str):
        try:
            payload = self._body()
        except ValueError as exc:
            return self._fail(400, "bad_json", str(exc))

        entry = store.get_host(host_id, with_secret=True)
        if not entry:
            return self._fail(404, "not_found", "这台主机已经不在清单里了")

        host = str(payload.get("host") or entry["host"]).strip()
        user = str(payload.get("user") or entry["user"]).strip()
        name = str(payload.get("name") or entry["name"]).strip() or host
        try:
            port = int(payload.get("port") or entry.get("port") or 443)
        except (TypeError, ValueError):
            return self._fail(400, "bad_port", "端口必须是数字")
        if not (1 <= port <= 65535):
            return self._fail(400, "bad_port", "端口必须在 1-65535 之间")

        # 密码留空 = 不改：解出原来那份继续用，绝不覆盖成空。
        password = str(payload.get("password") or "")
        if not password:
            try:
                password = store.secret_of(entry)
            except OSError as exc:
                return self._fail(500, "secret", f"密码解密失败：{exc}")
        if not password:
            return self._fail(400, "missing_password", "还没有存过密码，请先填写登录密码")

        # 改完必须能连上，否则不落盘 —— 别把能用的配置改成不能用的。
        try:
            info = esxi.probe(host, port, user, password)
        except esxi.EsxiError as exc:
            return self._fail(400, exc.kind, f"连不上 {host}:{port} —— {exc.message}")

        old_fp = entry.get("fingerprint")
        new_fp = info.get("fingerprint")
        try:
            updated = store.update_host(host_id, {
                "name": name, "host": host, "port": port,
                "user": user, "password": password,
            }, new_fp)
        except KeyError:
            return self._fail(404, "not_found", "这台主机已经不在清单里了")
        _invalidate_cache()
        self._json(200, {
            "ok": True,
            "host": updated,
            "fingerprint": new_fp,
            "fingerprint_changed": bool(old_fp and new_fp and old_fp != new_fp),
        })

    def _delete_host(self, host_id: str):
        try:
            payload = self._body()
        except ValueError as exc:
            return self._fail(400, "bad_json", str(exc))

        entry = store.get_host(host_id)
        if not entry:
            return self._fail(404, "not_found", "这台主机已经不在清单里了")
        confirm = str(payload.get("confirm") or "").strip()
        if confirm != entry["host"]:
            return self._fail(400, "confirm", f"确认地址不符，请原样输入：{entry['host']}")

        store.delete_host(host_id)
        _invalidate_cache()
        self._json(200, {"ok": True, "removed": entry["id"], "host": entry["host"]})

    # ---- 请求体 ---------------------------------------------------------
    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        return payload

    def _power(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._fail(400, "bad_json", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            return self._fail(400, "bad_json", "请求体必须是对象")

        host_id = str(payload.get("host_id") or "").strip()
        moref = str(payload.get("moref") or "").strip()
        action = str(payload.get("action") or "").strip()
        confirm_name = str(payload.get("confirm_name") or "").strip()
        vm_name = str(payload.get("name") or "").strip()

        if action not in esxi.POWER_LABEL:
            return self._fail(400, "bad_action", f"不认识的操作：{action or '（空）'}")
        if not host_id or not moref:
            return self._fail(400, "bad_request", "缺少 host_id 或 moref")
        if action in esxi.FORCE_ACTIONS and not confirm_name:
            return self._fail(400, "confirm", "危险操作必须先输入虚拟机名称确认")

        entry = store.get_host(host_id, with_secret=True)
        if not entry:
            return self._fail(404, "not_found", "这台主机已经不在清单里了")
        try:
            password = store.secret_of(entry)
        except OSError as exc:
            return self._fail(500, "secret", f"密码解密失败：{exc}")

        task = _new_task(host_id, moref, action, vm_name)
        threading.Thread(
            target=_run_power,
            args=(task["id"], entry, password, moref, action, confirm_name, vm_name),
            daemon=True,
        ).start()
        self._json(202, {"ok": True, "task": dict(task)})

    def _audit(self):
        self._json(200, {"ok": True, "items": store.read_audit(200)})

    def _shutdown(self):
        """给 DSH 面板的"停止"按钮用：先回话，再让服务自己退出。"""
        if _SERVER is None:
            return self._fail(503, "not_ready", "服务还没完全起来")
        self._json(200, {"ok": True, "message": "服务正在退出"})
        threading.Timer(0.2, _SERVER.shutdown).start()

    def _task(self, task_id: str):
        with _tasks_lock:
            task = _tasks.get(task_id)
        if not task:
            return self._fail(404, "not_found", "任务不存在或已被清理")
        self._json(200, {"ok": True, "task": dict(task)})


# ---- 电源操作的任务表 ----------------------------------------------------

_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
TASK_LIMIT = 50


def _new_task(host_id: str, moref: str, action: str, name: str = "") -> dict:
    task = {
        "id": "t_" + secrets.token_hex(8),
        "action": action,
        "label": esxi.POWER_LABEL[action],
        "host_id": host_id,
        "moref": moref,
        "name": name,
        "state": "running",
        "message": f"{esxi.POWER_LABEL[action]}中…",
        "error": None,
        "vm": None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": None,
    }
    with _tasks_lock:
        _tasks[task["id"]] = task
        while len(_tasks) > TASK_LIMIT:
            del _tasks[next(iter(_tasks))]
    return task


def _invalidate_cache() -> None:
    with _cache_lock:
        _cache["data"] = None
        _cache["ts"] = 0.0


def _run_power(task_id: str, entry: dict, password: str, moref: str, action: str,
               confirm_name: str = "", fallback_name: str = "") -> None:
    """在后台线程里跑完整条操作链，页面靠轮询任务状态看进展。"""
    result, error = None, None
    started = time.time()
    try:
        result = esxi.power_op(entry, password, moref, action, confirm_name)
    except esxi.EsxiError as exc:
        error = {"kind": exc.kind, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        error = {"kind": "other", "message": str(exc)}

    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if error:
            task["state"] = "error"
            task["error"] = error
            task["message"] = error["message"]
        else:
            task["state"] = "done"
            task["vm"] = result["vm"]
            task["name"] = result.get("name") or task.get("name") or fallback_name
            task["message"] = f"{result['label']}完成"

    _invalidate_cache()

    # 不管成败都留一条审计流水，只追加。
    try:
        store.append_audit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "host_id": entry.get("id"),
            "host": entry.get("host"),
            "vm": (result or {}).get("name") or fallback_name or None,
            "moref": moref,
            "action": action,
            "label": esxi.POWER_LABEL.get(action),
            "force": action in esxi.FORCE_ACTIONS,
            "ok": error is None,
            "error": (error or {}).get("message"),
            "seconds": round(time.time() - started, 1),
        })
    except OSError:
        pass


def _collect_one(item: dict) -> dict:
    try:
        item["stats"] = esxi.collect_host(item["_cfg"], item["_password"], include_vms=True)
    except esxi.EsxiError as exc:
        item["error"] = {"kind": exc.kind, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        item["error"] = {"kind": "other", "message": str(exc)}
    return item


def _build_inventory() -> dict:
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
        pool = ThreadPoolExecutor(max_workers=min(8, len(todo)))
        futures = {pool.submit(_collect_one, entry): entry for entry in todo}
        deadline = time.time() + COLLECT_TIMEOUT
        for future in futures:
            entry = futures[future]
            remaining = deadline - time.time()
            try:
                future.result(timeout=max(0.05, remaining))
            except FuturesTimeout:
                entry["error"] = {
                    "kind": "timeout",
                    "message": f"采集超时（{int(COLLECT_TIMEOUT)} 秒没回来）",
                }
            except Exception as exc:  # noqa: BLE001
                entry["error"] = {"kind": "other", "message": str(exc)}
        pool.shutdown(wait=False)

    vms = []
    for item in entries:
        stats = item.get("stats") or {}
        now_fp = stats.get("fingerprint")
        stored_fp = item.get("fingerprint")
        item["fingerprint_now"] = now_fp
        # 证书换了 = 可能重装过 ESXi，也可能有人在中间，卡片上必须说出来。
        item["fingerprint_changed"] = bool(now_fp and stored_fp and now_fp != stored_fp)
        for vm in stats.pop("vms", None) or []:
            vms.append({
                **vm,
                "host_id": item["id"],
                "host_name": item["name"],
                "host_addr": item["host"],
            })
        item.pop("_cfg", None)
        item.pop("_password", None)
    vms.sort(key=lambda row: (row.get("name") or "").lower())

    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "items": entries,
        "vms": vms,
    }


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
    global _SERVER
    _SERVER = server
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
