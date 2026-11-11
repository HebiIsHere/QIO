/**
 * R17-C（契约 §2.4 / §5 B4）· 一次点击一次导航。
 *
 * 星球页「继续讨论 / 从这里继续」：
 * * 「进入话题最新位置」= 一个 enter_topic（POST /api/anchor）；
 * * 「返回当前开放片段」= 一个 continue_from_history（POST /api/anchor，fragment=开放片段）；
 * * 「选择历史片段」= 一个 continue_from_history；
 * 三者都**不再**先发独立的取消命令（由后端在一个事务里清理不兼容的 registered 接续）。
 * 失败（409 / 网络）时原位置与原接续提示保留；同一动作重试复用同一 request_id。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { ref, shallowRef, nextTick } from "vue";
import PlanetView from "../PlanetView.vue";
import { useSessionStore } from "../../stores/session";
import { resetPlanetSession } from "../../composables/planetSession";
import type { TopicDetail, TopicFingerprint, PlanetTopicSummary } from "../../services/api";

const mocks = vi.hoisted(() => ({
  initMock: vi.fn(),
  goMock: vi.fn(() => Promise.resolve()),
  focusTopicMock: vi.fn(),
  pullBackMock: vi.fn(() => Promise.resolve()),
  markReturnOrientationMock: vi.fn(),
  rotateBackMock: vi.fn(() => 0),
  primeCameraMock: vi.fn(),
  setPausedMock: vi.fn(),
  setLowPowerMock: vi.fn(),
  setIdleSpinMock: vi.fn(),
  setRevealMock: vi.fn(),
  attachBrowseMock: vi.fn(),
  handleClickMock: vi.fn(() => false),
  setManageYawMock: vi.fn(),
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
  },
}));

let currentFake: ReturnType<typeof createFakePlanet> | null = null;

function createFakePlanet() {
  return {
    webglOK: ref(true),
    fps: ref(60),
    cameraState: ref<"overview" | "planet" | "focus">("planet"),
    selectedTopicId: ref<string | null>(null),
    markers: shallowRef([]),
    init: mocks.initMock,
    attachBrowse: mocks.attachBrowseMock,
    windowTopicIds: () => [] as (string | null)[],
    go: mocks.goMock,
    focusTopic: (topicId: string, topics: unknown) => {
      currentFake!.selectedTopicId.value = topicId;
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
    setManageYaw: (rad: number) => mocks.setManageYawMock(rad),
    setRingScreenWidth: vi.fn(),
    setReveal: mocks.setRevealMock,
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
  usePlanetScene: () => (currentFake = createFakePlanet()),
}));
vi.mock("../../services/api", () => ({ api: mocks.apiMock }));

const TOPICS: TopicFingerprint[] = [
  { topic_id: "t1", title: "话题 A", keywords: [], fragment_count: 3, last_activity: null, summary_preview: null },
  { topic_id: "t2", title: "话题 B", keywords: [], fragment_count: 1, last_activity: null, summary_preview: null },
];
const SUMMARIES: PlanetTopicSummary[] = TOPICS.map((t, i) => ({
  topic_id: t.topic_id,
  title: t.title,
  fragment_count: t.fragment_count,
  last_activity: null,
  summary_preview: null,
  visual_seed: i + 1,
}));

function detailWith(fragments: TopicDetail["fragments"]): TopicDetail {
  return { topic_id: "t1", name: "话题 A", fragments, entities: [], knowledge: [] };
}

function browsePage(items: PlanetTopicSummary[]) {
  return {
    seed: 7,
    pass_index: 0,
    cursor: "7.0.0",
    prev_cursor: "7.0.0",
    next_cursor: "7.0.12",
    has_more: false,
    pass_changed: false,
    total: items.length,
    visible_capacity: 12,
    items,
  };
}

function newPinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

function mountView(pinia: Pinia) {
  return mount(PlanetView, { global: { plugins: [pinia] } });
}

async function openManagement(w: ReturnType<typeof mountView>) {
  document.documentElement.setAttribute("data-motion", "reduced");
  await w.find("[data-mp-manage-toggle]").trigger("click");
  await nextTick();
  document.documentElement.removeAttribute("data-motion");
  await flushPromises();
}

async function openOverview(w: ReturnType<typeof mountView>, topicId = "t1") {
  await w.find(`[data-topic-id="${topicId}"]`).trigger("click");
  await flushPromises();
}

async function clickOverviewContinue(w: ReturnType<typeof mountView>) {
  await w.find("[data-memory-overview] [data-memory-continue]").trigger("click");
  await flushPromises();
}

async function clickRecordContinue(w: ReturnType<typeof mountView>) {
  await w.find(".mt-open-record").trigger("click");
  await flushPromises();
  await w.find("[data-memory-record] [data-memory-continue]").trigger("click");
  await flushPromises();
}

const HISTORY_FRAGMENT = {
  fragment_id: "f13",
  summary: "Anchor 生命周期：为什么 anchor 不等于最新片段",
  closed_at: "2026-09-12T00:00:00Z",
  message_count: 12,
  messages: [],
};
const OPEN_FRAGMENT = {
  fragment_id: "f20",
  summary: null,
  closed_at: null,
  message_count: 2,
  messages: [],
};

function wire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    topic_id: "t1",
    topic_name: "话题 A",
    fragment_id: null,
    fragment_title: null,
    historic: false,
    pending_intent_id: null,
    pending_intent_version: null,
    pending_source_fragment_id: null,
    pending_source_title: null,
    nav_version: 5,
    position_version: 5,
    position_identity: "db-1",
    ...overrides,
  };
}

/** 本地已接受的起点：历史位置 + 未落实的接续选择 A。 */
function seedPendingPosition(session: ReturnType<typeof useSessionStore>) {
  session.acceptPositionState(
    wire({
      fragment_id: "f-old",
      historic: true,
      pending_intent_id: "intent_A",
      pending_intent_version: 2,
      pending_source_fragment_id: "f-old",
      pending_source_title: "历史片段 A",
    }),
    "snapshot",
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  currentFake = null;
  resetPlanetSession();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-motion");
  mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS });
  mocks.apiMock.planetOverview.mockResolvedValue({
    topics: SUMMARIES,
    total: SUMMARIES.length,
    visible_capacity: 12,
  });
  mocks.apiMock.planetBrowse.mockImplementation(async (body?: { current_topic_id?: string | null }) => {
    const anchor = body?.current_topic_id;
    const items = anchor
      ? [...SUMMARIES.filter((s) => s.topic_id === anchor), ...SUMMARIES.filter((s) => s.topic_id !== anchor)]
      : SUMMARIES;
    return browsePage(items);
  });
  mocks.apiMock.getTopicDetail.mockResolvedValue(detailWith([]));
  mocks.apiMock.setAnchor.mockResolvedValue({
    ok: true,
    changed: true,
    replay: false,
    position: wire({ nav_version: 6, position_version: 6 }),
  });
  mocks.apiMock.continueFromHistory.mockResolvedValue({
    ok: true,
    changed: true,
    replay: false,
    position: wire({ fragment_id: "frag_new", fragment_title: "Anchor 生命周期", historic: true, nav_version: 6, position_version: 6 }),
    created_fragment_id: "frag_new",
    source_fragment_id: "f13",
  });
  mocks.apiMock.cancelContinuation.mockResolvedValue({ ok: true, cancelled: true });
  mocks.apiMock.getRuntimeState.mockResolvedValue({
    instance_id: "inst-1",
    revision: 1,
    turn_queue: { instance_id: "inst-1", revision: 1, running: null, queued: [], cancelled: [] },
    approvals: [],
    tasks: [],
    tools: [],
  });
});

describe("R17-C · 一次点击一个导航命令（不再先发取消）", () => {
  it("进入话题最新位置：只发一个 /api/anchor，取消命令 0 次", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    seedPendingPosition(session);

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickOverviewContinue(w);

    expect(mocks.apiMock.cancelContinuation, "一次点击不得先发独立取消命令").not.toHaveBeenCalled();
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledWith(
      "t1",
      null,
      expect.objectContaining({ requestId: expect.any(String), baseNavVersion: expect.any(Number) }),
    );
    expect(mocks.apiMock.continueFromHistory).not.toHaveBeenCalled();
    // 原子导航结果整份接受：接续提示被后端结果清理。
    expect(session.pendingContinuation).toBeNull();
    const facts = (session as unknown as { _positionFacts: Record<string, unknown> | null })._positionFacts;
    expect(facts).toMatchObject({ positionVersion: 6 });
    w.unmount();
  });

  it("返回当前开放片段：只发一个 continue_from_history，取消命令 0 次", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue(detailWith([OPEN_FRAGMENT]));
    mocks.apiMock.continueFromHistory.mockResolvedValue({
      ok: true,
      changed: true,
      replay: false,
      position: wire({ fragment_id: "f20", historic: false, nav_version: 6, position_version: 6 }),
    });
    const pinia = newPinia();
    const session = useSessionStore();
    seedPendingPosition(session);

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickRecordContinue(w);

    expect(mocks.apiMock.cancelContinuation).not.toHaveBeenCalled();
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledWith(
      "t1",
      "f20",
      expect.objectContaining({ requestId: expect.any(String), baseNavVersion: expect.any(Number) }),
    );
    expect(mocks.apiMock.setAnchor).not.toHaveBeenCalled();
    w.unmount();
  });

  it("选择历史片段：只发一个 continue_from_history，取消命令 0 次", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue(detailWith([HISTORY_FRAGMENT]));
    const pinia = newPinia();
    const session = useSessionStore();
    seedPendingPosition(session);

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickRecordContinue(w);

    expect(mocks.apiMock.cancelContinuation).not.toHaveBeenCalled();
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledWith(
      "t1",
      "f13",
      expect.objectContaining({ requestId: expect.any(String), baseNavVersion: expect.any(Number) }),
    );
    w.unmount();
  });

  it("409 依据过期：只发一个导航命令，原位置与原接续保留、错误可见", async () => {
    mocks.apiMock.setAnchor.mockRejectedValueOnce({
      status: 409,
      body: { code: "navigation_command_stale" },
      message: "POST /api/anchor -> 409",
    });
    const pinia = newPinia();
    const session = useSessionStore();
    seedPendingPosition(session);

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickOverviewContinue(w);

    expect(mocks.apiMock.setAnchor).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.cancelContinuation).not.toHaveBeenCalled();
    expect(session.currentTopicId).toBe("t1");
    expect(session.anchorFragmentId).toBe("f-old");
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent_A", intentVersion: 2 });
    const error = w.find("[data-memory-overview] .mt-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("起点没有改变");
    w.unmount();
  });

  it("网络失败：原位置与原接续保留；同一动作重试复用同一 request_id", async () => {
    mocks.apiMock.setAnchor.mockRejectedValueOnce(new Error("fetch failed"));
    const pinia = newPinia();
    const session = useSessionStore();
    seedPendingPosition(session);

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickOverviewContinue(w);

    expect(mocks.apiMock.setAnchor).toHaveBeenCalledTimes(1);
    expect(session.currentTopicId).toBe("t1");
    expect(session.anchorFragmentId).toBe("f-old");
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent_A", intentVersion: 2 });
    const error = w.find("[data-memory-overview] .mt-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("没能改变起点");
    const firstOptions = mocks.apiMock.setAnchor.mock.calls[0][2] as {
      requestId: string;
      baseNavVersion: number;
    };

    // 同一动作重试：复用同一请求身份（后端据此识别同一次用户动作）。
    await clickOverviewContinue(w);
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledTimes(2);
    const retryOptions = mocks.apiMock.setAnchor.mock.calls[1][2] as {
      requestId: string;
      baseNavVersion: number;
    };
    expect(retryOptions.requestId).toBe(firstOptions.requestId);
    expect(retryOptions.baseNavVersion).toBe(firstOptions.baseNavVersion);
    w.unmount();
  });

  it("没有待落实接续时进入话题：仍然只有一个导航命令", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickOverviewContinue(w);

    expect(mocks.apiMock.cancelContinuation).not.toHaveBeenCalled();
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledTimes(1);
    w.unmount();
  });
});
