/**
 * R5 反例（fb-c）：系统核对注记在**历史恢复**后丢失。
 *
 * 契约 K2.3（docs/plans/2026-10-10-final-boundaries-r1-r7.md）：
 * * 实时：TURN_END 的 annotation（别名 final_annotation）；
 * * 历史：raw.annotation（字符串）；legacy 旧记录可能把注记内联在正文末尾；
 * * 去重：字段存在 -> 用字段，正文按原样（不再从正文里二次抽取）；字段缺失且正文含
 *   内联表头 -> 按表头拆分；两者都在且等价 -> 只渲染一次（优先字段）；
 * * raw 缺失/异常/无注记 -> 正常恢复，保留 verified，不制造虚假失败提醒。
 *
 * 修复前红：session.ts::_historyMessage 只读 raw.verified，raw.annotation 整条丢掉 ->
 * 刷新后系统事实区域空了。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/fb_c_r5_annotation_history.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore, SYSTEM_ANNOTATION_HEADER } from "../session";
import type { StreamMessage } from "../session";

const { getSessionContext, getSessionMessagesBefore } = vi.hoisted(() => ({
  getSessionContext: vi.fn(),
  getSessionMessagesBefore: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: { getSessionContext, getSessionMessagesBefore },
  ApiError: class ApiError extends Error {},
}));

const BODY = "这是这一轮唯一的正式回答，正文只应该出现一次。";
const NOTE =
  SYSTEM_ANNOTATION_HEADER +
  "\n· 任务 ws_x 还没有测试证据\n这几项没有通过验证，不能当作「已完成 / 可使用」。";
const NOTE_OTHER = SYSTEM_ANNOTATION_HEADER + "\n· 另一份不同的核对结论：任务 ws_y 未注册。";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function countOf(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

function row(partial: Record<string, unknown> = {}) {
  return {
    id: "m_assist",
    role: "assistant",
    content: BODY,
    content_type: "text",
    created_at: "2026-10-10T05:00:00+00:00",
    turn_id: "turn_i",
    ...partial,
  };
}

function userRow(turnId: string, id = "m_user") {
  return {
    id,
    role: "user",
    content: "帮我核对这一轮",
    content_type: "text",
    created_at: "2026-10-10T04:59:59+00:00",
    turn_id: turnId,
  };
}

async function loadWith(messages: Record<string, unknown>[]) {
  const { session } = setup();
  getSessionContext.mockResolvedValueOnce({
    topic_id: "topic_1",
    topic_name: "默认话题",
    anchor_fragment: null,
    messages,
    tool_records: [],
    has_more: false,
    next_before: null,
  });
  await session.loadHistory();
  return session;
}

function assistantOf(session: ReturnType<typeof useSessionStore>, turnId: string): StreamMessage {
  const found = session.messages.find((m) => m.role === "assistant" && m.turnId === turnId);
  if (!found) throw new Error("没有找到该轮的助手消息：" + turnId);
  return found;
}

beforeEach(() => {
  localStorage.clear();
  getSessionContext.mockReset();
  getSessionMessagesBefore.mockReset();
});

describe("R5：历史恢复必须带回系统核对注记", () => {
  it("raw.annotation 优先：注记与 verified 一起恢复，正文只出现一次", async () => {
    const session = await loadWith([
      userRow("turn_i"),
      row({
        raw: JSON.stringify({
          annotation: NOTE,
          verified: { accepted: true, basis: "版本 3 / 2 条证据", claims: ["test_passed"] },
        }),
      }),
    ]);

    const message = assistantOf(session, "turn_i");
    expect(countOf(message.content, BODY), "正文只出现一次").toBe(1);
    expect(countOf(message.content, "系统核对"), "注记只出现一次").toBe(1);
    expect(message.content).toContain("任务 ws_x");
    expect(message.verified?.basis, "verified 必须保留").toBe("版本 3 / 2 条证据");
    expect(session.lastError).toBeNull();
  });

  it("旧内联注记（没有 raw.annotation）：按表头原样恢复，只出现一次", async () => {
    const session = await loadWith([userRow("turn_i"), row({ content: BODY + "\n\n" + NOTE })]);

    const message = assistantOf(session, "turn_i");
    expect(countOf(message.content, BODY)).toBe(1);
    expect(countOf(message.content, "系统核对")).toBe(1);
    expect(message.content).toContain("任务 ws_x");
  });

  it("字段与内联并存且等价：只保留一份（优先字段）", async () => {
    const session = await loadWith([
      userRow("turn_i"),
      row({
        content: BODY + "\n\n" + NOTE,
        raw: JSON.stringify({ annotation: NOTE }),
      }),
    ]);

    const message = assistantOf(session, "turn_i");
    expect(countOf(message.content, BODY)).toBe(1);
    expect(countOf(message.content, "系统核对")).toBe(1);
    expect(countOf(message.content, "任务 ws_x")).toBe(1);
  });

  it("字段与内联并存但不相同：字段优先，内联不再渲染（恰好一份）", async () => {
    const session = await loadWith([
      userRow("turn_i"),
      row({
        content: BODY + "\n\n" + NOTE_OTHER,
        raw: JSON.stringify({ annotation: NOTE }),
      }),
    ]);

    const message = assistantOf(session, "turn_i");
    expect(countOf(message.content, "系统核对"), "注记只能有一份").toBe(1);
    expect(message.content).toContain("任务 ws_x");
    expect(message.content, "字段优先：内联的旧结论不再显示").not.toContain("ws_y");
  });

  it("raw 异常（不是 JSON）：正常恢复正文，不制造失败提醒，也不伪造 verified", async () => {
    const session = await loadWith([userRow("turn_i"), row({ raw: "{这不是 JSON" })]);

    const message = assistantOf(session, "turn_i");
    expect(message.content).toBe(BODY);
    expect(message.verified).toBeUndefined();
    expect(session.lastError).toBeNull();
    expect(session.warning).toBeNull();
  });

  it("raw 正常但没有注记：不显示任何系统事实（不编造）", async () => {
    const session = await loadWith([
      userRow("turn_i"),
      row({ raw: JSON.stringify({ verified: { accepted: true, basis: "只有核对结论" } }) }),
    ]);

    const message = assistantOf(session, "turn_i");
    expect(message.content).toBe(BODY);
    expect(message.verified?.basis).toBe("只有核对结论");
    expect(message.content).not.toContain("系统核对");
  });

  it("翻更早的历史（分页）同样恢复 raw.annotation", async () => {
    const { session } = setup();
    session.historyCursor = "cursor_1";
    session.historyHasMore = true;
    getSessionMessagesBefore.mockResolvedValueOnce({
      topic_id: "topic_1",
      messages: [
        userRow("turn_old", "m_old_user"),
        row({
          id: "m_old_assist",
          turn_id: "turn_old",
          created_at: "2026-10-09T05:00:00+00:00",
          raw: JSON.stringify({ annotation: NOTE }),
        }),
      ],
      tool_records: [],
      has_more: false,
      next_before: null,
    });

    await session.loadOlderHistory();

    const message = assistantOf(session, "turn_old");
    expect(countOf(message.content, "系统核对")).toBe(1);
    expect(message.content).toContain("任务 ws_x");
    expect(countOf(message.content, BODY)).toBe(1);
  });

  it("重复加载历史（刷新两次）：注记仍恰好一份，消息不重复", async () => {
    const { session } = setup();
    const payload = {
      topic_id: "topic_1",
      topic_name: "默认话题",
      anchor_fragment: null,
      messages: [userRow("turn_i"), row({ raw: JSON.stringify({ annotation: NOTE }) })],
      tool_records: [],
      has_more: false,
      next_before: null,
    };
    getSessionContext.mockResolvedValue(payload);

    await session.loadHistory();
    await session.loadHistory();

    const list = session.messages.filter((m) => m.role === "assistant");
    expect(list, "重复加载不得产生重复消息").toHaveLength(1);
    expect(countOf(list[0]!.content, "系统核对")).toBe(1);
    expect(countOf(list[0]!.content, BODY)).toBe(1);
  });

  it("注记归属原 turn：只挂在有 raw.annotation 的那一轮", async () => {
    const session = await loadWith([
      userRow("turn_a", "m_user_a"),
      row({ id: "m_a", turn_id: "turn_a", created_at: "2026-10-10T04:00:00+00:00" }),
      userRow("turn_b", "m_user_b"),
      row({
        id: "m_b",
        turn_id: "turn_b",
        created_at: "2026-10-10T05:00:00+00:00",
        raw: JSON.stringify({ annotation: NOTE }),
      }),
    ]);

    expect(assistantOf(session, "turn_a").content, "没有注记的轮不得被塞一条").not.toContain("系统核对");
    expect(countOf(assistantOf(session, "turn_b").content, "系统核对")).toBe(1);
  });
});
