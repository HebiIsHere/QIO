/**
 * 「正在准备附件…」（用户必须满足项）：带附件发送后、后端**受理返回之前**的状态。
 *
 * 背景（B 的问题一）：准备完成前这一轮不入队、不发 TURN_START、模型 0 次调用 ——
 * 所以准备期间界面**不能**显示「正在思考 / 正在执行」、不能显示阶段历史或任何
 * 模型已经开始的迹象；同时要能**中止这次请求**（后端契约 §1.1：客户端断开 →
 * abandon + 清理本次克隆，绝不「先执行再取消」）。
 *
 * 修复前红：带附件发送后界面上只有「发送中」的既有状态，既没有统一的
 * 「正在准备附件…」，也没有中止入口。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/PreparingAttachments.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn((..._args: unknown[]) => Promise.resolve({ ok: true, topic_id: null })),
  getSessionContext: vi.fn(async () => ({
    topic_id: "",
    topic_name: null,
    anchor_fragment: null,
    messages: [],
  })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  removeAttachment: vi.fn(async () => undefined),
  retryAttachment: vi.fn(),
  waitUntilSettled: vi.fn(async (item: unknown) => item),
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
import { useSessionStore } from "../../stores/session";
import type { AttachmentRef } from "../../services/attachments";

function readyRef(id = "att_ready"): AttachmentRef {
  return {
    id,
    name: "报告.txt",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
  };
}

const ACCEPTED = {
  ok: true,
  accepted: true,
  turn_id: "turn_1",
  status: "accepted",
  topic_id: null,
  bound_attachment_ids: ["att_ready"],
};

/**
 * 挂载 + **在 api 层**受控：真实的 session.send 照常参与（乐观消息、受理回执、
 * 失败清理都走真代码），api.sendTurn 返回一个可 resolve / 可 abort 的 promise。
 */
/**
 * `abortFails`（默认 true）：abort 时请求以 AbortError 失败 —— 正常路径。
 * 设成 false 模拟**竞态**：信号已经 aborted，但响应已经在路上（随后仍成功返回）。
 */
async function mountDeferred(options: { abortFails?: boolean } = {}) {
  const abortFails = options.abortFails !== false;
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  let settle: (value: unknown) => void = () => undefined;
  let fail: (err: unknown) => void = () => undefined;
  let signal: AbortSignal | undefined;
  mocks.sendTurn.mockImplementationOnce((...args: unknown[]) => {
    signal = args[4] as AbortSignal | undefined;
    return new Promise((resolve, reject) => {
      settle = resolve as (value: unknown) => void;
      fail = reject;
      signal?.addEventListener("abort", () => {
        if (!abortFails) return; // 竞态：abort 只是标记，响应仍在路上
        reject(Object.assign(new Error("The user aborted a request."), { name: "AbortError" }));
      });
    });
  });
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();
  return {
    w,
    session,
    settle: (value: unknown = ACCEPTED) => settle(value),
    fail,
    signal: () => signal,
  };
}
/** 浏览器回退入口：真实地「选一个文件」，让 pending 里有一条已就绪的附件。 */
async function attachFile(w: VueWrapper, name = "报告.txt") {
  mocks.uploadAttachment.mockResolvedValueOnce(readyRef());
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: [new File(["x"], name)],
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}


async function typeAndSend(w: VueWrapper, text = "带附件的消息") {
  await w.find("textarea").setValue(text);
  await w.find(".send-btn").trigger("click");
  await flushPromises();
}

const PREPARING = '[data-test="preparing-attachments"]';

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.waitUntilSettled.mockImplementation(async (item: unknown) => item);
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [] });
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
});

describe("带附件发送：「正在准备附件…」", () => {
  it("后端还没受理时显示统一状态，且不显示任何执行迹象", async () => {
    const { w } = await mountDeferred();
    await attachFile(w);

    await typeAndSend(w);

    // 真的等了一段时间才显示（防抖）：先等过阈值
    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();

    const status = w.find(PREPARING);
    expect(status.exists(), "带附件、后端未受理时必须显示「正在准备附件…」").toBe(true);
    expect(status.text()).toContain("正在准备附件");
    // 绝不能出现「已经跑起来了」的迹象
    expect(status.text()).not.toContain("思考");
    expect(status.text()).not.toContain("执行中");
    expect(w.text(), "准备期没有阶段历史/过程区").not.toContain("个阶段");
    expect(w.find('[data-test="turn-process"]').exists()).toBe(false);
    expect(w.find(".process-line").exists()).toBe(false);
    // 发送确实带上了附件 id（不是只发文字）
    expect(mocks.sendTurn).toHaveBeenCalledTimes(1);
    expect(mocks.sendTurn.mock.calls[0]?.[2]).toEqual(["att_ready"]);
    w.unmount();
  });

  it("受理之后该状态消失（不残留）", async () => {
    const { w, session, settle } = await mountDeferred();
    await attachFile(w);
    await typeAndSend(w);
    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();
    expect(w.find(PREPARING).exists()).toBe(true);

    // 受理：把消息当成真的进了流（乐观消息 + turn_id 关联）
    session.pushUser("带附件的消息");
    settle(true);
    await flushPromises();
    await nextTick();

    expect(w.find(PREPARING).exists(), "受理之后不得继续显示准备状态").toBe(false);
    w.unmount();
  });

  it("不带附件的发送：不显示准备状态（不无中生有）", async () => {
    const { w } = await mountDeferred();
    await typeAndSend(w, "纯文字");

    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();

    expect(mocks.sendTurn).toHaveBeenCalledTimes(1);
    expect(mocks.sendTurn.mock.calls[0]?.[2]).toEqual([]);
    expect(w.find(PREPARING).exists()).toBe(false);
    w.unmount();
  });

  it("秒级就绪（受理很快）：不闪出准备状态", async () => {
    const { w, settle } = await mountDeferred();
    await attachFile(w);
    await typeAndSend(w, "很快的消息");
    // 阈值之前就受理
    settle(true);
    await flushPromises();
    await nextTick();
    expect(w.find(PREPARING).exists()).toBe(false);

    await new Promise((resolve) => setTimeout(resolve, 300));
    await nextTick();
    expect(w.find(PREPARING).exists(), "已经受理完了就不该再闪出来").toBe(false);
    w.unmount();
  });

  it("中止：请求被真的 abort，且不留「这一轮已发出」的执行迹象", async () => {
    const { w, session, signal } = await mountDeferred();
    await attachFile(w);
    await typeAndSend(w, "要中止的消息");
    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();

    const cancel = w.find('[data-test="preparing-cancel"]');
    expect(cancel.exists(), "准备状态必须能中止").toBe(true);
    await cancel.trigger("click");
    await flushPromises();

    // 真正的 abort（后端据此 abandon 预留，见契约 §1.1）：
    // api 层收到的 signal 被 abort，请求以 AbortError 收尾（mock 里就是这么接的）
    expect(signal(), "必须把 AbortSignal 交给发送调用").toBeTruthy();
    expect(signal()?.aborted, "点中止必须真的 abort 请求").toBe(true);

    await flushPromises();
    await nextTick();

    expect(w.find(PREPARING).exists()).toBe(false);
    expect(w.find('[data-test="turn-process"]').exists()).toBe(false);
    expect(session.messages.some((m) => m.role === "user" && m.content === "要中止的消息")).toBe(false);
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("要中止的消息");
    expect(w.text()).toContain("已中止");
    w.unmount();
  });
});

describe("abort 与「已受理」的竞态", () => {
  it("用户已按中止但请求已经受理：不假装从未发送，如实按「已取消」收尾", async () => {
    const { w, session, settle, signal } = await mountDeferred({ abortFails: false });
    await attachFile(w);
    await typeAndSend(w, "竞态的消息");
    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();

    await w.find('[data-test="preparing-cancel"]').trigger("click");
    await flushPromises();
    expect(signal()?.aborted).toBe(true);

    // 请求已经在那一刻受理了：后端把它当成一轮真实的 turn
    session.pushUser("竞态的消息");
    session.turnRunning = true;
    settle(true);
    await flushPromises();
    await nextTick();

    // 如实显示「已取消」，并且真的走了停止入口（不是假装没发过）
    const notice = w.find('[data-test="preparing-notice"]');
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("已取消");
    expect(w.find(PREPARING).exists()).toBe(false);
    expect(w.find('[data-test="turn-process"]').exists()).toBe(false);
    w.unmount();
  });
});
