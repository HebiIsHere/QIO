/**
 * R7 反例：重叠发送共享并互相清理准备状态。
 *
 * 冻结不变量（契约 §5 R7）：
 *   I1 每次发送拥有自己的准备状态对象与取消目标；没有跨发送共享的可变标量。
 *   I2 某次发送的完成清理只清自己那份：第二次发送绝不清空/置空第一次的
 *      prepareId、AbortController、定时器、取消状态。
 *   I3 仍未受理的发送，它的「中止」入口必须始终可达，且绑定它自己的 prepareId。
 *   I4 两次带附件发送若会同时处于「后端尚未受理」，第二次在派发前被拒绝：
 *      保留草稿与附件、给出可见原因、第一次完全不受影响；第一次受理后恢复正常。
 *   I5 组件卸载清掉所有定时器，不留悬挂；但**不 abort** 在途请求 ——
 *      abort 只由用户显式动作（「中止」/「停止」）触发，导航副作用不得丢掉已发送的消息
 *      （Lead 2026-10-10 裁决）。
 *
 * 确定性时序：第一次 sendTurn 用受控 deferred 挂住，第二次发送在它之前/之后按用例推进；
 * 防抖阈值用固定等待（200ms 阈值 → 等 260ms，同仓库既有测试口径），不用随机 sleep。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/r2-w3-r7-overlap-send.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
  cancelPreparing: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: "turn_1" })),
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
    isDesktopShell: () => false,
  };
});

import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";
import { loadPendingAttachments, type AttachmentRef } from "../../services/attachments";

const PREPARING = '[data-test="preparing-attachments"]';
const CANCEL = '[data-test="preparing-cancel"]';
const DEBOUNCE_MS = 200;

function readyRef(id: string, name: string): AttachmentRef {
  return {
    id,
    name,
    sizeBytes: 1024,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    actions: [],
  };
}

const ACCEPTED_1 = {
  ok: true,
  accepted: true,
  turn_id: "turn_1",
  status: "accepted",
  topic_id: "A",
  bound_attachment_ids: ["att_A"],
};
const ACCEPTED_LATER = {
  ok: true,
  accepted: true,
  turn_id: "turn_9",
  status: "accepted",
  topic_id: "A",
  bound_attachment_ids: [],
};

/** 挂载 + 让**第一次带附件发送**挂住（受控 deferred），后续发送立即受理。 */
async function mountWithPendingFirstSend() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = "A";
  let settleFirst: (value: unknown) => void = () => undefined;
  let rejectFirst: (err: unknown) => void = () => undefined;
  let firstSignal: AbortSignal | undefined;
  let firstPrepareId: string | undefined;
  mocks.sendTurn.mockImplementationOnce((...args: unknown[]) => {
    firstSignal = args[4] as AbortSignal | undefined;
    firstPrepareId = args[5] as string | undefined;
    return new Promise((resolve, reject) => {
      settleFirst = resolve as (value: unknown) => void;
      rejectFirst = reject as (err: unknown) => void;
    });
  });
  mocks.sendTurn.mockImplementation(async () => ACCEPTED_LATER);
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();
  return {
    w,
    session,
    settleFirst: (value: unknown = ACCEPTED_1) => settleFirst(value),
    rejectFirst,
    firstSignal: () => firstSignal,
    firstPrepareId: () => firstPrepareId,
  };
}

async function attachFile(w: VueWrapper, id: string, name: string) {
  mocks.uploadAttachment.mockResolvedValueOnce(readyRef(id, name));
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: [new File(["x"], name)],
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}

async function typeAndSend(w: VueWrapper, text: string) {
  await w.find("textarea").setValue(text);
  await w.find(".send-btn").trigger("click");
  await flushPromises();
}

function chipNames(w: VueWrapper): string[] {
  return w.findAll(".composer .chip .name").map((n) => n.text());
}

async function removeChip(w: VueWrapper, name: string) {
  const chip = w.findAll(".composer .chip").find((c) => c.text().includes(name));
  expect(chip, "找不到要移除的 chip：" + name).toBeTruthy();
  const btn = chip!.findAll("button").find((b) => (b.text() || "").trim() === "×");
  expect(btn, "chip 上没有移除按钮").toBeTruthy();
  await btn!.trigger("click");
  await flushPromises();
}

/** 越过「正在准备附件…」的防抖阈值（固定等待，非随机）。 */
async function waitPastDebounce() {
  await new Promise((resolve) => setTimeout(resolve, DEBOUNCE_MS + 60));
  await nextTick();
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item as never);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [], unconfirmed: [], missingIds: [] } as never);
});

describe("R7 (a) 第二次纯文字发送", () => {
  it("不得清掉第一次带附件发送的准备状态与中止入口（I1/I2/I3）", async () => {
    const { w, firstSignal, firstPrepareId } = await mountWithPendingFirstSend();
    await attachFile(w, "att_A", "第一次附件.txt");
    await typeAndSend(w, "第一条（带附件）");
    expect(mocks.sendTurn, "第一次带附件发送必须派发").toHaveBeenCalledTimes(1);

    // 用户把那条附件从待发列表移除 → 第二条是**纯文字**
    await removeChip(w, "第一次附件.txt");
    await typeAndSend(w, "第二条（纯文字）");
    expect(mocks.sendTurn, "第二次纯文字发送照常派发").toHaveBeenCalledTimes(2);

    // 第二次已经完成清理：第一次的防抖定时器不得被它清掉
    await waitPastDebounce();
    expect(
      w.find(PREPARING).exists(),
      "第一次仍未受理：它的准备状态必须还在（第二次的 finally 不得清掉第一次的定时器/状态）",
    ).toBe(true);

    // I3：中止入口必须可达，并且绑定第一次自己的 prepareId
    const prepId1 = String(firstPrepareId() ?? "");
    expect(prepId1, "第一次发送必须带准备标识").not.toBe("");
    const cancel = w.find(CANCEL);
    expect(cancel.exists(), "第一次的中止入口必须始终可达（I3）").toBe(true);
    await cancel.trigger("click");
    await flushPromises();
    expect(
      mocks.cancelPreparing,
      "中止必须绑定第一次自己的 prepareId，不得打到别的发送上（I3）",
    ).toHaveBeenCalledWith(prepId1);
    expect(firstSignal()?.aborted, "第一次自己的连接由它自己的中止放掉").toBe(true);
    w.unmount();
  });
});

describe("R7 (b) 第二次带附件发送", () => {
  it("派发前被拒绝：草稿与附件保留、有可见原因、第一次不受影响（I4）", async () => {
    const { w, settleFirst, firstSignal } = await mountWithPendingFirstSend();
    await attachFile(w, "att_A", "第一次附件.txt");
    await typeAndSend(w, "第一条（带附件）");

    // 第二次**带附件**：再选一个文件，然后发送
    await attachFile(w, "att_B", "第二次附件.txt");
    await typeAndSend(w, "第二条（带附件）");
    await waitPastDebounce();

    expect(
      mocks.sendTurn,
      "两次带附件发送会同时处于「后端尚未受理」：第二次必须在派发前被拒绝（I4）",
    ).toHaveBeenCalledTimes(1);

    // 草稿与附件都留着（用户不必重写）
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("第二条（带附件）");
    expect(chipNames(w), "附件必须留在待发列表").toContain("第二次附件.txt");
    // 可见原因（装置也能读到 data-test=attach-error）
    const reason = w.find('[data-test="attach-error"]');
    expect(reason.exists(), "拒绝必须给出可见原因").toBe(true);
    expect(reason.text()).toContain("还没有被后端受理");
    // 第一次完全不受影响
    expect(w.find(PREPARING).exists(), "第一次的准备状态不得受影响").toBe(true);
    expect(firstSignal()?.aborted, "第一次的连接不得被第二次影响").toBe(false);

    // 第一次受理之后：发送恢复正常（既有排队语义不变）
    settleFirst();
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledTimes(1);
    await typeAndSend(w, "第三条");
    expect(mocks.sendTurn, "第一次受理之后必须恢复正常发送").toHaveBeenCalledTimes(2);
    w.unmount();
  });
});

describe("R7 (c) 组件卸载", () => {
  /**
   * fake timers 下的确定性 flush：清空微任务队列并推进 0ms 定时器，
   * 直到条件成立（有界 200 轮）；不是随机 sleep，也不猜迭代轮数。
   */
  async function flushUntil(cond: () => boolean, what: string) {
    for (let i = 0; i < 200; i += 1) {
      if (cond()) return;
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(0);
    }
    throw new Error("flushUntil 条件未成立：" + what);
  }

  it("清掉全部在途准备定时器，但**不** abort 在途发送（I5）", async () => {
    vi.useFakeTimers();
    try {
      const pinia = createPinia();
      setActivePinia(pinia);
      const session = useSessionStore();
      session.currentTopicId = "A";
      let signal: AbortSignal | undefined;
      let settle: (value: unknown) => void = () => undefined;
      mocks.sendTurn.mockImplementationOnce((...args: unknown[]) => {
        signal = args[4] as AbortSignal | undefined;
        return new Promise((resolve) => {
          settle = resolve as (value: unknown) => void;
        });
      });
      mocks.sendTurn.mockImplementation(async () => ACCEPTED_LATER);

      const w = mount(Composer, { global: { plugins: [pinia] } });
      await flushUntil(() => mocks.restorePendingAttachments.mock.calls.length > 0, "挂载后的恢复核对");

      mocks.uploadAttachment.mockResolvedValueOnce(readyRef("att_A", "第一次附件.txt"));
      const input = w.find("input.file-input");
      Object.defineProperty(input.element, "files", {
        value: [new File(["x"], "第一次附件.txt")],
        configurable: true,
      });
      await input.trigger("change");
      await flushUntil(() => w.findAll(".composer .chip").length === 1, "附件入列");
      expect(loadPendingAttachments("A").map((i) => i.id)).toContain("att_A");

      await w.find("textarea").setValue("第一条（带附件）");
      // 参考基线：附件已入列、准备发送**之前**取；此后到断言之间不触发别的定时器
      const baseline = vi.getTimerCount();
      const root = w.element as HTMLElement;

      await w.find(".send-btn").trigger("click");
      await flushUntil(() => mocks.sendTurn.mock.calls.length === 1, "第一次发送派发");
      expect(signal?.aborted, "发送中：控制器还活着").toBe(false);
      /**
       * 派发后比基线多 2 个定时器（绝对值不为 0 是因为挂载 Vue/pinia/vitest 环境本身也有定时器）：
       *   +1 本次发送**自己的** 200ms 准备防抖定时器（Composer.beginPrepare）；
       *   +1 session store 给「刚入流的新消息」的 600ms freshIds 清理定时器
       *      （session.ts:1810，属于 store 不属于本组件，会在下面被推进掉）。
       */
      expect(
        vi.getTimerCount(),
        "派发后应只多出「本次准备防抖 + store 新消息清理」两个定时器",
      ).toBe(baseline + 2);

      w.unmount();

      // I5：卸载清掉**本组件**在途的准备定时器（store 那个 600ms 定时器不属于组件，仍挂着）
      expect(
        vi.getTimerCount(),
        "卸载必须清掉本组件在途的准备定时器（I5）",
      ).toBe(baseline + 1);
      // 裁决：卸载**不得** abort 在途发送（abort 只由用户显式动作触发）
      expect(signal?.aborted, "卸载不得 abort 在途发送：离开页面不丢已发送的消息").toBe(false);

      /**
       * 越过防抖阈值（200ms）与 store 定时器（600ms）：把当时挂着的定时器全部推进完。
       * 基线里那几个是环境自己的短命定时器（推进后归零），
       * 这里要求**一个都不剩** —— 本组件的准备回调既不补跑也不重生。
       */
      await vi.advanceTimersByTimeAsync(700);
      expect(
        vi.getTimerCount(),
        "卸载后越过全部已知定时器：不得再有任何悬挂定时器（含准备回调）",
      ).toBe(0);
      expect(
        root.querySelector('[data-test="preparing-attachments"]'),
        "卸载后越过防抖阈值不得再冒出准备状态",
      ).toBe(null);

      // 放行这次请求：不抛错；结果照既有 alive 语义只落持久化（A 的待发列表按「已发送」收尾）
      settle(ACCEPTED_1);
      await flushUntil(() => loadPendingAttachments("A").length === 0, "受理后按 A 落盘收尾");
      expect(signal?.aborted, "放行之后也不得被 abort").toBe(false);
      expect(vi.getTimerCount(), "放行之后同样不得有悬挂定时器").toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
