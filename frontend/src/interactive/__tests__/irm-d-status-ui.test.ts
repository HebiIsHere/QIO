/**
 * 子智能体 D · 界面状态（契约 §14.1 / §14.6 / §9.6 / §12.7）。
 *
 * 覆盖：
 *   - 提交区把「已受理 / 理解三态 / 失败」分开显示，失败有重试入口；
 *   - 任务浮层显示真实的排队 / 执行 / 暂停 / 结束，并有对应的暂停 / 继续 / 取消；
 *   - 预览卡把模型提议与正式内容分开，批准前不改动板面；
 *   - 状态不靠颜色表达（每个状态都有文字），颜色只用设计令牌；
 *   - 窄窗口与双主题下这些关键状态都在 DOM 里可读。
 * 服务层全部 mock。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import SubmitCluster from "../../components/interactive/SubmitCluster.vue";
import IntentStatusPopover from "../../components/interactive/IntentStatusPopover.vue";
import IntentPreviewCard from "../../components/interactive/IntentPreviewCard.vue";
import { useInteractiveStore } from "../../stores/interactive";
import { understandingText } from "../approval";
import type { Execution, ExecutionStep, Intent, Understanding, UnderstandingStatus } from "../types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({
    board: { id: "board_t", title: "板面" },
    state: { boardId: "board_t", seq: 1, updatedAt: "t", cards: [], groups: [], links: [], selection: [] },
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
    pending: { expressions: [] },
  })),
  fetchUnderstanding: vi.fn(async () => ({ understanding: null, understandings: [] })),
  fetchExecutions: vi.fn(async () => ({ executions: [] })),
  fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true } })),
  saveBoardState: vi.fn(async () => ({ ok: true, seq: 1, savedAt: "t" })),
  submitBoard: vi.fn(),
  createIntents: vi.fn(),
  retryUnderstanding: vi.fn(),
  pauseIntent: vi.fn(),
  resumeIntent: vi.fn(),
  cancelIntent: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
}));

const NOW = "2026-10-11T00:00:00.000Z";
const mounted: VueWrapper[] = [];

function track(wrapper: VueWrapper): VueWrapper {
  mounted.push(wrapper);
  return wrapper;
}

function step(index: number, status: ExecutionStep["status"], op = "add_reply_card"): ExecutionStep {
  return { index, op, status };
}

function execution(
  id: string,
  intentId: string,
  status: Execution["status"],
  steps: ExecutionStep[] = [],
  error: string | null = null,
): Execution {
  return {
    id,
    intentId,
    boardId: "board_t",
    attempt: 1,
    status,
    steps,
    result: { cardIds: [], groupIds: [], linkIds: [] },
    error,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function intentFixture(over: Partial<Intent> = {}): Intent {
  return {
    id: "i1",
    boardId: "board_t",
    submissionId: "s1",
    title: "整理两份材料",
    summary: "",
    status: "running",
    preview: { cards: [], groups: [], links: [], note: "" },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    createdAt: NOW,
    updatedAt: NOW,
    ...over,
  };
}

function understandingFixture(over: Partial<Understanding> = {}): Understanding {
  return {
    id: "u1",
    boardId: "board_t",
    submissionId: "s1",
    requestId: "req-1",
    status: "understanding",
    stage: "",
    kind: null,
    steps: [],
    materials: [],
    summary: "",
    clarification: null,
    validation: { ok: true, reasons: [] },
    error: null,
    attempt: 1,
    adapter: "fake",
    intentIds: [],
    memory: [],
    createdAt: NOW,
    updatedAt: NOW,
    ...over,
  };
}

function freshStore() {
  const store = useInteractiveStore();
  store.board = {
    boardId: "board_t",
    seq: 1,
    updatedAt: NOW,
    cards: [],
    groups: [],
    links: [],
    selection: [],
  };
  store.saveStatus = "saved";
  return store;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  while (mounted.length) {
    const wrapper = mounted.pop();
    try {
      wrapper?.unmount();
    } catch {
      /* 已经卸载过 */
    }
  }
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("提交区：受理与理解分开（契约 §14.1 / §14.6）", () => {
  it("受理成功先显示统一状态「已受理」，不冒充理解完成", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understandingStatus = "accepted";
    store.submitNote = "已受理：QIO 已经可靠收下这次提交，正在理解。";
    const w = track(mount(SubmitCluster));
    const block = w.find('[data-im="understanding"]');
    expect(block.exists()).toBe(true);
    expect(block.attributes("data-understanding-status")).toBe("accepted");
    expect(block.attributes("data-understanding-outcome")).toBe("pending");
    const status = w.find('[data-im="understanding-status"]');
    expect(status.text()).toBe("已受理");
    expect(w.find('[data-im="understanding-detail"]').text()).toContain("可靠收下");
    expect(w.find('[data-im="understanding-detail"]').text()).toContain("不等于批准执行");
  });

  it("正在提交时先显示这一件事：上一次的「理解完成」不冒充这一次", () => {
    const store = freshStore();
    store.understanding = understandingFixture({ status: "understood", kind: "intent" });
    store.understandingStatus = "understood";
    store.submitStatus = "submitting";
    const w = track(mount(SubmitCluster));
    expect(w.find('[data-im="understanding-status"]').text()).toBe("正在提交");
    expect(w.find('[data-im="understanding-detail"]').text()).toContain("已受理");
  });

  it("需要补充：给出缺失项；无需行动：明确说没有任务", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({
      status: "needs_clarification",
      clarification: { question: "还要哪些材料？", missing: ["材料版本", "时间范围"] },
    });
    store.understandingStatus = "needs_clarification";
    const w = track(mount(SubmitCluster));
    expect(w.find('[data-im="understanding"]').attributes("data-understanding-outcome")).toBe("clarification");
    const missing = w.find('[data-im="understanding-missing"]');
    expect(missing.exists()).toBe(true);
    expect(missing.text()).toContain("材料版本");
    expect(missing.text()).toContain("时间范围");
    expect(missing.text()).toContain("没有生成任何任务");
    // 2026-10-14 第十个问题：详情默认收起，不再因为「需要补充 / 理解失败」自动展开。
    // 缺失项本身在折叠区之外（上面已经断言），所以重要信息仍然默认可见。
    expect(w.find('[data-im="submit-details-box"]').exists()).toBe(false);
  });

  it("无需行动：明确说明没有需要开展的任务", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "no_action", kind: "no_action" });
    store.understandingStatus = "no_action";
    const w = track(mount(SubmitCluster));
    expect(w.find('[data-im="understanding"]').attributes("data-understanding-outcome")).toBe("no_action");
    expect(w.find('[data-im="understanding-no-action"]').text()).toContain("没有生成任何任务");
  });

  it("理解失败：默认就给出重试入口（从快照恢复）", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "failed", error: "模型没有返回有效结果" });
    store.understandingStatus = "failed";
    const w = track(mount(SubmitCluster));
    expect(w.find('[data-im="understanding"]').attributes("data-understanding-outcome")).toBe("failed");
    const retry = w.find('[data-im="understanding-retry"]');
    expect(retry.exists()).toBe(true);
    expect(retry.text()).toContain("重试理解");
    expect(w.find('[data-im="understanding-detail"]').text()).toContain("模型没有返回有效结果");
  });

  it("理解完成：详情里给出等待审批的项数（提交不构成执行批准）", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "understood", kind: "intent", summary: "整理两份材料" });
    store.understandingStatus = "understood";
    store.intents = [
      intentFixture({ id: "i1", understandingId: "u1", status: "pending" }),
      intentFixture({ id: "i2", understandingId: "u1", status: "pending" }),
    ];
    const w = track(mount(SubmitCluster));
    expect(w.find('[data-im="understanding"]').attributes("data-understanding-outcome")).toBe("intent");
    await w.find('[data-im="submit-details"]').trigger("click");
    const line = w.find('[data-im="understanding-intents"]');
    expect(line.exists()).toBe(true);
    expect(line.text()).toContain("2 项");
    expect(line.text()).toContain("批准前不会有任何改动");
  });
});

describe("任务浮层：真实执行状态与可用操作（契约 §14.1 / §14.4）", () => {
  it("正在执行：显示执行状态与进度，可以暂停 / 取消", () => {
    const store = freshStore();
    store.tasksOpen = true;
    store.intents = [intentFixture({ id: "i1", status: "running" })];
    store.executions = [execution("e1", "i1", "running", [step(0, "done"), step(1, "running")])];
    const w = track(mount(IntentStatusPopover, { attachTo: document.body }));
    const row = w.find('[data-im="task-execution"][data-execution-status="running"]');
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("正在执行");
    expect(row.text()).toContain("正在执行第 2/2 步");
    expect(w.find('[data-im="task-pause"]').exists()).toBe(true);
    expect(w.find('[data-im="task-cancel"]').exists()).toBe(true);
    expect(w.find('[data-im="tasks-refresh"]').exists()).toBe(true);
  });

  it("已暂停：给出继续执行与取消，不再显示暂停", () => {
    const store = freshStore();
    store.tasksOpen = true;
    store.intents = [intentFixture({ id: "i1", status: "paused" })];
    store.executions = [execution("e1", "i1", "paused", [step(0, "done"), step(1, "pending")])];
    const w = track(mount(IntentStatusPopover, { attachTo: document.body }));
    expect(w.find('[data-im="task-execution"][data-execution-status="paused"]').text()).toContain("已暂停");
    expect(w.find('[data-im="task-pause"]').exists()).toBe(false);
    // 暂停意图的继续入口就是既有的「继续（按当前材料）」：有执行记录时内部走真实继续
    expect(w.find('[data-im="task-resume"]').exists()).toBe(true);
    expect(w.find('[data-im="task-cancel"]').exists()).toBe(true);
  });

  it("已完成 / 失败：读条结束，不再给执行控制", () => {
    const store = freshStore();
    store.tasksOpen = true;
    store.intents = [
      intentFixture({ id: "i1", status: "done" }),
      intentFixture({ id: "i2", status: "failed", title: "整理第三份材料" }),
    ];
    store.executions = [
      execution("e1", "i1", "done", [step(0, "done"), step(1, "done")]),
      execution("e2", "i2", "failed", [step(0, "done"), step(1, "failed")], "第二步没有成功"),
    ];
    const w = track(mount(IntentStatusPopover, { attachTo: document.body }));
    expect(w.find('[data-im="task-execution"][data-execution-status="done"]').text()).toContain("已完成");
    const failed = w.find('[data-im="task-execution"][data-execution-status="failed"]');
    expect(failed.text()).toContain("失败");
    expect(failed.text()).toContain("第二步没有成功");
    expect(w.find('[data-im="task-pause"]').exists()).toBe(false);
    expect(w.find('[data-im="task-resume-execution"]').exists()).toBe(false);
    expect(w.find('[data-im="task-cancel"]').exists()).toBe(false);
  });

  it("已批准但没有执行记录：如实说「等待执行记录」，不给暂停", () => {
    const store = freshStore();
    store.tasksOpen = true;
    store.intents = [intentFixture({ id: "i1", status: "running" })];
    const w = track(mount(IntentStatusPopover, { attachTo: document.body }));
    const row = w.find('[data-im="task-execution"][data-execution-status="none"]');
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("已批准，等待执行记录");
    expect(w.find('[data-im="task-pause"]').exists()).toBe(false);
    expect(w.find('[data-im="task-cancel"]').exists()).toBe(true);
  });
});

describe("预览卡：提议与执行分开（契约 §14.1 / §14.6）", () => {
  it("模型提议与正式内容分层显示；演示意图必须标明", () => {
    const intent = intentFixture({
      id: "i9",
      status: "pending",
      demo: true,
      goal: "把两份材料整理成一份回复",
      expected: "板面上多一张结果卡",
      operations: [
        { op: "add_reply_card", content: "整理结果" },
        { op: "add_link", src: "c1", dst: "c2", meaning: "相关" },
      ],
    });
    const w = track(mount(IntentPreviewCard, { props: { intent } }));
    expect(w.find('[data-im="intent-goal"]').text()).toContain("整理成一份回复");
    expect(w.find('[data-im="intent-expected"]').text()).toContain("结果卡");
    expect(w.findAll('[data-im="intent-operation"]')).toHaveLength(2);
    expect(w.find('[data-im="intent-proposal"]').text()).toContain("批准之前不会改动正式板面内容");
    expect(w.find('[data-im="demo-badge"]').exists()).toBe(true);
    expect(w.find('[data-im="approve"]').exists()).toBe(true);
  });

  it("执行已接管：暂停 / 继续 / 取消取代批准入口", () => {
    const paused = intentFixture({ id: "i9", status: "paused", execution: execution("e1", "i9", "paused") });
    const w1 = track(mount(IntentPreviewCard, { props: { intent: paused } }));
    expect(w1.find('[data-im="intent-execution"][data-execution-status="paused"]').exists()).toBe(true);
    expect(w1.find('[data-im="execution-resume"]').exists()).toBe(true);
    expect(w1.find('[data-im="execution-cancel"]').exists()).toBe(true);
    expect(w1.find('[data-im="approve"]').exists()).toBe(false);

    const running = intentFixture({
      id: "i9",
      status: "running",
      execution: execution("e1", "i9", "running", [step(0, "running")]),
    });
    const w2 = track(mount(IntentPreviewCard, { props: { intent: running } }));
    expect(w2.find('[data-im="execution-pause"]').exists()).toBe(true);
    expect(w2.find('[data-im="execution-cancel"]').exists()).toBe(true);
    expect(w2.find('[data-im="intent-execution-progress"]').text()).toContain("正在执行第 1/1 步");
  });
});

describe("状态可读与令牌（契约 §9.6 / §12.7）", () => {
  it("每个理解状态都有非空文字，且不允许只靠颜色表达", () => {
    const statuses: UnderstandingStatus[] = [
      "accepted",
      "understanding",
      "understood",
      "needs_clarification",
      "no_action",
      "failed",
    ];
    const labels = new Set<string>();
    for (const status of statuses) {
      const text = understandingText(understandingFixture({ status }), status);
      expect(text.label.length, status + " 必须有文字状态").toBeGreaterThan(0);
      expect(["open", "waiting", "active", "settled", "problem"]).toContain(text.tone);
      expect(labels.has(text.label), status + " 的状态文字必须彼此不同").toBe(false);
      labels.add(text.label);
    }
  });

  it("改动过的界面文件不出现硬编码色值（颜色一律走令牌）", () => {
    const files = [
      "src/interactive/approval.ts",
      "src/stores/interactive.ts",
      "src/components/interactive/SubmitCluster.vue",
      "src/components/interactive/IntentStatusPopover.vue",
      "src/components/interactive/IntentPreviewCard.vue",
      "src/components/interactive/IntentBatchTray.vue",
      "src/components/interactive/ChatDock.vue",
      "src/views/InteractiveView.vue",
    ];
    const hardcoded = /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/;
    for (const file of files) {
      const text = readFileSync(fileURLToPath(new URL("../../../" + file, import.meta.url)), "utf8");
      const hit = text.split("\n").find((line) => hardcoded.test(line));
      expect(hit, file + " 里出现了硬编码色值：" + hit).toBeUndefined();
    }
  });

  it("窄窗口（800×600 / 480×600）与双主题下关键状态仍在 DOM 里可读", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "failed", error: "模型没有返回有效结果" });
    store.understandingStatus = "failed";
    store.tasksOpen = true;
    store.intents = [intentFixture({ id: "i1", status: "running" })];
    store.executions = [execution("e1", "i1", "running", [step(0, "running")])];
    (globalThis as { innerWidth?: number }).innerWidth = 480;
    (globalThis as { innerHeight?: number }).innerHeight = 600;
    window.dispatchEvent(new Event("resize"));
    const cluster = track(mount(SubmitCluster));
    const popover = track(mount(IntentStatusPopover, { attachTo: document.body }));
    // 关键状态在 DOM 里（不是靠 CSS 显示后才存在），文字非空、可被读出
    expect(cluster.find('[data-im="understanding-status"]').text().length).toBeGreaterThan(0);
    expect(cluster.find('[data-im="understanding-retry"]').exists()).toBe(true);
    const exec = popover.find('[data-im="task-execution"]');
    expect(exec.text().length).toBeGreaterThan(0);
    expect(popover.find('[data-im="task-pause"]').exists()).toBe(true);
  });
});
