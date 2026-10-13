/**
 * 对话窗口 ↔ 审批批量列表的互相避让（Lead 集成边界，契约 IRL §7.4 / §5.4）。
 *
 * 背景：A 把对话窗口改成可自由移动 / 调整尺寸，D 让审批入口在不展开详情时也可见。
 * 两者都贴右下，旧几何里「聊天永远占右侧固定一列」的前提失效 —— 这条用例证明
 * 批量列表与切换条会**按当前真实的**对话窗口矩形让开，而不是叠上去。
 *
 * 断言口径：不比死一个像素数（几何计划本身可能演进），只锁定**不变量**：
 * 让开之后，批量列表的矩形与对话窗口矩形不相交，且仍落在视口内。
 * 只在对话窗口发布了显示框时生效；未发布 / 已收起时与既有几何逐字节一致。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import IntentBatchTray from "../IntentBatchTray.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { INTENT_BATCH_STORAGE_KEY } from "../../../interactive/approval";
import { OVERLAY_BATCH_FOOTER, OVERLAY_EDGE, OVERLAY_GAP } from "../../../interactive/overlayLayout";
import { publishChatFrame, resetChatFrameState } from "../../../interactive/chatFrameState";
import type { BoardCard, Intent } from "../../../interactive/types";

const WIDE = { width: 1200, height: 800, stageTop: 52, stageHeight: 748, toolbarTop: 733 };
const TOGGLE_HEIGHT = 39;

let wrapper: VueWrapper | null = null;
let stage: HTMLElement;
let stageHost: HTMLElement;
let originalRect: typeof Element.prototype.getBoundingClientRect;

function fakeRect(top: number, height: number, left = 0, width = WIDE.width): DOMRect {
  return {
    x: left,
    y: top,
    top,
    left,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

function card(id: string): BoardCard {
  return {
    id,
    kind: "reply",
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
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  };
}

function intent(id: string): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    status: "pending",
    title: "演示任务 " + id,
    summary: "",
    preview: { cards: [card("c_" + id)], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  };
}

function recordBatches(ids: string[]): void {
  localStorage.setItem(
    INTENT_BATCH_STORAGE_KEY,
    JSON.stringify({ version: 1, records: ids.map((id) => ({ id, key: "session:demo" })) }),
  );
}

async function flushMeasure(): Promise<void> {
  await new Promise((done) => setTimeout(done, 5));
  await new Promise((done) => setTimeout(done, 5));
}

function stageStyle(): string {
  return stageHost.getAttribute("style") ?? "";
}

function stageVar(name: string): number {
  const match = stageStyle().match(new RegExp(name + ":\\s*(-?[0-9.]+)px"));
  return match ? Number(match[1]) : NaN;
}

function stageMode(): string {
  return stageHost.getAttribute("im-geo-mode") ?? "";
}

/** 批量列表在视口里的矩形（右贴 --im-geo-batch-right，宽 --im-geo-batch-max-w） */
function batchRect(): { left: number; right: number; width: number } {
  const right = stageVar("--im-geo-batch-right");
  const width = stageVar("--im-geo-batch-max-w");
  return { right: WIDE.width - right, left: WIDE.width - right - width, width };
}

async function mountTray(chatOpen: boolean, batchOpen: boolean): Promise<VueWrapper> {
  const store = useInteractiveStore();
  store.intents = [intent("i0"), intent("i1"), intent("i2"), intent("i3")];
  store.chatOpen = chatOpen;
  store.batchOpen = batchOpen;
  recordBatches(["i0", "i1", "i2", "i3"]);
  const mounted = mount(IntentBatchTray, { attachTo: stage });
  stageHost = mounted.element;
  await flushMeasure();
  return mounted;
}

/** 未发布任何显示框时的基线几何（每条用例自己量一次，不写死像素） */
async function baseline(): Promise<{ right: number; width: number; mode: string }> {
  resetChatFrameState();
  const mounted = await mountTray(true, true);
  const value = { right: stageVar("--im-geo-batch-right"), width: stageVar("--im-geo-batch-max-w"), mode: stageMode() };
  mounted.unmount();
  wrapper = null;
  await flushMeasure();
  return value;
}

function clearOf(rect: { left: number; right: number }, chat: { x: number; y: number; w: number; h: number }): boolean {
  // 要么整体在窗口左边、要么整体在窗口右边，两种都算让开；相交就不算。
  return rect.right <= chat.x - OVERLAY_GAP + 1 || rect.left >= chat.x + chat.w + OVERLAY_GAP - 1;
}

beforeEach(() => {
  setActivePinia(createPinia());
  resetChatFrameState();
  document.body.innerHTML = "";
  localStorage.clear();
  Object.defineProperty(window, "innerWidth", { value: WIDE.width, configurable: true, writable: true });
  Object.defineProperty(window, "innerHeight", { value: WIDE.height, configurable: true, writable: true });
  originalRect = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element): DOMRect {
    const hook = this.getAttribute("data-im");
    if (hook === "board-toolbar") return fakeRect(WIDE.toolbarTop, 96, 0, WIDE.width);
    if (hook === "chat-toggle") return fakeRect(WIDE.toolbarTop - 58, TOGGLE_HEIGHT, 0, 88);
    if (this.hasAttribute("data-v-app") || this.classList.contains("im-stage")) {
      return fakeRect(WIDE.stageTop, WIDE.stageHeight, 0, WIDE.width);
    }
    return fakeRect(0, 0);
  };
  stage = document.createElement("div");
  stage.className = "im-stage";
  document.body.appendChild(stage);
  const toolbar = document.createElement("div");
  toolbar.setAttribute("data-im", "board-toolbar");
  document.body.appendChild(toolbar);
  const toggle = document.createElement("button");
  toggle.setAttribute("data-im", "chat-toggle");
  document.body.appendChild(toggle);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  Element.prototype.getBoundingClientRect = originalRect;
  resetChatFrameState();
});

describe("批量列表让开用户控制的对话窗口", () => {
  it("对话窗口占住右侧：让开之后与窗口不相交，且仍落在视口内", async () => {
    const chat = { x: 700, y: 400, w: 460, h: 340 };
    publishChatFrame(chat);
    wrapper = await mountTray(true, true);
    const rect = batchRect();
    expect(clearOf(rect, chat)).toBe(true);
    expect(rect.left).toBeGreaterThanOrEqual(OVERLAY_EDGE - 1);
    expect(rect.right).toBeLessThanOrEqual(WIDE.width - OVERLAY_EDGE + 1);
  });

  it("对话窗口在左侧：让开之后仍然不相交", async () => {
    const chat = { x: OVERLAY_EDGE, y: 300, w: 300, h: 300 };
    publishChatFrame(chat);
    wrapper = await mountTray(true, true);
    expect(clearOf(batchRect(), chat)).toBe(true);
  });

  it("对话窗口竖直方向不在这条带上（贴顶）：几何与基线一致", async () => {
    const base = await baseline();
    publishChatFrame({ x: 700, y: 0, w: 460, h: 40 });
    wrapper = await mountTray(true, true);
    expect(stageVar("--im-geo-batch-right")).toBe(base.right);
    expect(stageVar("--im-geo-batch-max-w")).toBe(base.width);
  });

  it("对话窗口收起（collapsed）：空间视为可用，几何与基线一致", async () => {
    const base = await baseline();
    publishChatFrame({ x: 700, y: 400, w: 460, h: 340 }, true);
    wrapper = await mountTray(true, true);
    expect(stageVar("--im-geo-batch-right")).toBe(base.right);
    expect(stageVar("--im-geo-batch-max-w")).toBe(base.width);
  });

  it("未发布显示框：几何与基线一致（既有行为不变）", async () => {
    const base = await baseline();
    wrapper = await mountTray(true, true);
    expect(stageVar("--im-geo-batch-right")).toBe(base.right);
    expect(stageVar("--im-geo-batch-max-w")).toBe(base.width);
    expect(stageMode()).toBe(base.mode);
  });

  it("对话窗口吃光整条带：进入切换显示，绝不硬叠", async () => {
    publishChatFrame({ x: OVERLAY_EDGE, y: 120, w: WIDE.width - OVERLAY_EDGE * 2, h: WIDE.height - 200 });
    wrapper = await mountTray(true, true);
    expect(stageMode()).toBe("switched");
  });

  it("让开之后的批量面板底边仍不越过工具栏", async () => {
    publishChatFrame({ x: 700, y: 400, w: 460, h: 340 });
    wrapper = await mountTray(true, true);
    const height = stageVar("--im-geo-batch-max-h");
    expect(height).toBeLessThanOrEqual(WIDE.toolbarTop - OVERLAY_BATCH_FOOTER - WIDE.stageTop);
  });
});
