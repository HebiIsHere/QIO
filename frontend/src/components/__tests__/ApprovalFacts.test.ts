/**
 * 问题 1：审批事实必须**只有一份**整理（ApprovalFacts 共用），内联卡与弹窗说同样的话。
 *
 * 修复前红：内联卡只取一行 approvalIntent + capabilities ——
 * 模型 explanation 与系统 description 混成一句，真实命令 / 路径 / 工具参数 / 范围 /
 * 风险 / 验证 / 预算入口全都没有。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/ApprovalFacts.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import ApprovalFacts from "../ApprovalFacts.vue";
import ApprovalModal from "../ApprovalModal.vue";
import TurnProcess from "../TurnProcess.vue";
import { approvalFacts, useApprovalsStore, type ApprovalItem } from "../../stores/approvals";
import { useSessionStore } from "../../stores/session";
import { resetProcessState } from "../../stores/turnProcess";

const { respondApproval, getTrace, sendTurn, resendInterruptedTurn } = vi.hoisted(() => ({
  respondApproval: vi.fn(async () => ({ ok: true })),
  getTrace: vi.fn(async () => ({ turn_id: "turn_1", duration_ms: 0, phases: {} })),
  sendTurn: vi.fn(async () => ({ ok: true })),
  resendInterruptedTurn: vi.fn(async () => ({ ok: true })),
}));

vi.mock("../../services/api", () => ({
  api: { respondApproval, getTrace, sendTurn, resendInterruptedTurn, getUISettings: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

/** 一条「简短描述 + 实际命令 + 独立说明」的审批（审计问题 1 的复现形状）。 */
const SHELL_PAYLOAD: Record<string, unknown> = {
  tool: "run_shell",
  description: "想运行一条 shell 命令",
  explanation: "为了确认测试目录是否已清空，需要执行一次删除。",
  access: ["启动一个系统命令进程", "命令内容见下方详情"],
  capabilities: [
    "联网：否",
    "读取文件：否",
    "写入文件：否",
    "启动进程：是",
    "使用凭据：无",
    "副作用：destructive",
  ],
  detail: "实际命令：rm -rf /tmp/qio-scratch\n工作目录：D:\\work\\demo",
  arguments: { cmd: "rm -rf /tmp/qio-scratch", timeout: 30 },
  scope: "once",
  risk: "careful",
  policy_fingerprint: "fp_shell_1",
};

function item(payload: Record<string, unknown> = SHELL_PAYLOAD, kind = "tool_execution"): ApprovalItem {
  return { approval_id: "ap_1", kind, payload, turnId: "turn_1" };
}

beforeEach(() => {
  respondApproval.mockClear();
  getTrace.mockClear();
  resetProcessState();
  document.body.innerHTML = "";
});

describe("approvalFacts()：共用的事实整理", () => {
  it("模型 explanation 与系统 description 分别保留，真实命令 / 路径 / 参数都在", () => {
    const f = approvalFacts(item());
    expect(f).not.toBeNull();
    expect(f?.description).toBe("想运行一条 shell 命令");
    expect(f?.explanation).toContain("为了确认测试目录是否已清空");
    expect(f?.command).toContain("rm -rf /tmp/qio-scratch");
    expect(f?.paths.join(" | ")).toContain("D:\\work\\demo");
    expect(f?.params).toContain("rm -rf /tmp/qio-scratch");
    expect(f?.scopeLabel).toContain("仅这一次");
    expect(f?.risks).toContain("会执行命令");
    expect(f?.highRisk).toBe(true);
    expect(f?.advanced.raw).toContain("fp_shell_1");
  });

  it("验证信息有则显示、没有就是 null（不假装已验证）", () => {
    const none = approvalFacts(item({ description: "x" }));
    expect(none?.verification).toBeNull();
    const sub = approvalFacts(
      item(
        {
          name: "sub",
          tool_type: "subagent",
          test_summary: "n/a (subagent)",
          subagent_budget: { max_iterations: 4, max_tokens: 8000, output_limit_chars: 700 },
        },
        "tool_create",
      ),
    );
    expect(sub?.verification?.verified).toBe(false);
    expect(sub?.verification?.label).toBe("未验证");
    expect(sub?.budget).toEqual({ maxIterations: 4, maxTokens: 8000, outputLimitChars: 700 });
    expect(sub?.isSubagentCreate).toBe(true);
  });

  it("没有 payload / 没有审批时返回 null（不编造事实）", () => {
    expect(approvalFacts(null)).toBeNull();
  });
});

describe("ApprovalFacts.vue：默认可见区说清「做什么 / 为什么 / 真实操作 / 范围 / 风险」", () => {
  it("命令、路径、说明、范围、风险默认可见；高级详情默认折叠", () => {
    const w = mount(ApprovalFacts, { props: { item: item() }, global: { plugins: [createPinia()] } });
    const root = w.find("[data-test='approval-facts']");
    expect(root.exists()).toBe(true);
    expect(w.find(".intent").text()).toBe("想运行一条 shell 命令");
    expect(w.find("[data-test='approval-explanation']").text()).toContain("为了确认测试目录是否已清空");
    expect(w.find("[data-test='approval-command']").text()).toContain("rm -rf /tmp/qio-scratch");
    expect(w.find("[data-test='approval-paths']").text()).toContain("D:\\work\\demo");
    expect(w.find("[data-test='approval-scope']").text()).toContain("仅这一次");
    expect(w.find("[data-test='approval-risks']").text()).toContain("会执行命令");

    const adv = w.find("[data-test='approval-advanced']");
    expect(adv.exists()).toBe(true);
    expect(adv.attributes("open"), "高级详情默认折叠").toBeUndefined();
    expect(adv.text()).toContain("策略指纹：fp_shell_1");
    w.unmount();
  });

  it("子 agent 预算设置入口在内联卡里也能用（可改后批准）", async () => {
    const budget = { maxIterations: 5, maxTokens: 100000, outputLimitChars: 2000 };
    const w = mount(ApprovalFacts, {
      props: { item: item({ name: "sub", tool_type: "subagent", subagent_budget: {} }, "tool_create"), budget },
      global: { plugins: [createPinia()] },
    });
    expect(w.find(".budget-box").exists()).toBe(true);
    expect(w.find(".budget-box").text()).toContain("最大迭代");
    w.unmount();
  });
});

describe("问题 1：内联卡上的预算入口真的能改后批准", () => {
  it("子 agent 型工具创建：内联卡的「允许」带上改过的 subagent_budget", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.activeTurnId = "turn_1";
    session.turnRunning = true;
    const approvals = useApprovalsStore();
    approvals.enqueue(
      "ap_budget",
      "tool_create",
      {
        name: "sub",
        tool_type: "subagent",
        description: "创建一个子 agent 型工具",
        subagent_budget: { max_iterations: 4, max_tokens: 8000, output_limit_chars: 700 },
      },
      { autoOpen: false, turnId: "turn_1" },
    );
    const w = mount(TurnProcess, {
      props: { turnId: "turn_1", items: [], stages: [], facts: null, running: true },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });
    await nextTick();

    const card = w.find("[data-test='turn-process-approval']");
    expect(card.find(".budget-box").exists(), "内联卡必须给出预算设置入口").toBe(true);
    // 预算默认值来自这一条审批（不编造）
    const inputs = card.findAll(".budget-box input");
    expect(inputs.length).toBeGreaterThanOrEqual(3);
    expect((inputs[0]?.element as HTMLInputElement).value).toBe("4");

    await card.find("[data-test='turn-process-approval-allow']").trigger("click");
    await flushPromises();
    expect(respondApproval).toHaveBeenCalledWith(
      "ap_budget",
      "approved",
      {
        subagent_budget: { max_iterations: 4, max_tokens: 8000, output_limit_chars: 700 },
      },
      expect.objectContaining({ turnId: "turn_1" }),
    );
    w.unmount();
  });
});

describe("问题 1：内联卡与弹窗共用同一份审批事实（说同样的话）", () => {
  it("同一 payload：弹窗与过程区内联卡都出现命令 / 说明 / 路径", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const approvals = useApprovalsStore();
    // autoOpen 保持默认：弹窗立即显示（内联门控在同一个 store 下另行验证）
    approvals.enqueue("ap_modal", "tool_execution", SHELL_PAYLOAD, { turnId: "turn_1" });
    const modal = mount(ApprovalModal, { global: { plugins: [pinia] }, attachTo: document.body });
    await flushPromises();
    const modalText = modal.find(".modal").text();
    expect(modalText).toContain("想运行一条 shell 命令");
    expect(modalText).toContain("为了确认测试目录是否已清空，需要执行一次删除。");
    expect(modalText).toContain("rm -rf /tmp/qio-scratch");
    modal.unmount();

    const session = useSessionStore();
    session.activeTurnId = "turn_1";
    session.turnRunning = true;
    const inline = mount(TurnProcess, {
      props: { turnId: "turn_1", items: [], stages: [], facts: null, running: true },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });
    await nextTick();
    const card = inline.find("[data-test='turn-process-approval']");
    expect(card.exists()).toBe(true);
    const text = card.text();
    expect(text).toContain("想运行一条 shell 命令");
    expect(text).toContain("为了确认测试目录是否已清空，需要执行一次删除。");
    expect(text).toContain("rm -rf /tmp/qio-scratch");
    expect(text).toContain("仅这一次");
    expect(text).toContain("会执行命令");
    inline.unmount();
  });
});
