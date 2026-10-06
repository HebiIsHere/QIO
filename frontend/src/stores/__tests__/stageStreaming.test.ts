/**
 * 契约 §1.3 / §2.1 的前端消费（B 负责的一半）：
 *
 * * STAGE：阶段顺序、阶段内说明、状态（done 是终态）、narrative_id 去重；
 * * ASSISTANT：interim 按**事件字段**决定（旧实现写死 true，把正式回答也标成过程）、
 *   累计快照就地更新、(delta_id, seq) 去重、**同轮多次 interim 不互相覆盖**；
 * * TOOL_START / TOOL_END：stage_id 归属（不靠消息相邻位置）；
 * * TURN_END：duration_ms 记到 turn 上（折叠态显示），final_content 为 null 时不清空已确认文本。
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { currentStageOf, useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({})),
    getRuntimeState: vi.fn(async () => ({
      instance_id: "i",
      revision: 1,
      turn_queue: { instance_id: "i", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
      tools: [],
    })),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function stagePayload(over: Record<string, unknown> = {}) {
  return {
    turn_id: "t1",
    stage_id: "st_ab12_1",
    index: 1,
    status: "running",
    name: "读取仓库结构",
    text: "正在读取仓库结构",
    kind: "progress",
    op: "start",
    narrative_id: "msg_1",
    call_ids: ["call_1"],
    created_at: "2026-10-06T08:00:00+00:00",
    ...over,
  };
}

function assistants(session: ReturnType<typeof useSessionStore>) {
  return session.messages.filter((m) => m.role === "assistant");
}

describe("STAGE：阶段顺序 / 说明 / 状态", () => {
  it("两个阶段按 index 排序，说明落在各自阶段里，当前阶段是还在跑的那个", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({ type: "STAGE", id: "2", ts: "", data: stagePayload() });
    events.route({
      type: "STAGE",
      id: "3",
      ts: "",
      data: stagePayload({
        stage_id: "st_ab12_2",
        index: 2,
        name: "改写文件",
        text: "正在改写 narrative.py",
        op: "next",
        narrative_id: "msg_2",
      }),
    });
    const stages = session.stagesFor("t1");
    expect(stages.map((s) => s.stageId)).toEqual(["st_ab12_1", "st_ab12_2"]);
    expect(stages[0]?.name).toBe("读取仓库结构");
    expect(stages[0]?.notes[0]?.text).toBe("正在读取仓库结构");
    expect(stages[1]?.notes[0]?.text).toBe("正在改写 narrative.py");
    expect(currentStageOf(stages)?.stageId).toBe("st_ab12_2");
  });

  it("done 是终态；同一 narrative_id 重放不会重复出说明", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({ type: "STAGE", id: "2", ts: "", data: stagePayload() });
    // 重放同一事件（断线重连）：说明不重复
    events.route({ type: "STAGE", id: "3", ts: "", data: stagePayload() });
    expect(session.stagesFor("t1")[0]?.notes).toHaveLength(1);
    // 阶段自己结束
    events.route({ type: "STAGE", id: "4", ts: "", data: stagePayload({ status: "done", op: "end" }) });
    expect(session.stagesFor("t1")[0]?.status).toBe("done");
    // 之后迟到的 running 不能把它改回去
    events.route({ type: "STAGE", id: "5", ts: "", data: stagePayload({ status: "running" }) });
    expect(session.stagesFor("t1")[0]?.status).toBe("done");
  });

  it("没有 stage_id 的事件不产生阶段（旧 NARRATIVE 走 legacy 平铺，不伪造阶段）", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({
      type: "NARRATIVE",
      id: "2",
      ts: "",
      data: { narrative_id: "msg_n", turn_id: "t1", kind: "progress", text: "旧记录" },
    });
    expect(session.stagesFor("t1")).toHaveLength(0);
    expect(session.messages.some((m) => m.role === "narrative" && m.content === "旧记录")).toBe(true);
  });

  it("TURN_END 收口还挂着的阶段，并记下 duration_ms / queue_ms", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({ type: "STAGE", id: "2", ts: "", data: stagePayload() });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: {
        turn_id: "t1",
        status: "completed",
        final_content: "完成",
        duration_ms: 12345,
        queue_ms: 100,
        started_at: "2026-10-06T08:00:00+00:00",
        ended_at: "2026-10-06T08:00:12+00:00",
      },
    });
    expect(session.stagesFor("t1").every((s) => s.status === "done")).toBe(true);
    const facts = session.factsFor("t1");
    expect(facts?.durationMs).toBe(12345);
    expect(facts?.queueMs).toBe(100);
    expect(facts?.status).toBe("completed");
  });

  it("TOOL_START / TOOL_END 按 stage_id 归属，并登记进阶段的 call_ids", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({ type: "STAGE", id: "2", ts: "", data: stagePayload() });
    events.route({
      type: "TOOL_START",
      id: "3",
      ts: "",
      data: { turn_id: "t1", call_id: "call_9", tool: "read_file", stage_id: "st_ab12_1" },
    });
    const tool = session.messages.find((m) => m.role === "tool");
    expect(tool?.stageId).toBe("st_ab12_1");
    expect(session.stagesFor("t1")[0]?.callIds).toContain("call_9");
    events.route({
      type: "TOOL_END",
      id: "4",
      ts: "",
      data: { turn_id: "t1", call_id: "call_9", tool: "read_file", ok: true, stage_id: "st_ab12_1" },
    });
    expect(session.messages.filter((m) => m.role === "tool")).toHaveLength(1);
    expect(session.messages.find((m) => m.role === "tool")?.stageId).toBe("st_ab12_1");
  });
});

describe("ASSISTANT：interim 按字段、按 delta 保真、按 seq 去重", () => {
  it("interim 由事件字段决定（正式回答不再被写死成过程）", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { content: "正式回答", interim: false, streaming: true, delta_id: "dl_1", seq: 1 },
    });
    const last = assistants(session)[0];
    expect(last?.interim).not.toBe(true);
    expect(last?.streaming).toBe(true);
    expect(last?.assistantDeltaId).toBe("dl_1");
  });

  it("同一 delta 的累计快照就地更新，seq 回退的事件被丢弃", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({ type: "ASSISTANT", id: "2", ts: "", data: { content: "你好", streaming: true, delta_id: "dl_1", seq: 1 } });
    events.route({ type: "ASSISTANT", id: "3", ts: "", data: { content: "你好，世界", streaming: true, delta_id: "dl_1", seq: 2 } });
    expect(assistants(session)).toHaveLength(1);
    expect(assistants(session)[0]?.content).toBe("你好，世界");
    // 重复 / 迟到的旧 seq：整条丢弃（重连重放不重播、不回退）
    events.route({ type: "ASSISTANT", id: "4", ts: "", data: { content: "旧内容", streaming: true, delta_id: "dl_1", seq: 2 } });
    expect(assistants(session)[0]?.content).toBe("你好，世界");
  });

  it("同一轮里第二次 interim 不会覆盖第一段（task-2 要求修掉的缺陷）", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { content: "我先看看仓库", interim: true, streaming: true, delta_id: "dl_a", seq: 1 },
    });
    events.route({ type: "TOOL_END", id: "3", ts: "", data: { turn_id: "t1", call_id: "c1", tool: "fs_list", ok: true } });
    events.route({
      type: "ASSISTANT",
      id: "4",
      ts: "",
      data: { content: "再看一眼配置", interim: true, streaming: true, delta_id: "dl_b", seq: 1 },
    });
    expect(assistants(session).map((m) => m.content)).toEqual(["我先看看仓库", "再看一眼配置"]);
    // 换 delta 时上一条已经落定（不再假装还在流式）
    expect(assistants(session)[0]?.streaming).toBeUndefined();
    expect(assistants(session)[1]?.streaming).toBe(true);
  });

  it("守卫放行后才发现工具调用：同一段文字从正文改判为过程，不重复、不丢字", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { content: "我先说一句", interim: false, streaming: true, delta_id: "dl_c", seq: 1 },
    });
    events.route({
      type: "ASSISTANT",
      id: "3",
      ts: "",
      data: { content: "我先说一句", interim: true, streaming: true, delta_id: "dl_c", seq: 2 },
    });
    expect(assistants(session)).toHaveLength(1);
    expect(assistants(session)[0]?.interim).toBe(true);
    expect(assistants(session)[0]?.content).toBe("我先说一句");
  });

  it("取消 / 失败（final_content 为 null）不清空已经确认的流式文本", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { content: "已经生成的一段", interim: false, streaming: true, delta_id: "dl_d", seq: 1 },
    });
    events.route({ type: "TURN_END", id: "3", ts: "", data: { turn_id: "t1", status: "cancelled", final_content: null } });
    const last = assistants(session)[0];
    expect(last?.content).toBe("已经生成的一段");
    expect(last?.streaming).toBeUndefined();
    expect(session.turnRunning).toBe(false);
  });
});
