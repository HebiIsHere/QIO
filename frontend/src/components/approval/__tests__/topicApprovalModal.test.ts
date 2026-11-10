/**
 * 话题申请在统一确认弹窗内的行为（C · 契约 §2.1/§2.2/§2.4）。
 *
 * 覆盖验收点：内容只展示允许字段（不含原问题/摘要/预览）、create 改名后提交 edits、
 * 空名称本地阻断、reject 提交「在当前话题回答」、关闭只收起不 decide、重复点击只提交一次、
 * 过期/失败/目标消失/失效状态就地显示、收起后不自动重开、store 公共接口形状。
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
import { useApprovalsStore, type ApprovalItem } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";
import { useModalArbiterStore } from "../../../stores/modalArbiter";
import { feedbackGate } from "../../../stores/feedbackGate";

const APPROVAL_ID = "ap_topic_1";
const KEY = "topic:" + APPROVAL_ID;

function setup() {
  setActivePinia(createPinia());
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
    arbiter: useModalArbiterStore(),
  };
}

function switchPayload(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action: "switch",
    turn_id: "turn_1",
    from_topic_id: "t1",
    from_topic_name: "当前话题名",
    target_topic_id: "t2",
    target_topic_name: "目标话题名",
    reason: "讨论已经转到布局问题",
    source: "model",
    ...extra,
  };
}

function createPayload(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action: "create",
    turn_id: "turn_1",
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
  payload: Record<string, unknown> = switchPayload(),
  options: { id?: string; meta?: Partial<ApprovalItem>; autoOpen?: boolean } = {},
) {
  const kind = String(payload.action ?? "switch") === "create" ? "topic_create" : "topic_switch";
  approvals.enqueue(options.id ?? APPROVAL_ID, kind, payload, {
    autoOpen: options.autoOpen ?? true,
    turnId: "turn_1",
    ...(options.meta ? { meta: options.meta } : {}),
  });
}

function openTopic(unified: ReturnType<typeof useUnifiedApprovalsStore>, key = KEY) {
  expect(unified.openTopicApproval(key)).toBe(true);
}

beforeEach(() => {
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  mocks.respondTopicApproval.mockReset();
  mocks.request.mockResolvedValue({ ok: true, decision: "approve" });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  mocks.respondTopicApproval.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时只影响跨用例的暂缓键
  }
  feedbackGate.editing = false;
  feedbackGate.composing = false;
  feedbackGate.lastInputLeftAt = 0;
});

describe("话题申请渲染：只展示允许的字段", () => {
  it("切换：当前话题 → 目标话题 + 原因 + 两个动作（不显示原问题/摘要/预览）", () => {
    const { unified, approvals } = setup();
    enqueueTopic(
      approvals,
      switchPayload({
        // 即使载荷里混入了别的字段，也绝不能显示给用户
        question: "原问题不该出现",
        description: "原问题摘要不该出现",
        summary: "预览不该出现",
        preview: "预览正文不该出现",
      }),
    );
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    const content = w.find('[data-test="topic-approval-content"]');
    expect(content.exists()).toBe(true);
    expect(w.find('[data-test="topic-current-name"]').text()).toContain("当前话题名");
    expect(w.find('[data-test="topic-target-name"]').text()).toContain("目标话题名");
    expect(w.find('[data-test="topic-reason"]').text()).toContain("讨论已经转到布局问题");
    expect(w.find('[data-test="topic-approve"]').text()).toBe("转到并回答");
    expect(w.find('[data-test="topic-stay"]').text()).toBe("在当前话题回答");
    // 标题栏不再有第二个关闭按钮：关闭统一在正文右上角
    expect(w.find(".ua-close").exists()).toBe(false);
    expect(w.find('[data-test="topic-approval-close"]').exists()).toBe(true);

    const text = w.text();
    expect(text).not.toContain("原问题不该出现");
    expect(text).not.toContain("原问题摘要不该出现");
    expect(text).not.toContain("预览不该出现");
    expect(text).not.toContain("预览正文不该出现");
    w.unmount();
  });

  it("新建：展示当前话题与可编辑的模型建议名，主按钮是「新建并回答」", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, createPayload());
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    expect(w.find('[data-test="topic-target-name"]').exists()).toBe(false);
    const input = w.find('[data-test="topic-name-input"]');
    expect((input.element as HTMLInputElement).value).toBe("布局问题");
    expect(w.find('[data-test="topic-approve"]').text()).toBe("新建并回答");

    // 回车不直接提交
    await input.trigger("keydown.enter");
    await flushPromises();
    expect(mocks.respondTopicApproval).not.toHaveBeenCalled();
    expect(unified.visible).toBe(true);
    w.unmount();
  });
});

describe("话题申请决定：只走既有 /respond 路径", () => {
  it("create 改名后提交：edits.name 是用户改过的名字", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, createPayload());
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="topic-name-input"]').setValue("布局与间距问题");
    await w.find('[data-test="topic-approve"]').trigger("click");
    await flushPromises();

    expect(mocks.respondTopicApproval).toHaveBeenCalledTimes(1);
    expect(mocks.respondTopicApproval.mock.calls[0][0]).toBe(APPROVAL_ID);
    expect(mocks.respondTopicApproval.mock.calls[0][1]).toBe("approved");
    expect(mocks.respondTopicApproval.mock.calls[0][2]).toEqual({
      edits: { name: "布局与间距问题" },
    });
    expect(mocks.respondTopicApproval.mock.calls[0][3]).toMatchObject({ turnId: "turn_1" });
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    // 成功后队列收口（不显示「已切换/已创建」这类假事实，只收口）
    await flushPromises();
    expect(approvals.queue).toHaveLength(0);
    expect(unified.visible).toBe(false);
    w.unmount();
  });

  it("「在当前话题回答」= reject + scope.reason=stay_current", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals);
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="topic-stay"]').trigger("click");
    await flushPromises();

    expect(mocks.respondTopicApproval).toHaveBeenCalledTimes(1);
    expect(mocks.respondTopicApproval.mock.calls[0][1]).toBe("rejected");
    expect(mocks.respondTopicApproval.mock.calls[0][2]).toEqual({
      scope: { reason: "stay_current" },
    });
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    w.unmount();
  });

  it("空名称在本地阻断：不发请求、不关闭弹窗，就地显示错误", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, createPayload());
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    const input = w.find('[data-test="topic-name-input"]');
    await input.setValue("   ");
    await w.find('[data-test="topic-approve"]').trigger("click");
    await flushPromises();

    expect(mocks.respondTopicApproval).not.toHaveBeenCalled();
    expect(w.find('[data-test="topic-name-error"]').text()).toContain("话题名不能为空");
    expect(unified.visible).toBe(true);
    expect(approvals.queue).toHaveLength(1);
    // store 层也有同一道校验（直接调用 decideTopic 时同样不提交）
    const ok = await unified.decideTopic(KEY, { decision: "approve", edits: { name: "  " } });
    expect(ok).toBe(false);
    expect(mocks.respondTopicApproval).not.toHaveBeenCalled();
    expect(unified.submitError).toContain("话题名不能为空");
    w.unmount();
  });

  it("重复点击只提交一次，执行中两个按钮都禁用", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals);
    openTopic(unified);
    let release!: (value: unknown) => void;
    mocks.respondTopicApproval.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const w = mount(UnifiedApprovalModal);

    const approve = w.find('[data-test="topic-approve"]');
    await approve.trigger("click");
    await approve.trigger("click");
    await flushPromises();

    expect(mocks.respondTopicApproval).toHaveBeenCalledTimes(1);
    expect(approve.attributes("disabled")).toBeDefined();
    expect(w.find('[data-test="topic-stay"]').attributes("disabled")).toBeDefined();
    expect(w.find('[data-test="topic-progress"]').exists()).toBe(true);

    release({ ok: true });
    await flushPromises();
    expect(unified.visible).toBe(false);
    w.unmount();
  });
});

describe("关闭 = 只收起：不调用 decide、不消费申请", () => {
  it("右上角关闭只收起；同一申请不自动重开，用户可以从入口再打开", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals);
    expect(unified.maybeAutoOpen()).toBe(true);
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="topic-approval-close"]').trigger("click");
    await flushPromises();

    expect(mocks.respondTopicApproval).not.toHaveBeenCalled();
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(approvals.queue).toHaveLength(1);
    expect(unified.visible).toBe(false);
    expect(unified.collapsedTopicKeys.has(KEY)).toBe(true);
    // 已收起的同一申请不自动重开
    expect(unified.maybeAutoOpen()).toBe(false);
    // 入口主动打开仍然可以
    openTopic(unified);
    await flushPromises();
    expect(w.find('[data-test="topic-approval-content"]').exists()).toBe(true);
    w.unmount();
  });

  it("新建：收起后保留用户改过的新名称", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, createPayload());
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="topic-name-input"]').setValue("改过的名字");
    await w.find('[data-test="topic-approval-close"]').trigger("click");
    await flushPromises();
    expect(unified.topicDraftFor(unified.topicItems[0] ?? null)).toBe("改过的名字");

    openTopic(unified);
    await flushPromises();
    const input = w.find('[data-test="topic-name-input"]');
    expect((input.element as HTMLInputElement).value).toBe("改过的名字");
    w.unmount();
  });
});

describe("过期 / 失败 / 失效状态就地显示", () => {
  it("已过期：不再提供批准；仍可选择在当前话题回答", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, switchPayload(), {
      meta: { expiresAt: new Date(Date.now() - 60_000).toISOString() },
    });
    openTopic(unified);
    const w = mount(UnifiedApprovalModal);

    expect(w.find('[data-test="topic-expired"]').exists()).toBe(true);
    expect(w.find('[data-test="topic-approve"]').attributes("disabled")).toBeDefined();
    expect(w.find('[data-test="topic-stay"]').attributes("disabled")).toBeUndefined();
    await w.find('[data-test="topic-approve"]').trigger("click");
    await flushPromises();
    expect(mocks.respondTopicApproval).not.toHaveBeenCalled();
    const ok = await unified.decideTopic(KEY, { decision: "approve" });
    expect(ok).toBe(false);
    expect(unified.submitError).toContain("到期");
    w.unmount();
  });

  it("后端 400（空名称 code=topic_name_required）：就地显示中文原因、弹窗不关、申请不消费", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, createPayload());
    openTopic(unified);
    mocks.respondTopicApproval.mockRejectedValueOnce(
      Object.assign(new Error("400"), {
        status: 400,
        body: {
          detail: { code: "topic_name_required", message: "话题名不能为空" },
        },
      }),
    );
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="topic-approve"]').trigger("click");
    await flushPromises();

    expect(w.find(".ua-error").text()).toContain("话题名不能为空");
    expect(unified.visible).toBe(true);
    expect(approvals.queue).toHaveLength(1);
    expect(unified.submitErrorCode).toBe("topic_name_required");
    expect(w.find('[data-test="topic-name-input"]').exists()).toBe(true);
    w.unmount();
  });

  it("目标话题消失（400 code=topic_target_missing）：就地显示并保留「在当前话题回答」", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals);
    openTopic(unified);
    mocks.respondTopicApproval.mockRejectedValueOnce(
      Object.assign(new Error("400"), {
        status: 400,
        body: {
          detail: {
            code: "topic_target_missing",
            message: "目标话题已不存在，请选择在当前话题回答",
          },
        },
      }),
    );
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="topic-approve"]').trigger("click");
    await flushPromises();

    expect(w.find(".ua-error").text()).toContain("目标话题已不存在");
    expect(unified.visible).toBe(true);
    const stay = w.find('[data-test="topic-stay"]');
    expect(stay.exists()).toBe(true);
    expect(stay.attributes("disabled")).toBeUndefined();
    expect(approvals.queue).toHaveLength(1);
    w.unmount();
  });

  it("申请已失效（409 topic_application_stale）：不假成功，只给收口入口", async () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals);
    openTopic(unified);
    mocks.respondTopicApproval.mockRejectedValueOnce(
      Object.assign(new Error("409"), {
        status: 409,
        body: {
          detail: { code: "topic_application_stale", message: "没有等待处理的 waiter" },
        },
      }),
    );
    const w = mount(UnifiedApprovalModal);

    await w.find('[data-test="topic-approve"]').trigger("click");
    await flushPromises();

    expect(w.find('[data-test="topic-stale"]').exists()).toBe(true);
    expect(w.find('[data-test="topic-approve"]').exists()).toBe(false);
    expect(w.find('[data-test="topic-stay"]').exists()).toBe(false);
    expect(unified.lastNotice).toContain("失效");

    await w.find('[data-test="topic-stale-ack"]').trigger("click");
    await flushPromises();
    expect(approvals.queue).toHaveLength(0);
    expect(unified.visible).toBe(false);
    // 只提交过一次；收口不补决定
    expect(mocks.respondTopicApproval).toHaveBeenCalledTimes(1);
    w.unmount();
  });
});

describe("store 公共接口（D 只读引用）", () => {
  it("topicItems 给出 key / turnId 与契约字段；topic 项不进 toolItems", () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_tool_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    enqueueTopic(approvals, switchPayload());

    expect(unified.count).toBe(2);
    expect(unified.toolItems).toHaveLength(1);
    expect(unified.toolItems[0]?.identity).toBe("ap_tool_1");
    const topics = unified.topicItems;
    expect(topics).toHaveLength(1);
    expect(topics[0]).toMatchObject({
      key: KEY,
      approvalId: APPROVAL_ID,
      action: "switch",
      turnId: "turn_1",
      currentTopicName: "当前话题名",
      targetTopicName: "目标话题名",
      reason: "讨论已经转到布局问题",
    });
    // 统一队列里两项都在（按 approvals 队列到达顺序）
    expect(unified.items.map((item) => item.kind)).toEqual(["tool", "topic"]);
  });

  it("topicPendingForTurn 只匹配对应轮次", () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, switchPayload({ turn_id: "turn_1" }));

    expect(unified.topicPendingForTurn("turn_1")?.approvalId).toBe(APPROVAL_ID);
    expect(unified.topicPendingForTurn("turn_other")).toBeNull();
    expect(unified.topicPendingForTurn(null)).toBeNull();
  });

  it("新到达的话题申请自动展开；收起过的不自动重开；新的申请仍可自动展开", () => {
    const { unified, approvals } = setup();
    enqueueTopic(approvals, switchPayload(), { id: "ap_topic_1" });
    expect(unified.maybeAutoOpen()).toBe(true);
    expect(unified.activeKey).toBe(KEY);
    unified.close();
    expect(unified.collapsedTopicKeys.has(KEY)).toBe(true);
    expect(unified.maybeAutoOpen()).toBe(false);

    // 真正新到达的另一条申请（不同 approval_id）仍按规则自动弹
    enqueueTopic(approvals, createPayload(), { id: "ap_topic_2", autoOpen: true });
    expect(unified.maybeAutoOpen()).toBe(true);
    expect(unified.activeKey).toBe("topic:ap_topic_2");
  });
});
