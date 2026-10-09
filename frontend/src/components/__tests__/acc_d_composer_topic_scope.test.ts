/**
 * F08 反例（成员 D · 附件异步结果跨话题写入）。
 *
 * 基线行为（红）：restorePending / addFiles / addPaths / track 都用共享的 pending ref，
 *   A 的恢复或上传在切到 B 之后返回，会直接覆盖 B 的附件与持久化（服务登记 topic=A，前端写到 B）。
 * 期望（绿）：每个操作捕获所属话题与版本；切走后只落发起话题的持久化，绝不更新当前 UI；
 *   旧恢复不覆盖新恢复/用户编辑；卸载后完成只落持久化；旧恢复与新上传竞争时不丢用户新增。
 *
 * 模拟边界：真实 Composer + 真实 store + 真实 localStorage 持久化；
 *   附件网络边界为受控 mock（restorePendingAttachments / uploadAttachment 可注入延迟）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

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
  pickBrowserFile: vi.fn(),
  onPathDrop: vi.fn(),
  openAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
  restorePendingAttachments: vi.fn(),
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
    pickBrowserFile: mocks.pickBrowserFile,
    onPathDrop: mocks.onPathDrop,
    openAttachment: mocks.openAttachment,
    relocateAttachment: mocks.relocateAttachment,
    restorePendingAttachments: mocks.restorePendingAttachments,
    isDesktopShell: () => false,
  };
});

import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";
import { loadPendingAttachments, type AttachmentRef } from "../../services/attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "文件.txt",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function mountComposer(topicId: string | null) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = topicId;
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();
  return { w, session, pinia };
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

function chipNames(w: VueWrapper): string[] {
  return w.findAll(".composer .chip .name").map((n) => n.text());
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [], unconfirmed: [] } as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.pickBrowserFile.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item as never);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.uploadAttachment.mockImplementation(async (file: File) => ref({ id: "att_" + file.name, name: file.name, state: "ready" }));
  mocks.getSessionContext.mockResolvedValue({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] } as never);
});

describe("F08 附件异步结果按话题归属", () => {
  it("A 上传期间切 B：A 返回的附件只落 A 的持久化，不进 B 的 UI", async () => {
    mocks.restorePendingAttachments.mockImplementation(async (topicId: string | null) =>
      topicId === "B"
        ? { items: [ref({ id: "att_B", name: "B的.txt" })], dropped: [], unconfirmed: [] }
        : { items: [], dropped: [], unconfirmed: [] },
    );
    const upload = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValue(upload.promise as never);
    const { w, session } = await mountComposer("A");

    await chooseFiles(w, "a.txt"); // A 的上传在途
    expect(chipNames(w)).toEqual([]);

    session.currentTopicId = "B";
    await flushPromises();
    expect(chipNames(w)).toContain("B的.txt");

    upload.resolve(ref({ id: "att_A", name: "a.txt", state: "ready" }));
    await flushPromises();

    // A 的结果落 A 的持久化（服务登记 topic=A）
    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_A"]);
    // 当前话题 B 的 UI 与持久化不被污染
    expect(chipNames(w)).not.toContain("a.txt");
    expect(chipNames(w)).toContain("B的.txt");
    w.unmount();
  });

  it("A 恢复在途时切 B：A 的迟到恢复不覆盖 B 的附件与持久化", async () => {
    const slowA = deferred<{ items: AttachmentRef[]; dropped: string[]; unconfirmed: AttachmentRef[] }>();
    let call = 0;
    mocks.restorePendingAttachments.mockImplementation(async (topicId: string | null) => {
      call += 1;
      if (call === 1) return slowA.promise;
      return topicId === "B"
        ? { items: [ref({ id: "att_B", name: "B的.txt" })], dropped: [], unconfirmed: [] }
        : { items: [], dropped: [], unconfirmed: [] };
    });
    const { w, session } = await mountComposer("A");

    session.currentTopicId = "B";
    await flushPromises();
    expect(chipNames(w)).toContain("B的.txt");

    slowA.resolve({ items: [ref({ id: "att_A", name: "A的.txt" })], dropped: [], unconfirmed: [] });
    await flushPromises();

    expect(chipNames(w)).toContain("B的.txt");
    expect(chipNames(w)).not.toContain("A的.txt");
    w.unmount();
  });

  it("A→B→A：返回后被更新的一次恢复胜出，旧恢复不覆盖", async () => {
    const slowFirstA = deferred<{ items: AttachmentRef[]; dropped: string[]; unconfirmed: AttachmentRef[] }>();
    let call = 0;
    mocks.restorePendingAttachments.mockImplementation(async (topicId: string | null) => {
      call += 1;
      if (call === 1) return slowFirstA.promise; // 第一次 A 很慢
      if (topicId === "B") {
        return { items: [ref({ id: "att_B", name: "B的.txt" })], dropped: [], unconfirmed: [] };
      }
      return { items: [ref({ id: "att_A2", name: "A新版.txt" })], dropped: [], unconfirmed: [] };
    });
    const { w, session } = await mountComposer("A");

    session.currentTopicId = "B";
    await flushPromises();
    session.currentTopicId = "A";
    await flushPromises();
    expect(chipNames(w)).toContain("A新版.txt");

    // 第一次 A 的旧恢复迟到：必须被更新的恢复取代
    slowFirstA.resolve({ items: [ref({ id: "att_A_old", name: "A旧版.txt" })], dropped: [], unconfirmed: [] });
    await flushPromises();

    expect(chipNames(w)).toContain("A新版.txt");
    expect(chipNames(w)).not.toContain("A旧版.txt");
    w.unmount();
  });

  it("旧恢复与新上传竞争：恢复期间的附件新增不被旧快照抹掉", async () => {
    const slowRestore = deferred<{ items: AttachmentRef[]; dropped: string[]; unconfirmed: AttachmentRef[] }>();
    mocks.restorePendingAttachments.mockReturnValueOnce(slowRestore.promise as never);
    const { w } = await mountComposer("A");

    await chooseFiles(w, "新加的.txt"); // 恢复在途，用户新增
    expect(chipNames(w)).toContain("新加的.txt");

    slowRestore.resolve({ items: [ref({ id: "att_old", name: "旧的.txt" })], dropped: [], unconfirmed: [] });
    await flushPromises();

    expect(chipNames(w)).toContain("旧的.txt");
    expect(chipNames(w)).toContain("新加的.txt");
    w.unmount();
  });

  it("组件卸载后异步完成：只落持久化，不抛错", async () => {
    const upload = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValue(upload.promise as never);
    const { w } = await mountComposer("A");

    await chooseFiles(w, "晚到.txt");
    w.unmount();

    upload.resolve(ref({ id: "att_late", name: "晚到.txt", state: "ready" }));
    await flushPromises();

    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_late"]);
  });
});
