/**
 * 验收专用的 Vite 配置（只用于 Lead 在独立工作区起 dev server 做真实交互与截图检查）。
 *
 * 为什么需要它：这个工作区的 frontend/node_modules 是指向主检出的 junction，
 * 落在 Vite 的 fs.allow 之外，@fontsource 的中文字体会 403 —— 界面能用，但视觉检查
 * 会看到回退字体。这里只把真实 node_modules 路径补进 fs.allow，不改任何产品配置。
 *
 * 用法：
 *   node node_modules/vite/bin/vite.js --config scripts/interactive-verify/vite.e2e.config.ts --port 5299 --host 127.0.0.1
 */
import { defineConfig, mergeConfig, type UserConfig } from "vite";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import base from "../../frontend/vite.config";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..", "..", "frontend");
const modules = realpathSync(resolve(frontend, "node_modules"));

export default mergeConfig(
  base as UserConfig,
  defineConfig({
    root: frontend,
    server: { fs: { allow: [frontend, modules] } },
  }),
);
