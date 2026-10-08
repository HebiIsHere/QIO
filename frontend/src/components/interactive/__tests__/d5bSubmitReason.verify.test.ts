/**
 * 独立验收 D2-D：提交失败默认区必须说**本次**的事实（契约 §11.6）。
 *
 * 由独立验收子智能体 D2 编写，**不修改任何产品代码**。
 * 三条我没写过的反例：
 * 1. 板面保存也失败时，默认区不许声称「已保存的板面…都保留」（把会丢的改动说成安全）；
 * 2. 很长的失败原因要完整出现在默认区（不截断），且不出现「板面提交接口」这类实现用语；
 * 3. 重试成功之后，上一次失败的原因不许继续留在默认区。
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

async function mountCluster() {
  const wrapper = mount(SubmitCluster, { attachTo: document.body });
  await flushPromises();
  return wrapper;
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

describe("D2 反例：提交失败默认区按本次事实证明（§11.6）", () => {
  it("【组件/DOM】板面保存也失败：不许声称「已保存的板面都保留」，要说清还没存到服务器", async () => {
    const store = useInteractiveStore();
    store.board = emptyBoardState("board_default");
    store.lastSubmission = previousSubmission(null);
    store.submitStatus = "failed";
    store.submitError = "本次提交的真实原因：服务端拒绝了这次提交";
    store.saveStatus = "error";
    store.saveError = "板面保存失败：网络中断";

    const wrapper = await mountCluster();
    const text = wrapper.text();

    expect(text, "本次提交失败的真实原因没有出现在默认区").toContain("服务端拒绝了这次提交");
    expect(
      text,
      "板面保存也失败时，默认区却声称「已保存的板面…都保留」（把还没存到服务器的改动说成安全）",
    ).not.toContain("已保存的板面");
    expect(text, "板面保存失败时没有如实说明改动还没存到服务器").toMatch(/没有保存|还没有存到服务器|保存失败/);
    expect(wrapper.find('[data-im="submit-details-box"]').exists(), "默认状态下详情不该是展开的").toBe(false);
    expect(wrapper.find('button[data-im="submit"]').attributes("disabled"), "失败后重新提交按钮被禁用了").toBeUndefined();
  });

  it("【组件/DOM】很长的失败原因：默认区完整显示（不截断），也不出现「接口」这类实现用语", async () => {
    const tail = "长原因的结尾标记-D2";
    const longReason =
      "网络中断：提交请求没有到达服务端，浏览器报告 TypeError: Failed to fetch；" + "补充说明".repeat(30) + tail;
    const store = useInteractiveStore();
    store.board = emptyBoardState("board_default");
    store.lastSubmission = previousSubmission("上一次提交的旧原因");
    store.submitStatus = "failed";
    store.submitError = longReason;

    const wrapper = await mountCluster();
    const text = wrapper.text();

    expect(text, "默认区没有完整显示这次的长原因（结尾被截掉了）").toContain(tail);
    expect(text, "默认区出现了「接口」这类实现说明（用户看不懂）").not.toContain("接口");
    expect(text, "默认区把上一次提交的旧原因当成了本次原因").not.toContain("上一次提交的旧原因");
    expect(wrapper.find('button[data-im="submit"]').exists(), "失败后重新提交按钮不见了").toBe(true);
  });

  it("【组件/DOM】重试成功之后：默认区不许继续显示上一次失败的原因", async () => {
    const store = useInteractiveStore();
    store.board = emptyBoardState("board_default");
    store.submitStatus = "failed";
    store.submitError = "上一次失败的原因-D2";
    const wrapper = await mountCluster();
    expect(wrapper.find('[data-im="submit-failure"]').exists(), "失败时默认区没有失败说明").toBe(true);

    store.submitStatus = "succeeded";
    store.lastSubmission = previousSubmission(null);
    await flushPromises();
    expect(wrapper.find('[data-im="submit-failure"]').exists(), "重试成功之后失败说明还留在默认区").toBe(false);
    expect(wrapper.text(), "重试成功之后还在显示上一次失败的原因").not.toContain("上一次失败的原因-D2");
  });
});
