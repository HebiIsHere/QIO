/**
 * 起/停「隔离数据目录的后端 + 指定 worktree 的前端 dev server」，并可按需灌验收数据。
 *
 * 用法（在任意目录执行都可以，脚本自己算路径）：
 *   node scripts/mp-verify/serve.mjs up --label c --worktree D:\qio-dev\qio-mp-c \
 *        --app-port 5103 --backend-port 8751 --seed
 *   node scripts/mp-verify/serve.mjs status --label c
 *   node scripts/mp-verify/serve.mjs down --label c
 *
 * 默认值（都可用参数/环境变量覆盖）：
 *   --worktree      <本脚本所在 worktree>
 *   --app-port      QIO_MP_APP_PORT   或 5103
 *   --backend-port  QIO_MP_BACKEND_PORT 或 8751
 *   --data-dir      %TEMP%\qio-mp-verify\data\<label>（**绝不指向用户真实数据目录**）
 *   --python        QIO_MP_PYTHON；否则用 <worktree>\backend\.venv，其次 --fallback-python
 *
 * 端口分配约定（Lead 统一分配，避免互相抢）：base=5101 int=5102 C=5103 A=5104 B=5105；
 * 后端共享 8734，独立后端用 8751。开第二个环境时换 label / 端口即可。
 */
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { up, down, status, defaultDataDir } from "./lib/stack.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const command = args[0] ?? "up";

function flag(name, fallback) {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
}

const label = String(flag("label", "c"));
const worktree = resolve(String(flag("worktree", join(here, "..", ".."))));

async function runSeed(dataDir, pythonCmd) {
  const script = join(here, "seed.py");
  const cmd = Array.isArray(pythonCmd) && pythonCmd.length ? pythonCmd : [process.env.QIO_MP_PYTHON || "python"];
  return new Promise((done) => {
    const child = spawn(
      cmd[0],
      [...cmd.slice(1), script, "--data-dir", dataDir, "--worktree", worktree, "--python", cmd[0], "--reset"],
      { stdio: "inherit", env: { ...process.env, QIO_DATA_DIR: dataDir } },
    );
    child.on("exit", (code) => done(code));
  });
}

if (command === "down") {
  console.log(JSON.stringify(down(label), null, 2));
} else if (command === "status") {
  console.log(JSON.stringify(await status(label), null, 2));
} else if (command === "up") {
  const info = await up({
    label,
    worktree,
    appPort: flag("app-port", process.env.QIO_MP_APP_PORT ?? 5103),
    backendPort: flag("backend-port", process.env.QIO_MP_BACKEND_PORT ?? 8751),
    dataDir: flag("data-dir", defaultDataDir(label)),
    python: flag("python", process.env.QIO_MP_PYTHON),
    fallbackPython: flag("fallback-python", process.env.QIO_MP_FALLBACK_PYTHON),
  });
  console.log(JSON.stringify(info, null, 2));
  if (flag("seed", false)) {
    console.log("--- 灌验收数据 ---");
    const code = await runSeed(info.dataDir, info.pythonCmd);
    console.log("seed_exit=" + code);
    if (code !== 0) process.exit(code ?? 1);
  }
} else {
  console.error("未知命令：" + command + "（可用：up / status / down）");
  process.exit(2);
}
