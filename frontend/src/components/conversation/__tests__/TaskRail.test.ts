/**
 * 左侧任务栏组件（A）：真实状态表达、置顶、去重、多项展开、动作门控、失败可重试。
 * 这里只断言「用户看到的事实」，不测静态 CSS 类名。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  fetchTaskFeed: vi.fn(),
  fetchTaskDetail: vi.fn(),
  acknowledgeTask: vi.fn(),
  requestTaskCancel: vi.fn(),
}));

vi.mock("../../../services/taskFeedApi", () => api);

import TaskRail from "../TaskRail.vue";
import { useConversationTasksStore } from "../../../stores/conversationTasks";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useApprovalsStore } from "../../../stores/approvals";
import { useSessionStore } from "../../../stores/session";
import type { TaskCounts, TaskFeedItem, TaskFeedPage, TaskView } from "../../../conversation/contracts";

const ZERO_COUNTS: TaskCounts = {
  queued: 0,
  running: 0,
  waitingApproval: 0,
  waitingUser: 0,
  completed: 0,
  partial: 0,
  failed: 0,
  cancelled: 0,
  budgetExhausted: 0,
  unknown: 0,
};

function counts(overrides: Partial<TaskCounts> = {}): TaskCounts {
  return { ...ZERO_COUNTS, ...overrides };
}

function page(items: TaskFeedItem[], overrides: Partial<TaskFeedPage> = {}): TaskFeedPage {
  return {
    items,
    nextCursor: null,
    counts: counts(),
    serverTime: "2026-10-22T10:00:00.000Z",
    instanceId: "inst-1",
    ...overrides,
  };
}

function item(overrides: Partial<TaskFeedItem> = {}): TaskFeedItem {
  // 稳定身份是 source:sourceId：fixture 从 id 推出两者，避免测试自己造出重复身份。
  const id = String(overrides.id ?? "tool:1");
  const colon = id.indexOf(":");
  const source = overrides.source ?? (colon > 0 ? (id.slice(0, colon) as TaskFeedItem["source"]) : "tool");
  const sourceId = overrides.sourceId ?? (colon > 0 ? id.slice(colon + 1) : id);
  return {
    id,
    source,
    sourceId,
    title: "一次工具执行",
    status: "running",
    rawStatus: "running",
    origin: "topic",
    topicId: "t1",
    topicTitle: "布局讨论",
    executor: null,
    summary: "",
    queuedAt: null,
    startedAt: "2026-10-22T09:00:00.000Z",
    finishedAt: null,
    durationMs: null,
    requiresAttention: false,
    acknowledged: false,
    output: { truncated: false, preview: "", hint: "", readMore: false },
    actions: [],
    ...overrides,
  };
}

function feed(current: TaskFeedPage | Error, history?: TaskFeedPage | Error) {
  api.fetchTaskFeed.mockImplementation((opts: { view: TaskView }) => {
    const target = opts.view === "current" ? current : (history ?? page([]));
    if (target instanceof Error) return Promise.reject(target);
    return Promise.resolve(target);
  });
}

async function mountRail() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(TaskRail, { global: { plugins: [pinia] } });
  await flushPromises();
  await wrapper.vm.$nextTick();
  return wrapper;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.acknowledgeTask.mockResolvedValue(undefined);
  api.requestTaskCancel.mockResolvedValue({ accepted: false, detail: "只有独立任务支持取消" });
});

afterEach(() => {
  useConversationTasksStore().dispose();
  useConversationShellStore().dispose();
});

describe("状态与来源如实显示", () => {
  it("状态无法确认的任务不会被说成运行中；部分完成不折叠成已完成", async () => {
    feed(
      page([
        item({ id: "tool:unknown", status: "unknown", rawStatus: "mystery_state" }),
        item({ id: "tool:partial", status: "partial", rawStatus: "partially_completed" }),
      ]),
    );
    const wrapper = await mountRail();
    const text = wrapper.text();
    expect(text).toContain("状态无法确认");
    expect(text).toContain("部分完成");
    expect(text).not.toContain("运行中");
    expect(text).not.toContain("已完成");

    const unknownRow = wrapper.find('[data-task-id="tool:unknown"] .task-item');
    expect(unknownRow.attributes("data-status")).toBe("unknown");
    wrapper.unmount();
  });

  it("来源说明：公共维护 / 话题 / 来源未知不互相冒充", async () => {
    feed(
      page([
        item({ id: "maintenance:1", source: "maintenance", origin: "global", topicId: null, topicTitle: null, title: "记忆整理" }),
        item({ id: "tool:2", origin: "topic", topicTitle: "布局讨论" }),
        item({ id: "tool:3", origin: "unknown", topicId: null, topicTitle: null }),
      ]),
    );
    const wrapper = await mountRail();
    expect(wrapper.text()).toContain("公共维护");
    expect(wrapper.text()).toContain("布局讨论");
    expect(wrapper.text()).toContain("来源未知");
    wrapper.unmount();
  });
});

describe("排序、去重与展开", () => {
  it("需要处理的任务置顶，其余按时间从新到旧；同一个 id 只出现一行", async () => {
    feed(
      page([
        item({ id: "tool:new", startedAt: "2026-10-22T12:00:00.000Z" }),
        item({ id: "tool:dup", startedAt: "2026-10-22T11:00:00.000Z" }),
        item({
          id: "tool:failed",
          status: "failed",
          requiresAttention: true,
          startedAt: "2026-10-22T08:00:00.000Z",
        }),
      ]),
    );
    const wrapper = await mountRail();
    const ids = wrapper.findAll("[data-task-id]").map((row) => row.attributes("data-task-id"));
    expect(ids).toEqual(["tool:failed", "tool:new", "tool:dup"]);
    wrapper.unmount();
  });

  it("可以同时展开多个任务（不复用输入区「一次只展开一个」规则）", async () => {
    feed(
      page([
        item({ id: "tool:1", title: "第一个" }),
        item({ id: "tool:2", title: "第二个" }),
      ]),
    );
    const wrapper = await mountRail();
    const triggers = wrapper.findAll(".task-trigger");
    await triggers[0].trigger("click");
    await triggers[1].trigger("click");
    expect(wrapper.findAll(".task-detail")).toHaveLength(2);
    wrapper.unmount();
  });

  it("切换当前/历史后回来，展开状态仍保留", async () => {
    feed(page([item({ id: "tool:1" })]), page([item({ id: "tool:h", status: "completed" })]));
    const wrapper = await mountRail();
    await wrapper.find(".task-trigger").trigger("click");
    expect(wrapper.findAll(".task-detail")).toHaveLength(1);

    await wrapper.findAll('[role="tab"]')[1].trigger("click");
    await flushPromises();
    await wrapper.findAll('[role="tab"]')[0].trigger("click");
    await flushPromises();

    expect(wrapper.findAll(".task-detail")).toHaveLength(1);
    wrapper.unmount();
  });
});

describe("动作与审批入口", () => {
  it("只有后端 supported 的动作才渲染按钮", async () => {
    feed(
      page([
        item({
          id: "tool:1",
          actions: [
            { kind: "open_preview", label: "在右侧查看", supported: true },
            { kind: "cancel", label: "申请取消", supported: false },
          ],
        }),
      ]),
    );
    const wrapper = await mountRail();
    await wrapper.find(".task-trigger").trigger("click");

    const actions = wrapper.findAll(".task-action").map((button) => button.text());
    expect(actions).toContain("在右侧查看");
    expect(actions).not.toContain("申请取消");
    wrapper.unmount();
  });

  it("等待审批的任务有定位入口，点击打开统一审批弹窗（左侧没有批准/拒绝）", async () => {
    feed(
      page([
        item({
          id: "tool:waiting",
          status: "waiting_approval",
          requiresAttention: true,
          rawStatus: "waiting_approval",
        }),
      ]),
    );
    const wrapper = await mountRail();
    const approvals = useApprovalsStore();
    approvals.enqueue("approval-1", "tool", {}, { autoOpen: true });
    approvals.defer();
    expect(approvals.deferred).toBe(true);
    const entry = wrapper.find(".task-approval-entry");
    expect(entry.exists()).toBe(true);
    await entry.trigger("click");

    expect(approvals.deferred).toBe(false);
    const texts = wrapper.findAll("button").map((button) => button.text());
    expect(texts.join(" ")).not.toMatch(/批准|拒绝/);
    wrapper.unmount();
  });

  it("截断的输出标明「仅显示部分」并提供查看更多", async () => {
    feed(
      page([
        item({
          id: "tool:1",
          output: {
            truncated: true,
            preview: "只有一小段输出",
            hint: "完整输出需要按需读取",
            readMore: true,
          },
          actions: [{ kind: "read_more", label: "查看更多", supported: true }],
        }),
      ]),
    );
    const wrapper = await mountRail();
    await wrapper.find(".task-trigger").trigger("click");
    expect(wrapper.text()).toContain("仅显示部分");
    expect(wrapper.text()).toContain("查看更多");
    wrapper.unmount();
  });
});

describe("加载、失败与重试", () => {
  it("接口不可用：显示原因与重试，不显示空列表", async () => {
    const notFound = Object.assign(new Error("tasks -> 404"), { status: 404 });
    feed(notFound);
    const wrapper = await mountRail();
    expect(wrapper.text()).toContain("任务列表暂不可用");
    expect(wrapper.text()).toContain("重试");
    expect(wrapper.text()).not.toContain("当前没有任务");

    feed(page([]));
    await wrapper.find(".rail-error .qio-btn").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("当前没有任务");
    wrapper.unmount();
  });

  it("服务端确实返回空列表时才说「当前没有任务」", async () => {
    feed(page([]));
    const wrapper = await mountRail();
    expect(wrapper.text()).toContain("当前没有任务");
    wrapper.unmount();
  });

  it("恢复事项合并进当前任务并标明来源", async () => {
    feed(page([]));
    const wrapper = await mountRail();
    const session = useSessionStore();
    session.recoveryRecords = [
      {
        record_id: "rec-1",
        kind: "user_turn",
        state_class: "ready",
        status: "interrupted",
        message: "把这条消息重新发出去",
        topic_id: null,
        reason: "上一次没有执行完",
        created_at: "2026-10-22T08:00:00Z",
        updated_at: null,
        owner_instance_id: null,
        owner_state: "dead",
        owner_note: "",
        claim_generation: null,
        attempts: 1,
        last_error: null,
        actions: [{ id: "continue", label: "继续", enabled: true, reason: "" }],
      } as never,
    ];
    await wrapper.vm.$nextTick();
    expect(wrapper.text()).toContain("把这条消息重新发出去");
    expect(wrapper.text()).toContain("待恢复");

    await wrapper.find(".task-trigger").trigger("click");
    expect(wrapper.text()).toContain("去处理");
    wrapper.unmount();
  });
});
