/**
 * IRL-D · 审批入口可达、审批浮条避让、待审批预览卡的查看尺寸（任务书 §5.4 / §3.1）。
 *
 * 三条硬要求在这里被证明：
 * - **聊天收起时审批入口仍可达**：提交区里的待审批条目、右上批量入口、贴着预览的审批浮条，
 *   都不依赖对话窗口是否展开；
 * - **避让以共享桥为准**：对话窗口移动 / 调整 / 收起后，浮条按 chatObstacleFrame() 避让
 *   （收起 = 这块空间可用），多个面板不互相遮挡；
 * - **尺寸只是查看**：浮条的八向拖动 / 键盘 −＋ 复用公共尺寸原语，
 *   不写板面状态、不产生审批与意图、不调用模型。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import IntentStatusPopover from "../IntentStatusPopover.vue";
import IntentBatchTray from "../IntentBatchTray.vue";
import SubmitCluster from "../SubmitCluster.vue";
import IntentPreviewCard from "../IntentPreviewCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { INTENT_BATCH_STORAGE_KEY } from "../../../interactive/approval";
import { RESIZE_STEP } from "../../../interactive/resize";
import { publishChatFrame, resetChatFrameState, setChatCollapsed } from "../../../interactive/chatFrameState";
import type { BoardCard, Intent, Understanding } from "../../../interactive/types";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ pending: { expressions: [] } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
  saveBoardState: vi.fn(async () => ({ ok: true, seq: 1, savedAt: "t" })),
  submitBoard: vi.fn(),
  fetchUnderstanding: vi.fn(async () => ({ understanding: null, understandings: [] })),
  fetchExecutions: vi.fn(async () => ({ executions: [] })),
  fetchAcceptance: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  pauseIntent: vi.fn(),
  resumeIntent: vi.fn(),
  cancelIntent: vi.fn(),
  batchDecide: vi.fn(),
}));

const NOW = "2026-10-13T00:00:00.000Z";
const mounted: VueWrapper[] = [];
const VIEW = { width: 1024, height: 768 };
let originalRect: typeof Element.prototype.getBoundingClientRect | null = null;

function card(id: string): BoardCard {
  return {
    id,
    kind: "text",
    content: "预览内容",
    meta: {},
    x: 40,
    y: 40,
    w: 240,
    h: 96,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function intentFixture(over: Partial<Intent> = {}): Intent {
  return {
    id: "i1",
    boardId: "board_t",
    submissionId: "s1",
    understandingId: "u1",
    title: "整理两份材料",
    summary: "",
    status: "pending",
    preview: { cards: [card("pv_a")], groups: [], links: [], note: "" },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    createdAt: NOW,
    updatedAt: NOW,
    ...over,
  };
}

function understandingFixture(over: Partial<Understanding> = {}): Understanding {
  return {
    id: "u1",
    boardId: "board_t",
    submissionId: "s1",
    requestId: "req-1",
    status: "understood",
    stage: "",
    kind: "intent",
    steps: [],
    materials: [],
    summary: "",
    clarification: null,
    validation: { ok: true, reasons: [] },
    error: null,
    attempt: 1,
    adapter: "fake",
    intentIds: [],
    memory: [],
    createdAt: NOW,
    updatedAt: NOW,
    ...over,
  };
}

function freshStore() {
  const store = useInteractiveStore();
  store.board = {
    boardId: "board_t",
    seq: 1,
    updatedAt: NOW,
    cards: [],
    groups: [],
    links: [],
    selection: [],
  };
  return store;
}

function track<T extends VueWrapper>(wrapper: T): T {
  mounted.push(wrapper);
  return wrapper;
}

/** jsdom 没有布局：只给用例真正需要的矩形，其余一律零矩形（等于「量不到」）。 */
function installRects(map: Record<string, { left: number; top: number; width: number; height: number }>): void {
  originalRect = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element): DOMRect {
    const hook = this.getAttribute("data-im") ?? "";
    const box = map[hook];
    if (!box) {
      return { x: 0, y: 0, top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0, toJSON: () => ({}) } as DOMRect;
    }
    return {
      x: box.left,
      y: box.top,
      top: box.top,
      left: box.left,
      width: box.width,
      height: box.height,
      right: box.left + box.width,
      bottom: box.top + box.height,
      toJSON: () => ({}),
    } as DOMRect;
  };
}

function fakePreview(intentId: string, box: { left: number; top: number; width: number; height: number }): void {
  const node = document.createElement("div");
  node.setAttribute("data-im", "preview");
  node.setAttribute("data-intent-id", intentId);
  node.getBoundingClientRect = () =>
    ({
      x: box.left,
      y: box.top,
      top: box.top,
      left: box.left,
      width: box.width,
      height: box.height,
      right: box.left + box.width,
      bottom: box.top + box.height,
      toJSON: () => ({}),
    }) as DOMRect;
  document.body.appendChild(node);
}

function recordBatches(groups: Record<string, string[]>): void {
  const records = Object.entries(groups).flatMap(([key, ids]) => ids.map((id) => ({ id, key })));
  localStorage.setItem(INTENT_BATCH_STORAGE_KEY, JSON.stringify({ version: 1, records }));
}

/** 组件里的测量是节流的（0ms / 60ms 两个定时器）：等它们跑完再看位置。 */
async function settle(): Promise<void> {
  await new Promise((done) => setTimeout(done, 120));
}

function stripBox(): { left: number; top: number; right: number; bottom: number } {
  const root = document.querySelector('[data-im="intent"][data-docked="1"]') as HTMLElement | null;
  if (!root) throw new Error("审批浮条不在 DOM 里");
  const left = Number.parseFloat(root.style.left) || 0;
  const top = Number.parseFloat(root.style.top) || 0;
  return { left, top, right: left + 360, bottom: top + 158 };
}

function overlaps(
  a: { left: number; top: number; right: number; bottom: number },
  b: { left: number; top: number; right: number; bottom: number },
): boolean {
  return (
    Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0 &&
    Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0
  );
}

beforeEach(() => {
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  localStorage.clear();
  resetChatFrameState();
  Object.defineProperty(window, "innerWidth", { value: VIEW.width, configurable: true, writable: true });
  Object.defineProperty(window, "innerHeight", { value: VIEW.height, configurable: true, writable: true });
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  resetChatFrameState();
  if (originalRect) Element.prototype.getBoundingClientRect = originalRect;
  originalRect = null;
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("聊天收起时审批入口仍可达（§5.4）", () => {
  function withPendingApproval() {
    const store = freshStore();
    store.chatOpen = false;
    store.submitStatus = "succeeded";
    store.understanding = understandingFixture();
    store.understandingStatus = "understood";
    store.intents = [intentFixture({ id: "i1" })];
    return store;
  }

  it("提交区里的待审批条目与操作不依赖对话窗口", () => {
    const store = withPendingApproval();
    const w = track(mount(SubmitCluster));
    expect(store.chatOpen).toBe(false);
    expect(w.get('[data-im="submit-approval-state"]').text()).toContain("待审批 1 项");
    expect(w.find('[data-im="submit-approve"]').exists()).toBe(true);
    expect(w.find('[data-im="submit-reject"]').exists()).toBe(true);
    expect(w.find('[data-im="submit-locate"]').exists()).toBe(true);
  });

  it("任务浮层里贴着预览的审批浮条仍给出批准 / 拒绝 / 定位", () => {
    const store = withPendingApproval();
    store.tasksOpen = true;
    installRects({});
    const w = track(mount(IntentStatusPopover, { attachTo: document.body }));
    const strip = w.get('[data-im="intent"][data-docked="1"]');
    expect(strip.attributes("data-intent-id")).toBe("i1");
    // 待审批（不够批量列表门槛）也能从这里处理
    expect(strip.find('[data-im="approve"]').exists()).toBe(true);
    expect(strip.find('[data-im="reject"]').exists()).toBe(true);
    expect(strip.find('[data-im="preview-locate"]').exists()).toBe(true);
  });

  it("批量入口在聊天收起时仍然在（同一批够四项才出现）", () => {
    const store = freshStore();
    store.chatOpen = false;
    store.batchOpen = false;
    store.intents = ["i1", "i2", "i3", "i4"].map((id) =>
      intentFixture({ id, status: "pending", understandingId: "u1" }),
    );
    recordBatches({ "session:submission:s1": ["i1", "i2", "i3", "i4"] });
    const w = track(mount(IntentBatchTray, { attachTo: document.body }));
    expect(w.find('[data-im="batch-entry"]').exists()).toBe(true);
  });
});

describe("审批浮条按共享桥避让对话窗口、不与其他面板互压", () => {
  const ANCHOR = { left: 100, top: 100, width: 400, height: 200 };

  function mountPopoverWithPreview() {
    const store = freshStore();
    store.tasksOpen = true;
    store.chatOpen = true;
    store.intents = [intentFixture({ id: "i1", status: "running" })];
    fakePreview("i1", ANCHOR);
    installRects({ "board-toolbar": { left: 0, top: 700, width: VIEW.width, height: 60 } });
    const w = track(mount(IntentStatusPopover, { attachTo: document.body }));
    return { store, w };
  }

  it("对话窗口占着预览下方时，浮条换到不重叠的位置", async () => {
    const chat = { left: 100, top: 310, right: 500, bottom: 510 };
    publishChatFrame({ x: chat.left, y: chat.top, w: chat.right - chat.left, h: chat.bottom - chat.top }, false);
    mountPopoverWithPreview();
    await settle();
    const box = stripBox();
    expect(overlaps(box, chat), "浮条压住了对话窗口").toBe(false);
    expect(box.bottom, "浮条整体留在对话窗口上方").toBeLessThanOrEqual(chat.top);
  });

  it("对话窗口收起后，那块空间重新可用（不再当作障碍）", async () => {
    publishChatFrame({ x: 100, y: 310, w: 400, h: 200 }, false);
    mountPopoverWithPreview();
    await settle();
    const openBox = stripBox();
    expect(openBox.top).not.toBe(310);

    setChatCollapsed(true);
    window.dispatchEvent(new Event("resize"));
    await settle();
    const collapsedBox = stripBox();
    expect(collapsedBox.top, "收起后浮条回到预览下方").toBe(310);
  });

  it("任务浮层自己也是要避开的对象：两个面板不许互相遮挡", async () => {
    const pop = { left: 100, top: 300, right: 500, bottom: 520 };
    const store = freshStore();
    store.tasksOpen = true;
    store.chatOpen = false;
    store.intents = [intentFixture({ id: "i1", status: "running" })];
    fakePreview("i1", ANCHOR);
    installRects({});
    const w = track(mount(IntentStatusPopover, { attachTo: document.body }));
    // 任务浮层的矩形（量到之后浮条要避开它）
    const popEl = w.get('[data-im="tasks-popover"]').element as HTMLElement;
    popEl.getBoundingClientRect = () =>
      ({
        x: pop.left,
        y: pop.top,
        top: pop.top,
        left: pop.left,
        width: pop.right - pop.left,
        height: pop.bottom - pop.top,
        right: pop.right,
        bottom: pop.bottom,
        toJSON: () => ({}),
      }) as DOMRect;
    window.dispatchEvent(new Event("resize"));
    await settle();
    const box = stripBox();
    expect(overlaps(box, pop), "浮条压住了任务浮层").toBe(false);
  });
});

describe("待审批预览卡的查看尺寸（§3.1）", () => {
  it("键盘 −/＋ 只改这一处的显示尺寸：不写板面、不产生审批与意图", async () => {
    const store = freshStore();
    const approve = vi.spyOn(store, "approve");
    const reject = vi.spyOn(store, "reject");
    const commit = vi.spyOn(store, "commit");
    const before = JSON.stringify(store.board);
    const w = track(mount(IntentPreviewCard, { props: { intent: intentFixture(), docked: true }, attachTo: document.body }));
    const root = w.get('[data-im="intent"]').element as HTMLElement;

    await w.get('[data-resize-key="w+"]').trigger("click");
    expect(root.style.width).toBe(360 + RESIZE_STEP + "px");
    await w.get('[data-resize-key="h-"]').trigger("click");
    expect(root.style.height).toBe(220 - RESIZE_STEP + "px");

    expect(approve).not.toHaveBeenCalled();
    expect(reject).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    expect(JSON.stringify(store.board)).toBe(before);
    expect(store.understandingStatus).toBeNull();
  });

  it("八向拖动跟手、松手提交一次；Esc 取消回到拖动前那一版", async () => {
    const wrapper = track(
      mount(IntentPreviewCard, { props: { intent: intentFixture(), docked: true }, attachTo: document.body }),
    );
    const root = wrapper.get('[data-im="intent"]').element as HTMLElement;
    await wrapper.get('[data-resize-key="w+"]').trigger("click");
    const committed = root.style.width;
    expect(committed).toBe(360 + RESIZE_STEP + "px");

    const handle = wrapper.get('[data-resize-handle="se"]').element;
    handle.dispatchEvent(new MouseEvent("pointerdown", { clientX: 0, clientY: 0, button: 0, bubbles: true }));
    document.dispatchEvent(new MouseEvent("pointermove", { clientX: 40, clientY: 30, bubbles: true }));
    await wrapper.vm.$nextTick();
    const dragged = root.style.width;
    expect(dragged, "拖动中要跟手").not.toBe(committed);

    // Esc：回到拖动前那一版，不提交
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(root.style.width).toBe(committed);

    // 再拖一次并松手：尺寸落到这一版
    handle.dispatchEvent(new MouseEvent("pointerdown", { clientX: 0, clientY: 0, button: 0, bubbles: true }));
    document.dispatchEvent(new MouseEvent("pointermove", { clientX: 40, clientY: 30, bubbles: true }));
    document.dispatchEvent(new MouseEvent("pointerup", { clientX: 40, clientY: 30, bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(root.style.width).toBe(dragged);
  });

  it("列表里的完整卡片不带浮条把手（尺寸只在浮条形态下生效）", () => {
    const wrapper = track(mount(IntentPreviewCard, { props: { intent: intentFixture() } }));
    expect(wrapper.find('[data-resize-root="1"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="approve"]').exists()).toBe(true);
  });
});
