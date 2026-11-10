/**
 * P1R-D · 弹窗/入口与 E 的 store 字段接线（阶段 / 失效 / 恢复 / 冲突）。
 *
 * 只证明「后端事实 → store 派生 → 组件呈现/事件接线」这条链：
 * 阶段来自 session.applyTopicPhase（TOPIC_PHASE 的唯一入口），组件不自行推进；
 * 恢复重试只经 store 的唯一入口；查重冲突仍走既有工具审批，不生成第二份话题申请。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
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
import UnifiedApprovalLayer from "../../conversation/UnifiedApprovalLayer.vue";
import { useApprovalsStore, type ApprovalItem } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";
import { useSessionStore } from "../../../stores/session";

const APPROVAL_ID = "ap_topic_1";
const KEY = "topic:" + APPROVAL_ID;
const TURN_ID = "turn_1";
const OPERATION_ID = "topop-turn_1-topic_create-abc123";

function setup() {
  setActivePinia(createPinia());
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
    session: useSessionStore(),
  };
}

function topicPayload(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action: "create",
    turn_id: TURN_ID,
    operation_id: OPERATION_ID,
    param_version: 1,
    base_nav_version: 3,
    from_topic_id: "t1",
    from_topic_name: "当前话题名",
    new_name: "布局问题",
    reason: "这已经不是当前话题的范围",
    source: "model",
    ...extra,
  };
}

function enqueueTopic(
  approvals: ReturnType<typeof useApprovalsStore>,
  payload: Record<string, unknown> = topicPayload(),
) {
  approvals.enqueue(APPROVAL_ID, "topic_create", payload, {
    autoOpen: false,
    turnId: TURN_ID,
  });
}

function advancePhase(
  session: ReturnType<typeof useSessionStore>,
  phase: string,
  extra: Record<string, unknown> = {},
) {
  session.activeTurnId = TURN_ID;
  const ok = session.applyTopicPhase({
    phase,
    turn_id: TURN_ID,
    operation_id: OPERATION_ID,
    ...extra,
  });
  expect(ok, "阶段事件应被接受（active turn + 同一 operation）").toBe(true);
}

function openTopic(unified: ReturnType<typeof useUnifiedApprovalsStore>) {
  expect(unified.openTopicApproval(KEY)).toBe(true);
}

beforeEach(() => {
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  mocks.respondTopicApproval.mockReset();
  mocks.request.mockResolvedValue({ ok: true, decision: "approve", items: [] });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  mocks.respondTopicApproval.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时只影响跨用例的暂缓键
  }
});

describe("阶段 → 弹窗正文（受控文案）", () => {
  it("executing：顶部说明「已确认，执行中」，不再提供决定按钮", () => {
    const { unified, approvals, session } = setup();
    enqueueTopic(approvals);
    advancePhase(session, "executing");
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    expect(w.find(".ua-kicker").text()).toContain("已确认，执行中");
    const stage = w.find('[data-test="topic-stage"]');
    expect(stage.attributes("data-stage")).toBe("executing");
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    expect(w.find('[data-test="topic-stay"]').exists()).toBe(false);
    expect(w.text()).not.toContain("正在生成");
    w.unmount();
  });

  it("context_preparing：说明话题已变更、目标上下文准备中；不伪造模型生成", () => {
    const { unified, approvals, session } = setup();
    enqueueTopic(approvals);
    advancePhase(session, "executing");
    advancePhase(session, "context_preparing");
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    expect(w.find(".ua-kicker").text()).toContain("目标上下文准备中");
    expect(w.find('[data-test="topic-stage"]').attributes("data-stage")).toBe(
      "context_preparing",
    );
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    expect(w.text()).not.toContain("正在生成");
    w.unmount();
  });

  it("旧后端没有阶段事件：仍是等待用语与两个动作（不伪造后续阶段）", () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals);
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    expect(w.find(".ua-kicker").text()).toContain("等待你的选择");
    expect(w.find('[data-test="topic-stage"]').attributes("data-stage")).toBe(
      "awaiting_confirmation",
    );
    expect(w.find('[data-test="topic-recovery"]').exists()).toBe(false);
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(true);
    w.unmount();
  });
});

describe("恢复 → 弹窗重试只走 store 唯一入口", () => {
  it("recoverable_failure：显示后端真实原因；重试按钮调用 store 的 retry（不重发消息、不重复执行话题操作）", async () => {
    const { unified, approvals, session } = setup();
    enqueueTopic(approvals);
    advancePhase(session, "recoverable_failure", {
      recovery: {
        record_id: "rec_1",
        message: "话题已切换，回答准备失败",
        retryable: true,
      },
    });
    openTopic(unified);
    const retry = vi.spyOn(unified, "retryTopicAnswer").mockResolvedValue(true);
    const w = mount(UnifiedApprovalModal);

    const recovery = w.find('[data-test="topic-recovery"]');
    expect(recovery.exists()).toBe(true);
    expect(recovery.text()).toContain("回答准备失败");
    expect(w.find(".ua-kicker").text()).toContain("回答准备失败");

    await w.find('[data-test="topic-recovery-retry"]').trigger("click");
    await flushPromises();
    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry.mock.calls[0][0]).toBe("rec_1");
    // 没有借重试重新提交话题决定
    expect(mocks.respondTopicApproval).not.toHaveBeenCalled();
    w.unmount();
  });
});

describe("失效 → 弹窗保留原因、名称与恢复入口", () => {
  it("失效申请：原因可见、原确认不可执行、保留改名；收口不发 /respond", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals);
    unified.updateTopicDraft(KEY, "布局与间距问题");
    unified.markTopicInvalidated(APPROVAL_ID, {
      code: "topic_application_stale",
      reason: "你已经在别处切换了话题（导航位置已变化）",
    });
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    const invalidated = w.find('[data-test="topic-invalidated"]');
    expect(invalidated.exists()).toBe(true);
    expect(invalidated.text()).toContain("导航位置已变化");
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    expect((w.find('[data-test="topic-name-input"]').element as HTMLInputElement).value).toBe(
      "布局与间距问题",
    );

    await w.find('[data-test="topic-stay"]').trigger("click");
    await flushPromises();
    expect(mocks.respondTopicApproval).not.toHaveBeenCalled();
    expect(approvals.queue).toHaveLength(0);
    // 改过的名字仍保留在 store（收口 != 丢草稿）
    expect(unified.topicDrafts[KEY]).toBe("布局与间距问题");
    w.unmount();
  });
});

describe("查重冲突 → 既有工具审批（同一操作，不生成第二份申请）", () => {
  it("create_topic 二次审批：显示新名称与已有相似话题，仍走这条审批自己的按钮", () => {
    const { unified, approvals } = setup();
    const payload = {
      name: "布局问题",
      existing_topic_name: "布局",
      operation_id: "op_conflict_1",
      source_approval_id: APPROVAL_ID,
    };
    approvals.enqueue("ap_conflict_1", "create_topic", payload, { autoOpen: false });
    expect(unified.open()).toBe(true);
    const w = mount(UnifiedApprovalModal);

    const block = w.find('[data-test="topic-conflict-approval"]');
    expect(block.exists()).toBe(true);
    expect(block.text()).toContain("布局问题");
    expect(block.text()).toContain("布局");
    expect(block.text()).toContain("不会生成第二份话题申请");
    // 决定仍用这条既有工具审批的按钮，没有另造话题申请
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
    expect(unified.topicItems).toHaveLength(0);
    w.unmount();
  });
});

describe("对话层 → 阶段/恢复提示", () => {
  it("executing：对话里显示受控状态（不出现「生成」）；answers 后自然消失", async () => {
    const { session } = setup();
    advancePhase(session, "executing");
    const w = mount(UnifiedApprovalLayer, { props: { showMemory: false } });
    await flushPromises();

    const notice = w.find('[data-test="topic-stage-notice"]');
    expect(notice.exists()).toBe(true);
    expect(notice.attributes("data-stage")).toBe("executing");
    expect(notice.text()).toContain("已确认");
    expect(notice.text()).not.toContain("生成");

    advancePhase(session, "context_preparing");
    await flushPromises();
    expect(w.find('[data-test="topic-stage-notice"]').attributes("data-stage")).toBe(
      "context_preparing",
    );

    advancePhase(session, "answerable");
    await flushPromises();
    expect(w.find('[data-test="topic-stage-notice"]').exists()).toBe(false);
    w.unmount();
  });

  it("recoverable_failure：显示真实原因并可重试（调用 session 的唯一 retry 入口）", async () => {
    const { session, unified } = setup();
    advancePhase(session, "recoverable_failure", {
      recovery: { record_id: "rec_9", message: "话题已切换，回答准备失败", retryable: true },
    });
    const spy = vi
      .spyOn(session, "retryTopicAnswer")
      .mockResolvedValue({ ok: true, code: "", message: "" });
    const w = mount(UnifiedApprovalLayer, { props: { showMemory: false } });
    await flushPromises();

    const notice = w.find('[data-test="topic-stage-notice"]');
    expect(notice.exists()).toBe(true);
    expect(notice.attributes("role")).toBe("alert");
    expect(notice.text()).toContain("回答准备失败");

    await w.find('[data-test="topic-stage-retry"]').trigger("click");
    await flushPromises();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0]).toBe("rec_9");
    // 结果走既有顶部提示通道（不伪造阶段推进；成功文本由组件如实说明）
    expect(w.find('[data-test="ua-notices"]').text()).toContain("重新开始回答");
    w.unmount();
  });
});
