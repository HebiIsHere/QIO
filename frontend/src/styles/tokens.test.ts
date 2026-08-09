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
