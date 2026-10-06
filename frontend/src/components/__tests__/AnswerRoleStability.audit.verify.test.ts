/**
 * D 独立验收：正式回答的稳定性（审计问题 2 的**渲染层**证据 / plan §1.1）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §1.1（Lead 2026-10-06 最终口径）。
 * 只依据产品规则：
 *
 *   * 正文增量先以 interim=true 实时显示在**过程区**；
 *   * 该次调用结束且没有工具调用 → 同一 delta_id 用一条 {interim:false, streaming:false,
 *     content=累计全文} 把它**原样提升**到正式回答区（全局只有一份，不重复）；
 *   * 调用结束时有工具调用 → 留在过程区；**任何情况下都不允许「正式回答 → 过程区」的移动**，
 *     已显示的文字不得消失。
 *
 * 基线（e428bb9）现状：stores/session.ts 的 pushAssistant 明确写着「只允许 正文 → 过程
 * 这一个方向」，迟到的事件会把已经渲染在答案区的文字**搬回过程区** —— 因此本文件在
 * 修复前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/AnswerRoleStability.audit.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore } from "../../stores/session";
import type { AgentEvent } from "../../services/events";

vi.mock("../../services/api", () => {
  const payload = () => ({
    tasks: [],
    messages: [],
    approvals: [],
    tools: [],
    narratives: [],
    instance_id: "inst_audit",
    turn_queue: { running: null, queued: [], cancelled: [], revision: 0, instance_id: "inst_audit" },
  });
  return {
    api: new Proxy({}, { get: () => vi.fn(async () => payload()) }),
    ApiError: class ApiError extends Error {},
  };
});

/** 虚拟滚动在 jsdom 里没有布局，不会渲染任何轮次；换成「全部渲染」的桩。 */
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

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "evt_" + seq, ts: new Date().toISOString(), data };
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

function shape(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
}

async function mountStream() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const events = useEventStore();
  const session = useSessionStore();
  const wrapper = mount(MessageStream, { global: { plugins: [pinia] } });
  await settle();
  const stream = wrapper.find(".stream").element as HTMLElement;
  shape(stream, 1200, 400);
  await settle();
  return { wrapper, events, session };
}

function occurrenceCount(haystack: string, needle: string): number {
  if (!needle) return 0;
  return haystack.split(needle).length - 1;
}

/** 过程区文本（一轮一个）；没有过程区时返回空串。 */
function processText(wrapper: VueWrapper): string {
  const regions = wrapper.findAll('[data-test="turn-process"]');
  return regions.map((region) => region.text()).join("\n");
}

function assistantBubbles(wrapper: VueWrapper): string[] {
  return wrapper
    .findAll(".message.assistant")
    .map((node) => node.text())
    .filter((text) => text.trim().length > 0);
}

beforeEach(() => {
  seq = 0;
});

describe("契约 §1.1：正式回答只提升、绝不搬回过程区", () => {
  it("迟到的 interim=true（同一 delta_id）不得把已显示在答案区的文字搬走", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("回答我");

    const text = "这段文字已经进入正式回答区，绝不能被搬走。";
    // 1) 后端结束且没有工具调用 → 提升为正式回答（正文先到过过程区，这里只发提升事件）
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: text,
        interim: false,
        streaming: false,
        delta_id: "dl_1",
        seq: 2,
      }),
    );
    await settle();
    expect(
      occurrenceCount(wrapper.text(), text),
      "提升之后这段文字必须已经渲染出来",
    ).toBe(1);

    // 2) 迟到的事件（旧后端会把答案文字移回过程区）—— 前端必须拒绝这个移动
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: text,
        interim: true,
        streaming: false,
        delta_id: "dl_1",
        seq: 3,
        stage_id: "st_1",
      }),
    );
    await settle();

    expect(
      occurrenceCount(wrapper.text(), text),
      "同一份文字全局只能有一份（不得因此多出一条重复气泡）",
    ).toBe(1);
    expect(
      processText(wrapper).includes(text),
      "任何情况下都不允许「正式回答 → 过程区」的移动：文字必须留在答案区",
    ).toBe(false);
    expect(
      assistantBubbles(wrapper).some((bubble) => bubble.includes(text)),
      "这段文字必须仍然显示在正式回答区",
    ).toBe(true);
    wrapper.unmount();
  });

  it("实时过程说明 + 提升：提升之后过程区不留副本，答案区只有一份", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("边生成边显示");

    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: "第一句。",
        interim: true,
        streaming: true,
        delta_id: "dl_9",
        seq: 1,
      }),
    );
    await settle();
    expect(processText(wrapper), "过程区的实时文字必须先可见（provider 结束前）").toContain("第一句。");

    const full = "第一句。第二句。";
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: full,
        interim: true,
        streaming: true,
        delta_id: "dl_9",
        seq: 2,
      }),
    );
    await settle();
    // 提升：同一 delta_id、同一份文字、同一时刻改成正式回答
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: full,
        interim: false,
        streaming: false,
        delta_id: "dl_9",
        seq: 3,
      }),
    );
    await settle();

    expect(occurrenceCount(wrapper.text(), full), "提升之后全局只能有一份").toBe(1);
    expect(
      assistantBubbles(wrapper).some((bubble) => bubble.includes(full)),
      "提升之后必须显示在正式回答区",
    ).toBe(true);
    wrapper.unmount();
  });

  it("提升之后来的工具调用/阶段事件不改变答案区的内容", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("先回答再调用工具？");

    const text = "正式回答已经落定。";
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: text,
        interim: false,
        streaming: false,
        delta_id: "dl_20",
        seq: 1,
      }),
    );
    await settle();

    events.dispatch(
      ev("STAGE", {
        turn_id: "turn_1",
        stage_id: "st_late",
        index: 1,
        status: "running",
        name: "晚到的阶段",
        text: "晚到的阶段说明",
        kind: "progress",
        op: "start",
        narrative_id: "msg_late",
        call_id: null,
        call_ids: ["c_late"],
        created_at: "2026-10-06T08:00:00+00:00",
      }),
    );
    events.dispatch(
      ev("TOOL_START", { turn_id: "turn_1", call_id: "c_late", tool: "fs_read", stage_id: "st_late" }),
    );
    await settle();

    expect(occurrenceCount(wrapper.text(), text), "答案区内容不得因为工具事件被搬走或复制").toBe(1);
    expect(processText(wrapper).includes(text), "答案文字不得出现在过程区").toBe(false);
    wrapper.unmount();
  });
});
