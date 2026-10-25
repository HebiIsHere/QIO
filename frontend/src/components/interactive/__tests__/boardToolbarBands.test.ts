/**
 * 底部功能栏「栅格契约」的跨组件守卫（2026-10-15 布局重构，主智能体）。
 *
 * 为什么需要它：本轮把功能栏改成「两行主栏 + 按实际可用宽度分档」——
 * 行列关系由 BoardToolbar 的 grid-template-areas 决定，而**网格项**由 SubmitCluster 声明
 * （它自己 display:contents，四个子块各自 grid-area）。这条约定跨两个文件、
 * 没有任何类型系统兜底：任一边改了区域名，布局会静默退化成「内容长度决定行数」的旧行为。
 * 所以这里把契约钉死在源码文本上，并同时守住「不许退回视口断点」。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import BoardToolbar from "../BoardToolbar.vue";

const read = (relative: string) => readFileSync(resolve(process.cwd(), "src", relative), "utf8");
const toolbarSrc = read("components/interactive/BoardToolbar.vue");
const submitSrc = read("components/interactive/SubmitCluster.vue");

/** 取出某个标记之后到文件末尾的片段（用于只看某一档的规则） */
function after(source: string, marker: string): string {
  const at = source.indexOf(marker);
  expect(at, "找不到标记 " + marker).toBeGreaterThan(-1);
  return source.slice(at);
}

describe("功能栏栅格契约（BoardToolbar ↔ SubmitCluster）", () => {
  it("根节点带 data-tb-band，jsdom 量不到宽度时保持最宽档（不误判成最窄）", () => {
    setActivePinia(createPinia());
    const wrapper = mount(BoardToolbar);
    const root = wrapper.get('[data-im="board-toolbar"]');
    expect(root.attributes("data-tb-band")).toBe("wide");
    // 关键钩子一个都不能少（图标化只换外观，不换钩子）
    for (const hook of ["add-menu", "undo", "redo", "submit", "submit-cluster"]) {
      expect(root.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    wrapper.unmount();
  });

  it("三档的行列关系都写死：wide / mid / narrow 各有一份 grid-template-areas", () => {
    const wide = after(toolbarSrc, 'grid-template-areas:');
    expect(wide).toContain("edit");
    expect(wide).toContain("history");
    expect(wide).toContain("stats");
    expect(wide).toContain("actions");
    expect(wide).toContain("notes");

    const mid = after(toolbarSrc, '[data-tb-band="mid"]');
    expect(mid).toMatch(/grid-template-areas:/);
    // mid：上次提交状态与查看过程整组移到独立行（history 不再与 edit 同行）
    expect(mid).toMatch(/"edit\s+edit"/);
    expect(mid).toMatch(/"history\s+history"/);

    const narrow = after(toolbarSrc, '[data-tb-band="narrow"]');
    // narrow：提交操作组独占一行并右对齐
    expect(narrow).toMatch(/grid-template-areas:/);
    expect(narrow).toMatch(/justify-self:\s*end/);
    /*
      右对齐那条必须是**后代**选择器：.actions 隔着 .tb-submit / .submit-cluster 两层，
      而这两层是 display: contents —— 用 > 子选择器会静默失效
      （2026-10-15 真实渲染实测：写子选择器时 justify-self 仍是 auto，组内靠
      flex-end 才右对齐；改成后代选择器后整组盒子也贴右）。
    */
    expect(narrow).toMatch(/\.tb-main\s+:deep\(\.actions\)\s*\{\s*justify-self:\s*end/);
    expect(narrow).not.toMatch(/\.tb-main\s*>\s*:deep\(\.actions\)/);
  });

  it("不再用视口媒体查询决定行列关系（断点必须按功能栏实际可用宽度）", () => {
    // 旧的视口断点会把「窗口很宽但功能栏被挤到 764px」判成宽档，长说明就被压成竖列
    expect(toolbarSrc).not.toMatch(/@media[^{]*max-width:\s*1080px[^{]*\{[^}]*grid-template-areas/);
    // 分档必须来自实测的可用宽度，而不是 window.innerWidth
    expect(toolbarSrc).toContain("toolbarAvailableWidth");
    expect(toolbarSrc).toContain("toolbarBand");
  });

  it("提交区容器与 SubmitCluster 根都是 display:contents，四个子块自己声明 grid-area", () => {
    expect(toolbarSrc).toMatch(/\.tb-submit\s*\{[^}]*display:\s*contents/);
    expect(submitSrc).toMatch(/\.submit-cluster\s*\{[^}]*display:\s*contents/);
    for (const [selector, area] of [
      [".sc-history", "history"],
      [".sc-stats", "stats"],
      [".sc-notes", "notes"],
      [".actions", "actions"],
    ] as const) {
      const block = submitSrc.slice(submitSrc.indexOf(selector + " {"));
      expect(block, selector + " 缺少 grid-area").toContain("grid-area: " + area);
    }
  });

  it("按状态出现的两块内容不在时，不许留空的网格行（默认态就是两行）", () => {
    // 写死的网格行即使没有网格项也占一条 row-gap —— 必须用 :has() 按「这一行有没有内容」收掉
    for (const block of [toolbarSrc]) {
      expect(block).toContain(':not(:has([data-im="submit-history"]))');
      expect(block).toContain(':not(:has([data-im="submit-notes"]))');
    }
    // mid / narrow 都需要「缺哪一块少哪一行」的规则
    expect(toolbarSrc).toMatch(/\[data-tb-band="mid"\] \.tb-main:not\(:has\(\[data-im="submit-history"\]\)\)/);
    expect(toolbarSrc).toMatch(/\[data-tb-band="narrow"\] \.tb-main:not\(:has\(\[data-im="submit-notes"\]\)\)/);
    // 「两块都没有」必须是更高特异度的两段 :not，否则会被只缺一块的规则盖住
    expect(toolbarSrc).toMatch(
      /:not\(:has\(\[data-im="submit-history"\]\)\):not\(:has\(\[data-im="submit-notes"\]\)\)/,
    );
  });

  it("展开面板用功能栏完整宽度、在主栏上方，且不参与主栏高度", () => {
    const panels = submitSrc.slice(submitSrc.indexOf(".sc-panels"));
    expect(panels).toMatch(/position:\s*absolute/);
    expect(panels).toMatch(/left:\s*0/);
    expect(panels).toMatch(/right:\s*0/);
    expect(panels).toMatch(/bottom:\s*calc\(100%/);
    // 主栏高度只由主栏内容决定：面板不许写进 .tb-main 的行
    expect(toolbarSrc).not.toMatch(/"notes\s+panels"/);
  });
});
