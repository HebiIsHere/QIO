/**
 * #38 / 本组契约 §4.3 · 星球覆盖层的模态对话框语义与焦点循环（PlanetView 侧）。
 *
 * conversation 负责给背景加 inert、关闭后归还焦点；这里验证 PlanetView 自己必须做到：
 * - 打开时根层 role="dialog" + aria-modal="true" + data-planet-overlay；
 * - 初始焦点移入星球内第一个可用控件；
 * - Tab / Shift+Tab 在星球内首尾循环；
 * - 球态（关闭常驻）不是对话框，且不抢页面焦点。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick, ref, shallowRef } from "vue";
import PlanetView from "../PlanetView.vue";
import { resetPlanetSession } from "../../composables/planetSession";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listTopics: vi.fn(),
    planetOverview: vi.fn(),
    planetBrowse: vi.fn(),
    getTopicDetail: vi.fn(),
    setAnchor: vi.fn(),
    continueFromHistory: vi.fn(),
    cancelContinuation: vi.fn(),
    getRuntimeState: vi.fn(),
    getRecoveryRecords: vi.fn(async () => ({ records: [], total: 0, shown: 0, truncated: false })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ items: [] })),
    listKnowledge: vi.fn(async () => ({ knowledge: [] })),
    listEntities: vi.fn(async () => ({ entities: [] })),
    fragmentMessages: vi.fn(async () => ({ messages: [], total: 0 })),
    listMemoryPending: vi.fn(async () => ({ items: [], total: 0, truncated: false, maintenance_available: true })),
    endTopic: vi.fn(async () => ({ ok: true })),
    resumeTopic: vi.fn(async () => ({ ok: true })),
  },
}));

function createFakePlanet() {
  return {
    webglOK: ref(true),
    fps: ref(60),
    cameraState: ref<"overview" | "planet" | "focus">("planet"),
    selectedTopicId: ref<string | null>(null),
    markers: shallowRef([]),
    init: vi.fn(),
    attachBrowse: vi.fn(),
    windowTopicIds: () => [] as (string | null)[],
    go: vi.fn(() => Promise.resolve()),
    focusTopic: vi.fn(),
    handleClick: vi.fn(() => false),
    primeCamera: vi.fn(),
    pullBack: vi.fn(() => Promise.resolve()),
    markReturnOrientation: vi.fn(),
    rotateBack: vi.fn(() => 0),
    setPaused: vi.fn(),
    setLowPower: vi.fn(),
    setIdleSpin: vi.fn(),
    setManageYaw: vi.fn(),
    setRingScreenWidth: vi.fn(),
    setReveal: vi.fn(),
    sphereScreenRect: vi.fn(() => null),
    hoverTopicId: ref<string | null>(null),
    hoverLabel: ref<{ x: number; y: number; title: string } | null>(null),
    selectedLabel: ref<{ x: number; y: number; title: string } | null>(null),
    cancelAnimation: vi.fn(),
    resize: vi.fn(),
    setTheme: vi.fn(),
    refreshWindow: vi.fn(),
    setTopics: vi.fn(),
    debugState: vi.fn(() => ({})),
  };
}

vi.mock("../../composables/usePlanetScene", () => ({
  usePlanetScene: () => createFakePlanet(),
}));
vi.mock("../../services/api", () => ({ api: mocks.apiMock }));

const TOPICS = [
  { topic_id: "t1", title: "话题 A", keywords: [], fragment_count: 3, last_activity: null, summary_preview: null },
];
const SUMMARIES = [
  { topic_id: "t1", title: "话题 A", fragment_count: 3, last_activity: null, summary_preview: null, visual_seed: 1 },
];
const BROWSE = {
  seed: 7,
  pass_index: 0,
  cursor: "7.0.0",
  prev_cursor: "7.0.0",
  next_cursor: "7.0.12",
  has_more: false,
  pass_changed: false,
  total: 1,
  visible_capacity: 12,
  items: SUMMARIES,
};

function newPinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

function mountView(open = true) {
  return mount(PlanetView, {
    attachTo: document.body,
    props: { open },
    global: { plugins: [newPinia()] },
  });
}

/** 与组件内同一口径的「可用焦点目标」。 */
function planetFocusables(root: Element): HTMLElement[] {
  const selector =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
  return Array.from(root.querySelectorAll<HTMLElement>(selector)).filter((el) => {
    if (el.closest("[inert]")) return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  });
}

function pressTab(shiftKey = false): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true });
  window.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  vi.clearAllMocks();
  resetPlanetSession();
  document.body.innerHTML = "";
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-motion");
  mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS });
  mocks.apiMock.planetOverview.mockResolvedValue({ topics: SUMMARIES, total: 1, visible_capacity: 12 });
  mocks.apiMock.planetBrowse.mockResolvedValue(BROWSE);
  mocks.apiMock.getTopicDetail.mockResolvedValue({ topic_id: "t1", name: "话题 A", fragments: [], entities: [], knowledge: [] });
});

describe("星球打开时是模态对话框（#38 / §4.3）", () => {
  it("根层暴露 role=dialog、aria-modal=true、data-planet-overlay 与可用标签", async () => {
    const w = mountView(true);
    await flushPromises();
    const root = w.element as HTMLElement;
    expect(root.getAttribute("role")).toBe("dialog");
    expect(root.getAttribute("aria-modal")).toBe("true");
    expect(root.hasAttribute("data-planet-overlay")).toBe(true);
    expect(root.getAttribute("aria-label")).toBe("话题星球");
    expect(root.getAttribute("aria-hidden")).toBeNull();
    w.unmount();
  });

  it("球态（关闭常驻）不是对话框：不暴露 dialog/aria-modal，并标记 aria-hidden", async () => {
    const w = mountView(false);
    await flushPromises();
    const root = w.element as HTMLElement;
    expect(root.getAttribute("role")).toBeNull();
    expect(root.getAttribute("aria-modal")).toBeNull();
    expect(root.hasAttribute("data-planet-overlay")).toBe(false);
    expect(root.getAttribute("aria-hidden")).toBe("true");
    w.unmount();
  });

  it("打开后初始焦点进入星球内第一个可用控件", async () => {
    const w = mountView(true);
    await flushPromises();
    await nextTick();
    const root = w.element as HTMLElement;
    const items = planetFocusables(root);
    expect(items.length).toBeGreaterThan(0);
    expect(root.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(items[0]);
    w.unmount();
  });

  it("Tab 从最后一个可用控件回到第一个；Shift+Tab 从第一个回最后一个", async () => {
    const w = mountView(true);
    await flushPromises();
    await nextTick();
    const root = w.element as HTMLElement;
    const items = planetFocusables(root);

    items[items.length - 1].focus();
    const forward = pressTab(false);
    expect(forward.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(items[0]);

    items[0].focus();
    const backward = pressTab(true);
    expect(backward.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(items[items.length - 1]);
    w.unmount();
  });

  it("关闭（球态）后不抢页面焦点：焦点留原来的控件", async () => {
    const outside = document.createElement("button");
    outside.textContent = "页面上的其它控件";
    document.body.appendChild(outside);
    outside.focus();
    const w = mountView(false);
    await flushPromises();
    await nextTick();
    expect(document.activeElement).toBe(outside);
    // 关闭态下 Tab 不做星球内循环（不拦截事件）
    const tab = pressTab(false);
    expect(tab.defaultPrevented).toBe(false);
    w.unmount();
  });
});
