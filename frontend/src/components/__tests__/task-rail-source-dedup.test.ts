/**
 * 左侧任务栏折叠行的来源去重（用户提示词 §五.3）。
 *
 * 现场：工具任务的标题已经是「工具执行 · <工具名>」，折叠行 meta 又写一遍「工具执行」，
 * 同一行出现两次同样的来源；用户要求保留具体工具名、必要归属与状态，来源改为在
 * 展开后的详情 facts 里仍然可查。
 *
 * 这里直接挂载真实组件（不经过 store/接口），只断言用户看到的事实：
 * - 工具任务：标题一次、折叠行不再重复来源，工具名/归属/状态还在，详情仍有「来源」；
 * - 非工具任务：来源照常显示，去重规则不误伤；
 * - 规则是「标题以来源文案开头」而不是写死 source === "tool"，独立任务同一条规则。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import TaskRailItem from "../conversation/TaskRailItem.vue";
import type { TaskFeedItem } from "../../conversation/contracts";

function taskItem(overrides: Partial<TaskFeedItem> = {}): TaskFeedItem {
  const id = String(overrides.id ?? "tool:1");
  const colon = id.indexOf(":");
  const source = overrides.source ?? (colon > 0 ? (id.slice(0, colon) as TaskFeedItem["source"]) : "tool");
  const sourceId = overrides.sourceId ?? (colon > 0 ? id.slice(colon + 1) : id);
  return {
    id,
    source,
    sourceId,
    title: "工具执行 · 运行 Shell 命令",
    status: "running",
    rawStatus: "running",
    origin: "topic",
    topicId: "t1",
    topicTitle: "布局讨论",
    executor: "运行 Shell 命令",
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

function mountItem(item: TaskFeedItem, expanded = false) {
  return mount(TaskRailItem, {
    props: {
      item,
      expanded,
      detail: null,
      detailLoading: false,
      detailError: "",
      notice: "",
      retainedNote: "",
      busy: false,
      busyKind: "",
      highlighted: false,
    },
  });
}

describe("任务栏折叠行来源去重", () => {
  it("工具任务：标题已含「工具执行」时折叠行不再重复来源，工具名/归属/状态保留", () => {
    const wrapper = mountItem(taskItem());

    // 标题只出现一次，且保留具体工具名称
    const title = wrapper.get(".task-title");
    expect(title.text()).toBe("工具执行 · 运行 Shell 命令");
    expect(wrapper.findAll(".task-title")).toHaveLength(1);
    // 折叠行（标题行 + meta 行）里「工具执行」只由标题贡献一次，没有第二个来源标签
    expect((wrapper.get(".task-line").text().match(/工具执行/g) ?? []).length).toBe(1);
    expect(wrapper.find(".task-meta .task-source").exists()).toBe(false);
    expect(wrapper.get(".task-meta").text()).not.toContain("工具执行");
    // 必要的归属与状态仍在折叠行
    expect(wrapper.get(".task-status").text()).toBe("运行中");
    expect(wrapper.get(".task-origin").text()).toBe("布局讨论");

    wrapper.unmount();
  });

  it("工具任务：展开后详情 facts 里「来源」「执行者」仍可查", () => {
    const wrapper = mountItem(taskItem(), true);

    const labels = wrapper.findAll(".task-facts dt").map((dt) => dt.text());
    const values = wrapper.findAll(".task-facts dd").map((dd) => dd.text());
    expect(labels).toContain("来源");
    expect(labels).toContain("执行者");
    expect(values).toContain("工具执行");
    expect(values).toContain("运行 Shell 命令");

    wrapper.unmount();
  });

  it("非工具任务：来源照常显示，不被去重规则误伤", () => {
    const wrapper = mountItem(
      taskItem({
        id: "maintenance:1",
        source: "maintenance",
        title: "记忆整理",
        executor: null,
        origin: "global",
        topicId: null,
        topicTitle: null,
        status: "completed",
      }),
    );

    expect(wrapper.get(".task-meta .task-source").text()).toBe("记忆维护");
    expect(wrapper.get(".task-title").text()).toBe("记忆整理");

    wrapper.unmount();
  });

  it("独立任务同一条规则：标题以「独立任务」开头时折叠行不重复来源", () => {
    const wrapper = mountItem(
      taskItem({
        id: "subagent:42",
        source: "subagent",
        title: "独立任务 · 代码审查",
        executor: "代码审查",
      }),
    );

    expect(wrapper.find(".task-meta .task-source").exists()).toBe(false);
    expect(wrapper.get(".task-title").text()).toBe("独立任务 · 代码审查");

    wrapper.unmount();
  });
});
