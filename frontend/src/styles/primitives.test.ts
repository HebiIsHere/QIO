import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

// 路径放进变量：字面量会被 Vite 当成资源 URL 重写，读不到真实文件
const baseCssPath = "./base.css";
const css = readFileSync(new URL(baseCssPath, import.meta.url), "utf8");

/**
 * 组件原语（2026-09-15 第四阶段）。
 *
 * 这些断言是「统一组件语言」的底线：原语类必须存在，且状态表达必须成套
 * （hover / active / selected / focus 缺一不可），否则各模块又会自己发明一套。
 */
describe("base.css 组件原语", () => {
  it("卡片 / 行 / 工具条 / 标签 / 状态徽章 / 就地编辑 原语齐全", () => {
    for (const cls of [".qio-card", ".qio-row", ".qio-toolbar", ".qio-tag", ".qio-state", ".qio-inline-edit", ".qio-lift"]) {
      expect(css).toContain(cls);
    }
  });

  it("卡片状态用 data-state 表达（状态推进原位发生，不换新卡）", () => {
    for (const s of ['[data-state="running"]', '[data-state="waiting"]', '[data-state="ready"]', '[data-state="failed"]']) {
      expect(css).toContain(s);
    }
  });

  it("列表行 hover / active / selected / focus 四态可区分", () => {
    expect(css).toContain(".qio-row:hover");
    expect(css).toContain(".qio-row.is-active");
    expect(css).toContain('.qio-row.is-selected,.qio-row[aria-selected="true"]');
    expect(css).toContain(".qio-row:focus-visible");
  });

  it("状态徽章覆盖 ok / warn / err / info / quiet", () => {
    for (const s of [".qio-state.ok", ".qio-state.warn", ".qio-state.err", ".qio-state.info", ".qio-state.quiet"]) {
      expect(css).toContain(s);
    }
  });

  it("晶体玻璃材质靠多重线索成立（不是一道高光）", () => {
    expect(css).toContain(".qio-glass{");
    expect(css).toContain("--glass-edge-top");
    expect(css).toContain("--glass-inner");
    expect(css).toContain("--glass-tint-warm");
    expect(css).toContain("--glass-tint-cool");
    // 低雾化：模糊半径必须小（重毛玻璃不是 QIO 的方向）
    const blur = css.match(/backdrop-filter:blur\(var\(--glass-blur\)\)/);
    expect(blur).not.toBeNull();
  });

  /**
   * 本轮统一改造（2026-10-26）：这些原语是“表面”，圆角必须来自唯一 token。
   * 每条规则按选择器取块，避免整文件 contains 把别的规则当成通过。
   */
  it("本轮涉及的基础原语圆角统一为 --qio-ui-radius", () => {
    const selectors = [
      ".qio-btn", ".qio-btn.mini", ".qio-input", ".qio-select", ".qio-select-menu",
      ".qio-badge", ".qio-tag", ".qio-state", ".qio-row", ".qio-card",
      ".qio-inline-edit", ".qio-floating", ".qio-confirm--inline",
      ".qio-confirm--popover,.qio-confirm--layer",
    ];
    for (const sel of selectors) {
      const escaped = sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const blocks = css.match(new RegExp(escaped + "\\{[^}]*\\}", "g")) ?? [];
      expect(blocks.length, sel).toBeGreaterThan(0);
      // 主规则必须用 token；同选择器的补充规则（例如 textarea.qio-input 只调高度）
      // 如果声明了 border-radius 也必须用 token，没声明就不要求。
      expect(blocks.some((block) => block.includes("var(--qio-ui-radius)")), sel).toBe(true);
      for (const block of blocks) {
        if (block.includes("border-radius:")) expect(block, sel).toContain("var(--qio-ui-radius)");
      }
    }
  });

  it("中央弹窗共享骨架与动效工具类存在（QModal/QConfirm 共用）", () => {
    for (const cls of [".qio-modal-head", ".qio-modal-title", ".qio-modal-kicker", ".qio-modal-body", ".qio-modal-actions", ".qio-modal-scrim", ".qio-pop-in"]) {
      expect(css).toContain(cls);
    }
    // 窄窗纵排 + 等宽同高
    expect(css).toMatch(/\.qio-modal-actions > \*\{[^}]*flex:1 1 0/);
    expect(css).toMatch(/@media \(max-width:480px\)[\s\S]{0,200}?\.qio-modal-actions\{flex-direction:column/);
  });

  it("确认层三档与过渡工具类齐全", () => {
    for (const cls of [
      ".qio-confirm--inline", ".qio-confirm--popover", ".qio-confirm--layer", ".qio-confirm-scrim",
      ".qio-fade-enter-active", ".qio-rise-enter-active", ".qio-list-move", ".qio-collapse-enter-active", ".qio-swap-enter-active",
    ]) {
      expect(css).toContain(cls);
    }
  });
});

/**
 * reduced-motion 语义（第四阶段重写）。
 *
 * 旧实现把所有时长归零 → 状态变化变成瞬切，连续性丢失。
 * 新语义：保留必要的淡入淡出、去掉位移与弹性、时长压缩。
 */
describe("reduced-motion 降级语义", () => {
  it("不再把时长归零（保留连续性）", () => {
    expect(css).not.toContain("transition-duration:.001ms");
    expect(css).not.toContain("animation-duration:.001ms");
  });

  it("把位移令牌归零、低频弹性曲线退化为标准曲线", () => {
    expect(css).toMatch(/--shift-1:0px/);
    expect(css).toMatch(/--ease-3-settle:var\(--ease-1\)/);
  });

  it("系统偏好可被「标准」显式覆盖（与设置项语义一致）", () => {
    expect(css).toContain(':root:not([data-motion="standard"])');
  });
});
