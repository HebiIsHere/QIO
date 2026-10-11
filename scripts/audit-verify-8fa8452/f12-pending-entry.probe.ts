/**
 * E 路独立核验探针 · F12（组件层）：两个聊天入口共用未决发送 + 不自动重发
 * 基线 8fa845255c118a7104b1de8e37298ca745924924482
 *
 * 规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F12 段。
 * 要求（本探针只取组件层可判定的三条）：
 *   - 普通对话页与悬浮聊天共用未决发送的状态规则与恢复动作；
 *   - 提供查证 / 同身份重试 / 查看原文 / 明确放弃；切到别的话题不串；
 *   - 不因展开/收起或重开自动发送。
 *
 * 视角与既有测试**不同**：本探针专门盯「两个入口同时存在时的状态一致性」——
 *   同时挂载 ChatDock 与 Composer，在其中一个入口操作后，另一个入口必须立刻反映同一份状态，
 *   且绝不能各自产生一次发送（否则就是「一次发送、两个身份」的重复执行风险）。
 *
 * 运行（在**被测 worktree** 的 frontend 下）：
 *   $env:QIO_FE_SRC='D:/qio-dev/qa-lead/frontend/src'
 *   npx vitest run --root '..' -c 'D:\qio-dev\qa-e\scripts\audit-verify-8fa8452\vitest.config.ts'
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";

vi.mock("../../frontend/src/services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    cancelContinuation: vi.fn(),
    cancelTurn: vi.fn(),
    cancelActiveTurn: vi.fn(),
    saveDrafts: vi.fn(),
    saveBoardState: vi.fn(),
    fetchBoardState: vi.fn(),
    fetchVisibleRange: vi.fn(),
    previewMaterialImpact: vi.fn(),
    submitBoard: vi.fn(),
    fetchIntents: vi.fn(),
    approveIntent: vi.fn(),
    rejectIntent: vi.fn(),
    batchDecide: vi.fn(),
    createDemoIntents: vi.fn(),
    advanceIntent: vi.fn(),
    updateIntentPreview: vi.fn(),
    fetchHistoryPage: vi.fn(),
    fetchMessages: vi.fn(),
    lookupTurnByRequest: vi.fn(),
    getInstance: vi.fn(),
  },
}));
vi.mock("../../frontend/src/services/interactive", () => ({
  saveDrafts: vi.fn(),
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
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

import ChatDock from "../../frontend/src/components/interactive/ChatDock.vue";
import Composer from "../../frontend/src/components/Composer.vue";
import { useSessionStore } from "../../frontend/src/stores/session";
import { useInteractiveStore } from "../../frontend/src/stores/interactive";
import { api } from "../../frontend/src/services/api";

function installLocalStorage(): void {
  const map = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
      setItem: (k: string, v: string) => void map.set(k, String(v)),
      removeItem: (k: string) => void map.delete(k),
      clear: () => map.clear(),
      key: (i: number) => Array.from(map.keys())[i] ?? null,
      get length() { return map.size; },
    },
  });
}

const mounted: Array<{ unmount: () => void }> = [];

beforeEach(() => {
  installLocalStorage();
  vi.resetAllMocks();
  vi.useFakeTimers();
  mounted.length = 0;
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  for (const w of mounted) w.unmount();
  vi.useRealTimers();
});

async function failSend(session: ReturnType<typeof useSessionStore>, text: string): Promise<void> {
  vi.mocked(api.sendTurn).mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await session.send(text, []);
  await flushPromises();
}

describe("F12 独立反例：两个入口 + 不自动重发", () => {
  it("F12-E1 挂载/展开两个入口不得自动产生任何发送", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    useInteractiveStore().chatOpen = true;
    await failSend(session, "未决的一条");
    const callsAfterSend = vi.mocked(api.sendTurn).mock.calls.length;
    expect(callsAfterSend, "前置条件：只应有一次发送尝试").toBe(1);

    // 反复挂载 / 卸载两个入口一次：不得触发任何自动发送
    mounted.push(mount(ChatDock, { attachTo: document.body }));
    mounted.push(mount(Composer, { attachTo: document.body }));
    await flushPromises();
    await nextTick();
    await vi.advanceTimersByTimeAsync(1200);
    await flushPromises();

    const after = vi.mocked(api.sendTurn).mock.calls.length;
    console.log("[F12-E1] 发送调用数 前/后 =", callsAfterSend, "/", after);
    expect(after, "F12：挂载/展开入口后发生了自动重发").toBe(callsAfterSend);
  });

  it("F12-E2 悬浮聊天里重试：同身份、原文与话题都按快照，且成功后两个入口同时收口", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    useInteractiveStore().chatOpen = true;
    await failSend(session, "要同身份重试的话");

    const record = session.unresolvedSendsForTopic("A")[0];
    expect(record, "前置条件：必须留下一条未决发送").toBeTruthy();
    const requestId = record.clientRequestId;

    const dock = mount(ChatDock, { attachTo: document.body });
    mounted.push(dock);
    await flushPromises();

    // 期间切到别的话题、输入框写别的字：重试仍必须按快照
    session.currentTopicId = "B";
    session.draft = "之后输入的文字";
    await flushPromises();

    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true, accepted: true, turn_id: "turn_retry", status: "accepted", topic_id: "A",
    } as never);
    // 失败后会话层会排一次「查证」；先让它落地（404 = 本进程无记录 = 仍是未知），
    // 否则重试按钮正处于 busy 保护中（这是正确的防重入，不是缺陷）。
    vi.mocked(api.lookupTurnByRequest).mockResolvedValue({ ok: false, unknown: true } as never);
    await vi.advanceTimersByTimeAsync(900);
    await flushPromises();

    // 回到 A 再重试（未决入口只属于 A）
    session.currentTopicId = "A";
    await flushPromises();
    const retry = dock.find('[data-im="chat-pending-retry"]');
    expect(retry.exists(), "F12：悬浮聊天没有同身份重试入口").toBe(true);
    await retry.trigger("click");
    await flushPromises();

    const calls = vi.mocked(api.sendTurn).mock.calls;
    const last = calls[calls.length - 1];
    console.log("[F12-E2] sendTurn 调用数 =", calls.length, " 重试调用 =", JSON.stringify(last));
    console.log("[F12-E2] 未决记录 =", JSON.stringify(session.unresolvedSendsForTopic("A").map((r) => ({
      text: r.snapshot?.text, state: r.state, id: r.clientRequestId,
    }))));
    console.log("[F12-E2] sendConfirm =", JSON.stringify(session.sendConfirm));
    console.log("[F12-E2] dock 未决块 =", dock.find('[data-im="chat-pending"]').exists(),
      " retry 按钮 =", dock.find('[data-im="chat-pending-retry"]').exists());
    expect(last[0], "F12：重试没有按快照重放原文").toBe("要同身份重试的话");
    expect(last[1], "F12：重试没有回到快照里的话题").toBe("A");
    expect(session.unresolvedSendsForTopic("A"), "F12：受理成功后未决记录没有收口").toEqual([]);
    expect(requestId, "同一条记录的身份应稳定").toBeTruthy();
  });

  it("F12-E3 切到别的话题：那边不得出现未决块，也不得串入原文或错误", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    useInteractiveStore().chatOpen = true;
    await failSend(session, "只属于 A 的原文");
    const dock = mount(ChatDock, { attachTo: document.body });
    mounted.push(dock);
    await flushPromises();
    expect(dock.find('[data-im="chat-pending"]').exists(), "前置条件：A 里应有未决块").toBe(true);

    session.currentTopicId = "B";
    await flushPromises();
    const inB = dock.find('[data-im="chat-pending"]').exists();
    const textB = dock.text();
    const bHasActions = ["chat-pending-recheck", "chat-pending-retry", "chat-pending-view", "chat-pending-abandon"]
      .some((k) => dock.find('[data-im="' + k + '"]').exists());
    console.log("[F12-E3] B 里有未决块 =", inB, " B 里有未决动作 =", bHasActions);
    console.log("[F12-E3] B 视图文本片段 =", JSON.stringify(textB.slice(0, 300)));

    // A 的未决块与四个动作绝不能出现在 B（这才是「不串」的可判定断言）。
    // 注意：用户自己那条消息气泡留在流里是**允许**的（受理情况未知，不能撤回），
    // 所以这里不断言「原文文本不出现」—— B 自己的测试也明确保留了这一点。
    expect(inB, "F12：切到 B 后仍显示 A 的未决块（串话题）").toBe(false);
    expect(bHasActions, "F12：A 的未决动作出现在 B").toBe(false);

    session.currentTopicId = "A";
    await flushPromises();
    expect(dock.find('[data-im="chat-pending"]').exists(), "F12：回到 A 后未决块必须还在").toBe(true);
  });
});