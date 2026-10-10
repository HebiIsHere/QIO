/**
 * R4 问题一（前端）：回答阶段的流式正文必须**实时**进正式回答容器。
 *
 * 契约 §1.1：回答调用（tools=[]）的正文从第一个可发布增量起就是
 * `{interim:false, streaming:true}`；同一 delta_id 的后续增量是累计快照，
 * 收尾的 `{interim:false, streaming:false, content=累计全文}` 只做就地校准。
 *
 * 修复前红：DOM 上 .message.assistant 只有话题名/时间/复制按钮 —— 正文一个字都没有
 * （增量已到达、消息也建了，但打字机从 0 开始逐字点亮，整段到达的文本在 jsdom 里
 * 直到批次节拍才出现；「边生成边显示」变成了「看不到」）。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/AnswerContainerStreaming.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

import MessageStream from "../MessageStream.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore } from "../../stores/session";
import type { AgentEvent } from "../../services/events";

// 与 D 的验收件同一套装置：api 打桩 + 虚拟滚动换成「全部渲染」的桩（jsdom 没有布局）
vi.mock("../../services/api", () => {
  const payload = () => ({
    tasks: [],
    messages: [],
    approvals: [],
    tools: [],
    narratives: [],
    instance_id: "inst_answer",
    turn_queue: { running: null, queued: [], cancelled: [], revision: 0, instance_id: "inst_answer" },
  });
  return {
    api: new Proxy({}, { get: () => vi.fn(async () => payload()) }),
    ApiError: class ApiError extends Error {},
  };
});

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
  return { type, id: "ans_" + seq, ts: new Date().toISOString(), data };
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

async function mountStream() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const events = useEventStore();
  const session = useSessionStore();
  const wrapper = mount(MessageStream, { global: { plugins: [pinia] } });
  await settle();
  const stream = wrapper.find(".stream").element as HTMLElement;
  Object.defineProperty(stream, "scrollHeight", { value: 1200, configurable: true });
  Object.defineProperty(stream, "clientHeight", { value: 400, configurable: true });
  await settle();
  return { wrapper, events, session };
}

function answerText(wrapper: VueWrapper): string {
  return wrapper.findAll(".message.assistant").map((node) => node.text()).join("\n");
}

function processText(wrapper: VueWrapper): string {
  return wrapper.findAll('[data-test="turn-process"]').map((node) => node.text()).join("\n");
}

function count(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

const PROCESS = "过程说明：这一步先读文件。";
const ANSWER_ONE = "正式回答第一段。";
const ANSWER_TWO = "正式回答第一段。补上的第二段。";

async function drive(events: ReturnType<typeof useEventStore>, session: ReturnType<typeof useSessionStore>) {
  events.dispatch(ev("TURN_START", { turn_id: "turn_ans", revision: 1 }));
  session.pushUser("回答我");
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_ans",
      content: PROCESS,
      interim: true,
      streaming: true,
      delta_id: "dl_process",
      seq: 1,
      stage_id: "st_1",
    }),
  );
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_ans",
      content: ANSWER_ONE,
      interim: false,
      streaming: true,
      delta_id: "dl_answer",
      seq: 2,
    }),
  );
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_ans",
      content: ANSWER_TWO,
      interim: false,
      streaming: true,
      delta_id: "dl_answer",
      seq: 3,
    }),
  );
  await settle();
}

beforeEach(() => {
  seq = 0;
});

describe("R4 问题一：流式正式回答的 DOM", () => {
  it("增量到达后回答文字立即在 .message.assistant 里；过程区不含它、全局只有一份", async () => {
    const { wrapper, events, session } = await mountStream();
    await drive(events, session);

    const answers = answerText(wrapper);
    expect(answers, "正式回答必须在正式回答容器里").toContain(ANSWER_ONE);
    expect(answers, "后续增量要落在同一个气泡里").toContain("补上的第二段");
    expect(processText(wrapper), "过程容器不得出现正式回答").not.toContain("正式回答第一段");
    expect(processText(wrapper), "过程说明仍在过程容器").toContain("过程说明");
    expect(count(wrapper.text(), ANSWER_ONE), "全局只有一份").toBe(1);
    wrapper.unmount();
  });

  it("断流 + 一轮失败之后，已显示的正式回答仍在回答容器、不回过程区", async () => {
    const { wrapper, events, session } = await mountStream();
    await drive(events, session);

    events.dispatch(
      ev("ERROR", { code: "stream", message: "连接中断", recoverable: true, turn_id: "turn_ans" }),
    );
    events.dispatch(
      ev("TURN_END", {
        turn_id: "turn_ans",
        status: "failed",
        reason_code: "provider_error",
        reason: "连接中断",
        stopped_by: null,
        actions: ["retry"],
        // K2.2：缺省 = 不校准（本用例的意图是「没有 final_content」，不是清空已显示回答）
        final_content: null,
      }),
    );
    await settle();

    expect(answerText(wrapper), "已看到的回答不能被撤走").toContain("正式回答第一段");
    expect(processText(wrapper), "断流后也不能跑到过程区").not.toContain("正式回答第一段");
    expect(count(wrapper.text(), ANSWER_ONE), "仍然只有一份").toBe(1);
    wrapper.unmount();
  });

  it("收尾校准（同一 delta_id、streaming=false、累计全文）就地更新，不新建气泡", async () => {
    const { wrapper, events, session } = await mountStream();
    await drive(events, session);
    const before = wrapper.findAll(".message.assistant").length;

    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_ans",
        content: ANSWER_TWO + "收尾校准。",
        interim: false,
        streaming: false,
        delta_id: "dl_answer",
        seq: 4,
      }),
    );
    await settle();

    expect(wrapper.findAll(".message.assistant").length, "校准不得新建第二条").toBe(before);
    expect(answerText(wrapper)).toContain("收尾校准。");
    expect(count(wrapper.text(), ANSWER_ONE)).toBe(1);
    wrapper.unmount();
  });
});
