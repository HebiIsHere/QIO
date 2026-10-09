/**
 * 收尾轮 · D 的反例测试（19b：任务列表定位必须真实移动板面）。
 *
 * 反例：任务列表里的「在板面上定位」只改变局部聚焦项（focusedId + 重新测量浮条），
 * 板面纹丝不动 —— 用户点它之后看不到预览在哪里。
 * 应有结果：与单项审批浮条同一条通道（qio:interactive:locate-preview）真实通知，
 * 由 BoardCanvas 真实平移板面把预览带进视口。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import IntentStatusPopover from "../../components/interactive/IntentStatusPopover.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as board from "../board";
import { previewBounds } from "../approval";
import type { BoardState, Intent } from "../types";

function intentFixture(status: Intent["status"]): Intent {
  const seed = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "材料一" }), { kind: "text", content: "材料二" });
  return {
    id: "intent_1",
    boardId: "board_t",
    submissionId: null,
    title: "整理两份材料",
    summary: "",
    status,
    preview: {
      cards: [
        { ...seed.cards[0], id: "pv_a", x: 120, y: 200, w: 120, h: 70 },
        { ...seed.cards[1], id: "pv_b", x: 320, y: 200, w: 120, h: 70 },
      ],
      groups: [],
      links: [],
      note: "",
    },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
  };
}

describe("IntentStatusPopover 任务定位（19b）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  const mounted: VueWrapper[] = [];
  const seen: { intentId?: string; bounds?: unknown }[] = [];
  let listener: EventListener | null = null;

  afterEach(() => {
    while (mounted.length) {
      const wrapper = mounted.pop();
      try {
        wrapper?.unmount();
      } catch {
        /* 已经卸载过 */
      }
    }
    if (listener) window.removeEventListener("qio:interactive:locate-preview", listener);
    listener = null;
    seen.length = 0;
    document.body.innerHTML = "";
  });

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    listener = (event: Event) => {
      const detail = (event as CustomEvent).detail ?? {};
      seen.push({ intentId: detail.intentId, bounds: detail.bounds });
    };
    window.addEventListener("qio:interactive:locate-preview", listener);
  });

  it("点任务列表的「在板面上定位」→ 发出真实定位通知（带预览板面范围）", async () => {
    const intent = intentFixture("running");
    store.intents = [intent];
    store.tasksOpen = true;
    const wrapper = mount(IntentStatusPopover, { attachTo: document.body });
    mounted.push(wrapper);
    await wrapper.vm.$nextTick();
    expect(seen).toHaveLength(0); // 打开浮层本身不产生定位
    const button = wrapper.find('[data-im="task-locate"]');
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    await wrapper.vm.$nextTick();
    expect(seen).toHaveLength(1);
    expect(seen[0].intentId).toBe("intent_1");
    expect(seen[0].bounds).toEqual(previewBounds(intent.preview));
    wrapper.unmount();
  });

  it("没有预览位置的任务：如实无通知，不伪造 bounds", async () => {
    const intent: Intent = { ...intentFixture("paused"), preview: { cards: [], groups: [], links: [] } };
    store.intents = [intent];
    store.tasksOpen = true;
    const wrapper = mount(IntentStatusPopover, { attachTo: document.body });
    mounted.push(wrapper);
    await wrapper.vm.$nextTick();
    await wrapper.find('[data-im="task-locate"]').trigger("click");
    await wrapper.vm.$nextTick();
    expect(seen).toHaveLength(0);
    wrapper.unmount();
  });
});