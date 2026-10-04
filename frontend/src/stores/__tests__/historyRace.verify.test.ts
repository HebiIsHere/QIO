/**
 * 独立验证（验证方维护，`*.verify.test.ts`，不覆盖实现方的 `historyRace.test.ts`）：
 * **旧请求不得影响当前页面**（契约 WS1 §4，验收清单第 3 条）。
 *
 * 断言只依据冻结契约 `_JANK-RACE-CONTRACT.md`：
 *   * 提交结果前必须校验「请求代次 === 当前代次」**且**「话题 === 开始时的话题」；
 *     成功、错误、`finally` 三条路径都要判；
 *   * 旧分页的 `finally` 不得解锁新分页（`historyOlderLoading` 要按归属）；
 *   * 与 `currentTopicId` 相关的游标（`historyCursor` / `historyHasMore`）只在归属成立时更新；
 *   * 快照期间到达的实时消息/乐观消息不得因快照替换而消失。
 *
 * 这些用例在**修复前应当变红**（当前实现提交前没有任何归属校验）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(),
    getSessionMessagesBefore: vi.fn(),
    sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "t1", status: "accepted" })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: null })),
    getTurnQueue: vi.fn(async () => ({ running: null, queued: [], cancelled: [], revision: 1 })),
    getRuntimeState: vi.fn(async () => ({ instance_id: "inst_v", revision: 1, approvals: [], tasks: [], tools: [] })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

const getSessionContext = api.getSessionContext as unknown as ReturnType<typeof vi.fn>;
const getSessionMessagesBefore = api.getSessionMessagesBefore as unknown as ReturnType<typeof vi.fn>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function message(id: string, topicHint = "") {
  return {
    id,
    role: "user",
    content: `消息 ${id}${topicHint}`,
    content_type: "text",
    created_at: "2026-10-05T00:00:00.000Z",
  };
}

function context(topicId: string, ids: string[], extra: Record<string, unknown> = {}) {
  return {
    topic_id: topicId,
    topic_name: `话题 ${topicId}`,
    anchor_fragment: null,
    has_more: false,
    next_before: null,
    messages: ids.map((id) => message(id, `（${topicId}）`)),
    tool_records: [],
    ...extra,
  };
}

function page(ids: string[], extra: Record<string, unknown> = {}) {
  return {
    messages: ids.map((id) => message(id)),
    tool_records: [],
    has_more: false,
    next_before: null,
    ...extra,
  };
}

function ids(store: ReturnType<typeof useSessionStore>) {
  return store.messages.map((m) => m.id);
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe("WS1：慢的旧完整历史不得覆盖新话题（§4）", () => {
  it("B 先完成、A 后完成：当前话题与消息必须还是 B 的", async () => {
    const store = useSessionStore();
    const slowA = deferred<ReturnType<typeof context>>();
    const fastB = deferred<ReturnType<typeof context>>();
    getSessionContext.mockReturnValueOnce(slowA.promise).mockReturnValueOnce(fastB.promise);

    const first = store.loadHistory(); // A（慢）
    const second = store.loadHistory(); // B（快）
    fastB.resolve(context("topic_B", ["b1"]));
    await second;
    slowA.resolve(context("topic_A", ["a1"]));
    await first;

    expect(store.currentTopicId).toBe("topic_B");
    expect(ids(store)).toEqual(["b1"]);
    expect(store.history.status).toBe("ready");
  });

  it("旧请求失败晚到：不得把新话题的 history 置成 error", async () => {
    const store = useSessionStore();
    const slowA = deferred<ReturnType<typeof context>>();
    const fastB = deferred<ReturnType<typeof context>>();
    getSessionContext.mockReturnValueOnce(slowA.promise).mockReturnValueOnce(fastB.promise);

    const first = store.loadHistory();
    const second = store.loadHistory();
    fastB.resolve(context("topic_B", ["b1"]));
    await second;
    slowA.reject(new Error("旧请求失败"));
    await first;

    expect(store.history.status).toBe("ready");
    expect(store.history.error).toBeNull();
    expect(store.currentTopicId).toBe("topic_B");
  });

  it("retryHistory 走同一条状态机，同样要判归属", async () => {
    const store = useSessionStore();
    const slowA = deferred<ReturnType<typeof context>>();
    getSessionContext.mockReturnValueOnce(slowA.promise);
    const retrying = store.retryHistory();
    const fastB = deferred<ReturnType<typeof context>>();
    getSessionContext.mockReturnValueOnce(fastB.promise);
    const second = store.loadHistory();
    fastB.resolve(context("topic_B", ["b1"]));
    await second;
    slowA.resolve(context("topic_A", ["a1"]));
    await retrying;

    expect(store.currentTopicId).toBe("topic_B");
    expect(ids(store)).toEqual(["b1"]);
  });

  it("离开视图（store 重置）之后，旧请求不得把状态写回来", async () => {
    const store = useSessionStore();
    const slowA = deferred<ReturnType<typeof context>>();
    getSessionContext.mockReturnValueOnce(slowA.promise);
    const loading = store.loadHistory();
    store.$reset(); // 代理「离开视图 / 卸载」：状态被清空
    slowA.resolve(context("topic_A", ["a1"]));
    await loading;

    expect(store.currentTopicId).toBeNull();
    expect(store.messages).toEqual([]);
  });
});

describe("WS1：旧分页不得串进新话题、不得改游标（§4）", () => {
  it("A 的旧分页在切到 B 之后返回：消息与游标都必须还是 B 的", async () => {
    const store = useSessionStore();
    getSessionContext.mockResolvedValueOnce(
      context("topic_A", ["a2"], { has_more: true, next_before: "cursor_A" }),
    );
    await store.loadHistory();
    expect(store.historyCursor).toBe("cursor_A");

    const oldPage = deferred<ReturnType<typeof page>>();
    getSessionMessagesBefore.mockReturnValueOnce(oldPage.promise);
    const older = store.loadOlderHistory(); // 旧分页（用 A 的游标）
    expect(store.historyOlderLoading).toBe(true);

    getSessionContext.mockResolvedValueOnce(
      context("topic_B", ["b1"], { has_more: true, next_before: "cursor_B" }),
    );
    await store.loadHistory(); // 切到 B

    oldPage.resolve(page(["a1"], { has_more: true, next_before: "cursor_A_older" }));
    await older;

    expect(ids(store)).toEqual(["b1"]); // 没有把 a1 串进来
    expect(store.historyCursor).toBe("cursor_B"); // 游标没有被旧分页改掉
    expect(store.historyHasMore).toBe(true);
    expect(store.currentTopicId).toBe("topic_B");
  });

  it("旧分页的 finally 不得解锁新分页（historyOlderLoading 要按归属）", async () => {
    const store = useSessionStore();
    getSessionContext.mockResolvedValueOnce(
      context("topic_A", ["a2"], { has_more: true, next_before: "cursor_A" }),
    );
    await store.loadHistory();

    const oldPage = deferred<ReturnType<typeof page>>();
    getSessionMessagesBefore.mockReturnValueOnce(oldPage.promise);
    const older = store.loadOlderHistory(); // A 的分页在飞

    getSessionContext.mockResolvedValueOnce(
      context("topic_B", ["b1"], { has_more: true, next_before: "cursor_B" }),
    );
    await store.loadHistory(); // 切到 B：新话题的分页状态被重置

    const newPage = deferred<ReturnType<typeof page>>();
    getSessionMessagesBefore.mockReturnValueOnce(newPage.promise);
    const newer = store.loadOlderHistory(); // B 的分页真的发出去了
    expect(getSessionMessagesBefore).toHaveBeenCalledTimes(2);

    // 旧分页（失败）晚到 → 它的 finally 不能把新分页的 loading 解锁
    oldPage.reject(new Error("旧分页失败"));
    await older;
    expect(store.historyOlderLoading).toBe(true);

    newPage.resolve(page(["b0"], { has_more: false, next_before: null }));
    await newer;
    expect(store.historyOlderLoading).toBe(false);
    expect(ids(store)).toEqual(["b0", "b1"]);
  });
});

describe("WS1：快照期间到达的新消息不得消失（§4）", () => {
  it("同话题：加载快照期间推入的用户消息，快照应用后仍然在", async () => {
    const store = useSessionStore();
    const slow = deferred<ReturnType<typeof context>>();
    getSessionContext.mockReturnValueOnce(slow.promise);
    const loading = store.loadHistory();
    store.pushUser("快照期间发出的消息"); // 乐观消息（同一话题）
    slow.resolve(context("topic_A", ["a1"]));
    await loading;

    expect(ids(store)).toContain("a1");
    expect(store.messages.some((m) => m.content === "快照期间发出的消息")).toBe(true);
  });
});
