# 08: 收尾 —— README、打包与推送

**What to build:** 让一个没接触过这个项目的人，只看 README 就能装起来用；仓库里没有不该进去的运行时数据；代码推送到 GitHub。

**Blocked by:** 01, 02, 03, 04, 05, 06, 07

**Status:** done

- [x] README 的快速开始与实际操作一致（双击什么、看到什么）
- [x] `data/` 下的主机清单与审计记录不进版本库
- [x] 依赖只有一个 `pyvmomi`，`requirements.txt` 与实际一致
- [x] 全部代码提交并推送到 `origin/main`
- [x] 验收清单逐条勾选，未达标的如实标注

## 验证记录（2026-10-28）

| 项 | 检查方式 | 结果 |
|---|---|---|
| 快速开始与实操一致 | `start.bat` → `python server\app.py --open`，实测 `GET /` 200 / 41530 字节，页面含「操作」「danger-modal」 | 与 README 描述一致 |
| `data/` 不入库 | `git ls-files data` | 只有 `data/.gitkeep` |
| 依赖清单一致 | `requirements.txt` 与代码 import | 只有 `pyvmomi>=8.0.0`（其余全是标准库） |
| 已推送 | `git ls-remote origin refs/heads/main` | `f960a6b`，与本地 HEAD 一致 |
| 工作区干净 | `git status --porcelain` | 空 |

提交历史：

```
f960a6b feat(dsh-plugin): DSH Desktop 启动器面板；docs: 真机往返测量与 README 收尾
b66b828 feat(server,web): 主机编辑、移除确认、离线与证书变更提示
8514358 feat(server,web): 强制断电、强制重置与审计流水
8a231cf feat(server,web): 虚拟机聚合表与电源操作
6ea65d5 feat(server,web): 服务骨架与主机资源卡
4cad7d7 docs: 需求总结、术语表与两条架构决策
```

## 未达标 / 待确认（如实标注）

- **票 07 的界面部分未经目视确认**：客户端 bundle 只在 DSH Desktop 启动时加载，本机没有 HMR，
  需要完整重启 Desktop 才能在侧栏看到「VM 看板」按钮。代码、YAML、包解析、服务侧接口都已离线校验通过。
- **公开仓库里没有放截图**：截图里带着内网客户环境名与 IP，不适合进 public 仓库。
