/**
 * D 实机观察的定性：**带附件点「发送」没有发出 POST /api/turns**。
 *
 * 机制（本用例钉成事实）：
 * * 附件还在 prepared（首次准备没完成）→ Composer 自己的**就绪闸门**直接返回：
 *   不发请求、不动输入框，并给出可见原因（「附件还在准备中…」）；
 * * 同一个附件 ready 之后 → 正常调用 api.sendTurn，且**带准备标识**（第 6 参）。
 *
 * 所以实机里「no-request」的正确读法是「被前端就绪闸门挡住」，不是「发送处理器被短路」，
 * 也不是本轮取消接线引入的（闸门来自附件链路最初的提交 9b716ca）。
 * 顺带钉住诊断可见性：原因必须带 data-test="attach-error"，装置才能读到它。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async (..._args: unknown[]) => ({
    ok: true,
    accepted: true,
    turn_id: "turn_1",
    topic_id: null,
    bound_attachment_ids: [],
  })),
  getSessionContext: vi.fn(async () => ({
    topic_id: "",
    topic_name: null,
    anchor_fragment: null,
    messages: [],
  })),
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
import type { AttachmentRef } from "../../services/attachments";

function refWith(state: AttachmentRef["state"]): AttachmentRef {
  return {
    id: "att_big",
    name: "r7-大附件.bin",
    sizeBytes: 94 * 1024 * 1024,
    kind: "copy",
    display: "已保存副本",
    state,
    error: null,
  };
}

async function mountComposer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();
  return w;
}

async function attachWith(w: VueWrapper, ref: AttachmentRef) {
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

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [] });
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
});

describe("附件还在准备中时的发送入口（D 实机 no-request 的定性）", () => {
  it("prepared 的附件：**不发请求**，但给出可见原因（不静默）", async () => {
    const w = await mountComposer();
    await attachWith(w, refWith("prepared"));

    await w.find("textarea").setValue("带附件的消息");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    expect(mocks.sendTurn, "附件没就绪时不得发出请求").not.toHaveBeenCalled();
    const reason = w.find('[data-test="attach-error"]');
    expect(reason.exists(), "必须给出可见原因（装置也要能读到）").toBe(true);
    expect(reason.text()).toContain("还在准备中");
    // 输入框内容原样保留（用户不必重写）
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("带附件的消息");
    w.unmount();
  });

  it("ready 之后：正常发出请求，且带上准备标识", async () => {
    const w = await mountComposer();
    await attachWith(w, refWith("ready"));

    await w.find("textarea").setValue("附件就绪的消息");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    expect(mocks.sendTurn, "就绪之后必须正常发出请求").toHaveBeenCalledTimes(1);
    const args = mocks.sendTurn.mock.calls[0] ?? [];
    expect(args[2]).toEqual(["att_big"]);
    expect(String(args[5] ?? ""), "必须带准备标识（取消端点靠它定位）").not.toBe("");
    w.unmount();
  });
});
