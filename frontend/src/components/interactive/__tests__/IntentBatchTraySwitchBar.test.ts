/**
 * 面板切换条的真实定位、预留与按钮状态（子智能体 C 负责，契约 §11.7 / §11.8）。
 *
 * 上一轮的缺陷：切换条只拿到 `bottom / height`（元素是 static，等于没定位），
 * 而且关掉一个面板之后竖直区间不再为它预留 —— 面板直接压在切换条上。
 *
 * 这里挂真实组件、喂进真实量到的矩形（jsdom 没有布局，所以 rect 由用例给出）：
 * - 位置与高度必须来自几何计划的矩形，且落在面板与工具栏之间；
 * - 关掉 / 切换面板之后预留**不变**（与"两面板都开"同一个状态算出来的数字相同）；
 * - 按钮有选中态、可点击、可用键盘聚焦，样式全部走令牌（扫源码文本）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import IntentBatchTray from "../IntentBatchTray.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { INTENT_BATCH_STORAGE_KEY } from "../../../interactive/approval";
import {
  OVERLAY_CHAT_FOOTER,
  OVERLAY_EDGE,
  OVERLAY_GAP,
  OVERLAY_SWITCH_BAR_HEIGHT,
} from "../../../interactive/overlayLayout";
import type { BoardCard, Intent } from "../../../interactive/types";

let wrapper: VueWrapper | null = null;
let stage: HTMLElement;
let toolbar: HTMLElement;
/**
 * 几何变量写在「组件根节点的定位父级」上。真实应用里它就是 `.im-stage`；
 * @vue/test-utils 挂载时会多一层应用宿主（`data-v-app`），组件量到的父级就是它 ——
 * 所以这里直接读 `wrapper.element` 的样式，与组件写的是同一个元素。
 */
let stageHost: HTMLElement;
let originalRect: typeof Element.prototype.getBoundingClientRect;

function fakeRect(top: number, height: number, left = 0, width = 480): DOMRect {
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

function recordBatches(groups: Record<string, string[]>): void {
  const records = Object.entries(groups).flatMap(([key, ids]) => ids.map((id) => ({ id, key })));
  localStorage.setItem(INTENT_BATCH_STORAGE_KEY, JSON.stringify({ version: 1, records }));
}

/** 让 scheduleMeasure 的 0ms 定时器跑完（组件自己合并测量，不靠观察循环） */
async function flushMeasure(): Promise<void> {
  await new Promise((done) => setTimeout(done, 5));
  await new Promise((done) => setTimeout(done, 5));
}

/** 480×600：舞台 53 / 工具栏顶边 423（上一轮实测值），空间放不下两个面板 */
const VIEW = { width: 480, height: 600, stageTop: 53, stageHeight: 547, toolbarTop: 423 };

function setViewport(): void {
  Object.defineProperty(window, "innerWidth", { value: VIEW.width, configurable: true, writable: true });
  Object.defineProperty(window, "innerHeight", { value: VIEW.height, configurable: true, writable: true });
}

/**
 * jsdom 没有布局：所有矩形都由用例给出（数字来自上一轮 480×600 的实测值）。
 * 按 data-im 钩子分发，舞台宿主按 data-v-app / .im-stage 认。
 */
function installRects(): void {
  originalRect = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element): DOMRect {
    const hook = this.getAttribute("data-im");
    if (hook === "board-toolbar") return fakeRect(VIEW.toolbarTop, 96, 0, VIEW.width);
    if (hook === "chat-toggle") return fakeRect(VIEW.toolbarTop - 58, 34, 0, 88);
    if (this.hasAttribute("data-v-app") || this.classList.contains("im-stage")) {
      return fakeRect(VIEW.stageTop, VIEW.stageHeight, 0, VIEW.width);
    }
    return fakeRect(0, 0);
  };
}

/** 舞台宿主上的行内样式（几何控制器把 --im-geo-* 写在这里） */
function stageStyle(): string {
  return stageHost.getAttribute("style") ?? "";
}

/** 舞台宿主上的模式标记（side-by-side / stacked / switched） */
function stageMode(): string {
  return stageHost.getAttribute("im-geo-mode") ?? "";
}

function stageVar(name: string): number {
  const match = stageStyle().match(new RegExp(name + ":\\s*(-?[0-9.]+)px"));
  return match ? Number(match[1]) : Number.NaN;
}

async function mountTray(options: { chatOpen: boolean; batchOpen: boolean }): Promise<VueWrapper> {
  const store = useInteractiveStore();
  store.intents = [intent("i0"), intent("i1"), intent("i2"), intent("i3")];
  store.chatOpen = options.chatOpen;
  store.batchOpen = options.batchOpen;
  recordBatches({ "session:demo": ["i0", "i1", "i2", "i3"] });
  const mounted = mount(IntentBatchTray, { attachTo: stage });
  stageHost = mounted.element;
  await flushMeasure();
  return mounted;
}

beforeEach(() => {
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  localStorage.clear();
  setViewport();
  installRects();
  stage = document.createElement("div");
  stage.className = "im-stage";
  document.body.appendChild(stage);
  toolbar = document.createElement("div");
  toolbar.setAttribute("data-im", "board-toolbar");
  document.body.appendChild(toolbar);
  // 刻意不造聊天入口按钮：量不到它时几何层一点都不放宽（宁少几像素），
  // 这样「预留了多少」可以用常量算出来，不必依赖另一条测量链路。
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  Element.prototype.getBoundingClientRect = originalRect;
  vi.restoreAllMocks();
});

describe("切换条的真实定位与高度预留", () => {
  it("480×600 单开对话：切换条有真实定位，落在面板与工具栏之间", async () => {
    wrapper = await mountTray({ chatOpen: true, batchOpen: false });
    const bar = wrapper.find('[data-im="overlay-switch"]');
    expect(bar.exists()).toBe(true);
    expect(stageMode()).toBe("switched");

    // 位置与高度由几何矩形给出：从行内样式反算回视口坐标，必须落在舞台内、工具栏之上
    const style = bar.attributes("style") ?? "";
    const height = Number(/height:\s*([0-9.]+)px/.exec(style)?.[1]);
    const bottom = Number(/bottom:\s*([0-9.]+)px/.exec(style)?.[1]);
    const right = Number(/right:\s*([0-9.]+)px/.exec(style)?.[1]);
    const maxWidth = Number(/max-width:\s*([0-9.]+)px/.exec(style)?.[1]);
    expect(height).toBe(OVERLAY_SWITCH_BAR_HEIGHT);
    expect(right).toBe(OVERLAY_EDGE);
    expect(maxWidth).toBe(VIEW.width - 2 * OVERLAY_EDGE);
    const barTop = VIEW.height - bottom - height;
    const barBottom = barTop + height;
    expect(barTop).toBeGreaterThanOrEqual(VIEW.stageTop + OVERLAY_EDGE);
    expect(barBottom).toBeLessThanOrEqual(VIEW.toolbarTop - OVERLAY_EDGE);
    // 不压到聊天入口按钮（它在工具栏顶边往上 58px 起、高 34px 的那一行）
    expect(barBottom).toBeLessThanOrEqual(VIEW.toolbarTop - 58);
  });

  it("单开对话时切换条那一行是真实预留：比不预留时正好少掉切换条 + 两个间距", async () => {
    wrapper = await mountTray({ chatOpen: true, batchOpen: false });
    const top = VIEW.stageTop + OVERLAY_EDGE;
    const unreserved = VIEW.toolbarTop - OVERLAY_CHAT_FOOTER - top;
    const reserved = unreserved - OVERLAY_SWITCH_BAR_HEIGHT - 2 * OVERLAY_GAP;
    expect(stageVar("--im-geo-chat-max-h")).toBe(reserved);
    expect(stageVar("--im-geo-chat-max-h")).toBeLessThan(unreserved);
  });

  it("切换到另一个面板后预留不变（同一个明确布局状态）", async () => {
    wrapper = await mountTray({ chatOpen: true, batchOpen: false });
    const reserved = stageVar("--im-geo-chat-max-h");
    const store = useInteractiveStore();
    store.batchOpen = true; // 用户又打开审批列表 → 空间不足，只保留最近打开的这一个
    await flushMeasure();
    expect(stageMode()).toBe("switched");
    expect(store.chatOpen).toBe(false);
    expect(stageVar("--im-geo-batch-max-h")).toBe(reserved);
    // 面板与切换条各占各的：批量列表的上限也停在同一列，不会盖住切换条
    expect(stageVar("--im-geo-batch-max-h")).toBeGreaterThan(0);
  });

  it("同一个尺寸重复测量不会改写样式（不引入持续抖动）", async () => {
    wrapper = await mountTray({ chatOpen: true, batchOpen: false });
    const bar = wrapper.find('[data-im="overlay-switch"]');
    const before = bar.attributes("style");
    const stageBefore = stageStyle();
    window.dispatchEvent(new Event("resize"));
    await flushMeasure();
    window.dispatchEvent(new Event("resize"));
    await flushMeasure();
    expect(bar.attributes("style")).toBe(before);
    expect(stageStyle()).toBe(stageBefore);
  });
});

describe("切换条按钮的状态、键盘与主题样式", () => {
  it("当前显示的面板按钮是选中态，另一个可点且真的切换过去", async () => {
    wrapper = await mountTray({ chatOpen: true, batchOpen: false });
    const store = useInteractiveStore();
    const chatBtn = wrapper.find('[data-im="overlay-switch-chat"]');
    const batchBtn = wrapper.find('[data-im="overlay-switch-batch"]');
    expect(chatBtn.attributes("aria-pressed")).toBe("true");
    expect(batchBtn.attributes("aria-pressed")).toBe("false");
    expect(chatBtn.attributes("type")).toBe("button");
    expect(chatBtn.text()).toContain("看对话");
    expect(batchBtn.text()).toContain("看审批列表");

    await batchBtn.trigger("click");
    await flushMeasure();
    expect(store.batchOpen).toBe(true);
    expect(store.chatOpen).toBe(false);
    expect(wrapper.find('[data-im="overlay-switch-batch"]').attributes("aria-pressed")).toBe("true");
    expect(wrapper.find('[data-im="overlay-switch-chat"]').attributes("aria-pressed")).toBe("false");
  });

  it("按钮可以用键盘聚焦（不是装饰元素）", async () => {
    wrapper = await mountTray({ chatOpen: true, batchOpen: false });
    const chatBtn = wrapper.find('[data-im="overlay-switch-chat"]').element as HTMLButtonElement;
    chatBtn.focus();
    expect(document.activeElement).toBe(chatBtn);
  });

  it("样式全部走令牌：真实定位、边框/底色/圆角/字号、选中态、hover、focus", () => {
    const source = readFileSync(resolve(process.cwd(), "src/components/interactive/IntentBatchTray.vue"), "utf8");
    expect(source).toMatch(/\.overlay-switch\s*\{[^}]*position:\s*fixed/);
    expect(source).toMatch(/\.overlay-switch\s*\{[^}]*border:\s*1px solid var\(--border-strong\)/);
    expect(source).toMatch(/\.overlay-switch\s*\{[^}]*background:\s*var\(--bg-elevated\)/);
    expect(source).toMatch(/\.overlay-switch\s*\{[^}]*border-radius:\s*var\(--r-pill\)/);
    expect(source).toMatch(/\.switch-btn\s*\{[^}]*font-size:\s*var\(--fs-xs\)/);
    expect(source).toMatch(/\.switch-btn\s*\{[^}]*background:\s*var\(--bg-surface\)/);
    expect(source).toMatch(/\.switch-btn:hover\s*\{[^}]*border-color:\s*var\(--accent\)/);
    expect(source).toMatch(/\.switch-btn\[aria-pressed="true"\]\s*\{[^}]*background:\s*var\(--accent\)/);
    expect(source).toMatch(/\.switch-btn:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--focus-ring\)/);
    // 切换条这一段不许出现硬编码色值（颜色一律走令牌）
    const block = source.slice(source.indexOf(".overlay-switch {"), source.indexOf("@media (max-width: 900px)"));
    expect(/#[0-9a-fA-F]{3,8}\b|rgba?\(/.test(block)).toBe(false);
  });
});
