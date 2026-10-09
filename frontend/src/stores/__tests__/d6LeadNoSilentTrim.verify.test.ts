import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(), getSessionContext: vi.fn(), stopTurn: vi.fn(), cancelContinuation: vi.fn(),
    saveDrafts: vi.fn(), saveBoardState: vi.fn(), fetchBoardState: vi.fn(), fetchVisibleRange: vi.fn(),
    previewMaterialImpact: vi.fn(), submitBoard: vi.fn(), fetchIntents: vi.fn(), approveIntent: vi.fn(),
    rejectIntent: vi.fn(), batchDecide: vi.fn(), createDemoIntents: vi.fn(), advanceIntent: vi.fn(),
    updateIntentPreview: vi.fn(), fetchHistoryPage: vi.fn(), fetchMessages: vi.fn(),
  },
}));
import { api } from "../../services/api";

beforeEach(() => {
  localStorage.clear(); vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

describe("§12.5 主智能体反例：失败原文不做静默淘汰", () => {
  it("同话题连续九次不同原文全部失败：九份都可找回", async () => {
    const session = useSessionStore();
    session.currentTopicId = "T";
    for (let i = 1; i <= 9; i += 1) {
      vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断 " + i));
      session.draft = "失败原文 " + i;
      const attribution = session.sendAttribution();
      void attribution;
      await session.send("失败原文 " + i);
      await flushPromises();
    }
    expect(session.failedSendsForTopic("T").length, "九份未处理的失败原文必须全部保留").toBe(9);
    const texts = session.failedSendsForTopic("T").map((r) => r.text);
    expect(texts).toContain("失败原文 1");
    expect(texts).toContain("失败原文 9");
  });
});
