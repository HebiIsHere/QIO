/**
 * R4 问题二（前端交互）：附件没附上时，界面必须说清原因、保留文本与附件，
 * 并给出「移除这些附件后发送」。
 *
 * 修复前红：结构化 409 只有一句错误；没有逐条原因、也没有移除后重发的入口。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/ComposerAttachmentRejection.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  restorePendingAttachments: vi.fn(),
  savePendingAttachments: vi.fn(),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  pickLocalPath: vi.fn(),
  onPathDrop: vi.fn(),
  openAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return { ...actual, api: { ...actual.api, sendTurn: mocks.sendTurn } };
});

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
import { ApiError } from "../../services/api";
import type { AttachmentRef } from "../../services/attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "会议纪要.txt",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function structured409(): ApiError {
  const body = {
    detail: {
      code: "attachment_binding_failed",
      message: "有 1 个附件没有附上：att_1（这个附件已经属于别的一轮了）",
      rejected: [{ id: "att_1", reason: "这个附件已经属于别的一轮了" }],
      bound_attachment_ids: [],
    },
  };
  return new ApiError(409, "/api/turns", JSON.stringify(body), body);
}

async function mountComposer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await nextTick();
  await flushPromises();
  return { w, pinia };
}

function ok() {
  return { ok: true, accepted: true, turn_id: "turn_new", status: "accepted", topic_id: null };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.restorePendingAttachments.mockResolvedValue({ items: [ref()], dropped: [] });
  mocks.savePendingAttachments.mockResolvedValue(undefined);
  mocks.sendTurn.mockResolvedValue(ok());
});

describe("附件没附上：文本与附件留着，原因与出口都在", () => {
  it("结构化 409 → 草稿与附件保留、逐条原因可见、「移除这些附件后发送」可用", async () => {
    mocks.sendTurn.mockRejectedValueOnce(structured409());
    const { w } = await mountComposer();
    expect(w.findAll(".chip")).toHaveLength(1);

    await w.find("textarea").setValue("看看这个文件");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    // 文本与附件都留着（失败不是附件的错，用户不必重写）
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("看看这个文件");
    expect(w.findAll(".chip")).toHaveLength(1);
    const box = w.find('[data-test="attach-reject"]');
    expect(box.exists()).toBe(true);
    expect(box.text()).toContain("这个附件已经属于别的一轮了");
    expect(box.text()).toContain("会议纪要.txt");

    // 「移除这些附件后发送」：移除后立刻重发（第二次成功）
    await w.find('[data-test="attach-reject-remove"]').trigger("click");
    await flushPromises();
    expect(w.findAll(".chip")).toHaveLength(0);
    expect(w.find('[data-test="attach-reject"]').exists()).toBe(false);
    expect(mocks.sendTurn).toHaveBeenCalledTimes(2);
    const secondCall = mocks.sendTurn.mock.calls[1] as unknown[];
    expect(secondCall[2], "移除后重发不能再带上被拒的附件").toEqual([]);
  });

  it("用户手动移除被拒的附件：拒绝框里对应的那一行一起收掉", async () => {
    mocks.sendTurn.mockRejectedValueOnce(structured409());
    const { w } = await mountComposer();

    await w.find("textarea").setValue("看看这个文件");
    await w.find(".send-btn").trigger("click");
    await flushPromises();
    expect(w.find('[data-test="attach-reject"]').exists()).toBe(true);

    // chip 上的「×」：手动移除
    await w.find(".chip .act.remove").trigger("click");
    await flushPromises();
    expect(w.findAll(".chip")).toHaveLength(0);
    expect(w.find('[data-test="attach-reject"]').exists()).toBe(false);
  });

  it("普通失败：不伪造附件原因，也不出现「移除附件」入口", async () => {
    mocks.sendTurn.mockRejectedValueOnce(new ApiError(500, "/api/turns", "boom"));
    const { w } = await mountComposer();

    await w.find("textarea").setValue("看看这个文件");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    expect(w.find('[data-test="attach-reject"]').exists()).toBe(false);
    expect(w.findAll(".chip")).toHaveLength(1);
  });
});
