import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { ref, shallowRef, nextTick } from "vue";
import PlanetView from "../PlanetView.vue";
import { useSessionStore } from "../../stores/session";
import type { EntityCard, KnowledgeItem, TopicDetail, TopicFingerprint, TopicPosition } from "../../services/api";

const mocks = vi.hoisted(() => ({
  goMock: vi.fn(() => Promise.resolve()),
  focusTopicMock: vi.fn(),
  setThemeMock: vi.fn(),
  initMock: vi.fn(),
  loadTopicsMock: vi.fn(),
  setTopicsMock: vi.fn(),
  handleClickMock: vi.fn(),
  apiMock: {
    listTopics: vi.fn<() => Promise<{ topics: TopicFingerprint[] }>>(async () => ({ topics: [] })),
    getPositions: vi.fn<() => Promise<{ topics: TopicPosition[] }>>(async () => ({ topics: [] })),
    getTopicDetail: vi.fn<() => Promise<TopicDetail>>(async () => ({}) as TopicDetail),
    setAnchor: vi.fn<() => Promise<{ ok: boolean; topic_id: string; fragment_id: string | null }>>(
      async () => ({ ok: true, topic_id: "", fragment_id: null }),
    ),
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
  },
}));

let currentFake: ReturnType<typeof createFakePlanet> | null = null;
function createFakePlanet() {
  const selectedTopicId = ref<string | null>(null);
  return {
    webglOK: ref(true),
    fps: ref(60),
    cameraState: ref<"overview" | "planet" | "focus">("overview"),
    selectedTopicId,
    markers: shallowRef([]),
    init: mocks.initMock,
    loadTopics: mocks.loadTopicsMock,
    go: mocks.goMock,
    // 模拟真实 focusTopic：同步设置 selectedTopicId（触发详情 watcher）
    focusTopic: (topicId: string, topics: unknown) => {
      selectedTopicId.value = topicId;
      mocks.focusTopicMock(topicId, topics);
    },
    handleClick: mocks.handleClickMock,
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
const POSITIONS: TopicPosition[] = [{ topic_id: "t1", name: "话题 A", position: [0, 0, 1], activity: 1, updated_at: "" }];
const DETAIL: TopicDetail = { topic_id: "t1", name: "话题 A", fragments: [], entities: [], knowledge: [] };

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
  document.documentElement.removeAttribute("data-theme");
  mocks.handleClickMock.mockReturnValue(false);
  mocks.goMock.mockImplementation(() => Promise.resolve());
  mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS });
  mocks.apiMock.getPositions.mockResolvedValue({ topics: POSITIONS });
  mocks.apiMock.getTopicDetail.mockResolvedValue(DETAIL);
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [] });
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [] });
});

describe("PlanetView 相机联动", () => {
  it("无锚点挂载：初始化场景并推进到 planet（悬浮球点击 → 全屏近景）", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    expect(mocks.initMock).toHaveBeenCalled();
    expect(mocks.setTopicsMock).toHaveBeenCalledWith(POSITIONS);
    expect(mocks.loadTopicsMock).toHaveBeenCalledWith(POSITIONS);
    expect(mocks.goMock).toHaveBeenLastCalledWith("planet");
    w.unmount();
  });

  it("已有锚点：挂载后聚焦锚点话题并加载详情", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const w = mountView(pinia);
    await flushPromises();
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", POSITIONS);
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    w.unmount();
  });

  it("点击收起：相机先拉回 overview 再 emit close", async () => {
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".close-btn").trigger("click");
    await flushPromises();
    expect(mocks.goMock).toHaveBeenLastCalledWith("overview");
    expect(w.emitted("close")).toBeTruthy();
    w.unmount();
  });

  it("关闭动画进行中：话题列表/画布单击/双击不打断拉回，拉回结束后才 emit close", async () => {
    const resolvers: (() => void)[] = [];
    mocks.goMock.mockImplementation(() => new Promise<void>((r) => { resolvers.push(r); }));
    const w = mountView(newPinia());
    await flushPromises();
    expect(resolvers.length).toBe(1); // 打开时 go("planet") 已发起（挂起）

    await w.find(".close-btn").trigger("click");
    expect(resolvers.length).toBe(2); // 关闭拉回 go("overview") 已发起（挂起）
    expect(w.classes()).toContain("closing");

    // 关闭动画窗口内：话题点击不聚焦、画布单击不命中、双击不切回 planet
    await w.find(".topic-list li").trigger("click");
    await w.find("canvas").trigger("click", { clientX: 5, clientY: 5 });
    await w.find("canvas").trigger("dblclick");
    expect(mocks.focusTopicMock).not.toHaveBeenCalled();
    expect(mocks.handleClickMock).not.toHaveBeenCalled();
    expect(mocks.goMock).toHaveBeenCalledTimes(2);

    resolvers[1](); // 拉回完成 → 才 emit close
    await flushPromises();
    expect(w.emitted("close")).toBeTruthy();
    w.unmount();
  });

  it("Esc：焦点在输入框内不收起；焦点在外收起并拉回 overview", async () => {
    const w = mountView(newPinia(), true);
    await flushPromises();
    const searchInput = w.find(".panel-head input").element;
    searchInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.emitted("close")).toBeFalsy();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(mocks.goMock).toHaveBeenLastCalledWith("overview");
    expect(w.emitted("close")).toBeTruthy();
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

  it("「从这里开始」：保留 setAnchor 数据流，关闭前拉回 overview", async () => {
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
    expect(mocks.goMock).toHaveBeenLastCalledWith("overview");
    expect(w.emitted("close")).toBeTruthy();
    w.unmount();
  });

  it("列表点击话题：详情只请求一次（watcher 单一来源，慢 API 下不重复）", async () => {
    let resolveDetail!: (d: TopicDetail) => void;
    mocks.apiMock.getTopicDetail.mockImplementation(() => new Promise<TopicDetail>((r) => { resolveDetail = r; }));
    const w = mountView(newPinia());
    await flushPromises();
    await w.find(".topic-list li").trigger("click");
    await flushPromises();
    expect(mocks.focusTopicMock).toHaveBeenCalledWith("t1", POSITIONS);
    // 显式 loadDetail 已移除：watcher 是唯一详情来源，慢 API 下也只请求一次
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    resolveDetail(DETAIL);
    await flushPromises();
    w.unmount();
  });

  it("挂载时 API pending → 关闭 → API resolve 后 close 仍 emit（loadData 不打断拉回）", async () => {
    const resolvers: (() => void)[] = [];
    let resolveList!: () => void;
    let resolvePos!: () => void;
    mocks.goMock.mockImplementation(() => new Promise<void>((r) => { resolvers.push(r); }));
    mocks.apiMock.listTopics.mockImplementation(() => new Promise((r) => { resolveList = () => r({ topics: TOPICS }); }));
    mocks.apiMock.getPositions.mockImplementation(() => new Promise((r) => { resolvePos = () => r({ topics: POSITIONS }); }));
    const w = mountView(newPinia());
    // loadData 的 API 尚未 resolve 时先关闭
    await w.find(".close-btn").trigger("click");
    expect(mocks.goMock).toHaveBeenLastCalledWith("overview");
    expect(resolvers.length).toBe(1);
    // 随后 API resolve → loadData 继续，但不得再推进 planet / 聚焦话题
    resolveList();
    resolvePos();
    await flushPromises();
    expect(mocks.goMock).toHaveBeenCalledTimes(1);
    expect(mocks.focusTopicMock).not.toHaveBeenCalled();
    // 拉回完成 → close 仍正常 emit
    resolvers[0]();
    await flushPromises();
    expect(w.emitted("close")).toBeTruthy();
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
    expect(mocks.focusTopicMock).toHaveBeenLastCalledWith("t1", POSITIONS);
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
    expect(mocks.focusTopicMock).toHaveBeenLastCalledWith("t1", POSITIONS);
    vi.useRealTimers();
    w.unmount();
  });
});

describe("星球记忆中心面板", () => {
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
