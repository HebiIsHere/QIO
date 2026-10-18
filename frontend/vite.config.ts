import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const host = process.env.TAURI_DEV_HOST;
const here = dirname(fileURLToPath(import.meta.url));
let modules = resolve(here, "node_modules");
try {
  modules = realpathSync(modules);
} catch {
  // node_modules 不存在时保持原路径：配置本身不该因为环境而崩
}

// 「关于」分区内联的是**仓库根目录**的 LICENSE 与 THIRD-PARTY-NOTICES.md（唯一来源，
// 不在前端目录里复制第二份）。Vite 默认的 fs 白名单只覆盖 frontend/，所以要显式放行
// 仓库根目录，否则开发 / e2e / 视觉检查下设置页会因为 403 直接加载失败。
const repoRoot = resolve(here, "..");

export default defineConfig({
  plugins: [vue()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
    fs: { allow: [here, repoRoot, modules] },
  },
  envPrefix: ["VITE_", "TAURI_"],
  build: {
    target: "es2021",
    minify: "esbuild",
    sourcemap: false,
  },
});