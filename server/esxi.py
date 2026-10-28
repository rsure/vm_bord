"""vSphere 访问层：登录、证书指纹、主机资源采集。

三台 ESXi 的 SSH 是关的，只能走 443 上的 vSphere Web Services API，
所以这里全部是 SOAP 调用，不涉及任何 shell。
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import time

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
    if isinstance(exc, vim.fault.ToolsUnavailable):
        return EsxiError("tools", "客户机 VMware Tools 没在运行，无法优雅操作")
    if isinstance(exc, vim.fault.InvalidPowerState):
        return EsxiError("state", f"当前电源状态不允许这个操作：{text}")
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


# 一次 PropertyCollector 调用把表里要用的字段全取回来。逐个属性访问是 79 台 × 十几个
# 来回，慢到不可用；批量取是常数次往返。
VM_PROPS = [
    "name",
    "runtime.powerState",
    "runtime.bootTime",
    "config.instanceUuid",
    "config.uuid",
    "config.guestFullName",
    "config.guestId",
    "config.hardware.memoryMB",
    "config.hardware.numCPU",
    "config.hardware.device",
    "config.files.vmPathName",
    "guest.ipAddress",
    "guest.toolsStatus",
    "guest.toolsRunningStatus",
    "summary.quickStats.guestMemoryUsage",
]


def _flatten(prop_set, mo_id: str) -> dict:
    """把 "config.hardware.memoryMB" 这种扁平路径还原成嵌套字典。"""
    row: dict = {"_moId": mo_id}
    for prop in prop_set:
        cursor = row
        parts = prop.name.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = prop.val
    return row


def _retrieve_vms(si, hs) -> list[dict]:
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(hs, [vim.VirtualMachine], True)
    try:
        spec = vim.PropertyCollector.PropertySpec(
            type=vim.VirtualMachine, all=False, pathSet=VM_PROPS
        )
        obj_spec = vim.PropertyCollector.ObjectSpec(
            obj=view,
            skip=True,
            selectSet=[
                vim.TraversalSpec(
                    name="traverse", type=vim.view.ContainerView, path="view", skip=False
                )
            ],
        )
        filter_spec = vim.PropertyCollector.FilterSpec(propSet=[spec], objectSet=[obj_spec])
        options = vim.PropertyCollector.RetrieveOptions()
        result = content.propertyCollector.RetrievePropertiesEx(
            specSet=[filter_spec], options=options
        )
        return [_flatten(obj.propSet, obj.obj._moId) for obj in (result.objects if result else [])]
    finally:
        view.Destroy()


def _local_str(value) -> str | None:
    """vSphere 给的是 UTC 时间，页面上要按本机时区显示。"""
    if not value:
        return None
    try:
        if getattr(value, "tzinfo", None) is not None:
            value = value.astimezone()
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return None


def _vm_row(raw: dict) -> dict:
    cfg = raw.get("config") or {}
    hw = cfg.get("hardware") or {}
    guest = raw.get("guest") or {}
    runtime = raw.get("runtime") or {}
    quick = (raw.get("summary") or {}).get("quickStats") or {}
    files = cfg.get("files") or {}

    disk_bytes = 0
    for device in hw.get("device") or []:
        if isinstance(device, vim.vm.device.VirtualDisk):
            disk_bytes += int(getattr(device, "capacityInBytes", 0) or 0)

    power = runtime.get("powerState") or "unknown"
    tools = guest.get("toolsStatus")
    # 关机机的 guestFullName 常常是空的，回退到配置里的客户机类型，别留白。
    os_name = cfg.get("guestFullName") or cfg.get("guestId") or None
    boot = runtime.get("bootTime")
    used_mb = quick.get("guestMemoryUsage")

    return {
        "moref": raw.get("_moId"),
        "name": raw.get("name"),
        "ip": guest.get("ipAddress") or None,
        "os": os_name,
        "os_id": cfg.get("guestId") or None,
        "power": power,
        "tools": tools,
        "tools_running": guest.get("toolsRunningStatus"),
        "num_cpu": hw.get("numCPU"),
        "mem_mb": hw.get("memoryMB"),
        "mem_used_mb": used_mb if power == "poweredOn" and tools == "toolsOk" else None,
        "disk_gb": round(disk_bytes / 1024 / 1024 / 1024, 1),
        "vmx": files.get("vmPathName") or None,
        "uuid": cfg.get("instanceUuid") or cfg.get("uuid") or None,
        "boot_time": _local_str(boot),
    }


def _collect_vms(si, hs) -> list[dict]:
    rows = [_vm_row(raw) for raw in _retrieve_vms(si, hs)]
    rows.sort(key=lambda r: (r.get("name") or "").lower())
    return rows


def collect_host(entry: dict, password: str, include_vms: bool = False) -> dict:
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

        card = {
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
        if include_vms:
            card["vms"] = _collect_vms(si, hs)
        return card
    except EsxiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _friendly(exc) from exc
    finally:
        Disconnect(si)


# --------------------------------------------------------------------------
# 电源操作
# --------------------------------------------------------------------------

POWER_LABEL = {"on": "开机", "shutdown": "优雅关机", "reboot": "优雅重启"}
POWER_TIMEOUT = {"on": 120.0, "shutdown": 180.0, "reboot": 240.0}


def _vm_by_moref(content, moref: str) -> vim.VirtualMachine:
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        for vm in view.view:
            if getattr(vm, "_moId", None) == moref:
                return vm
    finally:
        view.Destroy()
    raise EsxiError("not_found", f"找不到这台虚拟机（{moref}），可能刚被删除")


_STATE_PROPS = [
    "runtime.powerState",
    "runtime.bootTime",
    "guest.ipAddress",
    "guest.toolsStatus",
    "guest.toolsRunningStatus",
]


def _read_state(content, vm) -> dict:
    """每次都向 vSphere 要一份新鲜属性。

    直接读 `vm.runtime.bootTime` 这类属性会拿到 managed object 上的旧快照；更要命的是
    Tools 上报客户机时间时 bootTime 会自己跳一下（实测同一台机器从 11:40:17 跳到
    11:40:47 却没有真重启），拿它判断"是否重启过"会误判。所以这里一律走
    PropertyCollector 重新取。
    """
    spec = vim.PropertyCollector.PropertySpec(
        type=vim.VirtualMachine, all=False, pathSet=_STATE_PROPS
    )
    object_spec = vim.PropertyCollector.ObjectSpec(obj=vm)
    filter_spec = vim.PropertyCollector.FilterSpec(propSet=[spec], objectSet=[object_spec])
    result = content.propertyCollector.RetrievePropertiesEx(
        specSet=[filter_spec], options=vim.PropertyCollector.RetrieveOptions()
    )
    raw = {}
    for entry in (result.objects if result else []):
        for prop in entry.propSet:
            raw[prop.name] = prop.val
    boot = raw.get("runtime.bootTime")
    return {
        "power": raw.get("runtime.powerState"),
        "tools": raw.get("guest.toolsStatus"),
        "tools_running": raw.get("guest.toolsRunningStatus"),
        "ip": raw.get("guest.ipAddress") or None,
        "boot_time": _local_str(boot),
        "boot_raw": boot,
    }


def _public_state(state: dict) -> dict:
    """去掉内部用的原始时间对象，只留页面要显示的字段。"""
    return {key: value for key, value in state.items() if key != "boot_raw"}


def _fault_message(err) -> str:
    message = getattr(err, "localizedMessage", None) or getattr(err, "msg", None)
    return message or str(err) or "vSphere 没有给出原因"


def _wait_task(task, timeout: float) -> None:
    deadline = time.time() + timeout
    while True:
        state = task.info.state
        if state == "success":
            return
        if state == "error":
            err = task.info.error
            kind = "tools" if isinstance(err, vim.fault.ToolsUnavailable) else "task"
            raise EsxiError(kind, _fault_message(err))
        if time.time() > deadline:
            raise EsxiError("timeout", f"等任务完成超过 {int(timeout)} 秒，请刷新确认结果")
        time.sleep(0.5)


def _wait_power(content, vm, done, timeout: float, label: str) -> dict:
    deadline = time.time() + timeout
    while True:
        try:
            state = _read_state(content, vm)
        except Exception:  # noqa: BLE001 - 开关机过程中短暂读不到属性是正常的
            state = {}
        if state and done(state):
            return state
        if time.time() > deadline:
            raise EsxiError("timeout", f"{label}还没完成（已等 {int(timeout)} 秒），请刷新确认结果")
        time.sleep(2.0)


def power_op(entry: dict, password: str, moref: str, action: str) -> dict:
    """开机 / 优雅关机 / 优雅重启。

    优雅路径依赖客户机里的 VMware Tools：Tools 没跑就直接拒绝，而不是偷偷降级成
    强制断电 —— 强制断电是另一件需要二次确认的事。
    """
    if action not in POWER_LABEL:
        raise EsxiError("bad_action", f"不认识的操作：{action}")

    si = connect(
        entry["host"], int(entry.get("port", DEFAULT_PORT)),
        entry.get("user") or "root", password,
    )
    try:
        content = si.RetrieveContent()
        vm = _vm_by_moref(content, moref)
        state = _read_state(content, vm)
        power = state["power"]

        if action == "on":
            if power == "poweredOn":
                raise EsxiError("state", "这台虚拟机已经是开机状态")
            _wait_task(vm.PowerOnVM_Task(), POWER_TIMEOUT["on"])
            state = _wait_power(
                content, vm, lambda s: s["power"] == "poweredOn", 30.0, "开机"
            )

        elif action == "shutdown":
            if power != "poweredOn":
                raise EsxiError("state", "这台虚拟机没有在运行，不需要关机")
            if state["tools_running"] != "guestToolsRunning":
                raise EsxiError("tools", "客户机 VMware Tools 没在运行，无法优雅关机，只能用强制断电")
            vm.ShutdownGuest()
            state = _wait_power(
                content, vm, lambda s: s["power"] != "poweredOn",
                POWER_TIMEOUT["shutdown"], "关机",
            )

        else:
            if power != "poweredOn":
                raise EsxiError("state", "这台虚拟机没有在运行，不需要重启")
            if state["tools_running"] != "guestToolsRunning":
                raise EsxiError("tools", "客户机 VMware Tools 没在运行，无法优雅重启")
            vm.RebootGuest()
            # 判断"重启完成"只能看 Tools 掉线后又回来：优雅重启时虚拟机全程 poweredOn，
            # 电源状态不会翻；而 bootTime 是客户机时钟推算出来的（这台机器时钟跳得厉害，
            # 实测没重启也能从 11:40:47 跳到 11:42:48），不能当依据。
            deadline = time.time() + POWER_TIMEOUT["reboot"]
            seen_down = False
            while True:
                try:
                    state = _read_state(content, vm)
                except Exception:  # noqa: BLE001
                    state = {}
                if state:
                    running = (state["power"] == "poweredOn"
                               and state["tools_running"] == "guestToolsRunning")
                    if not running:
                        seen_down = True
                    elif seen_down:
                        break
                if time.time() > deadline:
                    raise EsxiError(
                        "timeout",
                        f"重启还没完成（已等 {int(POWER_TIMEOUT['reboot'])} 秒），请刷新确认结果",
                    )
                time.sleep(2.0)

        return {"action": action, "label": POWER_LABEL[action], "vm": _public_state(state)}
    except EsxiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _friendly(exc) from exc
    finally:
        Disconnect(si)
