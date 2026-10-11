/**
 * E 路独立核验：**组件级探针专用** vitest 配置（需要 @vitejs/plugin-vue 编译 .vue SFC）。
 *
 * 为什么单独一个文件：纯 store/纯函数探针不需要 vue 插件；挂载 ChatDock/Composer 才需要。
 * 本文件在 frontend 之外，不能 import "vitest/config"；用 process.argv[1] 反推 node_modules 位置，
 * 再用 createRequire 从那里解析 @vitejs/plugin-vue（不依赖任何硬编码 worktree 路径）。
 *
 * 用法（在**被测 worktree** 的 frontend 下）：
 *   $env:QIO_FE_SRC='D:/qio-dev/<wt>/frontend/src'
 *   npx vitest run --root '..' -c 'D:\qio-dev\qa-e\scripts\audit-verify-8fa8452\vitest.vue.config.ts'
 */
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

function frontendRoot(): string {
  // argv[1] = <...>\frontend\node_modules\vitest\vitest.mjs
  const marker = "node_modules";
  const raw = String(process.argv[1] || "");
  const normalized = raw.replace(/\\/g, "/");
  const at = normalized.indexOf("/" + marker + "/");
  return at > 0 ? normalized.slice(0, at) : "D:/qio-dev/qa-lead/frontend";
}

const FE = frontendRoot();
// 有些基线 worktree 没有 frontend/node_modules（junction 未建）：允许用 QIO_FE_NM 兜底，
// 否则 createRequire 解析 @vitejs/plugin-vue 会失败。
const NM_CANDIDATE = (process.env.QIO_FE_NM || FE + "/node_modules").replace(/\\/g, "/");
const FE_NM = existsSync(NM_CANDIDATE) ? NM_CANDIDATE : "D:/qio-dev/qa-lead/frontend/node_modules";
const req = createRequire(resolve(FE_NM, "index.js"));
const vue = req("@vitejs/plugin-vue");
const vuePlugin = typeof vue === "function" ? vue() : (vue.default ? vue.default() : vue);

const FE_SRC = (process.env.QIO_FE_SRC || FE + "/src").replace(/\\/g, "/");
const E_PROBES = "D:/qio-dev/qa-e/scripts/audit-verify-8fa8452";

export default {
  plugins: [vuePlugin],
  resolve: {
    alias: [
      { find: /^(\.\.\/\.\.\/|.*)\/frontend\/src\//, replacement: FE_SRC + "/" },
      { find: "pinia", replacement: FE_NM + "/pinia" },
      { find: "vue", replacement: FE_NM + "/vue" },
      { find: "@vue/test-utils", replacement: FE_NM + "/@vue/test-utils" },
      // 基线 worktree 的 src 通过 junction 挂进来时，真实路径在 frontend 之外，
      // 裸包名 @tauri-apps/api/core 会解析失败；显式指到当前项目的 node_modules。
      { find: /^@tauri-apps\/(.*)$/, replacement: FE_NM + "/@tauri-apps/$1" },
    ],
  },
  test: {
    environment: "jsdom",
    include: [E_PROBES + "/f12-pending-entry.probe.ts"],
  },
};