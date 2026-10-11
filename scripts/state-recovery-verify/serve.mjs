/**
 * 第八轮状态恢复收尾 · 独立验收 D 的环境启动 / 停止（只给验收用）。
 *
 * 与 scripts/closure-c-verify/serve.mjs 的差别：
 * - 端口换成 8933（后端）/ 5433（前端），与其它工作区错开；
 * - QIO_DATA_DIR 固定指向**独立临时目录**（默认 %TEMP%\qio-sr-data），绝不碰真实数据目录；
 * - pid 文件在本目录（.pids.json），不会和别的装置互相停掉；
 * - 前端仍用 frontend/vite.e2e.config.ts（本工作区 node_modules 是 junction，
 *   默认 fs.allow 会让字体 403，字体没加载的截图不能作为排版/文案证据）。
 *
 * 用法：
 *   node scripts/state-recovery-verify/serve.mjs          # 启动
 *   node scripts/state-recovery-verify/serve.mjs --status # 只查就绪
 *   node scripts/state-recovery-verify/serve.mjs --stop   # 停掉自己起的两个进程
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { createConnection } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";
import { APP, APP_PORT, BACKEND, BACKEND_PORT, DATA_DIR, ROOT as root } from "./config.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const LOG_DIR = process.env.QIO_SR_LOG_DIR || join(process.env.TEMP || ".", "qio-sr-logs");
const PID_FILE = join(here, ".pids.json");
const PY = join(root, "backend", ".venv", "Scripts", "python.exe");
const VITE = join(root, "frontend", "node_modules", "vite", "bin", "vite.js");

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
      const resp = await fetch(url, { signal: AbortSignal.timeout(2500) });
      if (resp.status < 500) return true;
    } catch {
      /* 还没起来 */
    }
    await sleep(400);
  }
  return false;
}

function readPids() {
  if (!existsSync(PID_FILE)) return null;
  try {
    return JSON.parse(readFileSync(PID_FILE, "utf8"));
  } catch {
    return null;
  }
}

function stop() {
  const pids = readPids();
  if (!pids) {
    console.log("没有记录在案的 pid 文件：" + PID_FILE);
    return;
  }
  for (const [name, pid] of Object.entries(pids)) {
    if (typeof pid !== "number") continue;
    try {
      process.kill(pid);
      console.log("已请求停止 " + name + " pid=" + pid);
    } catch (error) {
      console.log(name + " pid=" + pid + " 停止失败（可能已经退出）：" + error.message);
    }
  }
}

async function main() {
  if (process.argv.includes("--stop")) return stop();
  const pids = readPids();
  const alreadyUp = pids && (await portOpen(BACKEND_PORT)) && (await portOpen(APP_PORT));
  if (process.argv.includes("--status") || alreadyUp) {
    const backendOk = await waitHttp(BACKEND + "/api/health", 5000);
    const appOk = await waitHttp(APP + "/", 5000);
    console.log("backend_ready=" + backendOk + " app_ready=" + appOk + " app=" + APP);
    if (process.argv.includes("--status")) process.exit(backendOk && appOk ? 0 : 1);
    if (alreadyUp && backendOk && appOk) return;
  }
  mkdirSync(LOG_DIR, { recursive: true });
  mkdirSync(DATA_DIR, { recursive: true });
  for (const [name, port] of [
    ["backend", BACKEND_PORT],
    ["vite", APP_PORT],
  ]) {
    if (await portOpen(port)) {
      console.error(
        "端口 " + port + "（" + name + "）已被占用：先 node scripts/state-recovery-verify/serve.mjs --stop，或换 QIO_SR_*_PORT。",
      );
      process.exit(2);
    }
  }
  const backendEnv = {
    ...process.env,
    PYTHONPATH: join(root, "backend", "src"),
    QIO_DATA_DIR: DATA_DIR,
    QIO_DEV_INSECURE: "1",
    QIO_ENABLE_TEST_EVENTS: "1",
  };
  const viteEnv = { ...process.env, VITE_QIO_BACKEND_URL: BACKEND };
  const backendLog = openSync(join(LOG_DIR, "backend.log"), "w");
  const viteLog = openSync(join(LOG_DIR, "vite.log"), "w");
  const backend = spawn(
    PY,
    ["-m", "uvicorn", "agent.main:create_app", "--factory", "--host", "127.0.0.1", "--port", String(BACKEND_PORT), "--log-level", "warning"],
    { cwd: join(root, "backend"), env: backendEnv, stdio: ["ignore", backendLog, backendLog], detached: true, windowsHide: true },
  );
  const vite = spawn(
    process.execPath,
    [VITE, "--config", "vite.e2e.config.ts", "--port", String(APP_PORT), "--strictPort", "--host", "127.0.0.1"],
    { cwd: join(root, "frontend"), env: viteEnv, stdio: ["ignore", viteLog, viteLog], detached: true, windowsHide: true },
  );
  backend.unref();
  vite.unref();
  writeFileSync(
    PID_FILE,
    JSON.stringify({ backend: backend.pid, vite: vite.pid, dataDir: DATA_DIR }, null, 2),
    "utf8",
  );
  console.log("backend pid=" + backend.pid + "  vite pid=" + vite.pid);
  console.log("QIO_DATA_DIR=" + DATA_DIR);
  console.log("日志：" + join(LOG_DIR, "backend.log") + " / " + join(LOG_DIR, "vite.log"));
  const backendOk = await waitHttp(BACKEND + "/api/health", 40000);
  console.log("backend_ready=" + backendOk);
  const appOk = await waitHttp(APP + "/", 40000);
  console.log("app_ready=" + appOk + " app=" + APP);
  const alive = backend.exitCode === null && vite.exitCode === null;
  console.log("both_alive=" + alive);
  if (!backendOk || !appOk || !alive) process.exit(1);
}

main().catch((error) => {
  console.error("启动失败：" + (error && error.message ? error.message : error));
  process.exit(1);
});
