/**
 * 对话内「待批准板面意图」组件测试（P73 Stage 2）。
 *
 * 标注：【组件/DOM】挂载真实 BoardIntentInbox；【模拟】服务端读取/拒绝与共享 store 的
 * confirmIntent。要证的是：
 * - board_default 上的 proposed / awaiting_confirmation 才会出现在对话里；
 * - 「确认并落地」真的走 store.confirmIntent（boardId=board_default），成功后重读列表；
 * - 「拒绝」走既有拒绝接口并重读；
 * - 每轮对话结束后会重读（turnRunning true → false），读取失败如实显示、不假装没有。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  fetchIntents: vi.fn(),
  rejectIntent: vi.fn(),
  confirmIntent: vi.fn(),
}));

vi.mock("../../../services/interactive", () => ({
  fetchIntents: mocks.fetchIntents,
  rejectIntent: mocks.rejectIntent,
}));

vi.mock("../../../stores/interactive", () => ({
  useInteractiveStore: () => ({ confirmIntent: mocks.confirmIntent }),
}));

import BoardIntentInbox from "../BoardIntentInbox.vue";
import { useSessionStore } from "../../../stores/session";
import type { Intent } from "../../../interactive/types";

function intent(status: Intent["status"], overrides: Partial<Intent> = {}): Intent {
  return {
    id: "in_1",
    boardId: "board_default",
    submissionId: null,
    title: "整理材料",
    summary: "",
    status,
    preview: {
      cards: [
        {
          id: "a",
          kind: "reply",
          content: "结果 A",
          meta: {},
          x: 100,
          y: 100,
          w: 320,
          h: 240,
          checked: false,
          hidden: false,
          folded: false,
          bookmarked: false,
          deleted: false,
          createdAt: "2026-10-21T00:00:00+00:00",
          updatedAt: "2026-10-21T00:00:00+00:00",
        },
      ],
      groups: [],
      links: [],
      note: "",
    },
    operations: [{ op: "add_reply_card", ref: "a", content: "结果 A" }],
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
    ...overrides,
  } as Intent;
}

function pendingPayload(intents: Intent[]) {
  return { intents, conflicts: [], batchAvailable: false, recovery: { paused: [] } };
}

let pinia: ReturnType<typeof createPinia>;

function mountInbox() {
  const wrapper = mount(BoardIntentInbox, {
    attachTo: document.body,
    global: { plugins: [pinia] },
  });
  return wrapper;
}

beforeEach(() => {
  localStorage.clear();
  pinia = createPinia();
  setActivePinia(pinia);
  mocks.fetchIntents.mockReset();
  mocks.rejectIntent.mockReset();
  mocks.confirmIntent.mockReset();
  mocks.fetchIntents.mockResolvedValue(pendingPayload([]));
  mocks.rejectIntent.mockResolvedValue({ ok: true });
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.clearAllTimers();
});

describe("对话内待批准板面意图：列表", () => {
  it("没有待批准意图时不渲染任何东西（不占位、不打扰对话）", async () => {
    const wrapper = mountInbox();
    await flushPromises();
    expect(wrapper.find('[data-im="conversation-board-intents"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("只列 board_default 的 proposed / awaiting_confirmation：标题 / 目标 / 预演摘要 / 两个按钮", async () => {
    mocks.fetchIntents.mockResolvedValue(
      pendingPayload([
        intent("proposed", { id: "in_a", title: "整理材料", goal: "把材料归成两类" }),
        intent("awaiting_confirmation", { id: "in_b", title: "补充关系" }),
        intent("completed", { id: "in_done" }),
        intent("proposed", { id: "in_other", boardId: "other_board" }),
      ]),
    );
    const wrapper = mountInbox();
    await flushPromises();

    expect(mocks.fetchIntents).toHaveBeenCalledWith("board_default");
    const items = wrapper.findAll('[data-im="conversation-board-intent"]');
    expect(items).toHaveLength(2);
    expect(items[0].text()).toContain("整理材料");
    expect(items[0].text()).toContain("把材料归成两类");
    expect(items[0].text()).toContain("1 张卡片");
    expect(items[0].find('[data-im="conversation-board-confirm"]').exists()).toBe(true);
    expect(items[0].find('[data-im="conversation-board-reject"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("读取失败如实显示错误（不把失败当成「没有待批准意图」）", async () => {
    mocks.fetchIntents.mockRejectedValue(new Error("网络中断"));
    const wrapper = mountInbox();
    await flushPromises();
    const error = wrapper.find('[data-im="conversation-board-intents-error"]');
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("网络中断");
    wrapper.unmount();
  });
});

describe("对话内待批准板面意图：确认 / 拒绝 / 刷新时机", () => {
  it("确认并落地：走 store.confirmIntent(board_default)，成功后重读列表", async () => {
    mocks.fetchIntents
      .mockResolvedValueOnce(pendingPayload([intent("proposed", { id: "in_a" })]))
      .mockResolvedValueOnce(pendingPayload([]));
    mocks.confirmIntent.mockResolvedValue({
      ok: true,
      detail: "已确认并落地：板面新增 1 张卡片（结果以板面与服务端账本为准）。",
    });
    const wrapper = mountInbox();
    await flushPromises();

    await wrapper.find('[data-im="conversation-board-confirm"]').trigger("click");
    await flushPromises();

    expect(mocks.confirmIntent).toHaveBeenCalledTimes(1);
    expect(mocks.confirmIntent).toHaveBeenCalledWith("in_a", { boardId: "board_default" });
    const notice = wrapper.find('[data-im="conversation-board-intents-notice"]');
    expect(notice.text()).toContain("已确认并落地");
    // 成功后重读：第二次返回空列表 → 面板消失（预演/待批准已处理）
    expect(mocks.fetchIntents.mock.calls.length).toBeGreaterThanOrEqual(2);
    // 列表已重读为空：没有待批准条目（结果提示还留着，便于用户看到刚才发生了什么）
    expect(wrapper.findAll('[data-im="conversation-board-intent"]')).toHaveLength(0);
    wrapper.unmount();
  });

  it("确认失败：如实显示原因（不假成功）", async () => {
    mocks.fetchIntents.mockResolvedValue(pendingPayload([intent("proposed", { id: "in_a" })]));
    mocks.confirmIntent.mockResolvedValue({ ok: false, detail: "确认没有完成：与已确认的意图冲突。" });
    const wrapper = mountInbox();
    await flushPromises();

    await wrapper.find('[data-im="conversation-board-confirm"]').trigger("click");
    await flushPromises();

    const error = wrapper.find('[data-im="conversation-board-intents-action-error"]');
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("与已确认的意图冲突");
    wrapper.unmount();
  });

  it("拒绝：走既有拒绝接口并重读列表", async () => {
    mocks.fetchIntents
      .mockResolvedValueOnce(pendingPayload([intent("proposed", { id: "in_a" })]))
      .mockResolvedValueOnce(pendingPayload([]));
    const wrapper = mountInbox();
    await flushPromises();

    await wrapper.find('[data-im="conversation-board-reject"]').trigger("click");
    await flushPromises();

    expect(mocks.rejectIntent).toHaveBeenCalledWith("in_a");
    expect(mocks.fetchIntents.mock.calls.length).toBeGreaterThanOrEqual(2);
    wrapper.unmount();
  });

  it("每轮对话结束后重读一次（turnRunning true → false）", async () => {
    const wrapper = mountInbox();
    await flushPromises();
    const before = mocks.fetchIntents.mock.calls.length;
    const session = useSessionStore();
    session.turnRunning = true;
    await nextTick();
    session.turnRunning = false;
    await nextTick();
    await flushPromises();
    expect(mocks.fetchIntents.mock.calls.length).toBe(before + 1);
    wrapper.unmount();
  });
});
