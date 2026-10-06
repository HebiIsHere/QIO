/**
 * D 独立验收：过程区默认可见区（审计问题 4 / plan §1.5）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 4 条 + §1.5。
 * 只依据产品规则（不看实现方结论）：
 *
 *   运行中默认**不展开**历史；默认可见区 = 状态行 + 当前阶段名 + 最新一条说明
 *   + 一行工具摘要。旧阶段的说明、同一阶段更早的说明、逐项工具卡默认都**不可见**，
 *   展开之后必须完整可回看。
 *
 * 验证方式：真挂载 TurnProcess（不是读实现结论），断言 DOM 上用户能看到什么。
 *
 * 基线（e428bb9）现状：TurnProcess.vue:76-85 的 watch(running) 会在运行中
 * setProcessExpanded(key, true) 自动展开抽屉，且 :343 把当前阶段的**逐项工具卡**
 * 渲染在默认可见区 —— 因此本文件在修复前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/ProcessDefaultVisibility.audit.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import TurnProcess from "../TurnProcess.vue";
import { resetProcessState } from "../../stores/turnProcess";
import type { StreamMessage, TurnFacts, TurnStage } from "../../stores/session";

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

let seq = 0;

function msg(partial: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  seq += 1;
  return {
    id: "m_" + seq,
    content: "",
    contentType: "text/plain",
    createdAt: "2026-10-06T08:00:00+00:00",
    ...partial,
  } as StreamMessage;
}

function note(text: string, narrativeId: string) {
  return { narrativeId, text, kind: "progress" as const, at: "2026-10-06T08:00:00+00:00" };
}

function stage(partial: Partial<TurnStage> & { stageId: string }): TurnStage {
  return { index: 1, name: "阶段", status: "running", notes: [], callIds: [], ...partial };
}

const FACTS_DONE: TurnFacts = {
  turnId: "turn_1",
  status: "completed",
  durationMs: 1500,
  queueMs: 0,
  startedAt: "2026-10-06T08:00:00+00:00",
  endedAt: "2026-10-06T08:00:01.500+00:00",
};

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

async function mountProcess(props: {
  items: StreamMessage[];
  stages: TurnStage[];
  running: boolean;
  facts?: TurnFacts | null;
  queued?: boolean;
}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(TurnProcess, {
    props: {
      turnId: "turn_1",
      items: props.items,
      stages: props.stages,
      facts: props.facts ?? null,
      running: props.running,
      queued: props.queued ?? false,
    },
    global: { plugins: [pinia] },
  });
  await settle();
  return wrapper;
}

/** 默认可见区 = 过程区根节点（历史抽屉按契约只在展开时进 DOM）。 */
function visibleText(wrapper: VueWrapper): string {
  const region = wrapper.find('[data-test="turn-process"]');
  expect(region.exists(), "契约 §1.5：必须有一个可识别的过程区").toBe(true);
  return region.text();
}

function drawer(wrapper: VueWrapper) {
  return wrapper.find('[data-test="turn-process-history"]');
}

async function expand(wrapper: VueWrapper): Promise<void> {
  const toggle = wrapper.find('[data-test="turn-process-toggle"]');
  expect(toggle.exists(), "过程区必须有可展开历史入口").toBe(true);
  if (toggle.attributes("aria-expanded") !== "true") {
    await toggle.trigger("click");
    await settle();
  }
}

beforeEach(() => {
  seq = 0;
  resetProcessState();
  if (typeof localStorage !== "undefined") localStorage.clear();
});

describe("契约 §1.5：运行中默认折叠", () => {
  it("首轮运行：默认不出现旧说明与逐项工具卡，只留状态行 + 当前阶段 + 最新说明", async () => {
    const wrapper = await mountProcess({
      running: true,
      stages: [
        stage({
          stageId: "st_1",
          index: 1,
          name: "读取仓库结构",
          notes: [note("上一阶段的说明，默认不该可见", "n1")],
        }),
        stage({
          stageId: "st_2",
          index: 2,
          name: "核对实现",
          notes: [note("最新的说明，必须默认可见", "n2")],
        }),
      ],
      items: [
        msg({ role: "tool", toolName: "audit_tool_marker_old", toolStatus: "success", stageId: "st_1" }),
        msg({ role: "tool", toolName: "audit_tool_marker_current", toolStatus: "success", stageId: "st_2" }),
      ],
    });

    expect(
      drawer(wrapper).exists(),
      "契约 §1.5：运行中默认**不展开**历史（默认可见区不得出现旧阶段与逐项工具卡）",
    ).toBe(false);

    const text = visibleText(wrapper);
    expect(text).toContain("核对实现");
    expect(text).toContain("最新的说明，必须默认可见");
    expect(text, "上一阶段的说明默认不该出现在可见区").not.toContain("上一阶段的说明");
    expect(text, "逐项工具卡默认不该出现在可见区").not.toContain("audit_tool_marker_old");
    expect(text, "逐项工具卡默认不该出现在可见区").not.toContain("audit_tool_marker_current");

    await expand(wrapper);
    expect(drawer(wrapper).exists(), "展开之后必须完整可回看").toBe(true);
    expect(drawer(wrapper).text()).toContain("上一阶段的说明");
    expect(drawer(wrapper).text()).toContain("audit_tool_marker_old");
    wrapper.unmount();
  });

  it("两次阶段切换 + 同阶段三条说明 + 并行工具：默认只留最新一条说明与一行工具摘要", async () => {
    const wrapper = await mountProcess({
      running: true,
      stages: [
        stage({
          stageId: "st_1",
          index: 1,
          name: "第一段",
          status: "done",
          notes: [note("第一段说明 A", "n1"), note("第一段说明 B", "n2")],
        }),
        stage({
          stageId: "st_2",
          index: 2,
          name: "第二段",
          status: "done",
          notes: [note("第二段说明", "n3")],
        }),
        stage({
          stageId: "st_3",
          index: 3,
          name: "第三段",
          status: "running",
          notes: [note("同阶段第一条说明", "n4"), note("同阶段第二条说明", "n5"), note("同阶段第三条说明", "n6")],
        }),
      ],
      items: [
        // 历史阶段的逐项调用：展开整轮历史后必须能回看
        msg({ role: "tool", toolName: "audit_history_tool", toolStatus: "success", stageId: "st_1" }),
        // 当前阶段的逐项调用：默认不可见，走「本阶段明细」独立开关
        msg({ role: "tool", toolName: "audit_parallel_a", toolStatus: "success", stageId: "st_3" }),
        msg({ role: "tool", toolName: "audit_parallel_b", toolStatus: "success", stageId: "st_3" }),
        msg({ role: "tool", toolName: "读取文件", toolStatus: "running", toolRunning: true, stageId: "st_3" }),
      ],
    });

    expect(drawer(wrapper).exists(), "运行中默认折叠：历史抽屉不该进 DOM").toBe(false);
    const text = visibleText(wrapper);
    expect(text).toContain("同阶段第三条说明");
    for (const hidden of ["第一段说明 A", "第一段说明 B", "第二段说明", "同阶段第一条说明", "同阶段第二条说明"]) {
      expect(text, "旧说明默认不该可见：" + hidden).not.toContain(hidden);
    }
    for (const hidden of ["audit_parallel_a", "audit_parallel_b"]) {
      expect(text, "逐项工具卡默认不该可见：" + hidden).not.toContain(hidden);
    }
    expect(text, "状态行必须给一行工具摘要（正在跑什么）").toMatch(/工具运行中|正在/);

    await expand(wrapper);
    const opened = drawer(wrapper).text();
    for (const shown of ["第一段说明 A", "第二段说明", "同阶段第一条说明", "audit_history_tool"]) {
      expect(opened, "展开整轮历史后必须能回看：" + shown).toContain(shown);
    }
    // 当前阶段的逐项调用走**独立**开关（契约 §1.5：当前阶段展开状态与历史分开管理）
    expect(
      wrapper.find('[data-test="turn-process-stage-tools"]').exists(),
      "当前阶段明细默认收起（不能因为展开了整轮历史就把它带出来）",
    ).toBe(false);
    const stageToggle = wrapper.find('[data-test="turn-process-stage-toggle"]');
    expect(stageToggle.exists(), "当前阶段有逐项调用时必须给独立开关").toBe(true);
    await stageToggle.trigger("click");
    await settle();
    const stageTools = wrapper.find('[data-test="turn-process-stage-tools"]');
    expect(stageTools.exists(), "点开本阶段明细后必须能看到逐项调用").toBe(true);
    expect(stageTools.text()).toContain("audit_parallel_a");
    wrapper.unmount();
  });

  it("可恢复错误：失败的工具卡默认不可见（结论在摘要里），展开后能看细节", async () => {
    const wrapper = await mountProcess({
      running: true,
      stages: [
        stage({ stageId: "st_1", index: 1, name: "执行中", notes: [note("正在执行这一步", "n1")] }),
      ],
      items: [
        msg({
          role: "tool",
          toolName: "audit_recovered_tool",
          toolStatus: "failed",
          toolOk: false,
          toolError: "第一次失败了",
          stageId: "st_1",
        }),
        msg({ role: "tool", toolName: "读取文件", toolStatus: "running", toolRunning: true, stageId: "st_1" }),
      ],
    });

    expect(drawer(wrapper).exists(), "运行中默认折叠").toBe(false);
    expect(visibleText(wrapper), "失败的工具卡属于逐项细节，默认不该可见").not.toContain(
      "audit_recovered_tool",
    );
    // 逐项调用在「本阶段明细」开关后面（默认收起）；点开才能看参数/结果/失败详情
    const stageToggle = wrapper.find('[data-test="turn-process-stage-toggle"]');
    expect(stageToggle.exists(), "有逐项调用时必须有本阶段明细开关（入口明确）").toBe(true);
    await stageToggle.trigger("click");
    await settle();
    const detail = wrapper.find('[data-test="turn-process-stage-tools"]');
    expect(detail.exists(), "点开本阶段明细后必须能看到逐项调用与失败详情").toBe(true);
    expect(detail.text()).toContain("audit_recovered_tool");
    wrapper.unmount();
  });

  it("完成：用户没碰过就保持折叠；用户手动展开过则不被自动收起（保护阅读）", async () => {
    const items = [
      msg({ role: "tool", toolName: "audit_after_done", toolStatus: "success", stageId: "st_1" }),
    ];
    const stages = [
      stage({ stageId: "st_1", index: 1, name: "完成阶段", status: "running", notes: [note("收尾说明", "n1")] }),
    ];

    // (a) 用户没碰过：运行中默认折叠，完成之后仍然折叠（默认可见区只有状态词 + 耗时）
    const untouched = await mountProcess({ running: true, items, stages });
    expect(drawer(untouched).exists(), "运行中默认折叠").toBe(false);
    await untouched.setProps({ running: false, facts: FACTS_DONE });
    await settle();
    expect(
      drawer(untouched).exists(),
      "完成之后默认可见区仍然只有状态行（不得因为状态变化把历史塞回用户眼前）",
    ).toBe(false);
    expect(untouched.text(), "完成后必须如实显示状态词").toMatch(/已完成|已停止/);
    untouched.unmount();

    // (b) 用户手动展开过：完成**不得**把它收走 —— 契约明文保护正在阅读的用户
    const manual = await mountProcess({ running: true, items, stages });
    await expand(manual);
    expect(drawer(manual).exists()).toBe(true);
    await manual.setProps({ running: false, facts: FACTS_DONE });
    await settle();
    expect(
      drawer(manual).exists(),
      "用户手动展开过 → 完成时不自动收起（保护正在上翻阅读的用户）",
    ).toBe(true);
    manual.unmount();
  });

  it("重连（组件重建）后默认可见区仍然是折叠的", async () => {
    const items = [
      msg({ role: "tool", toolName: "audit_reconnect", toolStatus: "success", stageId: "st_1" }),
    ];
    const stages = [
      stage({ stageId: "st_1", index: 1, name: "重连阶段", status: "running", notes: [note("重连说明", "n1")] }),
    ];

    const reconnected = await mountProcess({ running: true, items, stages });
    expect(
      drawer(reconnected).exists(),
      "重连（组件重建）后默认可见区仍然是折叠的：旧说明与逐项工具卡不可见",
    ).toBe(false);
    reconnected.unmount();
  });
});
