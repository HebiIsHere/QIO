/**
 * E 组 · M01 受控验收（视图层）：星球页的「从这里继续 / 进入话题」动作边界。
 *
 * 契约要求的动作边界（断言意图与改造前完全一致）：
 * * 明确进入「某话题的最新位置」或选择「**当前开放片段**」→ **先原子取消**旧的未落实接续选择
 *   （复用既有 cancelContinuation 接口），再改起点；
 * * 选择一段**历史片段** → 由后端在登记时替换旧意图（不需要前端先取消）；
 * * 只是浏览星球（点话题、展开片段）→ 不取消、也不改起点。
 *
 * 2026-10-27 改版后，入口从星球页的旧侧栏移到**管理区记忆标签页**：
 * 点「管理」→ 记忆页话题列表 → 话题概览（底部「继续讨论 / 回到对话继续」）→
 * 完整讨论记录（片段选中后的「从这里继续」）。本 spec 只换路径，断言一条不删。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { ref, shallowRef, nextTick } from "vue";
import PlanetView from "../PlanetView.vue";
import { useSessionStore } from "../../stores/session";
import { resetPlanetSession } from "../../composables/planetSession";
import type {
  TopicDetail,
  TopicFingerprint,
  PlanetTopicSummary,
} from "../../services/api";

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
  /** 管理过渡由 C 的 composable 调用（姿态组旋转）：测试只需要它存在 */
  setManageYawMock: vi.fn(),
  apiMock: {
    listTopics: vi.fn(),
    planetOverview: vi.fn(),
    planetBrowse: vi.fn(),
    getTopicDetail: vi.fn(),
    setAnchor: vi.fn(),
    continueFromHistory: vi.fn(),
    cancelContinuation: vi.fn(),
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
    /** 管理过渡（C 的 composable）每帧调用；记录一下即可 */
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

/**
 * 打开管理区并停住。
 * 用「减少动态效果」一次性到位：这里验的是动作边界，不是过渡曲线
 * （曲线与逐帧由 A 的 docs/mp-verify/a-page/frames-mp-a.mjs 覆盖）。
 */
async function openManagement(w: ReturnType<typeof mountView>) {
  document.documentElement.setAttribute("data-motion", "reduced");
  await w.find("[data-mp-manage-toggle]").trigger("click");
  await nextTick();
  document.documentElement.removeAttribute("data-motion");
  await flushPromises();
}

/** 记忆页：点话题行打开概览（只是浏览） */
async function openOverview(w: ReturnType<typeof mountView>, topicId = "t1") {
  await w.find(`[data-topic-id="${topicId}"]`).trigger("click");
  await flushPromises();
}

/** 概览底部的「继续讨论 / 回到对话继续」 */
async function clickOverviewContinue(w: ReturnType<typeof mountView>) {
  await w.find("[data-memory-overview] [data-memory-continue]").trigger("click");
  await flushPromises();
}

/** 打开完整讨论记录（自动选中最新片段），再从那个位置继续 */
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
    topic_id: "t1",
    fragment_id: null,
    fragment_title: null,
    historic: false,
  });
  mocks.apiMock.continueFromHistory.mockResolvedValue({
    ok: true,
    topic_id: "t1",
    fragment_id: "frag_new",
    fragment_title: "Anchor 生命周期",
    historic: true,
    created_fragment_id: "frag_new",
    source_fragment_id: "f13",
  });
  mocks.apiMock.cancelContinuation.mockResolvedValue({ ok: true, cancelled: true });
});

describe("明确改变起点时原子取消旧的接续选择", () => {
  it("进入话题最新位置：先取消（一次）再 setAnchor，界面提示随即撤下", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.setPendingContinuation({ intentId: "intent_A", sourceTitle: "历史片段 A" });

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);

    // 概览底部的动作：话题最新位置（不是历史片段）
    await clickOverviewContinue(w);

    expect(mocks.apiMock.cancelContinuation).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledWith("t1", null);
    // 顺序：取消必须发生在改起点之前
    const cancelOrder = mocks.apiMock.cancelContinuation.mock.invocationCallOrder[0];
    const anchorOrder = mocks.apiMock.setAnchor.mock.invocationCallOrder[0];
    expect(cancelOrder).toBeLessThan(anchorOrder);
    expect(session.pendingContinuation).toBeNull();
    w.unmount();
  });

  it("选中「当前开放片段」：同样先取消，且仍走 continueFromHistory（后端不新建片段）", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue(detailWith([OPEN_FRAGMENT]));
    mocks.apiMock.continueFromHistory.mockResolvedValue({
      ok: true,
      topic_id: "t1",
      fragment_id: "f20",
      fragment_title: null,
      historic: false,
      created_fragment_id: null,
      source_fragment_id: null,
    });
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.setPendingContinuation({ intentId: "intent_A", sourceTitle: "历史片段 A" });

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    // 完整讨论记录会默认选中最新片段（这里是当前开放片段 f20）
    await clickRecordContinue(w);

    expect(mocks.apiMock.cancelContinuation).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledWith("t1", "f20");
    expect(session.anchorHistoric).toBe(false);
    w.unmount();
  });

  it("选中历史片段：不先取消（后端登记时替换旧意图）", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue(detailWith([HISTORY_FRAGMENT]));
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.setPendingContinuation({ intentId: "intent_A", sourceTitle: "历史片段 A" });

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickRecordContinue(w);

    expect(mocks.apiMock.cancelContinuation).not.toHaveBeenCalled();
    expect(mocks.apiMock.continueFromHistory).toHaveBeenCalledWith("t1", "f13");
    w.unmount();
  });

  it("取消失败：不改起点、选择保留、错误可见", async () => {
    mocks.apiMock.cancelContinuation.mockRejectedValueOnce(new Error("POST /api/anchor/continue/cancel -> 500"));
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.setPendingContinuation({ intentId: "intent_A", sourceTitle: "历史片段 A" });

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickOverviewContinue(w);

    expect(mocks.apiMock.setAnchor).not.toHaveBeenCalled();
    expect(session.pendingContinuation).toEqual({ intentId: "intent_A", sourceTitle: "历史片段 A" });
    expect(session.lastError).toContain("没能取消");
    // 错误在概览底部可见（不是只在 console / 只在 store 里）
    const error = w.find("[data-memory-overview] .mt-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("没能取消");
    w.unmount();
  });
});

describe("只浏览不取消用户的选择", () => {
  it("点列表里的另一个话题只是查看：不取消选择、不改起点", async () => {
    const pinia = newPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.setPendingContinuation({ intentId: "intent_A", sourceTitle: "历史片段 A" });

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    // t2 只是被打开来看（概览），没有点「继续讨论」
    await openOverview(w, "t2");

    expect(mocks.apiMock.cancelContinuation).not.toHaveBeenCalled();
    expect(mocks.apiMock.setAnchor).not.toHaveBeenCalled();
    expect(mocks.apiMock.continueFromHistory).not.toHaveBeenCalled();
    expect(session.pendingContinuation).toEqual({ intentId: "intent_A", sourceTitle: "历史片段 A" });
    w.unmount();
  });

  it("没有待落实的选择时：进入话题不产生取消请求（既有行为不变）", async () => {
    const pinia = newPinia();
    useSessionStore().currentTopicId = "t1";

    const w = mountView(pinia);
    await flushPromises();
    await openManagement(w);
    await openOverview(w);
    await clickOverviewContinue(w);

    expect(mocks.apiMock.cancelContinuation).not.toHaveBeenCalled();
    expect(mocks.apiMock.setAnchor).toHaveBeenCalledWith("t1", null);
    w.unmount();
  });
});
