/**
 * §5 同步缓冲的有界性与溢出恢复。
 *
 * 受控验证的缺口：`stores/events.ts` 的 `resyncBuffer` 没有上限 —— 同步迟迟不返回时，
 * 实时事件会一直堆着；而且「溢出怎么处理」没有定义（旧实现根本不会溢出，因为无限增长）。
 *
 * 这里钉死三件事：
 * 1. 缓冲有明确上限，不会无限增长，溢出条目数可见；
 * 2. 溢出后必须**再拉一次权威状态**才能宣布同步成功（不许静默丢事件）；
 * 3. 恢复轮失败时如实报失败；同步提示定时器在单飞分支与结束时都清理。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { RESYNC_BUFFER_LIMIT, useEventStore } from "../events";
import type { AgentEvent } from "../../services/events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

const captured = vi.hoisted(() => ({
  onEvent: null as ((event: unknown) => void) | null,
}));

vi.mock("../../services/events", () => ({
  connectEvents: (onEvent: (event: unknown) => void) => {
    captured.onEvent = onEvent;
    return { onopen: null, onerror: null, close: vi.fn() };
  },
  publishTestEvent: vi.fn(async () => undefined),
}));

vi.mock("../../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

function runtimeState() {
  return {
    instance_id: "inst_test",
    revision: 1,
    turn_queue: { instance_id: "inst_test", revision: 1, running: null, queued: [], cancelled: [] },
    approvals: [],
    tasks: [],
    tools: [],
  };
}

/** 一次可以被测试手动放行的权威状态读取。 */
function deferredState(state: unknown) {
  let release!: () => void;
  const promise = new Promise((resolve) => {
    release = () => resolve(state);
  });
  vi.mocked(api.getRuntimeState).mockImplementationOnce(() => promise as never);
  return release;
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function feed(event: Partial<AgentEvent> & { type: string }) {
  captured.onEvent?.({ id: `${event.type}_${Math.random()}`, ts: "", data: {}, ...event });
}

/** 等异步链路推进到条件成立（比数 microtask 次数稳）。 */
async function waitUntil(predicate: () => boolean, turns = 400) {
  for (let i = 0; i < turns; i += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  throw new Error("等待条件超时：microtask 轮数用尽");
}

async function settle(turns = 60) {
  for (let i = 0; i < turns; i += 1) await Promise.resolve();
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("resyncBuffer 有明确上限", () => {
  it("同步期间事件一直到达：缓冲区停在上限，溢出条数可见", async () => {
    const { events } = setup();
    events.connect();
    const release = deferredState(runtimeState());

    feed({ type: "RESYNC" });
    await waitUntil(() => events.resyncing);

    const overflow = 25;
    for (let i = 0; i < RESYNC_BUFFER_LIMIT + overflow; i += 1) {
      feed({ type: "WARNING", data: { message: `警告 ${i}` } });
    }

    expect(events.resyncBuffer.length).toBe(RESYNC_BUFFER_LIMIT);
    expect(events.resyncDroppedEvents).toBe(overflow);

    release();
    await settle();
  });

  it("没有溢出的同步不会被额外加一轮（保持既有单飞语义）", async () => {
    const { events, session } = setup();
    events.connect();
    vi.mocked(api.getRuntimeState).mockResolvedValue(runtimeState() as never);

    feed({ type: "RESYNC" });
    await waitUntil(() => session.resyncState === "normal");

    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);
    expect(events.resyncDroppedEvents).toBe(0);
  });
});

describe("溢出后按权威状态恢复，不许静默丢事件", () => {
  it("溢出 → 再拉一次权威状态，之后才宣布同步成功，缓冲区清空", async () => {
    const { events, session } = setup();
    events.connect();
    const releaseFirst = deferredState(runtimeState());

    feed({ type: "RESYNC" });
    await waitUntil(() => events.resyncing);
    for (let i = 0; i < RESYNC_BUFFER_LIMIT + 3; i += 1) {
      feed({ type: "WARNING", data: { message: `警告 ${i}` } });
    }
    expect(events.resyncDroppedEvents).toBe(3);

    // 恢复轮：权威状态是新的，必须真的再读一次
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce(runtimeState() as never);
    releaseFirst();
    await waitUntil(() => session.resyncState !== "resyncing");

    expect(api.getRuntimeState).toHaveBeenCalledTimes(2);
    expect(session.resyncState).toBe("normal");
    expect(events.resyncBuffer).toEqual([]);
    // 恢复之后的溢出计数不再累计当轮那批（下一轮是干净的）
    expect(events.resyncDroppedEvents).toBe(0);
  });

  it("恢复轮失败 → 如实进入 failed 并保留原因，不宣称已同步", async () => {
    const { events, session } = setup();
    events.connect();
    const releaseFirst = deferredState(runtimeState());

    feed({ type: "RESYNC" });
    await waitUntil(() => events.resyncing);
    for (let i = 0; i < RESYNC_BUFFER_LIMIT + 1; i += 1) {
      feed({ type: "WARNING", data: { message: `警告 ${i}` } });
    }

    vi.mocked(api.getRuntimeState).mockRejectedValueOnce(new Error("network down"));
    releaseFirst();
    await waitUntil(() => session.resyncState !== "resyncing");

    expect(session.resyncState).toBe("failed");
    expect(session.lastError).toContain("network down");
  });
});

describe("同步提示定时器在全部分支清理", () => {
  it("重复 RESYNC 不留下第二个提示定时器；同步结束后归零", async () => {
    vi.useFakeTimers();
    const { events, session } = setup();
    events.connect();
    const release = deferredState(runtimeState());

    feed({ type: "RESYNC" });
    await waitUntil(() => events.resyncing);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);

    feed({ type: "RESYNC" });
    await vi.advanceTimersByTimeAsync(0);
    // 单飞分支不得再造一个没人清的定时器
    expect(vi.getTimerCount()).toBe(1);

    vi.mocked(api.getRuntimeState).mockResolvedValue(runtimeState() as never);
    release();
    await waitUntil(() => session.resyncState !== "resyncing");
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});
