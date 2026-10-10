/**
 * fb-E / 阶段二 · 跨模块组合（前端）：R5×R6（历史注记 + 回答身份校准）。
 *
 * (a) 实时路径：同一回答身份（delta_id）先到 12，TURN_END 带 answer_id + final_content=13 +
 *     独立 annotation → 只能有一条正式回答、正文恰好是 13、系统注记在独立区域恰好一次。
 * (b) 历史路径：raw.annotation 必须在恢复后仍然渲染成系统事实区域（R5），正文恰好一次。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/fb_e_combo_history_answer.test.ts
 */
import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { splitSystemAnnotation, useSessionStore, type StreamMessage } from "../session";
import MessageItem from "../../components/MessageItem.vue";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", messages: [] })),
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

const NOTE =
  "—— 系统核对（后端事实，不是模型的说法）：\n· 任务 ws_x 还没有测试证据\n这几项没有通过验证，不能当作「已完成 / 可使用」。";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { pinia, events: useEventStore(), session: useSessionStore() };
}

function assistants(session: ReturnType<typeof useSessionStore>): StreamMessage[] {
  return session.messages.filter((m) => m.role === "assistant");
}

describe("R5×R6 组合", () => {
  it("同身份校准 12→13 + 独立注记：一条回答、正文 13、注记恰好一次", () => {
    const { pinia, events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_combo" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_combo", content: "12", interim: false, streaming: true, delta_id: "dl_combo", seq: 1 },
    });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: {
        turn_id: "turn_combo",
        status: "completed",
        final_content: "13",
        answer_id: "dl_combo",
        annotation: NOTE,
      },
    });

    const list = assistants(session);
    expect(list, "同身份校准不得产生第二条正式回答").toHaveLength(1);
    const split = splitSystemAnnotation(list[0]!.content);
    expect(split.body, "正文必须被校准成 13").toBe("13");
    expect(split.annotation, "注记必须独立存在").toContain("系统核对");
    expect(split.annotation).toContain("不能当作「已完成 / 可使用」");
    expect(list[0]!.content.split("13").length - 1, "正文恰好一次").toBe(1);
    expect(list[0]!.content.split("系统核对").length - 1, "注记恰好一次").toBe(1);

    const w = mount(MessageItem, { props: { message: list[0] as never }, global: { plugins: [pinia] } });
    const note = w.find('[data-test="answer-system-note"]');
    expect(note.exists(), "系统事实区域必须独立渲染").toBe(true);
    expect(note.text().split("系统核对").length - 1).toBe(1);
    w.unmount();
  });

  it("历史恢复：raw.annotation 形态必须恢复出系统事实区域，正文恰好一次（R5）", () => {
    const { pinia, session } = setup();
    const message = session._historyMessage({
      id: "m_combo",
      role: "assistant",
      content: "====正文====\n历史里的正式回答。",
      content_type: "text",
      created_at: "2026-10-10T00:00:00+00:00",
      turn_id: "turn_combo_hist",
      raw: JSON.stringify({ annotation: NOTE }),
    } as never);

    const w = mount(MessageItem, { props: { message: message as never }, global: { plugins: [pinia] } });
    const note = w.find('[data-test="answer-system-note"]');
    expect(note.exists(), "历史恢复后注记必须仍在（系统事实区域）").toBe(true);
    expect(note.text()).toContain("系统核对");
    expect(note.text()).toContain("不能当作「已完成 / 可使用」");
    expect(w.text().split("====正文====").length - 1, "正文恰好一次").toBe(1);
    w.unmount();
  });
});
