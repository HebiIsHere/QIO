/**
 * 界面可读性 / 密度 / 入口的源码守卫（C 本轮）。
 *
 * 这些扫描测试守的是「很容易悄悄退回去」的东西：小字又被换成装饰色、窄窗口又整行藏掉操作说明、
 * 失败入口又只在编辑态可见。它们读源码文本，跑得快，不需要浏览器；
 * 真实像素对比度与工具栏高度由 scripts/closure-c-verify 的实机报告给出。
 *
 * 2026-10-26（合并后清理 · C2）：旧 ChatDock 与旧 ImpactConfirmDialog 已删除：
 * 前者整块能力由 v2/InteractiveConversation 承接（对应断言随组件删除），
 * 后者「辅助说明与主决定分层、不显示内部决定项 id」迁到 v2/ImpactConfirmLayer 上继续守。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const read = (relative: string) => readFileSync(resolve(process.cwd(), "src", relative), "utf8");
const submitCluster = read("components/interactive/SubmitCluster.vue");
const boardCard = read("components/interactive/BoardCard.vue");
const impactLayer = read("components/interactive/v2/ImpactConfirmLayer.vue");
const chromeTools = read("components/interactive/v2/InteractiveChromeTools.vue");
const saveStatus = read("components/interactive/v2/saveStatus.ts");

describe("亮色辅助文字的可读性（有操作意义的小字不能用装饰色）", () => {
  it("提交区详情里的草稿提示与失败原因都用可读色，不靠装饰色", () => {
    expect(submitCluster).toMatch(/\.line\.faint \{ color: var\(--text-muted\); \}/);
    expect(submitCluster).toMatch(/\.failure-reason \{ color: var\(--text-primary\); \}/);
    expect(submitCluster).toMatch(/\.failure-label \{ color: var\(--danger\); \}/);
  });

  it("提交区不出现硬编码色值（颜色只从语义令牌来）", () => {
    expect(submitCluster, "SubmitCluster 出现硬编码色值").not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });
});

describe("工具栏密度与窄窗口重排", () => {
  it("提交区在窄窗口下只压内边距：不用省略号/裁切隐藏唯一可读的正文", () => {
    expect(submitCluster).toContain("@media (max-width: 900px)");
    expect(submitCluster).toContain("@media (max-width: 620px)");
    const narrow = submitCluster.slice(submitCluster.indexOf("@media (max-width: 620px)"));
    // 窄档只收按钮内边距（主要操作仍然是原来的字号与完整文案）
    expect(narrow).toMatch(/\.submit \{[^}]*padding:/);
    expect(narrow).not.toMatch(/font-size/);
    /*
      2026-10-15 口径变化：上一版靠 -webkit-line-clamp 把范围收成一行来维持高度，
      本轮明确要求「普通说明自然换行，不使用省略号隐藏唯一可读取的正文」。
      真正的窄档重排改为按**功能栏实际可用宽度**分档（BoardToolbar 的 data-tb-band + 栅格），
      所以这里钉的是「不靠隐藏正文来维持高度」：整个提交区不许出现 line-clamp。
    */
    expect(submitCluster).not.toContain("-webkit-line-clamp");
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

  it("影响确认层的辅助说明与主决定分层，且不显示内部决定项 id", () => {
    expect(impactLayer).toContain('data-im="impact-confirm-note"');
    expect(impactLayer).toContain('class="ic-lead"');
    // 内部 id 只作为兜底的可读标题（title || intentId），不允许直接铺 {{ item.id }}
    expect(impactLayer).not.toContain("{{ item.id }}");
    expect(impactLayer).toMatch(/item\.title \|\| item\.intentId/);
  });

  it("顶部保存失败只给一句人话（原文放悬停），不把接口路径铺在状态行上", () => {
    // Phase 1 v2：保存状态已从 InteractiveView 顶部迁到全局栏的 InteractiveChromeTools；
    // 同一个口径断言在新归属文件上继续生效。
    expect(chromeTools).toContain("saveStatusView");
    // 失败原文改由 aria-label 携带（不再使用原生 title），状态行仍只显示一句人话
    expect(chromeTools).toContain("saveView.detail");
    expect(chromeTools).not.toContain(':title="saveView.detail');
    expect(saveStatus).toContain("humanizeFailure(input.error");
    expect(saveStatus).toContain("detail: full.detail");
  });
});
