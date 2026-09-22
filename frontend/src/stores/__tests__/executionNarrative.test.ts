/**
 * Execution Narrative（spec 2026-09-22）：
 *
 * * 叙事是独立消息 role，不参与最终回答合并；
 * * 去重按 `narrative_id`（= 后端落库的消息 id）：重连补发、历史重放都只留一条；
 * * 抽屉分组：一行叙事收纳它之后、下一行叙事之前的工具卡。
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore, groupTurnItems, type StreamMessage } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({ ok: true })),
    getRuntimeState: vi.fn(),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

const NARRATIVE = {
  narrative_id: "msg_1",
  turn_id: "turn_1",
  kind: "announce",
  text: "我先确认审批请求从后端到前端的完整路径。",
  tool: "grep_search",
  call_id: "c1",
  call_ids: ["c1", "c2"],
  created_at: "2026-09-22T09:41:09+00:00",
};

describe("Execution Narrative 事件", () => {
  it("NARRATIVE 事件按 narrative_id 去重（重连补发不重复）", () => {
    const { events, session } = setup();
    events.dispatch({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_1" } });
    events.dispatch({ type: "NARRATIVE", id: "e2", ts: "", data: NARRATIVE });
    events.dispatch({ type: "NARRATIVE", id: "e3", ts: "", data: NARRATIVE });
    const narratives = session.messages.filter((m) => m.role === "narrative");
    expect(narratives).toHaveLength(1);
    expect(narratives[0].content).toBe(NARRATIVE.text);
    expect(narratives[0].narrativeKind).toBe("announce");
    expect(narratives[0].narrativeCallIds).toEqual(["c1", "c2"]);
  });

  it("子任务的叙事不进主对话", () => {
    const { events, session } = setup();
    events.dispatch({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_1" } });
    events.dispatch({
      type: "NARRATIVE",
      id: "e2",
      ts: "",
      data: { ...NARRATIVE, narrative_id: "msg_9", turn_id: "subagent:task_1" },
    });
    expect(session.messages.some((m) => m.role === "narrative")).toBe(false);
  });

  it("同一条叙事重放多次也只出现一次（内容级去重兜底）", () => {
    const { events, session } = setup();
    events.dispatch({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_1" } });
    events.dispatch({
      type: "NARRATIVE",
      id: "e2",
      ts: "",
      data: { ...NARRATIVE, narrative_id: "msg_2" },
    });
    events.dispatch({
      type: "NARRATIVE",
      id: "e3",
      ts: "",
      data: { ...NARRATIVE, narrative_id: "msg_3" },
    });
    expect(session.messages.filter((m) => m.role === "narrative")).toHaveLength(1);
  });
});

describe("叙事抽屉分组", () => {
  function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
    return {
      content: "",
      contentType: "text",
      createdAt: "2026-09-22T09:41:00+00:00",
      ...partial,
    } as StreamMessage;
  }

  it("叙事行收纳它之后、下一行叙事之前的工具卡", () => {
    const items = [
      msg({ id: "n1", role: "narrative", narrativeCallIds: ["c1", "c2"] }),
      msg({ id: "t1", role: "tool", callId: "c1" }),
      msg({ id: "t2", role: "tool", callId: "c2" }),
      msg({ id: "n2", role: "narrative", narrativeCallIds: ["c3"] }),
      msg({ id: "t3", role: "tool", callId: "c3" }),
    ];
    const groups = groupTurnItems(items);
    expect(groups.map((g) => g.kind)).toEqual(["stage", "stage"]);
    const first = groups[0];
    const second = groups[1];
    if (first.kind !== "stage" || second.kind !== "stage") throw new Error("expected stages");
    expect(first.calls.map((c) => c.id)).toEqual(["t1", "t2"]);
    expect(second.calls.map((c) => c.id)).toEqual(["t3"]);
  });

  it("轮次开头的无声调用不挂在任何叙事下", () => {
    const groups = groupTurnItems([
      msg({ id: "t1", role: "tool", callId: "c9" }),
      msg({ id: "n1", role: "narrative" }),
    ]);
    expect(groups[0].kind).toBe("loose");
  });

  it("叙事之后的独立任务卡与创建卡留在抽屉里", () => {
    const groups = groupTurnItems([
      msg({ id: "n1", role: "narrative" }),
      msg({ id: "s1", role: "subagent", taskId: "task_1" }),
    ]);
    const stage = groups[0];
    if (stage.kind !== "stage") throw new Error("expected stage");
    expect(stage.calls.map((c) => c.id)).toEqual(["s1"]);
  });
});
