"""主机清单的持久化。

密码不以明文落盘：用 Windows DPAPI 加密成密文，只有同一台机器的同一个
Windows 用户能解开。清单存在 data/hosts.json，对外一律不返回密码。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HOSTS_FILE = DATA_DIR / "hosts.json"
AUDIT_FILE = DATA_DIR / "audit.jsonl"

_LOCK = threading.Lock()
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _win_dlls():
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
        fn.restype = wintypes.BOOL
        fn.argtypes = [
            ctypes.POINTER(_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_BLOB),
        ]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return crypt32, kernel32


_CRYPT32, _KERNEL32 = _win_dlls()


def _blob(raw: bytes):
    """造一个指向 raw 的 DATA_BLOB；返回的 buffer 必须一直被引用住。"""
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob = _BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _take(blob: _BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _KERNEL32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def dpapi_encrypt(text: str) -> str:
    if not text:
        return ""
    src, _keep = _blob(text.encode("utf-8"))
    out = _BLOB()
    ok = _CRYPT32.CryptProtectData(
        ctypes.byref(src), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptProtectData 失败（错误码 {ctypes.get_last_error()}）")
    return base64.b64encode(_take(out)).decode("ascii")


def dpapi_decrypt(payload: str) -> str:
    if not payload:
        return ""
    src, _keep = _blob(base64.b64decode(payload))
    out = _BLOB()
    ok = _CRYPT32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData 失败（错误码 {ctypes.get_last_error()}）")
    return _take(out).decode("utf-8")


# --------------------------------------------------------------------------
# 清单读写
# --------------------------------------------------------------------------

def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read() -> dict:
    ensure_data_dir()
    if not HOSTS_FILE.exists():
        return {"version": 1, "hosts": []}
    try:
        doc = json.loads(HOSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "hosts": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("hosts"), list):
        return {"version": 1, "hosts": []}
    return doc


def _write(doc: dict) -> None:
    ensure_data_dir()
    tmp = HOSTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HOSTS_FILE)


# --------------------------------------------------------------------------
# 审计流水
# --------------------------------------------------------------------------

_audit_lock = threading.Lock()


def append_audit(record: dict) -> None:
    """一行一条、只追加。审计文件不落库、不改写、不轮转。"""
    ensure_data_dir()
    line = json.dumps(record, ensure_ascii=False)
    with _audit_lock:
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def read_audit(limit: int = 200) -> list[dict]:
    """最近 limit 条审计记录，新的在前。"""
    if not AUDIT_FILE.exists():
        return []
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


def _public(entry: dict) -> dict:
    """对外的形状：永远不带密码，只告诉调用方有没有存过密码。"""
    return {
        "id": entry.get("id"),
        "name": entry.get("name") or entry.get("host"),
        "host": entry.get("host"),
        "port": entry.get("port", 443),
        "user": entry.get("user"),
        "fingerprint": entry.get("fingerprint"),
        "added_at": entry.get("added_at"),
        "has_password": bool(entry.get("password")),
    }


def list_hosts() -> list[dict]:
    with _LOCK:
        return [_public(h) for h in _read()["hosts"]]


def get_host(host_id: str, with_secret: bool = False) -> dict | None:
    with _LOCK:
        for entry in _read()["hosts"]:
            if entry.get("id") == host_id:
                return dict(entry) if with_secret else _public(entry)
    return None


def add_host(payload: dict, fingerprint: str | None) -> dict:
    """新增一台主机。payload 里的 password 会被就地加密。"""
    with _LOCK:
        doc = _read()
        for entry in doc["hosts"]:
            if entry.get("host") == payload["host"] and int(entry.get("port", 443)) == int(payload["port"]):
                raise ValueError(f"{payload['host']} 已经接入过了")
        entry = {
            "id": "h_" + secrets.token_hex(6),
            "name": payload.get("name") or payload["host"],
            "host": payload["host"],
            "port": int(payload["port"]),
            "user": payload["user"],
            "password": dpapi_encrypt(payload.get("password") or ""),
            "fingerprint": fingerprint,
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        doc["hosts"].append(entry)
        _write(doc)
        return _public(entry)


def update_host(host_id: str, payload: dict, fingerprint: str | None = None) -> dict:
    with _LOCK:
        doc = _read()
        for entry in doc["hosts"]:
            if entry.get("id") != host_id:
                continue
            if payload.get("name"):
                entry["name"] = payload["name"]
            if payload.get("host"):
                entry["host"] = payload["host"]
            if payload.get("port"):
                entry["port"] = int(payload["port"])
            if payload.get("user"):
                entry["user"] = payload["user"]
            if payload.get("password"):
                entry["password"] = dpapi_encrypt(payload["password"])
            if fingerprint:
                entry["fingerprint"] = fingerprint
            _write(doc)
            return _public(entry)
    raise KeyError("主机不存在")


def delete_host(host_id: str) -> bool:
    with _LOCK:
        doc = _read()
        kept = [h for h in doc["hosts"] if h.get("id") != host_id]
        if len(kept) == len(doc["hosts"]):
            return False
        doc["hosts"] = kept
        _write(doc)
        return True


def secret_of(entry: dict) -> str:
    """把加密的密码解出来，只在真正去连主机的那一刻调用。"""
    return dpapi_decrypt(entry.get("password") or "")
