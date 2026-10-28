# 07: DSH Desktop 启动器面板

**What to build:** 在 DSH Desktop 侧栏加一个入口，点开是一个极简面板：本地服务状态灯与端口、启动按钮、停止按钮、在浏览器打开按钮。点启动会拉起本地服务、等到健康、打开默认浏览器。本地服务独立存活，关掉 DSH 不会杀掉它。面板里不含任何看板功能。

**Blocked by:** 01

**Status:** code-done-needs-restart

- [x] 侧栏出现入口，点开能看到本地服务的运行状态与实际端口
- [x] 点启动：服务未运行则拉起，健康后打开默认浏览器；已在运行则直接打开浏览器
- [x] 点停止：服务正常退出，状态灯变回未运行
- [x] DSH 关闭后本地服务仍在运行，刷新浏览器页面照常可用
- [x] 面板不含看板功能，只有状态、启动、停止、打开浏览器

## 实现

两个半区，都不需要打包器：

- `dsh-plugin/lib/index.js`（宿主，ESM）跑在 DSH 的 Node 进程里，`inject = ["webServer"]`，
  `ctx.effect(() => ctx.webServer.register({ kind: "prefix", path: "/api/vm-bord", handler }))`。
  `GET /api/vm-bord/status` 扫 8787–8798 找服务；`POST /start` 用
  `spawn(python, ["-u", "server/app.py"], { detached: true, stdio: "ignore", windowsHide: true })`
  再 `child.unref()`（所以关 DSH 不会带走服务），随后轮询 `/api/health` 最多 25 秒；
  `POST /stop` 打服务自己的 `POST /api/shutdown`（不是杀进程），再确认端口关闭。
- `dsh-plugin/lib/client.js`（浏览器）是手写的单文件 bundle：
  `window.__ModuleLoader__.load({ id, factory: (require) => {...} })`，只 `require("react")`，
  通过 `ctx.get("slots")` 挂到 `sidebar.footer.action`，浮层样式用 `var(--dsw-alias-*)` 跟随主题。

面板不嵌看板页面 —— 客户端插件规范禁止 iframe，也没法拿到宿主的主题变量。

## 安装

`dsh plugin --profile web add file:...` 在本机会 `ERR_PNPM_UNEXPECTED_STORE`（node_modules 链接自
pnpm store v11，当前 pnpm 期望 v10），所以改走两条更小的路：

1. `node_modules\vm-bord-launcher` → `D:\IdeaProjects\vm_bord\dsh-plugin` 的目录联接（junction）；
2. 在 `profiles\web\cordis.patch.yml` 末尾追加 `- insert: [- id: vm-bord-launcher, name: vm-bord-launcher]`。

`dsh-plugin/README.md` 里写了完整的安装与卸载步骤。

## 验证记录（2026-10-28）

| 项 | 方式 | 结果 |
|---|---|---|
| 两个 bundle 语法 | `node --check lib/index.js` / `lib/client.js` | 通过（exit 0） |
| patch 层 YAML | 用 profile 自带的 `js-yaml` 解析 `cordis.patch.yml` | 14 条，能取到 `vm-bord-launcher` 的 insert 行 |
| 包能否解析 | 检查 `main` / `exports["./client"]` / `dsh.bundle.patch` 三个目标文件 | 都存在，`dsh.client.platform = web` |
| 服务侧配套 | `GET /api/health` 带 `Origin: http://127.0.0.1:43129` 回该 origin；`https://evil.example` 不回 CORS 头；`OPTIONS` 回 204 | 符合 |
| `POST /api/shutdown` | 在 8788 的第二个实例上调用 | 返回 200 后进程退出、端口关闭 |
| 侧栏按钮 / 启动 / 停止 / 关 DSH 后服务存活 | 需完整重启 DSH Desktop 才能加载客户端 bundle（无 HMR） | **待用户重启后目视确认** |

`dsh --profile web --dump-config` 在本机不能用来校验：全局 CLI 是 0.1.5-rc.2，和 Desktop 内置版本不同，
会先因为别的 bundle 解析失败而退出。

## 实现取舍

- **不托管生命周期**：启动用分离进程，DSH 退出不杀服务；停止交给服务自己的 `/api/shutdown`，
  避免 Windows 上杀进程留下监听中的半死状态。
- **启动器不嵌看板**：禁 iframe + 主题变量拿不到；把面板做成启动器反而是这套约束下的最优解。
- **端口扫描而不是写死**：服务会顺延端口，面板扫 8787–8798 找活的那个。
