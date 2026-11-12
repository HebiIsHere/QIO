/**
 * 文案 9 / 11 / 12（shell-recovery）。
 *
 * 9：任务列表「刷新」悬停只写「刷新任务列表」。
 * 11：队列常态用「待发送消息」；取消回执用实际状态；去掉两处后端确认解释。
 * 12：继续/停止操作条保留真实暂停原因和按钮；旧回执不得清掉等待期间出现的新暂停。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  cancelTurn: vi.fn(),
  respondApproval: vi.fn(),
}));

vi.mock("../../../services/api", () => ({
  api: {
    cancelTurn: mocks.cancelTurn,
    respondApproval: mocks.respondApproval,
  },
}));

import QueuePanel from "../QueuePanel.vue";
import ContinueBar from "../../ContinueBar.vue";
import { COMPOSER_ENTRY_TEXT } from "../entries";
import { useSessionStore } from "../../../stores/session";
import taskRailSource from "../TaskRail.vue?raw";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function freshPinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

describe("文案 9：刷新任务列表悬停只有一句", () => {
  it("刷新按钮只带「刷新任务列表」这个说明，不再附带用途解释", () => {
    const at = taskRailSource.indexOf('icon="refresh"');
    expect(at, "TaskRail 里应有刷新按钮").toBeGreaterThan(-1);
    const start = Math.max(0, at - 120);
    const end = taskRailSource.indexOf("/>", at);
    const segment = taskRailSource.slice(start, end);
    expect(segment).toContain('label="刷新任务列表"');
    expect(segment).not.toContain("description=");
    // 说明文案只在 label 属性里出现一次（注释不算界面文案）
    expect(taskRailSource.match(/label="刷新任务列表"/g) ?? []).toHaveLength(1);
  });
});

describe("文案 11：待发送消息与取消回执的实际状态", () => {
  it("入口与面板都用「待发送消息」，不再写后端确认解释", async () => {
    expect(COMPOSER_ENTRY_TEXT.queue.label).toBe("待发送消息");
    expect(COMPOSER_ENTRY_TEXT.queue.title).toBe("待发送消息");

    const pinia = freshPinia();
    const session = useSessionStore();
    session.turnQueue = {
      running: null,
      queued: [{ turn_id: "q1", message: "还没发出去的消息" }],
      cancelled: [],
    } as never;
    const w = mount(QueuePanel, { global: { plugins: [pinia] } });
    await flushPromises();
    const text = w.text();
    expect(text).toContain("待发送");
    expect(text).not.toContain("以后端确认为准");
    expect(text).not.toContain("取消是请求");
    w.unmount();
  });

  it("取消回执说已取消就写「已取消」；后端没取消就如实说没取消", async () => {
    mocks.cancelTurn.mockResolvedValueOnce({ ok: true, cancelled: true, turn_id: "q1" });
    const pinia = freshPinia();
    const session = useSessionStore();
    session.turnQueue = {
      running: null,
      queued: [{ turn_id: "q1", message: "还没发出去的消息" }],
      cancelled: [],
    } as never;
    const w = mount(QueuePanel, { global: { plugins: [pinia] } });
    await flushPromises();
    await w.find(".row-actions button").trigger("click");
    await flushPromises();
    expect(w.find(".notice").text()).toContain("已取消");
    expect(w.find(".notice").text()).not.toContain("以后端确认为准");
    w.unmount();

    // 后端说没取消：不能宣称已取消
    mocks.cancelTurn.mockResolvedValueOnce({ ok: true, cancelled: false, turn_id: "q1" });
    const pinia2 = freshPinia();
    const session2 = useSessionStore();
    session2.turnQueue = {
      running: null,
      queued: [{ turn_id: "q1", message: "还没发出去的消息" }],
      cancelled: [],
    } as never;
    const w2 = mount(QueuePanel, { global: { plugins: [pinia2] } });
    await flushPromises();
    await w2.find(".row-actions button").trigger("click");
    await flushPromises();
    expect(w2.find(".notice").text()).toContain("没有取消");
    w2.unmount();
  });
});

describe("文案 12：继续/停止操作条保留真实原因和按钮", () => {
  function mountBar(id: string, message = "这一轮没有新的进展") {
    const pinia = freshPinia();
    const session = useSessionStore();
    session.pendingContinue = {
      id,
      used: 3,
      max: 128,
      reason: "no_progress",
      budgetKind: "",
      message,
    } as never;
    const w = mount(ContinueBar, { global: { plugins: [pinia] } });
    return { w, session };
  }

  it("提交失败：真实暂停原因与两个按钮都留在原位，只多一句可重试原因", async () => {
    mocks.respondApproval.mockRejectedValueOnce(new Error("网络断了"));
    const { w, session } = mountBar("appr_1");
    await w.find("button.qio-btn.primary").trigger("click");
    await flushPromises();

    expect(session.pendingContinue?.id).toBe("appr_1");
    const text = w.text();
    expect(text).toContain("这一轮没有新的进展");
    expect(text).toContain("继续");
    expect(text).toContain("停止");
    expect(w.find(".qio-feedback.err").text()).toContain("网络断了");
    w.unmount();
  });

  it("被点的按钮显示「提交中…」，另一个按钮仍在", async () => {
    const pending = deferred<{ ok: boolean }>();
    mocks.respondApproval.mockReturnValueOnce(pending.promise as never);
    const { w } = mountBar("appr_2");
    await w.find("button.qio-btn.primary").trigger("click");
    await flushPromises();
    expect(w.find("button.qio-btn.primary").text()).toBe("提交中…");
    expect(w.find("button.qio-btn.quiet").text()).toBe("停止");
    pending.resolve({ ok: true });
    await flushPromises();
    w.unmount();
  });

  it("旧回执晚到：不能清掉等待期间出现的新暂停（原因和按钮都要保留）", async () => {
    const pending = deferred<{ ok: boolean }>();
    mocks.respondApproval.mockReturnValueOnce(pending.promise as never);
    const { w, session } = mountBar("appr_old");
    await w.find("button.qio-btn.primary").trigger("click");
    await flushPromises();

    // 等待回执期间，另一轮又被预算暂停了：新的真实原因与按钮必须留下
    session.pendingContinue = {
      id: "appr_new",
      used: 128,
      max: 128,
      reason: "budget",
      budgetKind: "tokens",
      message: "输出 token 预算耗尽（90000/90000）",
    } as never;
    await flushPromises();

    pending.resolve({ ok: true });
    await flushPromises();

    expect(session.pendingContinue?.id, "旧回执把新的暂停清掉了").toBe("appr_new");
    const text = w.text();
    expect(text).toContain("输出 token 预算已用完");
    expect(text).toContain("继续");
    expect(text).toContain("停止");
    w.unmount();
  });
});
