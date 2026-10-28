"""Linux/macOS 上密码加密（Fernet 分支）的回归测试。

背景：`_fernet()` 曾经在持有 `_key_lock` 的情况下调用 `machine_key()`，
而 `machine_key()` 自己也要拿同一把**非重入锁** —— 于是第一次接主机就
整进程挂死：py-spy 看到所有线程都停在 `store.py` 的 `machine_key` 里。
Windows 走 DPAPI、根本不经过 Fernet，所以 Windows 上的冒烟测不出来，
这个用例必须在 Linux 上跑。

用法：python tools/smoke_secrets.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def run_with_watchdog(fn, seconds: float = 5.0):
    """在线程里跑 fn，超时就判定卡死（自死锁时 join 永远不返回）。"""
    box: dict = {}

    def worker():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 要把异常带回主线程
            box["error"] = exc
            box["trace"] = traceback.format_exc()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(seconds)
    return (not thread.is_alive()), box


def main() -> int:
    if sys.platform.startswith("win"):
        print("SKIP  Windows 用 DPAPI 加密，不经过 Fernet 分支")
        return 0

    try:
        from cryptography.fernet import Fernet  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP  没装 cryptography：{exc}")
        return 0

    import store

    tmp = Path(tempfile.mkdtemp(prefix="vmbord-secret-"))
    store.DATA_DIR = tmp
    store.KEY_FILE = tmp / "secret.key"
    store.HOSTS_FILE = tmp / "hosts.json"
    store.AUDIT_FILE = tmp / "audit.jsonl"
    store._fernet_cache["obj"] = None

    # 1) 单线程：加密/解密往返，不能卡住
    alive, box = run_with_watchdog(lambda: (
        store.encrypt_secret("pw-123"),
        store.decrypt_secret(store.encrypt_secret("pw-123")),
    ))
    check("加密+解密不卡死（自死锁回归）", alive,
          "线程 5 秒没返回，说明 _key_lock 又被自己套住了" if not alive else "")
    if not alive:
        stuck = [t.name for t in threading.enumerate() if t.is_alive() and t is not threading.main_thread()]
        print(f"\n以下线程 5 秒没返回，基本就是又自死锁了：{stuck}")
        print("排查手段：py-spy dump --pid <服务进程> 看是不是都停在 store.machine_key")
        return 1

    if "error" in box:
        print(box["trace"])
        check("加密过程不抛异常", False, repr(box["error"]))
        return 1

    blob, back = box["value"]
    check("密文带 fernet:v1: 前缀", blob.startswith(store.FERNET_PREFIX), blob[:20])
    check("解回来是原值", back == "pw-123", back)
    check("密钥文件权限 0600", (store.KEY_FILE.stat().st_mode & 0o777) == 0o600,
          oct(store.KEY_FILE.stat().st_mode & 0o777))
    check("密钥文件不落明文密码", b"pw-123" not in store.KEY_FILE.read_bytes())

    # 2) 并发：多个线程同时首次加密，不能互相等到死
    store._fernet_cache["obj"] = None
    results: list = []

    def concurrent():
        results.append(store.decrypt_secret(store.encrypt_secret("pw-456")))

    threads = [threading.Thread(target=concurrent, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(8)
    check("4 线程并发加密不卡死", all(not t.is_alive() for t in threads))
    check("并发结果都正确", results == ["pw-456"] * 4, str(results))

    # 3) 明文校验：不同密码密文不同
    a = store.encrypt_secret("aaa")
    b = store.encrypt_secret("bbb")
    check("相同密钥下密文互不相同", a != b)

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} 项通过")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
