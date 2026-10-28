/**
 * 起/停「隔离数据目录的后端 + 本 worktree 的前端 dev server」。
 *
 * 为什么不用 scripts/e2e_up.py：它把端口（8734/5199）与数据目录写死，
 * 多个工作区并行验收会互相抢；而且它固定用默认 vite 配置 —— 本仓库的
 * frontend/node_modules 常是指向主检出的 junction，默认 fs.allow 会让中文字体 403，
 * 字体没加载的截图不能当视觉证据。这里统一走 frontend/vite.e2e.config.ts。
 *
 * 约定：
 * - 每个 label 一套服务，状态落 scripts/settings-verify/.state/<label>.json（已 gitignore）；
 * - 数据目录默认 %TEMP%/qio-settings-verify/data/<label>，绝不碰用户真实数据目录；
 * - 子进程 detached，服务活过本脚本；down 按 pid 停。
 */
import { spawn } from "node:child_process";
import { createConnection } from "node:net";
import { existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import {
  DEFAULT_APP_PORT,
  DEFAULT_BACKEND_PORT,
  REPO,
  STATE_DIR,
  dataDir as defaultDataDir,
  logDir as defaultLogDir,
  stateFile,
} from "./env.mjs";

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
      // 还没起来
    }
    await sleep(400);
  }
  return false;
}

async function probePython(cmd, cwd) {
  return new Promise((done) => {
    let child;
    try {
      child = spawn(cmd[0], [...cmd.slice(1), "-c", "import fastapi, uvicorn, keyring"], {
        stdio: "ignore",
        cwd,
      });
    } catch {
      done(false);
      return;
    }
    child.on("error", () => done(false));
    child.on("exit", (code) => done(code === 0));
  });
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

export async function up(options = {}) {
  const label = options.label ?? "d";
  const worktree = resolve(options.worktree ?? REPO);
  const appPort = Number(options.appPort ?? process.env.QIO_SETTINGS_APP_PORT ?? DEFAULT_APP_PORT);
  const backendPort = Number(options.backendPort ?? process.env.QIO_SETTINGS_BACKEND_PORT ?? DEFAULT_BACKEND_PORT);
  const dataDir = resolve(options.dataDir ?? defaultDataDir(label));
  const logDir = resolve(options.logDir ?? defaultLogDir(label));
  const attachBackend = options.attachBackend ? String(options.attachBackend) : null;
  const info = {
    label,
    worktree,
    appPort,
    backendPort,
    dataDir,
    backend: attachBackend || "http://127.0.0.1:" + backendPort,
    app: "http://127.0.0.1:" + appPort,
    logs: { backend: join(logDir, "backend.log"), vite: join(logDir, "vite.log") },
    startedAt: new Date().toISOString(),
    pids: {},
  };

  mkdirSync(logDir, { recursive: true });
  mkdirSync(dataDir, { recursive: true });
  mkdirSync(STATE_DIR, { recursive: true });

  for (const [name, port] of [
    ...(attachBackend ? [] : [["backend", backendPort]]),
    ["vite", appPort],
  ]) {
    if (await portOpen(port)) {
      throw new Error("端口 " + port + "（" + name + "）已被占用：先 node scripts/settings-verify/serve.mjs down --label " + label + "，或换端口。");
    }
  }

  let pyCmd = null;
  if (!attachBackend) {
    const candidates = [];
    const override = options.python || process.env.QIO_SETTINGS_PYTHON;
    if (override) candidates.push([override]);
    const winVenv = join(worktree, "backend", ".venv", "Scripts", "python.exe");
    const posixVenv = join(worktree, "backend", ".venv", "bin", "python");
    if (existsSync(winVenv)) candidates.push([winVenv]);
    else if (existsSync(posixVenv)) candidates.push([posixVenv]);
    if (process.env.QIO_SETTINGS_FALLBACK_PYTHON) candidates.push([process.env.QIO_SETTINGS_FALLBACK_PYTHON]);
    for (const cmd of candidates) {
      if (await probePython(cmd, worktree)) {
        pyCmd = cmd;
        break;
      }
    }
    if (!pyCmd) {
      throw new Error("找不到可用后端解释器：传 --python <path> 或设置 QIO_SETTINGS_PYTHON。");
    }
  }
  info.python = pyCmd ? pyCmd.join(" ") : null;
  info.pythonCmd = pyCmd;

  const backendEnv = {
    ...process.env,
    PYTHONPATH: join(worktree, "backend", "src"),
    QIO_DATA_DIR: dataDir,
    QIO_DEV_INSECURE: "1",
    QIO_ENABLE_TEST_EVENTS: "1",
    QIO_DISABLE_DB_CHECK: "1",
    QIO_ALLOWED_ORIGINS: "http://127.0.0.1:" + appPort,
  };
  const viteEnv = { ...process.env, VITE_QIO_BACKEND_URL: info.backend };
  const backendLog = openSync(info.logs.backend, "w");
  const viteLog = openSync(info.logs.vite, "w");

  const backend = attachBackend
    ? null
    : spawn(
        pyCmd[0],
        [...pyCmd.slice(1), "-m", "uvicorn", "agent.main:create_app", "--factory", "--host", "127.0.0.1", "--port", String(backendPort), "--log-level", "warning"],
        { cwd: join(worktree, "backend"), env: backendEnv, stdio: ["ignore", backendLog, backendLog], detached: true, windowsHide: true },
      );
  const viteBin = join(worktree, "frontend", "node_modules", "vite", "bin", "vite.js");
  if (!existsSync(viteBin)) throw new Error("前端依赖缺失：" + viteBin + "（不要在这里 npm install，依赖由 Lead 统一准备）");
  const vite = spawn(
    process.execPath,
    [viteBin, "--config", "vite.e2e.config.ts", "--port", String(appPort), "--strictPort", "--host", "127.0.0.1"],
    { cwd: join(worktree, "frontend"), env: viteEnv, stdio: ["ignore", viteLog, viteLog], detached: true, windowsHide: true },
  );
  backend?.unref();
  vite.unref();
  if (backend) info.pids.backend = backend.pid;
  info.pids.vite = vite.pid;

  const backendReady = attachBackend ? await waitHttp(info.backend + "/api/health", 8000) : await waitHttp(info.backend + "/api/health", 60000);
  const appReady = await waitHttp(info.app + "/", 60000);
  info.backendReady = backendReady;
  info.appReady = appReady;
  info.alive = (backend ? backend.exitCode === null : true) && vite.exitCode === null;
  writeFileSync(stateFile(label), JSON.stringify(info, null, 2), "utf8");
  if (!backendReady || !appReady) {
    const tail = (file) => {
      try {
        return readFileSync(file, "utf8").split("\n").slice(-20).join("\n");
      } catch {
        return "(无日志)";
      }
    };
    throw new Error("服务未就绪：backend=" + backendReady + " app=" + appReady + "\n--- backend.log ---\n" + tail(info.logs.backend) + "\n--- vite.log ---\n" + tail(info.logs.vite));
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
    // 忽略
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
