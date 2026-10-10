/**
 * D 独立验收：审批事实（审计问题 1 / plan §1.3）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 1 条 + §1.3。
 * 只依据产品规则：
 *
 *   内联审批卡必须同时给出「简短描述」与**独立的模型说明**，以及**真实操作事实**
 *   （命令 / 路径 / 工具参数 / 授权对象 / 范围 / 风险），并提供「查看完整信息」入口
 *   打开原弹窗；同一 approval_id 只有一套有效按钮，按钮按 id 应答。
 *
 * 基线（e428bb9）现状：TurnProcess.vue:264-274 的内联卡只取 approvalIntent(payload)
 * 一行 + approvalCapabilities，:287-295 claimInline 还让全局入口/弹窗让位 ——
 * 真实命令/路径/参数/scope、独立 explanation、授权与预算设置全部没有入口 ——
 * 因此本文件在修复前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/ApprovalFacts.audit.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h, nextTick } from "vue";
import ApprovalModal from "../ApprovalModal.vue";
import TurnProcess from "../TurnProcess.vue";
import { useApprovalsStore } from "../../stores/approvals";
import { useSessionStore } from "../../stores/session";
import { resetProcessState } from "../../stores/turnProcess";

vi.mock("../../services/api", () => {
  const payload = () => ({
    tasks: [],
    messages: [],
    approvals: [],
    tools: [],
    narratives: [],
    instance_id: "inst_audit",
    turn_queue: { running: null, queued: [], cancelled: [], revision: 0, instance_id: "inst_audit" },
  });
  return {
    api: new Proxy({}, { get: () => vi.fn(async () => payload()) }),
    ApiError: class ApiError extends Error {},
  };
});

const APPROVAL_ID = "ap_audit_1";

/** 「简短描述 + 实际命令 + 独立说明」三者俱全的审批载荷（plan §3 验收第 2 条）。 */
function payload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    description: "运行测试命令",
    explanation: "模型说明：这一步要跑单元测试，确认改动没有破坏既有行为。",
    command: "uv run --frozen pytest tests/test_audit_stream_role_verify.py -q",
    path: "D:\\qio-dev\\qio-fix-d\\backend",
    params: { text: "hi", limit: 3 },
    scope: "本轮工具执行（tool_execution）",
    risk: "medium",
    capabilities: ["执行本机命令", "只读本轮工作目录"],
    budget: { max_iterations: 5, token_budget: 200000 },
    verification: "命令只在受限子进程里执行；结果会写进工具记录",
    tool_name: "run_shell",
    ...overrides,
  };
}

interface HostOptions {
  turnId: string;
  withModal?: boolean;
}

function makeProps(turnId: string) {
  return {
    turnId,
    items: [] as never[],
    stages: [] as never[],
    facts: null,
    running: true,
  };
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

async function mountInline(options: HostOptions): Promise<{ wrapper: VueWrapper; approvals: ReturnType<typeof useApprovalsStore> }> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const approvals = useApprovalsStore();
  session.activeTurnId = options.turnId;
  const props = makeProps(options.turnId);

  const Host = defineComponent({
    setup() {
      const store = useApprovalsStore();
      return () =>
        h("div", [
          h(TurnProcess, props as never),
          options.withModal && !store.inlineClaimed ? h(ApprovalModal) : null,
        ]);
    },
  });
  const wrapper = mount(Host, { global: { plugins: [pinia] } });
  await settle();
  return { wrapper, approvals };
}

function inlineCard(wrapper: VueWrapper) {
  return wrapper.find('[data-test="turn-process-approval"]');
}

function actionsOf(wrapper: VueWrapper) {
  return inlineCard(wrapper).findAll("button");
}

function clickByLabel(wrapper: VueWrapper, pattern: RegExp) {
  return actionsOf(wrapper).find(
    (button) =>
      pattern.test(button.text()) || pattern.test(String(button.attributes("aria-label") ?? "")),
  );
}

beforeEach(() => {
  resetProcessState();
  if (typeof localStorage !== "undefined") localStorage.clear();
});

describe("契约 §1.3：内联审批的事实完整性与入口", () => {
  it("简短描述、独立说明、真实命令三者都在内联卡里可查看（不是只留一行 intent）", async () => {
    const { wrapper, approvals } = await mountInline({ turnId: "turn_1" });
    approvals.enqueue(APPROVAL_ID, "tool_execution", payload(), { turnId: "turn_1", autoOpen: true });
    await settle();

    const card = inlineCard(wrapper);
    expect(card.exists(), "当前轮 + 当前审批必须内联在过程区").toBe(true);
    const text = card.text();
    expect(text, "模型说明必须单独保留（不能只显示描述）").toContain("模型说明：这一步要跑单元测试");
    expect(text, "简短描述必须保留").toContain("运行测试命令");

    // 真实操作事实 + 查看完整信息的入口：三者都要有可查看路径
    const entry =
      clickByLabel(wrapper, /查看完整信息|完整信息|全部信息|查看详情|详情/) ??
      actionsOf(wrapper).find((b) => /展开|更多/.test(b.text()));
    expect(
      entry,
      "必须有一个「查看完整信息」入口（长技术明细可以折叠，但入口必须明确）",
    ).toBeTruthy();
    await entry!.trigger("click");
    await settle();
    const after = inlineCard(wrapper).exists() ? inlineCard(wrapper).text() : wrapper.text();
    expect(after, "查看完整信息之后必须能看到实际命令").toContain(
      "uv run --frozen pytest tests/test_audit_stream_role_verify.py -q",
    );
    wrapper.unmount();
  });

  it("允许/拒绝按 approval_id 应答（同一时刻只有一套有效按钮）", async () => {
    const { wrapper, approvals } = await mountInline({ turnId: "turn_1" });
    approvals.enqueue(APPROVAL_ID, "tool_execution", payload(), { turnId: "turn_1", autoOpen: true });
    await settle();
    const spy = vi.spyOn(approvals, "respondById").mockResolvedValue(undefined as never);

    const allow = actionsOf(wrapper).find((b) => /允许|批准|同意/.test(b.text()));
    expect(allow, "内联卡必须有允许按钮").toBeTruthy();
    await allow!.trigger("click");
    await settle();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0]).toBe(APPROVAL_ID);
    expect(spy.mock.calls[0][1]).toBe("approved");
    wrapper.unmount();
  });

  it("多条排队：内联卡展示的永远是当前那条审批自己的事实", async () => {
    const { wrapper, approvals } = await mountInline({ turnId: "turn_1" });
    approvals.enqueue(APPROVAL_ID, "tool_execution", payload(), { turnId: "turn_1", autoOpen: true });
    approvals.enqueue(
      "ap_audit_2",
      "credential_grant",
      payload({
        description: "授权写入凭据",
        explanation: "模型说明：这一步需要保存一个新的凭据。",
        command: "POST /api/credentials",
        tool_name: "credential_save",
      }),
      { turnId: "turn_1", autoOpen: true },
    );
    await settle();

    const first = inlineCard(wrapper).text();
    expect(first).toContain("运行测试命令");

    const spy = vi.spyOn(approvals, "respondById").mockImplementation(async (id: string) => {
      approvals.queue = approvals.queue.filter((item) => item.approval_id !== id);
    });
    const allow = actionsOf(wrapper).find((b) => /允许|批准|同意/.test(b.text()));
    await allow!.trigger("click");
    await settle();
    expect(spy.mock.calls[0][0], "第一条必须按它自己的 id 应答").toBe(APPROVAL_ID);

    const second = inlineCard(wrapper).text();
    expect(second, "下一条审批要显示它自己的事实").toContain("授权写入凭据");
    expect(second, "不得把上一条的命令留在卡上").not.toContain("uv run --frozen pytest");
    wrapper.unmount();
  });

  it("非当前轮 / 别的轮的审批不内联（仍由全局入口负责，不能既内联又隐藏入口）", async () => {
    const { wrapper, approvals } = await mountInline({ turnId: "turn_1" });
    approvals.enqueue(APPROVAL_ID, "tool_execution", payload(), { turnId: "turn_2", autoOpen: true });
    await settle();
    expect(
      inlineCard(wrapper).exists(),
      "不属于当前轮的审批不能内联到这一轮（否则用户看到的是错的那一轮）",
    ).toBe(false);
    wrapper.unmount();
  });
});
