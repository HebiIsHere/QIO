// 启动 vite dev server，验证星球原型页面与模块可编译访问
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const PORT = 5199;
const BASE = "http://127.0.0.1:" + PORT;
const frontendDir = fileURLToPath(new URL("../", import.meta.url));
const vite = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--port", String(PORT), "--strictPort", "--host", "127.0.0.1"], {
  cwd: frontendDir,
  stdio: ["ignore", "pipe", "pipe"],
});

let viteOut = "";
vite.stdout.on("data", (d) => { viteOut += d; });
vite.stderr.on("data", (d) => { viteOut += d; });

let lastErr = "";
async function waitReady() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`${BASE}/prototypes/planet/`);
      if (r.ok) return;
      lastErr = `status ${r.status}`;
    } catch (e) {
      lastErr = e.cause?.code || e.message;
    }
    await sleep(250);
  }
  throw new Error(`vite not ready (last: ${lastErr}):\n${viteOut}`);
}

async function check(url) {
  const r = await fetch(`${BASE}${url}`);
  const text = await r.text();
  if (r.status !== 200) {
    throw new Error(`${url} -> ${r.status}\n${text.slice(0, 600)}`);
  }
  if (text.includes("SyntaxError") || text.includes("Could not resolve")) {
    throw new Error(`${url} -> compile error in response\n${text.slice(0, 600)}`);
  }
  console.log(`OK ${r.status} ${url} (${text.length} bytes)`);
}

try {
  await waitReady();
  await check("/prototypes/planet/");
  await check("/prototypes/planet/main.js");
  await check("/prototypes/planet/layout.js");
  await check("/prototypes/planet/camera.js");
  await check("/prototypes/planet/data.js");
  console.log("planet prototype: compile OK");
} finally {
  vite.kill();
}