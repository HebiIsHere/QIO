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
