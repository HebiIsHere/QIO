/**
 * A · 通知成员身份/文案/目标（F03）、挂起恢复前重新过滤（F04）、
 * 弹窗期间分时失败统一汇总（F05）、hover/focus 分别暂停（F06）。
 *
 * 这些断言在修复前应当失败：通知文案被「最后一个成员」覆盖、恢复路径跳过有效性核对、
 * 弹窗结束不重新合并、暂停只记一个 key。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import {
  NOTICE_MERGE_WINDOW_MS,
  NOTICE_VISIBLE_MS,
  noticeDisplayText,
  noticeFullText,
  noticeTaskCount,
  noticeTaskIds,
  useNotificationsStore,
  type AppNoticeInput,
} from "../notifications";

function setup() {
  setActivePinia(createPinia());
  return { notifications: useNotificationsStore() };
}

function failNotice(
  over: Partial<AppNoticeInput> & Pick<AppNoticeInput, "key" | "text">,
): AppNoticeInput {
  return {
    kind: "task_failed" as const,
    mergeable: true,
    focus: true,
    ...over,
  };
}

/** 入队一条任务失败通知（默认合并窗口内的任务失败）。 */
function enqueueFailure(
  notifications: ReturnType<typeof useNotificationsStore>,
  key: string,
  taskId: string,
  text: string,
  stillRelevant: () => boolean = () => true,
) {
  notifications.enqueue(
    failNotice({
      key,
      text,
      taskIds: [taskId],
      stillRelevant,
    }),
  );
}

describe("F03 通知成员身份/文案/目标始终一致", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("合并窗口内 A、B 失败；显示前 B 已恢复 → 只剩 A 时就该显示 A 的文案与目标", () => {
    const { notifications } = setup();
    let recoveredB = false;
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了");
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了", () => !recoveredB);
    expect(notifications.notices).toHaveLength(1);

    // 真正显示之前 B 已经恢复：过滤后只剩 A
    recoveredB = true;
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);

    expect(notifications.hasActive).toBe(true);
    const active = notifications.active!;
    expect(active.members.map((member) => member.taskId)).toEqual(["subagent:a"]);
    expect(noticeTaskCount(active)).toBe(1);
    expect(noticeTaskIds(active)).toEqual(["subagent:a"]);
    // 显示文案 / 完整读屏文案都必须来自剩下的那个成员，而不是最后入队的 B
    expect(noticeDisplayText(active)).toBe("「整理资料」失败了");
    expect(noticeFullText(active)).toBe("「整理资料」失败了");
    expect(active.members[0].text).toBe("「整理资料」失败了");
  });

  it("只剩末项（A 恢复、B 留下）：文案与目标都是 B，且成员各自保留自己的文案", () => {
    const { notifications } = setup();
    let recoveredA = false;
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了", () => !recoveredA);
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了");

    recoveredA = true;
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);

    const active = notifications.active!;
    expect(active.members.map((member) => member.taskId)).toEqual(["subagent:b"]);
    expect(noticeDisplayText(active)).toBe("「编译报告」失败了");
    expect(noticeTaskIds(active)).toEqual(["subagent:b"]);
    expect(active.members[0].text).toBe("「编译报告」失败了");
  });

  it("全部成员恢复：整条通知丢弃，不显示过期失败", () => {
    const { notifications } = setup();
    let recovered = false;
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了", () => !recovered);
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了", () => !recovered);
    recovered = true;
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices).toHaveLength(0);
  });

  it("多项部分确认：去掉已处理成员后重新生成数量与目标，成员文案不互相覆盖", () => {
    const { notifications } = setup();
    let handledB = false;
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了");
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了", () => !handledB);
    enqueueFailure(notifications, "ev-c", "subagent:c", "「上传附件」失败了");

    handledB = true;
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);

    const active = notifications.active!;
    expect(noticeTaskCount(active)).toBe(2);
    expect(noticeDisplayText(active)).toBe("2 项任务失败了");
    expect(noticeTaskIds(active)).toEqual(["subagent:a", "subagent:c"]);
    expect(active.members.map((member) => member.text)).toEqual([
      "「整理资料」失败了",
      "「上传附件」失败了",
    ]);
  });

  it("同一任务重试后再次失败使用新事件身份，可再次提醒且目标仍是同一任务", () => {
    const { notifications } = setup();
    enqueueFailure(notifications, "task-failed:t1@v1", "subagent:t1", "「整理资料」失败了（第一次）");
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(noticeDisplayText(notifications.active)).toBe("「整理资料」失败了（第一次）");

    // 500ms 之外的新失败事件（新事件身份）产生新的提醒
    enqueueFailure(notifications, "task-failed:t1@v2", "subagent:t1", "「整理资料」失败了（重试后）");
    expect(notifications.notices).toHaveLength(2);
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS + 10);
    const active = notifications.active!;
    expect(noticeDisplayText(active)).toBe("「整理资料」失败了（重试后）");
    expect(noticeTaskIds(active)).toEqual(["subagent:t1"]);
  });

  it("自动收起后同一事件身份重复推送：不复活旧失败", () => {
    const { notifications } = setup();
    enqueueFailure(notifications, "ev-once", "subagent:x", "「任务」失败了");
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS + 10);
    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices).toHaveLength(0);

    // 同一个事件身份重放：已经是历史事件，不再弹
    enqueueFailure(notifications, "ev-once", "subagent:x", "「任务」失败了");
    expect(notifications.notices).toHaveLength(0);
    expect(notifications.hasActive).toBe(false);
  });
});

describe("F04 挂起恢复前重新核对相关性（与首次显示同一套过滤）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("已显示 → 弹窗挂起 → 期间问题解决 → 关闭后不再恢复旧通知", () => {
    const { notifications } = setup();
    let resolved = false;
    notifications.enqueue(
      failNotice({
        key: "ev", text: "「任务」失败了", taskIds: ["subagent:t1"], mergeable: false,
        stillRelevant: () => !resolved,
      }),
    );
    expect(notifications.hasActive).toBe(true);

    notifications.suspendForModal();
    resolved = true;
    notifications.resumeFromModal();

    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices).toHaveLength(0);
  });

  it("挂起期间部分成员恢复：按剩余成员重新生成文案/目标，并继续剩余时间", () => {
    const { notifications } = setup();
    let recoveredB = false;
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了");
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了", () => !recoveredB);
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(noticeTaskCount(notifications.active)).toBe(2);
    // 已展示 1 秒（剩约 4 秒）
    vi.advanceTimersByTime(1000);

    notifications.suspendForModal();
    recoveredB = true;
    notifications.resumeFromModal();

    const active = notifications.active!;
    expect(active).toBeTruthy();
    expect(active.members.map((member) => member.taskId)).toEqual(["subagent:a"]);
    expect(noticeDisplayText(active)).toBe("「整理资料」失败了");
    expect(noticeTaskIds(active)).toEqual(["subagent:a"]);
    // 继续剩余时长（约 4s），不是重新给 5s，也不是立刻收口
    vi.advanceTimersByTime(3900);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(200);
    expect(notifications.hasActive).toBe(false);
  });
});

describe("F05 弹窗期间分时到达的失败统一汇总", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("弹窗期间 A、B 相隔 1.5s 失败：结束后只有一条「2 项任务失败了」", () => {
    const { notifications } = setup();
    notifications.suspendForModal();
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了");
    vi.advanceTimersByTime(1500);
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了");
    // 基线行为：这是两条独立通知（500ms 合并窗口已过）
    expect(notifications.notices).toHaveLength(2);

    notifications.resumeFromModal();
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);

    expect(notifications.notices).toHaveLength(1);
    const active = notifications.active!;
    expect(noticeTaskCount(active)).toBe(2);
    expect(noticeDisplayText(active)).toBe("2 项任务失败了");
    expect(noticeTaskIds(active)).toEqual(["subagent:a", "subagent:b"]);

    // 只提醒一条：5s 后收起，不会接着再弹第二条
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS + 10);
    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices).toHaveLength(0);
  });

  it("旧通知合入新的未显示失败：更新后的汇总获得一次完整 5s 展示机会", () => {
    const { notifications } = setup();
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了");
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    // 已经展示 4.6s，只剩约 400ms
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 400);
    expect(notifications.hasActive).toBe(true);

    notifications.suspendForModal();
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了");
    vi.advanceTimersByTime(1500);
    notifications.resumeFromModal();

    // 合并后的汇总：文案与目标包含新失败
    expect(notifications.notices).toHaveLength(1);
    const active = notifications.active!;
    expect(noticeTaskCount(active)).toBe(2);
    expect(noticeDisplayText(active)).toBe("2 项任务失败了");
    // 给一次完整展示机会：不是只剩 400ms 匆匆消失
    vi.advanceTimersByTime(4000);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(1100);
    expect(notifications.hasActive).toBe(false);
  });

  it("弹窗期间全新入队（从未显示）的失败：从真正显示时起算完整 5s", () => {
    const { notifications } = setup();
    notifications.suspendForModal();
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了");
    vi.advanceTimersByTime(1500);
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了");
    notifications.resumeFromModal();
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 100);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(200);
    expect(notifications.hasActive).toBe(false);
  });

  it("中性引导提醒不并入失败数：先显示引导，失败仍是一条「2 项任务失败了」", () => {
    const { notifications } = setup();
    notifications.suspendForModal();
    notifications.enqueue({ key: "setup", kind: "setup_incomplete", text: "初始设置尚未完成", mergeable: false });
    enqueueFailure(notifications, "ev-a", "subagent:a", "「整理资料」失败了");
    vi.advanceTimersByTime(1500);
    enqueueFailure(notifications, "ev-b", "subagent:b", "「编译报告」失败了");
    notifications.resumeFromModal();
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);

    expect(notifications.active?.kind).toBe("setup_incomplete");
    expect(noticeDisplayText(notifications.active)).toBe("初始设置尚未完成");

    vi.advanceTimersByTime(NOTICE_VISIBLE_MS + 10);
    const active = notifications.active!;
    expect(active?.kind).toBe("task_failed");
    expect(noticeTaskCount(active)).toBe(2);
    expect(noticeDisplayText(active)).toBe("2 项任务失败了");
  });
});

describe("F06 hover 与 focus 暂停原因分开记录", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  function show(notifications: ReturnType<typeof useNotificationsStore>, key = "task-failed:p") {
    notifications.enqueue(
      failNotice({
        key, text: "「任务」失败了", taskIds: ["subagent:p"], mergeable: false,
      }),
    );
    expect(notifications.hasActive).toBe(true);
  }

  it("hover 暂停 + focus 暂停：鼠标离开但焦点仍在 → 不恢复倒计时", () => {
    const { notifications } = setup();
    show(notifications);
    const key = notifications.activeKey!;

    notifications.pause(key, "hover");
    notifications.pause(key, "focus");
    expect(notifications.pausedKeys).toContain(key);

    notifications.resume(key, "hover");
    // 键盘焦点仍在通知内：仍然暂停
    expect(notifications.pausedKeys).toContain(key);
    vi.advanceTimersByTime(6000);
    expect(notifications.hasActive).toBe(true);

    // 焦点也离开后，才按剩余时长继续
    notifications.resume(key, "focus");
    expect(notifications.pausedKeys).not.toContain(key);
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 100);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(200);
    expect(notifications.hasActive).toBe(false);
  });

  it("焦点离开但鼠标仍在 → 继续暂停（两个方向都成立）", () => {
    const { notifications } = setup();
    show(notifications);
    const key = notifications.activeKey!;

    notifications.pause(key, "focus");
    notifications.pause(key, "hover");
    notifications.resume(key, "focus");
    expect(notifications.pausedKeys).toContain(key);
    vi.advanceTimersByTime(6000);
    expect(notifications.hasActive).toBe(true);

    notifications.resume(key, "hover");
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 100);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(200);
    expect(notifications.hasActive).toBe(false);
  });

  it("重复同一暂停原因不会叠加；旧的无 reason 调用仍按独立原因 manual 工作", () => {
    const { notifications } = setup();
    show(notifications);
    const key = notifications.activeKey!;

    notifications.pause(key, "hover");
    notifications.pause(key, "hover");
    notifications.resume(key, "hover");
    expect(notifications.pausedKeys).not.toContain(key);
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 100);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(200);
    expect(notifications.hasActive).toBe(false);
  });

  it("关闭 / 挂起 / 重置都会清理暂停原因，不会让通知永不消失", () => {
    const { notifications } = setup();
    show(notifications);
    const key = notifications.activeKey!;
    notifications.pause(key, "hover");
    notifications.pause(key, "focus");

    notifications.suspendForModal();
    expect(notifications.pausedKeys).not.toContain(key);
    notifications.resumeFromModal();
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS + 10);
    expect(notifications.hasActive).toBe(false);
  });
});
