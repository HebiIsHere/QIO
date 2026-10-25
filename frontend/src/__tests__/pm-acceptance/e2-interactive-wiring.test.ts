/**
 * E 独立验收 · 互动页接线（阶段 1 基线复现 / 阶段 2 候选复验）
 *
 * 1. 对话窗显示框桥（COMMON §4.3）：v2 互动对话窗是**唯一生产者**；
 *    展开 / 调高要发布真实显示框，收起 / 卸载发布 null（不留幽灵矩形）。
 *    基线：生产者仍只在未挂载的旧 ChatDock 里，互动页 chatObstacleFrame() 永远是 null，
 *    卡片的浮层避让拿不到对话窗位置。
 * 2. 提交区批量入口（COMMON §4.4）：按钮必须打开真实任务面板的待确认分区。
 * 3. 旧能力迁移表（BRIEF §1.9）：MIGRATED_CAPABILITIES 的 status 必须与真实实现一致。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h, nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  service: {
    fetchBoardState: vi.fn(),
    fetchVisibleRange: vi.fn(),
    fetchIntents: vi.fn(),
    fetchIntentViews: vi.fn(),
    fetchUnderstanding: vi.fn(),
    fetchExecutions: vi.fn(),
    fetchDrafts: vi.fn(),
    saveBoardState: vi.fn(),
    saveDrafts: vi.fn(),
    checkMaterialImpact: vi.fn(),
    previewMaterialImpact: vi.fn(),
    submitBoard: vi.fn(),
    fetchAcceptance: vi.fn(),
    createIntents: vi.fn(),
    createDemoIntents: vi.fn(),
    approveIntent: vi.fn(),
    rejectIntent: vi.fn(),
    batchDecide: vi.fn(),
    updateIntentPreview: vi.fn(),
    advanceIntent: vi.fn(),
    pauseIntent: vi.fn(),
    resumeIntent: vi.fn(),
    cancelIntent: vi.fn(),
    retryUnderstanding: vi.fn(),
    saveIntentDisplay: vi.fn(),
  },
  request: vi.fn(),
}));

vi.mock("../../services/interactive", () => mocks.service);
vi.mock("../../services/api", () => ({
  request: mocks.request,
  api: { loadHistory: vi.fn(), getUISettings: vi.fn(), respondApproval: vi.fn() },
}));

import InteractiveView from "../../views/InteractiveView.vue";
import InteractiveConversation from "../../components/interactive/v2/InteractiveConversation.vue";
import { useInteractiveStore } from "../../stores/interactive";
import { useSessionStore } from "../../stores/session";
import { VIEW_STUBS } from "../../views/__tests__/i2v2-a-harness";
import { chatObstacleFrame, readChatFrame, resetChatFrameState } from "../../interactive/chatFrameState";
import { MIGRATED_CAPABILITIES } from "../../components/interactive/v2/intentDisplay";
import type { BoardCard, Intent, Understanding } from "../../interactive/types";

const STAMP = "2026-10-26T00:00:00.000Z";
const mounted: VueWrapper[] = [];
function track<T extends VueWrapper>(wrapper: T): T {
  mounted.push(wrapper);
  return wrapper;
}

/** jsdom 没有布局：给需要「量到真实矩形」的场景一个非零、可预期的框。 */
let originalRect: typeof Element.prototype.getBoundingClientRect | null = null;
function installRect(box: { left: number; top: number; width: number; height: number }): void {
  originalRect = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element): DOMRect {
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

const StreamStub = defineComponent({
  name: "MessageStream",
  setup() {
    return () => h("div", { "data-stub": "stream" });
  },
});
const EntriesStub = defineComponent({
  name: "ComposerEntries",
  setup() {
    return () => h("div", { "data-stub": "entries" });
  },
});
const ComposerStub = defineComponent({
  name: "Composer",
  setup() {
    return () => h("div", { "data-stub": "composer" });
  },
});

function mountConversationWindow(props: Record<string, unknown>): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = "A";
  return track(
    mount(InteractiveConversation, {
      props,
      global: {
        plugins: [pinia],
        stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
      },
      attachTo: document.body,
    }),
  );
}

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
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function intentFixture(over: Partial<Intent> = {}): Intent {
  return {
    id: "i1",
    boardId: "board_t",
    submissionId: "sub_batch",
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
    createdAt: STAMP,
    updatedAt: STAMP,
    ...over,
  };
}

function understandingFixture(): Understanding {
  return {
    id: "u1",
    boardId: "board_t",
    submissionId: "sub_batch",
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
    intentIds: ["i1", "i2", "i3", "i4"],
    memory: [],
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  localStorage.clear();
  resetChatFrameState();
  Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true, writable: true });
  Object.defineProperty(window, "innerHeight", { value: 768, configurable: true, writable: true });

  const api = mocks.service;
  api.fetchBoardState.mockResolvedValue({
    board: { id: "board_t", title: "互动板面", seq: 1 },
    seq: 1,
    state: { boardId: "board_t", seq: 1, cards: [], groups: [], links: [], updatedAt: STAMP },
    submissions: [],
    drafts: { drafts: {}, updatedAt: null, rev: 1, keyVersions: {} },
  });
  api.fetchVisibleRange.mockResolvedValue({ visibleRange: { cardIds: [], groupIds: [] } });
  api.fetchIntents.mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } });
  api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [] });
  api.fetchUnderstanding.mockResolvedValue({ understandings: [] });
  api.fetchExecutions.mockResolvedValue({ executions: [] });
  api.fetchDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.saveDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.saveBoardState.mockResolvedValue({ ok: true, seq: 2, savedAt: STAMP });
  api.checkMaterialImpact.mockResolvedValue({ ok: true, affected: [], impactConfirmationRequired: false });
  api.fetchAcceptance.mockResolvedValue({ acceptances: [] });
  api.saveIntentDisplay.mockResolvedValue({ ok: true });
  mocks.request.mockResolvedValue({ items: [] });
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  resetChatFrameState();
  if (originalRect) Element.prototype.getBoundingClientRect = originalRect;
  originalRect = null;
});

// ---------------------------------------------------------------------------
// §4.3 互动几何桥：v2 对话窗必须是真实生产者
// ---------------------------------------------------------------------------
describe("互动对话窗显示框桥（基线：只有未挂载的 ChatDock 会发布，互动页读到 null）", () => {
  it("展开时发布非零的真实显示框（卡片避让才有位置可用）", async () => {
    installRect({ left: 240, top: 320, width: 420, height: 360 });
    mountConversationWindow({ expanded: true });
    await flushPromises();
    await nextTick();
    window.dispatchEvent(new Event("resize"));
    await new Promise((done) => setTimeout(done, 120));

    const frame = readChatFrame();
    expect(frame, "互动页的对话窗没有发布显示框：chatObstacleFrame() 永远是 null").not.toBeNull();
    expect(frame!.w, "发布的显示框宽度为 0，下游无法用它避让").toBeGreaterThan(0);
    expect(frame!.h, "发布的显示框高度为 0").toBeGreaterThan(0);
  });

  it("收起后不留幽灵矩形：chatObstacleFrame() 返回 null，显示框同步清空", async () => {
    installRect({ left: 240, top: 320, width: 420, height: 360 });
    const w = mountConversationWindow({ expanded: true });
    await flushPromises();
    window.dispatchEvent(new Event("resize"));
    await new Promise((done) => setTimeout(done, 80));
    expect(readChatFrame(), "前提不成立：展开时就没有发布显示框").not.toBeNull();

    await w.setProps({ expanded: false });
    await nextTick();
    window.dispatchEvent(new Event("resize"));
    await new Promise((done) => setTimeout(done, 80));

    expect(chatObstacleFrame(), "收起后仍把对话窗当作障碍（幽灵矩形）").toBeNull();
    expect(readChatFrame(), "收起后显示框没有清空").toBeNull();
  });

  it("卸载后清空显示框，不给下一个页面留状态", async () => {
    installRect({ left: 240, top: 320, width: 420, height: 360 });
    const w = mountConversationWindow({ expanded: true });
    await flushPromises();
    window.dispatchEvent(new Event("resize"));
    await new Promise((done) => setTimeout(done, 80));
    w.unmount();
    mounted.pop();
    await nextTick();
    expect(readChatFrame()).toBeNull();
  });

  it("未展开时不发布显示框（收起状态不占障碍空间）", async () => {
    installRect({ left: 240, top: 320, width: 420, height: 360 });
    mountConversationWindow({ expanded: false });
    await flushPromises();
    window.dispatchEvent(new Event("resize"));
    await new Promise((done) => setTimeout(done, 80));
    expect(readChatFrame()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// §4.4 提交区批量入口：必须打开真实任务面板
// ---------------------------------------------------------------------------
describe("提交区批量入口（基线注释声称已修；本轮独立复核是否真的打开可见界面）", () => {
  it("点「打开批量审批」后任务面板的待确认分区可见，同批项目真实列出", async () => {
    // 场景必须来自页面自己的加载（服务端快照），不能在加载完成后再改 store：
    // 真实页面每次加载都会用服务端事实覆盖本地状态，测试若绕开这条链路，测到的就不是用户路径。
    const intents = ["i1", "i2", "i3", "i4"].map((id) =>
      intentFixture({ id, status: "pending", understandingId: "u1" }),
    );
    mocks.service.fetchIntents.mockResolvedValue({
      intents,
      conflicts: [],
      batchAvailable: true,
      recovery: { paused: [] },
    });
    mocks.service.fetchUnderstanding.mockResolvedValue({ understandings: [understandingFixture()] });

    // 批量入口在真实工具栏里的提交区内：不能把 BoardToolbar 换成替身，否则测的不是用户看到的入口
    const stubs: Record<string, unknown> = { ...VIEW_STUBS };
    delete stubs.BoardToolbar;
    const w = track(mount(InteractiveView, { attachTo: document.body, global: { stubs: stubs as never } }));
    await flushPromises();
    const store = useInteractiveStore();
    store.submitStatus = "succeeded";
    await nextTick();

    const button = w.find('[data-im="submit-open-batch"]');
    expect(button.exists(), "提交区没有批量入口（同批 4 项待审批）").toBe(true);
    await button.trigger("click");
    await nextTick();

    expect(store.tasksOpen, "点了批量入口但任务面板没有打开（只改了内部开关）").toBe(true);
    expect(
      w.find('[data-im="task-pending"]').exists(),
      "任务面板的待确认分区不可见：批量入口没有落到真实界面（taskRows=" + store.taskRows.length + "）",
    ).toBe(true);
    const rows = w.findAll('[data-im="task-pending-row"]');
    expect(rows.length, "任务面板没有列出这一批的待审批项目").toBeGreaterThan(0);
    // 旧托盘不再挂载：界面只能来自新面板
    expect(w.findComponent({ name: "IntentBatchTray" }).exists()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// BRIEF §1.9 迁移表与真实实现的一致性
// ---------------------------------------------------------------------------
describe("MIGRATED_CAPABILITIES 与实际实现一致（基线：有的能力已落地却仍标 needs-backend / kept-legacy）", () => {
  it("暂停 / 继续 / 取消执行已在新任务面板落地，不能再标 needs-backend", () => {
    const entry = MIGRATED_CAPABILITIES.find((item) => /暂停/.test(item.capability) && /继续/.test(item.capability));
    expect(entry, "迁移表里没有暂停 / 继续 / 取消条目").toBeTruthy();
    expect(
      entry!.status,
      "暂停 / 继续 / 取消已有后端接口与新版任务面板入口，迁移表却仍说「需要后端」（与真实不符）",
    ).toBe("migrated");
  });

  it("批次列表 / 同批批准 / 拒绝已由新任务面板承接，不能仍只说 kept-legacy", () => {
    const entry = MIGRATED_CAPABILITIES.find((item) => /批次列表/.test(item.capability));
    expect(entry, "迁移表里没有批次列表条目").toBeTruthy();
    expect(
      entry!.status,
      "批次列表与同批批准已在任务面板真实可用，迁移表仍标为旧链保留（误导后续删除判断）",
    ).toBe("migrated");
  });
});