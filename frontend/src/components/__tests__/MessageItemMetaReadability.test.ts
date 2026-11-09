/**
 * 对话侧信息层级（用户要求 §五.1 / §五.2 / §五.4）：
 *
 * 1. 普通对话每轮回答不再重复当前话题名（顶部话题栏保留），但 MessageItem 被
 *    其他场景复用时仍能按需显示归属 —— showTopic 能力保留，只由需要的场景传；
 * 2. 元信息（时间等）不再用 10.5px + 低透明度，静态即可读，hover 只做轻微增强；
 * 3. 知识提交失败时卡片默认可见的结论是可读中文（原始错误仍在展开详情里）。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/MessageItemMetaReadability.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import MessageItem from "../MessageItem.vue";
import type { StreamMessage } from "../../stores/session";

function makeMessage(over: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  return {
    id: "m1",
    content: "hi",
    contentType: "text",
    createdAt: "2026-11-09T09:42:11+08:00",
    ...over,
  };
}

function mountItem(message: StreamMessage, props: Record<string, unknown> = {}) {
  const pinia: Pinia = createPinia();
  setActivePinia(pinia);
  return {
    pinia,
    w: mount(MessageItem, {
      props: { message, ...props },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    }),
  };
}

const SRC = resolve(process.cwd(), "src");

/** 递归收集前端源码文件（守卫「同一能力只在某处传参」这类容易回退的事）。 */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) sourceFiles(full, out);
    else if (/\.vue$/.test(name)) out.push(full);
  }
  return out;
}

/**
 * 取源码里**独立一段** `.selector { … }` 规则。
 * 用行首匹配，避免命中宽度链列表里的 `.meta` 成员（那里只有 min-width）。
 */
function cssBlock(source: string, selector: string): string {
  const marker = "\n" + selector + " {";
  // 取最后一段：宽度链共享规则里也会出现同名选择器（那里只有 min-width）。
  const found = source.lastIndexOf(marker);
  if (found < 0) return "";
  const start = found + 1;
  const end = source.indexOf("\n}", start);
  return end < 0 ? "" : source.slice(start, end + 2);
}

beforeEach(() => {
  localStorage.clear();
});

describe("§五.1 / §五.2：话题名不再由每轮回答重复显示，但能力保留", () => {
  it("showTopic=false（普通对话默认）：不渲染话题名", () => {
    const { w } = mountItem(makeMessage({ role: "assistant", content: "回答正文", topicName: "天气" }));
    expect(w.find(".tname").exists()).toBe(false);
    w.unmount();
  });

  it("showTopic=true（需要归属的场景）：仍然渲染话题名，且用衬线声部", () => {
    const { w } = mountItem(
      makeMessage({ role: "assistant", content: "回答正文", topicName: "天气" }),
      { showTopic: true },
    );
    const tname = w.find(".tname");
    expect(tname.exists()).toBe(true);
    expect(tname.text()).toContain("天气");
    expect(tname.classes()).toContain("serif");
    w.unmount();
  });

  it("归属能力只在 MessageItem 里定义；普通对话过程区不再传 show-topic", () => {
    const files = sourceFiles(SRC).map((file) => ({
      path: relative(SRC, file).split("\\").join("/"),
      text: readFileSync(file, "utf8"),
    }));
    const declaring = files
      .filter((f) => /showTopic\?:\s*boolean/.test(f.text))
      .map((f) => f.path);
    expect(declaring, "话题标注能力必须保留在 MessageItem（被复用时按需显示）").toEqual([
      "components/MessageItem.vue",
    ]);
    const passing = files
      .filter((f) => /:show-topic=/.test(f.text))
      .map((f) => f.path);
    // MessageStream 负责「普通对话是否显示」（Lead 会去掉传参）；过程区/其他复用不得传。
    expect(passing).not.toContain("components/TurnProcess.vue");
    expect(passing.every((p) => p === "components/MessageStream.vue")).toBe(true);
  });
});

describe("§五.4：元信息静态可读（统一令牌，不靠 hover 才看清）", () => {
  it("元信息字号使用统一令牌、颜色用可读的 --text-muted，且不依赖 opacity<1", () => {
    const source = readFileSync(resolve(SRC, "components/MessageItem.vue"), "utf8");
    const meta = cssBlock(source, ".meta");
    expect(meta, ".meta 规则缺失").not.toBe("");
    expect(meta, "元信息字号必须走统一令牌（--fs-meta，暂回退 --fs-tech）").toMatch(
      /font-size:\s*var\(--fs-meta,\s*var\(--fs-tech\)\)/,
    );
    expect(meta, "不得再写死 10.5px").not.toMatch(/10\.5px/);
    expect(meta, "元信息颜色必须是可读令牌").toMatch(/color:\s*var\(--text-muted\)/);
    // 透明度 <1 会把 5.4:1 的令牌压到不可读；静态必须是完全不透明。
    expect(meta, "静态可读：默认不得依赖 opacity<1").not.toMatch(/opacity:\s*(?:0?\.\d+|0)\b/);
    expect(meta, "元信息静态不透明").toMatch(/opacity:\s*1/);
    // hover 只做轻微增强（颜色提升），不再把显形建立在悬停上。
    const hoverStart = source.indexOf(".message:hover .meta");
    const hover = hoverStart < 0 ? "" : source.slice(hoverStart, source.indexOf("}", hoverStart) + 1);
    expect(hover, "hover 规则存在").not.toBe("");
    expect(hover, "hover 不得再靠透明度显形").not.toMatch(/opacity:\s*(?:0?\.\d+|0)\b/);
  });

  it("渲染出的时间等元信息仍然存在且带 mono 声部", () => {
    const { w } = mountItem(makeMessage({ role: "assistant", content: "回答" }));
    const meta = w.find(".meta");
    expect(meta.exists()).toBe(true);
    expect(meta.classes()).toContain("mono");
    expect(meta.text()).toContain("09:42:11");
    w.unmount();
  });
});

describe("§四.6：知识提交失败的默认可读结论 + 原始错误可展开", () => {
  const RAW =
    "evidence_ids 必须来自本轮实际证据目录，未找到：msg_ghost";

  function knowledgeTool(partial: Partial<StreamMessage> = {}): StreamMessage {
    return makeMessage({
      role: "tool",
      callId: "c_k",
      toolName: "propose_knowledge",
      toolStatus: "failed",
      toolOk: false,
      toolCode: "evidence_not_found",
      toolCategoryLabel: "未知错误",
      toolError: RAW,
      presentation: { title: "提交知识", tool: "propose_knowledge" },
      ...partial,
    });
  }

  it("卡片默认行是可读结论；技术原文只在展开详情里", () => {
    const { w } = mountItem(knowledgeTool());
    const card = w.find(".tool-card");
    expect(card.exists()).toBe(true);
    expect(card.attributes("data-state")).toBe("failed");
    const line = card.find(".tool-fail-line").text();
    expect(line).toContain("知识提交未成功，引用依据未通过核对");
    expect(line).not.toContain("evidence_ids");
    // 原始错误仍在（展开详情），事实没有丢
    const detail = card.find(".tool-detail");
    expect(detail.exists()).toBe(true);
    expect(detail.find(".tool-error").text()).toContain(RAW);
    w.unmount();
  });

  it("有稳定 code 时优先用 code 生成结论，不用类别标签硬套", () => {
    const { w } = mountItem(knowledgeTool({ toolCategoryLabel: "超时" }));
    expect(w.find(".tool-fail-line").text()).toContain("引用依据未通过核对");
    expect(w.find(".tool-fail-line").text()).not.toContain("超时");
    w.unmount();
  });

  it("等待审批（成功但尚未生效）：卡片徽标说「等待审批」，不画成功勾", () => {
    const awaiting = makeMessage({
      role: "tool",
      callId: "c_a",
      toolName: "propose_knowledge",
      toolStatus: "success",
      toolCode: "awaiting_user_approval",
      toolResultStatus: "awaiting_approval",
      content: "已提交审批：「用户偏好清淡饮食」",
      presentation: { title: "提交知识", tool: "propose_knowledge", status: "等待审批" },
    });
    const { w } = mountItem(awaiting);
    const card = w.find(".tool-card");
    expect(card.attributes("data-state")).toBe("awaiting");
    expect(card.find(".tool-status").text()).toContain("等待审批");
    expect(card.find(".tool-mark").text()).not.toBe("✕");
    w.unmount();
  });
});
