/**
 * 0.3.2 窗口外框契约（源文本扫描，无需浏览器）。
 *
 * 透明留白 + CSS 20px 圆角是「窗口级」行为，分散在 tokens/base/App/各固定层/顶部锚定/
 * 视口换算里；任何一处漏改都不会让类型检查报错，但会让浮层画到透明留白带、让 100vh 算进
 * 留白、或让菜单越界。这里把关键事实逐条钉住（与 utils/windowFrame 的运行时单测互补）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = resolve(process.cwd(), "src");
const read = (rel: string) => readFileSync(resolve(SRC, rel), "utf8");
const tokens = read("styles/tokens.css");
const base = read("styles/base.css");
const app = read("App.vue");
const utils = read("utils/windowFrame.ts");

describe("窗口外框令牌（唯一来源 tokens.css）", () => {
  it("css 模式：28px 留白 + 20px 圆角 + soft·A 阴影（亮色 alpha 减半）", () => {
    expect(tokens).toMatch(/--window-frame-pad:\s*28px/);
    expect(tokens).toMatch(/--window-radius:\s*20px/);
    expect(tokens).toMatch(/--window-frame-inset:\s*var\(--window-frame-pad\)/);
    expect(tokens).toMatch(/--window-shadow:\s*0 5px 20px rgba\(0, 0, 0, \.40\),\s*0 1px 4px rgba\(0, 0, 0, \.28\)/);
    const light = tokens.slice(tokens.indexOf(':root[data-theme="light"]'));
    expect(light).toMatch(/--window-shadow:\s*0 5px 20px rgba\(20, 16, 24, \.20\)/);
  });

  it("最大化/贴靠/native/dwm 时留白、圆角、自绘阴影归零", () => {
    for (const selector of [
      ':root[data-window-state="maximized"]',
      ':root[data-window-state="snapped"]',
      ':root[data-window-frame="native"]',
      ':root[data-window-frame="dwm"]',
    ]) {
      expect(tokens).toContain(selector);
    }
    expect(tokens).toMatch(/--window-frame-pad:\s*0px/);
    expect(tokens).toMatch(/--window-frame-inset:\s*0px/);
    expect(tokens).toMatch(/--window-radius:\s*0px/);
    expect(tokens).toMatch(/--window-shadow:\s*none/);
  });

  it("根层透明，实底/圆角/阴影只在 .window-frame__surface 上画一次", () => {
    expect(base).toMatch(/html,body\{height:100%;background:transparent;\}/);
    expect(base).toMatch(/html\[data-window-frame="native"\]\s*\.window-controls\{display:none;\}/);
    const surface = app.match(/\.window-frame__surface\s*\{[^}]*\}/)?.[0] ?? "";
    expect(surface).toContain("border-radius: var(--window-radius)");
    expect(surface).toContain("box-shadow: var(--window-shadow)");
    expect(surface).toContain("overflow: hidden");
    const shell = app.match(/\.app-shell\s*\{[^}]*\}/)?.[0] ?? "";
    expect(shell).not.toMatch(/100vw|100vh/);
    expect(app).toContain("useWindowFrame()");
  });
});

describe("全窗 fixed 层 / 顶部锚定 / 视口换算都读同一组令牌", () => {
  it("全窗 fixed 层内缩到可见内容区（不再有 inset:0 级遮罩）", () => {
    const files = [
      "styles/base.css",
      "components/approval/UnifiedApprovalModal.vue",
      "components/planet/MaintenanceRestoreDialog.vue",
      "components/onboarding/OnboardingWizard.vue",
      "components/interactive/v2/ImpactConfirmLayer.vue",
      "components/WindowChrome.vue",
      "views/ConversationView.vue",
      "components/PlanetBoot.vue",
      "components/ui/QTipLayer.vue",
    ];
    for (const rel of files) {
      expect(read(rel), rel).toContain("var(--window-frame-inset)");
    }
    // 星球页是 fixed 整层但用 clip-path 裁到可见圆角区（转场数学与 inset:0 保持不变）
    expect(read("views/PlanetView.vue")).toContain("clip-path: inset(var(--window-frame-inset) round var(--window-radius))");
  });

  it("顶部锚定层用 --chrome-top（留白 + 标题条），不从窗口客户区顶算", () => {
    for (const rel of [
      "App.vue",
      "components/feedback/TopNotice.vue",
      "components/feedback/GlobalIssuePanel.vue",
      "components/conversation/UnifiedApprovalLayer.vue",
      "components/WindowChrome.vue",
    ]) {
      expect(read(rel), rel).toMatch(/var\(--chrome-top/);
    }
    expect(read("components/feedback/feedbackOffsets.ts")).toContain("--window-frame-inset");
  });

  it("需要的像素数走 utils/windowFrame（内容视口/内容矩形/钳制），100vh 修正", () => {
    for (const token of ["contentRect", "contentViewport", "frameInsetPx", "clampToContentRect", "windowRadiusPx"]) {
      expect(utils, token).toContain(token);
    }
    for (const rel of [
      "composables/useFloatingWindow.ts",
      "components/ui/QTipLayer.vue",
      "components/interactive/v2/InteractiveChromeTools.vue",
      "components/interactive/SelectionMenu.vue",
      "views/InteractiveView.vue",
      "components/interactive/v2/InteractiveConversation.vue",
      "stores/conversationShell.ts",
      "components/MessageStream.vue",
      "views/SettingsView.vue",
    ]) {
      expect(read(rel), rel).toContain("windowFrame");
    }
    expect(read("components/DevTaskEntry.vue")).toContain("2 * var(--window-frame-inset)");
    // QModal 不再自己算「100vh - 2*inset - 2*sp6」：它的高度上限相对**遮罩内容盒**，
    // 遮罩由 base.css 用 inset: var(--window-frame-inset) 定位并按内容视口留 padding。
    // 这样全站弹窗只有一处读窗口令牌，不各算一套（#82 的根因之一就是这套算法与 fixed 包含块打架）。
    const qmodal = read("components/ui/QModal.vue");
    expect(qmodal).toContain("max-height: 100%");
    // 注释里提到「不用 100vh」是允许的；这里禁止的是把它写成声明值（100vh;）
    expect(qmodal).not.toContain("100vh;");
    expect(read("styles/base.css")).toMatch(/\.qio-modal-scrim\{[^}]*inset:var\(--window-frame-inset\)/);
  });

  it("视口 JS 不再用裸 innerWidth/innerHeight（统一走 utils/windowFrame）", () => {
    const mustNot = [
      "components/ui/QTipLayer.vue",
      "composables/useQioTip.ts",
      "components/interactive/v2/InteractiveChromeTools.vue",
      "components/interactive/SelectionMenu.vue",
      "views/InteractiveView.vue",
      "components/interactive/v2/InteractiveConversation.vue",
      "stores/conversationShell.ts",
      "components/MessageStream.vue",
      "views/SettingsView.vue",
    ];
    for (const rel of mustNot) {
      expect(read(rel), rel).not.toMatch(/window\.innerWidth|window\.innerHeight/);
    }
    // useFloatingWindow 是唯一的换算点（frameViewport 内部减 2*inset），其余一律用 util
    const floating = read("composables/useFloatingWindow.ts");
    expect(floating).toContain("frameViewport");
    expect(floating).toContain("frameInsetPx");
  });
});

describe("WindowChrome：命中区上报 + caption-hover（0.3.2 壳通道）", () => {
  it("上报 qio_set_hit_regions（含 interactive 洞）并监听 caption-hover", () => {
    const chrome = read("components/WindowChrome.vue");
    expect(chrome).toContain("setHitRegions");
    expect(chrome).toContain("listenCaptionHover");
    expect(chrome).toContain("listenWindowState");
    expect(chrome).toContain("interactive: collectHoles()");
    expect(chrome).toContain("qio://caption-hover");
    expect(chrome).toContain("is-hover");
    expect(read("services/desktop.ts")).toContain("qio://window-state");
    expect(read("services/desktop.ts")).toContain("qio://frame-ready");
    expect(read("services/desktop.ts")).toContain("qio_set_hit_regions");
  });
});
