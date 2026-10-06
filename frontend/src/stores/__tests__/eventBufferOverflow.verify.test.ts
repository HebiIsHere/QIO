/**
 * 独立验证（验证方维护，`*.verify.test.ts`）：**同步期间的事件缓存必须有界**（契约 WS1 §5，验收清单第 6 条）。
 *
 * 契约要求：
 *   * `resyncBuffer` 设**明确上限**；
 *   * 溢出时标记「需要重新同步」、按权威状态恢复（必要时重连事件流）；
 *   * **不许静默丢事件还宣称同步成功**。
 *
 * 当前实现里 `route()` 无条件 `push`，没有上限也没有溢出标记 —— 修复前这些用例应当变红。
 *
 * 关于超时（实测口径，不是功能断言的一部分）：
 *   * 单跑：灌 2 万条事件走真实 store + jsdom 约 **7–10 秒**；5 千条约 2–3 秒；
 *   * 整套并行跑（全量 vitest + 其它重用例同时占 CPU）实测可慢到 **3–4 倍**，
 *     2026-10-06 出现过 30s 上限被顶掉的情况 —— 那是**机器负载**，不是功能回归；
 *   * 因此这里把上限放宽到「负载下也够用」：2 万那条 90s、5 千那两条 60s。
 *     **功能断言一个字都不放宽**（上限必须存在、必须标记重新同步、无溢出时一条不丢）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst_v",
      revision: 1,
      approvals: [],
      tasks: [],
      tools: [],
      turn_queue: { running: null, queued: [], cancelled: [], revision: 1 },
    })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

/** 压力用例：2 万条（单跑约 7–10 秒；并行跑可能 3–4 倍慢） */
const OVERFLOW_TOTAL = 20000;
/** 其余用例：5 千条已足够触发多次溢出，且跑得快 */
const OVERFLOW_SMALL = 5000;

/** 负载余量：只为「机器被别的重用例占住」留，不为放宽断言。 */
const TIMEOUT_STRESS_MS = 90000;
const TIMEOUT_NORMAL_MS = 60000;

function event(index: number) {
  return {
    type: "ASSISTANT",
    id: `evt_${index}`,
    ts: "2026-10-05T00:00:00.000Z",
    data: { content: `缓存期间到达的消息 ${index}` },
  } as never;
}

/** 「需要重新同步」的可观察信号（契约只说必须标记，具体字段名由实现决定） */
function needsResyncSignal(events: ReturnType<typeof useEventStore>, session: ReturnType<typeof useSessionStore>) {
  const text = `${session.warning ?? ""} ${session.lastError ?? ""}`;
  return (
    (events as unknown as { resyncAgain?: boolean }).resyncAgain === true ||
    (events as unknown as { _resyncAgain?: boolean })._resyncAgain === true ||
    session.resyncState !== "normal" ||
    /重新同步|同步失败|需要重新/.test(text)
  );
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe("WS1 §5：resyncBuffer 必须有明确上限", () => {
  it("同步期间灌入 2 万条事件：缓存必须有界，不能无限增长", { timeout: TIMEOUT_STRESS_MS }, () => {
    const events = useEventStore();
    const session = useSessionStore();
    events.resyncing = true; // 模拟同步在飞（不真的发请求）
    session.resyncState = "normal"; // 注意：不能预设成 resyncing，否则下面的「溢出信号」断言会变成永真

    for (let i = 0; i < OVERFLOW_TOTAL; i += 1) events.route(event(i));

    expect(events.resyncBuffer.length).toBeLessThan(OVERFLOW_TOTAL);
  });

  it("溢出时必须标记「需要重新同步」，不能静默丢事件", { timeout: TIMEOUT_NORMAL_MS }, () => {
    const events = useEventStore();
    const session = useSessionStore();
    events.resyncing = true;
    session.resyncState = "normal";

    for (let i = 0; i < OVERFLOW_SMALL; i += 1) events.route(event(i));

    // 这个信号必须由**溢出**产生：状态是我在灌之前设成 normal 的
    expect(needsResyncSignal(events, session)).toBe(true);
  });

  it("溢出之后不得宣称已同步（resyncState 不能停在 normal）", { timeout: TIMEOUT_NORMAL_MS }, async () => {
    const events = useEventStore();
    const session = useSessionStore();
    events.resyncing = true;
    session.resyncState = "normal";
    for (let i = 0; i < OVERFLOW_SMALL; i += 1) events.route(event(i));

    // 让同步「结束」：结束之后要么重新同步，要么如实报告需要重新同步
    events.resyncing = false;
    events.flushResyncBuffer();

    const claimedSynced = session.resyncState === "normal" && !needsResyncSignal(events, session);
    expect(claimedSynced).toBe(false);
  });

  it("没有溢出时：缓存事件按到达顺序补放，一条都不丢", () => {
    const events = useEventStore();
    events.resyncing = true;
    events.route(event(1));
    events.route(event(2));
    events.route(event(3));
    expect(events.resyncBuffer.length).toBe(3);

    const dispatched: string[] = [];
    const original = events.dispatch.bind(events);
    events.dispatch = ((evt: { id?: string }) => {
      dispatched.push(String(evt.id));
      return original(evt as never);
    }) as typeof events.dispatch;

    events.resyncing = false;
    events.flushResyncBuffer();

    expect(dispatched).toEqual(["evt_1", "evt_2", "evt_3"]);
    expect(events.resyncBuffer.length).toBe(0);
  });
});

