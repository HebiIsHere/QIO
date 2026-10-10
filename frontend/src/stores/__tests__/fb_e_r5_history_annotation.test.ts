/**
 * fb-E / R5 反例：系统核对注记在历史恢复后丢失。
 *
 * 用户可见规则（K2.3/K2.4）：
 *  * 历史记录里的注记形态是 raw.annotation（字符串）；legacy 旧记录可能内联在正文末尾；
 *  * 历史转换必须把它恢复出来，系统事实区域独立渲染，注记恰好一次、正文恰好一次、归属原 turn；
 *  * 字段存在就用字段；字段缺失且正文含固定表头才从正文拆分。
 *
 * 基线：_historyMessage 只读 raw.verified，完全不看 raw.annotation ——
 * 刷新/翻页后注记直接消失（正文还在，系统事实却没了）。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/fb_e_r5_history_annotation.test.ts
 */
import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
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

const BODY = "====正文====\n这是这一轮唯一的正式回答。";
const NOTE =
  "—— 系统核对（后端事实，不是模型的说法）：\n· 任务 ws_x 还没有测试证据\n这几项没有通过验证，不能当作「已完成 / 可使用」。";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { pinia, session: useSessionStore() };
}

function loadHistory(session: ReturnType<typeof useSessionStore>, raw: string | undefined, content: string) {
  return session._historyMessage({
    id: "m_acc_r5",
    role: "assistant",
    content,
    content_type: "text",
    created_at: "2026-10-10T00:00:00+00:00",
    turn_id: "turn_acc_r5",
    raw,
  } as never);
}

describe("R5 历史恢复里的系统核对注记", () => {
  it("raw.annotation 形态：注记必须恢复到系统事实区域，正文只出现一次", () => {
    const { pinia, session } = setup();
    const message = loadHistory(session, JSON.stringify({ annotation: NOTE }), BODY);
    const w = mount(MessageItem, { props: { message: message as never }, global: { plugins: [pinia] } });

    const note = w.find('[data-test="answer-system-note"]');
    expect(note.exists(), "raw.annotation 形态的注记在历史恢复后丢失（系统事实区域不存在）").toBe(true);
    expect(note.text()).toContain("系统核对");
    expect(note.text()).toContain("不能当作「已完成 / 可使用」");
    expect(w.text().split("====正文====").length - 1, "正文必须恰好一次").toBe(1);
    w.unmount();
  });

  it("legacy 内联形态（正文末尾含固定表头）：仍然只渲染一次、正文不被复制", () => {
    const { pinia, session } = setup();
    const message = loadHistory(session, undefined, BODY + "\n\n" + NOTE);
    const w = mount(MessageItem, { props: { message: message as never }, global: { plugins: [pinia] } });
    const note = w.find('[data-test="answer-system-note"]');
    expect(note.exists(), "内联注记必须被拆出来独立渲染").toBe(true);
    expect(w.text().split("====正文====").length - 1).toBe(1);
    expect(note.text().split("系统核对").length - 1, "注记只能渲染一次").toBe(1);
    w.unmount();
  });
});
