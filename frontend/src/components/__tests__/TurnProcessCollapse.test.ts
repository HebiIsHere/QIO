/**
 * 问题 4（默认折叠 / 一行工具摘要 / 状态分开管理）+ 问题 7（失败原因与可用操作）
 * 的 B 回归用例。
 *
 * 修复前红：
 *   * watch(running) 自动展开抽屉 → 旧阶段、旧说明、逐项工具卡全都默认可见；
 *   * 当前阶段与历史的展开状态共用同一个开关；
 *   * TurnFacts 没有 reason/actions → 失败/停止只有一个状态词。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/TurnProcessCollapse.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import TurnProcess from "../TurnProcess.vue";
import { useApprovalsStore } from "../../stores/approvals";
import { useSessionStore, type StreamMessage, type TurnFacts, type TurnStage } from "../../stores/session";
import { processKey, resetProcessState, setProcessExpanded } from "../../stores/turnProcess";

const { respondApproval, getTrace, sendTurn, resendInterruptedTurn } = vi.hoisted(() => ({
  respondApproval: vi.fn(async () => ({ ok: true })),
  getTrace: vi.fn(async () => ({ turn_id: "turn_1", duration_ms: 0, phases: {} })),
  sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_new", status: "accepted", topic_id: null })),
  resendInterruptedTurn: vi.fn(async () => ({
    ok: true,
    recovered_turn_id: "turn_1",
    turn_id: "turn_new",
    status: "queued",
  })),
}));

vi.mock("../../services/api", () => ({
  api: { respondApproval, getTrace, sendTurn, resendInterruptedTurn, getUISettings: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
  return { content: "", contentType: "text", createdAt: "2026-10-06T08:00:00+00:00", ...partial } as StreamMessage;
}

function stage(partial: Partial<TurnStage> & { stageId: string }): TurnStage {
  return { index: 1, name: "", status: "running", notes: [], callIds: [], ...partial };
}

function facts(partial: Partial<TurnFacts> = {}): TurnFacts {
  return {
    turnId: "turn_1",
    status: "completed",
    durationMs: null,
    queueMs: null,
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

const STAGE_1 = stage({
  stageId: "st_1",
  index: 1,
  name: "读取仓库结构",
  status: "done",
  notes: [{ narrativeId: "n1", text: "先看目录结构", kind: "progress", at: "2026-10-06T08:00:00+00:00" }],
  callIds: ["c1"],
});
const STAGE_2 = stage({
  stageId: "st_2",
  index: 2,
  name: "核对实现",
  status: "running",
  notes: [
    { narrativeId: "n2", text: "先确认接口", kind: "progress", at: "2026-10-06T08:00:01+00:00" },
    { narrativeId: "n3", text: "正在核对实现", kind: "progress", at: "2026-10-06T08:00:02+00:00" },
  ],
  callIds: ["c2", "c3"],
});
const TOOL_1 = msg({
  id: "t1",
  role: "tool",
  callId: "c1",
  stageId: "st_1",
  toolName: "fs_list",
  toolStatus: "success",
  presentation: { title: "列目录" },
});
const TOOL_2 = msg({
  id: "t2",
  role: "tool",
  callId: "c2",
  stageId: "st_2",
  toolName: "fs_read",
  toolStatus: "running",
  toolRunning: true,
  presentation: { title: "读取文件" },
});
const TOOL_3 = msg({
  id: "t3",
  role: "tool",
  callId: "c3",
  stageId: "st_2",
  toolName: "grep",
  toolStatus: "running",
  toolRunning: true,
  presentation: { title: "搜索代码" },
});

/** 可见文本里出现几次 needle（不用 html：模板注释不该参与计数） */
function count(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

/** 折叠行里的「调用 / 工具数量」token（问题 4：同一行只允许一个） */
function callCountTokens(text: string): string[] {
  return text.match(/\d+\s*次调用|\d+\s*项工具运行中/g) ?? [];
}

/** 全部结束的工具（截图场景：一轮跑完，没有运行中的调用） */
const DONE_TOOLS: StreamMessage[] = [
  msg({ id: "d1", role: "tool", callId: "c1", stageId: "st_1", toolName: "fs_list", toolStatus: "success", toolOk: true, presentation: { title: "列目录" } }),
  msg({ id: "d2", role: "tool", callId: "c2", stageId: "st_2", toolName: "fs_read", toolStatus: "success", toolOk: true, presentation: { title: "读取文件" } }),
  msg({ id: "d3", role: "tool", callId: "c3", stageId: "st_2", toolName: "grep", toolStatus: "success", toolOk: true, presentation: { title: "搜索代码" } }),
];

function mountProcess(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(TurnProcess, {
    props: { turnId: "turn_1", items: [], stages: [], facts: null, running: false, ...props },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
  return { w, session };
}

beforeEach(() => {
  resetProcessState();
  respondApproval.mockClear();
  getTrace.mockClear();
  sendTurn.mockClear();
  resendInterruptedTurn.mockClear();
});

describe("问题 4：运行中默认可见区 = 状态行 + 当前阶段名 + 最新说明 + 一行工具摘要", () => {
  it("默认不展开历史：旧阶段说明与逐项工具卡都看不见，没展开过的工具卡也不在默认可见区", async () => {
    const { w, session } = mountProcess({
      items: [TOOL_1, TOOL_2, TOOL_3],
      stages: [STAGE_1, STAGE_2],
      running: true,
    });
    session.turnPhase = "generating";
    await nextTick();

    const region = w.find("[data-test='turn-process']");
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("运行中");
    expect(status, "一行工具摘要").toContain("读取文件 · 2 项工具运行中");
    expect(w.find(".tp-cur-name").text()).toContain("核对实现");
    expect(w.find(".tp-cur-text").text()).toBe("正在核对实现");

    // 历史收起（DOM 里不存在）；旧阶段说明、逐项工具卡都不在默认可见区
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(false);
    expect(region.text()).not.toContain("先看目录结构");
    expect(region.text()).not.toContain("先确认接口");
    expect(w.findAll(".tool-card")).toHaveLength(0);
    expect(w.find("[data-test='turn-process-stage-tools']").exists()).toBe(false);
    w.unmount();
  });

  it("主开关只开历史，当前阶段明细是独立开关：两个状态分开管理", async () => {
    const { w, session } = mountProcess({
      items: [TOOL_1, TOOL_2, TOOL_3],
      stages: [STAGE_1, STAGE_2],
      running: true,
    });
    session.turnPhase = "generating";
    await nextTick();

    const toggle = w.find("[data-test='turn-process-toggle']");
    expect(toggle.attributes("aria-expanded")).toBe("false");
    await toggle.trigger("click");
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);
    // 历史里能回看旧阶段说明与旧阶段的调用卡
    const history = w.find("[data-test='turn-process-history']").text();
    expect(history).toContain("先看目录结构");
    expect(history).toContain("先确认接口");
    expect(w.findAll(".tool-card")).toHaveLength(1); // 只有旧阶段的调用卡
    // 当前阶段的调用卡仍收起（独立状态）
    expect(w.find("[data-test='turn-process-stage-tools']").exists()).toBe(false);

    const stageToggle = w.find("[data-test='turn-process-stage-toggle']");
    expect(stageToggle.exists()).toBe(true);
    expect(stageToggle.attributes("aria-expanded")).toBe("false");
    await stageToggle.trigger("click");
    const stageTools = w.find("[data-test='turn-process-stage-tools']");
    expect(stageTools.exists()).toBe(true);
    expect(stageTools.findAll(".tool-card")).toHaveLength(2);
    // 两个状态互相独立：历史仍然开着
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);

    // 收起历史不影响当前阶段明细
    await toggle.trigger("click");
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-stage-tools']").exists()).toBe(true);
    w.unmount();
  });

  /**
   * D 的真机截图缺陷：折叠态一行里「2 次调用」出现两次
   * （状态行「已完成 2 次调用」+ 抽屉摘要「2 个阶段 · 2 次调用」）。
   * 断言用**可见文本**计数，不看 html。
   */
  it("折叠态：调用数量只出现一次（截图场景：已完成 N 次调用 + N 个阶段）", async () => {
    const done1 = stage({ ...STAGE_1, status: "done" });
    const done2 = stage({ ...STAGE_2, status: "done", name: "核对实现" });
    const { w, session } = mountProcess({
      items: DONE_TOOLS,
      stages: [done1, done2],
      running: true,
    });
    session.turnPhase = "generating";
    await nextTick();

    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("已完成 3 次调用"); // 状态行给真实数量（一行摘要）
    expect(callCountTokens(status), "折叠态里调用/工具数量只能出现一次").toHaveLength(1);
    expect(count(status, "3 次调用")).toBe(1);
    expect(status).toContain("2 个阶段"); // 抽屉规模仍可见，但不重复数量
    w.unmount();
  });

  it("折叠态：正在跑工具时数量也只出现一次（截图场景②）", async () => {
    const { w, session } = mountProcess({
      items: [TOOL_1, TOOL_2, TOOL_3],
      stages: [STAGE_1, STAGE_2],
      running: true,
    });
    session.turnPhase = "generating";
    await nextTick();

    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("读取文件 · 2 项工具运行中");
    expect(callCountTokens(status)).toHaveLength(1);
    expect(status).toContain("2 个阶段");
    expect(status, "总数不在折叠行里重复").not.toContain("3 次调用");
    w.unmount();
  });

  it("折叠态：完成后数量只出现一次，耗时仍在（截图场景③）", async () => {
    const done1 = stage({ ...STAGE_1, status: "done" });
    const done2 = stage({ ...STAGE_2, status: "done" });
    const { w } = mountProcess({
      items: DONE_TOOLS,
      stages: [done1, done2],
      running: false,
      facts: facts({ status: "completed", durationMs: 2600 }),
    });
    await nextTick();

    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("已完成");
    expect(status).toContain("耗时");
    expect(status).toContain("2.6 秒");
    expect(callCountTokens(status)).toHaveLength(1);
    // 完成态状态行没有数量（只有阶段名），抽屉摘要补「3 次调用」这一次
    expect(status).toContain("3 次调用");
    expect(count(status, "3 次调用")).toBe(1);
    w.unmount();
  });

  it("未归属的中间话（显式 stage_id=null）作为「生成中说明」可见，不重复出现", async () => {
    const interim = msg({
      id: "a1",
      role: "assistant",
      content: "正在生成这一段说明",
      interim: true,
      streaming: true,
    });
    const { w, session } = mountProcess({ items: [interim], stages: [STAGE_2], running: true });
    session.turnPhase = "generating";
    await nextTick();

    expect(w.find(".tp-cur-text").text()).toBe("正在生成这一段说明");
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(false);
    expect(w.html().split("正在生成这一段说明").length - 1).toBe(1);
    w.unmount();
  });

  it("完成 / 失败 / 停止自动收起（没有用户手动展开时）", async () => {
    const { w, session } = mountProcess({
      items: [TOOL_1, TOOL_2, TOOL_3],
      stages: [STAGE_1, STAGE_2],
      running: true,
    });
    session.turnPhase = "generating";
    // 模拟「自动打开」的历史（不是用户手动的）：结束后必须自动收起
    setProcessExpanded(processKey(session.currentTopicId, "turn_1"), true);
    await nextTick();
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);

    await w.setProps({ running: false, facts: facts({ status: "failed", durationMs: 1200 }) });
    await nextTick();
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-stage-tools']").exists()).toBe(false);
    w.unmount();
  });

  it("用户手动展开过历史 / 正在上翻阅读：普通状态更新不强制收起", async () => {
    const { w, session } = mountProcess({
      items: [TOOL_1, TOOL_2, TOOL_3],
      stages: [STAGE_1, STAGE_2],
      running: true,
    });
    session.turnPhase = "generating";
    await nextTick();
    await w.find("[data-test='turn-process-toggle']").trigger("click"); // 用户手动展开
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);

    session.streamFollowing = false;
    await w.setProps({ running: false, facts: facts({ status: "completed", durationMs: 1000 }) });
    await nextTick();
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);
    w.unmount();
  });
});

describe("问题 4：状态行的失败摘要是系统事实，不是模型文案", () => {
  it("TOOL_END 的真实 error → 状态行带一句话原因；完整详情仍然折叠", async () => {
    const failed = msg({
      id: "t9",
      role: "tool",
      callId: "c9",
      stageId: "st_2",
      toolName: "grep",
      toolStatus: "failed",
      toolOk: false,
      toolError: "grep 退出码 2：路径不存在",
      presentation: { title: "搜索代码" },
    });
    const { w, session } = mountProcess({ items: [failed], stages: [STAGE_2], running: true });
    session.turnPhase = "generating";
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("1 项失败");
    expect(status).toContain("grep 退出码 2：路径不存在");
    // 失败详情仍然在折叠区：逐项工具卡不在默认可见区
    expect(w.findAll(".tool-card")).toHaveLength(0);
    w.unmount();
  });

  it("没有 error 的失败不编造原因：只说「N 项失败」", async () => {
    const failed = msg({
      id: "t10",
      role: "tool",
      callId: "c10",
      stageId: "st_2",
      toolName: "grep",
      toolStatus: "failed",
      toolOk: false,
      presentation: { title: "搜索代码" },
    });
    const { w, session } = mountProcess({ items: [failed], stages: [STAGE_2], running: true });
    session.turnPhase = "generating";
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("1 项失败");
    expect(status).not.toContain("这次执行没有成功"); // 不替失败编一句原因
    expect(status).not.toContain("：");
    w.unmount();
  });
});

describe("问题 1：同一 approval_id 任一时刻只有一套有效按钮", () => {
  function mountWithApproval(autoOpen = false) {
    const mounted = mountProcess({ items: [], stages: [], running: true });
    mounted.session.activeTurnId = "turn_1";
    mounted.session.turnRunning = true;
    useApprovalsStore().enqueue(
      "ap_full",
      "tool_execution",
      { description: "删除临时目录", access: ["删除：/tmp/qio-scratch"], capabilities: ["副作用：destructive"] },
      { turnId: "turn_1", autoOpen },
    );
    return mounted;
  }

  it("「查看完整信息」→ 弹窗接管（内联不再出按钮）；「稍后处理」→ 内联重新接管", async () => {
    const { w } = mountWithApproval(false);
    const approvals = useApprovalsStore();
    await nextTick();

    const card = () => w.find("[data-test='turn-process-approval']");
    expect(card().find("[data-test='turn-process-approval-allow']").exists()).toBe(true);
    expect(approvals.inlineClaimed).toBe(true);

    await card().find("[data-test='turn-process-approval-full']").trigger("click");
    await nextTick();
    // 交给原弹窗：窗口可见、内联声明释放、内联不再显示批准/拒绝
    expect(approvals.visible).toBe(true);
    expect(approvals.inlineClaimed).toBe(false);
    expect(card().find("[data-test='turn-process-approval-allow']").exists()).toBe(false);
    expect(card().find("[data-test='turn-process-approval-in-modal']").exists()).toBe(true);

    approvals.defer(); // 用户「稍后处理」：窗口收起
    await nextTick();
    expect(card().find("[data-test='turn-process-approval-allow']").exists()).toBe(true);
    expect(approvals.inlineClaimed).toBe(true);
    w.unmount();
  });

  it("autoOpen 也不能把按钮从过程区拿走：内联卡接管并抑制自动弹窗（显式查看仍可打开）", async () => {
    const { w } = mountWithApproval(true);
    const approvals = useApprovalsStore();
    await nextTick();
    const card = w.find("[data-test='turn-process-approval']");
    expect(card.find("[data-test='approval-facts']").exists()).toBe(true);
    // 契约 §1.3 第 1 项：操作按钮留在同一个过程区域内
    expect(card.find("[data-test='turn-process-approval-allow']").exists()).toBe(true);
    expect(card.find("[data-test='turn-process-approval-reject']").exists()).toBe(true);
    expect(approvals.inlineClaimed).toBe(true);
    // 自动弹窗被抑制（审批没有消失：内联卡就是那个可见入口）
    expect(approvals.visible).toBe(false);
    expect(approvals.deferred).toBe(true);

    // 用户显式点「查看完整信息」→ 弹窗打开，按钮交给弹窗（仍然只有一套）
    await card.find("[data-test='turn-process-approval-full']").trigger("click");
    await nextTick();
    expect(approvals.visible).toBe(true);
    expect(card.find("[data-test='turn-process-approval-allow']").exists()).toBe(false);
    expect(card.find("[data-test='turn-process-approval-in-modal']").exists()).toBe(true);
    w.unmount();
  });
});

describe("问题 7：失败 / 停止显示简短原因与确实可用的操作，详情默认收起", () => {
  const FAILED = facts({
    status: "failed",
    durationMs: 1500,
    reasonCode: "provider_error",
    reason: "模型服务没有响应（连续 2 次）",
    stoppedBy: "system",
    actions: ["retry", "resend", "continue"],
    errorText: "upstream 502: bad gateway",
  });

  it("原因可见、可用操作可点、详情默认折叠；continue 不重复出按钮", async () => {
    const { w, session } = mountProcess({ facts: FAILED });
    session.pushUser("帮我核对实现");
    const mine = session.messages[session.messages.length - 1];
    if (mine) mine.turnId = "turn_1";
    await nextTick();

    expect(w.find("[data-test='turn-process-reason']").text()).toContain("模型服务没有响应");

    const retry = w.find("[data-test='turn-process-action-retry']");
    const resend = w.find("[data-test='turn-process-action-resend']");
    expect(retry.exists()).toBe(true);
    expect(resend.exists()).toBe(true);
    // continue 由既有的继续/停止操作条承担，过程区不再出第二套按钮
    expect(w.find("[data-test='turn-process-action-continue']").exists()).toBe(false);

    const detail = w.find("[data-test='turn-process-outcome-detail']");
    expect(detail.exists()).toBe(true);
    expect(detail.attributes("open")).toBeUndefined();
    expect(detail.text()).toContain("upstream 502");

    await retry.trigger("click");
    // 第三个参数无条件带上（空数组 = 显式「这条消息没有附件」，契约 §1.4）
    expect(sendTurn).toHaveBeenCalledWith("帮我核对实现", null, []);
    await flushPromises(); // 重试提交结束后按钮才重新可用
    await resend.trigger("click");
    expect(resendInterruptedTurn).toHaveBeenCalledWith("turn_1");
    w.unmount();
  });

  it("旧记录（没有 reason/actions）：不伪造原因，也不出现操作按钮", async () => {
    const { w } = mountProcess({ facts: facts({ status: "failed", durationMs: 500 }) });
    await nextTick();
    expect(w.find("[data-test='turn-process-reason']").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-actions']").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-status']").text()).toContain("已失败");
    w.unmount();
  });

  it("不可用的操作不显示按钮：找不到该轮用户消息时 retry 不出现", async () => {
    const { w } = mountProcess({ facts: FAILED });
    await nextTick();
    expect(w.find("[data-test='turn-process-action-retry']").exists()).toBe(false);
    // 但要说清为什么没有这个入口（不静默）
    expect(w.find("[data-test='turn-process-action-note']").text()).toContain("找不到");
    w.unmount();
  });

  it("只有后端列出的操作才出现：actions 为空时不显示重试/重发", async () => {
    const { w } = mountProcess({
      facts: facts({ status: "cancelled", reason: "你停止了这一轮", reasonCode: "user_stopped", stoppedBy: "user" }),
    });
    await nextTick();
    expect(w.find("[data-test='turn-process-reason']").text()).toContain("你停止了这一轮");
    expect(w.find("[data-test='turn-process-action-retry']").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-action-resend']").exists()).toBe(false);
    w.unmount();
  });
});
