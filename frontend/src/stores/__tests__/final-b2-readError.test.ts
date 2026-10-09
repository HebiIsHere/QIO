/**
 * 收尾轮 B2 追加批次：区分「读取失败」与「写入失败」（契约 M7 / 反例 15）。
 *
 * 正确行为期望：
 * - 启动时本机失败原文**读取/解析失败** → failedSendReadError 给出真实原因；
 *   此时即使零条记录，界面也能说成「读取失败」，而不是「从来没有记录」或「已恢复」。
 * - 读取成功且零条 → failedSendReadError 为 null（这才是「从来没有记录」）。
 * - retryFailedSendRestore() 只重读本机：不发送、不重试、不动板面；
 *   读到的记录与内存记录按 id 合并，内存里更新/更多的记录不被覆盖。
 *
 * 标注：【状态】只依赖 stores/session.ts 的公开状态与动作；【模拟读失败】受控 localStorage。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    saveDrafts: vi.fn(),
  },
}));

vi.mock("../../services/interactive", () => ({
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

const FAILED_SEND_KEY = "qio.chat.failedSend.v1";

/** 用受控 localStorage 替换真实实现，返回还原函数 */
function breakStorage(): () => void {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => {
        throw new Error("本地存储被禁用");
      },
      setItem: () => {},
      removeItem: () => {},
    },
  });
  return () => {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("读取失败与「从来没有记录」必须分得开（M7）", () => {
  it("【状态】零条 + 读取失败：failedSendReadError 给出真实原因，不许当成没有记录", () => {
    const restoreStorage = breakStorage();
    try {
      const session = useSessionStore();
      expect(session.failedSends).toEqual([]);
      expect(
        session.failedSendReadError,
        "读取失败时没有给出真实原因，界面只能说成「没有记录」",
      ).toContain("本地存储被禁用");
      const retried = session.retryFailedSendRestore();
      expect(retried.ok).toBe(false);
      expect(retried.error, "重读仍然失败时要如实回报").toContain("本地存储被禁用");
      expect(retried.restored).toBe(0);
    } finally {
      restoreStorage();
    }
  });

  it("【状态】零条 + 读取成功：这是「从来没有记录」，不许报读取失败", () => {
    const session = useSessionStore();
    expect(session.failedSendReadError).toBeNull();
    expect(session.failedSends).toEqual([]);
    expect(session.retryFailedSendRestore()).toEqual({ ok: true, restored: 0, error: null });
  });

  it("【状态】读不出来时不许虚报「已恢复」：failedSends 保持为空", () => {
    const restoreStorage = breakStorage();
    try {
      const session = useSessionStore();
      const retried = session.retryFailedSendRestore();
      expect(retried.restored, "读失败却报告恢复出了记录").toBe(0);
      expect(session.failedSends).toEqual([]);
    } finally {
      restoreStorage();
    }
  });
});

describe("retryFailedSendRestore：只重读本机，按 id 合并不覆盖内存", () => {
  it("【状态】补进磁盘里存在、内存里还没有的记录；不发送", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "内存里的失败原文";
    await vi.advanceTimersByTimeAsync(500);
    session.sendAttribution();
    session.draft = "";
    await session.send("内存里的失败原文");
    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;

    // 模拟另一次会话在本机留下的记录（内存里没有它）
    const raw = JSON.parse(localStorage.getItem(FAILED_SEND_KEY) as string) as {
      version: number;
      topics: Record<string, unknown[]>;
    };
    raw.topics["A"] = [
      ...(raw.topics["A"] ?? []),
      {
        id: "fail_disk_only",
        draftId: "send_disk_only",
        topicId: "A",
        text: "只留在本机的失败原文",
        draftSeq: 3,
        contentVersion: 1,
        at: Date.now(),
        error: "网关超时",
      },
    ];
    localStorage.setItem(FAILED_SEND_KEY, JSON.stringify(raw));

    const restored = session.retryFailedSendRestore();
    expect(restored.ok).toBe(true);
    expect(restored.restored).toBe(1);
    expect(session.failedSendReadError).toBeNull();
    const texts = session.failedSendsForTopic("A").map((record) => record.text);
    expect(texts, "内存里的记录被重读覆盖/丢失").toContain("内存里的失败原文");
    expect(texts, "磁盘里已有的记录没有被补进来").toContain("只留在本机的失败原文");
    expect(
      vi.mocked(api.sendTurn).mock.calls.length - callsBefore,
      "重读本机存储时又打了发送请求（重读不是重发）",
    ).toBe(0);
    // 磁盘记录自己的原因也要一起带回来
    const diskRecord = session.failedSendsForTopic("A").find((r) => r.id === "fail_disk_only");
    expect(diskRecord).toBeTruthy();
    expect(session.failedSendErrors["fail_disk_only"]).toBe("网关超时");
  });

  it("【状态】同 id 的记录以内存为准：磁盘上的旧版本不许覆盖内存里更新的正文", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "互换前的正文";
    await vi.advanceTimersByTimeAsync(500);
    session.sendAttribution();
    session.draft = "";
    await session.send("互换前的正文");
    const record = session.failedSendsForTopic("A")[0];

    // 用户在内存里与失败原文互换：记录正文与版本都往前走了（还没有写回磁盘时的状态）
    session.draft = "用户后来写的正文";
    await vi.advanceTimersByTimeAsync(500);
    session.swapFailedSendText(record.id);
    const swapped = session.failedSendsForTopic("A")[0];
    expect(swapped.text).toBe("用户后来写的正文");

    // 磁盘上仍是互换前的旧版本（同 id）：重读不许把它盖回来
    const raw = JSON.parse(localStorage.getItem(FAILED_SEND_KEY) as string) as {
      version: number;
      topics: Record<string, Array<Record<string, unknown>>>;
    };
    raw.topics["A"] = (raw.topics["A"] ?? []).map((item) =>
      item.id === record.id ? { ...item, text: "互换前的正文", contentVersion: 1 } : item,
    );
    localStorage.setItem(FAILED_SEND_KEY, JSON.stringify(raw));

    session.retryFailedSendRestore();
    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "重读把内存里更新过的记录按磁盘旧版本覆盖了",
    ).toEqual(["用户后来写的正文"]);
  });
});
