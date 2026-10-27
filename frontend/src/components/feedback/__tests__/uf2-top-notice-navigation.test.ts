/**
 * A · F02：TopNotice 点击接入 C 的 openTaskNoticeTargets(taskIds)。
 *
 * 修复前行为：组件自己展开左栏 + 逐条 focusTask + `slice(0, 8)` 截断，且无论定位是否
 * 成功都关闭通知 —— 失败时用户什么也看不到。这里用 vi.mock 替代 C 的跨页导航服务，
 * 只锁定 TopNotice 的调用契约与失败处理（真实跨页导航由 C 的实现 + Lead 集成验证）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import TopNotice from "../TopNotice.vue";
import { useNotificationsStore } from "../../../stores/notifications";

const navigation = vi.hoisted(() => ({
  openTaskNoticeTargets: vi.fn(),
}));

// 路径按测试文件解析：src/components/feedback/__tests__ → src/services/noticeNavigation
vi.mock("../../../services/noticeNavigation", () => ({
  openTaskNoticeTargets: navigation.openTaskNoticeTargets,
}));

/** 10 个任务身份：远超旧的 `slice(0, 8)` 截断，用来锁“定位全部任务”。 */
const TEN_TASKS = [
  "subagent:a",
  "subagent:b",
  "subagent:c",
  "subagent:d",
  "subagent:e",
  "subagent:f",
  "subagent:g",
  "subagent:h",
  "subagent:i",
  "subagent:j",
];

function setup() {
  setActivePinia(createPinia());
  const notifications = useNotificationsStore();
  notifications.enqueue({
    key: "task-failed:nav",
    kind: "task_failed",
    text: "10 项任务失败了",
    taskIds: TEN_TASKS,
    mergeable: false,
    focus: true,
  });
  const w = mount(TopNotice, { attachTo: document.body });
  return { notifications, w };
}

describe("F02 TopNotice 点击 → openTaskNoticeTargets", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    navigation.openTaskNoticeTargets.mockReset();
  });

  it("调用 C 的导航服务并传入全部任务（不截断），成功才关闭通知", async () => {
    navigation.openTaskNoticeTargets.mockResolvedValue({
      ok: true,
      reason: "ok",
      taskIds: TEN_TASKS,
      message: "",
    });
    const { notifications, w } = setup();

    await w.find(".uf-notice__body").trigger("click");
    await flushPromises();

    expect(navigation.openTaskNoticeTargets).toHaveBeenCalledTimes(1);
    // 全部 10 个任务原样传入：不再 slice(0, 8)，也不逐条 focusTask
    expect(navigation.openTaskNoticeTargets.mock.calls[0][0]).toEqual(TEN_TASKS);
    expect(notifications.hasActive).toBe(false);
    expect(w.find(".uf-notice__hint").exists()).toBe(false);
    w.unmount();
  });

  it("blocked-by-modal：保留通知、不打断中央弹窗，并在通知内给出 role=status 说明", async () => {
    const message = "现在有需要先处理的中央弹窗：先处理它，通知和对应的任务记录都还在。";
    navigation.openTaskNoticeTargets.mockResolvedValue({
      ok: false,
      reason: "blocked-by-modal",
      taskIds: TEN_TASKS,
      message,
    });
    const { notifications, w } = setup();

    await w.find(".uf-notice__body").trigger("click");
    await flushPromises();

    // 不静默关闭：通知仍在，任务目标也仍在
    expect(notifications.hasActive).toBe(true);
    expect(notifications.active?.members.length).toBe(TEN_TASKS.length);
    const hint = w.find(".uf-notice__hint");
    expect(hint.exists()).toBe(true);
    expect(hint.attributes("role")).toBe("status");
    expect(hint.text()).toBe(message);
    w.unmount();
  });

  it("navigation-failed：保留通知，把服务返回的中文说明显示出来（不静默丢弃）", async () => {
    const message = "任务列表没能显示出来（页面可能正在切换）。可以再点一次通知，或在左侧展开任务列表逐条查看。";
    navigation.openTaskNoticeTargets.mockResolvedValue({
      ok: false,
      reason: "navigation-failed",
      taskIds: TEN_TASKS,
      message,
    });
    const { notifications, w } = setup();

    await w.find(".uf-notice__body").trigger("click");
    await flushPromises();

    expect(notifications.hasActive).toBe(true);
    expect(w.find(".uf-notice__hint").text()).toBe(message);
    expect(w.find(".uf-notice__hint").attributes("role")).toBe("status");
    w.unmount();
  });

  it("导航服务抛错：保留通知并给出兜底说明；再次点击成功才关闭", async () => {
    navigation.openTaskNoticeTargets.mockRejectedValueOnce(new Error("路由尚未就绪"));
    const { notifications, w } = setup();

    await w.find(".uf-notice__body").trigger("click");
    await flushPromises();
    expect(notifications.hasActive).toBe(true);
    const hint = w.find(".uf-notice__hint");
    expect(hint.exists()).toBe(true);
    expect(hint.text()).toContain("任务列表暂时无法打开");
    expect(hint.text()).toContain("路由尚未就绪");

    // 第二次点击：服务恢复 → 成功关闭
    navigation.openTaskNoticeTargets.mockResolvedValueOnce({
      ok: true,
      reason: "ok",
      taskIds: TEN_TASKS,
      message: "",
    });
    await w.find(".uf-notice__body").trigger("click");
    await flushPromises();
    expect(navigation.openTaskNoticeTargets).toHaveBeenCalledTimes(2);
    expect(notifications.hasActive).toBe(false);
    w.unmount();
  });
});
