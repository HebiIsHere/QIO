/**
 * 工具调用历史（前端）：
 * 历史加载把记录插进对应轮次，展开卡片时才取全文（参数 + 输出），
 * 取失败给原因、可重试；已清理 / 未保存有明确文案。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

function msg(id: string, role: string, content: string, at: string) {
  return { id, role, content, content_type: "text", created_at: at, turn_id: "turn_t1" };
}

function record(over: Record<string, unknown> = {}) {
  return {
    id: "tr1",
    turn_id: "turn_t1",
    call_id: "c1",
    seq: 1,
    tool_name: "fs_list",
    title: "列出目录",
    status: "failed",
    error: "列目录失败：找不到路径",
    duration_ms: 7,
    truncated: false,
    output_missing: false,
    missing_reason: "",
    output_chars: 12,
    preview: "列目录失败：找不到路径",
    created_at: "2026-09-24T10:00:03+00:00",
    ...over,
  };
}

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({})),
    getSessionMessagesBefore: vi.fn(async () => ({})),
    getToolRecord: vi.fn(async () => ({})),
    sendTurn: vi.fn(async () => ({})),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useSessionStore();
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("历史里的工具卡", () => {
  it("插在对应轮次的时间位置上，并带上状态与失败原因", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [
        msg("m1", "user", "帮我查", "2026-09-24T10:00:00+00:00"),
        msg("m2", "assistant", "查完了", "2026-09-24T10:00:05+00:00"),
      ],
      tool_records: [record()],
      has_more: false,
      next_before: null,
    } as never);

    const session = setup();
    await session.loadHistory();

    expect(session.messages.map((m) => m.role)).toEqual(["user", "tool", "assistant"]);
    const card = session.messages[1];
    expect(card.toolRecordId).toBe("tr1");
    expect(card.toolStatus).toBe("failed");
    expect(card.toolError).toBe("列目录失败：找不到路径");
    expect(card.toolDurationMs).toBe(7);
    expect(card.presentation?.title).toBe("列出目录");
    expect(card.content).toBe("列目录失败：找不到路径");   // 先显示预览
    expect(card.toolRecordLoaded).toBe(false);             // 全文还没取
  });

  it("跨页边界的同一条记录不会出现两次", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [
        msg("m1", "user", "帮我查", "2026-09-24T10:00:00+00:00"),
        msg("m2", "assistant", "查完了", "2026-09-24T10:00:05+00:00"),
      ],
      tool_records: [record()],
      has_more: true,
      next_before: "2026-09-24T10:00:00+00:00|m1",
    } as never);
    vi.mocked(api.getSessionMessagesBefore).mockResolvedValueOnce({
      topic_id: "t1",
      messages: [],
      tool_records: [record()],
      has_more: false,
      next_before: null,
    } as never);

    const session = setup();
    await session.loadHistory();
    await session.loadOlderHistory();

    expect(session.messages.filter((m) => m.role === "tool")).toHaveLength(1);
  });
});

describe("展开时取全文", () => {
  async function withCard() {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [msg("m1", "user", "帮我查", "2026-09-24T10:00:00+00:00")],
      tool_records: [record()],
      has_more: false,
      next_before: null,
    } as never);
    const session = setup();
    await session.loadHistory();
    return { session, cardId: session.messages[1].id };
  }

  it("取到全文后写回同一条消息：参数与输出都在", async () => {
    vi.mocked(api.getToolRecord).mockResolvedValueOnce({
      ...record(),
      arguments: { path: "." },
      output: "完整输出：目录为空",
    } as never);

    const { session, cardId } = await withCard();
    await session.loadToolRecord(cardId);

    expect(api.getToolRecord).toHaveBeenCalledWith("tr1");
    const card = session.messages[1];
    expect(card.content).toBe("完整输出：目录为空");
    expect(card.toolArgs).toContain('"path"');
    expect(card.toolRecordLoaded).toBe(true);
    expect(card.toolRecordError).toBe(null);
  });

  it("取全文失败：给出原因，不假装已加载", async () => {
    vi.mocked(api.getToolRecord).mockRejectedValueOnce(new Error("读取失败：404"));

    const { session, cardId } = await withCard();
    await session.loadToolRecord(cardId);

    const card = session.messages[1];
    expect(card.toolRecordLoaded).toBe(false);
    expect(card.toolRecordError).toContain("404");
    expect(card.content).toBe("列目录失败：找不到路径");   // 预览仍在
  });

  it("输出被保留期清理：标记原因，但保留已有预览", async () => {
    vi.mocked(api.getToolRecord).mockResolvedValueOnce({
      ...record(),
      arguments: { path: "." },
      output: "",
      output_missing: true,
      missing_reason: "retention",
    } as never);

    const { session, cardId } = await withCard();
    await session.loadToolRecord(cardId);

    const card = session.messages[1];
    expect(card.toolOutputMissing).toBe(true);
    expect(card.toolMissingReason).toBe("retention");
    expect(card.content).toBe("列目录失败：找不到路径");   // 预览不被清空
    expect(card.toolArgs).toContain('"path"');
  });

  it("已经取过全文的卡片不重复请求", async () => {
    vi.mocked(api.getToolRecord).mockResolvedValue({
      ...record(),
      arguments: {},
      output: "完整输出",
    } as never);

    const { session, cardId } = await withCard();
    await session.loadToolRecord(cardId);
    await session.loadToolRecord(cardId);

    expect(api.getToolRecord).toHaveBeenCalledTimes(1);
  });
});
