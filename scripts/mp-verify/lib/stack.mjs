/**
 * 「隔离数据目录的后端 + 指定 worktree 的前端 dev server」的启停。
 *
 * 为什么不是直接用 scripts/e2e_up.py：
 * 1. 它把端口（8734 / 5199）与数据目录写死，多个 worktree 同时验收会互相抢；
 * 2. 它只服务当前 checkout，不接受「用别的 worktree 的前端」——而 C 的核心验收是
 *    「基线 vs 改版」两套代码在**同一台机器、同一个后端、同一份数据**上对比；
 * 3. 它用默认 vite.config.ts。本仓库的 frontend/node_modules 常是指向主检出的 junction，
 *    Vite 的 fs.allow 挡不住它解析后的真实路径，@fontsource 中文字体会 403 —— 字体没加载
 *    的排版截图不能作为视觉证据。这里统一用 frontend/vite.e2e.config.ts（补 fs.allow，
 *    不改产品配置）。
 *
 * 约定：
 * - 每个 label 一套服务，状态落在 scripts/mp-verify/.state/<label>.json（已 gitignore）；
 * - 数据目录默认 %TEMP%\qio-mp-verify\data\<label>，绝不碰用户真实数据目录；
 * - 子进程 detached，服务活过本脚本。
 */
import { spawn } from "node:child_process";
import { createConnection } from "node:net";
import { existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const here = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(here, "..", "..", "..");
export const STATE_DIR = join(REPO, "scripts", "mp-verify", ".state");
const TEMP_ROOT = process.env.QIO_MP_TEMP || join(process.env.TEMP || ".", "qio-mp-verify");

export function stateFile(label) {
  return join(STATE_DIR, label + ".json");
}

export function readState(label) {
  const file = stateFile(label);
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

export function defaultDataDir(label) {
  return join(TEMP_ROOT, "data", label);
}

function portOpen(port) {
  return new Promise((done) => {
    const socket = createConnection({ host: "127.0.0.1", port }, () => {
      socket.destroy();
      done(true);
    });
    socket.on("error", () => done(false));
    socket.setTimeout(1200, () => {
      socket.destroy();
      done(false);
    });
  });
}

async function waitHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (resp.status < 500) return true;
    } catch {
      /* 还没起来 */
    }
    await sleep(400);
  }
  return false;
}

export async function up(options = {}) {
  const label = options.label ?? "c";
  const worktree = resolve(options.worktree ?? join(REPO, "..", "qio-mp-c"));
  const backendPort = Number(options.backendPort ?? process.env.QIO_MP_BACKEND_PORT ?? 8751);
  const appPort = Number(options.appPort ?? process.env.QIO_MP_APP_PORT ?? 5103);
  const dataDir = resolve(options.dataDir ?? defaultDataDir(label));
  const python = options.python ?? process.env.QIO_MP_PYTHON;
  const logDir = join(TEMP_ROOT, "logs", label);
  const info = {
    label,
    worktree,
    backendPort,
    appPort,
    dataDir,
    backend: "http://127.0.0.1:" + backendPort,
    app: "http://127.0.0.1:" + appPort,
    logs: { backend: join(logDir, "backend.log"), vite: join(logDir, "vite.log") },
    startedAt: new Date().toISOString(),
    pids: {},
  };

  mkdirSync(logDir, { recursive: true });
  mkdirSync(dataDir, { recursive: true });
  mkdirSync(STATE_DIR, { recursive: true });

  for (const [name, port] of [
    ["backend", backendPort],
    ["vite", appPort],
  ]) {
    if (await portOpen(port)) {
      throw new Error(
        "端口 " + port + "（" + name + "）已被占用：先 node scripts/mp-verify/serve.mjs down --label " + label + "，或换端口。",
      );
    }
  }

  // 借用一个能 import 依赖的解释器：逐个探测（同步探测用 spawn + 轮询退出码）
  let pyCmd = null;
  const candidates = [];
  if (python) candidates.push([python]);
  const winVenv = join(worktree, "backend", ".venv", "Scripts", "python.exe");
  const posixVenv = join(worktree, "backend", ".venv", "bin", "python");
  if (existsSync(winVenv)) candidates.push([winVenv]);
  else if (existsSync(posixVenv)) candidates.push([posixVenv]);
  if (options.fallbackPython) candidates.push([options.fallbackPython]);
  for (const cmd of candidates) {
    const probe = spawn(cmd[0], [...cmd.slice(1), "-c", "import fastapi, uvicorn, keyring"], {
      stdio: "ignore",
      cwd: worktree,
    });
    const code = await new Promise((done) => probe.on("exit", (c) => done(c)));
    if (code === 0) {
      pyCmd = cmd;
      break;
    }
  }
  if (!pyCmd) {
    throw new Error(
      "找不到可用的后端解释器。传入 --python <path>（例如 D:\\qio-dev\\qio-mp-int\\backend\\.venv\\Scripts\\python.exe）或设置 QIO_MP_PYTHON。",
    );
  }
  info.python = pyCmd.join(" ");
  info.pythonCmd = pyCmd;

  const backendEnv = {
    ...process.env,
    PYTHONPATH: join(worktree, "backend", "src"),
    QIO_DATA_DIR: dataDir,
    QIO_DEV_INSECURE: "1",
    QIO_DISABLE_DB_CHECK: "1",
    QIO_ENABLE_TEST_EVENTS: "1",
    QIO_ALLOWED_ORIGINS: "http://127.0.0.1:" + appPort,
  };
  const viteEnv = { ...process.env, VITE_QIO_BACKEND_URL: info.backend };
  const backendLog = openSync(info.logs.backend, "w");
  const viteLog = openSync(info.logs.vite, "w");

  const backend = spawn(
    pyCmd[0],
    [
      ...pyCmd.slice(1),
      "-m",
      "uvicorn",
      "agent.main:create_app",
      "--factory",
      "--host",
      "127.0.0.1",
      "--port",
      String(backendPort),
      "--log-level",
      "warning",
    ],
    {
      cwd: join(worktree, "backend"),
      env: backendEnv,
      stdio: ["ignore", backendLog, backendLog],
      detached: true,
      windowsHide: true,
    },
  );
  const viteBin = join(worktree, "frontend", "node_modules", "vite", "bin", "vite.js");
  if (!existsSync(viteBin)) {
    throw new Error("前端依赖缺失：" + viteBin + "（不要在这里 npm ci，等 Lead 复制依赖）");
  }
  const vite = spawn(
    process.execPath,
    [viteBin, "--config", "vite.e2e.config.ts", "--port", String(appPort), "--strictPort", "--host", "127.0.0.1"],
    {
      cwd: join(worktree, "frontend"),
      env: viteEnv,
      stdio: ["ignore", viteLog, viteLog],
      detached: true,
      windowsHide: true,
    },
  );
  backend.unref();
  vite.unref();
  info.pids = { backend: backend.pid, vite: vite.pid };

  const backendReady = await waitHttp(info.backend + "/api/health", 45000);
  const appReady = await waitHttp(info.app + "/", 45000);
  info.backendReady = backendReady;
  info.appReady = appReady;
  info.alive = backend.exitCode === null && vite.exitCode === null;
  writeFileSync(stateFile(label), JSON.stringify(info, null, 2), "utf8");
  if (!backendReady || !appReady) {
    const tail = (file) => {
      try {
        return readFileSync(file, "utf8").split("\n").slice(-15).join("\n");
      } catch {
        return "(无日志)";
      }
    };
    throw new Error(
      "服务未就绪：backend=" + backendReady + " app=" + appReady + "\n--- backend.log ---\n" + tail(info.logs.backend) + "\n--- vite.log ---\n" + tail(info.logs.vite),
    );
  }
  return info;
}

export function down(label) {
  const info = readState(label);
  if (!info) return { stopped: false, reason: "没有 " + label + " 的运行记录" };
  const stopped = [];
  for (const [name, pid] of Object.entries(info.pids ?? {})) {
    try {
      process.kill(pid);
      stopped.push(name + ":" + pid);
    } catch (error) {
      stopped.push(name + ":" + pid + "（已退出：" + error.message + "）");
    }
  }
  try {
    rmSync(stateFile(label));
  } catch {
    /* 忽略 */
  }
  return { stopped: true, pids: stopped };
}

export async function status(label) {
  const info = readState(label);
  if (!info) return { running: false, reason: "没有 " + label + " 的运行记录" };
  const backendReady = await waitHttp(info.backend + "/api/health", 2500);
  const appReady = await waitHttp(info.app + "/", 2500);
  const alive = [];
  for (const [name, pid] of Object.entries(info.pids ?? {})) {
    let ok = false;
    try {
      process.kill(pid, 0);
      ok = true;
    } catch {
      ok = false;
    }
    alive.push({ name, pid, alive: ok });
  }
  return { running: backendReady && appReady, backendReady, appReady, alive, ...info };
}
