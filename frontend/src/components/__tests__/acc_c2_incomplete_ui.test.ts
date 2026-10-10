/**
 * F06 / F12 界面消费（acc-c2）：incomplete 终态与排队取消必须在**界面上**如实表达。
 *
 * 契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md
 *   §二 C2 + §七 C2：incomplete = 未完成（不是完成），显示简短原因 + 确实可用的 retry，详情仍收起；
 *   §七 C1/C5：排队轮取消的结束事实落到它自己（retry 可用），不影响正在跑的 A。
 *
 * 修复前红（组件级）：
 * * TurnProcess 的 STATUS_WORD 没有 incomplete → 状态行说「已结束」、data-state=ready（绿色圆点），
 *   界面上与正常完成无法区分；
 * * ConversationView 的结局提示只认 cancelled → incomplete 被静默吞掉。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/acc_c2_incomplete_ui.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

import TurnProcess from "../TurnProcess.vue";
import MessageItem from "../MessageItem.vue";
import MessageStream from "../MessageStream.vue";
import ConversationView from "../../views/ConversationView.vue";
import { resetProcessState } from "../../stores/turnProcess";
import { useEventStore } from "../../stores/events";
import {
  useSessionStore,
  type StreamMessage,
  type TurnFacts,
} from "../../stores/session";

const { getSessionContext, sendTurn } = vi.hoisted(() => ({
  getSessionContext: vi.fn(),
  sendTurn: vi.fn(async () => ({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: "topic_1",
  })),
}));

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext,
    sendTurn,
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst_c2",
      revision: 1,
      turn_queue: { instance_id: "inst_c2", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
      tools: [],
      narratives: [],
    })),
    getTrace: vi.fn(async () => ({ turn_id: "turn_i", duration_ms: 0, phases: {} })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
  },
  ApiError: class ApiError extends Error {},
}));

vi.mock("@tanstack/vue-virtual", async () => {
  const { computed } = await import("vue");
  return {
    useVirtualizer: (options: { value: { count: number } }) =>
      computed(() => ({
        getTotalSize: () => options.value.count * 400,
        getVirtualItems: () =>
          Array.from({ length: options.value.count }, (_, index) => ({
            index,
            key: index,
            start: index * 400,
          })),
        measureElement: () => undefined,
      })),
  };
});

const BODY = "已经确认的这一段正文只应该出现一次。";
const REASON = "模型在这一轮结束标记之前就断流了，这段正文可能不完整。";

function facts(partial: Partial<TurnFacts> = {}): TurnFacts {
  const base: TurnFacts = {
    turnId: "turn_i",
    status: "incomplete",
    durationMs: 2600,
    queueMs: 40,
    startedAt: null,
    endedAt: null,
    reason: REASON,
    reasonCode: "incomplete_stream",
    stoppedBy: "system",
    actions: ["retry"],
    errorText: "upstream eof before finish_reason",
  };
  return { ...base, ...partial };
}

function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
  return {
    content: "",
    contentType: "text",
    createdAt: "2026-10-09T05:00:00+00:00",
    ...partial,
  } as StreamMessage;
}

/** 一轮的用户消息（retry 的目标）：没有它，界面不该给一个点不动的按钮。 */
function withUserMessage(session: ReturnType<typeof useSessionStore>, turnId: string, text: string) {
  session.pushUser(text);
  const m = session.messages[session.messages.length - 1];
  if (m) m.turnId = turnId;
  return m;
}

function mountProcess(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = "topic_1";
  if (props.turnId) {
    withUserMessage(session, String(props.turnId), "这一轮的用户消息");
  }
  const w = mount(TurnProcess, {
    props: {
      turnId: "turn_i",
      items: [],
      stages: [],
      facts: null,
      running: false,
      ...props,
    },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
  return { w, session, pinia };
}

let seq = 0;
function ev(type: string, data: Record<string, unknown>) {
  seq += 1;
  return { type, id: "ui_" + seq, ts: new Date().toISOString(), data };
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

beforeEach(() => {
  resetProcessState();
  seq = 0;
  localStorage.clear();
  getSessionContext.mockReset();
  sendTurn.mockClear();
  getSessionContext.mockResolvedValue({
    topic_id: "topic_1",
    topic_name: "默认话题",
    anchor_fragment: null,
    messages: [],
    tool_records: [],
    turn_facts: [],
    has_more: false,
    next_before: null,
  });
});

describe("F06 界面：incomplete 的过程区不是「已完成」", () => {
  it("状态行说「未完成」、data-state=incomplete（不是成功绿），原因可见、详情默认收起", async () => {
    const { w } = mountProcess({ turnId: "turn_i", facts: facts() });
    await nextTick();

    const region = w.find("[data-test='turn-process']");
    expect(region.attributes("data-state")).toBe("incomplete");
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("未完成");
    expect(status).not.toContain("已完成");
    expect(status).not.toContain("已结束");
    // 简短原因（后端人话）默认可见
    expect(w.find("[data-test='turn-process-reason']").text()).toContain("断流");
    // 详情仍收起：原因码 / 原始错误不在默认可见区
    const detail = w.find("[data-test='turn-process-outcome-detail']");
    expect(detail.exists()).toBe(true);
    expect((detail.element as HTMLDetailsElement).open).toBe(false);
    w.unmount();
  });

  it("retry 是**确实可用**的操作：点它真的重发这一轮的用户消息", async () => {
    const { w } = mountProcess({ turnId: "turn_i", facts: facts() });
    await nextTick();

    const retry = w.find("[data-test='turn-process-action-retry']");
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    await flushPromises();
    expect(sendTurn).toHaveBeenCalledWith("这一轮的用户消息", "topic_1", [], "turn_i");
    w.unmount();
  });

  it("找不到这一轮的用户消息时给出说明，而不是一个点不动的按钮", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mount(TurnProcess, {
      props: { turnId: "turn_missing", items: [], stages: [], facts: facts({ turnId: "turn_missing" }), running: false },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });
    await nextTick();
    expect(w.find("[data-test='turn-process-action-retry']").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-action-note']").text()).toContain("找不到");
    w.unmount();
  });

  it("completed 仍然说「已完成」（回归：不把正常完成一并改成未完成）", async () => {
    const { w } = mountProcess({
      turnId: "turn_ok",
      facts: facts({ turnId: "turn_ok", status: "completed", reason: null, reasonCode: "none", actions: [] }),
    });
    await nextTick();
    expect(w.find("[data-test='turn-process']").attributes("data-state")).toBe("ready");
    expect(w.find("[data-test='turn-process-status']").text()).toContain("已完成");
    w.unmount();
  });

  it("取消（cancelled）仍然说「已停止」：incomplete 不吞掉既有终态的表达", async () => {
    const { w } = mountProcess({
      turnId: "turn_c",
      facts: facts({
        turnId: "turn_c",
        status: "cancelled",
        reason: "你停止了这一轮",
        reasonCode: "user_stopped",
        stoppedBy: "user",
      }),
    });
    await nextTick();
    expect(w.find("[data-test='turn-process']").attributes("data-state")).toBe("stopped");
    expect(w.find("[data-test='turn-process-status']").text()).toContain("已停止");
    w.unmount();
  });
});

describe("F06 界面：刷新之后仍然是「未完成 + 原因 + retry」", () => {
  it("历史台账带回 incomplete：过程区在、原因在、retry 可点，正文只出现一次", async () => {
    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: "默认话题",
      anchor_fragment: null,
      messages: [
        {
          id: "m_user",
          role: "user",
          content: "帮我看看这段流",
          content_type: "text",
          created_at: "2026-10-09T05:00:00+00:00",
          turn_id: "turn_i",
        },
        {
          id: "m_assist",
          role: "assistant",
          content: BODY,
          content_type: "text",
          created_at: "2026-10-09T05:00:01+00:00",
          turn_id: "turn_i",
        },
      ],
      tool_records: [],
      turn_facts: [
        {
          turn_id: "turn_i",
          status: "incomplete",
          reason_code: "incomplete_stream",
          reason: REASON,
          stopped_by: "system",
          actions: ["retry"],
        },
      ],
      has_more: false,
      next_before: null,
    });

    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    await session.loadHistory();

    const wrapper = mount(MessageStream, { global: { plugins: [pinia] } });
    await settle();
    const stream = wrapper.find(".stream").element as HTMLElement;
    Object.defineProperty(stream, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(stream, "clientHeight", { value: 400, configurable: true });
    await settle();

    const region = wrapper.find("[data-test='turn-process']");
    expect(region.exists(), "刷新后未完成轮的过程区必须还在").toBe(true);
    expect(region.attributes("data-state")).toBe("incomplete");
    expect(region.text()).toContain("未完成");
    expect(region.text()).toContain("断流");
    expect(wrapper.find("[data-test='turn-process-action-retry']").exists()).toBe(true);

    // 已确认正文保留，且只出现一次（不得因为「全文不等」再补一条）
    expect(wrapper.text().split(BODY).length - 1).toBe(1);

    await wrapper.find("[data-test='turn-process-action-retry']").trigger("click");
    await flushPromises();
    expect(sendTurn).toHaveBeenCalledWith("帮我看看这段流", "topic_1", [], "turn_i");
    wrapper.unmount();
  });
});

describe("F06 界面：incomplete 不被静默吞掉（顶部结局提示）", () => {
  function mountView() {
    const pinia = createPinia();
    setActivePinia(pinia);
    return mount(ConversationView, {
      global: {
        plugins: [pinia],
        stubs: { MessageStream: true, Composer: true, SettingsFloat: true },
      },
    });
  }

  it("TURN_END(incomplete) 后显示安静的「未完成」，不是错误横幅", async () => {
    const w = mountView();
    await flushPromises();
    const session = useSessionStore();
    const events = useEventStore();
    events.route(ev("TURN_START", { turn_id: "turn_i", revision: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "turn_i", content: BODY, interim: false, streaming: true, delta_id: "dl_i", seq: 1 }));
    events.route(
      ev("TURN_END", {
        turn_id: "turn_i",
        status: "incomplete",
        reason_code: "incomplete_stream",
        reason: REASON,
        stopped_by: "system",
        actions: ["retry"],
        final_content: BODY,
        revision: 2,
      }),
    );
    await flushPromises();

    expect(session.lastError).toBeNull();
    expect(w.find(".notice.err").exists(), "不完整结束不是错误").toBe(false);
    const notice = w.find("[data-test='turn-incomplete-notice']");
    expect(notice.exists(), "incomplete 结局不得被界面吞掉").toBe(true);
    expect(notice.text()).toContain("未完成");
    expect(notice.text()).not.toContain("本轮执行失败");
    w.unmount();
  });

  it("真的失败（failed）仍然走错误横幅，不被 incomplete 的提示抢走", async () => {
    const w = mountView();
    await flushPromises();
    const events = useEventStore();
    events.route(ev("TURN_START", { turn_id: "turn_f", revision: 1 }));
    events.route(
      ev("TURN_END", {
        turn_id: "turn_f",
        status: "failed",
        reason_code: "provider_error",
        reason: "模型服务没有响应",
        stopped_by: "system",
        actions: ["retry"],
        error: "upstream 502",
        revision: 2,
      }),
    );
    await flushPromises();

    expect(w.find(".notice.err").exists()).toBe(true);
    expect(w.find("[data-test='turn-incomplete-notice']").exists()).toBe(false);
    w.unmount();
  });
});

describe("F11 回归：系统核对注记走独立「系统事实」区域，正文只出现一次", () => {
  const NOTE =
    "—— 系统核对（后端事实，不是模型的说法）：" + String.fromCharCode(10) +
    "· 任务 ws_x 还没有测试证据" + String.fromCharCode(10) +
    "这几项没有通过验证，不能当作「已完成 / 可使用」。";

  it("TURN_END 的独立 annotation 字段：正文留在气泡里，注记独立成块，两者各一次", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const events = useEventStore();
    events.route(ev("TURN_START", { turn_id: "turn_f11", revision: 1 }));
    events.route(
      ev("ASSISTANT", {
        turn_id: "turn_f11",
        content: BODY,
        interim: false,
        streaming: true,
        delta_id: "dl_f11",
        seq: 1,
      }),
    );
    events.route(
      ev("TURN_END", {
        turn_id: "turn_f11",
        status: "incomplete",
        reason_code: "incomplete_stream",
        reason: REASON,
        stopped_by: "system",
        actions: ["retry"],
        final_content: BODY,
        annotation: NOTE,
        revision: 2,
      }),
    );
    const message = session.messages.find((m) => m.role === "assistant")!;
    const w = mount(MessageItem, { props: { message }, global: { plugins: [pinia] } });
    await nextTick();

    const note = w.find("[data-test='answer-system-note']");
    expect(note.exists(), "注记必须在独立的「系统事实」区域渲染").toBe(true);
    expect(note.text()).toContain("系统核对");
    expect(note.text()).toContain("不能当作");
    // 正文气泡里不得再出现注记（它不是混在 Markdown 正文里的）
    expect(w.find(".assist-bubble").text()).not.toContain("系统核对");
    const text = w.text();
    expect(text.split(BODY).length - 1, "正文只出现一次").toBe(1);
    expect(text.split("—— 系统核对").length - 1, "注记只出现一次").toBe(1);
    w.unmount();
  });

  it("没有注记的普通回答不出现「系统事实」块（不制造噪声）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.pushAssistant("一段普通回答。");
    const message = session.messages[session.messages.length - 1]!;
    const w = mount(MessageItem, { props: { message }, global: { plugins: [pinia] } });
    await nextTick();
    expect(w.find("[data-test='answer-system-note']").exists()).toBe(false);
    expect(w.find(".assist-bubble").text()).toContain("一段普通回答。");
    w.unmount();
  });
});
