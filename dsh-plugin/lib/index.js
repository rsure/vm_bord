/**
 * vm_bord 启动器的宿主半区。
 *
 * 只做三件事：把 http://127.0.0.1:87xx 的健康检查翻译成"在不在跑"、按需拉起
 * python 服务、按需让它退出。看板本身的界面/功能全在同源的另一套服务里，
 * 这里一概不碰。
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const inject = ["webServer"];

const PORT_FIRST = 8787;
const PORT_LAST = 8798;
const HEALTH_TIMEOUT = 700;
const BOOT_TIMEOUT = 25_000;

const here = dirname(fileURLToPath(import.meta.url));

/** 项目根目录：插件默认就放在仓库里的 dsh-plugin/ 下；也允许用环境变量覆盖。 */
function projectRoot() {
  const candidates = [];
  if (process.env.VM_BORD_HOME) candidates.push(process.env.VM_BORD_HOME);
  candidates.push(resolve(here, "..", ".."));
  candidates.push(resolve(here, ".."));
  for (const dir of candidates) {
    if (existsSync(join(dir, "server", "app.py"))) return dir;
  }
  return null;
}

/** 依次尝试几种解释器写法，先到先得。 */
function pythonCandidates() {
  const list = [];
  if (process.env.VM_BORD_PYTHON) {
    list.push({ cmd: process.env.VM_BORD_PYTHON, args: [] });
  }
  list.push({ cmd: "python", args: [] });
  list.push({ cmd: "py", args: ["-3"] });
  list.push({ cmd: "C:\\Python312\\python.exe", args: [] });
  return list;
}

async function healthOf(port) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT),
    });
    if (!res.ok) return null;
    const body = await res.json();
    return body && body.ok === true ? body : null;
  } catch {
    return null;
  }
}

/** 顺延规则和 python 服务一致：从 8787 往上一路找第一个活着的。 */
async function findRunning() {
  for (let port = PORT_FIRST; port <= PORT_LAST; port += 1) {
    const health = await healthOf(port);
    if (health) return { running: true, port, version: health.version };
  }
  return { running: false, port: null, version: null };
}

function startProcess(root) {
  return new Promise((accept, reject) => {
    const attempts = pythonCandidates();
    let index = 0;

    const tryNext = () => {
      if (index >= attempts.length) {
        reject(new Error("找不到可用的 python（试过 VM_BORD_PYTHON、python、py -3）"));
        return;
      }
      const { cmd, args } = attempts[index];
      index += 1;
      let child;
      try {
        child = spawn(cmd, [...args, "server/app.py"], {
          cwd: root,
          detached: true,          // 独立存活：DSH 关掉也不影响它
          stdio: "ignore",         // 没有管道就没有"管道断了写日志暴死"那类问题
          windowsHide: true,
        });
      } catch {
        tryNext();
        return;
      }
      child.once("error", () => tryNext());
      child.once("spawn", () => {
        child.unref();
        accept(child.pid ?? 0);
      });
    };

    tryNext();
  });
}

async function waitUntilRunning(deadlineMs) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    const state = await findRunning();
    if (state.running) return state;
    await new Promise((done) => setTimeout(done, 400));
  }
  return findRunning();
}

async function stopRunning(port) {
  try {
    await fetch(`http://127.0.0.1:${port}/api/shutdown`, {
      method: "POST",
      signal: AbortSignal.timeout(3000),
    });
  } catch {
    // 已经停了也说不定，落到下面的轮询去确认
  }
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (!(await healthOf(port))) return true;
    await new Promise((done) => setTimeout(done, 300));
  }
  return false;
}

function send(res, status, payload) {
  try {
    res.statusCode = status;
    res.setHeader("content-type", "application/json; charset=utf-8");
    res.setHeader("cache-control", "no-store");
    res.end(JSON.stringify(payload));
  } catch {
    // 浏览器关掉连接是常态
  }
}

export function apply(ctx) {
  if (!ctx || typeof ctx.webServer?.register !== "function") return;

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "prefix",
      path: "/api/vm-bord",
      handler: async (req, res) => {
        const url = new URL(req.url, "http://localhost");
        const action = url.pathname.replace(/^\/api\/vm-bord\/?/, "").replace(/\/+$/, "");
        const method = (req.method || "GET").toUpperCase();

        if (action === "" || action === "status") {
          const state = await findRunning();
          return send(res, 200, { ok: true, ...state, root: projectRoot() });
        }

        if (action === "start" && method === "POST") {
          const already = await findRunning();
          if (already.running) return send(res, 200, { ok: true, ...already, started: false });
          const root = projectRoot();
          if (!root) {
            return send(res, 500, {
              ok: false,
              error: { kind: "config", message: "找不到项目根目录（缺 server/app.py），请设置 VM_BORD_HOME" },
            });
          }
          try {
            const pid = await startProcess(root);
            const state = await waitUntilRunning(BOOT_TIMEOUT);
            if (!state.running) {
              return send(res, 502, {
                ok: false,
                error: { kind: "boot", message: `python 已拉起（pid ${pid}）但 25 秒内没起来` },
              });
            }
            return send(res, 200, { ok: true, ...state, started: true });
          } catch (err) {
            return send(res, 500, {
              ok: false,
              error: { kind: "spawn", message: String(err?.message ?? err) },
            });
          }
        }

        if (action === "stop" && method === "POST") {
          const state = await findRunning();
          if (!state.running) return send(res, 200, { ok: true, running: false, port: null, stopped: false });
          const stopped = await stopRunning(state.port);
          return send(res, stopped ? 200 : 500, {
            ok: stopped,
            running: !stopped,
            port: stopped ? null : state.port,
            stopped,
            ...(stopped ? {} : { error: { kind: "timeout", message: "10 秒内没停下来" } }),
          });
        }

        return send(res, 404, { ok: false, error: { kind: "not_found", message: `不认识的接口：${action}` } });
      },
    }, "vm-bord-launcher: 本地看板启停"),
  );
}
