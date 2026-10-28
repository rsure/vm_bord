# 后端用 Python + pyvmomi，而不是 Node + vSphere SDK

选型不是偏好问题，是这三台机器已经把答案试出来了。三条硬事实：

1. 三台 ESXi 的 22 端口全关，只有 443 的 SOAP API 可用，任何方案都得走 vSphere Web Services。
2. SOAP 有几个非标准坑已经踩平：`GET /sdk` 返回 404 是正常的（端点只认 POST）；`RetrieveServiceContent` 必须带 `<vim25:_this type="ServiceInstance">ServiceInstance</vim25:_this>`，否则报 `Expected <_this> tag not found`；证书是自签的必须跳过校验。
3. 79 台虚拟机的完整采集已经在 `pyvmomi` 上跑通，字段、属性收集、启停任务的返回结构都验过了。

换 Node 的 vSphere SDK 意味着把上面全部重新验一遍，还要额外赌它对 ESXi 6.7 的兼容性，收益为零。前端则不引入构建链：单文件 HTML + 原生 JS，由 Python 直接吐出。

## Consequences

- 依赖只剩 `pyvmomi` 一个非标准库，其余全用标准库，装机成本接近于零。
- 与 DSH 启动器插件（Node 侧）之间通过 HTTP 通信，不共享进程 —— 这也是启动器能被单独重启的前提。
- 前端零构建的代价是放弃框架生态；对这个体量（一张表 + 三张卡 + 一个抽屉）是划算的。
