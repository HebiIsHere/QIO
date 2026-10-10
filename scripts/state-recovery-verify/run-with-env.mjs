/**
 * 用一个进程把「后端 + 前端 + 采集脚本」串起来：服务在同一个受管进程树里，
 * 采集脚本跑到哪，服务就活到哪（避免服务被外部清理掉导致 mid-run fetch failed）。
 *
 * 用法：
 *   node scripts/state-recovery-verify/run-with-env.mjs -- <脚本相对路径> [脚本参数...]
 * 例：
 *   node scripts/state-recovery-verify/run-with-env.mjs -- journey.mjs --label=baseline
 */
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { APP, APP_PORT, BACKEND, BACKEND_PORT, DATA_DIR, ROOT } from "./config.mjs";

const args = process.argv.slice(2);
const cut = args.indexOf("--");
const scriptArgs = cut >= 0 ? args.slice(cut + 1) : args;
if (!scriptArgs.length) {
  console.error("用法：node run-with-env.mjs -- <脚本> [参数...]");
  process.exit(2);
}
mkdirSync(DATA_DIR, { recursive: true });
const python = join(ROOT, "backend", ".venv", "Scripts", "python.exe");
const vite = join(ROOT, "frontend", "node_modules", "vite", "bin", "vite.js");

const backend = spawn(
  python,
  ["-m", "uvicorn", "agent.main:create_app", "--factory", "--host", "127.0.0.1", "--port", String(BACKEND_PORT), "--log-level", "warning"],
  { cwd: join(ROOT, "backend"), env: { ...process.env, PYTHONPATH: join(ROOT, "backend", "src"), QIO_DATA_DIR: DATA_DIR, QIO_DEV_INSECURE: "1", QIO_ENABLE_TEST_EVENTS: "1" }, stdio: ["ignore", "inherit", "inherit"] },
);
const front = spawn(
  process.execPath,
  [vite, "--config", "vite.e2e.config.ts", "--port", String(APP_PORT), "--strictPort", "--host", "127.0.0.1"],
  { cwd: join(ROOT, "frontend"), env: { ...process.env, VITE_QIO_BACKEND_URL: BACKEND }, stdio: ["ignore", "inherit", "inherit"] },
);
let childExited = false;
backend.on("exit", () => { childExited = true; });
front.on("exit", () => { childExited = true; });

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

function killAll() {
  for (const child of [backend, front]) {
    try {
      child.kill();
    } catch {
      /* 忽略 */
    }
  }
}

try {
  console.log("QIO_DATA_DIR=" + DATA_DIR);
  const backendOk = await waitHttp(BACKEND + "/api/health", 60000);
  const appOk = await waitHttp(APP + "/", 60000);
  console.log("backend_ready=" + backendOk + " app_ready=" + appOk);
  if (!backendOk || !appOk || childExited) {
    console.error("服务没起来，放弃采集");
    process.exitCode = 1;
  } else {
    const script = join(new URL(".", import.meta.url).pathname.replace(/^\//, ""), scriptArgs[0]);
    const code = await new Promise((resolve) => {
      const child = spawn(process.execPath, [script, ...scriptArgs.slice(1)], { cwd: ROOT, stdio: "inherit" });
      child.on("exit", (value) => resolve(value ?? 1));
    });
    process.exitCode = code;
  }
} finally {
  killAll();
  await sleep(800);
}
