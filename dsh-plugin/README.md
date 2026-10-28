# vm_bord-launcher（DSH Desktop 启动器面板）

在 DSH Desktop 侧栏放一个「VM 看板」按钮，点开是个小面板：状态灯、实际端口、启动、停止、在浏览器打开。
它只负责**启停和打开**，看板功能全在 `http://127.0.0.1:8787/` 那套服务里。

## 它是怎么工作的

- **宿主半区**（`lib/index.js`）跑在 DSH 的 Node 进程里，注册同源接口 `/api/vm-bord/{status,start,stop}`：
  启动时 `spawn` 一个**分离**的 python 进程（`detached: true` + `stdio: "ignore"`），所以关掉 DSH 不会带走它；
  停止走服务自己的 `POST /api/shutdown`，而不是杀进程。
- **浏览器半区**（`lib/client.js`）是手写的单文件 bundle，挂在 `sidebar.footer.action` 槽位上，
  只从宿主 `require("react")`，不发请求到任何第三方。

面板里**没有**看板功能，这是有意的：DSH 的客户端插件不允许 iframe，嵌页面这条路走不通。

## 安装（已在本机做过）

```powershell
# 1. 让 profile 的 node_modules 里能解析到本包（本机用的是 junction）
New-Item -ItemType Junction `
  -Path "$env:APPDATA\dsh-desktop\harness\profiles\web\node_modules\vm-bord-launcher" `
  -Target "D:\IdeaProjects\vm_bord\dsh-plugin"

# 2. 在 profile 的 patch 层里插入这一行（$env:APPDATA\dsh-desktop\harness\profiles\web\cordis.patch.yml）
#    - insert:
#        - id: vm-bord-launcher
#          name: vm-bord-launcher

# 3. 完整重启 DSH Desktop（客户端 bundle 只在启动时加载，没有 HMR）
```

`dsh plugin --profile web add file:...` 理论上更正规，但本机 pnpm store 版本不一致（v11 链接 / v10 期望），
会要求整包重装，所以用了上面这条更小的路子。

## 卸载

1. 删掉 `cordis.patch.yml` 末尾那两行 `- insert: ...`；
2. 删掉 `node_modules\vm-bord-launcher` 这个 junction；
3. 重启 DSH Desktop。

看板和它的数据（`data/hosts.json`、`data/audit.jsonl`）都不受影响。

## 已知边界

- 服务端口 8787 被占时服务会顺延到 8788/8789，面板会扫 8787–8798 找到实际端口。
- 找不到 python 时依次试 `VM_BORD_PYTHON` 环境变量、`python`、`py -3`、`C:\Python312\python.exe`。
- 项目根默认是插件的上两级目录，可用 `VM_BORD_HOME` 覆盖。
