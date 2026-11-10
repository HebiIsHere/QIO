/**
 * P1R-D · 话题申请阶段 / 失效 / 恢复 / 冲突的受控呈现（契约 §8）。
 *
 * 只验证组件契约：给定 E 的 store 派生字段（作为 props 传入）时，
 * 五种状态的文案、按钮可用性与事件接线正确；不重做布局、不引入第二套弹窗。
 * 数据来源约定见 task-4 与 D↔E 字段对齐消息。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import TopicApprovalContent from "../TopicApprovalContent.vue";
import type { TopicApprovalItem } from "../../../stores/unifiedApprovals";
import type { ApprovalItem } from "../../../stores/approvals";

function approvalOf(kind = "topic_switch", payload: Record<string, unknown> = {}): ApprovalItem {
  return { approval_id: "ap_topic_1", kind, payload };
}

function topicItem(overrides: Partial<TopicApprovalItem> = {}): TopicApprovalItem {
  return {
    key: "topic:ap_topic_1",
    approvalId: "ap_topic_1",
    kind: "topic_switch",
    action: "switch",
    currentTopicName: "当前话题",
    targetTopicName: "目标话题",
    targetTopicId: "t2",
    suggestedName: "",
    reason: "讨论已经转到布局问题",
    turnId: "turn_1",
    approval: approvalOf(),
    ...overrides,
  };
}

function mountContent(extraProps: Record<string, unknown> = {}, item = topicItem()) {
  return mount(TopicApprovalContent, {
    props: {
      item,
      name: String(extraProps.name ?? ""),
      busy: false,
      expired: false,
      stale: false,
      ...extraProps,
    },
  });
}

describe("阶段显示：受控状态文案（不伪造模型生成）", () => {
  it("等待确认：说明在等什么，两个动作都可用", () => {
    const w = mountContent();
    expect(w.find('[data-test="topic-stage"]').attributes("data-stage")).toBe(
      "awaiting_confirmation",
    );
    expect(w.find('[data-test="topic-stage"]').text()).toContain("等待你的选择");
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(true);
    expect(w.find('[data-test="topic-stay"]').exists()).toBe(true);
    w.unmount();
  });

  it("已确认执行中：只说明执行中，不再提供决定按钮", () => {
    const w = mountContent({ stage: "executing" });
    const stage = w.find('[data-test="topic-stage"]');
    expect(stage.attributes("data-stage")).toBe("executing");
    expect(stage.text()).toContain("已确认");
    expect(stage.text()).toContain("执行");
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    expect(w.find('[data-test="topic-stay"]').exists()).toBe(false);
    // 受控文案：不得伪造「模型生成中」
    expect(w.text()).not.toContain("正在生成");
    w.unmount();
  });

  it("目标上下文准备中：说明话题已经变化、上下文正在准备", () => {
    const w = mountContent({ stage: "context_preparing" });
    const stage = w.find('[data-test="topic-stage"]');
    expect(stage.attributes("data-stage")).toBe("context_preparing");
    expect(stage.text()).toContain("上下文");
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    expect(w.find('[data-test="topic-stay"]').exists()).toBe(false);
    w.unmount();
  });

  it("旧后端没有阶段字段：仍按「等待你的选择」呈现，不伪造执行/失败阶段", () => {
    const w = mountContent({ stage: null });
    // 有 pending 申请本身就等于等待确认；阶段字段缺失只说明后端没有后续阶段事件
    expect(w.find('[data-test="topic-stage"]').attributes("data-stage")).toBe(
      "awaiting_confirmation",
    );
    expect(w.find('[data-test="topic-recovery"]').exists()).toBe(false);
    expect(w.find('[data-test="topic-invalidated"]').exists()).toBe(false);
    expect(w.find('[data-test="topic-approve"]').text()).toBe("转到并回答");
    expect(w.find('[data-test="topic-stay"]').text()).toBe("在当前话题回答");
    w.unmount();
  });
});

describe("错误与恢复：显示真实错误 + 重试入口", () => {
  it("回答准备失败：显示真实原因；点重试把 recordId 交给外层（组件不发请求）", async () => {
    const w = mountContent({
      stage: "recoverable_failure",
      recovery: {
        recordId: "rec_1",
        message: "话题已切换，回答准备失败",
        retryable: true,
      },
    });
    const recovery = w.find('[data-test="topic-recovery"]');
    expect(recovery.exists()).toBe(true);
    expect(recovery.text()).toContain("回答准备失败");
    expect(recovery.text()).toContain("保留");
    const retry = w.find('[data-test="topic-recovery-retry"]');
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    expect(w.emitted("retry")?.[0]).toEqual(["rec_1"]);
    w.unmount();
  });

  it("不可重试的恢复：只说明事实，不给一个点了没用的重试按钮", () => {
    const w = mountContent({
      stage: "recoverable_failure",
      recovery: { recordId: "rec_2", message: "话题已切换，回答生成失败", retryable: false },
    });
    expect(w.find('[data-test="topic-recovery"]').text()).toContain("回答生成失败");
    expect(w.find('[data-test="topic-recovery-retry"]').exists()).toBe(false);
    w.unmount();
  });

  it("重试进行中：按钮禁用并如实说明，不重复提交", () => {
    const w = mountContent({
      stage: "recoverable_failure",
      recovery: { recordId: "rec_1", message: "话题已切换，回答准备失败", retryable: true },
      retrying: true,
    });
    expect(w.find('[data-test="topic-recovery-retry"]').attributes("disabled")).toBeDefined();
    expect(w.find('[data-test="topic-recovery"]').text()).toContain("重试");
    w.unmount();
  });
});

describe("申请失效：原确认不可执行、说明原因、保留恢复入口", () => {
  it("失效：不提供批准；说明没有执行也没有拒绝；保留「在当前话题回答」作为恢复入口", async () => {
    const w = mountContent({
      invalidated: true,
      invalidatedReason: "你已经在别处切换了话题（导航位置已变化）",
    });
    const block = w.find('[data-test="topic-invalidated"]');
    expect(block.exists()).toBe(true);
    expect(block.text()).toContain("导航位置已变化");
    expect(block.text()).toContain("没有执行");
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    // 恢复入口：在当前话题回答（不写拒绝范围、不发决定请求）
    const stay = w.find('[data-test="topic-stay"]');
    expect(stay.exists()).toBe(true);
    await stay.trigger("click");
    expect(w.emitted("acknowledge")).toHaveLength(1);
    expect(w.emitted("decide")).toBeUndefined();
    w.unmount();
  });

  it("失效优先于过期说明：不把失效说成「到期」", () => {
    const w = mountContent({ invalidated: true, invalidatedReason: "目标话题已被删除", expired: true });
    expect(w.find('[data-test="topic-invalidated"]').exists()).toBe(true);
    expect(w.find('[data-test="topic-expired"]').exists()).toBe(false);
    w.unmount();
  });

  it("新建申请失效时仍保留用户已改的新名称（输入框未重置、不自动提交）", async () => {
    const item = topicItem({
      action: "create",
      kind: "topic_create",
      targetTopicName: "",
      targetTopicId: "",
      suggestedName: "布局问题",
      approval: approvalOf("topic_create", { new_name: "布局问题" }),
    });
    const w = mountContent({ invalidated: true, invalidatedReason: "导航位置已变化", name: "布局与间距问题" }, item);
    const input = w.find('[data-test="topic-name-input"]');
    expect((input.element as HTMLInputElement).value).toBe("布局与间距问题");
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    expect(w.emitted("decide")).toBeUndefined();
    w.unmount();
  });
});

describe("查重冲突衔接：同一操作、不生成第二份申请", () => {
  it("冲突待确认：显示已有相似话题名，说明是同一操作的二次确认", () => {
    const item = topicItem({
      action: "create",
      kind: "topic_create",
      targetTopicName: "",
      suggestedName: "布局问题",
      approval: approvalOf("topic_create", { new_name: "布局问题" }),
    });
    const w = mountContent(
      {
        name: "布局与间距问题",
        conflict: {
          status: "conflict_pending",
          existingTopicId: "t9",
          existingTopicName: "布局问题",
        },
      },
      item,
    );
    const block = w.find('[data-test="topic-conflict"]');
    expect(block.exists()).toBe(true);
    expect(block.text()).toContain("布局问题");
    expect(block.text()).toContain("同一");
    expect(block.text()).toContain("不会生成第二份");
    // 决定动作仍走同一条申请（主按钮仍在，不另造一个「新建」入口）
    expect(w.find('[data-test="topic-approve"]').text()).toBe("新建并回答");
    w.unmount();
  });

  it("没有冲突字段（旧后端 / 无冲突）：不显示冲突行", () => {
    const w = mountContent({ conflict: null });
    expect(w.find('[data-test="topic-conflict"]').exists()).toBe(false);
    w.unmount();
  });
});
