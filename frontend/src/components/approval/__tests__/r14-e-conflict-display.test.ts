/**
 * R14-E · 查重冲突确认的展示接线（F10 后端字段 → 我的统一弹窗）。
 *
 * 背景：C 的 F10 重做把工具级查重冲突 payload 的展示字段统一为
 * `similar_topic_name`（真实相似话题标题；不参与 param_version / 批准身份）。
 * 本文件锁住组件侧消费契约，防止后端字段落库了、界面却仍显示空名称：
 *
 * - 只给 `similar_topic_name`（没有 existing_topic_name）时，界面也必须显示
 *   「已有话题『X』相似」；
 * - 仍是**同一次工具审批**（kind=create_topic）的二次确认，不生成第二份话题申请；
 * - 决定仍走这条审批自己的按钮（不另造提交路径）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  respondApproval: vi.fn(),
  respondTopicApproval: vi.fn(),
}));

vi.mock("../../../services/api", () => ({
  request: mocks.request,
  api: {
    respondApproval: mocks.respondApproval,
    respondTopicApproval: mocks.respondTopicApproval,
  },
}));

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

const NEW_NAME = "布局问题";
const SIMILAR_NAME = "布局设计规范整理";

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
  mocks.respondTopicApproval.mockReset();
  mocks.request.mockResolvedValue({ ok: true, decision: "approve", items: [] });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  mocks.respondTopicApproval.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
});

describe("查重冲突二次确认：显示真实已有话题名（F10 payload 字段）", () => {
  it("只给 similar_topic_name 也能显示「已有话题」名称，且不生成第二份话题申请", () => {
    const { unified, approvals } = setup();
    approvals.enqueue(
      "ap_conflict_similar_1",
      "create_topic",
      {
        // F10：相似话题的真实标题来自后端 fingerprint(dup_id).title
        similar_topic_name: SIMILAR_NAME,
        similar_topic_id: "topic_similar_1",
        score: 0.82,
        name: NEW_NAME,
        target_name: NEW_NAME,
        operation_id: "topop-turn_1-topic_create-abcdef",
        param_version: 2,
        conflict: "conflict_pending",
      },
      { autoOpen: false },
    );
    expect(unified.open()).toBe(true);
    const w = mount(UnifiedApprovalModal);

    const block = w.find('[data-test="topic-conflict-approval"]');
    expect(block.exists()).toBe(true);
    expect(block.text()).toContain(NEW_NAME);
    // 关键断言：已有话题名必须来自 similar_topic_name；NEW_NAME 不包含这段文字
    expect(block.text()).toContain(SIMILAR_NAME);
    expect(block.text()).toContain("不会生成第二份话题申请");
    // 同一操作身份可见，不伪造第二个申请
    expect(block.text()).toContain("topop-turn_1-topic_create-abcdef");
    expect(unified.topicItems).toHaveLength(0);
    // 决定仍走这条工具审批自己的按钮
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
    w.unmount();
  });

  it("旧后端只给 existing_topic_name 时仍显示（兼容键优先级不变）", () => {
    const { unified, approvals } = setup();
    approvals.enqueue(
      "ap_conflict_legacy_1",
      "create_topic",
      {
        name: NEW_NAME,
        existing_topic_name: "旧的相似话题名",
        operation_id: "op_legacy_1",
      },
      { autoOpen: false },
    );
    expect(unified.open()).toBe(true);
    const w = mount(UnifiedApprovalModal);
    const block = w.find('[data-test="topic-conflict-approval"]');
    expect(block.exists()).toBe(true);
    expect(block.text()).toContain("旧的相似话题名");
    w.unmount();
  });
});
