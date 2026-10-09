/**
 * F 组验证专用 vitest 配置（不改仓库自带的 vitest.config.ts）。
 *
 * 仓库自带的 vitest.config.ts 只收集 src 下的 test.ts 文件，而 F 组按约定新建的是
 * `rm-f-*.spec.ts`。这里用一份独立配置显式收集 F 组的用例，便于一键回归；
 * 不含 vue 插件（F 组用例只 import store / service，不 import .vue）。
 */
import { fileURLToPath } from "node:url";

export default {
  root: fileURLToPath(new URL("../../frontend/", import.meta.url)),
  test: {
    environment: "jsdom",
    setupFiles: ["./src/vitest.setup.ts"],
    include: ["src/**/__tests__/rm-f-*.spec.ts"],
  },
};
