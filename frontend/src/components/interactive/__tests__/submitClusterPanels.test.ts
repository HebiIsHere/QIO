/**
 * 提交区布局重构（2026-10-15 · task-2 / TB-L1）：两行主栏 + 三类互斥展开面板。
 *
 * 挂真实 SubmitCluster.vue，只 mock 服务层；断言的是**用户看得见的行为**：
 * - 正常状态只有主栏（没有说明区第三行、也没有凭空编的「上次提交」）；
 * - chip 上只写简短结论；完整说明在「上次提交结果」面板里；
 * - 三类面板共用同一个互斥状态：同一时间只开一个、再点同一入口收起；
 * - 三类数据不许串用（上次提交结果 vs 本次提交范围与改动 vs 上次提交过程）；
 * - aria-expanded / aria-controls 指向真实存在的面板；Esc 收起；
 * - 展开面板不触发提交。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { Expression, Understanding } from "../../../interactive/types";

const pendingExpressions: Expression[] = [
  {
    id: "e1",
    kind: "note_added",
    intentBearing: true,
    summary: "新增注释：本次要提交的那条",
  } as unknown as Expression,
];

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({
    boardId: "board_t",
    seq: 1,
    updatedAt: "2026-10-15T00:00:00.000Z",
    cards: [],
    groups: [],
    links: [],
    selection: [],
    pending: { expressions: pendingExpressions },
  })),
}));

const NOW = "2026-10-15T00:00:00.000Z";
const mounted: VueWrapper[] = [];

function track(wrapper: VueWrapper): VueWrapper {
  mounted.push(wrapper);
  return wrapper;
}

function understandingFixture(over: Partial<Understanding> = {}): Understanding {
  return {
    id: "u1",
    boardId: "board_t",
    submissionId: "s1",
    requestId: "req-1",
    status: "understood",
    stage: "",
    kind: "intent",
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
  store.board = board.emptyState("board_t");
  store.saveStatus = "saved";
  return store;
}

async function mountCluster(): Promise<VueWrapper> {
  const wrapper = track(mount(SubmitCluster));
  await flushPromises();
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

describe("默认只有两行主栏：不凭空出现第三行，也不编造上次提交", () => {
  it("没有历史提交、没有失败 / 未知 / 待审批时，说明区与「上次提交」都不渲染", async () => {
    const store = freshStore();
    store.submitStatus = "idle";
    const w = await mountCluster();

    expect(w.find('[data-im="submit-notes"]').exists(), "正常状态不该多出第三行").toBe(false);
    expect(w.find('[data-im="submit-history"]').exists()).toBe(false);
    expect(w.find('[data-im="submit-history-toggle"]').exists()).toBe(false);
    // 主栏两行该有的东西都在
    expect(w.get('[data-im="visible-range"]').text()).toContain("本次可见");
    expect(w.get('[data-im="submit-pending"]').text().length).toBeGreaterThan(0);
    expect(w.find('[data-im="submit-details"]').exists()).toBe(true);
    expect(w.get('[data-im="submit"]').text()).toBe("提交给 QIO");
  });

  it("有真实提交记录、还没有理解记录时，chip 只说「已提交」，不虚构理解结论", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.lastSubmission = {
      status: "succeeded",
      submission: { id: "s1", status: "succeeded" },
      expressions: [],
      checkedCleared: [],
      delivery: { delivered: true, reason: "" },
    } as never;
    const w = await mountCluster();
    expect(w.get('[data-im="submit-history-toggle"]').text()).toBe("上次提交 · 已提交");
  });

  it("只有提交状态、没有任何服务端记录时，不渲染「上次提交」（不许虚构）", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    const w = await mountCluster();
    expect(w.find('[data-im="submit-history-toggle"]').exists()).toBe(false);
  });

  it("结果未知时说明区出现（本次事实），但 chip 不出现（没有可证明的历史提交）", async () => {
    const store = freshStore();
    store.submitStatus = "unknown";
    const w = await mountCluster();
    expect(w.find('[data-im="submit-unknown"]').exists()).toBe(true);
    expect(w.find('[data-im="submit-history-toggle"]').exists()).toBe(false);
  });
});

describe("三类面板互斥、可收起，且 aria 指向真实面板", () => {
  async function withHistory() {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ summary: "上次提交的完整结论说明" });
    store.understandingStatus = "understood";
    store.understandings = [
      understandingFixture({ id: "u0", submissionId: "s0", status: "no_action", summary: "上一次提交的过程记录" }),
      store.understanding,
    ];
    const w = await mountCluster();
    return { store, w };
  }

  it("chip 打开「上次提交结果」，同一入口再点收起", async () => {
    const { w } = await withHistory();
    const chip = w.get('[data-im="submit-history-toggle"]');
    expect(chip.text()).toBe("上次提交 · 理解完成");
    expect(chip.attributes("aria-controls")).toBe("submit-result-panel");
    expect(chip.attributes("aria-expanded")).toBe("false");

    await chip.trigger("click");
    expect(chip.attributes("aria-expanded")).toBe("true");
    const panel = w.get('[data-im="submit-result-panel"]');
    expect(panel.attributes("id")).toBe("submit-result-panel");
    expect(panel.text()).toContain("上次提交结果");
    expect(panel.text()).toContain("上次提交的完整结论说明");

    await chip.trigger("click");
    expect(w.find('[data-im="submit-result-panel"]').exists()).toBe(false);
    expect(chip.attributes("aria-expanded")).toBe("false");
  });

  it("三个入口共用同一个互斥状态：开一个就关掉另一个", async () => {
    const { w } = await withHistory();
    await w.get('[data-im="submit-details"]').trigger("click");
    expect(w.find('[data-im="submit-details-box"]').exists()).toBe(true);
    expect(w.find('[data-im="submit-result-panel"]').exists()).toBe(false);

    await w.get('[data-im="submit-history-toggle"]').trigger("click");
    expect(w.find('[data-im="submit-result-panel"]').exists()).toBe(true);
    expect(w.find('[data-im="submit-details-box"]').exists()).toBe(false);

    await w.get('[data-im="understanding-history-toggle"]').trigger("click");
    expect(w.find('[data-im="submit-process-panel"]').exists()).toBe(true);
    expect(w.find('[data-im="submit-result-panel"]').exists()).toBe(false);
    expect(w.find('[data-im="submit-details-box"]').exists()).toBe(false);
  });

  it("Esc 收起当前面板", async () => {
    const { w } = await withHistory();
    await w.get('[data-im="submit-history-toggle"]').trigger("click");
    expect(w.find('[data-im="submit-result-panel"]').exists()).toBe(true);
    await w.get('[data-im="submit-history-toggle"]').trigger("keydown", { key: "Escape" });
    expect(w.find('[data-im="submit-result-panel"]').exists()).toBe(false);
  });
});

describe("三类数据不许串用", () => {
  it("「上次提交结果」只说上次结论；「详情」才说本次范围与改动", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ summary: "上次提交的完整结论说明" });
    store.understandingStatus = "understood";
    const w = await mountCluster();

    await w.get('[data-im="submit-history-toggle"]').trigger("click");
    const result = w.get('[data-im="submit-result-panel"]').text();
    expect(result).toContain("上次提交的完整结论说明");
    expect(result, "上次提交结果不许掺入本次未提交的改动列表").not.toContain("本次有效改动");
    expect(result).not.toContain("新增注释：本次要提交的那条");

    await w.get('[data-im="submit-details"]').trigger("click");
    const details = w.get('[data-im="submit-details-box"]').text();
    expect(details).toContain("本次提交范围与改动");
    expect(details).toContain("本次允许 QIO 查看");
    expect(details).toContain("新增注释：本次要提交的那条");
    expect(details, "本次范围与改动里不许出现上次提交的结论").not.toContain("上次提交的完整结论说明");
  });

  it("「上次提交过程」只读记录：不出现本次改动列表，也不触发提交", async () => {
    const store = freshStore();
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture({ summary: "本次结论" });
    store.understandingStatus = "understood";
    store.understandings = [
      understandingFixture({ id: "u0", submissionId: "s0", status: "no_action", summary: "上一次提交的过程记录" }),
      store.understanding,
    ];
    const submit = vi.spyOn(store, "submit");
    const w = await mountCluster();

    await w.get('[data-im="understanding-history-toggle"]').trigger("click");
    const process = w.get('[data-im="submit-process-panel"]').text();
    expect(process).toContain("上次提交过程");
    expect(process).toContain("上一次提交的过程记录");
    expect(process).not.toContain("本次有效改动");
    expect(submit, "查看过程绝不允许触发提交").not.toHaveBeenCalled();
    expect(process).toContain("不会启动新任务");
  });
});
