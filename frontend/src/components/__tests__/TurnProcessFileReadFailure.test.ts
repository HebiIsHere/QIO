/**
 * #12 过程区状态行的失败摘要（文件读取失败）：
 *
 * * 同一失败只给一句简短可读结论（如「这次没读到文件内容（1 个文件）」），
 *   不把工具卡里的整段真实原因 / 下一步再念一遍；
 * * 多个文件按真实名称去重计数；同名文件多次失败仍算 1 个文件；
 * * 其它工具失败保持既有口径（类别标签），不从错误文本猜；
 * * 与后端已去重的答案注记合起来，主体提示在 DOM 里只出现一次。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/TurnProcessFileReadFailure.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h, nextTick } from "vue";
import TurnProcess from "../TurnProcess.vue";
import MessageItem from "../MessageItem.vue";
import {
  useSessionStore,
  type StreamMessage,
  type TurnFacts,
  type TurnStage,
} from "../../stores/session";
import { resetProcessState } from "../../stores/turnProcess";

const { getTrace, respondApproval } = vi.hoisted(() => ({
  getTrace: vi.fn(async () => ({ turn_id: "turn_1", duration_ms: 0, phases: {} })),
  respondApproval: vi.fn(async () => ({ ok: true })),
}));

vi.mock("../../services/api", () => ({
  api: { getTrace, respondApproval, getUISettings: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
  return {
    content: "",
    contentType: "text",
    createdAt: "2026-11-12T10:00:00+00:00",
    ...partial,
  } as StreamMessage;
}

function stage(partial: Partial<TurnStage> & { stageId: string }): TurnStage {
  return { index: 1, name: "", status: "done", notes: [], callIds: [], ...partial };
}

function facts(partial: Partial<TurnFacts> = {}): TurnFacts {
  return {
    turnId: "turn_1",
    status: "completed",
    durationMs: 2600,
    queueMs: 0,
    startedAt: null,
    endedAt: null,
    reason: null,
    reasonCode: null,
    stoppedBy: null,
    actions: [],
    errorText: null,
    ...partial,
  };
}

const RAW_INSTRUCTION = "可以让用户重新上传附件；可以先不传这个文件继续回答。";

function fileReadTool(
  id: string,
  name: string,
  over: Partial<StreamMessage> = {},
): StreamMessage {
  return msg({
    id,
    role: "tool",
    callId: "c_" + id,
    stageId: "st_1",
    toolName: "read_attachment",
    toolStatus: "failed",
    toolOk: false,
    toolCode: "file_read_failed",
    toolCategory: "io_error",
    toolCategoryLabel: "读写错误",
    toolError: RAW_INSTRUCTION,
    toolFileRead: {
      attachmentId: "att_" + id,
      name,
      reason: "文件内容读不出来（压缩结构损坏）",
      nextStep: "请重新上传这个文件，或先把内容贴成文本。",
      diagnostic: "zip: bad central directory",
    },
    presentation: { title: "读取附件", tool: "read_attachment" },
    ...over,
  });
}

function mountProcess(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(TurnProcess, {
    props: { turnId: "turn_1", items: [], stages: [], facts: null, running: false, ...props },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
  return { w, session, pinia };
}

function statusText(w: ReturnType<typeof mountProcess>["w"]): string {
  return w.find("[data-test='turn-process-status']").text();
}

beforeEach(() => {
  resetProcessState();
  respondApproval.mockClear();
});

describe("文件读取失败：过程区状态行只给一句短结论", () => {
  it("显示「这次没读到文件内容（1 个文件）」，不重复工具卡里的整段原因", async () => {
    const { w } = mountProcess({
      items: [fileReadTool("a", "季度报告.docx")],
      stages: [stage({ stageId: "st_1", callIds: ["c_a"] })],
      facts: facts(),
    });
    await nextTick();
    const status = statusText(w);
    expect(status).toContain("这次没读到文件内容（1 个文件）");
    expect(status.split("这次没读到文件内容").length - 1).toBe(1);
    // 原因 / 下一步 / 模型内部指令都不进第一层状态行
    expect(status).not.toContain("压缩结构损坏");
    expect(status).not.toContain("请重新上传");
    expect(status).not.toContain("可以让用户");
    // 逐项工具卡默认不在 DOM 里（历史抽屉收起），失败事实没有丢
    expect(w.findAll(".tool-card")).toHaveLength(0);
    w.unmount();
  });

  it("多个不同文件按名称计数；同名文件多次失败仍算 1 个文件", async () => {
    const twoFiles = mountProcess({
      items: [fileReadTool("a", "报告.pdf"), fileReadTool("b", "附件.zip")],
      stages: [stage({ stageId: "st_1", callIds: ["c_a", "c_b"] })],
      facts: facts(),
    });
    await nextTick();
    expect(statusText(twoFiles.w)).toContain("这次没读到文件内容（2 个文件）");
    twoFiles.w.unmount();

    const sameFile = mountProcess({
      items: [fileReadTool("a", "报告.pdf"), fileReadTool("b", "报告.pdf")],
      stages: [stage({ stageId: "st_1", callIds: ["c_a", "c_b"] })],
      facts: facts(),
    });
    await nextTick();
    expect(statusText(sameFile.w)).toContain("这次没读到文件内容（1 个文件）");
    sameFile.w.unmount();
  });

  it("与其它工具失败同时存在：两种结论各自成立，不互相吞掉", async () => {
    const searchFailed = msg({
      id: "t_s",
      role: "tool",
      callId: "c_s",
      stageId: "st_1",
      toolName: "web_search",
      toolStatus: "failed",
      toolOk: false,
      toolCategory: "timeout",
      toolCategoryLabel: "超时",
      toolError: "connection timed out",
      presentation: { title: "网络搜索", tool: "web_search" },
    });
    const { w } = mountProcess({
      items: [fileReadTool("a", "季度报告.docx"), searchFailed],
      stages: [stage({ stageId: "st_1", callIds: ["c_a", "c_s"] })],
      facts: facts(),
    });
    await nextTick();
    const status = statusText(w);
    expect(status).toContain("这次没读到文件内容（1 个文件）");
    expect(status).toContain("网络搜索执行失败（超时）");
    expect(status).not.toContain("timed out");
    w.unmount();
  });

  it("展开历史后工具卡给完整事实，状态行仍是短结论（不重复整段）", async () => {
    const { w } = mountProcess({
      items: [fileReadTool("a", "季度报告.docx")],
      stages: [stage({ stageId: "st_1", callIds: ["c_a"] })],
      facts: facts(),
    });
    await nextTick();
    await w.find("[data-test='turn-process-toggle']").trigger("click");
    await nextTick();
    const card = w.find(".tool-card");
    expect(card.exists()).toBe(true);
    expect(card.find(".tool-fail-line").text()).toContain("这次没读到文件内容：季度报告.docx");
    expect(card.text()).toContain("压缩结构损坏");
    expect(card.text()).not.toContain("可以让用户");
    const status = statusText(w);
    expect(status).not.toContain("压缩结构损坏");
    w.unmount();
  });
});

describe("主体提示只出现一次（与后端已去重的答案注记合计）", () => {
  it("过程区短结论 + 答案注记（后端已去重）= DOM 里恰好一次「这次没读到文件内容」", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const items = [fileReadTool("a", "季度报告.docx")];
    const stages = [stage({ stageId: "st_1", callIds: ["c_a"] })];
    const turnFacts = facts();
    // 后端已把重复的「这次没读到文件内容」从答案注记里去掉：
    // 注记只保留可核对的事实位置，不再重复主体提示。
    const answer = msg({
      id: "answer_1",
      role: "assistant",
      contentType: "text",
      content:
        "我先读了这份文件。\n\n—— 系统核对（后端事实，不是模型的说法）：\n" +
        "本轮有 1 次文件读取没有完成，真实原因与下一步在过程记录与工具诊断里。",
    });
    const Host = defineComponent({
      render() {
        return h("div", [ 
          h(TurnProcess, {
            turnId: "turn_1",
            items,
            stages,
            facts: turnFacts,
            running: false,
          }),
          h(MessageItem, { message: answer }),
        ]);
      },
    });
    const w = mount(Host, {
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });
    await nextTick();
    const text = w.text();
    expect(text.split("这次没读到文件内容").length - 1).toBe(1);
    // 注记本身还在（独立系统事实区域），没有被前端吞掉
    expect(w.find("[data-test='answer-system-note']").exists()).toBe(true);
    w.unmount();
  });
});
