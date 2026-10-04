/**
 * §4 旧请求不得影响当前页面。
 *
 * 这里回归的是**受控乱序**下的真实缺口：
 * - 慢的旧完整历史迟到 → 不许把话题/消息/状态改回去（成功、错误两条路径都要判）；
 * - 旧分页跨话题迟到 → 不许串进新话题的列表、不许改游标、不许解锁新分页（finally 用 token 归属）；
 * - 连接后的并发刷新（开发任务 / 授权列表）→ 旧结果、旧实例的结果不得变成当前状态。
 *
 * 每个用例都是「先发起慢的、再让新的落地、最后放行旧的」，不依赖真实网络的时序。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(),
    getSessionMessagesBefore: vi.fn(),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

const getSessionContext = vi.mocked(api.getSessionContext);
const getSessionMessagesBefore = vi.mocked(api.getSessionMessagesBefore);
const getDevTasks = vi.mocked(api.getDevTasks);
const listDevAuthorizations = vi.mocked(api.listDevAuthorizations);

function msg(id: string) {
  return {
    id,
    role: "user",
    content: id,
    content_type: "text",
    created_at: `2026-01-01T00:00:${id.slice(-2)}+00:00`,
  };
}

function ctx(topicId: string, ids: string[], extra: Record<string, unknown> = {}) {
  return {
    topic_id: topicId,
    topic_name: `话题${topicId}`,
    anchor_fragment: null,
    messages: ids.map(msg),
    has_more: false,
    next_before: null,
    ...extra,
  };
}

function page(ids: string[], extra: Record<string, unknown> = {}) {
  return { messages: ids.map(msg), has_more: false, next_before: null, ...extra };
}

function devRow(id: string) {
  return {
    id,
    request: `任务 ${id}`,
    phase: "building",
    submitted: false,
    test_passed: null,
    test_evidence_current: true,
    updated_at: "2026-10-01T00:00:00+00:00",
    authorized: false,
    abandoned: false,
    abandoned_at: null,
  };
}

/** 可以被测试手动放行的异步结果。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useSessionStore();
}

beforeEach(() => {
  vi.clearAllMocks();
  getDevTasks.mockResolvedValue({ tasks: [] } as never);
  listDevAuthorizations.mockResolvedValue({ authorizations: [] } as never);
});

describe("完整历史：旧请求迟到不得改当前状态", () => {
  it("话题 A 的历史迟到（B 已落地）→ 整段丢弃，话题/标题/消息都还是 B", async () => {
    const slowA = deferred<unknown>();
    getSessionContext.mockImplementationOnce(() => slowA.promise as never);
    const session = setup();

    const loadingA = session.setAnchorAndSync("A", null, "话题A");
    getSessionContext.mockResolvedValueOnce(ctx("B", ["b1", "b2"]) as never);
    await session.setAnchorAndSync("B", null, "话题B");

    expect(session.currentTopicId).toBe("B");
    expect(session.messages.map((m) => m.id)).toEqual(["b1", "b2"]);

    slowA.resolve(ctx("A", ["a1", "a2"])); // A 迟到
    await loadingA;

    expect(session.currentTopicId).toBe("B");
    expect(session.topicName).toBe("话题B");
    expect(session.messages.map((m) => m.id)).toEqual(["b1", "b2"]);
    expect(session.history.status).toBe("ready");
  });

  it("旧历史的失败迟到 → 不把新话题标成 error（成功/错误都要判归属）", async () => {    const slowA = deferred<unknown>();
    getSessionContext.mockImplementationOnce(() => slowA.promise as never);
    const session = setup();

    const loadingA = session.setAnchorAndSync("A", null, "话题A");
    getSessionContext.mockResolvedValueOnce(ctx("B", ["b1"]) as never);
    await session.setAnchorAndSync("B", null, "话题B");

    slowA.reject(new Error("A 的历史读取失败"));
    await loadingA;

    expect(session.history.status).toBe("ready");
    expect(session.history.error).toBeNull();
    expect(session.currentTopicId).toBe("B");
    expect(session.messages.map((m) => m.id)).toEqual(["b1"]);
  });

  it("快照期间新到的乐观消息不能被快照抹掉（同话题）", async () => {
    const slow = deferred<unknown>();
    getSessionContext.mockImplementationOnce(() => slow.promise as never);
    const session = setup();

    const loading = session.loadHistory();
    session.pushUser("快照期间发出的消息"); // 快照还在路上时用户发了句话
    slow.resolve(ctx("A", ["a1"]));
    await loading;

    expect(session.messages.map((m) => m.id)).toContain("a1");
    expect(session.messages.some((m) => m.content === "快照期间发出的消息")).toBe(true);
    // 它比快照更新，应该排在快照之后
    expect(session.messages[session.messages.length - 1].content).toBe("快照期间发出的消息");
  });
});

describe("向前分页：旧分页迟到不得串进新话题", () => {
  it("A 的分页迟到 → 不串列表、不改游标、不解锁新分页（finally 用 token 归属）", async () => {
    const session = setup();

    // 话题 A：首屏有更早的历史，游标 curA
    getSessionContext.mockResolvedValueOnce(
      ctx("A", ["a9"], { has_more: true, next_before: "curA" }) as never,
    );
    await session.setAnchorAndSync("A", null, "话题A");

    const slowPageA = deferred<unknown>();
    getSessionMessagesBefore.mockImplementationOnce(() => slowPageA.promise as never);
    const pagingA = session.loadOlderHistory(); // A 的分页在飞
    expect(session.historyOlderLoading).toBe(true);

    // 切到 B：重新加载，分页状态一起重置
    getSessionContext.mockResolvedValueOnce(
      ctx("B", ["b1"], { has_more: true, next_before: "curB" }) as never,
    );
    await session.setAnchorAndSync("B", null, "话题B");
    expect(session.historyCursor).toBe("curB");

    const slowPageB = deferred<unknown>();
    getSessionMessagesBefore.mockImplementationOnce(() => slowPageB.promise as never);
    const pagingB = session.loadOlderHistory(); // B 的分页在飞
    expect(getSessionMessagesBefore).toHaveBeenLastCalledWith("B", "curB", expect.any(Number));

    // A 的旧分页迟到：整段丢弃
    slowPageA.resolve(page(["a1", "a2"], { has_more: true, next_before: "curA2" }));
    await pagingA;

    expect(session.messages.map((m) => m.id)).toEqual(["b1"]);
    expect(session.historyCursor).toBe("curB");
    expect(session.historyHasMore).toBe(true);
    // 旧 finally 不许解锁**新**分页
    expect(session.historyOlderLoading).toBe(true);

    slowPageB.resolve(page(["b0"], { has_more: false, next_before: null }));
    await pagingB;

    expect(session.messages.map((m) => m.id)).toEqual(["b0", "b1"]);
    expect(session.historyHasMore).toBe(false);
    expect(session.historyOlderLoading).toBe(false);
  });

  it("旧分页的错误迟到 → 不把它的失败写进新话题（lastError 不动）", async () => {
    const session = setup();
    getSessionContext.mockResolvedValueOnce(
      ctx("A", ["a9"], { has_more: true, next_before: "curA" }) as never,
    );
    await session.setAnchorAndSync("A", null, "话题A");

    const slowPageA = deferred<unknown>();
    getSessionMessagesBefore.mockImplementationOnce(() => slowPageA.promise as never);
    const pagingA = session.loadOlderHistory();

    getSessionContext.mockResolvedValueOnce(
      ctx("B", ["b1"], { has_more: false, next_before: null }) as never,
    );
    await session.setAnchorAndSync("B", null, "话题B");
    session.lastError = null;

    slowPageA.reject(new Error("A 的更早历史读取失败"));
    await pagingA;

    expect(session.lastError).toBeNull();
    expect(session.messages.map((m) => m.id)).toEqual(["b1"]);
  });
});

describe("连接后的并发刷新：旧结果不得变成当前状态", () => {
  it("两次 refreshDevTasks 乱序返回 → 只认最后发起的那次", async () => {
    const slowOld = deferred<unknown>();
    getDevTasks.mockImplementationOnce(() => slowOld.promise as never);
    const session = setup();

    const first = session.refreshDevTasks();
    getDevTasks.mockResolvedValueOnce({ tasks: [devRow("ws_new")] } as never);
    await session.refreshDevTasks();
    expect(session.devTasks.map((t) => t.id)).toEqual(["ws_new"]);

    slowOld.resolve({ tasks: [devRow("ws_old")] });
    await first;

    expect(session.devTasks.map((t) => t.id)).toEqual(["ws_new"]);
  });

  it("后端实例变了 → 旧实例的开发任务列表不得写进新实例状态", async () => {
    const slowOld = deferred<unknown>();
    getDevTasks.mockImplementationOnce(() => slowOld.promise as never);
    const session = setup();
    session.adoptInstance("inst_A");

    const first = session.refreshDevTasks();
    session.adoptInstance("inst_B"); // 后端重启

    slowOld.resolve({ tasks: [devRow("ws_from_A")] });
    await first;

    expect(session.devTasks).toEqual([]);
  });

  it("两次 refreshDevAuthorizations 乱序返回 → 只认最后发起的那次", async () => {
    const slowOld = deferred<unknown>();
    listDevAuthorizations.mockImplementationOnce(() => slowOld.promise as never);
    const session = setup();

    const first = session.refreshDevAuthorizations();
    listDevAuthorizations.mockResolvedValueOnce({
      authorizations: [{ task_id: "ws_new" }],
    } as never);
    await session.refreshDevAuthorizations();
    expect(session.devAuthorizations.map((a) => a.task_id)).toEqual(["ws_new"]);

    slowOld.resolve({ authorizations: [{ task_id: "ws_old" }] });
    await first;

    expect(session.devAuthorizations.map((a) => a.task_id)).toEqual(["ws_new"]);
  });
});
