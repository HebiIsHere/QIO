/**
 * F09 反例（成员 D · 待发附件重传替换）：替换必须绑定 (旧 ID, 新 ID, 话题)，且只有新附件
 * 达到 ready 并成功加入待发列表才删除旧条目。
 *
 * 基线行为（红）：Composer.finishReupload 在 addPaths/addFiles 之后**无条件** DELETE 旧附件；
 *   新上传失败 / 准备中 / 选择取消也会删掉旧失败附件，列表变空，同时出现「上传失败」与
 *   「已替换」两个提示。路径登记后准备未完成也会提前删旧。
 * 期望（绿）：新上传失败 / 准备失败 / 准备中 / 未入列 / 删除失败 → 旧条目保留，如实说明；
 *   只有指定新附件 ready 且入列 → 删除**指定旧 ID**；删除失败不报告无条件成功。
 *
 * 模拟边界：真实 Composer 组件 + 真实 store；附件 service 为受控 mock（边界=HTTP 层）。
 */
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
  getAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  pickLocalPath: vi.fn(),
  onPathDrop: vi.fn(),
  openAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
  restorePendingAttachments: vi.fn(),
  savePendingAttachments: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: { sendTurn: mocks.sendTurn, getSessionContext: mocks.getSessionContext, cancelTurn: mocks.cancelTurn },
}));

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    prepareAttachment: mocks.prepareAttachment,
    uploadAttachment: mocks.uploadAttachment,
    removeAttachment: mocks.removeAttachment,
    retryAttachment: mocks.retryAttachment,
    getAttachment: mocks.getAttachment,
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
import AttachmentChip from "../AttachmentChip.vue";
import type { AttachmentRef } from "../../services/attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_old",
    name: "旧的失败.txt",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "failed",
    error: "QIO 无法从原地址恢复",
    actions: ["reupload"],
    ...over,
  };
}

function freshPinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

async function mountComposerWithPending(items: AttachmentRef[]) {
  mocks.restorePendingAttachments.mockResolvedValue({ items, dropped: [], unconfirmed: [] } as never);
  const pinia = freshPinia();
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();
  return { w, pinia };
}

async function chooseFiles(w: VueWrapper, ...names: string[]) {
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: names.map((name) => new File(["x"], name)),
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}

function chipIds(w: VueWrapper): string[] {
  return w.findAll(".chip").map((c) => c.attributes("data-id") ?? c.find(".name").text());
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item as never);
  mocks.openAttachment.mockResolvedValue({ action: "view", note: "" } as never);
  mocks.savePendingAttachments.mockImplementation(() => undefined);
  mocks.getSessionContext.mockResolvedValue({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] } as never);
});

describe("F09 重传替换：只有新附件 ready 且入列才删旧", () => {
  it("正常替换：新附件 ready → 删除指定旧 ID，旧条目消失、新条目在列", async () => {
    mocks.uploadAttachment.mockResolvedValueOnce(ref({ id: "att_new", name: "新的.txt", state: "ready", actions: [] }) as never);
    const { w } = await mountComposerWithPending([ref()]);
    expect(chipIds(w)).toContain("旧的失败.txt");

    await w.find(".chip .act.reupload").trigger("click");
    await chooseFiles(w, "新的.txt");

    expect(mocks.removeAttachment).toHaveBeenCalledTimes(1);
    expect(mocks.removeAttachment).toHaveBeenCalledWith("att_old");
    const names = w.findAll(".chip .name").map((n) => n.text());
    expect(names).toContain("新的.txt");
    expect(names).not.toContain("旧的失败.txt");
    w.unmount();
  });

  it("新上传被拒绝：不删旧附件，旧条目保留，如实提示没被替换", async () => {
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("网络拒绝") as never);
    const { w } = await mountComposerWithPending([ref()]);

    await w.find(".chip .act.reupload").trigger("click");
    await chooseFiles(w, "新的.txt");

    expect(mocks.removeAttachment).not.toHaveBeenCalled();
    expect(w.findAll(".chip .name").map((n) => n.text())).toContain("旧的失败.txt");
    expect(w.find(".attach-error").text()).toContain("网络拒绝");
    expect(w.find(".attach-note").text()).toContain("没有被替换");
    w.unmount();
  });

  it("新附件准备失败（prepared → failed）：不删旧附件", async () => {
    mocks.uploadAttachment.mockResolvedValueOnce(ref({ id: "att_new", name: "新的.txt", state: "prepared", actions: undefined }) as never);
    mocks.waitUntilSettled.mockResolvedValueOnce(ref({ id: "att_new", name: "新的.txt", state: "failed", error: "复制失败" }) as never);
    const { w } = await mountComposerWithPending([ref()]);

    await w.find(".chip .act.reupload").trigger("click");
    await chooseFiles(w, "新的.txt");

    expect(mocks.removeAttachment).not.toHaveBeenCalled();
    expect(w.findAll(".chip .name").map((n) => n.text())).toContain("旧的失败.txt");
    expect(w.find(".attach-note").text()).toContain("还没就绪");
    w.unmount();
  });

  it("新附件还在准备中（未到最终态）：不提前删旧附件", async () => {
    mocks.uploadAttachment.mockResolvedValueOnce(ref({ id: "att_new", name: "新的.txt", state: "prepared", actions: undefined }) as never);
    mocks.waitUntilSettled.mockResolvedValueOnce(ref({ id: "att_new", name: "新的.txt", state: "prepared" }) as never);
    const { w } = await mountComposerWithPending([ref()]);

    await w.find(".chip .act.reupload").trigger("click");
    await chooseFiles(w, "新的.txt");

    expect(mocks.removeAttachment).not.toHaveBeenCalled();
    expect(w.findAll(".chip .name").map((n) => n.text())).toContain("旧的失败.txt");
    w.unmount();
  });

  it("删除旧附件失败：不报告无条件成功，旧条目保留可恢复", async () => {
    mocks.uploadAttachment.mockResolvedValueOnce(ref({ id: "att_new", name: "新的.txt", state: "ready", actions: [] }) as never);
    mocks.removeAttachment.mockRejectedValueOnce(new Error("后端没响应") as never);
    const { w } = await mountComposerWithPending([ref()]);

    await w.find(".chip .act.reupload").trigger("click");
    await chooseFiles(w, "新的.txt");

    expect(mocks.removeAttachment).toHaveBeenCalledWith("att_old");
    expect(w.findAll(".chip .name").map((n) => n.text())).toContain("旧的失败.txt");
    const note = w.find(".attach-note").text();
    expect(note).toContain("没有删掉");
    expect(note).not.toContain("已替换");
    w.unmount();
  });

  it("多文件选择：替换项明确为第一个文件；第一个失败不删旧，第二个正常加入", async () => {
    mocks.uploadAttachment
      .mockRejectedValueOnce(new Error("第一个失败") as never)
      .mockResolvedValueOnce(ref({ id: "att_second", name: "第二个.txt", state: "ready", actions: [] }) as never);
    const { w } = await mountComposerWithPending([ref()]);

    await w.find(".chip .act.reupload").trigger("click");
    await chooseFiles(w, "第一个.txt", "第二个.txt");

    expect(mocks.removeAttachment).not.toHaveBeenCalled();
    const names = w.findAll(".chip .name").map((n) => n.text());
    expect(names).toContain("旧的失败.txt");
    expect(names).toContain("第二个.txt");
    w.unmount();
  });

  it("双次重传竞态：两次替换都在途，旧 ID 只 DELETE 一次", async () => {
    let releaseFirst!: (value: AttachmentRef) => void;
    const firstUpload = new Promise<AttachmentRef>((resolve) => {
      releaseFirst = resolve;
    });
    let call = 0;
    mocks.uploadAttachment.mockImplementation((() => {
      call += 1;
      if (call === 1) return firstUpload as never;
      return Promise.resolve(ref({ id: "att_new2", name: "第二次.txt", state: "ready", actions: [] })) as never;
    }) as never);
    const { w } = await mountComposerWithPending([ref()]);

    // 第一次替换：上传在途（未完成）
    w.findComponent(AttachmentChip).vm.$emit("reupload", "att_old");
    await chooseFiles(w, "第一次.txt");
    // 第二次替换：同一旧 ID，仍能看到旧条目
    w.findComponent(AttachmentChip).vm.$emit("reupload", "att_old");
    await chooseFiles(w, "第二次.txt");

    // 第二次先 ready 并提交替换
    expect(mocks.removeAttachment.mock.calls.filter((c) => c[0] === "att_old")).toHaveLength(1);
    // 第一次迟到 ready：旧 ID 已被替换，不得再 DELETE 一次
    releaseFirst(ref({ id: "att_new1", name: "第一次.txt", state: "ready", actions: [] }));
    await flushPromises();
    expect(mocks.removeAttachment.mock.calls.filter((c) => c[0] === "att_old")).toHaveLength(1);
    w.unmount();
  });

  it("选择取消（没有选中文件）：不调用任何删除", async () => {
    const { w } = await mountComposerWithPending([ref()]);
    await w.find(".chip .act.reupload").trigger("click");
    await nextTick();
    // 浏览器里取消不会触发 change：onFileInput 根本不会被调用
    expect(mocks.removeAttachment).not.toHaveBeenCalled();
    expect(mocks.uploadAttachment).not.toHaveBeenCalled();
    w.unmount();
  });
});
