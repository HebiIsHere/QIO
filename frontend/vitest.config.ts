import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/vitest.setup.ts"],
    // 既有 *.test.ts 之外，本轮安全修复的测试按契约命名为 sr-e-*/sr-f-*.spec.ts
    include: ["src/**/*.test.ts", "src/**/*.spec.ts"],
  },
});
