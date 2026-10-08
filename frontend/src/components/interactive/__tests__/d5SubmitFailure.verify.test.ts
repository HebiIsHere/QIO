/**
 * 独立验收 D5-D：提交失败原因默认可见（契约 §11.6）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 按用户行为断言：不展开任何「详情」，默认的提交区就要说清**本次请求**的真实原因，
 * 不许拿上一次提交结果或通用说法糊过去。
 *
 * 标注：【组件/DOM】挂载真实 SubmitCluster.vue；【模拟】受控 store 状态。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyBoardState, type SubmissionResult } from "../../../interactive/types";
import { fetchBoardState } from "../../../services/interactive";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
}));

function previousSubmission(error: string | null): SubmissionResult {
  return {
    status: error ? "failed" : "succeeded",
    submission: { id: "sub_old", error: error ?? undefined },
    delivery: { delivered: false, reason: "" },
    expressions: [],
    checkedCleared: [],
    before: {},
    after: {},
    baseline: {},
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as unknown as SubmissionResult;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(fetchBoardState).mockResolvedValue({
    board: { id: "board_default", title: "板面" },
    state: emptyBoardState("board_default"),
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
    pending: { expressions: [] },
  } as never);
  setActivePinia(createPinia());
});

describe("场景 7：提交失败时默认区域显示本次原因（§11.6）", () => {
  it("【组件/DOM】上一次提交成功、本次请求失败：默认区域必须含本次原因", async () => {
    const store = useInteractiveStore();
    store.board = emptyBoardState("board_default");
    store.lastSubmission = previousSubmission(null);
    store.submitStatus = "failed";
    store.submitError = "网络中断：提交请求没有到达服务端";

    const wrapper = mount(SubmitCluster, { attachTo: document.body });
    await flushPromises();

    expect(wrapper.text(), "默认失败区域看不到本次提交失败的真实原因").toContain("网络中断");
    expect(wrapper.find('[data-im="submit-details-box"]').exists(), "默认状态下详情不该是展开的").toBe(false);
  });

  it("【组件/DOM】上一次提交也失败过：默认区域不许拿上一次的旧原因当本次原因", async () => {
    const store = useInteractiveStore();
    store.board = emptyBoardState("board_default");
    store.lastSubmission = previousSubmission("上一次提交的旧原因");
    store.submitStatus = "failed";
    store.submitError = "本次提交的真实原因：服务端拒绝了这次提交";

    const wrapper = mount(SubmitCluster, { attachTo: document.body });
    await flushPromises();

    const text = wrapper.text();
    expect(text, "默认失败区域没有说本次提交失败的真实原因").toContain("本次提交的真实原因");
    expect(
      text.includes("上一次提交的旧原因") && !text.includes("本次提交的真实原因"),
      "默认失败区域显示的是上一次提交的旧原因",
    ).toBe(false);
  });
});
