/**
 * IRL-D · 提交入口只显示**最新事实**（任务书 §5.1 — §5.5）。
 *
 * 本次的已知缺陷点：`submitNote` / `understandingNote` 与理解状态的组合 ——
 * 受理时写下的「正在理解」在理解已经有结论（理解完成 / 本次无需任务）之后
 * 仍然与当前状态并列显示，于是界面上同时出现两个互相矛盾的阶段。
 *
 * 这里挂真实组件，断言四件事：
 * - 当前区只有**最新一条**事实；旧阶段的说明只进可展开的过程历史；
 * - 待审批出现时审批入口**主动展开**（不必先展开详情），批准 / 拒绝 / 定位 / 批量入口都可达；
 * - 审批之后的执行事实只取服务端记录，不把「已批准」说成「已完成」；
 * - 提交成功但回执没有理解请求身份时，不许说成「还没有提交给 QIO」。
 * 服务层全部 mock。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { INTENT_BATCH_STORAGE_KEY } from "../../../interactive/approval";
import type { Execution, Intent, Understanding } from "../../../interactive/types";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ pending: { expressions: [] } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
  saveBoardState: vi.fn(async () => ({ ok: true, seq: 1, savedAt: "t" })),
  submitBoard: vi.fn(),
  fetchUnderstanding: vi.fn(async () => ({ understanding: null, understandings: [] })),
  fetchExecutions: vi.fn(async () => ({ executions: [] })),
}));

const NOW = "2026-10-13T00:00:00.000Z";
const mounted: VueWrapper[] = [];

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

function intentFixture(over: Partial<Intent> = {}): Intent {
  return {
    id: "i1",
    boardId: "board_t",
    submissionId: "s1",
    understandingId: "u1",
    title: "整理两份材料",
    summary: "",
    status: "pending",
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

function executionFixture(intentId: string, status: Execution["status"]): Execution {
  return {
    id: "e_" + intentId,
    intentId,
    boardId: "board_t",
    attempt: 1,
    status,
    steps: [],
    result: { cardIds: [], groupIds: [], linkIds: [] },
    error: null,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function recordBatches(groups: Record<string, string[]>): void {
  const records = Object.entries(groups).flatMap(([key, ids]) => ids.map((id) => ({ id, key })));
  localStorage.setItem(INTENT_BATCH_STORAGE_KEY, JSON.stringify({ version: 1, records }));
}

function mountCluster(): VueWrapper {
  const wrapper = mount(SubmitCluster);
  mounted.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("当前只有最新一条事实（§5.2 / §5.3）", () => {
  it("本次无需开展任务时，不再并列显示旧阶段的「正在理解」", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "no_action", kind: "no_action" });
    store.understandingStatus = "no_action";
    // 受理那一刻写下的说明（写的是「正在理解」）—— 理解已经有结论，它只是历史
    store.submitNote =
      "已受理：QIO 已经可靠收下这次提交，正在理解。受理不等于理解完成，也不等于批准执行。";
    const w = mountCluster();

    // 2026-10-15：已定局的结论不在主栏重复，结论就在 chip 上
    expect(w.get('[data-im="submit-history-toggle"]').text()).toContain("无需开展任务");
    expect(
      w.find('[data-im="understanding"]').exists(),
      "已定局、无需行动的理解结论不再占主栏说明区",
    ).toBe(false);
    expect(w.get('[data-im="submit-cluster"]').text()).not.toContain("正在理解");
    expect(
      w.find('[data-im="submit-acceptance-note"]').exists(),
      "旧阶段的说明不在当前区",
    ).toBe(false);

    // 2026-10-14：用户要求详情里只留「本次有效改动」，过程历史不再渲染；
    // 「旧阶段不与当前状态并列」这条保证仍然成立（上面已断言）
    expect(w.find('[data-im="understanding-history"]').exists()).toBe(false);
  });

  it("理解完成之后，旧的「自动查询已停止」不再是当前说明", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "understood", kind: "intent" });
    store.understandingStatus = "understood";
    store.understandingNote = "自动查询已停止：结果还没有回来，可以点「刷新理解状态」继续查询。";
    const w = mountCluster();

    expect(w.find('[data-im="understanding"]').exists()).toBe(false);
    expect(w.get('[data-im="submit-cluster"]').text()).not.toContain("自动查询已停止");
    expect(w.find('[data-im="understanding-note"]').exists()).toBe(false);
    // 2026-10-14：过程历史已删除（用户要求），这里只核对当前说明不含旧阶段
    expect(w.find('[data-im="understanding-history"]').exists()).toBe(false);
  });

  it("还在等结果时，同一阶段的说明就是当前说明（同阶段更新、不提前收进历史）", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understandingStatus = "understanding";
    store.understandingNote = "页面不在前台：已暂停自动查询，回到页面会继续。";
    const w = mountCluster();
    expect(w.get('[data-im="understanding-note"]').text()).toContain("页面不在前台");
    expect(w.get('[data-im="understanding"]').text()).not.toContain("自动查询已停止");
  });

  it("提交成功但回执没有理解请求身份：不许说成「还没有提交给 QIO」", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.submitAcceptedByLookup = true;
    store.submitNote = "这次提交的回执在传输中丢了：已按同一个请求身份查证，服务端确认它已被可靠受理。";
    store.understandingStatus = null;
    const w = mountCluster();
    const status = w.get('[data-im="submit-status"]').text();
    expect(status).toContain("已提交");
    expect(w.get('[data-im="submit-cluster"]').text()).not.toContain("还没有提交给 QIO");
    expect(w.get('[data-im="submit-acceptance-note"]').text()).toContain("同一个请求身份");
  });
});

describe("待审批出现时审批入口主动展开（§5.4）", () => {
  it("不展开详情就能看见待审批条目与批准 / 拒绝 / 定位", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "understood", kind: "intent" });
    store.understandingStatus = "understood";
    store.intents = [
      intentFixture({ id: "i1", title: "整理材料一" }),
      intentFixture({ id: "i2", title: "整理材料二" }),
    ];
    const w = mountCluster();

    // 详情没有自动弹开：审批入口本身就在当前区展开着
    expect(w.find('[data-im="submit-details-box"]').exists()).toBe(false);
    const approvals = w.get('[data-im="submit-approvals"]');
    expect(approvals.attributes("data-approval-count")).toBe("2");
    expect(w.get('[data-im="submit-approval-state"]').text()).toContain("待审批 2 项");
    expect(w.get('[data-im="submit-approval-state"]').text()).toContain("批准前不会改动板面");
    const rows = w.findAll('[data-im="submit-approval-item"]');
    expect(rows).toHaveLength(2);
    for (const hook of ["submit-approve", "submit-reject", "submit-locate"]) {
      expect(w.findAll('[data-im="' + hook + '"]')).toHaveLength(2);
    }

    // 批准走既有 store 入口（同一个意图只提交一次）
    const approve = vi.spyOn(store, "approve").mockResolvedValue({ ok: true, detail: "已批准" });
    await w.findAll('[data-im="submit-approve"]')[0].trigger("click");
    expect(approve).toHaveBeenCalledTimes(1);
    expect(approve.mock.calls[0][0]).toBe("i1");

    // 拒绝同样走既有入口
    const reject = vi.spyOn(store, "reject").mockResolvedValue({ ok: true });
    await w.findAll('[data-im="submit-reject"]')[1].trigger("click");
    expect(reject.mock.calls[0][0]).toBe("i2");
  });

  it("定位派发既有的板面定位事件（不产生审批，也不调用模型）", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "understood", kind: "intent" });
    store.understandingStatus = "understood";
    store.intents = [
      intentFixture({
        id: "i1",
        preview: { cards: [], groups: [], links: [], note: "" },
      }),
    ];
    const seen: { intentId?: string }[] = [];
    const listener = (event: Event) => {
      const detail = (event as CustomEvent).detail ?? {};
      seen.push({ intentId: detail.intentId });
    };
    window.addEventListener("qio:interactive:locate-preview", listener);
    try {
      const w = mountCluster();
      await w.get('[data-im="submit-locate"]').trigger("click");
      expect(seen).toHaveLength(1);
      expect(seen[0].intentId).toBe("i1");
    } finally {
      window.removeEventListener("qio:interactive:locate-preview", listener);
    }
  });

  it("同一批够四项时给出既有批量列表的入口（不另造第二套批量操作）", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "understood", kind: "intent" });
    store.understandingStatus = "understood";
    store.intents = ["i1", "i2", "i3", "i4"].map((id) => intentFixture({ id }));
    recordBatches({ "session:submission:s1": ["i1", "i2", "i3", "i4"] });
    const w = mountCluster();
    const open = w.get('[data-im="submit-open-batch"]');
    expect(open.text()).toContain("批量审批");
    await open.trigger("click");
    // 唯一的批量状态是任务面板：入口真实打开「待确认」分区（旧 batchOpen 通道已删除）
    expect(store.tasksOpen).toBe(true);
    expect(store.taskCenterFocus).toBe("pending");
  });

  it("审批之后的执行事实只取服务端记录：不把「已批准」说成「已完成」", () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "understood", kind: "intent" });
    store.understandingStatus = "understood";
    store.intents = [
      intentFixture({ id: "i1", status: "running" }),
      intentFixture({ id: "i2", status: "running" }),
      intentFixture({ id: "i3", status: "done" }),
    ];
    store.executions = [
      executionFixture("i1", "running"),
      executionFixture("i2", "paused"),
      executionFixture("i3", "done"),
    ];
    const w = mountCluster();
    expect(w.find('[data-im="submit-approvals"]').exists()).toBe(false);
    const state = w.get('[data-im="submit-handled-state"]').text();
    expect(state).toContain("执行中 1 项");
    expect(state).toContain("等待执行记录 1 项");
    expect(state).toContain("已结束 1 项");
    expect(state).toContain("服务端执行记录");
  });

  it("旧提交的理解过程不出现在当前状态里（2026-10-14：历史区已按用户要求删除）", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ status: "understood", kind: "intent" });
    store.understandingStatus = "understood";
    store.understandings = [
      store.understanding,
      understandingFixture({ id: "u0", submissionId: "s0", status: "no_action", summary: "上一次提交没有任务" }),
    ];
    const w = mountCluster();
    expect(w.find('[data-im="understanding"]').exists()).toBe(false);
    expect(w.get('[data-im="submit-cluster"]').text()).not.toContain("上一次提交");
    // 历史区与「查看过程」入口都已删除，旧提交的理解过程不再出现在界面上
    expect(w.find('[data-im="understanding-history"]').exists()).toBe(false);
    // 「查看过程」入口仍在（它打开的是「上次提交过程」面板），但不再有历史区
  });
});
