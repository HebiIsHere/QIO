/**
 * [P3 互动悬浮聊天] ChatDock 的附件入口（契约 §4.1）。
 *
 * 这一条硬要求是「悬浮聊天也能发附件，而且**只发当前消息 + 明确选定的附件**」：
 *
 *   1. 选择本地文件（uploadAttachment）/ 粘贴路径（prepareAttachment）两条入口都真的接上，
 *      并复用 attachmentOps 的**归属**（beginAttachmentOp / registerAttachmentIdentity）与
 *      **tombstone**（markAttachmentRemoved / isAttachmentDead）规则；
 *   2. 发送载荷里**只有**当前消息与明确选定的附件 —— 不自动带板面卡片、未提交改动、
 *      未许可文字卡、预览内容（断言 session.send 的实参形状，而不是「某函数被调用过」）；
 *   3. 附件准备失败：保留用户输入、给出可操作的重试入口，并且**不重复上传**；
 *   4. 明确移除的附件**不复活**（晚到的准备结果不许把 tombstone 覆盖掉）。
 *
 * 模拟边界：附件服务（网络/系统对话框）与 api。session 是真实 store（判据是真实行为）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_1", status: "accepted", topic_id: "A" })),
  getSessionContext: vi.fn(async () => ({ topic_id: "A" })),
  pickBrowserFile: vi.fn(),
  pickLocalPath: vi.fn(),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  openAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
}));

vi.mock("../../../services/api", () => ({
  api: {
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    stopTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
    cancelContinuation: vi.fn(async () => ({ ok: true })),
    cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    saveDrafts: vi.fn(async () => ({ ok: true })),
    saveBoardState: vi.fn(async () => ({ ok: true })),
    fetchBoardState: vi.fn(async () => ({ state: { cards: [], groups: [], links: [] } })),
    fetchVisibleRange: vi.fn(async () => ({ range: {} })),
    previewMaterialImpact: vi.fn(async () => ({})),
    submitBoard: vi.fn(async () => ({})),
    fetchIntents: vi.fn(async () => ({ intents: [] })),
    approveIntent: vi.fn(async () => ({})),
    rejectIntent: vi.fn(async () => ({})),
    batchDecide: vi.fn(async () => ({})),
    createDemoIntents: vi.fn(async () => ({})),
    advanceIntent: vi.fn(async () => ({})),
    updateIntentPreview: vi.fn(async () => ({})),
    fetchHistoryPage: vi.fn(async () => ({ messages: [] })),
    fetchMessages: vi.fn(async () => ({ messages: [] })),
  },
}));

vi.mock("../../../services/interactive", () => ({
  saveDrafts: vi.fn(async () => ({ ok: true })),
  fetchBoardState: vi.fn(async () => ({ state: { cards: [], groups: [], links: [] } })),
  saveBoardState: vi.fn(async () => ({ ok: true })),
  fetchVisibleRange: vi.fn(async () => ({ range: {} })),
  previewMaterialImpact: vi.fn(async () => ({})),
  submitBoard: vi.fn(async () => ({})),
  fetchIntents: vi.fn(async () => ({ intents: [] })),
  approveIntent: vi.fn(async () => ({})),
  rejectIntent: vi.fn(async () => ({})),
  batchDecide: vi.fn(async () => ({})),
  createDemoIntents: vi.fn(async () => ({})),
  advanceIntent: vi.fn(async () => ({})),
  updateIntentPreview: vi.fn(async () => ({})),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    pickBrowserFile: mocks.pickBrowserFile,
    pickLocalPath: mocks.pickLocalPath,
    prepareAttachment: mocks.prepareAttachment,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    removeAttachment: mocks.removeAttachment,
    retryAttachment: mocks.retryAttachment,
    openAttachment: mocks.openAttachment,
    relocateAttachment: mocks.relocateAttachment,
    isDesktopShell: () => false,
  };
});

import ChatDock from "../ChatDock.vue";
import { useSessionStore } from "../../../stores/session";
import { useInteractiveStore } from "../../../stores/interactive";
import type { AttachmentRef } from "../../../services/attachments";

const wrappers: Array<{ unmount: () => void }> = [];

function attachment(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "会议录音.mp3",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

async function mountDock() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = "A";
  useInteractiveStore().chatOpen = true;
  // 载荷形状要能逐字段断言（与 Composer 同一条入口：文字 + 附件 id + 附件引用）
  const sendMock = vi.fn(
    async (_text: string, _ids?: string[], _attachments?: AttachmentRef[]) => true,
  );
  session.send = sendMock as unknown as typeof session.send;
  const w = mount(ChatDock, { global: { plugins: [pinia] }, attachTo: document.body });
  wrappers.push(w);
  await flushPromises();
  return { w, session, sendMock };
}

/** 浏览器回退路径：把文件塞进 input[type=file] 再触发 change。 */
async function chooseFiles(w: ReturnType<typeof mount>, ...names: string[]) {
  const input = w.find('[data-im="chat-attach-file"]');
  Object.defineProperty(input.element, "files", {
    value: names.map((name) => new File(["x"], name)),
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}

/** 真实路径入口：桌面端直接弹选择器；浏览器里（或用户取消后）退回手输。 */
async function pastePath(w: ReturnType<typeof mount>, path: string) {
  if (!w.find('[data-im="chat-attach-path"]').exists()) {
    await w.find('[data-im="chat-attach-path-btn"]').trigger("click");
    await flushPromises();
  }
  await w.find('[data-im="chat-attach-path"]').setValue(path);
  await w.find('[data-im="chat-attach-path-add"]').trigger("click");
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.sendTurn.mockResolvedValue({ ok: true, accepted: true, turn_id: "turn_1", status: "accepted", topic_id: "A" } as never);
  mocks.getSessionContext.mockResolvedValue({ topic_id: "A" } as never);
  mocks.uploadAttachment.mockResolvedValue(attachment());
  mocks.prepareAttachment.mockResolvedValue(attachment({ id: "att_path", name: "路径来的.pdf" }));
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item);
  mocks.pickBrowserFile.mockResolvedValue(null);
  mocks.pickLocalPath.mockResolvedValue(null);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
});

afterEach(() => {
  for (const w of wrappers.splice(0)) w.unmount();
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// 1. 两条入口都真的接上
// ---------------------------------------------------------------------------

describe("ChatDock 附件入口：选择本地文件 / 粘贴路径", () => {
  it("选本地文件：走 uploadAttachment + 归属登记，附件事实用真实状态显示", async () => {
    const { w } = await mountDock();
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_file", name: "录音.mp3" }));
    await chooseFiles(w, "录音.mp3");

    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(1);
    const first = mocks.uploadAttachment.mock.calls[0][0] as File;
    expect(first.name).toBe("录音.mp3");
    const chip = w.find('[data-im="chat-attachment"]');
    expect(chip.exists(), "附件没有出现在悬浮聊天里").toBe(true);
    expect(chip.text()).toContain("录音.mp3");
  });

  it("粘贴路径：走 prepareAttachment（不是上传），并且能移除", async () => {
    const { w } = await mountDock();
    await pastePath(w, "D:/资料/季度报告.pdf");

    expect(mocks.prepareAttachment).toHaveBeenCalledTimes(1);
    expect(mocks.prepareAttachment.mock.calls[0][0]).toBe("D:/资料/季度报告.pdf");
    expect(mocks.uploadAttachment).not.toHaveBeenCalled();
    const chip = w.find('[data-im="chat-attachment"]');
    expect(chip.exists()).toBe(true);
    expect(chip.text()).toContain("路径来的.pdf");

    await w.find('[data-im="chat-attachment-remove"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-im="chat-attachment"]').exists(), "移除之后附件还在").toBe(false);
  });

  it("用户取消选择（拿不到文件/路径）：什么都不加，也不报一个假失败", async () => {
    const { w } = await mountDock();
    mocks.pickBrowserFile.mockResolvedValue(null);
    await w.find('[data-im="chat-attach-file-btn"]').trigger("click");
    await flushPromises();
    mocks.pickLocalPath.mockResolvedValue(null);
    await w.find('[data-im="chat-attach-path-btn"]').trigger("click");
    await flushPromises();

    expect(w.find('[data-im="chat-attachment"]').exists()).toBe(false);
    expect(w.find('[data-im="chat-attach-error"]').exists()).toBe(false);
    expect(mocks.uploadAttachment).not.toHaveBeenCalled();
    expect(mocks.prepareAttachment).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 2. 发送载荷：只有当前消息 + 明确选定的附件
// ---------------------------------------------------------------------------

describe("ChatDock 发送载荷：只有当前消息与明确选定的附件", () => {
  it("不带附件：仍然是纯文字发送（一个附件参数都不传）", async () => {
    const { w, sendMock } = await mountDock();
    await w.find('[data-im="chat-input"]').setValue("只发这句话");
    await nextTick();
    await w.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();

    expect(sendMock).toHaveBeenCalledTimes(1);
    const [text, ids, attachments] = sendMock.mock.calls[0];
    expect(text).toBe("只发这句话");
    expect(ids ?? []).toEqual([]);
    expect(attachments ?? []).toEqual([]);
  });

  it("带附件：载荷里恰好是当前消息 + 明确选定的那一个附件，板面/未提交改动/未许可文字卡/预览内容一个字都不带", async () => {
    const { w, session, sendMock } = await mountDock();
    const interactive = useInteractiveStore();
    // 诱饵：板面卡片、未许可文字卡、预览内容都摆在 store 里
    interactive.board = {
      cards: [
        { id: "card_secret", kind: "text", x: 0, y: 0, w: 100, h: 60, content: "未许可的文字卡正文", checked: false, hidden: false, meta: {} },
        { id: "card_preview", kind: "file", x: 0, y: 0, w: 100, h: 60, content: "", checked: true, hidden: false, meta: { name: "预览里的报告.docx", file: { attachmentId: "att_preview", name: "预览里的报告.docx" } } },
      ],
      groups: [],
      links: [],
    } as never;
    session.draft = "当前这条消息";
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_chosen", name: "选中的录音.mp3" }));
    await chooseFiles(w, "选中的录音.mp3");

    await w.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();

    expect(sendMock).toHaveBeenCalledTimes(1);
    const [text, ids, refs] = sendMock.mock.calls[0];
    expect(text).toBe("当前这条消息");
    expect(ids).toEqual(["att_chosen"]);
    expect((refs ?? []).map((item) => item.id)).toEqual(["att_chosen"]);
    const payload = JSON.stringify(sendMock.mock.calls[0]);
    for (const poison of ["card_secret", "未许可的文字卡正文", "att_preview", "预览里的报告.docx"]) {
      expect(payload, "发送载荷里混进了不该带的东西：" + poison).not.toContain(poison);
    }
  });

  it("用户移除后：不带上；晚到的准备结果也不复活它", async () => {
    const { w, sendMock } = await mountDock();
    let release: (value: AttachmentRef) => void = () => undefined;
    mocks.waitUntilSettled.mockImplementation(
      () => new Promise<AttachmentRef>((resolve) => {
        release = resolve;
      }),
    );
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_late", name: "晚到的.pdf", state: "prepared" }));
    await chooseFiles(w, "晚到的.pdf");
    await flushPromises();
    expect(w.find('[data-im="chat-attachment"]').exists(), "附件没有先出现在待发列表里").toBe(true);
    await w.find('[data-im="chat-attachment-remove"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-im="chat-attachment"]').exists()).toBe(false);

    // 晚到的准备结果到了：不许把已移除的附件放回来
    release(attachment({ id: "att_late", name: "晚到的.pdf", state: "ready" }));
    await flushPromises();
    expect(w.find('[data-im="chat-attachment"]').exists(), "已移除的附件被晚到结果复活了").toBe(false);

    await w.find('[data-im="chat-input"]').setValue("这条只能发文字");
    await nextTick();
    await w.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    const [, ids] = sendMock.mock.calls[0];
    expect(ids ?? []).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 3. 准备失败：保留输入 + 可操作的重试，不重复上传
// ---------------------------------------------------------------------------

describe("ChatDock 附件失败：保留输入、可重试、不重复上传", () => {
  it("准备失败：输入框原文保留，显示真实原因与重试入口；重试按同一个文件再走一次", async () => {
    const { w } = await mountDock();
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("磁盘写满了"));
    await chooseFiles(w, "大文件.bin");

    expect(w.find('[data-im="chat-input"]').exists()).toBe(true);
    const error = w.find('[data-im="chat-attach-error"]');
    expect(error.exists(), "失败时没有说明原因").toBe(true);
    expect(error.text()).toContain("磁盘写满了");
    expect(w.find('[data-im="chat-attach-retry"]').exists()).toBe(true);

    // 用户输入的原话还在
    await w.find('[data-im="chat-input"]').setValue("这条不能因为附件失败就丢掉");
    await nextTick();
    expect((w.find('[data-im="chat-input"]').element as HTMLTextAreaElement).value).toBe("这条不能因为附件失败就丢掉");

    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(1);
    mocks.uploadAttachment.mockResolvedValueOnce(attachment({ id: "att_retry", name: "大文件.bin" }));
    await w.find('[data-im="chat-attach-retry"]').trigger("click");
    await flushPromises();
    expect(mocks.uploadAttachment, "重试没有真的再试一次").toHaveBeenCalledTimes(2);
    expect(w.find('[data-im="chat-attachment"]').text()).toContain("大文件.bin");
    // 输入框里的原话没被附件操作清掉
    expect((w.find('[data-im="chat-input"]').element as HTMLTextAreaElement).value).toBe("这条不能因为附件失败就丢掉");
  });

  it("附件没准备好：发送入口禁用 + 说清卡在哪一个，文字留在输入框", async () => {
    const { w, sendMock } = await mountDock();
    // 附件落定成 failed（真实失败态）：发送入口必须挡住它，并说清卡在哪一个
    mocks.uploadAttachment.mockImplementation(async (file: File) =>
      attachment({ id: "att_bad", name: file.name, state: "prepared" }),
    );
    mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) =>
      attachment({ id: item.id, name: item.name, state: "failed", error: "磁盘写满了" }),
    );
    await chooseFiles(w, "还没好.pdf");
    await w.find('[data-im="chat-input"]').setValue("带附件的一条");
    await nextTick();

    const sendBtn = w.find('[data-im="chat-send"]');
    expect((sendBtn.element as HTMLButtonElement).disabled, "附件没准备好，发送按钮却是可点的").toBe(true);
    const error = w.find('[data-im="chat-attach-error"]');
    expect(error.exists(), "附件没准备好却没有说清原因").toBe(true);
    expect(error.text()).toContain("还没好.pdf");
    // 就算硬点也不会发出去
    await sendBtn.trigger("click");
    await flushPromises();
    expect(sendMock, "附件没准备好却发出去了").not.toHaveBeenCalled();
    expect((w.find('[data-im="chat-input"]').element as HTMLTextAreaElement).value).toBe("带附件的一条");
  });

  it("发送成功后附件出列：不会跟着下一条消息再发一次", async () => {
    const { w, sendMock } = await mountDock();
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_once", name: "一次性.pdf" }));
    await chooseFiles(w, "一次性.pdf");
    await w.find('[data-im="chat-input"]').setValue("第一条");
    await nextTick();
    await w.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-im="chat-attachment"]').exists(), "发送成功后附件没有出列").toBe(false);

    await w.find('[data-im="chat-input"]').setValue("第二条");
    await nextTick();
    await w.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    const second = sendMock.mock.calls[1];
    expect(second[0]).toBe("第二条");
    expect(second[1] ?? [], "上一条的附件被重复发送了").toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 4. 附件默认「数量 + 简述 + 展开入口」：不设一排固定高度的小滚动区
// ---------------------------------------------------------------------------

describe("ChatDock 附件条：默认简述 + 展开入口", () => {
  it("超过简述条数时其余收起，展开后全部可见且仍可移除", async () => {
    const { w } = await mountDock();
    mocks.uploadAttachment.mockImplementation(async (file: File) =>
      attachment({ id: "att_" + file.name, name: file.name }),
    );
    await chooseFiles(w, "资料一.pdf", "资料二.pdf", "资料三.pdf");

    expect(w.find('[data-im="chat-attach-total"]').text(), "没有给到附件数量").toContain("3 个附件");
    expect(w.findAll('[data-im="chat-attachment"]').length, "默认没有收敛成简述").toBe(2);

    const more = w.find('[data-im="chat-attachments-more"]');
    expect(more.exists(), "多出来的附件没有展开入口").toBe(true);
    expect(more.attributes("aria-expanded")).toBe("false");
    expect(more.text()).toContain("展开其余 1 个");

    await more.trigger("click");
    await flushPromises();
    expect(w.findAll('[data-im="chat-attachment"]').length, "展开后没有看到全部附件").toBe(3);
    expect(w.find('[data-im="chat-attachments-more"]').attributes("aria-expanded")).toBe("true");

    // 展开不是「多开一层滚动」：移除入口照旧可达，移除后条目数与数量都对得上
    await w.findAll('[data-im="chat-attachment-remove"]')[2].trigger("click");
    await flushPromises();
    expect(w.findAll('[data-im="chat-attachment"]').length).toBe(2);
    expect(w.find('[data-im="chat-attach-total"]').text()).toContain("2 个附件");
  });
});
