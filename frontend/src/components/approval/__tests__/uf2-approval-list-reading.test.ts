/**
 * uf2 · F09：返回审批列表时恢复真实阅读位置（B 组回归）。
 *
 * 已实测缺陷：28 条申请，列表滚到 scrollTop=500，打开中间一项并处理，返回列表变成 scrollTop=0
 * （详情用 v-if 替换列表，DOM 被销毁；回来时重新创建的列表没有恢复位置）。
 *
 * 验收（真实 DOM scrollTop，不用 store 状态冒充）：
 * - 返回列表恢复真实 scrollTop 与锚点偏移；
 * - 原条目已处理移除时恢复到合理相邻项，焦点也落在相邻项；
 * - 期间插入新申请/条数变化不会把用户无故送回顶部；
 * - 处理后仍是「列表态」，不自动进入下一条详情。
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({ request: vi.fn(), respondApproval: vi.fn() }));
vi.mock("../../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval },
}));

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import {
  useUnifiedApprovalsStore,
  type KnowledgeApprovalItem,
} from "../../../stores/unifiedApprovals";

let wrapper: VueWrapper | null = null;

const ROW_HEIGHT = 40;
const LIST_TOP = 24;

/**
 * jsdom 不做布局：用「行高 + 在列表里的真实顺序」模拟 offsetTop，
 * 这样组件恢复滚动位置时读到的是像浏览器一样的锚点几何信息，
 * 而 scrollTop 本身是 jsdom 真实可读回的 DOM 属性（不是替身），验收的仍是真实 DOM 值。
 */
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetTop", {
    configurable: true,
    get(this: HTMLElement) {
      const key = this.dataset?.approvalKey;
      if (!key || !this.parentElement) return 0;
      const index = Array.from(this.parentElement.children).indexOf(this);
      return LIST_TOP + index * ROW_HEIGHT;
    },
  });
});

function knowledge(id: string, content: string): KnowledgeApprovalItem {
  return {
    id,
    key: "knowledge:" + id,
    version: 1,
    content,
    category: "user_profile",
    scope: [],
    epistemicStatus: null,
    source: "主对话",
    evidenceIds: [],
    updatedAt: "2026-10-22T00:00:00+00:00",
    reason: "",
    arrivedAt: 1,
    detailStatus: "ready",
    detailError: "",
  };
}

function setupWithTools(count: number) {
  setActivePinia(createPinia());
  const approvals = useApprovalsStore();
  const unified = useUnifiedApprovalsStore();
  for (let i = 0; i < count; i += 1) {
    approvals.enqueue("appr_" + i, "tool_execution", { description: "执行命令 " + i }, { autoOpen: true });
  }
  // 列表态（与 finishHandled 后的状态一致）：弹窗打开、没有选中项
  unified.visible = true;
  unified.queueListOnly = true;
  unified.activeKey = null;
  return { approvals, unified };
}

function setupWithKnowledge(count: number) {
  setActivePinia(createPinia());
  const approvals = useApprovalsStore();
  const unified = useUnifiedApprovalsStore();
  unified.knowledge = Array.from({ length: count }, (_, i) => knowledge("kn_" + i, "知识 " + i));
  unified.visible = true;
  unified.queueListOnly = true;
  unified.activeKey = null;
  return { approvals, unified };
}

function mountModal() {
  wrapper = mount(UnifiedApprovalModal, { attachTo: document.body });
  return wrapper;
}

function listEl(w: VueWrapper): HTMLElement {
  return w.find(".ua-list").element as HTMLElement;
}

function rows(w: VueWrapper) {
  return w.findAll('[data-test="unified-approval-list-item"]');
}

async function settle() {
  await nextTick();
  await nextTick();
  await nextTick();
}

beforeEach(() => {
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时只影响跨用例的暂缓键
  }
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("uf2-F09 返回列表恢复真实滚动位置", () => {
  it("滚到 scrollTop=500 打开中间一项并处理：回到列表仍是 500（不是 0），焦点落在相邻项", async () => {
    const { approvals, unified } = setupWithTools(28);
    const w = mountModal();
    await settle();
    expect(rows(w)).toHaveLength(28);

    const list = listEl(w);
    list.scrollTop = 500;
    expect(list.scrollTop).toBe(500);

    // 打开第 16 项（下标 15）：详情替换列表
    await rows(w)[15].trigger("click");
    await settle();
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
    expect(w.find(".ua-list").exists()).toBe(false);

    // 处理完这一项：回到列表
    approvals.resolve("appr_15");
    unified.finishHandled("tool:appr_15");
    await settle();

    expect(unified.queueListOnly).toBe(true);
    expect(unified.activeKey).toBeNull();
    // 不自动进入下一条详情（不连弹）
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
    expect(rows(w)).toHaveLength(27);
    // 真实 DOM scrollTop 恢复（缺陷点：此前是 0）
    expect(listEl(w).scrollTop).toBe(500);
    // 焦点落在合理相邻项（原条目已移除 → 同位置的接班项）
    const focused = document.activeElement as HTMLElement | null;
    expect(focused?.dataset.approvalKey).toBe("tool:appr_16");
  });

  it("原条目仍存在（期间处理的是别的申请）：恢复真实滚动位置并把焦点放回原条目", async () => {
    const { approvals, unified } = setupWithTools(28);
    const w = mountModal();
    await settle();
    listEl(w).scrollTop = 500;

    await rows(w)[15].trigger("click");
    await settle();
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);

    // 期间处理的是排在前面的另一条（不在当前阅读锚点上）
    approvals.resolve("appr_0");
    unified.finishHandled("tool:appr_0");
    await settle();

    expect(unified.queueListOnly).toBe(true);
    const list = listEl(w);
    // 锚点整体上移一行：真实 scrollTop 按锚点偏移换算（500 + (新锚点顶 - 旧锚点顶)）
    expect(list.scrollTop).toBe(500 - ROW_HEIGHT);
    const focused = document.activeElement as HTMLElement | null;
    expect(focused?.dataset.approvalKey).toBe("tool:appr_15");
  });

  it("期间插入新申请导致条数/顺序变化：不把用户无故送回顶部", async () => {
    const { approvals, unified } = setupWithKnowledge(28);
    const w = mountModal();
    await settle();
    expect(rows(w)).toHaveLength(28);
    listEl(w).scrollTop = 500;

    await rows(w)[15].trigger("click");
    await settle();
    expect(unified.activeKey).toBe("knowledge:kn_15");

    // 处理这一项，同时有新的工具审批插到队列前面（知识项整体下移一行）
    unified.removeKnowledge("kn_15");
    unified.finishHandled("knowledge:kn_15");
    approvals.enqueue("appr_new", "tool_execution", { description: "新到达" }, { autoOpen: true });
    await settle();

    expect(unified.queueListOnly).toBe(true);
    expect(rows(w)).toHaveLength(28);
    // 邻居（kn_16）现在位于原锚点位置 + 1 行：恢复位置按锚点偏移换算，不是回到 0
    expect(listEl(w).scrollTop).toBe(500 + ROW_HEIGHT);
    expect(listEl(w).scrollTop).toBeGreaterThan(0);
  });
});
