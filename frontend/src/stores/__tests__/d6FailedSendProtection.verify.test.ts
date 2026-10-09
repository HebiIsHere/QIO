/**
 * 独立验收 D（第六轮）：失败原文与未绑定发送的保护（契约 §12.3 / §12.4 / §12.5）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 每条用例按「用户能观察到的正确行为」断言，对应验收条文：
 * - §12.3 反例 4：未绑定发送在飞，话题绑定 A、用户切到 B 后发送失败 —— 失败恢复记录
 *   必须落在原发送对应的话题（A），不许落到现在所在的话题（B）。
 * - §12.4 反例 5：相同文字两次发送、后失败、前成功 —— 成功清理只许处理**该次发送**
 *   对应的记录，不许按文字相同把后一次失败原文一并删掉。
 * - §12.5 反例 6：九份失败原文 —— 用户尚未处理的失败原文不许静默淘汰
 *   （§12.5 把「每话题保留 8 条」的静默裁剪口径废止；展示数量与保留数量分开，
 *   隐藏不等于删除），写本机、装回本机两个阶段都不许再裁剪一次。
 *
 * 标注：【状态】只依赖 store 的公开状态与动作；【模拟】受控接口替身。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useSessionStore } from "../session";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

/** 受控 sendTurn：按测试逐个改写实现。 */
const sendTurn = vi.fn();
vi.mock("../../services/api", () => ({
  api: {
    sendTurn: (...args: unknown[]) => sendTurn(...args),
  },
}));

function rejectedSend(times: number, message = "网络中断"): void {
  let left = times;
  sendTurn.mockImplementation(() => {
    if (left > 0) {
      left -= 1;
      return Promise.reject(new Error(message)) as never;
    }
    return Promise.resolve({ turn_id: "t_" + Date.now() }) as never;
  });
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("§12.3 反例 4：未绑定 → A → B → 失败，恢复记录不许落在 B", () => {
  it("【状态】失败原文与归属身份要归属到原发送对应的话题 A", async () => {
    const session = useSessionStore();
    session.draft = "没发出去的话";
    // 1) 话题还没确定：用户输入后点击发送（归属此刻定下；正文取自输入框，
    //    驱动修正：先打字再点发送才是真实路径 —— 空着输入框直接 sendAttribution 会生成
    //    空文本归属，send() 的 takeAttribution 文字对不上会走兜底派发新身份，测的不是产品缺陷。）
    const at = session.sendAttribution();
    expect(at.topicId, "点击发送时话题还没绑定").toBeNull();
    let reject!: (err: unknown) => void;
    sendTurn.mockImplementationOnce(
      () =>
        new Promise((_, rejec) => {
          reject = rejec;
        }) as never,
    );
    const sending = session.send("没发出去的话");
    await flushPromises();

    // 2) 发送在飞期间话题绑定到 A（本次发送的归属稳定为 A）
    session.currentTopicId = "topic_a";
    await flushPromises();

    // 3) 用户切到话题 B；此刻第一次发送才失败
    session.currentTopicId = "topic_b";
    await flushPromises();
    reject(new Error("网络中断"));
    await sending;
    await flushPromises();

    const inA = session.failedSendsForTopic("topic_a");
    const inB = session.failedSendsForTopic("topic_b");
    expect(
      inA.some((record) => record.text === "没发出去的话"),
      "恢复记录落在了 B（当前话题），而未绑定发送的归属已稳定为 A（§12.3）",
    ).toBe(true);
    expect(
      inB.some((record) => record.text === "没发出去的话"),
      "失败原文被算进了与之无关的话题 B",
    ).toBe(false);
    const record = inA.find((record) => record.text === "没发出去的话");
    expect(record?.draftId, "失败记录丢了这次发送的归属身份").toBe(at.draftId);
  });
});

describe("§12.4 反例 5：相同文字两次发送，后失败、前成功 —— 后一次原文不许消失", () => {
  it("【状态】前一次成功的回执不许按「文字相同」把后一次失败原文一并清掉", async () => {
    const session = useSessionStore();
    session.currentTopicId = "topic_t";
    await flushPromises();

    // 前一次发送（挂起中）
    let release!: (value: unknown) => void;
    sendTurn.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }) as never,
    );
    void session.send("重发的话");
    await flushPromises();

    // 后一次发送（同样文字）：立即失败
    rejectedSend(1);
    await expect(session.send("重发的话")).resolves.toBe(false);
    await flushPromises();
    expect(
      session.failedSendsForTopic("topic_t").length,
      "第二次发送失败后，后那一次的失败原文应该已经被记录",
    ).toBe(1);

    // 前一次（更早发出的）此刻才受理成功：只许清理这次发送自己的记录
    release({ turn_id: "t_first" });
    await flushPromises();
    await flushPromises();

    const remaining = session.failedSendsForTopic("topic_t");
    expect(
      remaining.some((record) => record.text === "重发的话"),
      "后一次的失败原文被「前一次成功」按文字相同一起清掉了（§12.4：成功只处理该次发送的记录，不许按文字相同匹配）",
    ).toBe(true);
  });
});

describe("§12.5 反例 6：九份失败原文，第一份不许被静默淘汰", () => {
  it("【状态】九次发送九次失败：每一条失败原文都仍可处理", async () => {
    const session = useSessionStore();
    session.currentTopicId = "topic_r";
    await flushPromises();
    rejectedSend(99);
    const texts: string[] = [];
    for (let i = 1; i <= 9; i += 1) {
      const text = "第" + i + "份失败原文";
      texts.push(text);
      await session.send(text);
      await flushPromises();
    }

    const kept = session.failedSendsForTopic("topic_r");
    for (const text of texts) {
      expect(
        kept.some((record) => record.text === text),
        "「" + text + "」被静默淘汰了（§12.5：不许自动淘汰用户尚未处理的失败原文）",
      ).toBe(true);
    }
    expect(
      kept.length,
      "展示数量可以少，但保留数量不许把未处理的原文悄悄弄丢",
    ).toBeGreaterThanOrEqual(9);
  });

  it("【状态】写本机存储时同样不许把最早的一份悄悄裁掉（磁盘已有记录不许在写入时丢失）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "topic_r2";
    await flushPromises();
    rejectedSend(99);
    for (let i = 1; i <= 9; i += 1) {
      await session.send("第" + i + "份失败原文之二");
      await flushPromises();
    }
    const persisted = localStorage.getItem("qio.chat.failedSend.v1");
    expect(persisted, "失败原文必须先写进本机存储").toBeTruthy();
    const parsed = JSON.parse(persisted as string) as {
      topics: Record<string, unknown[]>;
    };
    const bucket = parsed.topics["topic_r2"] ?? [];
    expect(
      bucket.length,
      "写本机的时候就已经把最早的一份悄悄裁掉了（用户还没有处理过）",
    ).toBeGreaterThanOrEqual(9);
  });
});
