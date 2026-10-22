/**
 * 发送 / 停止共用一个按钮（B，契约 §6）。
 *
 * 四态与校验语义：
 * - 空闲 + 无内容 → 禁用发送；
 * - 有文本或附件 → 发送态（回复期间提交进入队列，不阻塞下一句）；
 * - 回复中 + 无内容 → 停止态（共用同一个按钮）；
 * - 有内容但附件还在准备 / 只带附件没有文字 → 仍是发送态，但**不满足发送条件**：
 *   点击会给出真实校验原因，不发请求、不丢文本与附件。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async (..._args: unknown[]) => ({
    ok: true,
    accepted: true,
    turn_id: "turn_1",
    topic_id: null,
    bound_attachment_ids: [],
  })),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
  cancelPreparing: vi.fn(async () => ({ ok: true, cancelled: true })),
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

vi.mock("../../../services/api", () => ({
  api: {
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    cancelTurn: mocks.cancelTurn,
    cancelActiveTurn: mocks.cancelActiveTurn,
    cancelPreparing: mocks.cancelPreparing,
  },
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
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

import Composer from "../../Composer.vue";
import { useSessionStore } from "../../../stores/session";
import type { AttachmentRef } from "../../../services/attachments";

function attachment(state: AttachmentRef["state"], id = "att_1"): AttachmentRef {
  return {
    id,
    name: "资料.txt",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state,
    error: null,
  };
}

async function mountComposer(): Promise<{ w: VueWrapper; session: ReturnType<typeof useSessionStore> }> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();
  return { w, session };
}

async function attach(w: VueWrapper, ref: AttachmentRef) {
  mocks.uploadAttachment.mockResolvedValueOnce(ref);
  mocks.waitUntilSettled.mockResolvedValueOnce(ref);
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: [new File(["x"], ref.name)],
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}

function primary(w: VueWrapper) {
  return w.find(".send-btn");
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.sendTurn.mockResolvedValue({
    ok: true,
    accepted: true,
    turn_id: "turn_1",
    topic_id: null,
    bound_attachment_ids: [],
  } as never);
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [] } as never);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item);
});

describe("发送 / 停止共用按钮四态", () => {
  it("空闲且无内容：禁用发送态", async () => {
    const { w } = await mountComposer();
    const btn = primary(w);
    expect(btn.attributes("data-mode")).toBe("disabled");
    expect(btn.attributes("disabled")).toBeDefined();
    expect(btn.attributes("aria-label") ?? "").toContain("发送");
    w.unmount();
  });

  it("有文本：发送态且满足发送条件", async () => {
    const { w } = await mountComposer();
    await w.find("textarea").setValue("你好 QIO");
    const btn = primary(w);
    expect(btn.attributes("data-mode")).toBe("send");
    expect(btn.attributes("data-ready")).toBe("true");
    expect(btn.attributes("disabled")).toBeUndefined();
    await btn.trigger("click");
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledWith("你好 QIO", null, []);
    w.unmount();
  });

  it("回复中且无内容：同一按钮变成停止态，点击真正提交停止", async () => {
    const { w, session } = await mountComposer();
    session.turnRunning = true;
    session.activeTurnId = "turn_1";
    await nextTick();
    const btn = primary(w);
    expect(btn.attributes("data-mode")).toBe("stop");
    expect(btn.attributes("aria-label") ?? "").toContain("停止");
    // 旧选择器兼容：停止模式仍带 .stop-btn 语义类（样式由共用按钮统一接管）
    expect(w.find(".send-btn.stop-btn").exists()).toBe(true);
    await btn.trigger("click");
    await flushPromises();
    // 已知 turn_id：精确取消这一轮（不误伤排队消息）
    expect(mocks.cancelTurn).toHaveBeenCalledWith("turn_1");
    w.unmount();

    // 还没拿到 turn_id：退化为「取消后端当前的 active turn」，仍是同一个共用按钮
    const second = await mountComposer();
    second.session.turnRunning = true;
    second.session.activeTurnId = null;
    await nextTick();
    expect(primary(second.w).attributes("data-mode")).toBe("stop");
    await primary(second.w).trigger("click");
    await flushPromises();
    expect(mocks.cancelActiveTurn).toHaveBeenCalledTimes(1);
    second.w.unmount();
  });

  it("回复中且有文本：仍是发送态，提交进入现有队列（不阻塞下一句）", async () => {
    const { w, session } = await mountComposer();
    session.turnRunning = true;
    session.activeTurnId = "turn_1";
    await nextTick();
    await w.find("textarea").setValue("排队的一句");
    const btn = primary(w);
    expect(btn.attributes("data-mode")).toBe("send");
    expect(btn.attributes("title")).toContain("排队");
    await btn.trigger("click");
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledWith("排队的一句", null, []);
    const last = session.messages[session.messages.length - 1];
    expect(last?.content).toBe("排队的一句");
    expect(last?.queued).toBe(true);
    w.unmount();
  });
});

describe("「存在内容」与「满足发送条件」分开", () => {
  it("附件准备中：按钮仍是发送态但不 ready；点击不发请求并给出可见原因", async () => {
    const { w } = await mountComposer();
    await attach(w, attachment("prepared"));
    await w.find("textarea").setValue("带附件的消息");
    const btn = primary(w);
    expect(btn.attributes("data-mode")).toBe("send");
    expect(btn.attributes("data-ready")).toBe("false");

    await btn.trigger("click");
    await flushPromises();
    expect(mocks.sendTurn, "附件没就绪时不得发出请求").not.toHaveBeenCalled();
    const reason = w.find('[data-test="attach-error"]');
    expect(reason.exists()).toBe(true);
    expect(reason.text()).toContain("还在准备中");
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("带附件的消息");
    w.unmount();
  });

  it("只带附件没有文字：有内容（发送态）但不满足条件；不丢附件、不假装已发", async () => {
    const { w } = await mountComposer();
    await attach(w, attachment("ready"));
    await nextTick();
    const btn = primary(w);
    expect(btn.attributes("data-mode")).toBe("send");
    expect(btn.attributes("data-ready")).toBe("false");
    expect(btn.attributes("aria-label") ?? "").toContain("发送");

    await btn.trigger("click");
    await flushPromises();
    expect(mocks.sendTurn).not.toHaveBeenCalled();
    const reason = w.find('[data-test="attach-error"]');
    expect(reason.exists()).toBe(true);
    expect(reason.text()).toContain("只带附件还不能发送");
    // 附件仍在待发列表里，文字框仍有焦点/可用（没有清空任何东西）
    expect(w.find(".attach-row").exists()).toBe(true);
    w.unmount();
  });
});
