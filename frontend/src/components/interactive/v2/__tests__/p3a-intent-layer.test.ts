/**
 * Phase 3-A：虚拟意图层 / display 位置 / 显式 ids 确认 / 能力迁移（定向测试）。
 *
 * 守住契约 §6.1 / §6.2：
 * 1. display {x,y} 按 intent_id 绑定；没有持久化值时**确定性**落位，绝不随机、绝不 NaN；
 * 2. 拖动只改显示位置（松手 emit display-change 交回持久化接口），并钳进板面范围；
 * 3. 单条 / 同批确认只用**显式 ids**；needs_update / 未知 / 已开始的不能确认；
 * 4. 确认结果逐项反馈：部分失败分开列；只有服务端返回 completed 才算完成；
 *    只有响应里真的带 turn_id 才显示执行轮次，没有就说「不能认为已开始」；
 * 5. 旧工作台 / BatchTray / StatusPopover 的有效能力有明确去处（依赖/冲突/账本/恢复）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  api: {
    confirmSingleIntent: vi.fn(),
    batchConfirmIntents: vi.fn(),
    fetchRecoveryPreview: vi.fn(),
    recoverIntent: vi.fn(),
    fetchIntentViews: vi.fn(),
    fetchLedger: vi.fn(),
    fetchIntentResults: vi.fn(),
  },
  store: {
    boardId: "board_1",
    statusLabel: (status: string) =>
      ({
        proposed: "待确认",
        awaiting_confirmation: "待再次确认",
        approved: "已确认（等待执行）",
        running: "执行中",
        paused: "已暂停",
        completed: "已完成",
        partially_completed: "部分完成",
        failed: "失败",
        cancelled: "已取消",
        rejected: "已拒绝",
      })[status] ?? status,
  },
}));

vi.mock("../../../../services/interactive", () => mocks.api);
vi.mock("../../../../stores/interactive", () => ({ useInteractiveStore: () => mocks.store }));

import IntentLayer from "../IntentLayer.vue";
import BoardPreviewLayer from "../../BoardPreviewLayer.vue";
import {
  batchConfirmState,
  capabilityRows,
  clampIntentDisplay,
  confirmBlockReason,
  displayEquals,
  dragIntentDisplay,
  fallbackIntentDisplay,
  intentDisplayMap,
  readIntentDisplay,
  recoveryFacts,
  recoveryResultText,
  resolveIntentDisplay,
  selectedConfirmIds,
  selectableIntentIds,
  summarizeConfirm,
  type IntentViewWithDisplay,
} from "../intentDisplay";

const STAMP = "2026-10-24T00:00:00.000Z";

function view(overrides: Partial<IntentViewWithDisplay> = {}): IntentViewWithDisplay {
  return {
    intent_id: "i1",
    board_id: "board_1",
    version: 1,
    status: "proposed",
    title: "整理材料",
    goal: "把材料整理成清单",
    expected: "得到清单",
    scope: { read_card_ids: ["c1"], allowed_actions: ["add_text_card"] },
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    operations_summary: { applied: 1, pending: 2 },
    created_at: STAMP,
    updated_at: STAMP,
    ...overrides,
  } as IntentViewWithDisplay;
}

function mountLayer(views: IntentViewWithDisplay[], extra: Record<string, unknown> = {}) {
  return mount(IntentLayer, {
    props: { views, boardId: "board_1", width: 2400, height: 1600, ...extra },
    attachTo: document.body,
  });
}

function cardOf(wrapper: ReturnType<typeof mountLayer>, intentId: string) {
  return wrapper.findAll('[data-im="virtual-intent"]').find((item) => item.attributes("data-intent-id") === intentId)!;
}

function tap(wrapper: ReturnType<typeof mountLayer>, intentId: string): void {
  const card = cardOf(wrapper, intentId);
  card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 10, clientY: 10, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointerup", { clientX: 10, clientY: 10, bubbles: true }));
}

function styleOf(element: Element | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (element?.getAttribute("style") ?? "").split(";")) {
    const at = part.indexOf(":");
    if (at < 0) continue;
    out[part.slice(0, at).trim()] = part.slice(at + 1).trim();
  }
  return out;
}

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  document.body.innerHTML = "";
});

describe("显示位置：按 intent_id 绑定 + 确定性 fallback", () => {
  it("读取持久化 display：坏数据 / 缺字段一律 null（不猜、不补 0）", () => {
    expect(readIntentDisplay(view({ display: { x: 120, y: 80 } }))).toEqual({ x: 120, y: 80 });
    expect(readIntentDisplay(view({ display: null }))).toBeNull();
    expect(readIntentDisplay(view({ display: { x: Number.NaN, y: 10 } }))).toBeNull();
    expect(readIntentDisplay(view({ display: { x: 1 } as unknown as { x: number; y: number } }))).toBeNull();
    expect(readIntentDisplay(view({}))).toBeNull();
  });

  it("没有持久化值时确定性落位：同一顺序永远同一位置，不随机", () => {
    const first = fallbackIntentDisplay(0);
    const again = fallbackIntentDisplay(0);
    expect(first).toEqual(again);
    expect(fallbackIntentDisplay(2).x).not.toBe(first.x);
    expect(Number.isFinite(fallbackIntentDisplay(99).y)).toBe(true);
    expect(intentDisplayMap([view({ intent_id: "a" }), view({ intent_id: "b" })])).toEqual({
      a: fallbackIntentDisplay(0),
      b: fallbackIntentDisplay(1),
    });
  });

  it("优先级：外部覆盖 > 持久化 display > 确定性 fallback", () => {
    const stored = view({ intent_id: "a", display: { x: 500, y: 400 } });
    expect(resolveIntentDisplay(stored, 3)).toEqual({ point: { x: 500, y: 400 }, persisted: true });
    expect(resolveIntentDisplay(stored, 3, { a: { x: 10, y: 20 } })).toEqual({
      point: { x: 10, y: 20 },
      persisted: true,
    });
    expect(resolveIntentDisplay(view({ intent_id: "b" }), 2).persisted).toBe(false);
  });

  it("拖动位置钳进板面范围，并按画布缩放换算屏幕位移", () => {
    expect(clampIntentDisplay({ x: -50, y: -80 }, { w: 2400, h: 1600 })).toEqual({ x: 0, y: 0 });
    expect(clampIntentDisplay({ x: 9999, y: 9999 }, { w: 2400, h: 1600 })).toEqual({
      x: 2400 - 260,
      y: 1600 - 150,
    });
    // scale=2：屏幕 +100 = 板面 +50
    expect(dragIntentDisplay({ x: 100, y: 100 }, { dx: 100, dy: 60 }, { bounds: { w: 2400, h: 1600 }, scale: 2 })).toEqual({
      x: 150,
      y: 130,
    });
    expect(displayEquals({ x: 1, y: 2 }, { x: 1.2, y: 2.1 })).toBe(true);
    expect(displayEquals(null, null)).toBe(true);
  });
});

describe("可确认性：needs_update / 未知 / 已开始都不冒充可确认", () => {
  it("只有 proposed / awaiting_confirmation 可确认", () => {
    expect(confirmBlockReason(view({ status: "proposed" }))).toBeNull();
    expect(confirmBlockReason(view({ status: "awaiting_confirmation" }))).toBeNull();
    for (const status of ["approved", "running", "paused"]) {
      expect(confirmBlockReason(view({ status }))).toBe("already_started");
    }
    for (const status of ["completed", "partially_completed", "failed", "cancelled", "rejected"]) {
      expect(confirmBlockReason(view({ status }))).toBe("not_proposed");
    }
    expect(confirmBlockReason(view({ status: "weird_status" }))).toBe("unknown_status");
    expect(confirmBlockReason(view({ intent_id: "" }))).toBe("missing_id");
  });

  it("needs_update（含旧链 raw_legacy_status）不能确认，并有明确说明", () => {
    expect(confirmBlockReason(view({ status: "needs_update" }))).toBe("needs_update");
    expect(confirmBlockReason(view({ status: "proposed", raw_legacy_status: "needs_update" }))).toBe("needs_update");
  });

  it("显式 ids：只挑可确认的、去重保序；同批至少两条", () => {
    const views = [
      view({ intent_id: "a", status: "proposed" }),
      view({ intent_id: "b", status: "needs_update" }),
      view({ intent_id: "c", status: "awaiting_confirmation" }),
      view({ intent_id: "d", status: "running" }),
    ];
    expect(selectableIntentIds(views)).toEqual(["a", "c"]);
    expect(selectedConfirmIds(views, ["c", "b", "a", "a", "d"])).toEqual(["c", "a"]);
    expect(batchConfirmState(views, ["a"]).enabled).toBe(false);
    expect(batchConfirmState(views, ["a", "c"]).enabled).toBe(true);
    expect(batchConfirmState(views, ["a", "b"]).ids).toEqual(["a"]);
    expect(batchConfirmState(views, ["a"]).hint).toContain("单条");
  });
});

describe("确认结果：显式 ids / 部分失败 / 真实 turn_id / 不说成完成", () => {
  it("逐项结果按请求的显式 ids 展开；服务端没返回的项不算成功", () => {
    const summary = summarizeConfirm(
      { results: [{ intent_id: "a", ok: true, status: "approved", turn_id: "turn_1" }] } as never,
      ["a", "b"],
    );
    expect(summary.rows.map((row) => row.intentId)).toEqual(["a", "b"]);
    expect(summary.failedIds).toEqual(["b"]);
    expect(summary.rows[1].returned).toBe(false);
    expect(summary.rows[1].reason).toContain("没有返回");
    expect(summary.rows[0].turnId).toBe("turn_1");
    expect(summary.notice).toContain("部分失败");
    expect(summary.notice).toContain("已确认不等于已完成");
    expect(summary.completedIds).toEqual([]);
  });

  it("只有服务端 completed 才算完成；approved 只说已确认", () => {
    const summary = summarizeConfirm(
      {
        batch: true,
        results: [
          { intent_id: "a", ok: true, status: "approved" },
          { intent_id: "b", ok: true, status: "completed" },
        ],
      } as never,
      ["a", "b"],
      { batch: true },
    );
    expect(summary.rows[0].completed).toBe(false);
    expect(summary.rows[0].statusText).toContain("已确认");
    expect(summary.rows[0].statusText).not.toContain("已完成");
    expect(summary.rows[1].completed).toBe(true);
    expect(summary.completedIds).toEqual(["b"]);
  });

  it("没有 turn_id 时标记「不能认为已开始」；响应级 turn_id 也认", () => {
    const withoutTurn = summarizeConfirm({ results: [{ intent_id: "a", ok: true, status: "approved" }] } as never, ["a"]);
    expect(withoutTurn.startedUnknownIds).toEqual(["a"]);
    expect(withoutTurn.notice).toContain("不能认为已经开始执行");

    const responseLevel = summarizeConfirm(
      { turn_id: "turn_top", results: [{ intent_id: "a", ok: true, status: "approved" }] } as never,
      ["a"],
    );
    expect(responseLevel.turnIds).toEqual({ a: "turn_top" });
    expect(responseLevel.startedUnknownIds).toEqual([]);
  });
});

describe("详情能力行", () => {
  it("详情能力行包含依赖 / 冲突 / 账本 / 授权失效 / 恢复", () => {
    const rows = capabilityRows(
      view({
        intent_id: "a",
        status: "failed",
        depends_on: ["x"],
        conflicts_with: ["y"],
        conflicts: [{ intent_id: "y", status: "running" }],
        authorization_valid: false,
        summary: "失败原因",
      }),
    );
    const keys = rows.map((row) => row.key);
    expect(keys).toContain("dependencies");
    expect(keys).toContain("operations");
    expect(keys).toContain("authorization");
    expect(rows.find((row) => row.key === "authorization")!.text).toContain("旧授权已失效");

    // 恢复行：授权仍有效 + 失败 / 部分完成 / 暂停才出现（与 canRecover 同口径）
    const recoverable = capabilityRows(view({ intent_id: "b", status: "failed", authorization_valid: true }));
    expect(recoverable.map((row) => row.key)).toContain("recovery");
    expect(capabilityRows(view({ intent_id: "c", status: "proposed" })).map((row) => row.key)).not.toContain("recovery");
  });

  it("恢复事实与结果：只说受理，不说执行完成", () => {
    const facts = recoveryFacts({
      intent_id: "a",
      can_resume: true,
      reasons: [],
      unfinished_operations: [{ operation_id: "op_1", status: "failed" }],
      unfinished_dependencies: ["dep_1"],
    } as never);
    expect(facts.canResume).toBe(true);
    expect(facts.text).toContain("可恢复");
    expect(facts.text).toContain("op_1");
    expect(recoveryFacts(null).available).toBe(false);
    const resumed = recoveryResultText({ ok: true, status: "running" });
    expect(resumed).toContain("恢复已受理");
    // 只说「不代表已完成」：恢复受理 ≠ 执行完成
    expect(resumed).toContain("不代表已完成");
    expect(resumed).not.toContain("恢复已完成");
    expect(recoveryResultText({ ok: false, reason: "条件不满足" })).toContain("没有完成");
  });
});

describe("IntentLayer：虚拟外观 / 选中 / 拖动 / 确认 / 恢复", () => {
  it("虚线虚拟意图（不是成果卡），needs_update 不可选并给出原因", () => {
    const wrapper = mountLayer([
      view({ intent_id: "i1", status: "proposed", display: { x: 120, y: 90 } }),
      view({ intent_id: "i2", status: "needs_update", title: "过期意图" }),
    ]);
    const cards = wrapper.findAll('[data-im="virtual-intent"]');
    expect(cards).toHaveLength(2);
    expect(cards[0].attributes("data-virtual")).toBe("1");
    expect(cards[0].attributes("data-display-persisted")).toBe("true");
    expect(styleOf(cards[0].element).left).toBe("120px");
    // needs_update：有原因、没有勾选框、不能确认
    expect(cardOf(wrapper, "i2").find('[data-im="intent-block"]').text()).toContain("需要更新");
    expect(cardOf(wrapper, "i2").find('[data-im="intent-check"]').exists()).toBe(false);
    expect(cardOf(wrapper, "i1").find('[data-im="intent-check"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("点击选中 → 详情显示能力行；拖动只改显示并从开始位置 emit display-change", async () => {
    const wrapper = mountLayer([
      view({ intent_id: "i1", display: { x: 100, y: 100 }, depends_on: ["i0"], conflicts_with: ["i9"] }),
    ]);
    const card = cardOf(wrapper, "i1");

    card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 150, clientY: 150, bubbles: true }));
    window.dispatchEvent(new MouseEvent("pointerup", { clientX: 150, clientY: 150, bubbles: true }));
    await flushPromises();
    const selectEvents = wrapper.emitted("select") ?? [];
    expect(selectEvents[selectEvents.length - 1]).toEqual(["i1"]);
    expect(wrapper.find('[data-im="intent-detail"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="intent-row:dependencies"]').exists()).toBe(true);

    card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 150, clientY: 150, bubbles: true }));
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 210, clientY: 190, bubbles: true }));
    await flushPromises();
    expect(styleOf(cardOf(wrapper, "i1").element).left).toBe("160px");
    window.dispatchEvent(new MouseEvent("pointerup", { clientX: 210, clientY: 190, bubbles: true }));
    await flushPromises();
    const displayEvents = wrapper.emitted("display-change") ?? [];
    expect(displayEvents[displayEvents.length - 1]).toEqual([{ intentId: "i1", x: 160, y: 140 }]);
    wrapper.unmount();
  });

  it("单条确认：显式 id、逐项结果、真实 turn_id；没有 turn_id 时如实说明", async () => {
    mocks.api.confirmSingleIntent.mockResolvedValue({
      results: [{ intent_id: "i1", ok: true, status: "approved", turn_id: "turn_9" }],
    });
    const wrapper = mountLayer([view({ intent_id: "i1" })]);
    tap(wrapper, "i1");
    await flushPromises();
    await wrapper.find('[data-im="intent-confirm"]').trigger("click");
    await flushPromises();

    expect(mocks.api.confirmSingleIntent).toHaveBeenCalledWith("i1", "board_1");
    const row = wrapper.find('[data-im="intent-confirm-row"]');
    expect(row.attributes("data-intent-id")).toBe("i1");
    expect(row.attributes("data-turn-id")).toBe("turn_9");
    expect(row.attributes("data-completed")).toBe("false");
    const notice = wrapper.find('[data-im="intent-confirm-notice"]').text();
    expect(notice).toContain("已确认（等待执行）");
    expect(notice).toContain("已确认不等于已完成");
    wrapper.unmount();

    mocks.api.confirmSingleIntent.mockResolvedValue({
      results: [{ intent_id: "i1", ok: true, status: "approved" }],
    });
    const second = mountLayer([view({ intent_id: "i1" })]);
    tap(second, "i1");
    await flushPromises();
    await second.find('[data-im="intent-confirm"]').trigger("click");
    await flushPromises();
    expect(second.find('[data-im="intent-confirm-started-unknown"]').text()).toContain("i1");
    second.unmount();
  });

  it("同批确认：显式 ids、部分失败分开列、needs_update 不会被带进批次", async () => {
    mocks.api.batchConfirmIntents.mockResolvedValue({
      batch: true,
      results: [
        { intent_id: "i1", ok: true, status: "approved", turn_id: "turn_a" },
        { intent_id: "i3", ok: false, reason: "依赖未就绪" },
      ],
    });
    const wrapper = mountLayer([
      view({ intent_id: "i1", status: "proposed" }),
      view({ intent_id: "i2", status: "needs_update" }),
      view({ intent_id: "i3", status: "awaiting_confirmation" }),
    ]);
    expect(wrapper.find('[data-im="intent-batch-confirm"]').attributes("disabled")).toBeDefined();
    for (const id of ["i1", "i3"]) {
      await cardOf(wrapper, id).find('[data-im="intent-check"]').setValue(true);
    }
    await flushPromises();
    const button = wrapper.find('[data-im="intent-batch-confirm"]');
    expect(button.attributes("disabled")).toBeUndefined();
    expect(wrapper.find('[data-im="intent-batch-hint"]').text()).toContain("已选 2");
    await button.trigger("click");
    await flushPromises();

    expect(mocks.api.batchConfirmIntents).toHaveBeenCalledWith("board_1", ["i1", "i3"]);
    expect(wrapper.find('[data-im="intent-confirm-partial"]').text()).toContain("i3");
    expect(wrapper.findAll('[data-im="intent-confirm-row"]')).toHaveLength(2);
    wrapper.unmount();
  });

  it("没有板面身份时不发确认请求（如实报错，不伪造结果）", async () => {
    const wrapper = mountLayer([view({ intent_id: "i1" })], { boardId: "" });
    tap(wrapper, "i1");
    await flushPromises();
    // 没有板面身份：确认按钮直接禁用（不发出请求），不让用户以为点了会成功
    const confirmButton = wrapper.find('[data-im="intent-confirm"]');
    expect(confirmButton.attributes("disabled")).toBeDefined();
    await confirmButton.trigger("click");
    await flushPromises();
    expect(mocks.api.confirmSingleIntent).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("恢复入口：先读真实预览，再按版本恢复；结果不说成已完成", async () => {
    mocks.api.fetchRecoveryPreview.mockResolvedValue({
      available: true,
      preview: { intent_id: "i4", can_resume: true, unfinished_operations: [{ operation_id: "op_1", status: "failed" }] },
    });
    mocks.api.recoverIntent.mockResolvedValue({ ok: true, status: "running" });
    const wrapper = mountLayer([view({ intent_id: "i4", status: "failed", version: 3 })]);
    tap(wrapper, "i4");
    await flushPromises();
    await wrapper.find('[data-im="intent-recover-preview"]').trigger("click");
    await flushPromises();
    expect(mocks.api.fetchRecoveryPreview).toHaveBeenCalledWith("i4");
    expect(wrapper.find('[data-im="intent-recovery"]').text()).toContain("可恢复");
    await wrapper.find('[data-im="intent-recover-confirm"]').trigger("click");
    await flushPromises();
    expect(mocks.api.recoverIntent).toHaveBeenCalledWith("i4", "board_1", 3);
    const notice = wrapper.find('[data-im="intent-layer-notice"]').text();
    expect(notice).toContain("恢复已受理");
    expect(notice).toContain("不代表已完成");
    wrapper.unmount();
  });
});

describe("BoardPreviewLayer：display 位置按 intent_id 平移预览", () => {
  const legacyIntent = {
    id: "pi1",
    boardId: "board_1",
    submissionId: null,
    title: "预览",
    summary: "",
    status: "pending",
    preview: { cards: [{ id: "pc1", kind: "text", content: "内容", meta: {}, x: 100, y: 100, w: 200, h: 120 }], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  } as never;

  it("给了 display 就整体平移；没有 display 时保持旧坐标", () => {
    const plain = mount(BoardPreviewLayer, {
      props: { intents: [legacyIntent], locatedIntentId: null, width: 2400, height: 1600 },
    });
    expect(styleOf(plain.find(".preview-card").element).left).toBe("100px");
    plain.unmount();

    const shifted = mount(BoardPreviewLayer, {
      props: {
        intents: [legacyIntent],
        locatedIntentId: null,
        width: 2400,
        height: 1600,
        display: { pi1: { x: 400, y: 300 } },
      },
    });
    expect(shifted.find('[data-im="preview"]').attributes("data-display-persisted")).toBe("true");
    expect(styleOf(shifted.find(".preview-card").element).left).toBe("400px");
    expect(styleOf(shifted.find(".preview-card").element).top).toBe("300px");
    shifted.unmount();
  });
});

