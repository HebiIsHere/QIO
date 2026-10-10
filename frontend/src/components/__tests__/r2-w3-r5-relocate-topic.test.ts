/**
 * R5 反例：原生选择器失败后走「粘贴路径」，重新定位被写到**当前话题**而不是发起话题。
 *
 * 冻结规则（契约 §5 R5）：原始**话题 + 附件身份**必须贯穿
 * 「打开选择器 → 失败回退 → 粘贴路径 → 提交」全过程；切话题后结果仍只属于发起话题 A。
 *
 * 确定性时序（受控 deferred，无随机 sleep）：
 *   A 话题点「重新定位」→ 原生选择器挂住 → 切到 B → 选择器**失败**（回退到粘贴路径）
 *   → 粘贴路径提交 → 断言结果落 A、绝不写进 B。
 *
 * 基线（6ca65f9）行为：relocateOne 的 capture 在回退时被丢弃，submitPath 用
 * **提交时刻的 currentTopicId（= B）** 重新冻结身份 → 重新定位结果落进 B。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/r2-w3-r5-relocate-topic.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
  cancelPreparing: vi.fn(async () => ({ ok: true, cancelled: true })),
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
  desktopShell: { value: false },
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
    getAttachment: mocks.getAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    pickLocalPath: mocks.pickLocalPath,
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

/** 引用型附件：只有它才有「重新定位」入口。 */
function referenceRef(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_A",
    name: "A文件.txt",
    sizeBytes: 2048,
    kind: "reference",
    display: "引用本地文件",
    state: "ready",
    error: null,
    actions: ["relocate"],
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
  return { w, session };
}

/** 浏览器回退入口：选一个文件 → 待发列表里出现一条引用型附件。 */
async function chooseFile(w: VueWrapper, name = "A文件.txt") {
  mocks.uploadAttachment.mockImplementationOnce(async () =>
    referenceRef({ id: "att_A", name }),
  );
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: [new File(["x"], name)],
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}

function chipOf(w: VueWrapper, name: string) {
  const chip = w.findAll(".composer .chip").find((c) => c.text().includes(name));
  expect(chip, "找不到 chip：" + name).toBeTruthy();
  return chip!;
}

async function clickRelocate(w: VueWrapper, name: string) {
  const locate = chipOf(w, name).findAll("button").find((b) => b.text().includes("重新定位"));
  expect(locate, "chip 上没有「重新定位」按钮").toBeTruthy();
  await locate!.trigger("click");
  await flushPromises();
}

/** 粘贴路径并提交（按钮文案在重新定位模式下是「重新定位」）。 */
async function submitPath(w: VueWrapper, path: string) {
  await w.find(".path-input").setValue(path);
  const btn = w
    .findAll(".path-row button")
    .find((b) => b.text().includes("重新定位") || b.text().includes("添加"));
  expect(btn, "路径行没有提交按钮").toBeTruthy();
  await btn!.trigger("click");
  await flushPromises();
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) {
    const value: unknown = mocks[key];
    // vi.fn() 是 function（不是 object）：两个分支都要覆盖，否则跨用例不重置
    if (typeof value === "function" && "mockReset" in value) {
      (value as unknown as { mockReset: () => void }).mockReset();
    }
  }
  mocks.desktopShell.value = false;
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item as never);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [], unconfirmed: [], missingIds: [] } as never);
  mocks.relocateAttachment.mockImplementation(async (id: string) =>
    referenceRef({ id, name: "A文件-新位置.txt" }),
  );
});

describe("R5：原生选择器失败后粘贴路径的归属", () => {
  it("A 启动 → 切到 B → 选择器失败 → 粘贴路径提交：结果只属于 A", async () => {
    mocks.desktopShell.value = true;
    const picked = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValue(picked.promise as never);

    const { w, session } = await mountComposer("A");
    await chooseFile(w, "A文件.txt");
    expect(w.findAll(".composer .chip").length, "A 话题里应有一条待发附件").toBe(1);

    // A 话题点「重新定位」：打开原生选择器（挂住）
    await clickRelocate(w, "A文件.txt");

    // 选择器还没返回，用户切到 B
    session.currentTopicId = "B";
    await flushPromises();

    // 选择器失败 → 回退到「粘贴路径」
    picked.reject(new Error("原生文件选择器不可用"));
    await flushPromises();
    expect(w.find(".path-input").exists(), "失败后必须给出粘贴路径入口").toBe(true);

    // 粘贴新位置并提交
    await submitPath(w, "C:/tmp/A文件-新位置.txt");

    // 归属：重新定位作用在 A 的那条附件上
    expect(mocks.relocateAttachment, "必须真的走重新定位").toHaveBeenCalledWith(
      "att_A",
      "C:/tmp/A文件-新位置.txt",
    );
    // 结果绝不能写进当前话题 B（R5 的核心反例）
    expect(
      loadPendingAttachments("B").map((i) => i.id),
      "重新定位的结果不得写进当前话题 B",
    ).not.toContain("att_A");
    // 必须落回发起话题 A，且带着新事实
    const persistedA = loadPendingAttachments("A");
    expect(persistedA.map((i) => i.id), "结果必须落回发起话题 A").toContain("att_A");
    expect(
      persistedA.find((i) => i.id === "att_A")?.name,
      "落回 A 的必须是这次重新定位的新事实",
    ).toBe("A文件-新位置.txt");
    // B 的界面也不得出现属于 A 的结果说明
    expect(w.text(), "B 的界面不得替 A 报告重新定位结果").not.toContain("已受理重新定位");
    w.unmount();
  });

  it("取消粘贴路径：这次重新定位意图被清掉，之后的路径按「添加」处理", async () => {
    mocks.desktopShell.value = true;
    const picked = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValue(picked.promise as never);
    mocks.prepareAttachment.mockImplementation(async () => referenceRef({ id: "att_new" }));

    const { w } = await mountComposer("A");
    await chooseFile(w, "A文件.txt");
    await clickRelocate(w, "A文件.txt");
    picked.reject(new Error("原生文件选择器不可用"));
    await flushPromises();
    expect(w.find(".path-input").exists()).toBe(true);

    // 用户按路径行的「取消」：重新定位意图必须被清掉
    const cancel = w.findAll(".path-row button").find((b) => b.text().includes("取消"));
    expect(cancel, "路径行应有取消按钮").toBeTruthy();
    await cancel!.trigger("click");
    await flushPromises();
    expect(w.find(".path-input").exists(), "取消后路径行收起").toBe(false);

    // 之后再打开路径行：这次是「添加」（不是重新定位）
    await w.findAll(".attach-btn").find((b) => b.text().includes("路径"))!.trigger("click");
    await flushPromises();
    await submitPath(w, "C:/tmp/另一个文件.txt");

    expect(mocks.relocateAttachment, "取消之后不得再提交重新定位").not.toHaveBeenCalled();
    expect(mocks.prepareAttachment, "取消之后的路径按新增附件处理").toHaveBeenCalled();
    w.unmount();
  });

  it("卸载后：在途的重新定位结果不写 UI，但仍按 A 落盘", async () => {
    mocks.desktopShell.value = true;
    const picked = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValue(picked.promise as never);

    const { w } = await mountComposer("A");
    await chooseFile(w, "A文件.txt");
    await clickRelocate(w, "A文件.txt");

    w.unmount();
    // 组件已经卸载，选择器这时才返回路径
    picked.resolve("C:/tmp/A文件-新位置.txt");
    await flushPromises();
    await flushPromises();

    expect(mocks.relocateAttachment, "卸载后仍按发起话题提交重新定位").toHaveBeenCalledWith(
      "att_A",
      "C:/tmp/A文件-新位置.txt",
    );
    expect(
      loadPendingAttachments("A").find((i) => i.id === "att_A")?.name,
      "卸载后结果仍要落回发起话题 A 的持久化",
    ).toBe("A文件-新位置.txt");
  });
});