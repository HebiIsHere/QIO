/**
 * E 路独立核验：**专用 vitest 配置**（只新增于 scripts/audit-verify-8fa8452/，
 * 不修改产品代码，也不修改仓库既有 frontend/vitest.config.ts）。
 *
 * 设计点：
 * 1) 不用 defineConfig / import "vitest/config" —— 本文件在 frontend 之外，
 *    Node 的模块解析找不到 frontend/node_modules/vitest；直接导出裸对象即可。
 * 2) include 用绝对路径指向 qa-e 里的探针；**被测源码**由 QIO_FE_SRC 决定，
 *    因此同一份探针可以在任意 worktree（A/B/C/D/lead）上跑，做差分核验。
 * 3) 裸包名（pinia/vue/@vue/test-utils）alias 到当前 worktree 的 node_modules
 *    （所有 worktree 的 node_modules 都是指向 D:\qio-dev\qio\frontend\node_modules 的 junction）。
 *
 * 用法（在**被测 worktree** 的 frontend 目录下）：
 *   $env:QIO_FE_SRC = 'D:/qio-dev/qa-b/frontend/src'
 *   $env:QIO_FE_NM  = 'D:/qio-dev/qa-b/frontend/node_modules'
 *   npx vitest run --root '..' -c 'D:\qio-dev\qa-e\scripts\audit-verify-8fa8452\vitest.config.ts'
 */
const HERE = "D:/qio-dev/qa-e";
const FE_NM = (process.env.QIO_FE_NM || HERE + "/frontend/node_modules").replace(/\\/g, "/");
const FE_SRC = (process.env.QIO_FE_SRC || HERE + "/frontend/src").replace(/\\/g, "/");
const E_PROBES = HERE + "/scripts/audit-verify-8fa8452";

export default {
  resolve: {
    alias: [
      // 探针文件里写的是 ../../frontend/src/...（相对 qa-e），这里改指向被测 worktree
      { find: /^(\.\.\/\.\.\/|.*)\/frontend\/src\//, replacement: FE_SRC + "/" },
      { find: "pinia", replacement: FE_NM + "/pinia" },
      { find: "vue", replacement: FE_NM + "/vue" },
      { find: "@vue/test-utils", replacement: FE_NM + "/@vue/test-utils" },
    ],
  },
  test: {
    environment: "jsdom",
    include: [
      E_PROBES + "/f07-server-facts.probe.ts",
      E_PROBES + "/f02-f03-send-snapshot.probe.ts",
      E_PROBES + "/f08-f10-f11-interactive.probe.ts",
      E_PROBES + "/f10-min.probe.ts",
    ],
  },
};