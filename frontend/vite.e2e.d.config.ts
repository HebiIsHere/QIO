/**
 * 验收专用 Vite 配置（子智能体 D 本轮的实机截图用；放在 frontend 下是为了让
 * 配置文件里的 import "vite" 能解析到 frontend/node_modules）。
 *
 * 与 scripts/interactive-verify/vite.e2e.config.ts 内容等价：
 * 只把 junction 指向的真实 node_modules 补进 fs.allow，让 @fontsource 字体不再 403，
 * 不改任何产品配置。
 */
import { defineConfig, mergeConfig, type UserConfig } from "vite";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import base from "./vite.config";

const here = dirname(fileURLToPath(import.meta.url));
const modules = realpathSync(resolve(here, "node_modules"));

export default mergeConfig(
  base as UserConfig,
  defineConfig({
    root: here,
    server: { fs: { allow: [here, modules] } },
  }),
);
