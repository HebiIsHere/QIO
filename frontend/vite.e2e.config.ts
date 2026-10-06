/**
 * 验收专用的 Vite 配置（只用于起 dev server 做真实交互与截图检查）。
 *
 * 为什么放在 frontend/ 而不是 scripts/：这个文件要 `import "vite"` 与 `@vitejs/plugin-vue`，
 * 而只有 frontend/ 目录下能解析到 node_modules。放在 scripts/ 下时，
 * 任何工作区（尤其是子智能体的独立工作区）都会报 ERR_MODULE_NOT_FOUND。
 *
 * 它做的事只有一件：把 node_modules 的真实路径补进 `server.fs.allow`，
 * 让 @fontsource 的中文字体不再 403（工作区里 node_modules 常是指向主检出的 junction，
 * 落在 Vite 默认的 fs 白名单之外）。**不改任何产品配置**。
 *
 * 用法（在仓库根目录或 frontend/ 下都可以）：
 *   node frontend/node_modules/vite/bin/vite.js --config frontend/vite.e2e.config.ts --port 5399 --host 127.0.0.1
 */
import { defineConfig, mergeConfig, type UserConfig } from "vite";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import base from "./vite.config";

const here = dirname(fileURLToPath(import.meta.url));
let modules = resolve(here, "node_modules");
try {
  modules = realpathSync(modules);
} catch {
  // node_modules 不存在时保持原路径：配置本身不该因为环境而崩
}

export default mergeConfig(
  base as UserConfig,
  defineConfig({
    server: { fs: { allow: [here, modules] } },
  }),
);
