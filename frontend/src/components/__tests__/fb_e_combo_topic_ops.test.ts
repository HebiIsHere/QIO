/**
 * fb-E / 阶段二 · 跨模块组合（前端）：R3×R4（切话题 + 移除 + 晚到结果）。
 *
 * 场景：在 A 触发路径选择（R4：选择器在途）→ 切到 B → 在 B 新增并移除一个附件（R3 tombstone）
 * → 放行 A 的选择器结果（必须只落 A，不进 B 的 UI）→ 再放行 B 那次恢复的补丁（仍带着被移除的 id，
 * 必须被移除水位拦下，不得复活）。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/fb_e_combo_topic_ops.test.ts
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
  desktopShell: { value: false },
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
    isDesktopShell: () => mocks.desktopShell.value,
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
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
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

async function clickRemove(w: VueWrapper, name: string) {
  const chip = w.findAll(".composer .chip").find((c) => c.text().includes(name));
  expect(chip, "找不到要移除的 chip：" + name).toBeTruthy();
  const btn = chip!.findAll("button").find((b) => (b.text() || "").trim() === "×");
  await btn!.trigger("click");
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) {
    const value: unknown = mocks[key];
    if (value && typeof value === "object" && "mockReset" in (value as object)) {
      (value as { mockReset: () => void }).mockReset();
    }
  }
  mocks.desktopShell.value = true;
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [], unconfirmed: [] } as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.pickBrowserFile.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item as never);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.uploadAttachment.mockImplementation(async (file: File) =>
    ref({ id: "att_" + file.name, name: file.name, state: "ready" }),
  );
  mocks.getAttachment.mockImplementation(async (id: string) => ref({ id, name: id, state: "ready" }));
  mocks.getSessionContext.mockResolvedValue({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] } as never);
});

describe("R3×R4 组合：切话题 + 移除 + 晚到结果", () => {
  it("A 的选择器在切到 B 后返回只落 A；B 在途移除的附件不被晚到补丁复活", async () => {
    const picked = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValue(picked.promise as never);
    mocks.prepareAttachment.mockImplementation(async () =>
      ref({ id: "att_来自A", name: "来自A.txt", state: "prepared" }),
    );

    const { w, session } = await mountComposer("A");
    await w.find(".composer .attach-btn").trigger("click");
    await flushPromises();

    // 切到 B：B 的恢复在途（受控 deferred），记录它回显的修订号
    let seenRevision = -1;
    const inflight = deferred<unknown>();
    mocks.restorePendingAttachments.mockImplementation((_topicId: string | null, options?: { revision?: number }) => {
      seenRevision = typeof options?.revision === "number" ? options.revision : 0;
      return inflight.promise as never;
    });
    session.currentTopicId = "B";
    await flushPromises();

    // 在 B 新增一个附件并移除它（tombstone 发生在 B 的恢复在途之后）
    await chooseFiles(w, "B被移除.txt");
    expect(chipNames(w)).toContain("B被移除.txt");
    await clickRemove(w, "B被移除.txt");
    expect(chipNames(w)).not.toContain("B被移除.txt");

    // A 的选择器返回：必须归发起话题 A，且不得写当前话题 B 的 UI
    picked.resolve("C:/tmp/来自A.txt");
    await flushPromises();
    await flushPromises();
    const calls = mocks.prepareAttachment.mock.calls as unknown as [string, { topicId?: string | null }][];
    expect(calls[0]?.[1]?.topicId, "A 的选择结果必须归发起话题 A").toBe("A");
    expect(loadPendingAttachments("A").map((i) => i.id)).toContain("att_来自A");
    expect(chipNames(w), "A 的结果不得写进当前话题 B 的 UI").not.toContain("来自A.txt");

    // B 的恢复补丁晚到，且仍带着被移除的 id → 必须被移除水位拦下
    inflight.resolve({
      topicId: "B",
      revision: seenRevision,
      restored: [ref({ id: "att_B被移除.txt", name: "B被移除.txt", state: "ready" })],
      missing: [],
      missingIds: [],
    });
    await flushPromises();
    await flushPromises();
    expect(chipNames(w), "晚到的恢复补丁复活了 B 里已移除的附件").not.toContain("B被移除.txt");
    expect(loadPendingAttachments("B").map((i) => i.id)).not.toContain("att_B被移除.txt");
    w.unmount();
  });
});
