"""票 04 冒烟：强制断电 / 强制重置 的确认闸门 + 审计流水。

会真的对 192.168.2.165 上的「165-东安实业测试」动手，跑完恢复开机。
用法：python tools/smoke_t04.py [--base http://127.0.0.1:8787]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
TARGET = "165-东安实业测试"
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def call(path: str, payload=None, method: str | None = None):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def find_vm() -> dict:
    _, data = call("/api/inventory")
    for vm in data.get("vms", []):
        if vm["name"] == TARGET:
            return vm
    raise SystemExit(f"清单里找不到 {TARGET}")


def state_of(vm: dict) -> dict:
    _, data = call("/api/inventory")
    for row in data.get("vms", []):
        if row["moref"] == vm["moref"]:
            return row
    return {}


def wait_task(task_id: str, limit: float = 180.0) -> dict:
    deadline = time.time() + limit
    while time.time() < deadline:
        time.sleep(1.5)
        _, data = call("/api/tasks/" + task_id)
        task = data.get("task") or {}
        if task.get("state") != "running":
            return task
    return {"state": "timeout", "error": {"message": "等太久"}}
    # noqa: unreachable


def audit_lines() -> list[dict]:
    _, data = call("/api/audit")
    return data.get("items", [])


def main() -> int:
    vm = find_vm()
    print(f"目标：{vm['name']}  moref={vm['moref']}  电源={vm['power']}  tools={vm['tools_running']}")
    base_payload = {"host_id": vm["host_id"], "moref": vm["moref"], "name": vm["name"]}

    # A. 参数校验：缺 confirm_name 直接拒绝
    status, data = call("/api/vms/power", dict(base_payload, action="poweroff"))
    check("A1 危险操作缺 confirm_name → 400",
          status == 400 and (data.get("error") or {}).get("kind") == "confirm",
          f"{status} {(data.get('error') or {}).get('message')}")

    status, data = call("/api/vms/power", dict(base_payload, action="boom"))
    check("A2 非法 action → 400", status == 400, str(status))

    status, data = call("/api/vms/power",
                        {"host_id": "nope", "moref": "vm-1", "action": "poweroff",
                         "confirm_name": TARGET})
    check("A3 未知主机 → 404", status == 404, str(status))

    # B. 名称不符：任务失败在 confirm，机器状态不变
    before = state_of(vm)
    status, data = call("/api/vms/power", dict(base_payload, action="poweroff", confirm_name="随便写的"))
    task = wait_task(data["task"]["id"], 60)
    after = state_of(vm)
    check("B1 名称不符 → 任务失败 kind=confirm",
          task.get("state") == "error" and (task.get("error") or {}).get("kind") == "confirm",
          (task.get("error") or {}).get("message"))
    check("B2 名称不符时虚拟机状态未变", before.get("power") == after.get("power"),
          f"{before.get('power')} → {after.get('power')}")

    # C. 真机：强制断电 → 开机 → 强制重置
    if vm["power"] != "poweredOn":
        print("目标当前不是开机状态，先开机")
        _, data = call("/api/vms/power", dict(base_payload, action="on"))
        wait_task(data["task"]["id"], 180)

    t0 = time.time()
    _, data = call("/api/vms/power", dict(base_payload, action="poweroff", confirm_name=TARGET))
    task = wait_task(data["task"]["id"], 180)
    took = round(time.time() - t0, 1)
    row = state_of(vm)
    check("C1 强制断电成功且状态为 poweredOff",
          task.get("state") == "done" and row.get("power") == "poweredOff",
          f"{took}s → {row.get('power')}")

    _, data = call("/api/vms/power", dict(base_payload, action="on"))
    task = wait_task(data["task"]["id"], 180)
    row = state_of(vm)
    check("C2 重新开机恢复 poweredOn",
          task.get("state") == "done" and row.get("power") == "poweredOn",
          str(row.get("power")))

    print("等 Tools 回到 guestToolsRunning（最多 180 秒）")
    deadline = time.time() + 180
    while time.time() < deadline:
        row = state_of(vm)
        if row.get("tools_running") == "guestToolsRunning":
            break
        time.sleep(5)
    ready = row.get("tools_running") == "guestToolsRunning"
    check("C3 开机后 Tools 回到 guestToolsRunning", ready, str(row.get("tools_running")))

    if ready:
        t0 = time.time()
        _, data = call("/api/vms/power", dict(base_payload, action="reset", confirm_name=TARGET))
        task = wait_task(data["task"]["id"], 180)
        took = round(time.time() - t0, 1)
        check("C4 强制重置任务成功", task.get("state") == "done",
              f"{took}s {task.get('message')}")
        # 重置后 Tools 会掉线再回来
        seen_down = False
        deadline = time.time() + 180
        while time.time() < deadline:
            row = state_of(vm)
            running = row.get("power") == "poweredOn" and row.get("tools_running") == "guestToolsRunning"
            if not running:
                seen_down = True
            elif seen_down:
                break
            time.sleep(5)
        check("C5 重置后客户机重新起来（Tools 掉线又回来）", seen_down and running,
              f"{row.get('power')} / {row.get('tools_running')}")

    # D. 审计
    items = audit_lines()
    actions = {i.get("action") for i in items}
    check("D1 审计里有 poweroff 与 reset 记录",
          {"poweroff", "reset"} <= actions, str(sorted(actions)))
    sample = next((i for i in items if i.get("action") == "poweroff"), {})
    need = {"ts", "host_id", "host", "vm", "moref", "action", "label", "force", "ok", "error", "seconds"}
    check("D2 审计字段齐全", need <= set(sample), str(sorted(set(sample))))
    check("D3 失败记录也留痕（ok=false 且带 error）",
          any(i.get("ok") is False and i.get("error") for i in items),
          str(sum(1 for i in items if i.get("ok") is False)))
    check("D4 force 标记为 true 且常规操作不标", 
          all(i.get("force") is True for i in items if i.get("action") in {"poweroff", "reset"}),
          str(len(items)))

    print(f"\n{len(PASS)} PASS / {len(FAIL)} FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    if "--base" in sys.argv:
        BASE = sys.argv[sys.argv.index("--base") + 1]
    sys.exit(main())
