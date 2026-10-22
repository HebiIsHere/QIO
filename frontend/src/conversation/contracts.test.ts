import { describe, expect, it } from "vitest";
import {
  TASK_STATUS_LABEL,
  isAttentionStatus,
  normalizeTaskStatus,
  taskIdentity,
} from "./contracts";
import {
  attachmentPreviewId,
  replyCodePreviewId,
  taskOutputPreviewId,
} from "./previewTypes";

describe("任务状态映射（不猜、不美化）", () => {
  it("未知原状态映射为 unknown，绝不默认成运行中", () => {
    expect(normalizeTaskStatus("novel_state")).toBe("unknown");
    expect(normalizeTaskStatus("")).toBe("unknown");
    expect(normalizeTaskStatus(null)).toBe("unknown");
    expect(normalizeTaskStatus(undefined)).toBe("unknown");
  });

  it("部分完成、取消、预算耗尽各自独立", () => {
    expect(normalizeTaskStatus("partially_completed")).toBe("partial");
    expect(normalizeTaskStatus("partial")).toBe("partial");
    expect(normalizeTaskStatus("cancelled")).toBe("cancelled");
    expect(normalizeTaskStatus("budget_exhausted")).toBe("budget_exhausted");
    expect(normalizeTaskStatus("completed")).toBe("completed");
  });

  it("排队与运行分开（提交即排队，不伪造开始）", () => {
    expect(normalizeTaskStatus("queued")).toBe("queued");
    expect(normalizeTaskStatus("pending")).toBe("queued");
    expect(normalizeTaskStatus("running")).toBe("running");
    expect(normalizeTaskStatus("waiting_approval")).toBe("waiting_approval");
  });

  it("需要处理的状态只有等待审批/等待处理/失败", () => {
    expect(isAttentionStatus("failed")).toBe(true);
    expect(isAttentionStatus("waiting_approval")).toBe(true);
    expect(isAttentionStatus("waiting_user")).toBe(true);
    expect(isAttentionStatus("partial")).toBe(false);
    expect(isAttentionStatus("completed")).toBe(false);
  });

  it("每个展示状态都有中文标签，不给用户看内部枚举", () => {
    for (const [status, label] of Object.entries(TASK_STATUS_LABEL)) {
      expect(label.length).toBeGreaterThan(0);
      expect(label).not.toBe(status);
    }
    expect(TASK_STATUS_LABEL.unknown).toContain("无法确认");
  });

  it("任务身份是 source:sourceId，稳定且可去重", () => {
    expect(taskIdentity("subagent", "task_1")).toBe("subagent:task_1");
    expect(taskIdentity("dev_task", "d1")).toBe("dev_task:d1");
  });
});

describe("预览身份（不能只用文件名）", () => {
  it("同一附件同一版本复用标签，版本不同是两个身份", () => {
    expect(attachmentPreviewId("a1", "v1")).toBe("attachment:a1@v1");
    expect(attachmentPreviewId("a1", "v2")).not.toBe(attachmentPreviewId("a1", "v1"));
    expect(attachmentPreviewId("a1", null)).toBe("attachment:a1");
  });

  it("同名不同文件不会合并", () => {
    expect(attachmentPreviewId("a1", "v1")).not.toBe(attachmentPreviewId("a2", "v1"));
  });

  it("不同回复里的代码块有各自稳定身份", () => {
    expect(replyCodePreviewId("m1", 0)).toBe("replycode:m1:0");
    expect(replyCodePreviewId("m1", 1)).not.toBe(replyCodePreviewId("m1", 0));
    expect(replyCodePreviewId("m2", 0)).not.toBe(replyCodePreviewId("m1", 0));
  });

  it("任务成果身份带任务 id，不同任务的同名文件不合并", () => {
    expect(taskOutputPreviewId("t1", "a1")).toBe("task:t1:a1");
    expect(taskOutputPreviewId("t2", "a1")).not.toBe(taskOutputPreviewId("t1", "a1"));
  });
});
