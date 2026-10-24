import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  resolve(process.cwd(), "src/components/interactive/BoardCanvas.vue"),
  "utf8",
);

function block(selector: string): string {
  const start = source.indexOf(selector);
  if (start < 0) return "";
  const open = source.indexOf("{", start);
  const close = source.indexOf("}", open);
  return open < 0 || close < 0 ? "" : source.slice(open + 1, close);
}

describe("板面点阵纸", () => {
  it("亮色板面是白色底，不再使用灰黄底", () => {
    const viewport = block(".board-viewport {");
    expect(viewport).toContain("--bg-surface");
    expect(viewport).not.toContain("--bg-inset");
    const surface = block(".board-surface {");
    expect(surface).toContain("--bg-surface");
  });

  it("使用主题色点阵：24px 间距、1px 点、低透明度", () => {
    const surface = block(".board-surface {");
    expect(surface).toContain("radial-gradient");
    expect(surface).toContain("24px 24px");
    expect(surface).toMatch(/color-mix\([^)]*var\(--accent\)/);
    expect(surface).toContain("1px");
  });
});
