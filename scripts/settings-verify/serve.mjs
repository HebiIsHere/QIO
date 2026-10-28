/**
 * 起/停设置页验收环境（后端 + 前端 dev server），并打印状态。
 *
 * 用法（任意目录执行均可，脚本自己算路径）：
 *   node scripts/settings-verify/serve.mjs up [--label d] [--app-port 5106] [--backend-port 8756]
 *   node scripts/settings-verify/serve.mjs status --label d
 *   node scripts/settings-verify/serve.mjs down --label d
 */
import { up, down, status } from "./lib/stack.mjs";
import { DEFAULT_APP_PORT, DEFAULT_BACKEND_PORT } from "./lib/env.mjs";

const args = process.argv.slice(2);
const command = args[0] ?? "up";
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};

const label = String(flag("label", "d"));
try {
  if (command === "down") {
    console.log(JSON.stringify(down(label), null, 2));
  } else if (command === "status") {
    console.log(JSON.stringify(await status(label), null, 2));
  } else if (command === "up") {
    const info = await up({
      label,
      worktree: flag("worktree", null),
      appPort: Number(flag("app-port", process.env.QIO_SETTINGS_APP_PORT ?? DEFAULT_APP_PORT)),
      backendPort: Number(flag("backend-port", process.env.QIO_SETTINGS_BACKEND_PORT ?? DEFAULT_BACKEND_PORT)),
      dataDir: flag("data-dir", null),
      python: flag("python", process.env.QIO_SETTINGS_PYTHON),
      attachBackend: flag("attach-backend", null),
    });
    console.log(JSON.stringify(info, null, 2));
  } else {
    console.error("未知命令：" + command + "（可用：up / status / down）");
    process.exit(2);
  }
} catch (error) {
  console.error("失败：" + (error && error.stack ? error.stack : error));
  process.exit(1);
}
