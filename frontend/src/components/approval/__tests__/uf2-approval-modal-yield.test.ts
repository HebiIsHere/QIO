/**
 * uf2 · F01：中央弹窗让位不完整（B 组回归）。
 *
 * 已确认缺陷：先打开审批，再打开更高优先级的影响确认，仲裁活动项已切换，
 * 但 `.ua-modal` 仍可见（因为只看 hiddenBy()，它只识别 suspendAll）。
 *
 * 这里按真实顺序复现并锁定验收：
 * - 先审批后影响确认：审批被隐藏（v-show）、键盘（Esc/Tab）不生效；
 * - 高优先级关闭后：审批恢复可见，焦点回到审批弹窗内，当前申请/草稿不丢；
 * - 先影响确认后审批：审批不强行显示、也不丢申请；挡路窗口关闭后自动进入；
 * - 关闭询问挂起/取消：让位与恢复。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
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
import { useModalArbiterStore } from "../../../stores/modalArbiter";

function knowledge(id: string, content: string): KnowledgeApprovalItem {
  return {
    id,
    key: "knowledge:" + id,
    version: 1,
    content,
    category: "user_profile",
    scope: ["全局（你）"],
    epistemicStatus: "user_explicit",
    source: "主对话",
    evidenceIds: [],
    updatedAt: "2026-10-22T00:00:00+00:00",
    reason: "",
    arrivedAt: 1,
    detailStatus: "ready",
    detailError: "",
  };
}

let wrapper: VueWrapper | null = null;

function setup() {
  setActivePinia(createPinia());
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
    arbiter: useModalArbiterStore(),
  };
}

function mountModal() {
  wrapper = mount(UnifiedApprovalModal, { attachTo: document.body });
  return wrapper;
}

function displayOf(w: VueWrapper, selector: string): string {
  const element = w.find(selector).element as HTMLElement;
  return element.style.display;
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

describe("uf2-F01 先审批后影响确认：审批必须让位（隐藏、不可交互），关闭后恢复", () => {
  it("更高优先级影响确认打开：审批遮罩 v-show 隐藏、DOM/当前申请保留", async () => {
    const { unified, arbiter } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一")];
    unified.open("knowledge:kn_1");
    const w = mountModal();
    await flushPromises();
    expect(displayOf(w, ".ua-mask")).toBe("");
    expect(arbiter.active?.id).toBe("unified-approval");

    // 影响确认是更高优先级（40 > 30）：它占住唯一可见位置
    expect(arbiter.open({ id: "impact-1", kind: "impact" })).toBe(true);
    await flushPromises();

    // 缺陷复现点：此前这里仍然是可见的
    expect(displayOf(w, ".ua-mask")).toBe("none");
    expect(w.find(".ua-modal").exists()).toBe(true);
    expect(unified.visible).toBe(true);
    expect(unified.activeKey).toBe("knowledge:kn_1");
  });

  it("被隐藏的审批层不再处理键盘：Esc 不关闭、不抢更高层的按键", async () => {
    const { unified, arbiter } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一")];
    unified.open("knowledge:kn_1");
    const w = mountModal();
    await flushPromises();
    arbiter.open({ id: "impact-1", kind: "impact" });
    await flushPromises();

    await w.find(".ua-modal").trigger("keydown", { key: "Escape" });
    await flushPromises();
    expect(unified.visible).toBe(true);
    expect(arbiter.isOpen("unified-approval")).toBe(true);
  });

  it("更高优先级关闭后：审批恢复可见、焦点回到弹窗内、草稿与编辑态不丢", async () => {
    const { unified, arbiter } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一")];
    unified.open("knowledge:kn_1");
    const w = mountModal();
    await flushPromises();

    await w.find('[data-test="unified-approval-edit"]').trigger("click");
    await w.find("#knowledge-approval-draft").setValue("改了一半的内容");
    unified.updateDraft("改了一半的内容");
    expect(unified.editingKey).toBe("knowledge:kn_1");

    arbiter.open({ id: "impact-1", kind: "impact" });
    await flushPromises();
    expect(displayOf(w, ".ua-mask")).toBe("none");

    arbiter.close("impact-1");
    await flushPromises();

    expect(displayOf(w, ".ua-mask")).toBe("");
    expect(arbiter.active?.id).toBe("unified-approval");
    // 焦点重新进入活动层（不能留在页面外）
    expect(w.find(".ua-modal").element.contains(document.activeElement)).toBe(true);
    // 草稿与编辑态原样保留
    expect(unified.editingKey).toBe("knowledge:kn_1");
    expect(unified.draftFor("knowledge:kn_1")).toBe("改了一半的内容");
  });
});

describe("uf2-F01 先影响确认后审批：审批不强行显示、也不丢申请；挡路关闭后自动进入", () => {
  it("影响确认先开着：审批先不渲染；它关闭后审批自动成为活动层", async () => {
    const { unified, arbiter } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一")];
    expect(arbiter.open({ id: "impact-1", kind: "impact" })).toBe(true);

    expect(unified.open("knowledge:kn_1")).toBe(true);
    const w = mountModal();
    await flushPromises();

    // 不得强行显示：此刻没有审批的可交互 DOM
    expect(w.find(".ua-modal").exists()).toBe(false);
    expect(arbiter.isOpen("unified-approval")).toBe(false);
    // 申请与当前项不丢
    expect(unified.visible).toBe(true);
    expect(unified.activeKey).toBe("knowledge:kn_1");

    arbiter.close("impact-1");
    await flushPromises();

    expect(arbiter.active?.id).toBe("unified-approval");
    expect(w.find(".ua-modal").exists()).toBe(true);
    expect(displayOf(w, ".ua-mask")).toBe("");
    expect(w.find(".ua-modal").element.contains(document.activeElement)).toBe(true);
  });
});

describe("uf2-F01 关闭询问挂起/取消：让位后恢复原上下文", () => {
  it("suspendAll 隐藏审批；resumeAll 恢复可见与焦点", async () => {
    const { unified, arbiter } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一")];
    unified.open("knowledge:kn_1");
    const w = mountModal();
    await flushPromises();
    expect(displayOf(w, ".ua-mask")).toBe("");

    arbiter.suspendAll("close-ask");
    await flushPromises();
    expect(displayOf(w, ".ua-mask")).toBe("none");
    expect(unified.activeKey).toBe("knowledge:kn_1");

    arbiter.resumeAll();
    await flushPromises();
    expect(displayOf(w, ".ua-mask")).toBe("");
    expect(w.find(".ua-modal").element.contains(document.activeElement)).toBe(true);
  });
});
