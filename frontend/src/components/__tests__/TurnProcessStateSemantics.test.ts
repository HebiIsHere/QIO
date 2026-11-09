/**
 * 过程区的「状态语义」回归（用户要求 §四）：
 *
 * 旧实现把整轮 TURN_END 的 completed 直接写成「已完成」，并把第一项 toolError 的
 * 前 40 字拼成「N 项失败：…」。这会把三件不同的事混成一件：
 *   ① 本轮回复流程结束了；② 某一次调用失败了；③ 用户目标完成了 / 知识已提交审批。
 *
 * 新契约（本文件钉住）：
 *   1. completed 只说「本轮已结束」——回复结束不等于目标完成，更不等于知识已保存；
 *   2. 有等待审批的结构化事实时才说「等待审批」，且不宣称已完成；
 *   3. 任意一次工具失败不把整轮变成失败；默认可见区只给可读的结构化结论，
 *      原始错误留在逐项工具卡的展开详情里；
 *   4. 最后阶段的长名称不再挤占第一层状态行（阶段数/调用数是次级信息）；
 *   5. 窄宽度靠换行而不是裁切：状态行与详情入口都在（jsdom 没有布局，
 *      这里用源码守卫钉住 CSS 换行规则，渲染断言只管内容层级）。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/TurnProcessStateSemantics.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import TurnProcess from "../TurnProcess.vue";
import {
  useSessionStore,
  type StreamMessage,
  type TurnFacts,
  type TurnStage,
} from "../../stores/session";
import { resetProcessState } from "../../stores/turnProcess";

const { getTrace, respondApproval } = vi.hoisted(() => ({
  getTrace: vi.fn(async () => ({ turn_id: "turn_1", duration_ms: 0, phases: {} })),
  respondApproval: vi.fn(async () => ({ ok: true })),
}));

vi.mock("../../services/api", () => ({
  api: { getTrace, respondApproval, getUISettings: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
  return {
    content: "",
    contentType: "text",
    createdAt: "2026-11-09T10:00:00+00:00",
    ...partial,
  } as StreamMessage;
}

function stage(partial: Partial<TurnStage> & { stageId: string }): TurnStage {
  return { index: 1, name: "", status: "done", notes: [], callIds: [], ...partial };
}

function facts(partial: Partial<TurnFacts> = {}): TurnFacts {
  return {
    turnId: "turn_1",
    status: "completed",
    durationMs: 2600,
    queueMs: 0,
    startedAt: null,
    endedAt: null,
    reason: null,
    reasonCode: null,
    stoppedBy: null,
    actions: [],
    errorText: null,
    ...partial,
  };
}

function mountProcess(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(TurnProcess, {
    props: { turnId: "turn_1", items: [], stages: [], facts: null, running: false, ...props },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
  return { w, session, pinia };
}

async function openHistory(w: ReturnType<typeof mountProcess>["w"]) {
  const toggle = w.find("[data-test='turn-process-toggle']");
  if (toggle.exists() && toggle.attributes("aria-expanded") !== "true") {
    await toggle.trigger("click");
    await nextTick();
  }
}

const KNOWLEDGE_FAILED = msg({
  id: "t_k",
  role: "tool",
  callId: "c_k",
  stageId: "st_1",
  toolName: "propose_knowledge",
  toolStatus: "failed",
  toolOk: false,
  toolCode: "evidence_not_found",
  toolError:
    "evidence_ids 必须来自本轮实际证据目录，未找到：msg_ghost（完整技术原文只在展开详情里）",
  presentation: { title: "提交知识", tool: "propose_knowledge" },
});

const STAGE_1 = stage({
  stageId: "st_1",
  index: 1,
  name: "整理本轮依据并提交知识",
  status: "done",
  notes: [{ narrativeId: "n1", text: "先整理依据", kind: "progress", at: "" }],
  callIds: ["c_k"],
});

beforeEach(() => {
  resetProcessState();
  respondApproval.mockClear();
});

describe("§四.1/2/3：区分「本轮结束」「调用失败」「等待审批」", () => {
  it("completed 只说「本轮已结束」：不宣称目标完成，也不说知识已保存", async () => {
    const { w } = mountProcess({
      items: [KNOWLEDGE_FAILED],
      stages: [STAGE_1],
      facts: facts({ status: "completed" }),
    });
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("本轮已结束");
    expect(status).not.toContain("已完成");
    expect(status).not.toContain("目标完成");
    expect(status).not.toContain("已保存");
    // 截图里那类「知识已提交审批」不是从状态词里来的：这里只允许在等待审批事实下出现
    expect(status).not.toContain("已提交审批");
    w.unmount();
  });

  it("有等待审批的结构化事实：说「等待审批」，不宣称已完成", async () => {
    const awaiting = msg({
      id: "t_a",
      role: "tool",
      callId: "c_a",
      stageId: "st_1",
      toolName: "propose_knowledge",
      toolStatus: "success",
      toolCode: "awaiting_user_approval",
      toolResultStatus: "awaiting_approval",
      content: "已提交审批：「…」",
      presentation: { title: "提交知识", tool: "propose_knowledge", status: "等待审批" },
    });
    const { w } = mountProcess({ items: [awaiting], stages: [STAGE_1], facts: facts() });
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("等待审批");
    expect(status).not.toContain("已完成");
    expect(status).not.toContain("已保存");
    w.unmount();
  });

  it("同一次知识提交先失败后重试成功：按最后一次成功证据呈现，不念旧失败", async () => {
    const retried = msg({
      id: "t_k2",
      role: "tool",
      callId: "c_k2",
      stageId: "st_1",
      toolName: "propose_knowledge",
      toolStatus: "success",
      toolCode: "awaiting_user_approval",
      toolResultStatus: "awaiting_approval",
      content: "已提交审批：「…」",
      presentation: { title: "提交知识", tool: "propose_knowledge" },
    });
    const { w } = mountProcess({
      items: [KNOWLEDGE_FAILED, retried],
      stages: [stage({ stageId: "st_1", callIds: ["c_k", "c_k2"] })],
      facts: facts(),
    });
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    // 用户要求 §四.4：有后续成功证据时按证据呈现 —— 最终事实是「等待审批」。
    expect(status).toContain("等待审批");
    expect(status).not.toContain("知识提交未成功");
    expect(status).not.toContain("引用依据未通过核对");
    // 旧失败仍留在展开后的逐项工具卡里（事实可追查，不在第一层反复念）
    expect(w.findAll(".tool-card")).toHaveLength(0);
    await openHistory(w);
    const cards = w.findAll(".tool-card");
    expect(cards).toHaveLength(2);
    expect(cards[0]?.find(".tool-fail-line").text()).toContain("知识提交未成功");
    w.unmount();
  });

  it("工具失败不改整轮状态：data-state 仍是 ready，可读摘要代替原始错误", async () => {
    const { w } = mountProcess({ items: [KNOWLEDGE_FAILED], stages: [STAGE_1], facts: facts() });
    await nextTick();
    const region = w.find("[data-test='turn-process']");
    expect(region.attributes("data-state")).toBe("ready");
    expect(region.attributes("data-state")).not.toBe("failed");
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("知识提交未成功，引用依据未通过核对");
    // 技术原文（evidence_ids / 未找到…）不进第一层状态行
    expect(status).not.toContain("evidence_ids");
    expect(status).not.toContain("未找到");
    // 工具失败事实不能从过程区消失：展开后仍然看得到这次调用与原始错误
    expect(w.findAll(".tool-card")).toHaveLength(0);
    await openHistory(w);
    const card = w.find(".tool-card");
    expect(card.exists()).toBe(true);
    expect(card.find(".tool-fail-line").text()).toContain("知识提交未成功，引用依据未通过核对");
    expect(card.find(".tool-fail-line").text()).not.toContain("evidence_ids");
    // 原始错误在折叠详情里（可展开查看），不是默认可见的那一行
    const detail = card.find(".tool-detail");
    expect(detail.exists()).toBe(true);
    expect(detail.find(".tool-error").text()).toContain("evidence_ids");
    w.unmount();
  });

  it("通用工具失败：用后端给的类别标签兜底，不用中文错误文本猜原因", async () => {
    const failed = msg({
      id: "t_w",
      role: "tool",
      callId: "c_w",
      stageId: "st_1",
      toolName: "web_search",
      toolStatus: "failed",
      toolOk: false,
      toolCategory: "timeout",
      toolCategoryLabel: "超时",
      toolError: "connection timed out after 30s",
      presentation: { title: "网络搜索", tool: "web_search" },
    });
    const { w } = mountProcess({
      items: [failed],
      stages: [stage({ stageId: "st_1", callIds: ["c_w"] })],
      facts: facts(),
    });
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("网络搜索执行失败（超时）");
    expect(status).not.toContain("timed out");
    w.unmount();
  });

  it("没有结构化事实的失败：只说「执行失败」，不编造原因", async () => {
    const failed = msg({
      id: "t_g",
      role: "tool",
      callId: "c_g",
      stageId: "st_1",
      toolName: "grep",
      toolStatus: "failed",
      toolOk: false,
      toolError: "grep 退出码 2：路径不存在",
      presentation: { title: "搜索代码", tool: "grep" },
    });
    const { w } = mountProcess({
      items: [failed],
      stages: [stage({ stageId: "st_1", callIds: ["c_g"] })],
      facts: facts(),
    });
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("搜索代码执行失败");
    expect(status).not.toContain("退出码");
    // 原始工具名也不进第一层状态行（内部标识只在展开后的工具卡里）
    expect(status).not.toContain("grep");
    w.unmount();
  });
});

describe("§四.8：第一层状态与次级信息分层", () => {
  it("最后阶段的长名称不再挤占状态行；阶段数/调用数留在次级摘要", async () => {
    const longName = "核对全部证据、整理引用编号并把这条知识提交给你审批的收尾阶段";
    const { w } = mountProcess({
      items: [
        msg({ id: "t1", role: "tool", callId: "c1", stageId: "st_l", toolName: "fs_read", toolStatus: "success" }),
      ],
      stages: [stage({ stageId: "st_l", index: 1, name: longName, callIds: ["c1"] })],
      facts: facts(),
    });
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).not.toContain(longName);
    expect(status).toContain("本轮已结束");
    // 次级信息入口仍在：阶段数 + 耗时，且都能展开/查看
    expect(status).toContain("1 个阶段");
    expect(w.find("[data-test='turn-process-duration']").text()).toContain("耗时");
    expect(w.find("[data-test='turn-process-toggle']").exists()).toBe(true);
    w.unmount();
  });

  it("窄宽度靠换行保内容：状态行与详情入口都允许折行（源码守卫）", () => {
    const source = readFileSync(resolve(process.cwd(), "src/components/TurnProcess.vue"), "utf8");
    /** 取第一段 `.selector { … }` 规则（源码守卫不需要完整 CSS 解析）。 */
    function cssBlock(selector: string): string {
      const start = source.indexOf(selector + " {");
      if (start < 0) return "";
      const end = source.indexOf("\n}", start);
      return end < 0 ? "" : source.slice(start, end + 2);
    }
    expect(cssBlock(".tp-status"), ".tp-status 必须允许换行（窄窗口不裁切）").toMatch(
      /flex-wrap:\s*wrap/,
    );
    expect(cssBlock(".tp-toggle"), "状态按钮本身也必须可折行").toMatch(/flex-wrap:\s*wrap/);
    expect(
      cssBlock(".tp-detail"),
      "长状态说明必须能任意位置折行（不能把状态与展开入口挤出可见区）",
    ).toMatch(/overflow-wrap:\s*anywhere/);
  });
});
