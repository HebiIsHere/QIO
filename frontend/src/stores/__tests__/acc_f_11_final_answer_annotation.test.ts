/**
 * F 独立验证（阶段一 · F11）：工具失败 + 系统注释下，正式回答正文唯一且注释完整。
 *
 * 契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C1 ——
 * 「前端 applyFinalAnswer 以 turn 身份 + 最终校准为准，禁止再用「全文是否相等」判断同一次回答」。
 *
 * 跨层：夹具 docs/acc/acc-f-f11-events.json 由后端真实链路
 * （backend/tests/test_acc_f_11_annotation_final_answer.py：假 provider → adapter → loop →
 * TurnManager → 事件总线）捕获并归一化；这里用**真实 Pinia store**（useEventStore +
 * useSessionStore）复放，断言界面上的正式回答只有一条、且系统注释完整。
 *
 * 基线：ASSISTANT 的正文 body 与 TURN_END.final_content（body + 注释）不相等 →
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

describe("F11 工具失败 + 系统注释：正式回答正文唯一且注释完整", () => {
  it("真实后端事件夹具复放：不得把同一段正式回答显示两次", () => {
    const events = loadFixture();
    const { events: store, session } = setup();
    for (const event of events) store.route(event as never);

    const assistants = session.messages.filter((m) => m.role === "assistant");
    const withBody = assistants.filter((m) => m.content.includes(BODY));

    expect(withBody.length, JSON.stringify(assistants.map((m) => m.content.slice(0, 24)))).toBe(1);
    expect(assistants.length, JSON.stringify(assistants.map((m) => m.content.slice(0, 24)))).toBe(1);

    const answer = withBody[0];
    expect(answer?.content).toContain(ANNOTATION_HEADER);
    expect(answer?.content).toContain(ANNOTATION_FOOTER);
    expect(answer?.interim).not.toBe(true);
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
    expect(assistants.filter((m) => m.content.includes(BODY)).length).toBe(1);
  });
});
