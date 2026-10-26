/**
 * FU2-C 追加补口：确认"已保存但没有开始执行"的真实原因必须用户可见。
 *
 * 背景（Lead 追加）：A 的修复在「原提交话题无法确认」时后端返回
 * turnStart.code=topic_unresolved / turnStart.reason（以及顶层 startError/reason），
 * 但前端 ConfirmResponse 声明了 started/startError 却没人消费 —— 用户只看到
 * 「已确认但没有执行轮次」，看不到真实原因。
 *
 * 先红后绿：
 * 1. summarizeConfirm 消费 started===false / startError / turnStart.reason+code /
 *    顶层 reason：notice/错误里出现「本次没有开始执行：<原因>」；
 *    topic_unresolved 时明确说「原提交话题无法确认」；
 * 2. 成功路径（started!==false）既有文案不变，不出现失败说明；
 * 3. IntentLayer 把失败原因放在用户可见的错误行里（不再只是绿色 notice）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  confirmSingleIntent: vi.fn(),
  batchConfirmIntents: vi.fn(),
  fetchRecoveryPreview: vi.fn(),
  recoverIntent: vi.fn(),
  rejectIntent: vi.fn(),
}));

vi.mock("../../../../services/interactive", () => ({
  confirmSingleIntent: mocks.confirmSingleIntent,
  batchConfirmIntents: mocks.batchConfirmIntents,
  fetchRecoveryPreview: mocks.fetchRecoveryPreview,
  recoverIntent: mocks.recoverIntent,
  rejectIntent: mocks.rejectIntent,
}));

import IntentLayer from "../IntentLayer.vue";
import { summarizeConfirm, type IntentViewWithDisplay } from "../intentDisplay";

function view(overrides: Partial<IntentViewWithDisplay> = {}): IntentViewWithDisplay {
  return {
    intent_id: "a",
    board_id: "board_1",
    version: 1,
    status: "proposed",
    title: "整理发布材料",
    ...overrides,
  } as IntentViewWithDisplay;
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.fetchRecoveryPreview.mockResolvedValue({ available: false, preview: null });
});

describe("summarizeConfirm：开始执行失败的真实原因", () => {
  it("started=false + startError → notice 说明本次没有开始执行并带原因", () => {
    const summary = summarizeConfirm(
      {
        started: false,
        startError: "原提交保存的话题无法确认",
        results: [{ intent_id: "a", ok: true, status: "approved" }],
      } as never,
      ["a"],
    );
    expect(summary.startFailed).toBe(true);
    expect(summary.startError).toContain("原提交保存的话题无法确认");
    expect(summary.notice).toContain("本次没有开始执行");
    expect(summary.notice).toContain("原提交保存的话题无法确认");
    // 已知没开始：不再用「没有返回执行轮次、不能认为已经开始」的含糊话覆盖真实原因
    expect(summary.startedUnknownIds).toEqual([]);
  });

  it("turnStart.code=topic_unresolved / turnStart.reason → 明确说原提交话题无法确认", () => {
    const summary = summarizeConfirm(
      {
        started: false,
        turnStart: {
          code: "topic_unresolved",
          reason: "原提交保存的 topic_1 已不存在，无法核对",
        },
        results: [{ intent_id: "a", ok: true, status: "approved" }],
      } as never,
      ["a"],
    );
    expect(summary.startFailed).toBe(true);
    expect(summary.startCode).toBe("topic_unresolved");
    expect(summary.notice).toContain("原提交话题无法确认");
    expect(summary.notice).toContain("topic_1 已不存在");
    expect(summary.notice).toContain("本次没有开始执行");
  });

  it("顶层 reason 作为兜底原因（没有 startError / turnStart 时）", () => {
    const summary = summarizeConfirm(
      {
        started: false,
        reason: "预算不足，未开始执行",
        results: [{ intent_id: "a", ok: true, status: "approved" }],
      } as never,
      ["a"],
    );
    expect(summary.startFailed).toBe(true);
    expect(summary.notice).toContain("预算不足");
  });

  it("成功路径（started!==false）：既有文案不变，不出现失败说明", () => {
    const summary = summarizeConfirm(
      {
        started: true,
        turn_id: "turn_9",
        results: [{ intent_id: "a", ok: true, status: "approved" }],
      } as never,
      ["a"],
    );
    expect(summary.startFailed).toBe(false);
    expect(summary.startError).toBe("");
    expect(summary.notice).not.toContain("没有开始执行");
    expect(summary.notice).toContain("已确认不等于已完成");
  });
});

describe("IntentLayer：失败原因用户可见", () => {
  it("单条确认 started=false：错误行包含真实原因（不是只有已确认）", async () => {
    setActivePinia(createPinia());
    mocks.confirmSingleIntent.mockResolvedValue({
      started: false,
      startError: "原提交保存的话题无法确认",
      results: [{ intent_id: "a", ok: true, status: "approved" }],
    });
    const w = mount(IntentLayer, {
      props: {
        views: [view()],
        boardId: "board_1",
        width: 1200,
        height: 800,
        selectedId: "a",
      },
    });
    await flushPromises();

    await w.get('[data-im="intent-confirm"]').trigger("click");
    await flushPromises();

    const error = w.find('[data-im="intent-layer-error"]');
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("本次没有开始执行");
    expect(error.text()).toContain("原提交保存的话题无法确认");
    w.unmount();
  });

  it("单条确认成功：不出现错误行，notice 保持既有口径", async () => {
    setActivePinia(createPinia());
    mocks.confirmSingleIntent.mockResolvedValue({
      started: true,
      turn_id: "turn_9",
      results: [{ intent_id: "a", ok: true, status: "approved", turn_id: "turn_9" }],
    });
    const w = mount(IntentLayer, {
      props: {
        views: [view()],
        boardId: "board_1",
        width: 1200,
        height: 800,
        selectedId: "a",
      },
    });
    await flushPromises();

    await w.get('[data-im="intent-confirm"]').trigger("click");
    await flushPromises();

    expect(w.find('[data-im="intent-layer-error"]').exists()).toBe(false);
    const notice = w.get('[data-im="intent-layer-notice"]');
    expect(notice.text()).toContain("已确认");
    expect(notice.text()).not.toContain("没有开始执行");
    w.unmount();
  });

  it("同批确认 started=false：错误行同样带原因", async () => {
    setActivePinia(createPinia());
    mocks.batchConfirmIntents.mockResolvedValue({
      started: false,
      turnStart: { code: "topic_unresolved", reason: "原提交保存的话题无法确认" },
      results: [
        { intent_id: "a", ok: true, status: "approved" },
        { intent_id: "b", ok: true, status: "approved" },
      ],
    });
    const w = mount(IntentLayer, {
      props: {
        views: [
          view({ intent_id: "a", title: "一" }),
          view({ intent_id: "b", title: "二" }),
        ],
        boardId: "board_1",
        width: 1200,
        height: 800,
      },
    });
    await flushPromises();

    for (const check of w.findAll('[data-im="intent-check"]')) await check.setValue(true);
    await w.get('[data-im="intent-batch-confirm"]').trigger("click");
    await flushPromises();

    const error = w.find('[data-im="intent-layer-error"]');
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("原提交话题无法确认");
    w.unmount();
  });
});
