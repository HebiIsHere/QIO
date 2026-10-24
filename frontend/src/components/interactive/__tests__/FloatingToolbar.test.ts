import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(process.cwd(), "src");
const toolbar = readFileSync(resolve(SRC, "components/interactive/BoardToolbar.vue"), "utf8");
const submit = readFileSync(resolve(SRC, "components/interactive/SubmitCluster.vue"), "utf8");

describe("悬浮工具栏精简", () => {
  it("搜索已交给全局栏，底部工具栏不再有第二个搜索入口", () => {
    expect(toolbar).not.toContain('data-im="search-toggle"');
    expect(toolbar).not.toContain("BoardSearchPanel");
  });

  it("悬浮模式只保留图标状态 + 提交，不铺统计文字", () => {
    expect(submit).toContain("compact?: boolean");
    expect(submit).toContain('v-if="!compact"');
    expect(submit).toContain("sc-details-icon");
  });
});
