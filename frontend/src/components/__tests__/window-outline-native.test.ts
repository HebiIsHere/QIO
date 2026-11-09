/**
 * 桌面窗口外轮廓（用户提示词 §六）的原生裁切链路守卫。
 *
 * 本机当前没有 Rust 工具链，原生部分无法在这里编译/运行；但「窗口四角由谁裁切」不能
 * 在网页里假装 —— 所以用源码/配置契约把关键决定钉住，避免以后又被改回「只在 .app-shell
 * 上加 border-radius」：
 *
 * 1) 无边框窗口保留阴影（DWM 帧），窗口/网页底设为深色：Tauri 官方配置文档明确说
 *    `decorations: false` + `shadow: true` 会给窗口加 1px 白边，且 Windows 11 上带圆角；
 *    深色窗口底同时避免启动白闪或圆角边缘露出白/黑直角底。
 * 2) 壳里显式请求 Windows 11 系统圆角（DWMWCP_ROUND）并去掉 DWM 边框
 *    （DWMWA_BORDER_COLOR = DWMWA_COLOR_NONE）。半径由系统决定（系统不提供任意像素值），
 *    最大化 / 贴边 / 还原也由系统处理；壳侧不自己监听窗口状态、不写固定圆角。
 * 3) 网页层不许给 .app-shell 加 border-radius 伪装原生圆角（那只会得到「方窗口里再画
 *    一个圆角矩形」，四角仍露出窗口底色）。
 * 4) 属性只在 cfg(windows) 下调用主窗口，失败只记日志，不影响启动。
 *
 * 必须在真机上验收、这里无法证明的项：四角实际外观（系统半径不是精确 20px）、阴影、
 * 拖动/缩放、多 DPI、最大化/贴边/还原、点击命中区。见本轮交付报告。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const ROOT = process.cwd();
const conf = JSON.parse(readFileSync(resolve(ROOT, "src-tauri/tauri.conf.json"), "utf8"));
const cargo = readFileSync(resolve(ROOT, "src-tauri/Cargo.toml"), "utf8");
const mainRs = readFileSync(resolve(ROOT, "src-tauri/src/main.rs"), "utf8");
const appVue = readFileSync(resolve(ROOT, "src/App.vue"), "utf8");
const tokensCss = readFileSync(resolve(ROOT, "src/styles/tokens.css"), "utf8");

describe("桌面窗口外轮廓：原生裁切链路", () => {
  it("无边框窗口保留阴影，并把窗口/网页底设为深色（避免 1px 白边与启动白闪）", () => {
    const win = conf.app.windows[0];
    expect(win.decorations).toBe(false);
    expect(win.shadow).toBe(true);
    expect(win.transparent).not.toBe(true);
    // 原生窗口底必须与默认主题的 --bg-base 同色：启动/缩放时圆角边缘不会闪白或露出黑底
    const darkBase = tokensCss.match(/--bg-base:\s*(#[0-9a-fA-F]{6})/)?.[1] ?? "";
    expect(darkBase).toBeTruthy();
    expect(String(win.backgroundColor).toLowerCase()).toBe(darkBase.toLowerCase());
  });

  it("壳里显式请求 Win11 系统圆角，并去掉 DWM 边框", () => {
    expect(cargo).toContain('"Win32_Graphics_Dwm"');
    for (const token of [
      "DWMWA_WINDOW_CORNER_PREFERENCE",
      "DWMWCP_ROUND",
      "DWMWA_BORDER_COLOR",
      "DWMWA_COLOR_NONE",
      "DwmSetWindowAttribute",
    ]) {
      expect(mainRs, token).toContain(token);
    }
    // 只在 Windows 编译；非 Windows 平台不引入 DWM
    expect(mainRs).toMatch(/#\[cfg\(windows\)\]\s*fn apply_windows_window_outline/);
    // 只对主窗口应用，且 setup 里真的调用了
    expect(mainRs).toContain('get_webview_window("main")');
    expect(mainRs).toContain("apply_windows_window_outline(&window)");
  });

  it("网页层不伪装原生圆角：.app-shell 不加 border-radius", () => {
    const shellRule = appVue.match(/\.app-shell\s*\{[^}]*\}/)?.[0] ?? "";
    expect(shellRule).toBeTruthy();
    expect(shellRule).not.toContain("border-radius");
  });
});
