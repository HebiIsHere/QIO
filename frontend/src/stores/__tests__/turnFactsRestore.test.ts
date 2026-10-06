/**
 * 刷新 / 历史恢复之后，失败轮的结束事实与「重试」入口必须还在。
 *
 * 缺陷（D 实机 S6）：失败轮刷新后 processRegions 2→1、retryAfterRefresh=false。
 * 根因（前端这一半）：实时 TURN_END 的 reason/actions 只活在内存里，刷新即丢；
 * 历史恢复路径虽然已经会读后端的 `turn_facts`，但后端今天还没有这个字段
 * （已另报 Lead 请求后端补：历史/分页/RESYNC 各带一份）。
 *
 * 本文件钉两件事：
 * 1) 后端**给了** `turn_facts` 时按它恢复（不得用状态词猜）；
 * 2) 后端**没给**时，用本机记录的**真实 TURN_END 事实**兜底（同一浏览器刷新场景），
 *    旧记录没有这些字段时仍然什么都不显示 —— 绝不伪造原因。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/turnFactsRestore.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { AgentEvent } from "../../services/events";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

const { getSessionContext, getSessionMessagesBefore } = vi.hoisted(() => ({
  getSessionContext: vi.fn(async () => ({ topic_id: "topic_1", topic_name: null, anchor_fragment: null, messages: [] as Record<string, unknown>[] })),
  getSessionMessagesBefore: vi.fn(async () => ({ topic_id: "topic_1", messages: [] as Record<string, unknown>[], tool_records: [], has_more: false, next_before: null })),
}));

vi.mock("../../services/api", () => ({
  api: { getSessionContext, getSessionMessagesBefore },
  ApiError: class ApiError extends Error {},
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "rf_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

/** 历史里那一轮的用户消息（turn_id 是恢复归属的唯一凭据）。 */
function userRow(turnId: string, text = "帮我改这个文件") {
  return {
    id: "msg_user_1",
    role: "user",
    content: text,
    content_type: "text",
    created_at: "2026-10-07T05:00:00+00:00",
    turn_id: turnId,
  };
}

beforeEach(() => {
  seq = 0;
  getSessionContext.mockClear();
  getSessionMessagesBefore.mockClear();
  localStorage.clear();
});

describe("失败轮的事实：历史恢复", () => {
  it("后端历史带 turn_facts 时按它恢复（reason + actions + 重试目标都在）", async () => {
    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: null,
      anchor_fragment: null,
      messages: [userRow("turn_failed")],
      turn_facts: [
        {
          turn_id: "turn_failed",
          status: "failed",
          reason_code: "provider_error",
          reason: "模型服务没有响应（连续 2 次）",
          stopped_by: "system",
          actions: ["retry", "resend"],
        },
      ],
    } as never);
    const { session } = setup();

    await session.loadHistory();

    const facts = session.factsFor("turn_failed");
    expect(facts?.reason).toBe("模型服务没有响应（连续 2 次）");
    expect(facts?.actions).toEqual(["retry", "resend"]);
    // 「重试」要能重发这一轮的用户消息（入口可点）
    expect(session.userMessageFor("turn_failed")?.content).toBe("帮我改这个文件");
  });

  it("旧记录（历史里没有 turn_facts）：不伪造原因，facts 为 null", async () => {
    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: null,
      anchor_fragment: null,
      messages: [userRow("turn_old")],
    } as never);
    const { session } = setup();

    await session.loadHistory();

    expect(session.factsFor("turn_old")).toBeNull();
  });

  it("翻更早的历史（分页）同样恢复 turn_facts", async () => {
    getSessionMessagesBefore.mockResolvedValueOnce({
      topic_id: "topic_1",
      messages: [userRow("turn_older")],
      tool_records: [],
      has_more: false,
      next_before: null,
      turn_facts: [
        { turn_id: "turn_older", status: "cancelled", reason_code: "user_stopped", reason: "你停止了这一轮", actions: ["retry"] },
      ],
    } as never);
    const { session } = setup();
    session.currentTopicId = "topic_1";
    // 分页前置：还有更早的一页 + 游标（否则 loadOlderHistory 直接返回 false）
    session.historyHasMore = true;
    session.historyCursor = "2026-10-07T04:00:00+00:00|msg_user_1";

    const loaded = await session.loadOlderHistory();
    expect(loaded).toBe(true);

    expect(session.factsFor("turn_older")?.reason).toBe("你停止了这一轮");
    expect(session.factsFor("turn_older")?.actions).toEqual(["retry"]);
  });
});

describe("失败轮的事实：本机刷新兜底（同一浏览器）", () => {
  it("实时失败 → 刷新（新 store）后仍然有原因与可用操作，重试入口可解析", async () => {
    // 第一段：这一轮真的失败了，TURN_END 带事实
    const first = setup();
    first.events.route(ev("TURN_START", { turn_id: "turn_live", revision: 1 }));
    first.events.route(
      ev("TURN_END", {
        turn_id: "turn_live",
        status: "failed",
        reason_code: "provider_error",
        reason: "模型服务没有响应（连续 2 次）",
        stopped_by: "system",
        actions: ["retry"],
        error: "upstream 502",
      }),
    );
    expect(first.session.factsFor("turn_live")?.actions).toEqual(["retry"]);

    // 刷新：新的 pinia / 新的 store（localStorage 保留），历史里没有 turn_facts（今天的后端）
    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: null,
      anchor_fragment: null,
      messages: [userRow("turn_live")],
    } as never);
    const second = setup();

    await second.session.loadHistory();

    const facts = second.session.factsFor("turn_live");
    expect(facts, "刷新后失败轮的事实必须还在").not.toBeNull();
    expect(facts?.reason).toBe("模型服务没有响应（连续 2 次）");
    expect(facts?.reasonCode).toBe("provider_error");
    expect(facts?.actions).toEqual(["retry"]);
    expect(second.session.userMessageFor("turn_live")?.content).toBe("帮我改这个文件");
  });

  it("后端历史给了新事实时以它为准（覆盖本机缓存）", async () => {
    const first = setup();
    first.events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    first.events.route(
      ev("TURN_END", { turn_id: "turn_x", status: "failed", reason: "旧的失败原因", actions: ["retry"] }),
    );

    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: null,
      anchor_fragment: null,
      messages: [userRow("turn_x")],
      turn_facts: [
        { turn_id: "turn_x", status: "completed", reason_code: "none", reason: "", actions: [] },
      ],
    } as never);
    const second = setup();
    await second.session.loadHistory();

    const facts = second.session.factsFor("turn_x");
    expect(facts?.status).toBe("completed");
    expect(facts?.actions).toEqual([]);
  });

  it("没有事实的轮次不会从缓存里凭空长出来", () => {
    const { session } = setup();
    expect(session.factsFor("turn_never_seen")).toBeNull();
  });
});
