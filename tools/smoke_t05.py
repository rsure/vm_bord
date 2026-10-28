"""票 05 冒烟：主机编辑（密码留空不改）、移除确认、离线卡片、证书指纹变更提示。

真机只做无害改动（改备注名、改回原名），另用一台不可达地址做离线与移除实验。
用法：python tools/smoke_t05.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import store  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def call(path: str, payload=None, method: str | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def inventory(fresh: bool = False) -> dict:
    if fresh:
        time.sleep(11)          # 等 10 秒缓存过期
    _, data = call("/api/inventory")
    return data


def main() -> int:
    _, data = call("/api/hosts")
    hosts = data["hosts"]
    check("0 起步：清单里有 3 台真机", len(hosts) == 3, f"{len(hosts)} 台")
    if len(hosts) != 3:
        return 1
    target = hosts[0]
    print(f"编辑对象：{target['name']}  {target['host']}:{target['port']}")

    # A. 编辑：密码留空不该覆盖
    new_name = target["name"] + "-改"
    status, data = call(f"/api/hosts/{target['id']}", {
        "name": new_name, "host": target["host"], "port": target["port"],
        "user": target["user"], "password": "",
    }, method="PUT")
    check("A1 密码留空的编辑能保存成功", status == 200 and data["host"]["name"] == new_name,
          str(status))
    _, hosts_now = call("/api/hosts")
    row = next((h for h in hosts_now["hosts"] if h["id"] == target["id"]), {})
    check("A2 名称已改、密码仍在（has_password=true）",
          row.get("name") == new_name and row.get("has_password") is True, json.dumps(row, ensure_ascii=False)[:120])

    inv = inventory(fresh=True)
    item = next((i for i in inv["items"] if i["id"] == target["id"]), {})
    check("A3 密码没被清空：改完后这台主机还能采集",
          bool(item.get("stats")) and not item.get("error"),
          (item.get("error") or {}).get("message", "stats ok"))

    # 改回原名，顺便确认再改一次也正常
    status, data = call(f"/api/hosts/{target['id']}", {"name": target["name"]}, method="PUT")
    check("A4 能改回原名（只传 name，其余字段沿用）",
          status == 200 and data["host"]["name"] == target["name"], str(status))

    # 密码填错：必须 400，且配置不变
    status, data = call(f"/api/hosts/{target['id']}", {
        "name": "不该被写进去的名字", "password": "wrong-password-zz",
    }, method="PUT")
    check("A5 错误密码编辑被拒（400 kind=auth）",
          status == 400 and (data.get("error") or {}).get("kind") == "auth",
          f"{status} {(data.get('error') or {}).get('message')}")
    _, hosts_now = call("/api/hosts")
    row = next((h for h in hosts_now["hosts"] if h["id"] == target["id"]), {})
    check("A6 被拒的编辑没有落盘（名字没被污染）", row.get("name") == target["name"], row.get("name"))

    # B. 加一台坏主机，然后移除它；其余主机不受影响
    bad = store.add_host({"name": "zz-冒烟-坏机", "host": "192.168.2.199", "port": 443,
                          "user": "root", "password": "x"}, None)
    check("B1 注入一台不可达主机成功", bool(bad.get("id")), bad.get("host"))

    status, data = call(f"/api/hosts/{bad['id']}/delete", {"confirm": "写错了"}, method="POST")
    check("B2 确认地址不符 → 400 confirm", status == 400, str(status))
    _, hosts_now = call("/api/hosts")
    check("B3 确认不符时主机还在", any(h["id"] == bad["id"] for h in hosts_now["hosts"]))

    inv = inventory(fresh=True)
    bad_item = next((i for i in inv["items"] if i["id"] == bad["id"]), {})
    others = [i for i in inv["items"] if i["id"] != bad["id"]]
    check("B4 离线主机卡片带可读原因", bool(bad_item.get("error")),
          str((bad_item.get("error") or {}).get("kind")))
    check("B5 一台离线不影响其它主机", all(i.get("stats") for i in others), f"{len(others)} 台正常")

    status, data = call(f"/api/hosts/{bad['id']}/delete", {"confirm": "192.168.2.199"}, method="POST")
    check("B6 地址确认正确 → 移除成功", status == 200, str(status))
    _, hosts_now = call("/api/hosts")
    check("B7 移除后清单回到 3 台", len(hosts_now["hosts"]) == 3, f"{len(hosts_now['hosts'])} 台")

    inv = inventory(fresh=True)
    check("B8 移除一台后其余主机采集与虚拟机列表照常",
          len(inv["items"]) == 3 and len(inv["vms"]) >= 70,
          f"{len(inv['items'])} 主机 / {len(inv['vms'])} 台虚拟机")

    # C. 证书指纹变了要提示
    doc = json.loads(store.HOSTS_FILE.read_text(encoding="utf-8"))
    victim = next(h for h in doc["hosts"] if h["id"] == target["id"])
    original_fp = victim["fingerprint"]
    victim["fingerprint"] = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
    store.HOSTS_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    inv = inventory(fresh=True)
    item = next((i for i in inv["items"] if i["id"] == target["id"]), {})
    check("C1 指纹与记录不一致时卡片能拿到提示标记",
          item.get("fingerprint_changed") is True,
          f"now={str(item.get('fingerprint_now'))[:17]} stored={str(item.get('fingerprint'))[:17]}")

    doc = json.loads(store.HOSTS_FILE.read_text(encoding="utf-8"))
    for h in doc["hosts"]:
        if h["id"] == target["id"]:
            h["fingerprint"] = original_fp
    store.HOSTS_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    inv = inventory(fresh=True)
    item = next((i for i in inv["items"] if i["id"] == target["id"]), {})
    check("C2 指纹恢复后提示消失", item.get("fingerprint_changed") is False, str(item.get("fingerprint_changed")))

    print(f"\n{len(PASS)} PASS / {len(FAIL)} FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
