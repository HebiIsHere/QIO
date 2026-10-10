/**
 * fb-E / R3（入口一）反例：附件准备中（poll 在途）被移除，旧 poll 结果把它 upsert 回来。
 *
 * 用户可见规则（K1.4）：removeOne 必须让该附件进入 removed/tombstone；
 * 之后任何在途的轮询结果都**不得**把它重新加回待发列表或持久化。
 *
 * 确定性时序：waitUntilSettled 返回受控 deferred；先移除 chip，再放行 poll 的最终结果。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/fb_e_r3_poll_removed.test.ts
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
  expect(btn, "chip 上没有移除按钮").toBeTruthy();
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
  mocks.desktopShell.value = false;
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

describe("R3① 准备中移除后，旧 poll 不得把它加回来", () => {
  it("poll 在途移除 → 放行 poll 后仍不得复活", async () => {
    const settle = deferred<AttachmentRef>();
    mocks.waitUntilSettled.mockReturnValue(settle.promise as never);

    const { w } = await mountComposer("A");
    await chooseFiles(w, "准备中.txt");
    expect(chipNames(w)).toContain("准备中.txt");

    await clickRemove(w, "准备中.txt");
    expect(chipNames(w)).not.toContain("准备中.txt");

    // 放行在途轮询的最终结果（服务说它已经 ready）
    settle.resolve(ref({ id: "att_准备中.txt", name: "准备中.txt", state: "ready" }));
    await flushPromises();

    expect(chipNames(w), "已移除的附件被旧 poll 结果重新加回列表").not.toContain("准备中.txt");
    expect(loadPendingAttachments("A").map((i) => i.id)).not.toContain("att_准备中.txt");
    w.unmount();
  });
});
