import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
const cssPath = "./tokens.css";
const css = readFileSync(new URL(cssPath, import.meta.url), "utf8");
const required = ["--bg-base", "--bg-elevated", "--text-strong", "--accent", "--serif", "--mono", "data-theme=\"light\""];
describe("tokens.css", () => {
  it("包含必需令牌与亮色主题", () => {
    for (const tok of required) expect(css).toContain(tok);
  });
  it("暗色为默认", () => expect(css.indexOf("data-theme")).toBeGreaterThan(css.indexOf(":root {")));
  it("暗色关键值与规范一致", () => {
    expect(css).toContain("--bg-base:#171015");
    expect(css).toContain("--accent:#c51b7d");
  });
  it("亮色关键值与规范一致", () => {
    const lightBlock = css.slice(css.indexOf('data-theme="light"'));
    expect(lightBlock).toContain("--bg-base:#fcfcfb");
    expect(lightBlock).toContain("--link:#b0136a");
    expect(lightBlock).toContain("--approve-bg:#2a6e4f");
    expect(lightBlock).toContain("--reject-bg:#6e2a2a");
  });
  it("保留既有组件引用的遗留令牌", () => {
    const legacy = ["--bg-overlay", "--bg-panel", "--bg-accent-subtle", "--accent-veil", "--accent-softer", "--border-danger", "--success-soft", "--danger-soft", "--approve-bg", "--reject-bg"];
    for (const tok of legacy) expect(css).toContain(tok);
  });
});

/**
 * 第四阶段（2026-09-15）动效与材质令牌。
 *
 * 这些断言防的是「又慢慢退回硬编码」：三层时长、曲线集、位移令牌、晶体玻璃
 * 必须成体系地存在，并且旧名（--dur-*）只能是别名，不能重新变成独立数值。
 */
describe("tokens.css v3 动效与材质", () => {
  const tiers = [
    "--mo-1-press", "--mo-1-state", "--mo-1-fast", "--mo-1-exit", "--mo-1-menu", "--mo-1-wide",
    "--mo-2-in", "--mo-2-move", "--mo-2-out", "--mo-2-wide",
    "--mo-3-prepare", "--mo-3-expand", "--mo-3-settle", "--mo-3-collapse",
  ];
  const curves = ["--ease-1", "--ease-1-out", "--ease-2", "--ease-2-out", "--ease-3-in", "--ease-3-settle", "--ease-3-out"];
  const shifts = ["--shift-1", "--shift-2", "--shift-4", "--shift-8", "--shift-panel"];
  const glass = [
    "--glass-bg", "--glass-bg-strong", "--glass-blur", "--glass-sat", "--glass-border",
    "--glass-edge-top", "--glass-inner", "--glass-tint-warm", "--glass-tint-cool", "--glass-shadow",
  ];

  it("三层动效时长令牌齐全", () => {
    for (const t of tiers) expect(css).toContain(t);
  });

  it("曲线集齐全且刻意保持少", () => {
    for (const c of curves) expect(css).toContain(c);
  });

  it("低频关键转场的曲线有柔性收束但不过冲成 bounce", () => {
    const m = css.match(/--ease-3-settle:cubic-bezier\(([^)]+)\)/);
    expect(m).not.toBeNull();
    const parts = m![1].split(",").map((n) => Number(n.trim()));
    expect(parts).toHaveLength(4);
    // cubic-bezier(x1, y1, x2, y2)。y1 略大于 1 = 单次柔和过冲；
    // 过冲幅度必须小（>1.12 就是明显回弹，属游戏化弹跳，不允许）。
    expect(parts[1]).toBeGreaterThan(1);
    expect(parts[1]).toBeLessThan(1.12);
    // 起手不僵硬：控制点 x1 小、y1 抬升（不是线性，也不是硬弹）
    expect(parts[0]).toBeLessThan(0.4);
    expect(parts[2]).toBeLessThan(0.5);
    // 终点必须回到 1（不能停在过冲位置）
    expect(parts[3]).toBe(1);
  });

  it("位移令牌齐全（组件里不许写裸位移）", () => {
    for (const s of shifts) expect(css).toContain(s);
  });

  it("旧时长名全部降级为别名，指向分层令牌", () => {
    expect(css).toContain("--dur-planet-in:var(--mo-3-expand)");
    expect(css).toContain("--dur-press:var(--mo-1-press)");
    expect(css).toContain("--dur-panel:var(--mo-2-move)");
    expect(css).toContain("--ease:var(--ease-1)");
    // 不允许出现「旧名 = 裸数值」的回退
    expect(css).not.toMatch(/--dur-(fast|base|slow|press|toggle|menu):\s*\d/);
  });

  it("晶体玻璃令牌在暗色与净白两套主题里都定义", () => {
    const light = css.slice(css.indexOf('data-theme="light"'));
    for (const g of glass) {
      expect(css).toContain(g);
      expect(light).toContain(g);
    }
  });
});
