import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [vue()],
  // 设置页「关于」分区内联仓库根目录的 LICENSE / THIRD-PARTY-NOTICES.md（唯一来源），
  // 它们在前端目录之外，必须显式放行，否则测试环境会 Denied ID。
  server: { fs: { allow: [here, resolve(here, "..")] } },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/vitest.setup.ts"],
    // 既有 *.test.ts 之外，本轮安全修复的测试按契约命名为 sr-e-*/sr-f-*.spec.ts
    include: ["src/**/*.test.ts", "src/**/*.spec.ts"],
  },
});
