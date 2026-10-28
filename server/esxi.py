"""vSphere 访问层：登录、证书指纹、主机资源采集。

三台 ESXi 的 SSH 是关的，只能走 443 上的 vSphere Web Services API，
所以这里全部是 SOAP 调用，不涉及任何 shell。
"""

from __future__ import annotations

import hashlib
import socket
import ssl

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

DEFAULT_PORT = 443
CONNECT_TIMEOUT = 10


class EsxiError(Exception):
    """带上可读分类的失败，前端直接把它显示出来。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _ssl_context() -> ssl.SSLContext:
    # ESXi 用自签证书，链校验必然失败；指纹由 cert_fingerprint 单独记录。
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _friendly(exc: BaseException) -> EsxiError:
    """把 pyVmomi 抛出来的一堆类型收敛成四种可读原因。"""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, (ConnectionRefusedError,)):
        return EsxiError("refused", "连接被拒绝，目标端口可能没开")
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in low:
        return EsxiError("timeout", "连接超时，主机可能不在线")
    if isinstance(exc, socket.gaierror) or "name or service not known" in low:
        return EsxiError("dns", "地址解析失败")
    if isinstance(exc, ssl.SSLError):
        return EsxiError("cert", f"TLS 握手失败：{text}")
    if "incorrect user name or password" in low or "cannot complete login" in low:
        return EsxiError("auth", "账号或密码错误")
    if "no permission" in low:
        return EsxiError("auth", "账号没有权限")
    if isinstance(exc, vim.fault.InvalidLogin):
        return EsxiError("auth", "账号或密码错误")
    return EsxiError("other", text or exc.__class__.__name__)


def cert_fingerprint(host: str, port: int = DEFAULT_PORT, timeout: float = 6) -> str | None:
    """取服务端证书的 SHA256 指纹。失败返回 None —— 指纹是附加信息，不该挡住主流程。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            with _ssl_context().wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError:
        return None
    if not der:
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def connect(host: str, port: int, user: str, password: str):
    try:
        return SmartConnect(
            host=host,
            port=int(port),
            user=user,
            pwd=password,
            sslContext=_ssl_context(),
            connectionPoolTimeout=CONNECT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - 统一收敛成 EsxiError
        raise _friendly(exc) from exc


def _the_host(si) -> vim.HostSystem:
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    try:
        items = list(view.view)
    finally:
        view.Destroy()
    if not items:
        raise EsxiError("other", "登录成功但读不到主机对象")
    return items[0]


def probe(host: str, port: int, user: str, password: str) -> dict:
    """只验证能不能连上，用于“添加主机”时的保存前校验。"""
    si = connect(host, port, user, password)
    try:
        hs = _the_host(si)
        product = hs.summary.config.product
        return {
            "product": product.fullName if product else None,
            "version": product.version if product else None,
            "build": product.build if product else None,
            "fingerprint": cert_fingerprint(host, port),
        }
    except EsxiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _friendly(exc) from exc
    finally:
        Disconnect(si)


def _gb(value_bytes) -> float:
    return round((value_bytes or 0) / 1024 / 1024 / 1024, 1)


def collect_host(entry: dict, password: str) -> dict:
    """采集一台主机的资源概况，塞进卡片需要的形状。

    entry 来自 store，password 是解密后的明文（只在内存里活这一次调用）。
    """
    host = entry["host"]
    port = int(entry.get("port", DEFAULT_PORT))
    si = connect(host, port, entry.get("user") or "root", password)
    try:
        hs = _the_host(si)
        summary = hs.summary
        hardware = summary.hardware
        quick = summary.quickStats
        product = summary.config.product

        cores = int(getattr(hardware, "numCpuCores", 0) or 0)
        cpu_mhz = float(getattr(hardware, "cpuMhz", 0) or 0)
        cpu_used_mhz = float(getattr(quick, "overallCpuUsage", 0) or 0)
        cpu_capacity = cores * cpu_mhz
        cpu_pct = round(cpu_used_mhz / cpu_capacity * 100, 1) if cpu_capacity else None
        if cpu_pct is not None:
            cpu_pct = min(cpu_pct, 100.0)

        mem_total_mb = int(getattr(hardware, "memorySize", 0) or 0) / 1024 / 1024
        mem_used_mb = int(getattr(quick, "overallMemoryUsage", 0) or 0)
        mem_pct = round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb else None

        datastores = []
        for ds in hs.datastore or []:
            s = ds.summary
            datastores.append({
                "name": s.name,
                "capacity_gb": _gb(s.capacity),
                "free_gb": _gb(s.freeSpace),
                "accessible": bool(s.accessible),
            })
        datastores.sort(key=lambda d: d["name"])

        vms = list(hs.vm or [])
        vm_on = sum(1 for vm in vms if getattr(vm.runtime, "powerState", None) == "poweredOn")

        return {
            "ok": True,
            "error": None,
            "product": product.fullName if product else None,
            "version": product.version if product else None,
            "build": product.build if product else None,
            "api_version": product.apiVersion if product else None,
            "hostname": summary.config.name,
            "connection_state": summary.runtime.connectionState,
            "cpu_model": getattr(hardware, "cpuModel", None),
            "cpu_cores": cores,
            "cpu_mhz": round(cpu_mhz),
            "cpu_usage_pct": cpu_pct,
            "mem_total_mb": round(mem_total_mb),
            "mem_used_mb": mem_used_mb,
            "mem_usage_pct": mem_pct,
            "uptime_seconds": int(getattr(quick, "uptime", 0) or 0),
            "datastores": datastores,
            "vm_total": len(vms),
            "vm_on": vm_on,
            "fingerprint": cert_fingerprint(host, port),
        }
    except EsxiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _friendly(exc) from exc
    finally:
        Disconnect(si)
