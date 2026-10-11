// 独立验收（E）自建 vitest 配置：不改动 frontend/ 下任何文件。
// 探针文件在 scripts/final-verify-subagent/（仓库根内），不在 frontend 的 node_modules 解析链上，
// 所以测试文件自身的裸依赖（pinia / @vue/test-utils）用绝对路径 alias 指到 frontend 的安装。
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
    include: ["D:/qio-dev/qio-final/scripts/final-verify-subagent/**/*.probe.test.ts"],
  },
};
