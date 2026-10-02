import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

/** 视觉检查用构建（只服务 frontend/visual 下的演示页，不参与产品构建）。 */
export default defineConfig({
  root: "visual",
  plugins: [vue()],
  build: {
    outDir: "../.visual-out",
    emptyOutDir: true,
    target: "es2021",
    rollupOptions: { input: { demo: "visual/timing-demo.html" } },
  },
  server: { port: 5299, strictPort: true, host: "127.0.0.1" },
  preview: { port: 5299, strictPort: true, host: "127.0.0.1" },
});
