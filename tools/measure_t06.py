"""票 06：真机往返（优雅关机 → 确认关机 → 开机 → 等 Tools 就绪）+ 全量刷新耗时。

结果写 .scratch/t06.json，供验收记录引用。
用法：python tools/measure_t06.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
TARGET = "165-东安实业测试"
OUT = pathlib.Path(__file__).resolve().parent.parent / ".scratch" / "t06.json"
CACHE_TTL = 10.0


def call(path: str, payload=None, method: str | None = None, timeout: float = 60.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body, round(time.time() - t0, 3)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}"), round(time.time() - t0, 3)


def find_vm() -> dict:
    _, data, _ = call("/api/inventory")
    for vm in data["vms"]:
        if vm["name"] == TARGET:
            return vm
    raise SystemExit(f"找不到 {TARGET}")


def state(moref: str, fresh: bool = True) -> dict:
    if fresh:
        time.sleep(CACHE_TTL + 0.6)     # 绕开 10 秒缓存，拿真值
    _, data, _ = call("/api/inventory")
    for row in data["vms"]:
        if row["moref"] == moref:
            return row
    return {}


def wait_task(task_id: str, limit: float = 300.0) -> tuple[dict, float]:
    t0 = time.time()
    while time.time() - t0 < limit:
        time.sleep(1.5)
        _, data, _ = call("/api/tasks/" + task_id)
        task = data.get("task") or {}
        if task.get("state") != "running":
            return task, round(time.time() - t0, 1)
    return {"state": "timeout"}, round(time.time() - t0, 1)


def main() -> int:
    vm = find_vm()
    print(f"目标：{vm['name']} moref={vm['moref']} 电源={vm['power']} tools={vm['tools_running']}")
    payload = {"host_id": vm["host_id"], "moref": vm["moref"], "name": vm["name"]}
    result: dict = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "vm": {"name": vm["name"], "moref": vm["moref"], "host": vm["host_addr"],
               "ip": vm["ip"], "os": vm["os"], "num_cpu": vm["num_cpu"], "mem_mb": vm["mem_mb"]},
        "steps": [],
        "perf": {},
    }

    def step(name: str, **kwargs) -> None:
        row = {"step": name, "at": time.strftime("%H:%M:%S"), **kwargs}
        result["steps"].append(row)
        print(f"  {name}: " + json.dumps(kwargs, ensure_ascii=False))

    if vm["power"] != "poweredOn":
        print("目标不在开机状态，先开机")
        _, data, _ = call("/api/vms/power", dict(payload, action="on"))
        wait_task(data["task"]["id"])

    # 1. 优雅关机
    t0 = time.time()
    status, data, _ = call("/api/vms/power", dict(payload, action="shutdown"))
    task, waited = wait_task(data["task"]["id"])
    step("优雅关机 任务完成", http=status, task_state=task.get("state"),
         message=task.get("message"), task_seconds=waited,
         wall_seconds=round(time.time() - t0, 1))
    row = state(vm["moref"])
    step("确认电源状态", power=row.get("power"), tools=row.get("tools_running"))

    # 2. 开机
    t0 = time.time()
    status, data, _ = call("/api/vms/power", dict(payload, action="on"))
    task, waited = wait_task(data["task"]["id"])
    step("开机 任务完成", http=status, task_state=task.get("state"),
         message=task.get("message"), task_seconds=waited,
         wall_seconds=round(time.time() - t0, 1))

    # 3. 等 Tools 就绪
    t0 = time.time()
    tools_at = None
    deadline = time.time() + 240
    while time.time() < deadline:
        row = state(vm["moref"])
        if row.get("power") == "poweredOn" and row.get("tools_running") == "guestToolsRunning":
            tools_at = round(time.time() - t0, 1)
            break
    step("Tools 就绪", seconds_since_power_on=tools_at,
         power=row.get("power"), tools=row.get("tools_running"), ip=row.get("ip"))

    # 4. 全量刷新耗时（冷采集，绕开 10 秒缓存）
    cold = []
    for _ in range(3):
        time.sleep(CACHE_TTL + 0.6)
        status, data, took = call("/api/inventory")
        cold.append({"seconds": took, "hosts": len(data.get("items", [])),
                     "vms": len(data.get("vms", [])), "cached": data.get("cached")})
        print(f"  冷采集: {took}s  主机 {cold[-1]['hosts']}  虚拟机 {cold[-1]['vms']}")
    result["perf"]["cold_inventory"] = cold

    # 5. 缓存命中耗时
    _, _, took = call("/api/inventory")
    result["perf"]["warm_inventory"] = {"seconds": took}
    print(f"  缓存命中: {took}s")

    # 6. 证书指纹校验耗时（直连一次 TLS）
    result["perf"]["cold_min"] = min(c["seconds"] for c in cold)
    result["perf"]["cold_max"] = max(c["seconds"] for c in cold)
    result["perf"]["under_5s"] = result["perf"]["cold_max"] <= 5.0
    print(f"  冷采集区间 {result['perf']['cold_min']}s ~ {result['perf']['cold_max']}s  "
          f"（要求 ≤5s：{'达标' if result['perf']['under_5s'] else '不达标'}）")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
