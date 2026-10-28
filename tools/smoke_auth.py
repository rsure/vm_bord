"""登录鉴权冒烟：自己起一个带 --auth 的实例，跑完就关。

覆盖：未登录被挡、登录页可访问、错误密码、正确密码拿 cookie、
带 cookie 能读接口、退出后 cookie 失效、健康检查始终开放。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8798"
USER, PASS = "admin", "admin@123"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))


def call(path: str, method: str = "GET", body: dict | None = None,
         cookie: str | None = None, follow: bool = False):
    """返回 (status, headers, text)。不自动跟随重定向，好观察 303。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler if follow else _NoRedirect
    )
    try:
        with opener.open(req, timeout=10) as res:
            return res.status, dict(res.headers), res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_ready(timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            status, _, _ = call("/api/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-u", "server/app.py", "--port", "8798",
         "--auth", f"{USER}:{PASS}"],
        cwd=".",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready():
            print("服务没起来")
            return 1

        status, headers, _ = call("/api/health")
        check(status == 200, "健康检查不需要登录", f"status={status}")
        check(json.loads(_)["auth"] is True, "健康检查报告已开启鉴权")

        status, _, _ = call("/api/hosts")
        check(status == 401, "未登录读接口被拒", f"status={status}")

        status, headers, _ = call("/")
        check(status == 303 and headers.get("Location") == "/login",
              "未登录访问首页跳到登录页", f"status={status} loc={headers.get('Location')}")

        status, _, body = call("/login")
        check(status == 200 and "vm_bord" in body and 'id="u"' in body,
              "登录页可直接打开", f"status={status}")

        status, _, body = call("/api/login", "POST", {"username": USER, "password": "wrong"})
        check(status == 401, "错误密码被拒", f"status={status}")
        check("密码" in json.loads(body)["error"]["message"], "错误提示可读",
              json.loads(body)["error"]["message"])

        status, _, body = call("/api/login", "POST", {"username": "root", "password": PASS})
        check(status == 401, "只认 admin 这个账号，别的账号不行", f"status={status}")

        status, headers, _ = call("/api/login", "POST", {"username": USER, "password": PASS})
        set_cookie = headers.get("Set-Cookie", "")
        check(status == 200 and "vmbord_sid=" in set_cookie, "正确密码登录成功", f"status={status}")
        check("HttpOnly" in set_cookie, "会话 cookie 是 HttpOnly", set_cookie[:60])
        token = set_cookie.split("vmbord_sid=", 1)[1].split(";", 1)[0]

        status, _, body = call("/api/hosts", cookie=f"vmbord_sid={token}")
        hosts = json.loads(body).get("hosts", [])
        check(status == 200 and isinstance(hosts, list), "带 cookie 能读接口", f"hosts={len(hosts)}")
        check(all("has_password" in h and "password" not in h for h in hosts),
              "返回里没有明文密码字段")

        status, _, _ = call("/", cookie=f"vmbord_sid={token}")
        check(status == 200, "带 cookie 能拿到看板页面", f"status={status}")

        status, _, _ = call("/api/hosts", cookie="vmbord_sid=forged-token")
        check(status == 401, "伪造的 cookie 无效", f"status={status}")

        status, headers, _ = call("/api/logout", "POST", cookie=f"vmbord_sid={token}")
        check(status == 200 and "Max-Age=0" in headers.get("Set-Cookie", ""),
              "退出会清掉 cookie", headers.get("Set-Cookie", "")[:60])

        # 连续错密码触发锁定
        codes = []
        for _i in range(6):
            codes.append(call("/api/login", "POST", {"username": USER, "password": "nope"})[0])
        check(429 in codes, "连续错密码会被锁一段时间", f"codes={codes}")
    finally:
        try:
            call("/api/shutdown", "POST")
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    ok = sum(1 for passed, _ in results if passed)
    print(f"\n{ok}/{len(results)} 项通过")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
