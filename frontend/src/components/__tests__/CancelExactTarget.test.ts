/**
 * 契约 §1.2：中止必须指向**取消确认返回的 turn_id**。
 *
 * 真缺陷（plan §0 第 2 条）：`already_started` 分支走 `stopTurn()` →
 * `session.stopActiveTurn()` → 停的是 **session.activeTurnId**。当**另一轮**在跑（A）
 * 而本次要中止的是 B 时，实际取消的是 A —— 停了别的任务；B 已入队但回执未到前端时
 * （activeTurnId 为空）更会退回 `cancelActiveTurn()`，取消「当前 active」。
 *
 * 本文件断言**精确取消目标 ID**，并断言另一运行任务没收到取消 ——
 * 「某个停止入口被调用过」不算通过。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(),
  getSessionContext: vi.fn(async () => ({
    topic_id: "",
    topic_name: null,
    anchor_fragment: null,
    messages: [],
  })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: "turn_active" })),
  cancelPreparing: vi.fn(),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  removeAttachment: vi.fn(async () => undefined),
  retryAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  pickLocalPath: vi.fn(async () => null),
  onPathDrop: vi.fn(() => () => undefined),
  openAttachment: vi.fn(async () => undefined),
  relocateAttachment: vi.fn(),
  restorePendingAttachments: vi.fn(async () => ({ items: [], dropped: [] })),
  savePendingAttachments: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    cancelTurn: mocks.cancelTurn,
    cancelActiveTurn: mocks.cancelActiveTurn,
    cancelPreparing: mocks.cancelPreparing,
  },
}));

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    prepareAttachment: mocks.prepareAttachment,
    uploadAttachment: mocks.uploadAttachment,
    removeAttachment: mocks.removeAttachment,
    retryAttachment: mocks.retryAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    pickLocalPath: mocks.pickLocalPath,
    onPathDrop: mocks.onPathDrop,
    openAttachment: mocks.openAttachment,
    relocateAttachment: mocks.relocateAttachment,
    restorePendingAttachments: mocks.restorePendingAttachments,
    savePendingAttachments: mocks.savePendingAttachments,
    isDesktopShell: () => false,
  };
});

import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";
import type { AttachmentRef } from "../../services/attachments";

const READY: AttachmentRef = {
  id: "att_ready",
  name: "报告.txt",
  sizeBytes: 2048,
  kind: "copy",
  display: "已保存副本",
  state: "ready",
  error: null,
};

const PREPARING = '[data-test="preparing-attachments"]';
const NOTICE = '[data-test="preparing-notice"]';

interface Options {
  /** 另一轮（A）正在运行：普通「停止」会打到它 */
  activeTurnId?: string | null;
  /** 取消端点的确认结果（默认：已放行，且给出本次 turn_id） */
  cancelResult?: unknown;
  cancelFails?: boolean;
  /** 确认挂住不返回：用于「确认还在路上时重复点击」 */
  cancelPending?: boolean;
}

async function mountAndStartSend(options: Options = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  if (options.activeTurnId) {
    session.activeTurnId = options.activeTurnId;
    session.turnRunning = true;
  }

  let settleSend: (value: unknown) => void = () => undefined;
  let prepareId: string | undefined;
  mocks.sendTurn.mockImplementationOnce((...args: unknown[]) => {
    prepareId = args[5] as string | undefined;
    return new Promise((resolve) => {
      settleSend = resolve as (value: unknown) => void;
    });
  });
  let releaseCancel: (value: unknown) => void = () => undefined;
  mocks.cancelPreparing.mockImplementation(async () => {
    if (options.cancelPending) {
      return new Promise((resolve) => {
        releaseCancel = resolve;
      });
    }
    if (options.cancelFails) throw new Error("网络断了");
    return (
      options.cancelResult ?? {
        ok: true,
        cancelled: false,
        already_started: true,
        turn_id: "turn_this_send",
      }
    );
  });

  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();

  // 附件 ready + 发送（请求挂住 = 正在准备）
  mocks.uploadAttachment.mockResolvedValueOnce(READY);
  mocks.waitUntilSettled.mockResolvedValueOnce(READY);
  const fileInput = w.find("input.file-input");
  Object.defineProperty(fileInput.element, "files", {
    value: [new File(["x"], READY.name)],
    configurable: true,
  });
  await fileInput.trigger("change");
  await flushPromises();

  await w.find("textarea").setValue("要中止的消息");
  await w.find(".send-btn").trigger("click");
  await flushPromises();
  // 过「正在准备附件…」的防抖阈值（既有口径）
  await new Promise((resolve) => setTimeout(resolve, 260));
  await nextTick();

  return {
    w,
    session,
    prepareId: () => prepareId,
    settleSend: (value: unknown = { ok: true, accepted: true, turn_id: "turn_this_send" }) =>
      settleSend(value),
    releaseCancel: (value: unknown = { ok: true, cancelled: true, turn_id: "turn_this_send" }) =>
      releaseCancel(value),
  };
}

async function clickAbort(w: VueWrapper) {
  const cancel = w.find('[data-test="preparing-cancel"]');
  expect(cancel.exists(), "准备状态必须能中止").toBe(true);
  await cancel.trigger("click");
  await flushPromises();
  await nextTick();
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [] });
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
});

describe("中止的目标身份（契约 §1.2）", () => {
  it("另一轮在跑：只取消**取消确认返回的** turn_id，绝不误伤运行中的那一轮", async () => {
    const { w, session, prepareId } = await mountAndStartSend({ activeTurnId: "turn_other_running" });
    await clickAbort(w);

    expect(mocks.cancelPreparing).toHaveBeenCalledWith(prepareId());
    expect(mocks.cancelTurn, "必须调用一次精确取消").toHaveBeenCalledTimes(1);
    expect(
      mocks.cancelTurn.mock.calls[0]?.[0],
      "取消目标必须是取消确认返回的 turn_id，而不是 activeTurnId",
    ).toBe("turn_this_send");
    expect(
      mocks.cancelTurn.mock.calls.map((call) => call[0]),
      "另一运行任务不得收到取消",
    ).not.toContain("turn_other_running");
    expect(mocks.cancelActiveTurn, "不得退回「停当前任务」").not.toHaveBeenCalled();
    expect(session.activeTurnId, "另一轮仍然是当前活动轮").toBe("turn_other_running");
    w.unmount();
  });

  it("本次那一轮已入队但回执未到前端（activeTurnId 为空）：仍按确认的 turn_id 取消", async () => {
    const { w } = await mountAndStartSend({ activeTurnId: null });
    await clickAbort(w);

    expect(mocks.cancelTurn.mock.calls.map((call) => call[0])).toEqual(["turn_this_send"]);
    expect(
      mocks.cancelActiveTurn,
      "不得用「取消当前 active」顶替（那会取消别的任务）",
    ).not.toHaveBeenCalled();
    w.unmount();
  });

  it("身份缺失（already_started 但没给 turn_id）：明确说明，不静默退回停当前任务", async () => {
    const { w } = await mountAndStartSend({
      activeTurnId: "turn_other_running",
      cancelResult: { ok: true, cancelled: false, already_started: true },
    });
    await clickAbort(w);

    expect(mocks.cancelTurn, "没有身份就不该猜一个目标").not.toHaveBeenCalled();
    expect(mocks.cancelActiveTurn, "也不得退回停当前任务").not.toHaveBeenCalled();
    const notice = w.find(NOTICE);
    expect(notice.exists()).toBe(true);
    expect(notice.text(), "必须明确说明拿不到身份").toMatch(/标识|身份|无法确认/);
    expect(notice.text()).not.toContain("已按「停止」取消");
    w.unmount();
  });

  it("取消失败：不宣称成功，也不发出任何取消", async () => {
    const { w } = await mountAndStartSend({ cancelFails: true });
    await clickAbort(w);

    expect(mocks.cancelTurn).not.toHaveBeenCalled();
    expect(mocks.cancelActiveTurn).not.toHaveBeenCalled();
    const notice = w.find(NOTICE);
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("中止失败");
    w.unmount();
  });

  it("这一轮其实已经结束：如实说明当前状态，不说「没有发送」", async () => {
    const { w } = await mountAndStartSend({
      cancelResult: { ok: true, cancelled: false, already_started: true, turn_id: "turn_done" },
    });
    mocks.cancelTurn.mockResolvedValueOnce({ ok: false, cancelled: false, turn_id: "turn_done" });
    await clickAbort(w);

    expect(mocks.cancelTurn.mock.calls.map((call) => call[0])).toEqual(["turn_done"]);
    const text = w.text();
    expect(text, "已执行的轮次不得说「没有发送」").not.toContain("没有发送");
    expect(w.find(NOTICE).text(), "要如实说明：停止请求没有可取消的目标").toMatch(
      /已经结束|没有可取消|未能停止|失败/,
    );
    w.unmount();
  });

  it("确认还在路上时重复点击：只问一次后端（按钮也在 busy 中）", async () => {
    const { w, releaseCancel } = await mountAndStartSend({
      activeTurnId: "turn_other_running",
      cancelPending: true,
    });
    await clickAbort(w);
    // 确认未到：界面显示「正在中止…」，按钮 disabled → 第二次点击不会再问一次
    expect(w.find('[data-test="preparing-cancel"]').attributes("disabled")).toBeDefined();
    await clickAbort(w);

    expect(mocks.cancelPreparing, "重复点击不得重复询问后端").toHaveBeenCalledTimes(1);

    releaseCancel({ ok: true, cancelled: true, turn_id: "turn_this_send" });
    await flushPromises();
    await nextTick();
    expect(mocks.cancelTurn, "cancelled 分支不该发停止请求").not.toHaveBeenCalled();
    expect(mocks.cancelPreparing).toHaveBeenCalledTimes(1);
    w.unmount();
  });

  it("结论已定之后：中止入口收起，不存在第二次重复取消", async () => {
    const { w } = await mountAndStartSend({ activeTurnId: "turn_other_running" });
    await clickAbort(w);

    expect(mocks.cancelPreparing).toHaveBeenCalledTimes(1);
    expect(mocks.cancelTurn).toHaveBeenCalledTimes(1);
    // 后端说这一轮已经放行：它不再是「准备中」，中止入口如实收起（不会重复取消）
    expect(w.find(PREPARING).exists(), "已放行之后不得继续显示准备状态").toBe(false);
    expect(w.find('[data-test="preparing-cancel"]').exists()).toBe(false);
    w.unmount();
  });

  it("普通「停止当前任务」按钮语义不变：仍然停止当前 active 轮", async () => {
    const { w, session } = await mountAndStartSend({ activeTurnId: "turn_other_running" });
    // 先把这次准备中的发送放掉（受理），让普通停止按钮可用
    const stop = w.find(".stop-btn");
    expect(stop.exists()).toBe(true);
    await stop.trigger("click");
    await flushPromises();

    expect(mocks.cancelTurn.mock.calls.map((call) => call[0])).toEqual(["turn_other_running"]);
    expect(mocks.cancelActiveTurn).not.toHaveBeenCalled();
    expect(session.activeTurnId).toBe("turn_other_running");
    w.unmount();
  });
});
