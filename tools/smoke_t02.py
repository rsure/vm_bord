"""Ticket 02 冒烟：聚合表的数据面。

验的是服务端能不能给出正确的表数据（OS 回退、内存口径、磁盘合计、排序、
单机失败隔离、10 秒缓存）。搜索与排序是前端行为，靠截图与页面交互核对。
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
sys.path.insert(0, str(ROOT / "server"))

import esxi  # noqa: E402
import store  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
FAKE_HOST = "192.168.2.199"
EXPECTED_VMS = 79
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)


def call(path: str, timeout: float = 60.0):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_up(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = call("/api/health", timeout=3)
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    real_hosts = store.list_hosts()
    check("three real hosts are configured", len(real_hosts) == 3, str(len(real_hosts)))
    if failures:
        print("先把三台主机接进来再跑这个冒烟")
        return 1

    fake = store.add_host(
        {"name": "坏主机", "host": FAKE_HOST, "port": 443, "user": "root", "password": "x"},
        None,
    )
    print(f"      injected unreachable host {FAKE_HOST} for failure isolation")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "app.py"), "--port", str(PORT)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        check("server answers /api/health", wait_up())

        started = time.time()
        status, body = call("/api/inventory", timeout=90)
        cold = time.time() - started
        items = body.get("items") or []
        vms = body.get("vms") or []
        print(f"      cold inventory took {cold:.2f}s, {len(items)} hosts, {len(vms)} vms")

        check("inventory returns all four hosts", status == 200 and len(items) == 4, str(len(items)))
        broken = [i for i in items if i["host"] == FAKE_HOST]
        healthy = [i for i in items if i["host"] != FAKE_HOST]
        check(
            "unreachable host reports a readable error instead of breaking the page",
            bool(broken) and broken[0].get("error") and not broken[0].get("stats"),
            str((broken[0].get("error") or {}).get("kind") if broken else None),
        )
        check("the other three hosts still return cards", all(h.get("stats") for h in healthy))
        check("failures are isolated (timeout is the only slow part)", cold < 30, f"{cold:.1f}s")

        check("aggregated table has every VM of the three hosts", len(vms) == EXPECTED_VMS, str(len(vms)))

        names = [v["name"] or "" for v in vms]
        check("rows are sorted by name", names == sorted(names, key=str.lower))

        blank_os = [v["name"] for v in vms if not v.get("os")]
        check("every VM has an OS name (powered-off falls back to guestId)", not blank_os, str(blank_os[:3]))

        bad_mem = [
            v["name"] for v in vms
            if v.get("mem_used_mb") is not None and not (v["power"] == "poweredOn" and v["tools"] == "toolsOk")
        ]
        check("live memory only appears for running VMs with healthy Tools", not bad_mem, str(bad_mem[:3]))

        no_mem = [v["name"] for v in vms if not v.get("mem_mb")]
        check("every VM reports its configured memory", not no_mem, str(no_mem[:3]))

        bad_disk = [v["name"] for v in vms if not isinstance(v.get("disk_gb"), (int, float))]
        check("every VM reports a disk total", not bad_disk, str(bad_disk[:3]))

        target = next((v for v in vms if "东安实业" in (v["name"] or "")), None)
        check("the known 100 GB VM sums to 100 GB", bool(target) and target["disk_gb"] == 100.0,
              f"{target['disk_gb'] if target else 'not found'}")

        live = [v for v in vms if v["power"] == "poweredOn"]
        check("powered-on VMs mostly have an IP", sum(1 for v in live if v.get("ip")) >= 13,
              f"{sum(1 for v in live if v.get('ip'))}/{len(live)}")
        no_ip_with_tools = [
            v["name"] for v in live if v.get("tools") == "toolsOk" and not v.get("ip")
        ]
        check("a running VM with healthy Tools always reports an IP", not no_ip_with_tools,
              str(no_ip_with_tools[:3]))
        check("powered-off VMs carry no IP", all(not v.get("ip") for v in vms if v["power"] != "poweredOn"))

        check("host_name is attached to every row", all(v.get("host_name") for v in vms))

        detail_ok = [v for v in vms if v.get("vmx") and v.get("uuid")]
        check("expandable detail has vmx path and uuid", len(detail_ok) >= len(vms) - 2,
              f"{len(detail_ok)}/{len(vms)}")

        started = time.time()
        status, again = call("/api/inventory", timeout=30)
        warm = time.time() - started
        check("second refresh within 10s is served from cache", again.get("cached") is True and warm < 0.5,
              f"cached={again.get('cached')} {warm:.3f}s")
        check("cached payload is identical", len(again.get("vms") or []) == len(vms))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        errors = [line for line in out.splitlines() if "Traceback" in line or "Error" in line]
        if errors:
            print("--- server log errors ---")
            print("\n".join(errors[-8:]))

    store.delete_host(fake["id"])
    print(f"      removed the injected host, {len(store.list_hosts())} hosts remain")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
