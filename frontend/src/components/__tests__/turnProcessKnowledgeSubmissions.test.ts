/**
 * 问题 3 回归：同一轮多次知识提交必须分别保留结果。
 *
 * 被钉住的行为（默认可见的状态行）：
 * 1. A 提交失败 + B 提交等待审批（无身份关联）→ 两件事都要说，不能只显示等待审批；
 * 2. 同一身份（后端 toolKnowledgeKey）的重试成功 → 旧失败不再念（该项已更新）；
 * 3. 不同身份的成功不能抹掉另一条失败；
 * 4. 没有任何身份时不得默认是重试（A 失败 + B 成功仍要报 A）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import TurnProcess from "../TurnProcess.vue";
import {
  type StreamMessage,
  type TurnFacts,
  useSessionStore,
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

function knowledge(
  id: string,
  callId: string,
  opts: {
    failed?: boolean;
    awaiting?: boolean;
    code?: string;
    key?: string;
  } = {},
): StreamMessage {
  const failed = opts.failed === true;
  return {
    id,
    role: "tool",
    content: "",
    contentType: "tool",
    createdAt: "2026-11-09T10:00:00+00:00",
    callId,
    toolName: "propose_knowledge",
    presentation: { title: "提交知识", tool: "propose_knowledge" },
    toolStatus: failed ? "failed" : "success",
    toolOk: !failed,
    ...(opts.awaiting
      ? { toolResultStatus: "awaiting_approval", toolCode: "awaiting_user_approval" }
      : {}),
    ...(failed ? { toolCode: opts.code ?? "evidence_not_found" } : {}),
    ...(opts.key ? { toolKnowledgeKey: opts.key } : {}),
  } as StreamMessage;
}

function facts(): TurnFacts {
  return {
    turnId: "turn_1",
    status: "completed",
    durationMs: 1200,
    queueMs: 0,
    startedAt: null,
    endedAt: null,
    reason: null,
    reasonCode: null,
    stoppedBy: null,
    actions: [],
    errorText: null,
  };
}

function mountProcess(items: StreamMessage[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  useSessionStore();
  return mount(TurnProcess, {
    props: {
      turnId: "turn_1",
      items,
      stages: [],
      facts: facts(),
      running: false,
    },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

function statusText(w: ReturnType<typeof mountProcess>): string {
  return w.find('[data-test="turn-process-status"]').text();
}

beforeEach(() => {
  resetProcessState();
  respondApproval.mockClear();
});

describe("同一轮多条知识提交分别保留结局", () => {
  it("A 失败 + B 等待审批：摘要同时说明失败与等待审批", () => {
    const w = mountProcess([
      knowledge("t_a", "c_a", { failed: true }),
      knowledge("t_b", "c_b", { awaiting: true }),
    ]);
    const text = statusText(w);
    expect(text).toContain("知识提交等待你确认（尚未生效）");
    expect(text).toContain("知识提交未成功");
  });

  it("同一身份重试成功：不再念已被解决的旧失败", () => {
    const w = mountProcess([
      knowledge("t_a", "c_a", { failed: true, key: "fp_same" }),
      knowledge("t_b", "c_b", { awaiting: true, key: "fp_same" }),
    ]);
    const text = statusText(w);
    expect(text).toContain("知识提交等待你确认（尚未生效）");
    expect(text).not.toContain("知识提交未成功");
  });

  it("不同身份的成功不能抹掉另一条失败", () => {
    const w = mountProcess([
      knowledge("t_a", "c_a", { failed: true, key: "fp_a" }),
      knowledge("t_b", "c_b", { awaiting: true, key: "fp_b" }),
    ]);
    const text = statusText(w);
    expect(text).toContain("知识提交未成功");
    expect(text).toContain("知识提交等待你确认（尚未生效）");
  });

  it("没有身份关联时不默认是重试：A 失败 + B 成功仍报 A", () => {
    const w = mountProcess([
      knowledge("t_a", "c_a", { failed: true }),
      knowledge("t_b", "c_b", { awaiting: true }),
    ]);
    const text = statusText(w);
    expect(text).toContain("知识提交未成功");
  });
});
