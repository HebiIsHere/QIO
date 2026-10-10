/**
 * F 独立验证（阶段一 · F11）：工具失败 + 系统核对注记下，正式回答正文唯一且注释完整。
 *
 * 契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C1 ——
 * 「前端 applyFinalAnswer 以 turn 身份 + 最终校准为准，禁止再用「全文是否相等」判断同一次
 * 回答」；Lead 2026-10-09 冻结裁定：系统核对注记由后端以**独立字段** annotation
 * （兼容 final_annotation）随 TURN_END 交付，final_content 保持**纯正文**；前端把注记作为
 * 独立「系统事实」区域渲染（与正文拆开，正文不重复）。
 *
 * 跨层：夹具 docs/acc/acc-f-f11-events.json 由后端真实链路
 * （backend/tests/test_acc_f_11_annotation_final_answer.py：假 provider → adapter → loop →
 * TurnManager → 事件总线）捕获并归一化；这里用**真实 Pinia store**（useEventStore +
 * useSessionStore）复放。
 *
 * 断言按**拆分语义**写，不依赖 store 内部字段名：把这条回答拆成「正文 / 系统注记」后，
 * ① 同一段正式回答只显示一条消息；② 拆出的正文恰好等于 BODY 且不含注记表头；
 * ③ 注记完整（含表头与结论句「不能当作「已完成 / 可使用」」）；④ 整段回答里 BODY 恰好
 * 出现一次。
 *
 * 基线：ASSISTANT 的正文 body 与 TURN_END.final_content（body + 注记，旧形态）不相等 →
 * applyFinalAnswer 走 pushAssistant 追加第二条 → 正文出现两次。
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

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

/**
 * 夹具路径：vitest 在 frontend/ 下运行，仓库根在上一级；两种候选都试，
 * 都不在就明确报错（不静默跳过）。
 */
function fixturePath(): string {
  const candidates = [
    resolve(process.cwd(), "..", "docs", "acc", "acc-f-f11-events.json"),
    resolve(process.cwd(), "docs", "acc", "acc-f-f11-events.json"),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("缺少后端真实事件夹具；已尝试：" + candidates.join(" / "));
}

const FIXTURE = fixturePath();
const BODY = "====正文====\n这是这一轮唯一的正式回答。";
// 冻结契约里的注记表头 / 结论句（backend core/turn_facts.py::ANNOTATION_HEADER / FOOTER）。
const ANNOTATION_HEADER = "—— 系统核对（后端事实，不是模型的说法）：";
const ANNOTATION_FOOTER = "不能当作「已完成 / 可使用」。";

interface FixtureEvent {
  type: string;
  id: string;
  ts: string;
  data: Record<string, unknown>;
}

function loadFixture(): FixtureEvent[] {
  let raw: string;
  try {
    raw = readFileSync(FIXTURE, "utf-8");
  } catch (err) {
    throw new Error(
      "缺少后端真实事件夹具 " +
        FIXTURE +
        "；先运行 backend/tests/test_acc_f_11_annotation_final_answer.py（" +
        String(err) +
        "）",
    );
  }
  return JSON.parse(raw) as FixtureEvent[];
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

/** 独立字段形态优先（annotation / final_annotation / …），取不到返回 null。 */
function fieldNote(message: Record<string, unknown>): string | null {
  for (const key of ["annotation", "finalAnnotation", "system_annotation", "systemAnnotation"]) {
    const value = message[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

/** 与产品实现同义：按冻结表头把回答内容切成「正文 / 系统注记」。 */
function splitAnswer(text: string): { body: string; note: string | null } {
  const raw = String(text ?? "");
  const index = raw.indexOf(ANNOTATION_HEADER);
  if (index < 0) return { body: raw.trim(), note: null };
  return {
    body: raw.slice(0, index).replace(/\s+$/, "").trim(),
    note: raw.slice(index).trim() || null,
  };
}

describe("F11 工具失败 + 系统注记：正式回答正文唯一且注释完整", () => {
  it("真实后端事件夹具复放：正文只显示一次，注记完整且与正文分开", () => {
    const events = loadFixture();
    const { events: store, session } = setup();
    for (const event of events) store.route(event as never);

    const assistants = session.messages.filter((m) => m.role === "assistant");
    // ① 正文唯一：同一段正式回答只能有一条消息。
    expect(assistants.length, JSON.stringify(assistants.map((m) => m.content.slice(0, 24)))).toBe(1);

    const answer = assistants[0] as unknown as Record<string, unknown>;
    const content = String(answer.content ?? "");
    const split = splitAnswer(content);
    const note = fieldNote(answer) ?? split.note;

    // ② 正文纯净：拆出的正文恰好是 BODY，且不含注记表头。
    expect(split.body, JSON.stringify(content.slice(0, 40))).toBe(BODY);
    expect(split.body).not.toContain(ANNOTATION_HEADER);

    // ③ 注记完整：表头 + 结论句都必须交付（不得截断 / 缺失）。
    expect(note, JSON.stringify(content.slice(-120))).toBeTruthy();
    expect(note).toContain(ANNOTATION_HEADER);
    expect(note).toContain(ANNOTATION_FOOTER);

    // ④ 整段回答里 BODY 恰好出现一次。
    expect(content.split(BODY).length - 1).toBe(1);
    expect(answer.interim).not.toBe(true);
    expect(session.turnRunning).toBe(false);
  });

  it("同一 turn 重复 TURN_END（重连重放）仍然只有一条正式回答", () => {
    const events = loadFixture();
    const { events: store, session } = setup();
    for (const event of events) store.route(event as never);
    // 重连重放最后一条 TURN_END（id 相同）不得再追加
    const last = events[events.length - 1];
    store.route(last as never);

    const assistants = session.messages.filter((m) => m.role === "assistant");
    expect(assistants.length).toBe(1);
    expect(assistants.filter((m) => m.content.includes(BODY)).length).toBe(1);
    expect(assistants[0].content.split(BODY).length - 1).toBe(1);
  });
});
