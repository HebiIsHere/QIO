// 第二批独立对抗性探针（batch2）自建 vitest 配置：不改动 frontend/ 下任何文件。
// 探针文件在仓库根的 scripts/final-verify-subagent/batch2/ 下，裸依赖用绝对路径 alias
// 指到 frontend 的安装（与第一批独立复核同一口径，但目录与 include 独立）。
export default {
  root: "D:/qio-dev/qio-final/frontend",
  resolve: {
    alias: {
      pinia: "D:/qio-dev/qio-final/frontend/node_modules/pinia/dist/pinia.mjs",
      "@vue/test-utils": "D:/qio-dev/qio-final/frontend/node_modules/@vue/test-utils/dist/vue-test-utils.esm-bundler.mjs",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["D:/qio-dev/qio-final/frontend/src/vitest.setup.ts"],
    include: ["D:/qio-dev/qio-final/scripts/final-verify-subagent/batch2/**/*.probe.test.ts"],
  },
};
