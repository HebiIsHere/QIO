/**
 * E 独立验收 · 聊天页用户可见面（阶段 1 基线复现 / 阶段 2 候选复验）
 *
 * 验收口径（COMMON §0 / §4.1 / §4.2）：
 * 1. 权限模式：对话页与互动页都常驻可见；读取中 / 读取失败如实显示。
 * 2. 执行结果与历史：两个聊天路由在「待审批为 0」时仍能进入并看到真实历史；
 *    终态结果不随几秒提示消失，两个审批的结果按 approval_id 各自保留。
 * 3. pending 恢复失败必须可见且可重试，不能伪装成「没有待审批」。
 * 4. 已批准 / 等待执行不得显示成已完成。
 *
 * 这些用例只断言**用户能看到的 DOM**（按钮 / 文本 / 可操作元素）；
 * store 只用于把场景摆到用户会遇到的真实状态，不作为结论依据。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

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
  loadChatHistory: vi.fn(),
  getUISettings: vi.fn(),
  respondApproval: vi.fn(),
  getPendingApprovals: vi.fn(),
  getApprovalHistory: vi.fn(),
}));

vi.mock("../../services/interactive", () => mocks.service);
vi.mock("../../services/api", () => ({
  request: mocks.request,
  api: {
    loadHistory: mocks.loadChatHistory,
    getUISettings: mocks.getUISettings,
    respondApproval: mocks.respondApproval,
  },
}));
vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    getPendingApprovals: mocks.getPendingApprovals,
    getApprovalHistory: mocks.getApprovalHistory,
  };
});

import InteractiveView from "../../views/InteractiveView.vue";
import ConversationView from "../../views/ConversationView.vue";
import UnifiedApprovalModal from "../../components/approval/UnifiedApprovalModal.vue";
import { useApprovalsStore, type ApprovalItem } from "../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";
import { useSecurityStore } from "../../stores/security";
import { useInteractiveStore } from "../../stores/interactive";
import { VIEW_STUBS } from "../../views/__tests__/i2v2-a-harness";

const STAMP = "2026-10-26T00:00:00.000Z";
const mounted: VueWrapper[] = [];

function track<T extends VueWrapper>(wrapper: T): T {
  mounted.push(wrapper);
  return wrapper;
}

function mountInteractive(): VueWrapper {
  return track(mount(InteractiveView, { attachTo: document.body, global: { stubs: VIEW_STUBS as never } }));
}

const composerStub = {
  name: "Composer",
  template: `<div class="stub-composer">
    <slot name="entries"></slot>
    <slot name="leading"></slot>
    <textarea id="composer-input"></textarea>
  </div>`,
};
const dockStub = {
  name: "PlanetDock",
  emits: ["open"],
  template: `<button class="stub-dock" @click="$emit('open')">dock</button>`,
};
const planetStub = {
  name: "PlanetView",
  props: { seq: { type: Number, default: 0 }, open: { type: Boolean, default: true } },
  template: `<div class="stub-planet" :data-open="String(open)"></div>`,
};

function mountConversation(): VueWrapper {
  return track(
    mount(ConversationView, {
      attachTo: document.body,
      global: {
        stubs: {
          MessageStream: true,
          Composer: composerStub,
          SettingsFloat: true,
          PlanetDock: dockStub,
          PlanetView: planetStub,
        },
      },
    }),
  );
}

/** 执行结果历史的可操作入口（文案或无障碍名包含「执行结果 / 审批结果 / 结果与历史」等真实说法）。 */
const HISTORY_LABEL = /(执行结果历史|审批结果与历史|结果与历史|审批历史|执行历史|审批结果|执行结果)/;

function findOpenableHistoryEntry(wrapper: VueWrapper): HTMLElement | null {
  const nodes = Array.from(wrapper.element.querySelectorAll<HTMLElement>("*"));
  for (const el of nodes) {
    const attrs = ["aria-label", "title", "data-test", "data-im"]
      .map((name) => el.getAttribute(name) ?? "")
      .join(" ");
    const directText = Array.from(el.childNodes)
      .filter((node) => node.nodeType === 3)
      .map((node) => node.textContent ?? "")
      .join(" ")
      .trim();
    const isControl =
      el.tagName === "BUTTON" ||
      el.tagName === "A" ||
      el.tagName === "SUMMARY" ||
      el.getAttribute("role") === "button" ||
      el.getAttribute("role") === "tab";
    if (isControl && HISTORY_LABEL.test(attrs + " " + directText)) return el;
    if (!isControl && el.hasAttribute("data-test") && HISTORY_LABEL.test(attrs + " " + directText)) {
      return el;
    }
  }
  return null;
}

async function openHistoryIfNeeded(wrapper: VueWrapper): Promise<HTMLElement | null> {
  const entry = findOpenableHistoryEntry(wrapper);
  if (!entry) return null;
  if (entry.tagName === "BUTTON" || entry.tagName === "A" || entry.tagName === "SUMMARY" || entry.getAttribute("role") === "button") {
    entry.click();
    await flushPromises();
    await nextTick();
    await flushPromises();
  }
  return entry;
}

function approvalItem(over: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    approval_id: "appr_e1",
    kind: "tool_create",
    payload: {
      name: "子智能体：整理材料",
      tool_type: "subagent",
      subagent_budget: { max_iterations: 5, max_tokens: 100000, output_limit_chars: 2000 },
    },
    turnId: null,
    sessionId: null,
    requestDigest: null,
    state: "waiting",
    label: "创建子智能体",
    purpose: "把两份材料整理成发布清单",
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  localStorage.clear();
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });

  const api = mocks.service;
  api.fetchBoardState.mockResolvedValue({
    board: { id: "board_default", title: "互动板面", seq: 1 },
    seq: 1,
    state: { boardId: "board_default", seq: 1, cards: [], groups: [], links: [], updatedAt: STAMP },
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
  mocks.loadChatHistory.mockResolvedValue({ messages: [], anchor: null });
  mocks.getUISettings.mockResolvedValue({ typewriter_cps: 50 });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  mocks.getPendingApprovals.mockResolvedValue([]);
  mocks.getApprovalHistory.mockResolvedValue([]);
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// F01 / §4.2 权限模式：两个聊天页都要常驻可见
// ---------------------------------------------------------------------------
describe("权限模式：两个聊天路由都常驻可见（基线：互动页 v2 布局没有徽标）", () => {
  it("互动页有权限模式徽标，且收起 / 展开对话都在", async () => {
    const security = useSecurityStore();
    security.mode = "read_only";
    security.label = "只读模式";
    security.loaded = true;
    const store = useInteractiveStore();

    const w = mountInteractive();
    await flushPromises();
    const badges = () => w.findAll('[data-im="permission-mode-badge"]');
    expect(badges().length, "互动页缺少权限模式徽标（真实页面上用户看不到当前模式）").toBeGreaterThan(0);
    expect(badges()[0].text()).toContain("只读");

    store.chatOpen = true;
    await nextTick();
    expect(badges().length, "对话展开后徽标被遮掉 / 卸载了").toBeGreaterThan(0);
    store.chatOpen = false;
    await nextTick();
    expect(badges().length, "对话收起后徽标消失了（应当常驻）").toBeGreaterThan(0);
  });

  it("互动页读取中 / 读取失败时如实显示，不冒充默认模式", async () => {
    const security = useSecurityStore();
    security.loaded = false;
    security.error = null;
    const w = mountInteractive();
    await flushPromises();
    const text = w.text();
    expect(text, "读取中时互动页没有任何权限模式提示").toContain("权限模式读取中");

    security.error = "读取权限模式失败：HTTP 500";
    await nextTick();
    expect(w.text(), "读取失败时互动页没有说明").toContain("权限模式未知");
  });

  it("对照：对话页本来就有权限模式徽标", async () => {
    const security = useSecurityStore();
    security.mode = "restricted";
    security.label = "受限模式";
    security.loaded = true;
    const w = mountConversation();
    await flushPromises();
    expect(w.findAll('[data-im="permission-mode-badge"]').length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// F02/F04 §4.1 执行结果与历史：0 待办仍可达、结果可回看
// ---------------------------------------------------------------------------
describe("执行结果与历史：待审批为 0 时仍能进入（基线：历史只在被门控掉的旧 ApprovalModal 里）", () => {
  const rows = [
    { approval_id: "appr_hist_a", outcome: "executed", summary: "已执行：写入 2 个文件", at: STAMP },
    { approval_id: "appr_hist_b", outcome: "failed", summary: "执行失败：目标文件被占用", at: STAMP },
  ];

  it("互动页：0 待审批仍有历史入口，打开后能看到两条真实结果（按 approval_id 分开）", async () => {
    mocks.getApprovalHistory.mockResolvedValue(rows);
    const approvals = useApprovalsStore();
    expect(approvals.queue.length).toBe(0);
    const w = mountInteractive();
    await flushPromises();

    const entry = await openHistoryIfNeeded(w);
    expect(entry, "互动页没有可打开的「执行结果 / 审批历史」入口（0 待办时历史不可达）").not.toBeNull();
    const text = w.text();
    expect(text, "历史里看不到已执行结果").toContain("已执行：写入 2 个文件");
    expect(text, "历史里看不到失败结果").toContain("执行失败：目标文件被占用");
  });

  it("对话页：0 待审批仍有历史入口，打开后能看到两条真实结果", async () => {
    mocks.getApprovalHistory.mockResolvedValue(rows);
    const w = mountConversation();
    await flushPromises();

    const entry = await openHistoryIfNeeded(w);
    expect(entry, "对话页没有可打开的「执行结果 / 审批历史」入口（历史不可达）").not.toBeNull();
    const text = w.text();
    expect(text).toContain("已执行：写入 2 个文件");
    expect(text).toContain("执行失败：目标文件被占用");
  });

  it("批准后要等执行：已批准显示为「等待执行」，不显示成已完成", async () => {
    setActivePinia(createPinia());
    const approvals = useApprovalsStore();
    const unified = useUnifiedApprovalsStore();
    approvals.queue = [approvalItem({ state: "approved" })];
    unified.noteQueueChanged();
    unified.open();
    const w = track(mount(UnifiedApprovalModal));
    await nextTick();

    expect(w.text()).toContain("已批准");
    expect(w.text()).toContain("等待执行");
    expect(w.text(), "把「已批准、等待执行」说成了已完成").not.toContain("已完成");
    expect(w.find('[data-test="unified-approval-allow"]').exists(), "已批准后还留着批准按钮").toBe(false);
  });

  it("终态结果不随弹窗消失：两个审批按 approval_id 各自保留，打开历史仍能看到", async () => {
    setActivePinia(createPinia());
    const approvals = useApprovalsStore();
    const w = mountInteractive();
    await flushPromises();
    mocks.getApprovalHistory.mockResolvedValue([
      { approval_id: "appr_a", outcome: "executed", summary: "审批 A 已执行：写了 3 个文件", at: STAMP },
      { approval_id: "appr_b", outcome: "failed", summary: "审批 B 执行失败：磁盘只读", at: STAMP },
    ]);

    approvals.recordResult({ approval_id: "appr_b", phase: "failed", ok: false, summary: "审批 B 执行失败：磁盘只读" });
    approvals.recordResult({ approval_id: "appr_a", phase: "executed", ok: true, summary: "审批 A 已执行：写了 3 个文件" });
    await flushPromises();
    await nextTick();

    const entry = await openHistoryIfNeeded(w);
    expect(entry, "审批结束后没有任何可回看的结果入口（结果随提示消失）").not.toBeNull();
    const text = w.text();
    expect(text, "并发两个审批的结果互相覆盖 / 丢失（缺少 A）").toContain("审批 A 已执行");
    expect(text, "并发两个审批的结果互相覆盖 / 丢失（缺少 B）").toContain("审批 B 执行失败");
  });
});

// ---------------------------------------------------------------------------
// §4.1 恢复失败必须可见、可重试；不能伪装成空
// ---------------------------------------------------------------------------
describe("pending 恢复失败：可见错误 + 重试（基线：只写进 store，界面无任何提示）", () => {
  /**
   * 恢复失败的可见性是「入口可发现 + 打开后能读原因并重试」两段：
   * ⑤ 审批入口的无障碍名必须写明读取失败（用户能发现），打开后必须给出原因与重试
   * （A 的实现放在「结果与历史」面板里，与「历史读取失败」分开说明）。
   */
  async function openApprovalsEntry(wrapper: VueWrapper): Promise<void> {
    const entry = wrapper.find('[data-entry="approvals"]');
    expect(entry.exists(), "恢复失败后连审批入口都没有（用户无从发现）").toBe(true);
    const label = entry.attributes("aria-label") ?? "";
    expect(label, "审批入口没有说明「待审批列表读取失败」，用户看不出异常").toContain("失败");
    await entry.trigger("click");
    await flushPromises();
    await nextTick();
    await flushPromises();
  }

  function assertFailureSurface(wrapper: VueWrapper, where: string): void {
    const alerts = Array.from(wrapper.element.querySelectorAll('[role="alert"]')).map((el) => el.textContent ?? "").join(" ");
    expect(alerts, where + "：打开入口后看不到恢复失败原因").toContain("恢复待审批项失败");
    const retry = Array.from(wrapper.element.querySelectorAll("button")).filter((btn) => /重试/.test(btn.textContent ?? ""));
    expect(retry.length, where + "：恢复失败没有可点的重试入口").toBeGreaterThan(0);
  }

  it("互动页：恢复失败在审批入口可见，打开后能读原因并重试，不显示成「没有待审批」", async () => {
    mocks.getPendingApprovals.mockRejectedValue(new Error("HTTP 500：服务端内部错误"));
    const approvals = useApprovalsStore();
    await approvals.recoverPending();
    expect(approvals.recoveryError, "场景准备失败：store 没有记录恢复失败").toBeTruthy();

    const w = mountInteractive();
    await flushPromises();
    await openApprovalsEntry(w);
    assertFailureSurface(w, "互动页");
  });

  it("对话页：恢复失败在审批入口可见，打开后能读原因并重试", async () => {
    mocks.getPendingApprovals.mockRejectedValue(new Error("HTTP 500：服务端内部错误"));
    const approvals = useApprovalsStore();
    await approvals.recoverPending();

    const w = mountConversation();
    await flushPromises();
    await openApprovalsEntry(w);
    assertFailureSurface(w, "对话页");
  });
});
