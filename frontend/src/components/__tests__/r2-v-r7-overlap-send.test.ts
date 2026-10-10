/**
 * V 组独立验证：R7（重叠发送各自持有准备状态；I1–I5）。
 *
 * 装置：真实 Composer + 真实 session store；只把网络与文件对话框换成受控桩，
 * 用**受控 Promise**把第一次请求卡在「后端尚未受理」，再让第二次发送发生。
 * 时间用 vi 的假定时器推进（不是随机 sleep）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(),
  cancelPreparing: vi.fn(),
  cancelTurn: vi.fn(),
  cancelActiveTurn: vi.fn(),
  getSessionContext: vi.fn(),
  uploadAttachment: vi.fn(),
  prepareAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
  restorePendingAttachments: vi.fn(),
  savePendingAttachments: vi.fn(),
  pickLocalPath: vi.fn(),
  onPathDrop: vi.fn(),
  openAttachment: vi.fn(),
  isDesktopShell: vi.fn(),
}));

vi.mock("../../services/api", () => {
  const base: Record<string, unknown> = {
    sendTurn: mocks.sendTurn,
    cancelPreparing: mocks.cancelPreparing,
    cancelTurn: mocks.cancelTurn,
    cancelActiveTurn: mocks.cancelActiveTurn,
    getSessionContext: mocks.getSessionContext,
  };
  return {
    api: new Proxy(base, {
      get(target, prop: string) {
        if (prop in target) return target[prop];
        return vi.fn(async () => ({}));
      },
    }),
  };
});

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    uploadAttachment: mocks.uploadAttachment,
    prepareAttachment: mocks.prepareAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    removeAttachment: mocks.removeAttachment,
    retryAttachment: mocks.retryAttachment,
    relocateAttachment: mocks.relocateAttachment,
    restorePendingAttachments: mocks.restorePendingAttachments,
    pickLocalPath: mocks.pickLocalPath,
    onPathDrop: mocks.onPathDrop,
    openAttachment: mocks.openAttachment,
    isDesktopShell: mocks.isDesktopShell,
  };
});

import Composer from "../Composer.vue";
import AttachmentChip from "../AttachmentChip.vue";
import type { AttachmentRef } from "../../services/attachments";

const TOPIC = "topic-v-r7";

function chip(id: string, state: AttachmentRef["state"] = "ready"): AttachmentRef {
  return {
    id,
    name: id + ".bin",
    sizeBytes: 1024,
    kind: "copy",
    display: "已保存副本",
    state,
    error: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function okSend(turnId = "turn_v_r7") {
  return { ok: true, accepted: true, turn_id: turnId, topic_id: TOPIC, bound_attachment_ids: [] };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mountComposer(): Promise<VueWrapper> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = (await import("../../stores/session")).useSessionStore(pinia);
  session.currentTopicId = TOPIC;
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flush();
  await vi.advanceTimersByTimeAsync(1);
  return w;
}

async function attachReady(w: VueWrapper, ref: AttachmentRef): Promise<void> {
  mocks.uploadAttachment.mockResolvedValueOnce(ref);
  mocks.waitUntilSettled.mockResolvedValueOnce(ref);
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: [new File(["x"], ref.name)],
    configurable: true,
  });
  await input.trigger("change");
  await flush();
}

function textareaValue(w: VueWrapper): string {
  return (w.find("textarea").element as HTMLTextAreaElement).value;
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.sendTurn.mockResolvedValue(okSend());
  mocks.cancelPreparing.mockResolvedValue({ ok: true, cancelled: true, turn_id: "turn_v_r7" });
  mocks.cancelTurn.mockResolvedValue({ ok: true, cancelled: true, turn_id: "turn_v_r7" });
  mocks.cancelActiveTurn.mockResolvedValue({ ok: true, cancelled: true });
  mocks.getSessionContext.mockResolvedValue({
    topic_id: TOPIC,
    topic_name: null,
    anchor_fragment: null,
    messages: [],
    turn_facts: [],
    tool_records: [],
    has_more: false,
    next_before: null,
  });
  mocks.uploadAttachment.mockResolvedValue(chip("att_default"));
  mocks.prepareAttachment.mockResolvedValue(chip("att_default"));
  mocks.waitUntilSettled.mockResolvedValue(chip("att_default"));
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.retryAttachment.mockResolvedValue(chip("att_default"));
  mocks.relocateAttachment.mockResolvedValue(chip("att_default"));
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [] });
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.onPathDrop.mockReturnValue(() => undefined);
  mocks.openAttachment.mockResolvedValue(undefined as never);
  mocks.isDesktopShell.mockReturnValue(false);
});

describe("R7 重叠发送：每次发送各持一份准备状态", () => {
  it("第二次纯文字发送：不清掉第一次的中止入口（I2），中止仍绑定第一次的 prepareId（I3）", async () => {
    const w = await mountComposer();
    await attachReady(w, chip("att_one"));

    const gate = deferred<ReturnType<typeof okSend>>();
    mocks.sendTurn.mockImplementationOnce(() => gate.promise);

    await w.find("textarea").setValue("第一条（带附件）");
    await w.find(".send-btn").trigger("click");
    await flush();
    // 过了防抖阈值：中止入口可见（I3：始终可达）
    await vi.advanceTimersByTimeAsync(260);
    expect(w.find('[data-test="preparing-cancel"]').exists()).toBe(true);

    const firstPrepareId = String(mocks.sendTurn.mock.calls[0]?.[5] ?? "");
    expect(firstPrepareId, "第一次发送必须有自己的准备标识").not.toBe("");

    // 用户把这条附件从待发列表移除，于是下一条是纯文字
    const chipComp = w.findComponent(AttachmentChip);
    chipComp.vm.$emit("remove", "att_one");
    await flush();
    expect(w.findAllComponents(AttachmentChip)).toHaveLength(0);

    await w.find("textarea").setValue("第二条（纯文字）");
    await w.find(".send-btn").trigger("click");
    await flush();

    expect(mocks.sendTurn, "第二次发送应当正常派发").toHaveBeenCalledTimes(2);
    const secondArgs = mocks.sendTurn.mock.calls[1] ?? [];
    expect(secondArgs[2], "第二次没有附件").toEqual([]);
    expect(secondArgs[5], "第二次没有准备标识（它不带附件）").toBeUndefined();

    // I2：第二次的收尾不得清掉第一次的准备状态
    expect(
      w.find('[data-test="preparing-cancel"]').exists(),
      "第一次的中止入口被第二次发送清掉了",
    ).toBe(true);

    // I3：点中止只作用于第一次自己的 prepareId
    await w.find('[data-test="preparing-cancel"]').trigger("click");
    await flush();
    expect(mocks.cancelPreparing).toHaveBeenCalledTimes(1);
    expect(mocks.cancelPreparing.mock.calls[0]?.[0]).toBe(firstPrepareId);

    gate.resolve(okSend());
    await flush();
    w.unmount();
  });

  it("两次带附件发送：第二次在派发前被拒，草稿与附件保留，第一次完全不受影响（I4）", async () => {
    const w = await mountComposer();
    await attachReady(w, chip("att_two"));

    const gate = deferred<ReturnType<typeof okSend>>();
    mocks.sendTurn.mockImplementationOnce(() => gate.promise);

    await w.find("textarea").setValue("第一条（带附件）");
    await w.find(".send-btn").trigger("click");
    await flush();
    await vi.advanceTimersByTimeAsync(260);
    expect(w.find('[data-test="preparing-cancel"]').exists()).toBe(true);

    // 第二次：**带附件**（pending 里还有第一条的附件）
    await w.find("textarea").setValue("第二条（也带附件）");
    await w.find(".send-btn").trigger("click");
    await flush();

    expect(mocks.sendTurn, "第二次带附件发送必须在派发前被拒").toHaveBeenCalledTimes(1);
    const reason = w.find('[data-test="attach-error"]');
    expect(reason.exists(), "必须给出可见原因").toBe(true);
    expect(reason.text()).toContain("还没有被后端受理");
    // 草稿与附件都留着
    expect(textareaValue(w)).toBe("第二条（也带附件）");
    expect(w.findAllComponents(AttachmentChip)).toHaveLength(1);
    // 第一次的界面与状态一动不动
    expect(w.find('[data-test="preparing-cancel"]').exists()).toBe(true);

    // 第一次受理之后：拦截解除、发送恢复正常
    gate.resolve(okSend());
    await flush();
    expect(w.find('[data-test="preparing-cancel"]').exists()).toBe(false);
    expect(w.findAllComponents(AttachmentChip), "受理后 chip 清掉").toHaveLength(0);

    await attachReady(w, chip("att_three"));
    await w.find("textarea").setValue("第三条（带附件）");
    await w.find(".send-btn").trigger("click");
    await flush();
    expect(mocks.sendTurn, "第一次受理后发送恢复正常").toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it("卸载：清掉所有准备定时器，但**不** abort 在途请求（I5）", async () => {
    const w = await mountComposer();
    await attachReady(w, chip("att_unmount"));

    const gate = deferred<ReturnType<typeof okSend>>();
    mocks.sendTurn.mockImplementationOnce(() => gate.promise);

    await w.find("textarea").setValue("在途的一条");
    await w.find(".send-btn").trigger("click");
    await flush();
    await vi.advanceTimersByTimeAsync(260);

    const signal = mocks.sendTurn.mock.calls[0]?.[4] as AbortSignal | undefined;
    expect(signal, "带附件的发送必须能拿到自己的取消目标").toBeTruthy();
    expect(signal?.aborted).toBe(false);
    expect(vi.getTimerCount(), "准备定时器应当存在").toBeGreaterThan(0);

    w.unmount();
    await flush();
    await vi.advanceTimersByTimeAsync(500);

    expect(vi.getTimerCount(), "卸载后不得留下悬挂定时器").toBe(0);
    expect(signal?.aborted, "卸载不得 abort 在途请求（用户已发起的消息必须跑完）").toBe(false);

    gate.resolve(okSend());
    await flush();
  });
});
