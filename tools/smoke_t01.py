"""Ticket 01 冒烟：起服务 -> 接入一台真机 -> 读还原卡。

只用标准库，输出全 ASCII，避免控制台编码干扰。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8790
BASE = f"http://127.0.0.1:{PORT}"
CREDS = {"name": "ESXi-152", "host": "192.168.2.152", "port": 443, "user": "root", "password": "mzkj!@#123"}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)


def call(method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_up(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = call("GET", "/api/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        check("server starts and answers /api/health", wait_up())
        if failures:
            return 1

        status, body = call("GET", "/api/hosts")
        check("GET /api/hosts is empty before any host is added", status == 200 and body.get("hosts") == [])

        status, body = call("POST", "/api/hosts", {**CREDS, "password": "definitely-wrong"})
        kind = (body.get("error") or {}).get("kind")
        check("wrong password is rejected with kind=auth", status == 400 and kind == "auth", f"{status} {kind}")

        status, body = call("POST", "/api/hosts", CREDS)
        host_id = (body.get("host") or {}).get("id")
        check("POST /api/hosts saves the host", status == 201 and bool(host_id), f"{status}")
        check(
            "fingerprint is recorded from the server certificate",
            bool((body.get("info") or {}).get("fingerprint")),
            str((body.get("info") or {}).get("fingerprint"))[:23],
        )
        check("response never echoes the plaintext password", CREDS["password"] not in json.dumps(body))
        exposed = sorted(k for k in (body.get("host") or {}) if "pass" in k)
        check("host shape only exposes has_password", exposed == ["has_password"], str(exposed))

        status, body = call("POST", "/api/hosts", CREDS)
        check("adding the same host again is a 409", status == 409, f"{status}")

        raw = (ROOT / "data" / "hosts.json").read_text(encoding="utf-8")
        check("hosts.json is written", bool(raw.strip()))
        check("hosts.json does not contain the plaintext password", CREDS["password"] not in raw)
        check("hosts.json keeps an encrypted blob", "password" in raw and len(raw) > 100)

        started = time.time()
        status, body = call("GET", "/api/inventory")
        elapsed = time.time() - started
        items = body.get("items") or []
        check("GET /api/inventory returns one item", status == 200 and len(items) == 1, f"{status}")
        if items:
            item = items[0]
            stats = item.get("stats")
            check("host card has no error", bool(stats) and not item.get("error"), str(item.get("error")))
            if stats:
                print(
                    "      card: %s %s build %s | %s cores @ %s MHz | mem %s/%s MB (%s%%) "
                    "| cpu %s%% | datastores %s | vms %s on / %s"
                    % (
                        stats.get("version"), stats.get("build"), stats.get("build"),
                        stats.get("cpu_cores"), stats.get("cpu_mhz"),
                        stats.get("mem_used_mb"), stats.get("mem_total_mb"), stats.get("mem_usage_pct"),
                        stats.get("cpu_usage_pct"),
                        len(stats.get("datastores") or []),
                        stats.get("vm_on"), stats.get("vm_total"),
                    )
                )
                check("product version is 6.7.x", str(stats.get("version", "")).startswith("6.7"), str(stats.get("version")))
                check("vm_total matches the 17 VMs on 192.168.2.152", stats.get("vm_total") == 17, str(stats.get("vm_total")))
                check("cpu_usage_pct is within 0..100", isinstance(stats.get("cpu_usage_pct"), (int, float)) and 0 <= stats["cpu_usage_pct"] <= 100, str(stats.get("cpu_usage_pct")))
                check("memory usage is derived", isinstance(stats.get("mem_usage_pct"), (int, float)) and stats["mem_usage_pct"] > 0, str(stats.get("mem_usage_pct")))
                check("datastore list is non-empty", len(stats.get("datastores") or []) > 0, str(len(stats.get("datastores") or [])))
            print(f"      inventory took {elapsed:.2f}s")

        with urllib.request.urlopen(BASE + "/", timeout=10) as res:
            page = res.read().decode("utf-8", "replace")
            check("GET / serves the single-file page", res.status == 200 and "vm_bord" in page, str(res.status))
            check("page is served as utf-8 html", "text/html" in (res.headers.get("Content-Type") or ""))
            check("page has no build step (plain script tag)", "<script>" in page)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        if out.strip():
            print("--- server log (tail) ---")
            print("\n".join(out.strip().splitlines()[-12:]))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
