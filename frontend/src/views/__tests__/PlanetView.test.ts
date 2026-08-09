import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { ref, shallowRef, nextTick } from "vue";
import PlanetView from "../PlanetView.vue";
import { useSessionStore } from "../../stores/session";
import type { TopicDetail, TopicFingerprint, TopicPosition } from "../../services/api";

const mocks = vi.hoisted(() => ({
  goMock: vi.fn(() => Promise.resolve()),
  focusTopicMock: vi.fn(),
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
  },
}));

let currentFake: ReturnType<typeof createFakePlanet> | null = null;
function createFakePlanet() {
  return {
    webglOK: ref(true),
    fps: ref(60),
    cameraState: ref<"overview" | "planet" | "focus">("overview"),
    selectedTopicId: ref<string | null>(null),
    markers: shallowRef([]),
    init: mocks.initMock,
    loadTopics: mocks.loadTopicsMock,
    go: mocks.goMock,
    focusTopic: mocks.focusTopicMock,
    handleClick: mocks.handleClickMock,
    cancelAnimation: vi.fn(),
    resize: vi.fn(),
    setTheme: vi.fn(),
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

function mountView(pinia: Pinia) {
  return mount(PlanetView, { global: { plugins: [pinia], stubs: { MarkdownContent: true } } });
}

function newPinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS });
  mocks.apiMock.getPositions.mockResolvedValue({ topics: POSITIONS });
  mocks.apiMock.getTopicDetail.mockResolvedValue(DETAIL);
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
});
