"""vm_bord 本地服务：标准库 HTTP 服务 + JSON API + 静态页面。

默认监听 127.0.0.1 给本机浏览器用，端口默认 8787，被占用就往后顺延。
部署到服务器时用 ``--host 0.0.0.0 --auth 账号:密码``：一旦开了账号，除了
登录页和健康检查，所有页面和接口都要求先登录（会话用签名 cookie）。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
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
VERSION = "0.7.0"

# ---- 登录态 ---------------------------------------------------------------
COOKIE_NAME = "vmbord_sid"
SESSION_TTL = 12 * 3600  # 12 小时
# 连续登录失败 5 次锁 60 秒，挡掉最朴素的暴力猜密码。
FAIL_LIMIT = 5
FAIL_WINDOW = 60.0

_AUTH_USER = ""
_AUTH_PASS = ""
_AUTH_ON = False
_FAILS: dict[str, list] = {}
_FAILS_LOCK = threading.Lock()

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


# ---- 会话签名 -------------------------------------------------------------
def _sign(user: str, exp: int) -> str:
    msg = f"{user}|{exp}".encode("utf-8")
    return hmac.new(store.machine_key(), msg, hashlib.sha256).hexdigest()


def _make_session(user: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    raw = f"{user}|{exp}|{_sign(user, exp)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _read_session(token: str) -> str | None:
    """验签 + 看过期；通过就返回用户名，否则 None。"""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        user, exp_s, sig = raw.split("|", 2)
        exp = int(exp_s)
    except (ValueError, TypeError, base64.binascii.Error):
        return None
    if exp < int(time.time()):
        return None
    if not hmac.compare_digest(sig, _sign(user, exp)):
        return None
    return user


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · vm_bord</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<style>
  :root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --fg:#1c2024;
          --muted:#6b7280; --line:#e3e6ea; --accent:#2563eb; --bad:#dc2626; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14171a; --card:#1b1f23; --fg:#e6e8eb; --muted:#9aa3ad;
            --line:#2b3138; --accent:#5b91f5; --bad:#f87171; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--bg); color:var(--fg);
         font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  form { width:min(340px,92vw); background:var(--card); border:1px solid var(--line);
         border-radius:14px; padding:26px 24px; box-shadow:0 12px 32px rgb(0 0 0 / .08); }
  h1 { margin:0 0 4px; font-size:18px; }
  h1 span { color:var(--accent); }
  p.sub { margin:0 0 20px; color:var(--muted); font-size:12.5px; }
  label { display:block; margin-bottom:12px; }
  label b { display:block; margin-bottom:5px; font-weight:600; font-size:12.5px; color:var(--muted); }
  input { width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:9px;
          background:transparent; color:var(--fg); font:inherit; }
  input:focus { outline:2px solid color-mix(in srgb, var(--accent) 45%, transparent); border-color:var(--accent); }
  button { width:100%; margin-top:6px; padding:10px 14px; border:0; border-radius:9px;
           background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.6; cursor:default; }
  .err { min-height:18px; margin-top:10px; color:var(--bad); font-size:12.5px; }
</style></head>
<body>
<form id="f" autocomplete="on">
  <h1>vm<span>_bord</span></h1>
  <p class="sub">虚拟机看板 · 请先登录</p>
  <label><b>账号</b><input id="u" name="username" autocomplete="username" required autofocus></label>
  <label><b>密码</b><input id="p" name="password" type="password" autocomplete="current-password" required></label>
  <button id="go" type="submit">登录</button>
  <div class="err" id="err"></div>
</form>
<script>
const f = document.getElementById("f");
f.onsubmit = async (ev) => {
  ev.preventDefault();
  const go = document.getElementById("go");
  const err = document.getElementById("err");
  err.textContent = "";
  go.disabled = true; go.textContent = "登录中…";
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: document.getElementById("u").value,
                             password: document.getElementById("p").value }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error?.message || "登录失败");
    location.href = "/";
  } catch (e) {
    err.textContent = e.message;
    go.disabled = false; go.textContent = "登录";
  }
};
</script>
</body></html>
"""


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

    def _send(self, status: int, body: bytes, ctype: str, no_store: bool = True,
              extra: list[tuple[str, str]] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        for key, value in extra or ():
            self.send_header(key, value)
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

    # ---- 登录态 ---------------------------------------------------------
    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _locked(self) -> float:
        """还剩多少秒不能再试密码（0 = 没有被锁）。"""
        with _FAILS_LOCK:
            rec = _FAILS.get(self._client_ip())
        if not rec:
            return 0.0
        return max(0.0, rec.get("until", 0.0) - time.time())

    def _note_fail(self) -> None:
        ip, now = self._client_ip(), time.time()
        with _FAILS_LOCK:
            rec = _FAILS.setdefault(ip, {"count": 0, "first": now, "until": 0.0})
            if now - rec["first"] > FAIL_WINDOW:
                rec["count"], rec["first"] = 0, now
            rec["count"] += 1
            if rec["count"] >= FAIL_LIMIT:
                rec["until"] = now + FAIL_WINDOW
                rec["count"], rec["first"] = 0, now

    def _clear_fail(self) -> None:
        with _FAILS_LOCK:
            _FAILS.pop(self._client_ip(), None)

    def _cookie(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE_NAME:
                return value
        return ""

    def _authed(self) -> bool:
        if not _AUTH_ON:
            return True
        return _read_session(self._cookie()) == _AUTH_USER

    def _redirect(self, location: str, extra=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in extra or ():
            self.send_header(key, value)
        self._cors()
        self.end_headers()

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
            if method == "GET" and path == "/login":
                return self._login_page()
            if method == "POST" and path == "/api/login":
                return self._login()
            # 健康检查不挡：部署脚本和 DSH 面板要能探活，它不泄露任何敏感信息。
            if method == "GET" and path == "/api/health":
                return self._json(200, {
                    "ok": True, "version": VERSION,
                    "port": self.server.server_port, "auth": _AUTH_ON,
                })
            if not self._authed():
                # 这里不读请求体，直接关连接：留着 body 在 socket 里，下一个
                # keep-alive 请求会把它当成请求行，服务端报 400 后整条连接报废。
                self.close_connection = True
                if path.startswith("/api/"):
                    return self._fail(401, "auth", "请先登录")
                return self._redirect("/login")
            if method == "POST" and path == "/api/logout":
                return self._logout()
            if method == "GET" and path in ("/", "/index.html"):
                return self._serve_file(WEB_DIR / "index.html")
            if method == "GET" and path.startswith("/static/"):
                return self._serve_file(WEB_DIR / path[len("/static/"):], scoped=True)
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

    # ---- 登录 -----------------------------------------------------------
    def _login_page(self):
        self._send(200, LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")

    def _login(self):
        if not _AUTH_ON:
            return self._json(200, {"ok": True, "auth": False, "message": "本服务没有开启登录"})
        wait = self._locked()
        if wait > 0:
            return self._fail(429, "locked", f"密码错误次数太多，请 {int(wait) + 1} 秒后再试")
        try:
            payload = self._body()
        except ValueError as exc:
            return self._fail(400, "bad_json", str(exc))
        user = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        ok_user = hmac.compare_digest(user.encode("utf-8"), _AUTH_USER.encode("utf-8"))
        ok_pass = hmac.compare_digest(password.encode("utf-8"), _AUTH_PASS.encode("utf-8"))
        if not (ok_user and ok_pass):
            self._note_fail()
            self.log_message("登录失败：%s", self._client_ip())
            return self._fail(401, "auth", "账号或密码不对")
        self._clear_fail()
        cookie = (f"{COOKIE_NAME}={_make_session(_AUTH_USER)}; Path=/; "
                  f"Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax")
        self._send(200, _json_bytes({"ok": True, "user": _AUTH_USER}),
                   "application/json; charset=utf-8", extra=[("Set-Cookie", cookie)])
        self.log_message("登录成功：%s", self._client_ip())

    def _logout(self):
        cookie = f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        self._send(200, _json_bytes({"ok": True}), "application/json; charset=utf-8",
                   extra=[("Set-Cookie", cookie)])

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
    端口，连接落到谁那儿说不清，所以必须关掉，让重绑直接失败、好走端口顺延。
    POSIX 上它只是允许跨过 TIME_WAIT，抢不走正在监听的端口，开着才能让服务
    重启后立刻回到原端口，而不是被前一秒的 TIME_WAIT 顶到 8788。
    """

    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 浏览器提前断开很常见，不该把一整段 traceback 打到控制台。
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def _bind(host: str, port: int) -> tuple[LocalServer, int]:
    last_result = None
    for candidate in range(port, port + PORT_SPAN):
        try:
            return LocalServer((host, candidate), Handler), candidate
        except OSError as exc:
            last_result = exc
    raise SystemExit(f"{host}:{port}-{port + PORT_SPAN - 1} 都起不来：{last_result}")


def _parse_auth(spec: str) -> tuple[str, str]:
    user, sep, password = (spec or "").partition(":")
    if not sep or not user or not password:
        raise SystemExit("--auth 的格式是 账号:密码，例如 --auth admin:admin@123")
    return user, password


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="vm_bord 虚拟机看板")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1；部署到服务器用 0.0.0.0")
    parser.add_argument("--port", type=int, default=PORT_BASE, help=f"监听端口，默认 {PORT_BASE}")
    parser.add_argument("--auth", default=os.environ.get("VMBORD_AUTH", ""), metavar="账号:密码",
                        help="开启登录（也可以放环境变量 VMBORD_AUTH，不给就不校验，仅适合本机使用）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)

    global _AUTH_USER, _AUTH_PASS, _AUTH_ON
    if args.auth:
        _AUTH_USER, _AUTH_PASS = _parse_auth(args.auth)
        _AUTH_ON = True

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    server, port = _bind(args.host, args.port)
    global _SERVER
    _SERVER = server
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    url = f"http://{shown}:{port}/"
    print(f"vm_bord 已启动：{url}")
    print(f"监听：{args.host}:{port}　登录：{'开启（账号 ' + _AUTH_USER + '）' if _AUTH_ON else '未开启'}")
    print(f"清单文件：{store.HOSTS_FILE}")
    print("按 Ctrl+C 停止。")

    if args.open:
        threading.Timer(0.7, lambda: _open_browser(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


def _open_browser(url: str) -> None:
    """有桌面才开浏览器；服务器上静默跳过。"""
    try:
        if webbrowser.open(url):
            return
    except Exception:  # noqa: BLE001 - 没有浏览器/没有 DISPLAY 都可能炸
        pass
    print(f"没找到浏览器，请手动打开：{url}")


if __name__ == "__main__":
    raise SystemExit(main())
