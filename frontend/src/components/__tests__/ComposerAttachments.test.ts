import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  pickLocalPath: vi.fn(),
  onPathDrop: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    cancelTurn: mocks.cancelTurn,
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
    isDesktopShell: () => false,
  };
});

import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";
import type { AttachmentRef } from "../../services/attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "会议纪要.txt",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "prepared",
    error: null,
    ...over,
  };
}

function freshPinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

async function mountComposer(pinia = freshPinia()) {
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await nextTick();
  return { w, pinia };
}

/** 探针必须在 mount **之前**替换 store.send：Composer 在 setup 时取一次 send。 */
function stubSend(result = true) {
  const session = useSessionStore();
  const sendMock = vi.fn(async () => result);
  session.send = sendMock as unknown as typeof session.send;
  return { session, sendMock };
}

/** 浏览器回退路径：把文件塞进 input[type=file] 再触发 change。 */
async function chooseFiles(w: VueWrapper, ...names: string[]) {
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: names.map((name) => new File(["x"], name)),
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}

/** 真实路径入口：粘贴路径后回车。 */
async function pastePath(w: VueWrapper, path: string) {
  await w.findAll(".attach-btn")[1].trigger("click");
  await w.find(".path-input").setValue(path);
  await w.find(".path-input").trigger("keydown.enter");
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item);
  // 默认：登记返回「按路径取名」的准备中引用
  mocks.prepareAttachment.mockImplementation(async (path: string) =>
    ref({ name: String(path).split(/[\\/]/).pop() || "a.txt", state: "prepared" }),
  );
  mocks.uploadAttachment.mockImplementation(async (file: File) => ref({ name: file.name, state: "ready" }));
});

describe("Composer 附件入口", () => {
  it("默认没有附件时不显示附件区，但入口按钮在", async () => {
    const { w } = await mountComposer();
    expect(w.find(".attach-area").exists()).toBe(false);
    expect(w.find(".attach-btn").exists()).toBe(true);
    w.unmount();
  });

  it("浏览器回退：选择文件走字节上传，chip 出现并显示保存方式", async () => {
    const { w } = await mountComposer();
    await chooseFiles(w, "照片.png");

    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(1);
    const chip = w.find(".chip");
    expect(chip.exists()).toBe(true);
    expect(chip.text()).toContain("照片.png");
    expect(chip.text()).toContain("已保存副本");
    w.unmount();
  });

  it("路径输入：粘贴本地路径走真实路径登记（不是字节上传）", async () => {
    mocks.waitUntilSettled.mockResolvedValue(ref({ name: "报告.docx", state: "ready" }));
    const { w } = await mountComposer();
    await pastePath(w, "D:\\tmp\\报告.docx");

    expect(mocks.prepareAttachment).toHaveBeenCalledWith("D:\\tmp\\报告.docx", { topicId: null });
    expect(mocks.uploadAttachment).not.toHaveBeenCalled();
    expect(w.find(".chip").text()).toContain("报告.docx");
    w.unmount();
  });

  it("准备中：不得当作发送成功，文本与附件都留着", async () => {
    const pinia = freshPinia();
    const { sendMock } = stubSend(true);
    const { w } = await mountComposer(pinia);
    mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item); // 一直准备中
    await pastePath(w, "D:\\tmp\\还在复制.bin");

    await w.find("textarea").setValue("这条消息带着还在准备的附件");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    expect(sendMock).not.toHaveBeenCalled();
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("这条消息带着还在准备的附件");
    expect(w.find(".attach-error").text()).toContain("还在准备中");
    expect(w.find(".chip").text()).toContain("准备中");
    const session = useSessionStore();
    expect(session.messages.some((m) => m.content.includes("还在准备"))).toBe(false);
    w.unmount();
  });

  it("附件失败：不得清空已输入文本，并给出重试入口", async () => {
    const pinia = freshPinia();
    const { sendMock } = stubSend(true);
    const { w } = await mountComposer(pinia);
    mocks.waitUntilSettled.mockResolvedValue(
      ref({ name: "坏的.bin", state: "failed", error: "磁盘空间不足：无法保存副本" }),
    );
    await pastePath(w, "D:\\tmp\\坏的.bin");
    expect(w.find(".chip").text()).toContain("准备失败");
    expect(w.find(".chip").attributes("title")).toContain("磁盘空间不足");

    await w.find("textarea").setValue("别把我的字弄丢");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    expect(sendMock).not.toHaveBeenCalled();
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("别把我的字弄丢");
    expect(w.find(".attach-error").text()).toContain("没有准备好");

    // 重试成功后同一行变回就绪，可以正常发送
    mocks.retryAttachment.mockResolvedValue(ref({ name: "坏的.bin", state: "prepared" }));
    mocks.waitUntilSettled.mockResolvedValue(ref({ name: "坏的.bin", state: "ready" }));
    await w.find(".chip .act").trigger("click");
    await flushPromises();
    expect(mocks.retryAttachment).toHaveBeenCalledWith("att_1");
    expect(w.find(".chip").text()).toContain("已保存副本");
    w.unmount();
  });

  it("发送成功：把 ids 与 refs 一起交给 session.send，并清掉 chip", async () => {
    const pinia = freshPinia();
    const { sendMock } = stubSend(true);
    const { w } = await mountComposer(pinia);
    await chooseFiles(w, "a.txt");

    await w.find("textarea").setValue("带上附件");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    expect(sendMock).toHaveBeenCalledTimes(1);
    const [text, ids, refs] = sendMock.mock.calls[0] as unknown as [string, string[], AttachmentRef[]];
    expect(text).toBe("带上附件");
    expect(ids).toEqual(["att_1"]);
    expect(refs[0].id).toBe("att_1");
    expect(refs[0].display).toBe("已保存副本");
    expect(w.find(".attach-area").exists()).toBe(false);
    w.unmount();
  });

  it("发送被拒（网络失败）：文本与附件都留着，用户可以直接重试", async () => {
    const pinia = freshPinia();
    const { sendMock } = stubSend(false);
    const { w } = await mountComposer(pinia);
    await chooseFiles(w, "a.txt");

    await w.find("textarea").setValue("这条会失败");
    await w.find(".send-btn").trigger("click");
    await flushPromises();

    expect(sendMock).toHaveBeenCalledTimes(1);
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("这条会失败");
    expect(w.find(".chip").exists()).toBe(true); // 附件没有被顺手清掉
    w.unmount();
  });

  it("移除附件调用 DELETE；失败时 chip 回位（不制造假象）", async () => {
    const { w } = await mountComposer();
    await chooseFiles(w, "a.txt");

    await w.find(".chip .act.remove").trigger("click");
    await flushPromises();
    expect(mocks.removeAttachment).toHaveBeenCalledWith("att_1");
    expect(w.find(".chip").exists()).toBe(false);

    await chooseFiles(w, "a.txt");
    mocks.removeAttachment.mockRejectedValueOnce(new Error("后端没响应"));
    await w.find(".chip .act.remove").trigger("click");
    await flushPromises();
    expect(w.find(".chip").exists()).toBe(true);
    expect(w.find(".attach-error").text()).toContain("移除附件失败");
    w.unmount();
  });

  it("引用型附件：chip 写明「引用本地文件」，悬停说明只是位置", async () => {
    mocks.uploadAttachment.mockResolvedValue(
      ref({ state: "ready", kind: "reference", display: "引用本地文件", name: "大视频.mp4", sizeBytes: 500_000_000 }),
    );
    const { w } = await mountComposer();
    await chooseFiles(w, "大视频.mp4");

    const chip = w.find(".chip");
    expect(chip.text()).toContain("引用本地文件");
    expect(chip.text()).toContain("500.0 MB");
    expect(chip.attributes("title")).toContain("不保证内容仍然存在");
    w.unmount();
  });

  it("登记失败：说清原因且不产生假 chip（原文件不会被动）", async () => {
    mocks.prepareAttachment.mockRejectedValueOnce(new Error("找不到这个文件：C:\\fakepath\\x.txt"));
    const { w } = await mountComposer();
    await pastePath(w, "C:\\fakepath\\x.txt");

    expect(w.find(".chip").exists()).toBe(false);
    expect(w.find(".attach-error").text()).toContain("没有登记成功");
    expect(w.find(".attach-error").text()).toContain("找不到这个文件");
    w.unmount();
  });
});
