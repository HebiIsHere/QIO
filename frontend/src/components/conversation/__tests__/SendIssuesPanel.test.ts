/**
 * 入口①「发送异常与草稿」里的「上次没有执行的消息」区（SendIssuesPanel.vue）。
 *
 * 迁移自旧的 components/InterruptedTurnEntry.vue 测试（2026-10-26 合并后清理 · C1）：
 * 旧的常驻入口已删除，继续 / 忽略 / 全部忽略 / 真实失败可重试 / 结果说明留在屏幕上
 * 这些有效能力由入口①承接。负向断言一条不少：不自动重放、失败不静默、成功后列表消失。
 *
 * 另外迁移旧的「悬浮聊天」组件级草稿保存失败断言（原 stores/__tests__/draftPersistence.test.ts，
 * 2026-10-26 · C2）：旧 ChatDock 删除后，草稿保存失败的真实原因 + 重试入口在入口①这里。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SendIssuesPanel from "../SendIssuesPanel.vue";
import { useSessionStore } from "../../../stores/session";
import type { InterruptedTurn } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  resendInterruptedTurn: vi.fn(),
  dismissInterruptedTurn: vi.fn(),
}));

vi.mock("../../../services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(detail);
      this.name = "ApiError";
    }
  },
  api: {
    resendInterruptedTurn: mocks.resendInterruptedTurn,
    dismissInterruptedTurn: mocks.dismissInterruptedTurn,
  },
}));

function turn(id: string, message: string): InterruptedTurn {
  return {
    turn_id: id,
    message,
    topic_id: "topic_1",
    status: "interrupted",
    reason: "queued_at_restart",
    reason_text: "这条消息当时还在排队，进程退出后没有开始执行",
    created_at: "2026-10-10T10:00:00+00:00",
    ended_at: "2026-10-10T10:05:00+00:00",
  };
}

function setup(turns: InterruptedTurn[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.applyInterruptedState([], turns, []);
  const w = mount(SendIssuesPanel, { global: { plugins: [pinia] } });
  return { w, session };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.resendInterruptedTurn.mockResolvedValue({ ok: true, turn_id: "turn_new", status: "queued" });
  mocks.dismissInterruptedTurn.mockResolvedValue({ ok: true, dismissed: "turn_a" });
});

describe("发送异常面板 · 上次没有执行的消息", () => {
  it("没有未执行的消息时不渲染这一区", () => {
    const { w } = setup([]);
    expect(w.find('[data-section="interrupted"]').exists()).toBe(false);
  });

  it("列出原文、原因与两个动作；不自动重放", () => {
    const { w } = setup([turn("turn_a", "帮我把发布闸门跑一遍")]);
    const section = w.find('[data-section="interrupted"]');
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain("帮我把发布闸门跑一遍");
    expect(section.text()).toContain("不会自动重放");
    expect(section.text()).toContain("继续发送这条");
    expect(section.text()).toContain("忽略");
    // 打开面板本身不发任何请求（不自动重发）
    expect(mocks.resendInterruptedTurn).not.toHaveBeenCalled();
    expect(mocks.dismissInterruptedTurn).not.toHaveBeenCalled();
  });

  it("点「继续发送这条」→ 调 resend，成功后这一条消失并留下结果说明", async () => {
    const { w, session } = setup([turn("turn_a", "帮我把发布闸门跑一遍")]);
    await w.find('[data-section="interrupted"] button.qio-btn.mini.primary').trigger("click");
    await flushPromises();
    expect(mocks.resendInterruptedTurn).toHaveBeenCalledWith("turn_a");
    expect(session.interruptedTurns).toEqual([]);
    expect(w.find('[data-section="interrupted"]').exists()).toBe(false);
    expect(w.find(".notice").text()).toContain("已经按原话题重新排队");
  });

  it("请求在飞的时候按钮禁用并显示「提交中…」（重复点击不会发第二次）", async () => {
    const { w } = setup([turn("turn_a", "帮我把发布闸门跑一遍")]);
    let release: (v: unknown) => void = () => {};
    mocks.resendInterruptedTurn.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    await w.find('[data-section="interrupted"] button.qio-btn.mini.primary').trigger("click");
    await flushPromises();
    const button = w.find('[data-section="interrupted"] button.qio-btn.mini.primary');
    expect(button.text()).toBe("提交中…");
    expect(button.attributes("disabled")).toBeDefined();
    await button.trigger("click");
    await flushPromises();
    expect(mocks.resendInterruptedTurn).toHaveBeenCalledTimes(1);
    release({ ok: true, turn_id: "turn_new", status: "queued" });
    await flushPromises();
  });

  it("网络失败 → 入口保留，并说明可以重试（不静默）", async () => {
    const { w, session } = setup([turn("turn_a", "帮我把发布闸门跑一遍")]);
    mocks.resendInterruptedTurn.mockRejectedValueOnce(new Error("网络断了"));
    await w.find('[data-section="interrupted"] button.qio-btn.mini.primary').trigger("click");
    await flushPromises();
    expect(mocks.resendInterruptedTurn).toHaveBeenCalledTimes(1);
    // 失败不能把记录清掉
    expect(session.interruptedTurns).toHaveLength(1);
    expect(w.find('[data-section="interrupted"]').exists()).toBe(true);
    expect(w.find(".notice").text()).toContain("可以重试");
  });

  it("点「忽略」→ 调 dismiss，成功后这一条消失并留下结果说明", async () => {
    const { w, session } = setup([turn("turn_a", "帮我把发布闸门跑一遍")]);
    await w.find('[data-section="interrupted"] button.qio-btn.mini.quiet').trigger("click");
    await flushPromises();
    expect(mocks.dismissInterruptedTurn).toHaveBeenCalledWith("turn_a");
    expect(session.interruptedTurns).toEqual([]);
    expect(w.find('[data-section="interrupted"]').exists()).toBe(false);
    expect(w.find(".notice").text()).toContain("已忽略这一条");
  });

  it("多条时给一次「全部忽略」，一条时不给（没意义的多余操作）", async () => {
    const two = setup([turn("turn_a", "第一条"), turn("turn_b", "第二条")]);
    expect(two.w.find('[data-section="interrupted"] button.bulk').text()).toContain("全部忽略（2 条）");
    two.w.unmount();

    const one = setup([turn("turn_a", "第一条")]);
    expect(one.w.find('[data-section="interrupted"] button.bulk').exists()).toBe(false);
    one.w.unmount();
  });
});
describe("发送异常面板 · 草稿保存失败", () => {
  const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

  function breakLocalStorage(message: string): void {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: () => {
          throw new Error(message);
        },
        setItem: () => {
          throw new Error(message);
        },
        removeItem: () => {
          throw new Error(message);
        },
      },
    });
  }

  afterEach(() => {
    if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
  });

  it("显示真实原因与重试入口，不说「已保存」", async () => {
    breakLocalStorage("本地存储被禁用");
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.draft = "写不进本机的字";
    session.flushDraft();
    expect(session.draftSaveStatus).toBe("error");

    const w = mount(SendIssuesPanel, { global: { plugins: [pinia] } });
    const section = w.find('[data-section="draft-save"]');
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain("草稿没有保存进本机");
    expect(section.text()).toContain("本地存储被禁用");
    expect(w.text()).not.toContain("已保存");
    expect(w.find('[data-im="composer-draft-retry"]').exists()).toBe(true);

    // 存储恢复后点重试：只重写草稿，状态改成已保存
    if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
    await w.find('[data-im="composer-draft-retry"]').trigger("click");
    await flushPromises();
    expect(session.draftSaveStatus).toBe("saved");
    expect(w.find('[data-section="draft-save"]').exists()).toBe(false);
    w.unmount();
  });
});
