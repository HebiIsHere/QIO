/**
 * D 组验收（页面级）共用挂载设施 —— 只被 acceptance-i2v2-page-*.test.ts 使用，不是产品代码。
 *
 * 为什么需要：互动页的真实接线在 InteractiveView.vue 里（F01 影响确认界面、F03 任务入口、
 * F05 恢复入口、F02/F06 意图来源）。验收测试要挂**真实页面**，只把重型子组件换成轻替身，
 * 服务层用受控假数据（不连真实后端 / 不调用模型）。
 */
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h } from "vue";
import InteractiveView from "../InteractiveView.vue";
import * as api from "../../services/interactive";
import * as attachmentsApi from "../../services/attachments";
import { useInteractiveStore } from "../../stores/interactive";
import { emptyState } from "../../interactive/board";
import type { BoardState } from "../../interactive/types";

/** 画布替身：保留 overlay 插槽（IntentLayer 真的渲染在页面里），其余几何无关紧要。 */
export const BoardCanvasStub = defineComponent({
  name: "BoardCanvas",
  setup(_props, { slots }) {
    return () =>
      h("div", { "data-stub": "board-canvas" }, [
        slots.overlay ? slots.overlay({ scale: 1, width: 2400, height: 1600 }) : null,
      ]);
  },
});

/** 会话流替身（消息列表不属于本轮验收）。 */
export const MessageStreamStub = defineComponent({
  name: "MessageStream",
  setup() {
    return () => h("div", { "data-stub": "stream" });
  },
});

/** 输入框替身：保留 leading 插槽，页面结构与真实一致即可。 */
export const ComposerStub = defineComponent({
  name: "Composer",
  props: { chatBinding: { type: Object, default: undefined } },
  setup(props, { slots }) {
    return () =>
      h("div", { "data-stub": "composer", "data-binding": props.chatBinding ? JSON.stringify(props.chatBinding) : "" }, [
        slots.leading ? slots.leading() : null,
      ]);
  },
});

export interface PageHarness {
  wrapper: VueWrapper;
  store: ReturnType<typeof useInteractiveStore>;
}

export interface MountPageOptions {
  /** 板上初始状态（默认空板）。 */
  board?: BoardState;
  /** true = 用真实 BoardCanvas（F02 需要旧预览层与新意图层同屏对比）。 */
  realBoard?: boolean;
}

/**
 * 受控服务层：把 store / 页面会调到的接口全部换成可断言的假实现。
 * 返回值里的 api 是**真实模块对象**（spyOn 之后仍然可用），方便用例逐个改回执。
 */
export function stubServices() {
  const state = emptyState("board_t");
  const serverBoard = { ...state, boardId: "board_t" };
  const spies = {
    fetchBoardState: vi.spyOn(api, "fetchBoardState").mockResolvedValue({
      board: { id: "board_t", title: "验收板面" },
      state: serverBoard,
      seq: 0,
      submissions: [],
      drafts: { drafts: {}, rev: 0 },
    } as never),
    fetchVisibleRange: vi.spyOn(api, "fetchVisibleRange").mockResolvedValue({ visibleRange: {} } as never),
    fetchIntents: vi.spyOn(api, "fetchIntents").mockResolvedValue({
      intents: [],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never),
    fetchUnderstanding: vi.spyOn(api, "fetchUnderstanding").mockResolvedValue({
      understanding: null,
      understandings: [],
    } as never),
    fetchExecutions: vi.spyOn(api, "fetchExecutions").mockResolvedValue({ executions: [] } as never),
    fetchAcceptance: vi.spyOn(api, "fetchAcceptance").mockResolvedValue({ acceptance: null } as never),
    fetchIntentViews: vi.spyOn(api, "fetchIntentViews").mockResolvedValue({
      available: true,
      source: "service",
      intents: [],
      batch_confirm: { min: 2, eligible_ids: [], available: true },
    } as never),
    saveIntentDisplay: vi.spyOn(api, "saveIntentDisplay").mockResolvedValue({
      ok: true,
      display: { x: 0, y: 0 },
    } as never),
    saveBoardState: vi.spyOn(api, "saveBoardState").mockResolvedValue({ seq: 1, state: serverBoard } as never),
    checkMaterialImpact: vi.spyOn(api, "checkMaterialImpact").mockResolvedValue({ ok: true } as never),
    fetchRecoveryPreview: vi.spyOn(api, "fetchRecoveryPreview").mockResolvedValue({ available: true, preview: null } as never),
    recoverIntent: vi.spyOn(api, "recoverIntent").mockResolvedValue({ ok: true } as never),
    pauseIntent: vi.spyOn(api, "pauseIntent").mockResolvedValue({ ok: true } as never),
    resumeIntent: vi.spyOn(api, "resumeIntent").mockResolvedValue({ ok: true } as never),
    cancelIntent: vi.spyOn(api, "cancelIntent").mockResolvedValue({ ok: true } as never),
    batchDecide: vi.spyOn(api, "batchDecide").mockResolvedValue({ ok: true } as never),
    batchConfirmIntents: vi.spyOn(api, "batchConfirmIntents").mockResolvedValue({ results: [] } as never),
    confirmSingleIntent: vi.spyOn(api, "confirmSingleIntent").mockResolvedValue({ results: [] } as never),
    fetchWaiting: vi.spyOn(api, "fetchWaiting").mockResolvedValue({ available: true, waiting: null } as never),
    fetchLedger: vi.spyOn(api, "fetchLedger").mockResolvedValue({ available: true, counts: {} } as never),
    fetchIntentResults: vi.spyOn(api, "fetchIntentResults").mockResolvedValue({ available: true, results: [] } as never),
  };
  // 附件阅读位置不是本轮验收对象；不联网即可。
  try {
    vi.spyOn(attachmentsApi, "putBoardPreviewPosition").mockResolvedValue({ ok: true } as never);
  } catch {
    /* 该导出不存在时不影响页面挂载 */
  }
  return spies;
}

export function resetServices(): void {
  vi.restoreAllMocks();
  localStorage.clear();
}

/**
 * 挂真实互动页。只替身：画布（保留 overlay 插槽）、消息流、输入框、顶栏、工具栏、模式切换。
 * 恢复面板 / 影响确认框 / 事项行都保持真实实现 —— 它们正是验收对象。
 */
export function mountInteractivePage(board?: BoardState, options: MountPageOptions = {}): PageHarness {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useInteractiveStore();
  if (board) store.board = board;
  const stubs: Record<string, unknown> = {
    BoardCanvas: options.realBoard ? false : BoardCanvasStub,
        InteractiveChromeTools: true,
        InteractiveBookmarks: true,
        MessageStream: MessageStreamStub,
        Composer: ComposerStub,
        BoardToolbar: true,
        ConversationModeSwitch: true,
    ExecutionRecordPanel: true,
  };
  const wrapper = mount(InteractiveView, {
    attachTo: document.body,
    global: {
      plugins: [pinia],
      stubs,
    },
  });
  return { wrapper, store };
}

/** 等页面上的异步读取（store.load → loadIntents → 恢复面板）落定。 */
export async function settlePage(wrapper: VueWrapper): Promise<void> {
  await flushPromises();
  await wrapper.vm.$nextTick();
  await flushPromises();
  await wrapper.vm.$nextTick();
}

/** 给虚拟意图卡片定位（data-intent-id 在虚拟卡与旧预览上都存在，这里只取虚拟卡）。 */
export function virtualIntent(wrapper: VueWrapper, intentId: string) {
  return wrapper
    .findAll('[data-im="virtual-intent"]')
    .find((item) => item.attributes("data-intent-id") === intentId);
}

/** 从 GPU 样式里读 left / top（jsdom 不解析布局，两者都是内联样式）。 */
export function stylePoint(element: Element | undefined): { x: number; y: number } {
  const style = element?.getAttribute("style") ?? "";
  const read = (name: string) => {
    const match = new RegExp(name + ":\\s*([-0-9.]+)px").exec(style);
    return match ? Number(match[1]) : Number.NaN;
  };
  return { x: read("left"), y: read("top") };
}
