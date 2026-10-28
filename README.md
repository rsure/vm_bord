# vm_bord

ESXi 虚拟机巡检看板 —— 一个只监听本机的小服务，把三台 ESXi 单机上的 79 台虚拟机收进一页表格，支持搜索、排序、刷新（手动或每 60 秒自动）和开关机。

## 为什么有这个东西

三台 ESXi（`192.168.2.152` / `.153` / `.154`）平时要分别登录三个 Host Client 页面来回切。这个看板把三台合成一页：顶部是主机资源卡，中间是全部虚拟机的聚合表格，能就地开机、优雅关机、优雅重启、强制断电。

三台的 22 端口（SSH）都是关闭的，只有 443 上的 vSphere Web Services API 可用，所以全程走 SOAP API，不登机器跑命令。

## 界面

一页三段，不分标签页：

1. **主机资源卡**（每台 ESXi 一张）：版本与 build、主机名、CPU 型号/核数/主频/使用率、内存总量与已用、各数据存储总量与剩余、虚拟机总数与开机数、运行时长、证书指纹、Host Client 外链。
2. **虚拟机聚合表**（全部主机合并）：名称、所属主机、IP、操作系统、电源状态、配置内存（运行中括号附实时占用）、磁盘合计、VMware Tools 状态、操作按钮。点行展开只读详情（CPU 核数、VMX 路径、UUID、开机时间）。
3. **设置抽屉**（右上角）：接入新主机、编辑已有主机、移除主机（二次确认）、离线与证书变更提示。

> 截图留在本地 `.scratch/`（含内网客户环境名与地址），没有放进公开仓库。

## 能力

| 能力 | 说明 |
|---|---|
| 主机接入 | 填地址/端口/账号/密码，**保存前先真连一次**，失败按"拒绝连接 / 超时 / 地址解析 / TLS 握手 / 账号密码 / 权限"给出可读原因 |
| 密码存储 | Windows DPAPI 加密后落 `data/hosts.json`，机器+用户绑定，页面永不回显密码 |
| 证书指纹 | 首次连接记录 SHA256 指纹；日后指纹变了卡片上直接报警（不是默默重连） |
| 聚合表格 | 三台并发采集，单台挂了只影响自己那张卡，不拖垮整页 |
| 搜索 / 排序 | 按名称或 IP 即时过滤；名称、电源、内存、磁盘可切升降序 |
| 刷新 | 手动刷新 + 每 60 秒自动刷新（后台标签页、电源操作进行中、抽屉或弹窗打开时跳过）+ "最后更新于"，10 秒内重复刷新走缓存 |
| 电源操作 | 开机、优雅关机、优雅重启（需 Tools 在跑）；强制断电、强制重置收在 `⋯` 二级菜单，必须勾选确认并输入虚拟机名 |
| 审计 | 每次电源操作（含失败）追加一行到 `data/audit.jsonl`，`GET /api/audit` 可查 |
| DSH 启动器 | DSH Desktop 侧栏一个「VM 看板」按钮，可启停服务并在浏览器打开 |
| 深色模式 | 跟随系统 |

## 快速开始

```powershell
# 1. 装依赖（唯一非标准库依赖）
pip install -r requirements.txt

# 2. 启动
start.bat            # 或 python server\app.py --open
```

浏览器会打开 `http://127.0.0.1:8787`（8787 被占用时自动顺延到 8788/8789，页面右上角标签显示实际端口）。第一次打开是空的，在「设置」里接入一台 ESXi 即可。

也可以直接双击桌面快捷方式 `vm_bord 虚拟机看板.lnk`。

## 部署到 Linux 服务器（CentOS 7 验证）

服务器上同一份代码，用 `--host 0.0.0.0` 对外监听，并**必须**加 `--auth` 开登录：

```bash
sudo mkdir -p /usr/local/hk/tools/vmbord
# 把仓库内容放到上面这个目录后：
sudo VMBORD_AUTH='admin:admin@123' bash deploy/install.sh 8787
```

`deploy/install.sh` 会依次做：检查/安装 `python3`、建 `venv`、装依赖、把口令写进 `/etc/vm-bord.env`（0600）、
安装 `vm-bord.service` 并 `enable --now`、最后做一次健康检查。之后：

```bash
systemctl status vm-bord      # 状态
journalctl -u vm-bord -f      # 日志
systemctl restart vm-bord     # 重启（改代码后）
```

CentOS 7 官方源的 `python3` 只有 3.6.8，而本程序用到了 `from __future__ import annotations`（3.7+），
所以**不能用系统那个 python3**；SCL 源也已随 CentOS 7 EOL 失效。实践中用的是自带的 Miniconda：

```bash
curl -fsSL -o /tmp/miniconda.sh \
  https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-py38_4.12.0-Linux-x86_64.sh
bash /tmp/miniconda.sh -b -p /usr/local/hk/tools/miniconda3   # 新版要求 glibc>=2.28，必须用 py38_4.12.0
sudo VMBORD_AUTH='admin:admin@123' bash deploy/install.sh 8787
```

`install.sh` 会自动挑选 >=3.8 的解释器（含上面这个路径），也可以用 `VMBORD_PYTHON=/path/to/python3` 指定。
`requirements.txt` 里用环境标记处理版本差异：Python <3.10 装 `pyvmomi 8.x`（9.x 起要求 3.10+）。

Linux 上密码不再用 DPAPI（Windows 专有），改成 `data/secret.key`（0600）+ Fernet；
`hosts.json` 里老 Windows 密文解不开是正常的，**服务器上需要重新录一次 ESXi 密码**。

## 实测数据（2026-10-28，三台真机）

| 动作 | 实测 |
|---|---|
| 三台并发冷采集（79 台虚拟机） | 0.58～1.52 秒 |
| 命中 10 秒缓存 | 0.002 秒 |
| 优雅关机（CentOS 7，8 核 16 GB） | 93～95 秒 |
| 开机 | 2～3 秒 |
| 优雅重启 | 105 秒 |
| 强制断电 / 强制重置 | 约 1.5 秒 |

优雅操作慢是客户机自己关机/重启的耗时，不是接口慢。重启的判定不能看电源状态（全程 `poweredOn`），也不能看 `runtime.bootTime`（它由客户机时钟推算，客户机时钟偏了就会自己跳），只能看"Tools 掉线后又回来"。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务版本、实际端口、是否开了登录（**不需要登录**） |
| GET | `/login` | 登录页（不需要登录） |
| POST | `/api/login` | 登录，成功发 `vmbord_sid` 签名 cookie（12 小时） |
| POST | `/api/logout` | 退出并清 cookie |
| GET | `/api/hosts` | 已接入主机（不含密码） |
| POST | `/api/hosts` | 接入主机（先 probe 再落盘） |
| PUT | `/api/hosts/<id>` | 编辑主机（密码留空 = 不改） |
| POST | `/api/hosts/<id>/delete` | 移除主机（须 `confirm` 输入地址） |
| GET | `/api/inventory` | 全部主机卡片 + 聚合虚拟机列表（10 秒缓存） |
| POST | `/api/vms/power` | 电源操作，异步任务，202 返回 task id |
| GET | `/api/tasks/<id>` | 查询电源任务（进行中 / 成功 / 失败原因） |
| GET | `/api/audit` | 审计流水（新的在前） |
| POST | `/api/shutdown` | 优雅退出（给启动器用） |

## 目录

```
vm_bord/
├── server/
│   ├── app.py           HTTP 路由、并发采集、缓存、电源任务、CORS、登录鉴权
│   ├── esxi.py          vSphere 访问层（连接、采集、电源操作）
│   └── store.py         主机清单持久化 + 密码加密（DPAPI / Fernet）+ 审计
├── web/
│   └── index.html       看板页面（单文件、零构建、原生 JS）
├── deploy/
│   ├── install.sh       Linux 一键安装（依赖 + venv + systemd + 健康检查）
│   └── vm-bord.service  systemd 单元模板
├── dsh-plugin/          DSH Desktop 侧栏启动器面板（宿主 + 浏览器两个半区）
├── tools/               冒烟与测量脚本
├── data/                运行时数据（不入库）
├── docs/                需求总结、术语表、决策记录
├── start.bat
└── README.md
```

## 环境

- Windows：Python 3.10+，密码加密用系统 DPAPI
- Linux / macOS：Python 3.6+，密码加密用 `data/secret.key` + Fernet（需 `cryptography`）
- `pyvmomi`（唯一非标准库依赖）
- 可选：DSH Desktop（要侧栏启动器按钮才需要）

## 安全

- 默认只监听 `127.0.0.1`；对外部署时用 `--host 0.0.0.0` 并务必配 `--auth 账号:密码`。
- 开了登录后，除 `/login` 与 `/api/health` 外全都要求会话 cookie（HMAC 签名、12 小时、HttpOnly）；同一 IP 连续 5 次密码错误锁 60 秒。
- 跨源请求只放行 `Origin` 为本机回环地址的，别的来源一律不给 CORS 头。
- 密码只以密文存在 `hosts.json`，解密密钥留在本机（DPAPI 或 `data/secret.key`）。
- `data/` 不进版本库；审计记录同样只在本机。

## 文档

- [需求总结](docs/需求总结.md) —— 范围、功能点、验收标准、不做什么
- [术语表](GLOSSARY.md) —— 主机 / 虚拟机 / 优雅关机 / 强制断电 等口径
- 决策记录
  - [0001 独立本地页面与 DSH 启动器壳](docs/adr/0001-独立本地页面与DSH启动器壳.md)
  - [0002 Python + pyvmomi](docs/adr/0002-python-pyvmomi.md)
- 开发票档：`.scratch/dashboard/issues/`

## 现状

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 需求确认 + 仓库骨架 | 完成 |
| M2 | 本地服务 + 看板页面 | 完成 |
| M3 | 真实开关机验证 | 完成 |
| M4 | DSH 启动器面板 | 代码完成，待重启 DSH 目视确认 |
| M5 | 收尾与推送 | 进行中 |
