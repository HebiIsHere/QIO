/**
 * B · 公共接口冒烟：仲裁 / 通知 / 全局异常三个 store 必须先立住，A/C/D 才能接入。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useModalArbiterStore } from "../modalArbiter";
import {
  NOTICE_MERGE_WINDOW_MS,
  NOTICE_VISIBLE_MS,
  useNotificationsStore,
} from "../notifications";
import { useGlobalIssuesStore } from "../globalIssues";

describe("modalArbiter：同一时刻只允许一个中央弹窗", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("打开一个后，同优先级/更低优先级的再打开会被拒绝", () => {
    const arbiter = useModalArbiterStore();
    expect(arbiter.open({ id: "approval-a", kind: "approval" })).toBe(true);
    expect(arbiter.open({ id: "confirm-b", kind: "confirm" })).toBe(false);
    expect(arbiter.count).toBe(1);
    expect(arbiter.active?.id).toBe("approval-a");
    // 幂等：同一个 id 重复打开仍然成功，不重复入栈
    expect(arbiter.open({ id: "approval-a", kind: "approval" })).toBe(true);
    expect(arbiter.count).toBe(1);
  });

  it("更高优先级可以叠上去；关闭后低层恢复活动", () => {
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "approval-a", kind: "approval" });
    expect(arbiter.open({ id: "impact-b", kind: "impact" })).toBe(true);
    expect(arbiter.active?.id).toBe("impact-b");
    arbiter.close("impact-b");
    expect(arbiter.active?.id).toBe("approval-a");
    expect(arbiter.hasModal).toBe(true);
    arbiter.close("approval-a");
    expect(arbiter.hasModal).toBe(false);
  });

  it("suspendAll 只隐藏不卸载；hiddenBy 给出挂起者，resumeAll 恢复", () => {
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "impact-b", kind: "impact" });
    arbiter.suspendAll("close-ask");
    expect(arbiter.hiddenBy("impact-b")).toBe("close-ask");
    expect(arbiter.hasModal).toBe(true);
    expect(arbiter.blocksAutoApproval).toBe(true);
    arbiter.resumeAll();
    expect(arbiter.hiddenBy("impact-b")).toBeNull();
  });
});

describe("notifications：单条、合并、去重、弹窗期间排队", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.useFakeTimers();
  });

  it("非合并通知立即显示，5s 后自动收起", () => {
    const notifications = useNotificationsStore();
    notifications.enqueue({ key: "setup:1", kind: "setup_incomplete", text: "还没设置完" });
    expect(notifications.active?.key).toBe("setup:1");
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 1);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(2);
    expect(notifications.hasActive).toBe(false);
  });

  it("500ms 内的任务失败合并为一条「N 项任务失败了」，同一事件身份只计一次", () => {
    const notifications = useNotificationsStore();
    notifications.enqueue({ key: "t1", kind: "task_failed", text: "任务一失败了", taskIds: ["task-1"] });
    notifications.enqueue({ key: "t2", kind: "task_failed", text: "任务二失败了", taskIds: ["task-2"] });
    // 合并窗口内不显示（等 500ms 一次性显示合成文案）
    expect(notifications.hasActive).toBe(false);
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 1);
    expect(notifications.hasActive).toBe(true);
    expect(notifications.active?.members.length).toBe(2);
    // 重复的同一事件身份：不新增成员
    notifications.enqueue({ key: "t1", kind: "task_failed", text: "任务一失败了", taskIds: ["task-1"] });
    expect(notifications.active?.members.length).toBe(2);
  });

  it("弹窗期间排队；弹窗关闭后先过滤已恢复的成员再显示", () => {
    const arbiter = useModalArbiterStore();
    const notifications = useNotificationsStore();
    arbiter.open({ id: "approval-a", kind: "approval" });
    let recovered = false;
    notifications.enqueue({
      key: "t1",
      kind: "task_failed",
      text: "任务一失败了",
      taskIds: ["task-1"],
      mergeable: false,
      stillRelevant: () => !recovered,
    });
    expect(notifications.hasActive).toBe(false);
    recovered = true;
    arbiter.close("approval-a");
    notifications.flushQueue();
    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices.length).toBe(0);
  });
});

describe("globalIssues：一个入口、根因去重、部分恢复", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("同一根因重复上报只计一次，部分恢复只移除该项", () => {
    const issues = useGlobalIssuesStore();
    issues.upsert({ id: "sse_disconnected", kind: "sse_disconnected", title: "连接断开", reason: "第一次", actions: [], diagnostics: [], severity: "error", at: Date.now() });
    issues.upsert({ id: "sse_disconnected", kind: "sse_disconnected", title: "连接断开", reason: "第二次", actions: [], diagnostics: [], severity: "error", at: Date.now() });
    issues.upsert({ id: "no_credentials", kind: "no_credentials", title: "没有可用凭据", reason: "", actions: [], diagnostics: [], severity: "warning", at: Date.now() });
    expect(issues.count).toBe(2);
    expect(issues.issues.find((issue) => issue.id === "sse_disconnected")?.reason).toBe("第二次");
    issues.resolve("sse_disconnected");
    expect(issues.count).toBe(1);
    issues.resolve("no_credentials");
    expect(issues.hasIssues).toBe(false);
  });
});