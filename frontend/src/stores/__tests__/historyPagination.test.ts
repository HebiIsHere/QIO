/**
 * 历史消息渐进加载（spec 第 20 条）。
 *
 * 硬性要求：首屏只加载最近一页；向上读时再加载更早；
 * 不丢消息、不重复、顺序不乱；重新进入 Topic 后不出现重复历史。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

function msg(id: string, content = id) {
  return {
    id,
    role: "user",
    content,
    content_type: "text",
    created_at: `2026-01-01T00:00:${id.slice(-2)}+00:00`,
  };
}

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({})),
    getSessionMessagesBefore: vi.fn(async () => ({})),
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

describe("首次进入 Topic", () => {
  it("只加载最近一页，并记住还有更早的历史", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [msg("m08"), msg("m09")],
      has_more: true,
      next_before: "2026-01-01T00:00:08+00:00|m08",
    } as never);

    const session = setup();
    await session.loadHistory();

    expect(session.messages.map((m) => m.id)).toEqual(["m08", "m09"]);
    expect(session.historyHasMore).toBe(true);
    expect(session.historyCursor).toContain("m08");
  });

  it("没有更早历史时标记 hasMore=false", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [msg("m01")],
      has_more: false,
      next_before: null,
    } as never);

    const session = setup();
    await session.loadHistory();
    expect(session.historyHasMore).toBe(false);
    expect(session.historyCursor).toBeNull();
  });
});

describe("向前翻页", () => {
  it("更早的一页插到最前面，顺序不乱", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [msg("m04"), msg("m05")],
      has_more: true,
      next_before: "c4|m04",
    } as never);
    vi.mocked(api.getSessionMessagesBefore).mockResolvedValueOnce({
      messages: [msg("m02"), msg("m03")],
      has_more: true,
      next_before: "c2|m02",
    } as never);

    const session = setup();
    await session.loadHistory();
    const ok = await session.loadOlderHistory();

    expect(ok).toBe(true);
    expect(session.messages.map((m) => m.id)).toEqual(["m02", "m03", "m04", "m05"]);
    expect(api.getSessionMessagesBefore).toHaveBeenCalledWith("t1", "c4|m04", expect.any(Number));
    expect(session.historyCursor).toBe("c2|m02");
  });

  it("重叠返回的消息不会变成重复条目（新消息到达导致的分页重叠）", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [msg("m03"), msg("m04")],
      has_more: true,
      next_before: "c4|m04",
    } as never);
    vi.mocked(api.getSessionMessagesBefore).mockResolvedValueOnce({
      // 后端这一页把已在本地出现过的 m03/m04 又带回来了
      messages: [msg("m02"), msg("m03"), msg("m04")],
      has_more: false,
      next_before: null,
    } as never);

    const session = setup();
    await session.loadHistory();
    await session.loadOlderHistory();

    expect(session.messages.map((m) => m.id)).toEqual(["m02", "m03", "m04"]);
    expect(session.historyHasMore).toBe(false);
  });

  it("没有更早历史时不发请求", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [msg("m01")],
      has_more: false,
      next_before: null,
    } as never);

    const session = setup();
    await session.loadHistory();
    const ok = await session.loadOlderHistory();

    expect(ok).toBe(false);
    expect(api.getSessionMessagesBefore).not.toHaveBeenCalled();
  });

  it("并发调用只发一次请求（滚动容易连续触发）", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [msg("m04")],
      has_more: true,
      next_before: "c4|m04",
    } as never);
    let release!: (v: unknown) => void;
    vi.mocked(api.getSessionMessagesBefore).mockImplementationOnce(
      () => new Promise((r) => { release = r; }) as never,
    );

    const session = setup();
    await session.loadHistory();
    const first = session.loadOlderHistory();
    const second = session.loadOlderHistory();

    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);
    release({ messages: [msg("m03")], has_more: false, next_before: null });
    await first;
    await second;
  });

  it("重新进入 Topic 会重置分页状态，不残留上一话题的游标", async () => {
    vi.mocked(api.getSessionContext).mockResolvedValue({
      topic_id: "t2",
      topic_name: "新话题",
      anchor_fragment: null,
      messages: [msg("n01")],
      has_more: false,
      next_before: null,
    } as never);

    const session = setup();
    session.historyHasMore = true;
    session.historyCursor = "stale";
    await session.loadHistory();

    expect(session.historyHasMore).toBe(false);
    expect(session.historyCursor).toBeNull();
    expect(session.messages.map((m) => m.id)).toEqual(["n01"]);
  });
});
