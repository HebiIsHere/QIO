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
  /** 取消端点（契约 §1.1）：中止必须**以后端确认为准** */
  cancelPreparing: vi.fn(
    (_prepareId: string) => Promise.resolve({ ok: true, cancelled: true }) as Promise<unknown>,
  ),
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
  let prepareId: string | undefined;
  let cancelSettle: (value: unknown) => void = () => undefined;
  let cancelFail: (err: unknown) => void = () => undefined;
  // 取消端点默认挂住：这样能断言「确认之前不得宣称已中止」
  mocks.cancelPreparing.mockImplementation((...args: unknown[]) => {
    void args;
    return new Promise<unknown>((resolve, reject) => {
      cancelSettle = resolve as (value: unknown) => void;
      cancelFail = reject as (err: unknown) => void;
    });
  });
  mocks.sendTurn.mockImplementationOnce((...args: unknown[]) => {
    signal = args[4] as AbortSignal | undefined;
    prepareId = args[5] as string | undefined;
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
    prepareId: () => prepareId,
    confirmCancel: (value: unknown = { ok: true, cancelled: true, turn_id: "turn_1" }) =>
      cancelSettle(value),
    failCancel: (err: unknown) => cancelFail(err),
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

  it("中止：先调取消端点、**以后端确认为准**，并如实显示「这一轮没有发送」", async () => {
    const { w, session, signal, prepareId, confirmCancel } = await mountDeferred();
    await attachFile(w);
    await typeAndSend(w, "要中止的消息");
    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();

    const cancel = w.find('[data-test="preparing-cancel"]');
    expect(cancel.exists(), "准备状态必须能中止").toBe(true);
    await cancel.trigger("click");
    await flushPromises();

    // 发送时带了准备标识：后端才能定位这次准备（abort 本身不是证据）
    expect(prepareId(), "必须把准备标识交给发送调用").toBeTruthy();
    expect(mocks.cancelPreparing).toHaveBeenCalledWith(prepareId());

    // 后端还没确认：**不得**宣称已中止，界面处于「正在中止…」
    expect(w.text(), "确认之前不得宣称已中止").not.toContain("已中止");
    expect(w.find('[data-test="preparing-cancel"]').text()).toContain("正在中止");

    confirmCancel({ ok: true, cancelled: true, turn_id: "turn_p" });
    await flushPromises();
    await nextTick();

    // 确认之后：如实说「没有发送」，请求连接也放掉
    expect(signal()?.aborted, "确认之后才放掉连接").toBe(true);
    expect(w.find(PREPARING).exists()).toBe(false);
    expect(w.find('[data-test="turn-process"]').exists()).toBe(false);
    expect(session.messages.some((m) => m.role === "user" && m.content === "要中止的消息")).toBe(false);
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("要中止的消息");
    expect(w.text()).toContain("已中止");
    w.unmount();
  });

  it("拿不到后端确认：保留原因与可用操作，**不宣称**已中止", async () => {
    const { w, failCancel } = await mountDeferred();
    await attachFile(w);
    await typeAndSend(w, "确认失败的消息");
    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();

    await w.find('[data-test="preparing-cancel"]').trigger("click");
    await flushPromises();
    failCancel(new Error("网络断了"));
    await flushPromises();
    await nextTick();

    const notice = w.find('[data-test="preparing-notice"]');
    expect(notice.exists(), "失败必须给出可理解的原因").toBe(true);
    expect(notice.text()).toContain("中止失败");
    expect(notice.text()).not.toContain("已中止");
    // 还能再点一次（不是「已完成」的死状态）
    expect(w.find('[data-test="preparing-cancel"]').attributes("disabled")).toBeUndefined();
    w.unmount();
  });
});

describe("后端已经放行时的中止（already_started）", () => {
  it("如实说「已受理，已按停止取消」，并走既有停止流程", async () => {
    const { w, session, settle, confirmCancel } = await mountDeferred();
    await attachFile(w);
    await typeAndSend(w, "竞态的消息");
    await new Promise((resolve) => setTimeout(resolve, 260));
    await nextTick();

    await w.find('[data-test="preparing-cancel"]').trigger("click");
    await flushPromises();

    // 后端事实：这一轮已经放行/开始 —— 不得假装「没有发送」
    confirmCancel({ ok: true, cancelled: false, already_started: true, turn_id: "turn_1" });
    await flushPromises();
    await nextTick();

    // 已放行：POST 随后照常返回受理回执（准备状态随之收起）
    session.pushUser("竞态的消息");
    settle(true);
    await flushPromises();
    await nextTick();

    const notice = w.find('[data-test="preparing-notice"]');
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("已受理");
    expect(notice.text()).toContain("停止");
    // 走了既有停止入口（cancelTurn / cancelActiveTurn 之一）
    expect(
      mocks.cancelTurn.mock.calls.length + mocks.cancelActiveTurn.mock.calls.length,
      "already_started 必须真的调用停止入口",
    ).toBeGreaterThan(0);
    expect(w.find(PREPARING).exists()).toBe(false);
    expect(w.find('[data-test="turn-process"]').exists()).toBe(false);
    w.unmount();
  });
});
