import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { ref, shallowRef, nextTick, watch } from "vue";
import PlanetView from "../PlanetView.vue";
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
  /** 球体屏幕几何：默认 null = 拿不到真实尺寸（走降级路径） */
  sphereRectMock: vi.fn<() => { cx: number; cy: number; radius: number } | null>(() => null),
  apiMock: {
    listTopics: vi.fn<() => Promise<{ topics: TopicFingerprint[] }>>(async () => ({ topics: [] })),
    planetOverview: vi.fn(async () => ({ topics: [] as PlanetTopicSummary[], total: 0, visible_capacity: 12 })),
    planetBrowse: vi.fn(async (_body?: { current_topic_id?: string | null }) => browsePage([])),
    getTopicDetail: vi.fn<() => Promise<TopicDetail>>(async () => ({}) as TopicDetail),
    // 与真实后端契约一致：返回权威 fragment_title / historic（historic=false 表示当前开放片段）
    setAnchor: vi.fn(async (_topicId: string, fragmentId: string | null) => ({
      ok: true,
      topic_id: "t1",
      fragment_id: fragmentId,
      fragment_title: fragmentId === "f13" ? "Anchor 生命周期" : null,
      historic: Boolean(fragmentId) && fragmentId !== "f20",
    })),
    // 与真实后端契约一致：来源是开放片段时不开新片段（historic=false）
    continueFromHistory: vi.fn(async (topicId: string, fragmentId: string) =>
      fragmentId === "f20"
        ? {
            ok: true,
            topic_id: topicId,
            fragment_id: "f20",
            fragment_title: null,
            historic: false,
            created_fragment_id: null,
            source_fragment_id: null,
          }
        : {
            ok: true,
            topic_id: topicId,
            fragment_id: "frag_new",
            fragment_title: "Anchor 生命周期",
            historic: true,
            created_fragment_id: "frag_new",
            source_fragment_id: fragmentId,
          },
    ),
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
    /** 第三层接口：只有真正展开某段历史时才取原文（按页） */
    fragmentMessages: vi.fn(async (_id: string, offset = 0, limit = 20) => ({
      fragment_id: _id,
      topic_id: "t1",
      total: 0,
      offset,
      limit,
      messages: [] as { id: string; role: string; content: string; content_type: string; created_at: string }[],
    })),
    reviseKnowledge: vi.fn(async (_id: string, _content: string) => ({ ok: true, knowledge_id: _id })),
    revokeKnowledge: vi.fn(async (_id: string) => ({ ok: true })),
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
    /** 收起时转回「打开前的朝向」：与体量收缩同时开始（用户要求：两个方向都要有旋转） */
    markReturnOrientation: mocks.markReturnOrientationMock,
    rotateBack: mocks.rotateBackMock,
    setPaused: mocks.setPausedMock,
    /** 入口小球（球态）用的两个开关：低帧率 + 空闲自转 */
    setLowPower: mocks.setLowPowerMock,
    setIdleSpin: mocks.setIdleSpinMock,
    /** 环的目标屏幕宽度（小球上环要看得见）；补偿倍数由渲染器按当时几何现算 */
    setRingScreenWidth: vi.fn(),
    /** 第四阶段 Planet 连续体：信息密度（0 = 抽象态，1 = 完整 Planet） */
    setReveal: mocks.setRevealMock,
    /**
     * 球体在屏幕上的几何。测试环境里没有真实 WebGL 球体，返回 null 表示
     * 「拿不到真实尺寸」——此时转场走降级路径（不做缩放对齐），
     * 这正是真实环境里 WebGL 不可用时的行为，也保证既有交互测试不受转场几何影响。
     */
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
});

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

  it("已有锚点：挂载后聚焦锚点话题并加载详情", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", []);
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    w.unmount();
  });

  it("点击收起：先收势（轻微后撤）→ 再整体淡出 → 最后 emit close", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".close-btn").trigger("click");
    await flushPromises();
    /**
     * 收势阶段不动相机。
     *
     * 这条以前断言 `pullBack(0.45, 180)` —— 那记相机后撤是旧编排的遗留物，
     * 在新编排（同一个对象收拢回入口）里它会让球体先缩小一次、星球层再缩一次，
     * 用户实测反馈「收起有两段动画」。现在只保留收势（浮层退场 / 密度回到抽象态）。
     */
    expect(mocks.pullBackMock).not.toHaveBeenCalled();
    expect(w.classes()).toContain("settling");
    expect(w.emitted("close")).toBeFalsy(); // 还没结束，不提前卸载
    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("展开时记下球体朝向，收起时与体量收缩同时转回去（两个方向都要有旋转）", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises();
    // 展开：在聚焦发生之前记下「打开前的朝向」（收起时要转回它）
    expect(mocks.markReturnOrientationMock).toHaveBeenCalled();

    mocks.rotateBackMock.mockClear();
    await w.find(".close-btn").trigger("click");
    await flushPromises();
    /**
     * 收起：体量收缩一开始就发起「转回去」，并且**封顶在收起窗口内**
     * （超出去会和球态的自转抢同一个四元数；见 usePlanetScene.rotateBack）。
     * 注意要先把收势（180ms）+ 几帧等待走完，才会进入体量收缩段。
     */
    await vi.advanceTimersByTimeAsync(320);
    expect(mocks.rotateBackMock).toHaveBeenCalledTimes(1);
    expect(mocks.rotateBackMock.mock.calls[0][0]).toBeGreaterThan(100);

    await vi.advanceTimersByTimeAsync(620);
    vi.useRealTimers();
    w.unmount();
  });

  it("收势进行中：话题列表/画布单击/双击不打断，收势结束后才 emit close", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises(); // 数据一到位就会落位聚焦（见 runEnter 的冷路径说明）

    await w.find(".close-btn").trigger("click");
    // 收势阶段（未推进定时器 = 停在收势里）
    expect(w.classes()).toContain("settling");

    // 收势窗口内：话题点击不聚焦、画布单击不命中、双击不切回 planet
    await w.find(".topic-list li").trigger("click");
    await w.find("canvas").trigger("click", { clientX: 5, clientY: 5 });
    await w.find("canvas").trigger("dblclick");
    expect(mocks.focusTopicMock).not.toHaveBeenCalled();
    expect(mocks.handleClickMock).not.toHaveBeenCalled();
    expect(mocks.goMock).toHaveBeenCalledTimes(1); // 只有挂载时那一次 go("planet")，收起不再拉回 overview

    await vi.advanceTimersByTimeAsync(620); // 收势 180ms + 收缩 420ms → 才 emit close
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("Esc：焦点在输入框内不收起；焦点在外收起（走收势+淡出）", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia(), true);
    await flushPromises();
    const searchInput = w.find(".panel-head input").element;
    searchInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.emitted("close")).toBeFalsy();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.classes()).toContain("settling"); // 收起已经开始
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

  it("场景选中话题变化 → 联动加载详情面板", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.selectedTopicId.value = "t1";
    await nextTick();
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    w.unmount();
  });

  it("「从这里开始」：保留 setAnchor 数据流，成功后走收势再关闭", async () => {
    vi.useFakeTimers();
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();
    await w.find(".start-btn").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledWith("t1", null);
    expect(session.currentTopicId).toBe("t1");
    expect(session.topicName).toBe("话题 A");
    expect(w.classes()).toContain("settling"); // 成功后才走收起
    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("列表点击话题：详情只请求一次（watcher 单一来源，慢 API 下不重复）", async () => {
    let resolveDetail!: (d: TopicDetail) => void;
    mocks.apiMock.getTopicDetail.mockImplementation(() => new Promise<TopicDetail>((r) => { resolveDetail = r; }));
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".topic-list li").trigger("click");
    await flushPromises();
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", []);
    // 显式 loadDetail 已移除：watcher 是唯一详情来源，慢 API 下也只请求一次
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    resolveDetail(DETAIL);
    await flushPromises();
    w.unmount();
  });

  it("挂载时 API pending → 先收起 → API resolve 后不再推进/聚焦，且 close 仍 emit", async () => {
    vi.useFakeTimers();
    let resolveList!: () => void;
    let resolvePos!: () => void;
    mocks.apiMock.listTopics.mockImplementation(() => new Promise((r) => { resolveList = () => r({ topics: TOPICS }); }));
    mocks.apiMock.planetBrowse.mockImplementation(() => new Promise((r) => { resolvePos = () => r(browsePage(SUMMARIES)); }));
    const w = mountView(newPinia());
    // loadData 的 API 尚未 resolve 时先关闭
    await w.find(".close-btn").trigger("click");
    expect(w.classes()).toContain("settling");
    // 随后 API resolve → loadData 继续，但不得再推进 planet / 聚焦话题
    resolveList();
    await flushPromises();
    expect(mocks.goMock).not.toHaveBeenCalled();
    expect(mocks.focusTopicMock).not.toHaveBeenCalled();
    // 收势 + 淡出完成 → close 仍正常 emit
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

describe("PlanetView 右侧话题边栏", () => {
  it("默认收起：panel 无 open class，存在展开按钮（aria-expanded=false）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    const panel = w.find(".panel");
    expect(panel.exists()).toBe(true);
    expect(panel.classes()).not.toContain("open");
    const toggle = w.find(".panel-toggle");
    expect(toggle.exists()).toBe(true);
    expect(toggle.attributes("aria-expanded")).toBe("false");
    w.unmount();
  });

  it("点击展开按钮：panel 展开（open class + aria-expanded=true）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".panel-toggle").trigger("click");
    await nextTick();
    expect(w.find(".panel").classes()).toContain("open");
    expect(w.find(".panel-toggle").attributes("aria-expanded")).toBe("true");
    w.unmount();
  });

  it("再次点击收起按钮：panel 收起", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".panel-toggle").trigger("click");
    await nextTick();
    await w.find(".panel-toggle").trigger("click");
    await nextTick();
    expect(w.find(".panel").classes()).not.toContain("open");
    w.unmount();
  });

  it("画布点击命中话题点：展开边栏并委托场景聚焦", async () => {
    mocks.handleClickMock.mockReturnValue(true);
    const w = mountView(newPinia());
    await flushPromises();
    await w.find("canvas").trigger("click", { clientX: 10, clientY: 20 });
    await nextTick();
    expect(mocks.handleClickMock).toHaveBeenCalledWith(10, 20);
    expect(w.find(".panel").classes()).toContain("open");
    w.unmount();
  });

  it("画布点击未命中话题点：边栏保持收起", async () => {
    mocks.handleClickMock.mockReturnValue(false);
    const w = mountView(newPinia());
    await flushPromises();
    await w.find("canvas").trigger("click", { clientX: 10, clientY: 20 });
    await nextTick();
    expect(w.find(".panel").classes()).not.toContain("open");
    w.unmount();
  });

  it("收起态点击话题点：展开边栏，动画结束后重对焦焦点话题", async () => {
    mocks.handleClickMock.mockReturnValue(true);
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.selectedTopicId.value = "t1";
    vi.useFakeTimers();
    await w.find("canvas").trigger("click", { clientX: 10, clientY: 20 });
    await nextTick();
    expect(w.find(".panel").classes()).toContain("open");
    const callsBefore = mocks.focusTopicMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(400);
    expect(mocks.focusTopicMock).toHaveBeenCalledTimes(callsBefore + 1);
    expect(mocks.focusTopicMock).toHaveBeenLastCalledWith("t1", []);
    vi.useRealTimers();
    w.unmount();
  });

  it("已展开时点击话题点：画布中心未变，不触发额外重对焦", async () => {
    mocks.handleClickMock.mockReturnValue(true);
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".panel-toggle").trigger("click");
    await nextTick();
    expect(w.find(".panel").classes()).toContain("open");
    currentFake!.selectedTopicId.value = "t1";
    vi.useFakeTimers();
    const callsBefore = mocks.focusTopicMock.mock.calls.length;
    await w.find("canvas").trigger("click", { clientX: 10, clientY: 20 });
    await vi.advanceTimersByTimeAsync(400);
    expect(mocks.focusTopicMock).toHaveBeenCalledTimes(callsBefore);
    vi.useRealTimers();
    w.unmount();
  });

  it("焦点话题存在时切换边栏开合：动画结束后重对焦", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.selectedTopicId.value = "t1";
    vi.useFakeTimers();
    await w.find(".panel-toggle").trigger("click");
    await nextTick();
    const callsBefore = mocks.focusTopicMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(400);
    expect(mocks.focusTopicMock).toHaveBeenCalledTimes(callsBefore + 1);
    expect(mocks.focusTopicMock).toHaveBeenLastCalledWith("t1", []);
    vi.useRealTimers();
    w.unmount();
  });
});

describe("PlanetView 详情加载失败（P0：失败必须自己可见且可重试）", () => {
  it("详情 500：没有详情也要显示失败原因与重试入口", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    mocks.apiMock.getTopicDetail.mockRejectedValueOnce(new Error("boom"));
    const w = mountView(pinia);
    await flushPromises();
    await nextTick();

    expect(w.find(".detail").exists()).toBe(false);
    const err = w.find(".detail-error");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("boom");
    expect(err.find("button").text()).toContain("重试");
    w.unmount();
  });

  it("详情加载失败：首行说人话，技术细节折叠可达（同样是普通模式规则）", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    mocks.apiMock.getTopicDetail.mockRejectedValueOnce(
      new Error('/api/graph/topics/t1 -> 500: {"detail":"boom"}'),
    );
    const w = mountView(pinia);
    await flushPromises();
    await nextTick();
    const err = w.find(".detail-error");
    const first = err.find(".err-text").text();
    expect(first).toContain("加载话题详情失败");
    expect(first).not.toContain("/api/");
    expect(err.text()).toContain("/api/graph/topics/t1");
    expect(err.find(".err-detail").exists()).toBe(true);
    w.unmount();
  });

  it("点重试：重新请求同一话题并在成功后显示详情", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    mocks.apiMock.getTopicDetail.mockRejectedValueOnce(new Error("boom"));
    const w = mountView(pinia);
    await flushPromises();
    await nextTick();
    mocks.apiMock.getTopicDetail.mockResolvedValueOnce(DETAIL);

    await w.find(".detail-error button").trigger("click");
    await flushPromises();
    await nextTick();

    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledTimes(2);
    expect(w.find(".detail-error").exists()).toBe(false);
    expect(w.find(".detail").exists()).toBe(true);
    w.unmount();
  });

  it("首次数据加载失败：显示可见失败说明与重试入口（不能拿空球冒充完整内容）", async () => {
    mocks.apiMock.listTopics.mockRejectedValueOnce(new Error("offline"));
    const w = mountView(newPinia());
    await flushPromises();
    const banner = w.find(".load-error");
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain("offline");
    expect(banner.find("button").text()).toContain("重试");

    mocks.apiMock.listTopics.mockResolvedValueOnce({ topics: TOPICS });
    await banner.find("button").trigger("click");
    await flushPromises();
    expect(w.find(".load-error").exists()).toBe(false);
    expect(w.findAll(".topic-list li").length).toBe(1);
    w.unmount();
  });

  /**
   * 第四阶段：普通模式不把内部协议术语甩给用户。
   *
   * 实测截图里失败条第一行是「星球数据加载失败：/api/planet/overview -> 500: {...}」——
   * 那是给排查看的，不是给人读的。规则：首行说人话，技术细节折叠但**仍然可达**
   * （不是删掉，否则用户和排查都拿不到原因）。
   */
  it("数据加载失败：首行是人话，内部接口与状态码只在可展开的技术详情里", async () => {
    mocks.apiMock.listTopics.mockRejectedValueOnce(
      new Error('/api/graph/topics -> 500: {"detail":"boom"}'),
    );
    const w = mountView(newPinia());
    await flushPromises();
    const banner = w.find(".load-error");
    const first = banner.find(".le-text").text();
    expect(first).toContain("星球数据加载失败");
    expect(first).not.toContain("/api/");
    expect(first).not.toContain("500");
    // 技术细节仍然可达（折叠）
    expect(banner.text()).toContain("/api/graph/topics");
    expect(banner.find(".le-detail").exists()).toBe(true);
    w.unmount();
  });
});

describe("星球记忆中心面板", () => {
  it("选中片段后：明示「已选择历史位置」+「从这里继续」，并提交 topic+fragment（任务05 A）", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue({
      ...DETAIL,
      fragments: [
        {
          fragment_id: "f13",
          summary: "Anchor 生命周期：为什么 anchor 不等于最新片段",
          closed_at: "2026-09-12T00:00:00Z",
          message_count: 12,
          messages: [],
        },
      ],
    });
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();

    // 片段列表现在是「摘要 + 消息数 + 查看原文 / 从这里继续」的记忆浏览入口
    expect(w.find(".section-title").text()).toContain("片段历史");
    expect(w.find(".fragment-item .raw-toggle").text()).toContain("查看原文");
    expect(w.find(".fragment-item .frag-continue").text()).toContain("从这里继续");
    await w.find(".fragment-item").trigger("click");
    await nextTick();
    const hint = w.find(".selected-position");
    expect(hint.exists()).toBe(true);
    expect(hint.text()).toContain("Anchor 生命周期");
    expect(w.find(".start-btn").text()).toContain("从这里继续");
    expect(w.find(".start-btn").text()).toContain("这个历史位置");

    await w.find(".start-btn").trigger("click");
    await flushPromises();
    // 第二阶段：从历史继续 = 新建接续片段（旧片段不变），不是直接把位置钉在旧片段上
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledWith("t1", "f13");
    expect(session.anchorHistoric).toBe(true);
    // 标题用后端返回的权威值（不是前端从摘要里截的 40 字）
    expect(session.anchorFragment?.title).toBe("Anchor 生命周期");
    // 位置落在新建的接续片段上，来源历史留在后端 source_fragment_id
    expect(session.anchorFragmentId).toBe("frag_new");
    w.unmount();
  });

  it("选中「当前开放片段」不算历史位置：不显示「从…继续」，文案区分当前片段（任务05 B）", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue({
      ...DETAIL,
      fragments: [
        {
          fragment_id: "f20",
          summary: null,
          closed_at: null,
          message_count: 2,
          messages: [],
        },
      ],
    });
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();

    await w.find(".fragment-item").trigger("click");
    await nextTick();
    expect(w.find(".selected-position").text()).toContain("当前片段");
    expect(w.find(".start-btn").text()).toContain("当前片段");

    await w.find(".start-btn").trigger("click");
    await flushPromises();
    // 选中的就是当前开放片段：后端不会另开一段，位置仍停在这个片段上
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledWith("t1", "f20");
    // 当前开放片段不是「历史位置」：对话页不应出现「从…继续」
    expect(session.anchorHistoric).toBe(false);
    w.unmount();
  });

  it("快速点击 A→B：慢的 A 后返回也不覆盖 B 的详情（问题8）", async () => {
    const A_DETAIL: TopicDetail = { topic_id: "tA", name: "话题 A", fragments: [], entities: [], knowledge: [] };
    const B_DETAIL: TopicDetail = { topic_id: "tB", name: "话题 B", fragments: [], entities: [], knowledge: [] };
    const pending = new Map<string, (d: TopicDetail) => void>();
    const getDetail = mocks.apiMock.getTopicDetail as unknown as ReturnType<typeof vi.fn>;
    getDetail.mockImplementation(
      (id: string) => new Promise<TopicDetail>((r) => { pending.set(id, r); }),
    );

    const w = mountView(newPinia());
    await flushPromises();
    // A latency 200ms（后返回），B latency 20ms（先返回）
    currentFake!.selectedTopicId.value = "tA";
    await nextTick();
    currentFake!.selectedTopicId.value = "tB";
    await nextTick();

    pending.get("tB")!(B_DETAIL);
    await flushPromises();
    expect(w.find(".detail h3").text()).toBe("话题 B");

    pending.get("tA")!(A_DETAIL);
    await flushPromises();
    expect(w.find(".detail h3").text()).toBe("话题 B");
    expect(w.find(".detail h3").text()).not.toBe("话题 A");
    w.unmount();
  });

  it("从这里开始：API 失败不改本地锚点、不自动关闭、显示错误，重试成功才生效（问题2）", async () => {
    vi.useFakeTimers();
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.topicName = "旧话题";
    const w = mountView(pinia);
    await flushPromises();

    const setAnchor = mocks.apiMock.setAnchor as unknown as ReturnType<typeof vi.fn>;
    setAnchor.mockRejectedValueOnce(new Error("POST /api/anchor -> 500: boom"));

    await w.find(".start-btn").trigger("click");
    await flushPromises();

    // 失败：本地锚点不变、不拉回、不关闭；错误可见
    expect(session.currentTopicId).toBe("t1");
    expect(session.topicName).toBe("旧话题");
    expect(w.classes()).not.toContain("settling"); // 失败不该开始收起
    expect(w.emitted("close")).toBeFalsy();
    expect(w.find(".anchor-error").text()).toContain("未切换");

    // 重试成功 → 本地锚点更新 + 关闭
    await w.find(".start-btn").trigger("click");
    await flushPromises();
    expect(session.topicName).toBe("话题 A");
    expect(w.classes()).toContain("settling"); // 成功才走收起
    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("三页签可切换，管理模式加宽面板", async () => {
    const pinia = newPinia();
    const w = mountView(pinia, true);
    await flushPromises();
    expect(w.find(".tab-knowledge").exists()).toBe(true);
    await w.find(".tab-knowledge").trigger("click");
    await flushPromises();
    expect(w.find(".panel").classes()).toContain("manage");
    await w.find(".tab-topic").trigger("click");
    await flushPromises();
    w.unmount();
  });

  it("同话题 focusTopicFromPanel：知识面板点击关联话题强制刷新详情", async () => {
    mocks.apiMock.listKnowledge.mockResolvedValue({
      knowledge: [{
        id: "kn_1", category: "goal", state: "active", content: "测试知识",
        confidence: null, topic_id: "t1", topic_name: "话题 A", created_at: "", updated_at: "",
      }],
    });
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1"; // 锚点 → 挂载即加载 t1 详情
    const w = mountView(pinia, true);
    await flushPromises();
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledTimes(1);
    // 切到知识页签，点击与当前详情同话题的关联话题链接
    await w.find(".tab-knowledge").trigger("click");
    await flushPromises();
    await w.find(".k-topic-link").trigger("click");
    await flushPromises();
    // watcher 会跳过同话题 reload，这里应强制刷新 → 再次请求详情
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledTimes(2);
    expect(mocks.apiMock.getTopicDetail).toHaveBeenLastCalledWith("t1");
    w.unmount();
  });

  it("实体标签可点击 → 打开实体页签", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue({
      ...DETAIL,
      entities: [{ id: "n1", name: "王翠华" }],
    });
    mocks.apiMock.listEntities.mockResolvedValue({
      entities: [{
        id: "ec_1", node_id: "n1", name: "王翠华", aliases: [], kind: "家人",
        summary: "我妈妈", attributes: [], relations: [], state: "active",
        created_at: "", updated_at: "",
      }],
    });
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia, true);
    await flushPromises();
    await w.find(".entity-tag").trigger("click");
    await flushPromises();
    expect(w.find(".tab-entity").classes()).toContain("active");
    // openByNodeId 按 node_id 命中 → 实体卡详情已展开
    expect(w.find(".e-title").text()).toContain("王翠华");
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

  it("WebGL 不可用：给出可读降级说明，而不是只 console.error", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.webglOK.value = false;
    await nextTick();
    expect(w.find(".webgl-fallback").exists()).toBe(true);
    expect(w.find(".webgl-fallback").text()).toContain("无法启用 3D 星球");
    w.unmount();
  });
});

describe("任务05 收尾：加载提示、详情布局、选择可达性与浏览状态", () => {
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
    // 概览之后才会请求第一批窗口（两层接口顺序调用）
    await flushPromises();
    resolvePos();
    await flushPromises();
    expect(w.find(".planet-loading").exists()).toBe(false);
    w.unmount();
  });

  it("详情区：可滚动中部 + 固定底部操作区，操作按钮不在滚动容器内（底部不覆盖内容）", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();

    const body = w.find(".panel-body");
    expect(body.exists()).toBe(true);
    const actions = w.find(".detail-actions");
    expect(actions.exists()).toBe(true);
    expect(actions.find(".start-btn").exists()).toBe(true);
    // 操作按钮属于固定底部区，不属于滚动中部
    expect(body.find(".start-btn").exists()).toBe(false);
    // 可滚动中部含列表与详情正文
    expect(body.find(".topic-list").exists()).toBe(true);
    expect(body.find(".detail").exists()).toBe(true);
    w.unmount();
  });

  it("三者区分：明确显示「浏览的话题」「选中的片段」「已生效的起点」", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.setAnchor("t1", "f13", "话题 A", { id: "f13", title: "Anchor 生命周期" }, true);
    const w = mountView(pinia);
    await flushPromises();

    const text = w.find(".detail-identity").text();
    expect(text).toContain("浏览的话题");
    expect(text).toContain("话题 A");
    expect(text).toContain("选中的片段");
    expect(text).toContain("已生效的起点");
    expect(text).toContain("Anchor 生命周期");
    w.unmount();
  });

  it("话题列表可键盘选择：li 可聚焦，Enter 触发聚焦（列表键盘可达）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    const li = w.find(".topic-list li");
    expect(li.attributes("tabindex")).toBe("0");
    expect(li.attributes("role")).toBe("option");
    await li.trigger("keydown.enter");
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", []);
    w.unmount();
  });

  it("Esc 分层：面板打开时先收面板不关星球；面板已收起才收起星球", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia(), true);
    await flushPromises();
    await w.find(".panel-toggle").trigger("click");
    await nextTick();
    expect(w.find(".panel").classes()).toContain("open");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(w.find(".panel").classes()).not.toContain("open");
    expect(mocks.pullBackMock).not.toHaveBeenCalled();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.classes()).toContain("settling"); // 收起已经开始
    await vi.advanceTimersByTimeAsync(620);
    expect(w.emitted("close")).toBeTruthy();
    vi.useRealTimers();
    w.unmount();
  });

  it("重开时以当前在聊的话题为中心（浏览记忆不把它顶掉；用户实测要求）", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS2 });
    mocks.apiMock.planetOverview.mockResolvedValue({ topics: SUMMARIES2, total: 2, visible_capacity: 12 });
    mocks.apiMock.planetBrowse.mockImplementation(async (body?: { current_topic_id?: string | null }) => browseFor(body?.current_topic_id, SUMMARIES2));

    const first = mountView(pinia);
    await flushPromises();
    // 浏览到另一个话题（星球选中态是唯一来源）
    currentFake!.selectedTopicId.value = "t2";
    await flushPromises();
    first.unmount();

    mocks.focusTopicMock.mockClear();
    setActivePinia(pinia);
    const second = mountView(pinia);
    await flushPromises();
    /**
     * 用户实测要求：「打开前当前话题不在最中心，就在打开的过程中转到中心」。
     * 所以重开时**当前在聊的话题（t1）优先**，浏览记忆（t2）只在它不可见时兜底
     * —— 这一条覆盖了原来「起点未变就回到上次浏览的话题」的旧规则。
     */
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", []);
    second.unmount();
  });

  it("起点已变化时按新起点定位（浏览记忆不覆盖明确的新起点）", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS2 });
    mocks.apiMock.planetOverview.mockResolvedValue({ topics: SUMMARIES2, total: 2, visible_capacity: 12 });
    mocks.apiMock.planetBrowse.mockImplementation(async (body?: { current_topic_id?: string | null }) => browseFor(body?.current_topic_id, SUMMARIES2));

    const first = mountView(pinia);
    await flushPromises();
    currentFake!.selectedTopicId.value = "t2";
    await flushPromises();
    first.unmount();

    // 起点明确变化（同话题、指定了历史片段）→ 以新起点为准
    useSessionStore().setAnchor("t1", "f13", "话题 A", { id: "f13", title: "Anchor 生命周期" }, true);
    mocks.focusTopicMock.mockClear();
    setActivePinia(pinia);
    const second = mountView(pinia);
    await flushPromises();
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", []);
    second.unmount();
  });

  it("WebGL 不可用时管理入口仍可用：面板可展开并列出话题", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    currentFake!.webglOK.value = false;
    await nextTick();
    await w.find(".panel-toggle").trigger("click");
    await nextTick();
    expect(w.find(".panel").classes()).toContain("open");
    expect(w.findAll(".topic-list li").length).toBe(1);
    w.unmount();
  });

  it("A→B→C 连续选择：无论返回顺序如何，最后选中的 C 一定胜出", async () => {
    const detailOf = (id: string, name: string): TopicDetail => ({ topic_id: id, name, fragments: [], entities: [], knowledge: [] });
    const pending = new Map<string, (d: TopicDetail) => void>();
    const getDetail = mocks.apiMock.getTopicDetail as unknown as ReturnType<typeof vi.fn>;
    getDetail.mockImplementation((id: string) => new Promise<TopicDetail>((r) => { pending.set(id, r); }));

    mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS2 });
    mocks.apiMock.planetOverview.mockResolvedValue({ topics: SUMMARIES2, total: 2, visible_capacity: 12 });
    mocks.apiMock.planetBrowse.mockImplementation(async (body?: { current_topic_id?: string | null }) => browseFor(body?.current_topic_id, SUMMARIES2));
    const w = mountView(newPinia());
    await flushPromises();

    currentFake!.selectedTopicId.value = "t1";
    await nextTick();
    currentFake!.selectedTopicId.value = "t2";
    await nextTick();
    currentFake!.selectedTopicId.value = "t1"; // 回到 A 作为「C」
    await nextTick();

    // 乱序返回：过期的 B 先到 → 不得显示出来
    pending.get("t2")!(detailOf("t2", "话题 B"));
    await flushPromises();
    expect(w.find(".detail").exists()).toBe(false);
    expect(w.find(".detail-loading").exists()).toBe(true); // 仍在等当前这一次
    expect(w.find(".panel-body .detail h3").exists()).toBe(false);
    // 当前的 A 后到 → 显示 A
    pending.get("t1")!(detailOf("t1", "话题 A"));
    await flushPromises();
    expect(w.find(".detail h3").text()).toBe("话题 A");
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
    expect(label.attributes("style")).toContain("left: 200px");

    // 悬停标签出现时，选中标签让位（同一个位置不叠两层文字）
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

    /**
     * 关闭 = 回到入口小球状态，**不是停止渲染**。
     * 小球现在由真实星球场景承担（同一个场景缩到入口尺度），所以它会继续以低帧率渲染、
     * 并保持慢速自转；对应用户要的「入口小球就是真实星球，平常微微旋转」。
     */
    await w.setProps({ open: false });
    await flushPromises();
    expect(w.find(".planet-view").classes()).toContain("ball");
    expect(mocks.setLowPowerMock).toHaveBeenLastCalledWith(true);
    expect(mocks.setIdleSpinMock).toHaveBeenLastCalledWith(true);
    expect(mocks.setPausedMock).not.toHaveBeenLastCalledWith(true);

    // 重新打开
    await w.setProps({ open: true });
    await flushPromises();
    expect(mocks.initMock).toHaveBeenCalledTimes(1); // 复用：没有重建场景
    expect(mocks.setLowPowerMock).toHaveBeenLastCalledWith(false);
    expect(mocks.setIdleSpinMock).toHaveBeenLastCalledWith(false);
    expect(mocks.apiMock.listTopics.mock.calls.length).toBeGreaterThan(listCallsAfterMount); // 数据仍然刷新
    w.unmount();
  });

  it("关闭过程中又打开一次：放弃这次收尾（不 emit、不把新层变成收起中）", async () => {
    vi.useFakeTimers();
    const w = mountView(newPinia());
    w.setProps({ seq: 7 });
    await flushPromises();

    await w.find(".close-btn").trigger("click");
    expect(w.classes()).toContain("settling"); // 收势进行中
    // 关闭动画还没结束，用户又打开了一次（序号变成 8）
    await w.setProps({ seq: 8 });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(620);

    expect(w.emitted("close")).toBeUndefined(); // 旧收尾被放弃：既不 emit，也不结束新的一次打开
    expect(w.classes()).not.toContain("closing");
    expect(w.classes()).not.toContain("settling");
    vi.useRealTimers();
    w.unmount();
  });
});

describe("第二阶段：浏览 ≠ 进入话题", () => {
  beforeEach(() => {
    mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS2 });
    mocks.apiMock.planetOverview.mockResolvedValue({ topics: SUMMARIES2, total: 2, visible_capacity: 12 });
    mocks.apiMock.planetBrowse.mockImplementation(async (body?: { current_topic_id?: string | null }) =>
      browseFor(body?.current_topic_id, SUMMARIES2),
    );
  });

  it("选中话题只是查看：不调用 setAnchor，也不新建接续片段", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();

    await w.findAll(".topic-list li")[1].trigger("click");
    await flushPromises();

    expect(mocks.apiMock.setAnchor).not.toHaveBeenCalled();
    expect(mocks.apiMock.continueFromHistory).not.toHaveBeenCalled();
    expect(useSessionStore().currentTopicId).toBe("t1");
  });

  it("窗口里没有的话题被选中时进入展示窗口（搜索/列表都能让它出现）", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    // 第一批只带当前话题：t2 不在窗口里，只能靠「列表/搜索命中 → 注入窗口」出现
    mocks.apiMock.planetBrowse.mockImplementation(async () => browsePage([SUMMARIES[0]]));
    const w = mountView(pinia);
    await flushPromises();
    expect(attachedSession?.windowSlots().some((s) => s?.topic_id === "t2")).toBe(false);

    await w.findAll(".topic-list li")[1].trigger("click");
    await flushPromises();

    expect(attachedSession?.windowSlots().some((s) => s?.topic_id === "t2")).toBe(true);
    expect(mocks.focusTopicMock).toHaveBeenLastCalledWith("t2", []);
  });

  it("未选片段时按钮是「进入这个话题」，按下才提交新的起点", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();

    expect(w.find(".start-btn").text()).toContain("进入「话题 A」");
    await w.find(".start-btn").trigger("click");
    await flushPromises();

    expect(mocks.apiMock.setAnchor).toHaveBeenCalledWith("t1", null);
    expect(mocks.apiMock.continueFromHistory).not.toHaveBeenCalled();
  });

  it("浏览会话里选中的话题被锁定：查看期间不会被回收", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();

    await w.findAll(".topic-list li")[1].trigger("click");
    await flushPromises();
    const slot = attachedSession!.windowSlots().findIndex((s) => s?.topic_id === "t2");

    attachedSession!.takeSwap(1, { backSlots: [slot] }, performance.now() + 10_000);

    expect(attachedSession!.windowSlots()[slot]?.topic_id).toBe("t2");
  });
});

/**
 * 第三阶段 Task 9：Topic Detail 成为完整的记忆浏览链路
 * 话题 → 片段（摘要 / 时间 / 消息数）→ 原文（按需 + 分页）→ 从这里继续。
 */
describe("第三阶段：话题详情的记忆浏览链路", () => {
  const FRAG = {
    fragment_id: "f13",
    summary: "Anchor 生命周期的讨论",
    closed_at: "2026-09-10T08:00:00Z",
    created_at: "2026-09-10T07:00:00Z",
    message_count: 8,
  };
  const DETAIL_FULL: TopicDetail = {
    topic_id: "t1",
    name: "话题 A",
    summary: "Anchor 生命周期的讨论。",
    keywords: ["Anchor", "生命周期"],
    last_activity: "2026-09-14T09:00:00Z",
    message_count: 42,
    fragments: [FRAG],
    entities: [],
    knowledge: [
      { id: "kn_1", category: "user_profile", state: "pending_review", content: "用户偏好简洁", confidence: null, updated_at: "2026-09-14T09:00:00Z" },
    ],
  };

  function mountWithDetail() {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";
    mocks.apiMock.getTopicDetail.mockResolvedValue(DETAIL_FULL);
    return mountView(pinia);
  }

  it("详情分层：一句摘要 + 最近活动/片段数/消息数 + 关键词", async () => {
    const w = mountWithDetail();
    await flushPromises();
    expect(w.find(".detail-summary").text()).toContain("Anchor 生命周期");
    const facts = w.find(".detail-facts").text();
    expect(facts).toContain("1 个片段");
    expect(facts).toContain("42 条消息");
    expect(facts).toContain("最近活动");
    expect(w.find(".detail-keywords").text()).toContain("Anchor");
    // 片段项：时间 + 摘要 + 消息数
    const item = w.find(".fragment-item");
    expect(item.find(".fragment-summary").text()).toContain("Anchor 生命周期的讨论");
    expect(item.find(".fragment-count").text()).toContain("8 条消息");
    w.unmount();
  });

  it("默认不加载原文；点「查看原文」才按需取第一页，并说明这是只读历史", async () => {
    const fetchRaw = mocks.apiMock.fragmentMessages as unknown as ReturnType<typeof vi.fn>;
    fetchRaw.mockImplementation(async (id: string, offset = 0, limit = 20) => ({
      fragment_id: id,
      topic_id: "t1",
      total: 30,
      offset,
      limit,
      messages: [
        { id: `m${offset + 1}`, role: "user", content: `第 ${offset + 1} 条`, content_type: "text", created_at: "2026-09-10T07:00:00Z" },
        { id: `m${offset + 2}`, role: "assistant", content: "回答", content_type: "text", created_at: "2026-09-10T07:01:00Z" },
      ],
    }));

    const w = mountWithDetail();
    await flushPromises();
    expect(fetchRaw).not.toHaveBeenCalled(); // 默认只有摘要

    await w.find(".fragment-item .raw-toggle").trigger("click");
    await flushPromises();
    expect(fetchRaw).toHaveBeenCalledWith("f13", 0, 20);
    const note = w.find(".raw-note").text();
    expect(note).toContain("当时发生过");
    expect(note).toContain("只读");
    expect(note).toContain("当前对话没有停在这里");
    expect(w.findAll(".raw-item").length).toBe(2);

    // 继续读取：追加下一段，不重复第一段
    await w.find(".raw-more").trigger("click");
    await flushPromises();
    expect(fetchRaw).toHaveBeenLastCalledWith("f13", 2, 20);
    expect(w.findAll(".raw-item").length).toBe(4);
    w.unmount();
  });

  it("分页读完后给出收尾提示（不再无限读）", async () => {
    const fetchRaw = mocks.apiMock.fragmentMessages as unknown as ReturnType<typeof vi.fn>;
    fetchRaw.mockImplementation(async (id: string, offset = 0, limit = 20) => ({
      fragment_id: id,
      topic_id: "t1",
      total: 1,
      offset,
      limit,
      messages: [{ id: "m1", role: "user", content: "只有一条", content_type: "text", created_at: "2026-09-10T07:00:00Z" }],
    }));
    const w = mountWithDetail();
    await flushPromises();
    await w.find(".fragment-item .raw-toggle").trigger("click");
    await flushPromises();
    expect(w.find(".raw-more").exists()).toBe(false);
    expect(w.find(".raw-end").text()).toContain("已读完");
    w.unmount();
  });

  it("读取原文失败：卡片内可见原因 + 重试（不静默失败）", async () => {
    const fetchRaw = mocks.apiMock.fragmentMessages as unknown as ReturnType<typeof vi.fn>;
    fetchRaw.mockRejectedValueOnce(new Error("offline"));
    const w = mountWithDetail();
    await flushPromises();
    await w.find(".fragment-item .raw-toggle").trigger("click");
    await flushPromises();
    expect(w.find(".raw-error").text()).toContain("读取原文失败");
    expect(w.find(".raw-error button").exists()).toBe(true);
    w.unmount();
  });

  it("片段上的「从这里继续」走 continueFromHistory（旧片段不改）", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    mocks.apiMock.getTopicDetail.mockResolvedValue(DETAIL_FULL);
    const w = mountView(pinia);
    await flushPromises();
    await w.find(".fragment-item .frag-continue").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledWith("t1", "f13");
    w.unmount();
  });

  it("知识条目的状态用中文（不把 pending_review 这类内部状态名丢给用户）", async () => {
    const w = mountWithDetail();
    await flushPromises();
    const state = w.find(".knowledge-item .k-state").text();
    expect(state).toBe("待确认");
    expect(state).not.toContain("pending_review");
    w.unmount();
  });

  it("知识修正失败：错误留在该条目上并可重试（不再只写 console）", async () => {
    const revise = mocks.apiMock.reviseKnowledge as unknown as ReturnType<typeof vi.fn>;
    revise.mockRejectedValueOnce(new Error("offline"));
    const w = mountWithDetail();
    await flushPromises();
    await w.find(".knowledge-item .qio-btn.mini").trigger("click"); // 修正
    await w.find(".knowledge-edit").setValue("用户偏好极简");
    const saveBtn = w.findAll(".knowledge-item .qio-btn.mini").find((b) => b.text().includes("保存"));
    await saveBtn!.trigger("click");
    await flushPromises();
    expect(revise).toHaveBeenCalled();
    const err = w.find(".k-feedback.err");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("修改没有保存");
    expect(err.text()).toContain("可以重试");

    // 重试成功：错误消失，出现短暂的「已保存」
    await saveBtn!.trigger("click");
    await flushPromises();
    expect(w.find(".k-feedback.err").exists()).toBe(false);
    expect(w.find(".k-feedback.ok").text()).toContain("已保存");
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

  it("能拿到球体屏幕几何时，星球层从入口小球的尺度开始（同一个对象长大）", async () => {
    // 入口小球在视口右侧（96×96 → 半径 48），球体屏幕半径 300 → 起始缩放约 0.16
    const entry = document.createElement("button");
    entry.setAttribute("data-planet-entry", "");
    entry.getBoundingClientRect = () =>
      ({ left: 1200, top: 400, width: 96, height: 96, right: 1296, bottom: 496, x: 1200, y: 400 }) as DOMRect;
    document.body.appendChild(entry);
    mocks.sphereRectMock.mockReturnValue({ cx: 700, cy: 400, radius: 300 });
    vi.useFakeTimers();
    const w = mountView(newPinia());
    await flushPromises();
    const stage = w.find(".planet-stage");
    // jsdom 的 cssstyle 不保存自定义属性，所以读组件暴露的 data-stage-k
    const k = Number(stage.attributes("data-stage-k"));
    expect(k).toBeGreaterThan(0);
    expect(k).toBeLessThan(0.3); // 远小于 1：确实是「从小球尺度开始」
    expect(mocks.sphereRectMock).toHaveBeenCalled();
    // 缩放中间态：画布坐标不再对应真实球面，命中必须锁住
    await w.find("canvas").trigger("click", { clientX: 10, clientY: 10 });
    expect(mocks.handleClickMock).not.toHaveBeenCalled();
    vi.useRealTimers();
    w.unmount();
    entry.remove();
  });
});
