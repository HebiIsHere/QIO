/**
 * 界面可读性 / 密度 / 入口的源码守卫（C 本轮）。
 *
 * 这些扫描测试守的是「很容易悄悄退回去」的东西：小字又被换成装饰色、窄窗口又整行藏掉操作说明、
 * 失败入口又只在编辑态可见。它们读源码文本，跑得快，不需要浏览器；
 * 真实像素对比度与工具栏高度由 scripts/closure-c-verify 的实机报告给出。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const read = (relative: string) => readFileSync(resolve(process.cwd(), "src", relative), "utf8");
const chatDock = read("components/interactive/ChatDock.vue");
const submitCluster = read("components/interactive/SubmitCluster.vue");
const boardCard = read("components/interactive/BoardCard.vue");
const impact = read("components/interactive/ImpactConfirmDialog.vue");
const view = read("views/InteractiveView.vue");
const background = "var(--bg-elevated)";

/** 取出某个选择器的规则块（第一个匹配） */
function block(source: string, selector: string): string {
  const start = source.indexOf(selector);
  expect(start, "找不到选择器 " + selector).toBeGreaterThan(-1);
  const open = source.indexOf("{", start);
  const close = source.indexOf("}", open);
  return source.slice(open + 1, close);
}

describe("亮色辅助文字的可读性（有操作意义的小字不能用装饰色）", () => {
  it("快捷键说明：字号走令牌，颜色走可读色，不再是 10.5px + --text-faint", () => {
    const hint = block(chatDock, ".panel-hint {");
    expect(hint).toContain("font-size: var(--fs-xs)");
    expect(hint).toContain("color: var(--text-muted)");
    expect(chatDock).not.toContain("font-size: 10.5px");
  });

  it("草稿状态、占位说明、更早消息说明同样提到可读色", () => {
    expect(chatDock).toMatch(/\.stream-older \{ color: var\(--text-muted\); \}/);
    expect(chatDock).toContain(".input::placeholder { color: var(--text-muted); }");
    // .draft-status 块里带 --text-muted
    const draft = block(chatDock, ".draft-status {");
    expect(draft).toContain("color: var(--text-muted)");
  });

  it("480px 这类窄窗口不再整行藏掉快捷键说明（信息不可达）", () => {
    const narrow = chatDock.slice(chatDock.indexOf("@media (max-width: 560px)"));
    expect(narrow).not.toMatch(/\.panel-hint\s*\{\s*display:\s*none/);
  });

  it("提交区详情里的草稿提示与失败原因都用可读色，不靠装饰色", () => {
    expect(submitCluster).toMatch(/\.line\.faint \{ color: var\(--text-muted\); \}/);
    expect(submitCluster).toMatch(/\.failure-reason \{ color: var\(--text-primary\); \}/);
    expect(submitCluster).toMatch(/\.failure-label \{ color: var\(--danger\); \}/);
  });

  it("这几处不出现硬编码色值（颜色只从语义令牌来）", () => {
    for (const [name, source] of [["ChatDock", chatDock], ["SubmitCluster", submitCluster], ["ImpactConfirmDialog", impact]] as const) {
      expect(source, name + " 出现硬编码色值").not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    }
  });
});

describe("工具栏密度与窄窗口重排", () => {
  it("提交区在窄窗口下逐档收紧，并且明确让错误区整行可读", () => {
    expect(submitCluster).toContain("@media (max-width: 900px)");
    expect(submitCluster).toContain("@media (max-width: 620px)");
    // 窄档里范围收成一行、按钮内边距收一档（实测 480 常态工具栏从 108px 压到 ~104px）
    const narrow = submitCluster.slice(submitCluster.indexOf("@media (max-width: 620px)"));
    expect(narrow).toContain("-webkit-line-clamp: 1");
    expect(narrow).toMatch(/\.submit \{[^}]*padding:/);
  });

  it("工具栏在窄窗口下压行高与内边距，但不缩按钮字号", () => {
    const toolbar = read("components/interactive/BoardToolbar.vue");
    const narrow = toolbar.slice(toolbar.indexOf("@media (max-width: 620px)"));
    expect(narrow).toContain("line-height: 1.45");
    // 主要操作不缩成元信息字号：窄档里不许把 .tb-btn 的 font-size 调小
    expect(narrow).not.toMatch(/\.tb-btn[^}]*font-size/);
  });
});

describe("入口可达", () => {
  it("保存前的影响预判失败 / 确认过期有一次可达的重试入口（重新预判并保存）", () => {
    expect(submitCluster).toContain('data-im="submit-recheck"');
    expect(submitCluster).toContain("store.saveNow()");
    // 没有错误时不许凭空占一行（humanizeFailure(null) 会返回「没有拿到失败原因」）
    expect(submitCluster).toContain("const raw = store.impactCheckError;");
    expect(submitCluster).toMatch(/return raw \? humanizeFailure/);
  });

  it("关闭态卡片也能看到并处理本机 / 草稿异常（不必先点「编辑」）", () => {
    expect(boardCard).toContain("draftLocalRemovalErrorFor");
    expect(boardCard).toContain("cardDraftNeedsAttention");
    // 关闭态分支里必须真的渲染它（不只是有个 computed）
    expect(boardCard).toMatch(/关闭态也能看到、能处理的状态[\s\S]{0,200}CardDraftHint v-if="cardDraftNeedsAttention"/);
  });

  it("影响确认框的辅助说明与主决定分层，且不显示内部决定项 id", () => {
    expect(impact).toContain('data-im="impact-note"');
    expect(impact).toContain("lead-sub");
    expect(impact).not.toContain("{{ item.id }}");
  });

  it("顶部保存失败只给一句人话（原文放悬停），不把接口路径铺在状态行上", () => {
    expect(view).toContain("humanizeFailure(store.saveError");
    expect(view).toContain("saveDetail");
  });
});
