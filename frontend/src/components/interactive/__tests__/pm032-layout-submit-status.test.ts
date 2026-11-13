/**
 * PM-QIO032 #80（UI）/ #03（UI）：重复提交与轮次主状态
 *
 * 数据层由 i-state 提供（第三轮 commit 1754b8b8 / e37cf86e）：
 * - 未改动的重复提交：store.submitStatus="duplicate"、roundStatusLabel="已提交过"、不再发 POST；
 * - 原受理记录由 store.lastSubmission / latestAcceptedSubmission 保留，empty/duplicate 回执不覆盖；
 * 本文件只验证 **SubmitCluster 的 UI 消费**（不伪造 store 判定）：
 * 1) duplicate 主状态显示「已提交过」，给出「查看原进度」；
 * 2) 点「查看原进度」打开的上次提交结果面板仍指向上一次真正被受理的记录；
 * 3) 轮次主状态：非特殊状态取 store.roundStatusLabel；duplicate / unknown 用任务书措辞
 *    （合并 i-state 第三轮后两者一致；合并前也不会被旧标签说成笼统「已结束」）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ pending: { expressions: [] } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(),
  saveDrafts: vi.fn(),
  saveBoardState: vi.fn(),
  submitBoard: vi.fn(),
  checkMaterialImpact: vi.fn(),
  fetchAcceptance: vi.fn(),
}));

const mounted: VueWrapper[] = [];

function lastSubmissionFixture() {
  return {
    status: "succeeded",
    submission: { id: "s_original", seq: 7, status: "succeeded", createdAt: "2026-11-13T00:00:00Z" },
    before: { cards: [], groups: [], links: [], selection: [], empty: true },
    after: { cards: [], groups: [], links: [], selection: [], empty: true },
    expressions: [{ id: "e1", kind: "note_added", summary: "原受理记录里的改动" }],
    baseline: { updated: true, previousSeq: 6, seq: 7 },
    delivery: { delivered: true, reason: "", detail: "" },
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
    checkedCleared: [],
  } as never;
}

function mountCluster(setup: (store: ReturnType<typeof useInteractiveStore>) => void) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useInteractiveStore();
  store.board = board.emptyState("board_t");
  setup(store);
  const wrapper = mount(SubmitCluster, { global: { plugins: [pinia] } });
  mounted.push(wrapper);
  return { wrapper, store };
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("#80 重复提交（UI）：显示「已提交过」并可查看原进度", () => {
  it("duplicate 主状态只出现一处；「查看原进度」打开上次提交结果面板", async () => {
    const { wrapper } = mountCluster((store) => {
      store.submitStatus = "duplicate";
      store.lastSubmission = lastSubmissionFixture();
    });
    await flushPromises();

    // 主状态：本次提交：已提交过（不再显示失败/未知/待确认）
    const status = wrapper.get('[data-im="submit-status"]').text();
    expect(status).toContain("已提交过");
    expect(wrapper.get('[data-im="submit-status"]').text()).not.toContain("失败");
    expect(wrapper.find('[data-im="submit-failure"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="submit-unknown"]').exists()).toBe(false);

    // duplicate 专属一块：说明没有再次执行，并提供查看原进度
    const duplicate = wrapper.get('[data-im="submit-duplicate"]');
    expect(duplicate.text()).toContain("已提交过");
    const viewButton = wrapper.get('[data-im="submit-duplicate-view"]');
    expect(viewButton.text()).toContain("查看原进度");
    expect(wrapper.find('[data-im="submit-result-panel"]').exists()).toBe(false);

    // 查看原进度 → 上次提交结果面板（读 store.lastSubmission，i-state 保证指向原受理记录）
    await viewButton.trigger("click");
    expect(wrapper.find('[data-im="submit-duplicate"]').exists()).toBe(true);
    const result = wrapper.get('[data-im="submit-result-panel"]');
    expect(result.text()).toContain("上次提交");
    // 结果面板读 store.lastSubmission（i-state 保证 duplicate 不覆盖原受理记录）：
    // 原受理记录的改动条数被如实说出，不是本次重复提交编的新数字。
    expect(result.text()).toContain("本次 1 项改动已记录");
  });
});

describe("#03 主状态（UI）：统一轮次标签来自 store.roundStatusLabel", () => {
  it("非特殊状态用 store 的统一轮次标签（提交中 → 提交中，而不是旧组件自己的说法）", async () => {
    // 说明区（.sc-notes）只在有状态可说明时渲染；submitting 是一条真实主状态。
    const { wrapper, store } = mountCluster((current) => {
      current.submitStatus = "submitting";
    });
    await flushPromises();
    const label = (store as unknown as { roundStatusLabel?: string }).roundStatusLabel ?? "";
    expect(label).toBeTruthy();
    expect(label).not.toContain("已结束");
    const status = wrapper.get('[data-im="submit-status"]').text();
    expect(status).toContain(label);
    // 主状态只出现一处
    expect(wrapper.findAll('[data-im="submit-status"]').length).toBe(1);
  });
});
