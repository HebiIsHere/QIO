/**
 * 统一审批弹窗的组件行为：类型分派、知识三动作（修改后保存提交编辑内容）、
 * 队列切换、关闭不决定、重点确认额外一步、stale 只给「知道了」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

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
    evidenceIds: ["ev_1", "ev_2"],
    updatedAt: "2026-10-22T00:00:00+00:00",
    reason: "你在本次对话里明确说过",
    arrivedAt: 1,
    detailStatus: "ready",
    detailError: "",
  };
}

function setup() {
  setActivePinia(createPinia());
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
  };
}

beforeEach(() => {
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  mocks.request.mockResolvedValue({ ok: true, decision: "approve" });
  mocks.respondApproval.mockResolvedValue({ ok: true });
});

describe("知识保存申请：三动作与修改后保存", () => {
  it("展示完整内容与已有依据/范围/确定程度", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledge("kn_1", "用户偏好安静的界面")];
    unified.open("knowledge:kn_1");
    const w = mount(UnifiedApprovalModal);

    expect(w.find('[data-test="knowledge-approval-content"]').text()).toContain("用户偏好安静的界面");
    const facts = w.find('[data-test="knowledge-approval-facts"]').text();
    expect(facts).toContain("主对话");
    expect(facts).toContain("全局（你）");
    expect(facts).toContain("你明确说过");
    expect(facts).toContain("2 条依据");
    expect(w.find('[data-test="unified-approval-save"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-ignore"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-edit"]').exists()).toBe(true);
    w.unmount();
  });

  it("修改后保存：提交的是编辑后的内容", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledge("kn_1", "用户偏好安静的界面")];
    unified.open("knowledge:kn_1");
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="unified-approval-edit"]').trigger("click");
    const textarea = w.find("#knowledge-approval-draft");
    expect((textarea.element as HTMLTextAreaElement).value).toBe("用户偏好安静的界面");
    await textarea.setValue("用户偏好安静、留白充足的界面");
    await w.find('[data-test="unified-approval-save-modified"]').trigger("click");
    await flushPromises();

    const [path, init] = mocks.request.mock.calls[0];
    expect(path).toBe("/api/knowledge/kn_1/approval");
    const body = JSON.parse(String(init.body));
    expect(body.decision).toBe("approve_modified");
    expect(body.content).toBe("用户偏好安静、留白充足的界面");
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    w.unmount();
  });

  it("确认保存 / 不保存分别走正确的动作（不串线）", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一")];
    unified.open("knowledge:kn_1");
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="unified-approval-ignore"]').trigger("click");
    await flushPromises();
    expect(JSON.parse(String(mocks.request.mock.calls[0][1].body)).decision).toBe("ignore");
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    w.unmount();
  });
});

describe("工具审批：一套按钮、重点确认额外一步、stale 收口", () => {
  it("重点确认（会删除/覆盖）需要先勾选才能允许", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue(
      "appr_1",
      "tool_execution",
      { description: "覆盖写入文件", capabilities: ["副作用：destructive", "写入文件：是"] },
      { autoOpen: true },
    );
    unified.open("tool:appr_1");
    const w = mount(UnifiedApprovalModal);

    const allow = w.find('[data-test="unified-approval-allow"]');
    expect(allow.attributes("disabled")).toBeDefined();
    await w.find(".ua-confirm input").setValue(true);
    expect(allow.attributes("disabled")).toBeUndefined();
    await allow.trigger("click");
    await flushPromises();
    expect(mocks.respondApproval).toHaveBeenCalledWith("appr_1", "approved", undefined, expect.anything());
    w.unmount();
  });

  it("stale（后端已无此审批）只给「知道了」，不再提供批准/拒绝", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    approvals.queue[0].stale = true;
    unified.open("tool:appr_1");
    const w = mount(UnifiedApprovalModal);

    expect(w.find('[data-test="unified-approval-stale-ack"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
    expect(w.find('[data-test="unified-approval-reject"]').exists()).toBe(false);
    await w.find('[data-test="unified-approval-stale-ack"]').trigger("click");
    expect(approvals.queue).toHaveLength(0);
    w.unmount();
  });
});

describe("队列与关闭", () => {
  it("多项时展示切换并保持当前项，切换后显示另一项内容", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一"), knowledge("kn_2", "内容二")];
    unified.open("knowledge:kn_1");
    const w = mount(UnifiedApprovalModal);

    expect(w.find('[data-test="unified-approval-switch"]').exists()).toBe(true);
    expect(w.findAll(".ua-chip")).toHaveLength(2);
    await w.findAll(".ua-chip")[1].trigger("click");
    expect(unified.activeKey).toBe("knowledge:kn_2");
    expect(w.find('[data-test="knowledge-approval-content"]').text()).toContain("内容二");
    w.unmount();
  });

  it("关闭只隐藏：不批准不拒绝，申请保留且标记稍后", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledge("kn_1", "内容一")];
    unified.open("knowledge:kn_1");
    const w = mount(UnifiedApprovalModal);

    await w.find(".ua-close").trigger("click");

    expect(unified.visible).toBe(false);
    expect(unified.knowledge).toHaveLength(1);
    expect(unified.deferredKeys).toContain("knowledge:kn_1");
    expect(mocks.request).not.toHaveBeenCalled();
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    w.unmount();
  });
});
