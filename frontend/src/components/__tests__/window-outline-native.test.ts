/**
 * 桌面窗口外轮廓（0.3.2）契约：窗口四角/边缘由谁裁切。
 *
 * 0.3.2 的窗口外壳有两种模式（由壳的 QIO_WINDOW_FRAME 决定）：
 * - css：透明窗 + 前端 .window-frame__surface 画 20px 圆角与自绘阴影，壳侧 DWM 必须
 *   DONOTROUND + 无边框（否则系统圆角/边框再画一层）；
 * - native：decorations:true，系统窗框/阴影/圆角接管，前端留白/圆角/阴影归零；
 * - dwm：0.3.1 现状对照（decorations:false + 系统阴影 + DWMWCP_ROUND）。
 *
 * 网页层唯一的圆角/实底/阴影只在 .window-frame__surface 上；.app-shell 不得再画圆角
 * （那会得到「方窗口里再画一个圆角矩形」）。壳源码尚未落地三模式时，保留对 0.3.1
 * 现状的断言（测试不空转），落地后自动切到新契约。
 */
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const ROOT = process.cwd();
const read = (rel: string) => readFileSync(resolve(ROOT, rel), "utf8");
const conf = JSON.parse(read("src-tauri/tauri.conf.json")) as {
  app: { windows?: Array<Record<string, unknown>> };
};
const appVue = read("src/App.vue");
const tokensCss = read("src/styles/tokens.css");
const baseCss = read("src/styles/base.css");

function rustSources(): string {
  /* 0.3.2 壳把窗口层拆到 src-tauri/src/window_frame/ 子目录，必须递归收集 */
  const root = resolve(ROOT, "src-tauri", "src");
  let out = "";
  const walk = (dir: string): void => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (name.endsWith(".rs")) out += readFileSync(full, "utf8");
    }
  };
  walk(root);
  return out;
}
const rust = rustSources();
const shellSupportsFrame = rust.includes("QIO_WINDOW_FRAME");

describe("桌面窗口外轮廓：圆角/实底/阴影的归属", () => {
  it("网页层圆角只在 .window-frame__surface；.app-shell 不伪装原生圆角", () => {
    const surfaceRule = appVue.match(/\.window-frame__surface\s*\{[^}]*\}/)?.[0] ?? "";
    expect(surfaceRule, ".window-frame__surface 样式块").toBeTruthy();
    expect(surfaceRule).toContain("border-radius: var(--window-radius)");
    expect(surfaceRule).toContain("box-shadow: var(--window-shadow)");
    expect(surfaceRule).toContain("overflow: hidden");

    const shellRule = appVue.match(/\.app-shell\s*\{[^}]*\}/)?.[0] ?? "";
    expect(shellRule, ".app-shell 样式块").toBeTruthy();
    expect(shellRule).not.toContain("border-radius");
    expect(shellRule).not.toMatch(/100vw|100vh/);
  });

  it("窗口根层透明、留白/圆角/阴影令牌唯一来源 tokens.css", () => {
    expect(baseCss).toMatch(/html,body\{height:100%;background:transparent;\}/);
    expect(tokensCss).toMatch(/--window-frame-pad:\s*28px/);
    expect(tokensCss).toMatch(/--window-radius:\s*20px/);
    expect(tokensCss).toMatch(/--window-shadow:/);
    expect(tokensCss).toMatch(/:root\[data-window-state="maximized"\]/);
    expect(tokensCss).toMatch(/:root\[data-window-state="snapped"\]/);
  });

  it("css 模式：透明窗 + 壳不请求系统圆角（防双层圆角/双层阴影）", () => {
    if (!shellSupportsFrame) {
      /* 改造期（T2 尚未落地）：0.3.1 现状 —— 无边框 + 保留系统阴影 + DWM 圆角 */
      const win = conf.app.windows?.[0] ?? {};
      expect(win.decorations).toBe(false);
      expect(win.shadow).toBe(true);
      expect(win.transparent).not.toBe(true);
      for (const token of [
        "DWMWA_WINDOW_CORNER_PREFERENCE",
        "DWMWCP_ROUND",
        "DWMWA_BORDER_COLOR",
        "DWMWA_COLOR_NONE",
        "DwmSetWindowAttribute",
      ]) {
        expect(rust, token).toContain(token);
      }
      return;
    }
    expect(rust).toContain("QIO_WINDOW_FRAME");
    expect(rust).toMatch(/transparent/i);
    expect(rust).toMatch(/DWMWCP_DONOTROUND/);
    expect(rust).toMatch(/DWMWA_BORDER_COLOR|DWMWA_COLOR_NONE/);
  });

  it("native/dwm 回退存在，且前端三态归零规则覆盖两种模式", () => {
    if (shellSupportsFrame) {
      expect(rust).toContain("native");
      expect(rust).toContain("dwm");
      expect(rust).toMatch(/decorations/i);
      expect(rust).toMatch(/DWMWCP_ROUND/);
    } else {
      // T2 未落地时：至少保住 0.3.1 的 DWM 圆角链路（主窗口 + setup 调用）
      expect(rust).toContain("apply_windows_window_outline");
      expect(rust).toContain('get_webview_window("main")');
    }
    expect(tokensCss).toMatch(/:root\[data-window-frame="native"\]/);
    expect(tokensCss).toMatch(/:root\[data-window-frame="dwm"\]/);
  });
});
