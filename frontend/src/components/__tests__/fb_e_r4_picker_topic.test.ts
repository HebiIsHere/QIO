/**
 * fb-E / R4 反例：文件选择器等待期间切换话题，结果被注册进**新话题**。
 *
 * 用户可见规则（K1.1）：每个异步附件操作在**发起时刻**捕获 topicId；
 * 选择器返回后**不得**再读 currentTopicId 决定归属 —— 结果必须落到发起话题。
 *
 * 确定性时序：pickLocalPath 返回受控 deferred；期间把 currentTopicId 从 A 切到 B；
 * 再放行路径。断言 prepareAttachment 收到 topicId=A，且结果只落 A 的持久化、不进 B。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/fb_e_r4_picker_topic.test.ts
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

describe("R4 选择器等待期间切话题", () => {
  it("选择器返回后必须归发起话题 A，不得注册进 B", async () => {
    mocks.desktopShell.value = true; // 桌面壳：走原生选择器
    const picked = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValue(picked.promise as never);
    mocks.prepareAttachment.mockImplementation(async (_path: string, options: { topicId?: string | null }) =>
      ref({ id: "att_来自A", name: "来自A.txt", state: "prepared" }),
    );

    const { w, session } = await mountComposer("A");
    await w.find(".composer .attach-btn").trigger("click");
    await flushPromises();

    session.currentTopicId = "B";
    await flushPromises();

    picked.resolve("C:/tmp/来自A.txt");
    await flushPromises();
    await flushPromises();

    const calls = mocks.prepareAttachment.mock.calls as unknown as [string, { topicId?: string | null }][];
    expect(calls.length, "prepareAttachment 应该被调用一次").toBeGreaterThan(0);
    expect(calls[0]?.[1]?.topicId, "结果必须归发起话题 A，而不是返回时的当前话题 B").toBe("A");
    expect(loadPendingAttachments("A").map((i) => i.id)).toContain("att_来自A");
    expect(loadPendingAttachments("B").map((i) => i.id)).not.toContain("att_来自A");
    w.unmount();
  });
});
