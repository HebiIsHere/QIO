/**
 * 一轮 = 一个过程区域（契约 §1.5）：
 *
 * * 状态行是系统事实（受理中 / 运行中 / 等待确认 / 已停止 / 已完成 · 耗时）；
 * * 当前阶段突出显示，历史可展开（阶段顺序 + 历次说明 + 关联工具）；
 * * 完成 / 失败 / 停止自动收起，但**用户手动展开过、或正在上翻阅读时不动**；
 * * legacy 记录平铺，不伪造阶段；同一段过程文字只出现一次；
 * * 内联审批复用既有 approvals store（同一时刻只允许一套按钮）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import TurnProcess from "../TurnProcess.vue";
import { useApprovalsStore } from "../../stores/approvals";
import {
  useSessionStore,
  type StreamMessage,
  type TurnFacts,
  type TurnStage,
} from "../../stores/session";
import { resetProcessState } from "../../stores/turnProcess";

const { respondApproval, getTrace } = vi.hoisted(() => ({
  respondApproval: vi.fn((..._args: unknown[]) => Promise.resolve({ ok: true })),
  getTrace: vi.fn((..._args: unknown[]) =>
    Promise.resolve({ turn_id: "turn_1", duration_ms: 0, phases: {} }),
  ),
}));

vi.mock("../../services/api", () => ({
  api: { respondApproval, getTrace, getUISettings: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
  return {
    content: "",
    contentType: "text",
    createdAt: "2026-10-06T08:00:00+00:00",
    ...partial,
  } as StreamMessage;
}

function stage(partial: Partial<TurnStage> & { stageId: string }): TurnStage {
  return {
    index: 1,
    name: "",
    status: "running",
    notes: [],
    callIds: [],
    ...partial,
  };
}

function facts(partial: Partial<TurnFacts> = {}): TurnFacts {
  return {
    turnId: "turn_1",
    status: "completed",
    durationMs: null,
    queueMs: null,
    startedAt: null,
    endedAt: null,
    ...partial,
  };
}

function mountProcess(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(TurnProcess, {
    props: {
      turnId: "turn_1",
      items: [],
      stages: [],
      facts: null,
      running: false,
      ...props,
    },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
  return { w, session, pinia };
}

const RUNNING_STAGE = stage({
  stageId: "st_ab12_1",
  index: 1,
  name: "读取仓库结构",
  status: "running",
  notes: [
    { narrativeId: "msg_1", text: "正在读取仓库结构", kind: "progress", at: "2026-10-06T08:00:00+00:00" },
  ],
  callIds: ["c1"],
});

const RUNNING_TOOL = msg({
  id: "t1",
  role: "tool",
  callId: "c1",
  toolName: "read_file",
  toolStatus: "running",
  stageId: "st_ab12_1",
  presentation: { title: "读取文件" },
});

beforeEach(() => {
  resetProcessState();
  respondApproval.mockClear();
  getTrace.mockClear();
});

describe("过程区：状态行与当前阶段", () => {
  it("运行中：状态行是系统事实（工具行），当前阶段突出显示，工具就在当前阶段里", async () => {
    const { w, session } = mountProcess({ items: [RUNNING_TOOL], stages: [RUNNING_STAGE], running: true });
    session.turnPhase = "generating";
    await nextTick();

    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("运行中");
    expect(status).toContain("读取文件 · 1 项工具运行中");
    expect(w.find(".tp-current").text()).toContain("读取仓库结构");
    // 运行中的工具看得见（不是被收起藏起来）
    expect(w.findAll(".tool-card")).toHaveLength(1);
    w.unmount();
  });

  it("完成：自动收起历史，状态行与总耗时仍然在（总耗时来自 TURN_END）", async () => {
    const done = stage({ ...RUNNING_STAGE, status: "done" });
    const { w } = mountProcess({
      items: [RUNNING_TOOL],
      stages: [done],
      running: false,
      facts: facts({ status: "completed", durationMs: 12345 }),
    });
    await nextTick();
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-status']").text()).toContain("已完成");
    expect(w.find("[data-test='turn-process-duration']").text()).toContain("12 秒");
    w.unmount();
  });

  it("失败：状态说失败，历史收起（失败事实留在状态行）", async () => {
    const failedTool = msg({ ...RUNNING_TOOL, id: "t2", callId: "c2", toolStatus: "failed", toolError: "boom" });
    const { w } = mountProcess({
      items: [failedTool],
      stages: [stage({ ...RUNNING_STAGE, status: "done" })],
      running: false,
      facts: facts({ status: "failed", durationMs: 500 }),
    });
    await nextTick();
    const status = w.find("[data-test='turn-process-status']").text();
    expect(status).toContain("已失败");
    expect(status).toContain("1 项失败");
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(false);
    w.unmount();
  });
});

describe("展开状态：自动收起 vs 用户的选择", () => {
  it("用户手动展开后，轮次结束不强制收起", async () => {
    const { w } = mountProcess({ items: [RUNNING_TOOL], stages: [RUNNING_STAGE], running: true });
    await nextTick();
    const toggle = w.find("[data-test='turn-process-toggle']");
    await toggle.trigger("click"); // 收起（记为用户手动）
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(false);
    await toggle.trigger("click"); // 再展开（用户正在阅读历史）
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);

    await w.setProps({ running: false, facts: facts({ status: "completed", durationMs: 1000 }) });
    await nextTick();
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);
    w.unmount();
  });

  it("用户正在上翻阅读（没在跟随底部）时，普通状态更新不强制收起", async () => {
    const { w, session } = mountProcess({ items: [RUNNING_TOOL], stages: [RUNNING_STAGE], running: true });
    await nextTick();
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);
    session.streamFollowing = false;
    await w.setProps({ running: false, facts: facts({ status: "completed", durationMs: 1000 }) });
    await nextTick();
    expect(w.find("[data-test='turn-process-history']").exists()).toBe(true);
    w.unmount();
  });
});

describe("同一内容只出现一次 / legacy 平铺", () => {
  it("阶段说明与同一段中间话只渲染一次", async () => {
    const text = "正在读取仓库结构";
    const interim = msg({
      id: "a1",
      role: "assistant",
      content: text,
      interim: true,
      streaming: true,
      stageId: "st_ab12_1",
    });
    const { w } = mountProcess({ items: [interim], stages: [RUNNING_STAGE], running: true });
    await nextTick();
    const html = w.html();
    expect(html.split(text).length - 1).toBe(1);
    w.unmount();
  });

  it("legacy（没有阶段）：平铺渲染旧叙事行，不伪造阶段", async () => {
    const narrative = msg({
      id: "n1",
      role: "narrative",
      content: "旧版过程说明",
      narrativeKind: "progress",
      narrativeCallIds: ["c1"],
    });
    const tool = msg({ id: "t1", role: "tool", callId: "c1", toolName: "read_file", toolStatus: "success" });
    const { w } = mountProcess({ items: [narrative, tool], stages: [], running: true });
    await nextTick();
    expect(w.find(".tp-stage").exists()).toBe(false);
    expect(w.find("[data-test='turn-process-history']").text()).toContain("旧版过程说明");
    expect(w.findAll(".tool-card")).toHaveLength(1);
    w.unmount();
  });
});

describe("内联审批：复用既有 approvals store，同一时刻只允许一套按钮", () => {
  function withPendingTurn() {
    const mounted = mountProcess({ items: [], stages: [], running: true });
    mounted.session.activeTurnId = "turn_1";
    mounted.session.turnRunning = true;
    return mounted;
  }

  it("属于当前轮的审批在过程区内联显示，按钮走 respondById（不是自己发请求）", async () => {
    const { w } = withPendingTurn();
    const approvals = useApprovalsStore();
    approvals.enqueue(
      "ap_1",
      "tool_execution",
      { description: "删除临时目录", capabilities: ["副作用：destructive"] },
      { turnId: "turn_1", autoOpen: false },
    );
    await nextTick();

    const card = w.find("[data-test='turn-process-approval']");
    expect(card.exists()).toBe(true);
    expect(card.text()).toContain("删除临时目录");
    expect(card.text()).toContain("副作用：destructive");
    // 内联声明生效：全局入口 / 弹窗不得再对同一条显示按钮
    expect(approvals.inlineClaimed).toBe(true);

    await card.findAll(".qio-btn")[0]?.trigger("click");
    expect(respondApproval).toHaveBeenCalledWith(
      "ap_1",
      "approved",
      undefined,
      expect.objectContaining({ turnId: "turn_1" }),
    );
    w.unmount();
  });

  it("不属于当前轮的审批不进过程区（仍走全局入口）", async () => {
    const { w } = withPendingTurn();
    useApprovalsStore().enqueue("ap_2", "tool_execution", { description: "别的轮" }, { turnId: "turn_other", autoOpen: false });
    await nextTick();
    expect(w.find("[data-test='turn-process-approval']").exists()).toBe(false);
    expect(useApprovalsStore().inlineClaimed).toBe(false);
    w.unmount();
  });
});
