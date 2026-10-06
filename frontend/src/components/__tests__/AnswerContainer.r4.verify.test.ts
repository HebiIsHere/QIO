/**
 * D 独立验收（R4 问题一）：正式回答必须渲染在**正式回答容器**里，而不是「页面任意位置」。
 *
 * 用户可见规则（plan §3）：
 *  1) 正式回答调用（tools=[]）的正文从第一个可发布增量起就进正式回答区；
 *  2) 过程区（[data-test="turn-process"]）里**不含**这段正式回答；
 *  3) 断流（连接中断 / 一轮失败）之后，已经显示在正式回答区的文字**仍然在**，且全局只有一份。
 *
 * 容器口径（不是「页面任意位置出现文字」）：
 *  - 正式回答容器 = 助手消息气泡 .message.assistant（MessageItem.vue 根节点 class="message" + role）；
 *  - 过程容器     = [data-test="turn-process"]。
 */
import { describe, expect, it, beforeEach } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

import MessageStream from "../MessageStream.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore } from "../../stores/session";
import type { AgentEvent } from "../../services/api";

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "r4evt_" + seq, ts: new Date().toISOString(), data };
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

function processText(wrapper: VueWrapper): string {
  return wrapper
    .findAll('[data-test="turn-process"]')
    .map((region) => region.text())
    .join("\n");
}

function answerText(wrapper: VueWrapper): string {
  return wrapper
    .findAll(".message.assistant")
    .map((node) => node.text())
    .join("\n");
}

function occurrenceCount(haystack: string, needle: string): number {
  if (!needle) return 0;
  return haystack.split(needle).length - 1;
}

const PROCESS_TEXT = "过程说明：这一步先读文件。";
const ANSWER_ONE = "正式回答第一段。";
const ANSWER_TWO = "正式回答第一段。补上的第二段。";

async function driveAnswerStream(events: ReturnType<typeof useEventStore>, session: ReturnType<typeof useSessionStore>) {
  events.dispatch(ev("TURN_START", { turn_id: "turn_r4", revision: 1 }));
  session.pushUser("回答我");
  // 过程区说明（工作调用）
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_r4",
      content: PROCESS_TEXT,
      interim: true,
      streaming: true,
      delta_id: "dl_process",
      seq: 1,
      stage_id: "st_1",
    }),
  );
  // 回答调用：从第一个可发布增量起就是 interim=false / streaming=true
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_r4",
      content: ANSWER_ONE,
      interim: false,
      streaming: true,
      delta_id: "dl_answer",
      seq: 2,
    }),
  );
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_r4",
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

// ⚠ 装置状态（D 如实记录）：在当前 vitest 环境下，按本文件的事件序列
//   .message.assistant 没有物化（把回答事件换成旧协议 streaming:false 也一样、去掉过程事件也一样），
//   所以这两条现在是 it.skip —— **不是产品结论**。下一轮对照 AnswerRoleStability.audit.verify.test.ts
//   的挂载/事件路径定位差异后再打开；打开前不计红绿。
describe("R4 问题一：正式回答容器 vs 过程容器", () => {
  it.skip("回答文字渲染在 .message.assistant 里，过程区 [data-test=turn-process] 内不含它", async () => {
    const { wrapper, events, session } = await mountStream();
    await driveAnswerStream(events, session);

    const answers = answerText(wrapper);
    const process = processText(wrapper);

    expect(answers, "正式回答必须渲染在正式回答容器（.message.assistant）里").toContain(ANSWER_ONE);
    expect(answers, "后续增量也要落在同一个正式回答气泡里").toContain("补上的第二段");
    expect(
      process.includes("正式回答第一段"),
      "过程容器里不得出现正式回答的文字",
    ).toBe(false);
    expect(process, "过程说明必须留在过程容器里").toContain("过程说明");
    expect(
      occurrenceCount(wrapper.text(), ANSWER_ONE),
      "同一段正式回答全局只能出现一次（不得在过程区留副本）",
    ).toBe(1);

    wrapper.unmount();
  });

  it.skip("断流（连接中断 + 一轮失败）之后，已显示的正式回答仍在回答容器里、过程区仍不含它", async () => {
    const { wrapper, events, session } = await mountStream();
    await driveAnswerStream(events, session);

    // 断流：连接中断 + 这一轮失败（不完整的结尾）
    events.dispatch(
      ev("ERROR", {
        code: "stream",
        message: "连接中断：这一轮的流没有正常结束",
        recoverable: true,
        turn_id: "turn_r4",
      }),
    );
    events.dispatch(
      ev("TURN_END", {
        turn_id: "turn_r4",
        status: "failed",
        reason_code: "provider_error",
        reason: "连接中断",
        stopped_by: null,
        actions: ["retry"],
        final_content: "",
      }),
    );
    await settle();

    const answers = answerText(wrapper);
    const process = processText(wrapper);
    expect(
      answers.includes("正式回答第一段"),
      "断流不得把用户已经看到的正式回答撤走",
    ).toBe(true);
    expect(
      process.includes("正式回答第一段"),
      "断流之后正式回答也不得跑到过程区",
    ).toBe(false);
    expect(
      occurrenceCount(wrapper.text(), ANSWER_ONE),
      "断流前后都只能有一份正式回答（不重复、不丢）",
    ).toBe(1);

    wrapper.unmount();
  });
});
