/**
 * A（后端）问题 2 的前端一半（Lead 追加契约 task-6）：
 *
 * 1. 同一个 delta_id 允许 interim=true → 正式回答的**提升**（同一条消息、不重打、不重复）；
 *    正式回答 → 过程区**永远不允许**。
 * 2. 事件里**显式** stage_id=null 的中间话不得回退到「到达时的当前阶段」；
 *    带 stage_id 的同一 delta_id 快照到达后就地归位（不新增消息、不新增阶段）。
 * 3. 字段缺失（旧后端）仍回落到当前阶段 —— 兼容不回归。
 *
 * 修复前红：pushAssistant 只允许「正文 → 过程」方向、null 会回退到当前阶段。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/assistantPromotion.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { AgentEvent } from "../../services/events";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: { getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })) },
  ApiError: class ApiError extends Error {},
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "evt_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

type Session = ReturnType<typeof useSessionStore>;

function assistants(session: Session) {
  return session.messages.filter((m) => m.role === "assistant");
}

function noteTexts(session: Session, turnId: string): string[] {
  return (session.stagesByTurn[turnId] ?? []).flatMap((s) => s.notes.map((n) => n.text));
}

/** 先开一轮并建一个正在跑的阶段（显式 null 的对照物）。 */
function startTurnWithStage(events: ReturnType<typeof useEventStore>, session: Session, turnId = "turn_1") {
  events.route(ev("TURN_START", { turn_id: turnId, revision: 1 }));
  events.route(
    ev("STAGE", {
      turn_id: turnId,
      stage_id: "st_1",
      index: 1,
      status: "running",
      name: "读取仓库结构",
      text: "正在读取仓库结构",
      kind: "progress",
      op: "start",
      narrative_id: "n_1",
      created_at: "2026-10-06T08:00:00+00:00",
    }),
  );
  void session;
}

beforeEach(() => {
  seq = 0;
});

describe("问题 2 前端：同一 delta_id 的 interim → 正式回答提升", () => {
  it("提升是同一条消息：不新增、内容取累计全文、不再是过程说明", () => {
    const { events, session } = setup();
    startTurnWithStage(events, session);

    events.route(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: "前半句",
        interim: true,
        streaming: true,
        delta_id: "dl_1",
        seq: 1,
        stage_id: "st_1",
      }),
    );
    expect(assistants(session)).toHaveLength(1);
    expect(noteTexts(session, "turn_1")).toContain("前半句");

    events.route(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: "前半句，再加后半句。",
        interim: false,
        streaming: false,
        delta_id: "dl_1",
        seq: 2,
        stage_id: null,
      }),
    );

    const list = assistants(session);
    expect(list, "提升不能新建第二条（同一 delta_id 是同一条消息）").toHaveLength(1);
    expect(list[0]?.content).toBe("前半句，再加后半句。");
    expect(list[0]?.interim, "提升后是正式回答").not.toBe(true);
    expect(list[0]?.streaming).toBeFalsy();
    // 同一段文字不能既在过程区（阶段说明）又在正文区
    expect(noteTexts(session, "turn_1")).not.toContain("前半句");
  });

  it("补发的累计快照可以带同一个 seq：提升不能被去重挡掉", () => {
    const { events, session } = setup();
    startTurnWithStage(events, session);

    events.route(
      ev("ASSISTANT", { turn_id: "turn_1", content: "第一版", interim: true, streaming: true, delta_id: "dl_s", seq: 1, stage_id: "st_1" }),
    );
    events.route(
      ev("ASSISTANT", { turn_id: "turn_1", content: "第一版（完整）", interim: false, streaming: false, delta_id: "dl_s", seq: 1, stage_id: null }),
    );

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(list[0]?.content).toBe("第一版（完整）");
    expect(list[0]?.interim).not.toBe(true);
  });

  it("正式回答 → 过程区永远不允许：同 delta 的后续 interim 一律忽略", () => {
    const { events, session } = setup();
    startTurnWithStage(events, session);

    events.route(
      ev("ASSISTANT", { turn_id: "turn_1", content: "正式回答", interim: false, streaming: true, delta_id: "dl_f", seq: 1 }),
    );
    events.route(
      ev("ASSISTANT", { turn_id: "turn_1", content: "正式回答", interim: true, streaming: true, delta_id: "dl_f", seq: 2, stage_id: "st_1" }),
    );

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(list[0]?.interim, "已进入正文区的文字不得被移回过程区").not.toBe(true);
    expect(noteTexts(session, "turn_1")).toEqual(["正在读取仓库结构"]);
  });
});

describe("问题 2 前端：显式 stage_id=null 不回退到当前阶段", () => {
  it("显式 null：先按未归属渲染，阶段说明里不留这条文字", () => {
    const { events, session } = setup();
    startTurnWithStage(events, session);

    events.route(
      ev("ASSISTANT", { turn_id: "turn_1", content: "先想想", interim: true, streaming: true, delta_id: "dl_x", seq: 1, stage_id: null }),
    );

    const a = assistants(session)[0];
    expect(a?.stageId ?? "", "显式 null 不能猜成「到达时的当前阶段」").toBe("");
    expect(noteTexts(session, "turn_1")).toEqual(["正在读取仓库结构"]);
  });

  it("带 stage_id 的同一 delta 快照到达后就地归位：同一条消息、阶段里只留一条说明", () => {
    const { events, session } = setup();
    startTurnWithStage(events, session);

    events.route(
      ev("ASSISTANT", { turn_id: "turn_1", content: "先想想", interim: true, streaming: true, delta_id: "dl_x", seq: 1, stage_id: null }),
    );
    events.route(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: "先想想，再看入口",
        interim: true,
        streaming: true,
        delta_id: "dl_x",
        seq: 2,
        stage_id: "st_1",
        call_ids: ["c1"],
      }),
    );

    const list = assistants(session);
    expect(list, "归位是就地更新，不新增消息").toHaveLength(1);
    expect(list[0]?.stageId).toBe("st_1");
    expect(noteTexts(session, "turn_1")).toEqual(["正在读取仓库结构", "先想想，再看入口"]);
    const stages = session.stagesByTurn["turn_1"] ?? [];
    expect(stages, "归位不得新建阶段").toHaveLength(1);
    expect(stages[0]?.callIds).toContain("c1");
  });

  it("字段缺失（旧后端）仍回落到当前阶段：兼容不回归", () => {
    const { events, session } = setup();
    startTurnWithStage(events, session);

    events.route(
      ev("ASSISTANT", { turn_id: "turn_1", content: "兼容旧后端", interim: true, streaming: true, delta_id: "dl_legacy", seq: 1 }),
    );

    expect(assistants(session)[0]?.stageId).toBe("st_1");
    expect(noteTexts(session, "turn_1")).toContain("兼容旧后端");
  });
});
