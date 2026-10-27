import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { ref, shallowRef, nextTick, watch } from "vue";
import PlanetView from "../PlanetView.vue";
import ManagementArea from "../../components/planet/ManagementArea.vue";
import { useSessionStore } from "../../stores/session";
import { useUiStore } from "../../stores/ui";
import { resetPlanetSession } from "../../composables/planetSession";
import { planetContinuum } from "../../composables/planetContinuum";
import type {
  EntityCard,
  KnowledgeItem,
  PlanetTopicSummary,
  TopicDetail,
  TopicFingerprint,
} from "../../services/api";
import type { PlanetBrowseSession } from "../../planet/browseSession";

const mocks = vi.hoisted(() => ({
  goMock: vi.fn(() => Promise.resolve()),
  focusTopicMock: vi.fn(),
  pullBackMock: vi.fn(() => Promise.resolve()),
  /** 展开时记下「打开前的球体朝向」；收起时与体量收缩同时转回它（返回实际夹角，0 = 不需要转） */
  markReturnOrientationMock: vi.fn(),
  rotateBackMock: vi.fn((_maxMs: number) => 0),
  primeCameraMock: vi.fn(),
  setPausedMock: vi.fn(),
  setLowPowerMock: vi.fn(),
  setIdleSpinMock: vi.fn(),
  setThemeMock: vi.fn(),
  initMock: vi.fn(),
  attachBrowseMock: vi.fn(),
  setTopicsMock: vi.fn(),
  refreshWindowMock: vi.fn(),
  handleClickMock: vi.fn(),
  setRevealMock: vi.fn(),
  /** 管理展开时由 C 的 transition 调用的旋转入口（A 只按契约透传，不实现旋转） */
  setManageYawMock: vi.fn(),
  /** 球体屏幕几何：默认 null = 拿不到真实尺寸（走降级路径） */
  sphereRectMock: vi.fn<() => { cx: number; cy: number; radius: number } | null>(() => null),
  apiMock: {
    listTopics: vi.fn<() => Promise<{ topics: TopicFingerprint[] }>>(async () => ({ topics: [] })),
    planetOverview: vi.fn(async () => ({ topics: [] as PlanetTopicSummary[], total: 0, visible_capacity: 12 })),
    planetBrowse: vi.fn(async (_body?: { current_topic_id?: string | null }) => browsePage([])),
    getTopicDetail: vi.fn<() => Promise<TopicDetail>>(async () => ({}) as TopicDetail),
    // 与真实后端契约一致：返回权威 fragment_title / historic（historic=false 表示当前开放片段）
    setAnchor: vi.fn(async (topicId: string, fragmentId: string | null) => ({
      ok: true,
      topic_id: topicId,
      fragment_id: fragmentId,
      fragment_title: fragmentId === "f13" ? "Anchor 生命周期" : null,
      historic: Boolean(fragmentId) && fragmentId !== "f20",
    })),
    continueFromHistory: vi.fn(async (topicId: string, fragmentId: string) => ({
      ok: true,
      topic_id: topicId,
      fragment_id: "frag_new",
      fragment_title: "Anchor 生命周期",
      historic: true,
      created_fragment_id: "frag_new",
      source_fragment_id: fragmentId,
    })),
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
    listMemoryPending: vi.fn(async () => ({ items: [], total: 0, truncated: false, maintenance_available: true })),
    endTopic: vi.fn(async (topicId: string) => ({ ok: true, topic_id: topicId })),
    resumeTopic: vi.fn(async (topicId: string) => ({ ok: true, topic_id: topicId })),
  },
}));

let currentFake: ReturnType<typeof createFakePlanet> | null = null;
/** 最近一次 attachBrowse 传入的浏览会话（测试用它断言展示窗口） */
let attachedSession: PlanetBrowseSession | null = null;
function createFakePlanet() {
  const selectedTopicId = ref<string | null>(null);
  return {
    webglOK: ref(true),
    fps: ref(60),
    cameraState: ref<"overview" | "planet" | "focus">("overview"),
    selectedTopicId,
    markers: shallowRef([]),
    init: mocks.initMock,
    attachBrowse: (session: PlanetBrowseSession, options?: { onWindowChange?: () => void }) => {
      attachedSession = session;
      mocks.attachBrowseMock(session, options);
      options?.onWindowChange?.();
    },
    windowTopicIds: () => attachedSession?.windowSlots().map((s) => s?.topic_id ?? null) ?? [],
    refreshWindow: mocks.refreshWindowMock,
    go: mocks.goMock,
    // 模拟真实 focusTopic：同步设置 selectedTopicId（触发详情 watcher）
    focusTopic: (topicId: string, topics: unknown) => {
      selectedTopicId.value = topicId;
      mocks.focusTopicMock(topicId, topics);
    },
    handleClick: mocks.handleClickMock,
    primeCamera: mocks.primeCameraMock,
    pullBack: mocks.pullBackMock,
    markReturnOrientation: mocks.markReturnOrientationMock,
    rotateBack: mocks.rotateBackMock,
    setPaused: mocks.setPausedMock,
    setLowPower: mocks.setLowPowerMock,
    setIdleSpin: mocks.setIdleSpinMock,
    /** 管理展开时 C 的 transition 会调它（场景内的独立 yaw 组） */
    setManageYaw: mocks.setManageYawMock,
    setRingScreenWidth: vi.fn(),
    setReveal: mocks.setRevealMock,
    sphereScreenRect: mocks.sphereRectMock,
    hoverTopicId: ref<string | null>(null),
    hoverLabel: ref<{ x: number; y: number; title: string } | null>(null),
    selectedLabel: ref<{ x: number; y: number; title: string } | null>(null),
    cancelAnimation: vi.fn(),
    resize: vi.fn(),
    setTheme: mocks.setThemeMock,
    setTopics: mocks.setTopicsMock,
  };
}

vi.mock("../../composables/usePlanetScene", () => ({
  usePlanetScene: () => (currentFake = createFakePlanet()),
}));

vi.mock("../../services/api", () => ({ api: mocks.apiMock }));

const TOPICS: TopicFingerprint[] = [{ topic_id: "t1", title: "话题 A", keywords: [], fragment_count: 3, last_activity: null, summary_preview: null }];
const SUMMARIES: PlanetTopicSummary[] = [
  { topic_id: "t1", title: "话题 A", fragment_count: 3, last_activity: null, summary_preview: null, visual_seed: 1 },
];
const DETAIL: TopicDetail = { topic_id: "t1", name: "话题 A", fragments: [], entities: [], knowledge: [] };

const TOPICS2: TopicFingerprint[] = [
  ...TOPICS,
  { topic_id: "t2", title: "话题 B", keywords: [], fragment_count: 1, last_activity: null, summary_preview: null },
];
const SUMMARIES2: PlanetTopicSummary[] = [
  ...SUMMARIES,
  { topic_id: "t2", title: "话题 B", fragment_count: 1, last_activity: null, summary_preview: null, visual_seed: 2 },
];

/** 后端浏览接口的响应形状（游标可前进可后退） */
function browsePage(items: PlanetTopicSummary[], cursor = "7.0.0") {
  return {
    seed: 7,
    pass_index: 0,
    cursor,
    prev_cursor: cursor,
    next_cursor: "7.0.12",
    has_more: false,
    pass_changed: false,
    total: items.length,
    visible_capacity: 12,
    items,
  };
}

/** 后端契约：第一批永远把「当前所在话题」放在首位 */
function browseFor(currentTopicId: string | null | undefined, items: PlanetTopicSummary[]) {
  if (!currentTopicId) return browsePage(items);
  const anchor = items.find((item) => item.topic_id === currentTopicId);
  if (!anchor) return browsePage(items);
  return browsePage([anchor, ...items.filter((item) => item.topic_id !== currentTopicId)]);
}

function mountView(pinia: Pinia, attach = false) {
  return mount(PlanetView, { attachTo: attach ? document.body : undefined, global: { plugins: [pinia] } });
}

function newPinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

beforeEach(() => {
  vi.clearAllMocks();
  attachedSession = null;
  resetPlanetSession();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-motion");
  mocks.handleClickMock.mockReturnValue(false);
  mocks.goMock.mockImplementation(() => Promise.resolve());
  mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS });
  mocks.apiMock.planetOverview.mockResolvedValue({ topics: SUMMARIES, total: SUMMARIES.length, visible_capacity: 12 });
  mocks.apiMock.planetBrowse.mockImplementation(async (body?: { current_topic_id?: string | null }) =>
    browseFor(body?.current_topic_id, SUMMARIES),
  );
  mocks.apiMock.getTopicDetail.mockResolvedValue(DETAIL);
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [] });
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [] });
  mocks.apiMock.listMemoryPending.mockResolvedValue({ items: [], total: 0, truncated: false, maintenance_available: true });
  mocks.apiMock.endTopic.mockResolvedValue({ ok: true, topic_id: "t1" });
  mocks.apiMock.resumeTopic.mockResolvedValue({ ok: true, topic_id: "t2" });
});

/**
 * 切换管理区（展开/收起）。
 *
 * 为什么在「减少动态效果」下点：C 的 transition 在 reduced 下 setExpanded 一帧到位（契约要求），
 * 而 jsdom 里 rAF 的时间戳与 performance.now 不同源，逐帧进度在单测里不可靠
 * —— 真实动画路径由 docs/mp-verify/a-page 的浏览器截图 + data-mp 读数验证。
 * 这里只切换类名/进度这一件事，顺便把「减少动态效果直接到位」也测掉。
 */
async function toggleManagement(w: ReturnType<typeof mountView>) {
  document.documentElement.setAttribute("data-motion", "reduced");
  await w.find("[data-mp-manage-toggle]").trigger("click");
  await nextTick();
  document.documentElement.removeAttribute("data-motion");
  await nextTick();
}

/** 在「减少动态效果」下执行一次动作（用于画布点击 / 搜索入口这类会展开管理区的路径） */
async function withReducedMotion<T>(fn: () => Promise<T>): Promise<T> {
  document.documentElement.setAttribute("data-motion", "reduced");
  try {
    return await fn();
  } finally {
    document.documentElement.removeAttribute("data-motion");
  }
}

describe("PlanetView 相机联动", () => {
  it("无锚点挂载：初始化场景并推进到 planet（悬浮球点击 → 全屏近景）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    expect(mocks.initMock).toHaveBeenCalled();
    expect(mocks.attachBrowseMock).toHaveBeenCalled();
    expect(attachedSession?.windowSlots().filter(Boolean).map((s) => s!.topic_id)).toEqual(["t1"]);
    expect(mocks.goMock).toHaveBeenLastCalledWith("planet", null, 420);
    w.unmount();
  });

  it("已有锚点：挂载后聚焦锚点话题（详情改为按需在记忆标签页打开）", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", []);
    // 打开页面本身不再顺手拉详情：概览由记忆标签页在需要时加载
    expect(mocks.apiMock.getTopicDetail).not.toHaveBeenCalled();
    w.unmount();
  });

  it("点击收起：先收势（不后撤相机）→ 再整体淡出 → 最后 emit close", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".close-btn").trigger("click");
    await flushPromises();
    expect(mocks.pullBackMock).not.toHaveBeenCalled();
    expect(w.classes()).toContain("settling");
    expect(w.emitted("close")).toBeFalsy();
    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("展开时记下球体朝向，收起时与体量收缩同时转回去（两个方向都要有旋转）", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises();
    expect(mocks.markReturnOrientationMock).toHaveBeenCalled();

    mocks.rotateBackMock.mockClear();
    await w.find(".close-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(320);
    expect(mocks.rotateBackMock).toHaveBeenCalledTimes(1);
    expect(mocks.rotateBackMock.mock.calls[0][0]).toBeGreaterThan(100);

    await vi.advanceTimersByTimeAsync(620);
    vi.useRealTimers();
    w.unmount();
  });

  it("收势进行中：画布单击/双击不打断，收势结束后才 emit close", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises();

    await w.find(".close-btn").trigger("click");
    expect(w.classes()).toContain("settling");

    await w.find("canvas").trigger("click", { clientX: 5, clientY: 5 });
    await w.find("canvas").trigger("dblclick");
    expect(mocks.handleClickMock).not.toHaveBeenCalled();
    expect(mocks.goMock).toHaveBeenCalledTimes(1); // 只有挂载时那一次 go("planet")

    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("点击画布：委托给场景 handleClick（话题点聚焦路径）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    await w.find("canvas").trigger("click", { clientX: 10, clientY: 20 });
    expect(mocks.handleClickMock).toHaveBeenCalledWith(10, 20);
    w.unmount();
  });

  it("场景选中话题变化：只记录浏览记忆与锁定槽位，不按选中态反复拉详情", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.selectedTopicId.value = "t1";
    await nextTick();
    expect(mocks.apiMock.getTopicDetail).not.toHaveBeenCalled();
    const { planetSession } = await import("../../composables/planetSession");
    expect(planetSession.browsedTopicId).toBe("t1");
    w.unmount();
  });

  it("挂载时 API pending → 先收起 → API resolve 后不再推进/聚焦，且 close 仍 emit", async () => {
    vi.useFakeTimers();
    let resolveList!: () => void;
    let resolvePos!: () => void;
    mocks.apiMock.listTopics.mockImplementation(() => new Promise((r) => { resolveList = () => r({ topics: TOPICS }); }));
    mocks.apiMock.planetBrowse.mockImplementation(() => new Promise((r) => { resolvePos = () => r(browsePage(SUMMARIES)); }));
    const w = mountView(newPinia());
    await w.find(".close-btn").trigger("click");
    expect(w.classes()).toContain("settling");
    resolveList();
    await flushPromises();
    expect(mocks.goMock).not.toHaveBeenCalled();
    expect(mocks.focusTopicMock).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });
});

describe("PlanetView 主题同步", () => {
  it("light 主题挂载：场景初始化后应用 setTheme('light')", async () => {
    document.documentElement.setAttribute("data-theme", "light");
    const w = mountView(newPinia());
    await flushPromises();
    expect(mocks.setThemeMock).toHaveBeenCalledWith("light");
    w.unmount();
  });

  it("dark 主题挂载：应用 setTheme('dark')", async () => {
    document.documentElement.setAttribute("data-theme", "dark");
    const w = mountView(newPinia());
    await flushPromises();
    expect(mocks.setThemeMock).toHaveBeenCalledWith("dark");
    w.unmount();
  });

  it("data-theme 变化：MutationObserver 同步再次调用 setTheme", async () => {
    document.documentElement.setAttribute("data-theme", "light");
    const w = mountView(newPinia());
    await flushPromises();
    expect(mocks.setThemeMock).toHaveBeenLastCalledWith("light");

    document.documentElement.setAttribute("data-theme", "dark");
    await flushPromises();
    expect(mocks.setThemeMock).toHaveBeenLastCalledWith("dark");
    expect(mocks.setThemeMock).toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it("卸载后断开 observer：data-theme 变化不再调用 setTheme", async () => {
    document.documentElement.setAttribute("data-theme", "light");
    const w = mountView(newPinia());
    await flushPromises();
    const callsAfterMount = mocks.setThemeMock.mock.calls.length;
    w.unmount();
    document.documentElement.setAttribute("data-theme", "dark");
    await flushPromises();
    expect(mocks.setThemeMock).toHaveBeenCalledTimes(callsAfterMount);
  });
});

describe("记忆星球页布局（本轮改版：内容区域 + 管理区）", () => {
  it("内容区域独立一层：星球层、管理区、页面工具行都在它里面（全局栏之下）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    const content = w.find(".planet-content");
    expect(content.exists()).toBe(true);
    expect(content.find(".planet-stage canvas").exists()).toBe(true);
    expect(content.find("aside.panel").exists()).toBe(true);
    expect(content.find(".page-tools").exists()).toBe(true);
    // 管理区只能在全局栏下方：内容层自己让出标题条（只读断言源码在 mp-a-layout 里）
    expect(w.find(".planet-view").classes()).not.toContain("managing");
    w.unmount();
  });

  it("页面工具行：默认有搜索与管理两个入口，管理区收起时 aria-expanded=false", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    expect(w.find("[data-mp-search-open]").exists()).toBe(true);
    const toggle = w.find("[data-mp-manage-toggle]");
    expect(toggle.exists()).toBe(true);
    expect(toggle.attributes("aria-expanded")).toBe("false");
    expect(toggle.text()).toContain("管理");
    w.unmount();
  });

  it("点管理：管理区展开（--mp / data-mp / managing），管理内容挂载在面板里", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    await toggleManagement(w);
    expect(w.find("[data-mp-manage-toggle]").attributes("aria-expanded")).toBe("true");
    expect(w.classes()).toContain("managing");
    expect(w.element.getAttribute("data-mp")).toBe("1.000");
    expect(w.findComponent(ManagementArea).exists()).toBe(true);
    expect(w.find("[data-view-panel=\"memory\"]").exists()).toBe(true);
    w.unmount();
  });

  it("管理区收起再展开：状态不重置（列表搜索词、已挂载的面板都保留）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    await toggleManagement(w);
    await flushPromises();
    await w.find(".mt-list-head input").setValue("保留我");
    const listCalls = mocks.apiMock.listTopics.mock.calls.length;

    await toggleManagement(w);
    expect(w.find("[data-mp-manage-toggle]").attributes("aria-expanded")).toBe("false");
    expect(w.classes()).not.toContain("managing");
    expect(w.element.getAttribute("data-mp")).toBe("0.000");

    await toggleManagement(w);
    await flushPromises();
    expect((w.find(".mt-list-head input").element as HTMLInputElement).value).toBe("保留我");
    expect(mocks.apiMock.listTopics.mock.calls.length).toBe(listCalls); // 没有重置数据
    w.unmount();
  });

  it("画布命中话题点：展开管理区落在记忆页，且不做任何重对焦（旋转由 C 负责）", async () => {
    mocks.handleClickMock.mockReturnValue(true);
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.selectedTopicId.value = "t1";
    const callsBefore = mocks.focusTopicMock.mock.calls.length;
    await withReducedMotion(async () => {
      await w.find("canvas").trigger("click", { clientX: 10, clientY: 20 });
      await flushPromises();
    });
    expect(w.classes()).toContain("managing");
    expect(w.attributes("data-view")).toBe("tabs");
    expect(w.attributes("data-tab")).toBe("memory");
    expect(mocks.focusTopicMock).toHaveBeenCalledTimes(callsBefore);
    // 记忆页同步打开被选中的话题概览
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    w.unmount();
  });

  it("画布未命中话题点：管理区保持收起", async () => {
    mocks.handleClickMock.mockReturnValue(false);
    const w = mountView(newPinia());
    await flushPromises();
    await w.find("canvas").trigger("click", { clientX: 10, clientY: 20 });
    await nextTick();
    expect(w.classes()).not.toContain("managing");
    expect(w.findComponent(ManagementArea).exists()).toBe(false);
    w.unmount();
  });

  it("Esc 分层：管理区打开时先收管理区；再按收起星球", async () => {
    const w = mountView(newPinia(), true);
    await flushPromises();
    await toggleManagement(w);
    expect(w.classes()).toContain("managing");

    // 第一层：收管理区（减少动态效果下一次到位；逻辑目标态与类名同时变）
    await withReducedMotion(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await nextTick();
    });
    expect(w.find("[data-mp-manage-toggle]").attributes("aria-expanded")).toBe("false");
    expect(w.classes()).not.toContain("managing");
    expect(w.classes()).not.toContain("settling");

    // 第二层：收起星球（走收势 + 淡出）
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.classes()).toContain("settling");
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(w.emitted("close")).toBeTruthy();
    w.unmount();
  });

  it("Esc：焦点在管理区输入框内不收起任何一层", async () => {
    const w = mountView(newPinia(), true);
    await flushPromises();
    await toggleManagement(w);
    const input = w.find(".mt-list-head input").element as HTMLInputElement;
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.classes()).toContain("managing");
    expect(w.classes()).not.toContain("settling");
    w.unmount();
  });

  it("管理按钮带真实未处理数量角标（来自 planetPending store）", async () => {
    mocks.apiMock.listMemoryPending.mockResolvedValue({ items: [], total: 2, truncated: false, maintenance_available: true });
    const w = mountView(newPinia());
    await flushPromises();
    const toggle = w.find("[data-mp-manage-toggle]");
    await nextTick();
    expect(toggle.find(".pending-badge").text()).toBe("2");
    expect(toggle.attributes("aria-label")).toContain("2 项待处理");
    w.unmount();
  });

  it("WebGL 不可用时管理入口仍可用：点开管理区仍能列出话题", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.webglOK.value = false;
    await nextTick();
    await toggleManagement(w);
    expect(w.find(".webgl-fallback").exists()).toBe(true);
    expect(w.findAll("[data-topic-id]").length).toBe(1);
    w.unmount();
  });

  it("管理区「继续讨论」：走既有锚点切换（setAnchor）后收势并关闭", async () => {
    vi.useFakeTimers();
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();
    await toggleManagement(w);

    w.findComponent(ManagementArea).vm.$emit("continue", { topicId: "t2", fragmentId: null });
    await flushPromises();
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledWith("t2", null);
    expect(session.currentTopicId).toBe("t2");
    expect(w.classes()).toContain("settling");
    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("页面工具行「搜索」：展开管理区并直接落在搜索视图", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    await withReducedMotion(async () => {
      await w.find("[data-mp-search-open]").trigger("click");
      await flushPromises();
    });
    expect(w.classes()).toContain("managing");
    expect(w.attributes("data-view")).toBe("search");
    expect(w.find("[data-mp-search]").exists()).toBe(true);
    w.unmount();
  });

  it("WebGL 降级说明仍可读；管理内容不遮挡全局栏（面板在内容层内部）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.webglOK.value = false;
    await nextTick();
    expect(w.find(".webgl-fallback").text()).toContain("无法启用 3D 星球");
    // 管理区不在根层直接挂：它在 .planet-content 内部（因此永远在全局栏下方）
    expect(w.find(".planet-content aside.panel").exists()).toBe(true);
    w.unmount();
  });
});

describe("PlanetView 图形诊断可见性（任务04 A1/A7）", () => {
  it("正常模式不显示 WebGL / FPS HUD", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    expect(w.find(".hud").exists()).toBe(false);
    w.unmount();
  });

  it("开发者模式才显示 HUD", async () => {
    const pinia = newPinia();
    useUiStore().setDeveloperMode(true);
    const w = mountView(pinia);
    await flushPromises();
    expect(w.find(".hud").text()).toContain("WebGL · 60 fps");
    useUiStore().setDeveloperMode(false);
    w.unmount();
  });
});

describe("任务05 收尾：加载提示与话题点标签", () => {
  it("首次数据加载中显示加载提示，加载完成后消失（不以空球冒充完整内容）", async () => {
    let resolveOverview!: () => void;
    let resolvePos!: () => void;
    mocks.apiMock.planetOverview.mockImplementation(
      () => new Promise((r) => { resolveOverview = () => r({ topics: SUMMARIES, total: 1, visible_capacity: 12 }); }),
    );
    mocks.apiMock.planetBrowse.mockImplementation(() => new Promise((r) => { resolvePos = () => r(browsePage(SUMMARIES)); }));
    const w = mountView(newPinia());
    await nextTick();
    const loading = w.find(".planet-loading");
    expect(loading.exists()).toBe(true);
    expect(loading.text()).toContain("正在加载");

    resolveOverview();
    await flushPromises();
    resolvePos();
    await flushPromises();
    expect(w.find(".planet-loading").exists()).toBe(false);
    w.unmount();
  });

  it("首次数据加载失败：显示可见失败说明与重试入口（不拿空球冒充完整内容）", async () => {
    mocks.apiMock.planetOverview.mockRejectedValueOnce(new Error("boom"));
    const w = mountView(newPinia());
    await flushPromises();
    expect(w.find(".load-error").exists()).toBe(true);
    expect(w.find(".load-error").text()).toContain("星球数据加载失败");
    w.unmount();
  });

  it("悬停话题点：显示简短名称，移开后不再常驻", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    expect(w.find(".topic-hint").exists()).toBe(false);

    currentFake!.hoverTopicId.value = "t1";
    currentFake!.hoverLabel.value = { x: 120, y: 80, title: "话题 A" };
    await nextTick();
    const hint = w.find(".topic-hint");
    expect(hint.text()).toBe("话题 A");
    expect(hint.attributes("style")).toContain("left: 120px");

    currentFake!.hoverTopicId.value = null;
    currentFake!.hoverLabel.value = null;
    await nextTick();
    expect(w.find(".topic-hint").exists()).toBe(false);
    w.unmount();
  });

  it("选中话题的名称常驻在点旁边，悬停其它点时让位", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.selectedLabel.value = { x: 200, y: 150, title: "话题 A" };
    await nextTick();
    const label = w.find(".topic-hint.selected");
    expect(label.exists()).toBe(true);
    expect(label.text()).toBe("话题 A");

    currentFake!.hoverTopicId.value = "t2";
    currentFake!.hoverLabel.value = { x: 300, y: 200, title: "话题 B" };
    await nextTick();
    expect(w.find(".topic-hint.selected").exists()).toBe(false);
    expect(w.find(".topic-hint").text()).toBe("话题 B");
    w.unmount();
  });
});

describe("PlanetView 实例复用（性能：不再每次重建 WebGL 场景）", () => {
  it("open 从 false 变回 true 时复用同一场景：不再 init、会重新拉数据并恢复全帧率", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    expect(mocks.initMock).toHaveBeenCalledTimes(1);
    const listCallsAfterMount = mocks.apiMock.listTopics.mock.calls.length;

    await w.setProps({ open: false });
    await flushPromises();
    expect(w.find(".planet-view").classes()).toContain("ball");
    expect(mocks.setLowPowerMock).toHaveBeenLastCalledWith(true);
    expect(mocks.setIdleSpinMock).toHaveBeenLastCalledWith(true);

    await w.setProps({ open: true });
    await flushPromises();
    expect(mocks.initMock).toHaveBeenCalledTimes(1);
    expect(mocks.setLowPowerMock).toHaveBeenLastCalledWith(false);
    expect(mocks.apiMock.listTopics.mock.calls.length).toBeGreaterThan(listCallsAfterMount);
    w.unmount();
  });

  it("关闭过程中又打开一次：放弃这次收尾（不 emit、不把新层变成收起中）", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    w.setProps({ seq: 7 });
    await flushPromises();

    await w.find(".close-btn").trigger("click");
    expect(w.classes()).toContain("settling");
    await w.setProps({ seq: 8 });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(620);

    expect(w.emitted("close")).toBeUndefined();
    expect(w.classes()).not.toContain("closing");
    expect(w.classes()).not.toContain("settling");
    vi.useRealTimers();
    w.unmount();
  });
});

/**
 * 第四阶段：Planet 连续体。
 *
 * 入口小球与全屏 Planet 必须是**同一个对象的不同尺度状态**，所以这里守的是编排本身：
 * 进入分三段推进（激活 → 展开 → 接管），退出严格反向（收势 → 收缩 → 交还），
 * 而且阶段状态与 PlanetDock 共享（`planetContinuum`），不是各自计时。
 */
describe("PlanetView 连续体编排（第四阶段）", () => {
  it("进入：激活 → 展开 → 场景接管；退出：收势 → 收缩 → 交还", async () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const stop = watch(() => planetContinuum.phase, (p) => seen.push(p), { immediate: true });
    const w = mountView(newPinia());
    await flushPromises();

    // A 入口激活：小球被激活、铺底淡入，此时还不接受交互
    expect(planetContinuum.phase).toBe("activating");
    expect(mocks.setRevealMock).toHaveBeenCalledWith(0);

    await vi.advanceTimersByTimeAsync(260);
    await flushPromises();
    expect(planetContinuum.phase).toBe("expanding");

    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(planetContinuum.phase).toBe("ready");
    expect(seen).toEqual(expect.arrayContaining(["activating", "expanding", "ready"]));
    expect(w.classes()).toContain("layers");

    // 退出：先降密度/收浮层，再收缩体量，最后交还小球
    await w.find(".close-btn").trigger("click");
    await flushPromises();
    expect(planetContinuum.phase).toBe("collapsing");
    await vi.advanceTimersByTimeAsync(260);
    await flushPromises();
    expect(planetContinuum.phase).toBe("returning");
    await vi.advanceTimersByTimeAsync(400);
    await flushPromises();
    expect(planetContinuum.phase).toBe("idle");
    expect(w.emitted("close")).toBeTruthy();

    stop();
    vi.useRealTimers();
    w.unmount();
  });

  it("星球开着时卸载（=离开对话页去设置）：连续体收干净，入口球不会被 handed 永久锁住", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises();
    await vi.advanceTimersByTimeAsync(1800);
    await flushPromises();
    expect(planetContinuum.phase).toBe("ready");
    planetContinuum.ballLive = true;

    w.unmount();
    expect(planetContinuum.phase).toBe("idle");
    expect(planetContinuum.ballLive).toBe(false);
    vi.useRealTimers();
  });
});
