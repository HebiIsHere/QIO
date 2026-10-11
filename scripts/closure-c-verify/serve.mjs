/**
 * C（界面与审美）验收环境的启动 / 停止脚本 —— 只给验收用，不参与产品打包。
 *
 * 为什么需要它（而不是直接复用 scripts/e2e_up.py）：
 * 1. e2e_up.py 用默认 `vite.config.ts` 起前端。本工作区的 `frontend/node_modules` 是指向
 *    别的检出（实测 D:\qio-dev\qio-protect\frontend\node_modules）的 junction，Vite 默认的
 *    `server.fs.allow` 白名单挡不住它解析后的真实路径，@fontsource 的中文字体一律 403 ——
 *    字体没加载就做排版/对比度验收是不可信的。这里改用 `frontend/vite.e2e.config.ts`
 *    （它把真实 node_modules 路径补进 fs.allow，不改产品配置）。
 * 2. 端口与数据目录必须与其它工作区隔离：后端 8921、前端 5421，
 *    QIO_DATA_DIR 固定指向**独立临时目录**（绝不碰用户真实数据目录）。
 *
 * 用法：
 *   node scripts/closure-c-verify/serve.mjs            # 启动（印出 pid 与日志路径）
 *   node scripts/closure-c-verify/serve.mjs --status   # 只检查是否就绪
 *   node scripts/closure-c-verify/serve.mjs --stop     # 停掉自己起的两个进程
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { createConnection } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
export const BACKEND_PORT = Number(process.env.QIO_C_BACKEND_PORT || 8921);
export const APP_PORT = Number(process.env.QIO_C_APP_PORT || 5421);
const DATA_DIR = process.env.QIO_C_DATA_DIR || join(process.env.TEMP || ".", "qio-closure-c-data");
const LOG_DIR = join(process.env.TEMP || ".", "qio-closure-c-logs");
const PID_FILE = join(here, ".pids.json");
const PY = join(root, "backend", ".venv", "Scripts", "python.exe");
const VITE = join(root, "frontend", "node_modules", "vite", "bin", "vite.js");

export const APP = `http://127.0.0.1:${APP_PORT}`;
export const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`;

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
  let tries = 0;
  while (Date.now() < deadline) {
    tries += 1;
    if (tries % 10 === 0) console.log(`仍在等 ${url}（第 ${tries} 次）`);
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

function stop() {
  if (!existsSync(PID_FILE)) {
    console.log("没有记录在案的 pid 文件：" + PID_FILE);
    return;
  }
  const pids = JSON.parse(readFileSync(PID_FILE, "utf8"));
  for (const [name, pid] of Object.entries(pids)) {
    try {
      process.kill(pid);
      console.log(`已请求停止 ${name} pid=${pid}`);
    } catch (error) {
      console.log(`${name} pid=${pid} 停止失败（可能已经退出）：${error.message}`);
    }
  }
}

async function main() {
  if (process.argv.includes("--stop")) return stop();
  mkdirSync(LOG_DIR, { recursive: true });
  mkdirSync(DATA_DIR, { recursive: true });
  for (const [name, port] of [["backend", BACKEND_PORT], ["vite", APP_PORT]]) {
    if (await portOpen(port)) {
      console.error(`端口 ${port}（${name}）已被占用：先 node scripts/closure-c-verify/serve.mjs --stop，或换 QIO_C_*_PORT。`);
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
  /** detached：这两个服务必须活过本脚本（脚本只负责起与查），否则调用方的命令行一结束它们就被带走（实测过）。 */
  const backend = spawn(PY, ["-m", "uvicorn", "agent.main:create_app", "--factory", "--host", "127.0.0.1", "--port", String(BACKEND_PORT), "--log-level", "warning"], {
    cwd: join(root, "backend"), env: backendEnv, stdio: ["ignore", backendLog, backendLog], detached: true, windowsHide: true,
  });
  const vite = spawn(process.execPath, [VITE, "--config", "vite.e2e.config.ts", "--port", String(APP_PORT), "--strictPort", "--host", "127.0.0.1"], {
    cwd: join(root, "frontend"), env: viteEnv, stdio: ["ignore", viteLog, viteLog], detached: true, windowsHide: true,
  });
  backend.unref();
  vite.unref();
  writeFileSync(PID_FILE, JSON.stringify({ backend: backend.pid, vite: vite.pid, dataDir: DATA_DIR }, null, 2), "utf8");
  console.log(`backend pid=${backend.pid}  vite pid=${vite.pid}`);
  console.log(`QIO_DATA_DIR=${DATA_DIR}`);
  console.log(`日志：${join(LOG_DIR, "backend.log")} / ${join(LOG_DIR, "vite.log")}`);
  const backendOk = await waitHttp(BACKEND + "/api/health", 30000);
  console.log(`backend_ready=${backendOk}`);
  const appOk = await waitHttp(APP + "/", 30000);
  console.log(`app_ready=${appOk} app=${APP}`);
  const alive = backend.exitCode === null && vite.exitCode === null;
  console.log(`both_alive=${alive}`);
  if (!backendOk || !appOk || !alive) process.exit(1);
}

main().catch((error) => {
  console.error("启动失败：" + (error && error.message ? error.message : error));
  process.exit(1);
});
