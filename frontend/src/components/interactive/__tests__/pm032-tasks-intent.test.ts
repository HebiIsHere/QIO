/**
 * PM-QIO032 · i-tasks：意图层（#27 / #74 / 文案 10 / 文案 14）。
 *
 * 只喂真实字段形状（IntentView）：授权快照 / authorization_valid / authorization_invalidated /
 * authorization_changes / operations_summary；断言用户读到的措辞与真实事实一致。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  api: {
    confirmSingleIntent: vi.fn(),
    batchConfirmIntents: vi.fn(),
    fetchRecoveryPreview: vi.fn(),
    recoverIntent: vi.fn(),
    rejectIntent: vi.fn(),
  },
}));
vi.mock("../../../services/interactive", () => mocks.api);

import IntentLayer from "../v2/IntentLayer.vue";
import {
  authorizationNotice,
  intentProgressText,
  partialConfirmView,
  summarizeConfirm,
} from "../v2/intentDisplay";

const STAMP = "2026-10-25T00:00:00.000Z";

function view(overrides: Record<string, unknown> = {}) {
  return {
    intent_id: "i1",
    board_id: "board_1",
    version: 1,
    status: "proposed",
    title: "整理材料",
    goal: "整理",
    expected: "得到清单",
    scope: { read_card_ids: ["c1"], allowed_actions: ["add_text_card"] },
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    created_at: STAMP,
    updated_at: STAMP,
    ...overrides,
  } as never;
}

const mounted: VueWrapper[] = [];

function mountLayer(views: unknown[], extra: Record<string, unknown> = {}) {
  const wrapper = mount(IntentLayer, {
    props: { views: views as never, boardId: "board_1", width: 2400, height: 1600, ...extra },
    attachTo: document.body,
  });
  mounted.push(wrapper);
  return wrapper;
}

function cardOf(wrapper: VueWrapper, intentId: string) {
  return wrapper.findAll('[data-im="virtual-intent"]').find((item) => item.attributes("data-intent-id") === intentId)!;
}

function tap(wrapper: VueWrapper, intentId: string): void {
  const card = cardOf(wrapper, intentId);
  card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 10, clientY: 10, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointerup", { clientX: 10, clientY: 10, bubbles: true }));
}

beforeEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe("#27 授权：首次未授权「待确认」vs 原授权失效「需重新确认」", () => {
  it("首次未授权：待确认，不冒充「需重新确认」", async () => {
    const notice = authorizationNotice(view({ authorization_valid: false, status: "proposed" }) as never);
    expect(notice.state).toBe("pending");
    expect(notice.label).toBe("待确认");
    expect(notice.reason).not.toContain("需重新确认");

    const wrapper = mountLayer([view({ authorization_valid: false, status: "proposed" })]);
    await flushPromises();
    expect(cardOf(wrapper, "i1").get('[data-im="virtual-intent-status"]').text()).toBe("待确认");
    tap(wrapper, "i1");
    await flushPromises();
    expect(wrapper.find('[data-im="intent-auth-pending"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="intent-reauth-change"]').exists(), "首次未授权不是授权失效").toBe(false);
  });

  it("原授权因范围变化失效：需重新确认，并在确认按钮前给出真实变更", async () => {
    const reconfirmView = view({
      intent_id: "i1",
      status: "awaiting_confirmation",
      version: 3,
      authorization_valid: false,
      authorization_invalidated: true,
      authorization: { version: 2, scope: { read_card_ids: ["c1"] }, confirmed_at: STAMP },
      scope: { read_card_ids: ["c1", "c2", "c3"] },
      authorization_changes: ["范围：读取卡片 1 张 → 3 张"],
    });
    const notice = authorizationNotice(reconfirmView as never);
    expect(notice.state).toBe("reconfirm");
    expect(notice.label).toBe("需重新确认");
    expect(notice.changes.join("；")).toContain("读取卡片 1 张 → 3 张");

    const wrapper = mountLayer([reconfirmView]);
    await flushPromises();
    expect(cardOf(wrapper, "i1").get('[data-im="virtual-intent-status"]').text()).toBe("需重新确认");
    tap(wrapper, "i1");
    await flushPromises();
    const change = wrapper.get('[data-im="intent-reauth-change"]');
    expect(change.text()).toContain("读取卡片 1 张 → 3 张");
    // 真实变更必须在确认按钮之前（用户先看到变了什么，再决定按钮）
    const html = wrapper.html();
    expect(html.indexOf('data-im="intent-reauth-change"')).toBeLessThan(html.indexOf('data-im="intent-confirm"'));
  });

  it("服务端没给变更明细时如实说明，不编造具体变更", () => {
    const notice = authorizationNotice(
      view({ status: "failed", version: 4, authorization_valid: false, authorization_invalidated: true }) as never,
    );
    expect(notice.state).toBe("reconfirm");
    expect(notice.changes).toEqual([]);
    expect(notice.reason).toContain("未返回具体变更");
  });
});

describe("文案 14 已确认意图显示实际进度；重复确认真的被拦住时才说原因", () => {
  it("已确认意图显示真实进度，卡片不常态显示「不能重复确认」", async () => {
    const approved = view({
      intent_id: "i-run",
      status: "approved",
      operations_summary: { applied: 1, running: 1, pending: 1 },
    });
    expect(intentProgressText(approved as never)).toContain("已完成 1 / 共 3");

    const wrapper = mountLayer([approved]);
    await flushPromises();
    const card = cardOf(wrapper, "i-run");
    expect(card.get('[data-im="intent-progress"]').text()).toContain("已完成 1 / 共 3");
    expect(card.text()).not.toContain("已经开始过");
    expect(card.find('[data-im="intent-block"]').exists()).toBe(false);

    tap(wrapper, "i-run");
    await flushPromises();
    expect(wrapper.find('[data-im="intent-detail-progress"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="intent-needs-update"]').exists()).toBe(false);
  });

  it("真的重复确认被拦住时才提示原因（不发出第二次确认请求）", async () => {
    const approved = view({ intent_id: "i-run", status: "approved", operations_summary: { applied: 1 } });
    const wrapper = mountLayer([approved]);
    await flushPromises();
    (wrapper.vm as unknown as { confirmOne: (v: unknown) => Promise<void> }).confirmOne(approved);
    await flushPromises();
    expect(wrapper.get('[data-im="intent-layer-error"]').text()).toContain("不能重复确认");
    expect(mocks.api.confirmSingleIntent).not.toHaveBeenCalled();
  });
});

describe("文案 10 部分完成：标题 / 成功项 / 失败项 / 重试入口", () => {
  it("同批部分失败分开列，并可只重试失败项", async () => {
    mocks.api.batchConfirmIntents.mockResolvedValue({
      batch: true,
      results: [
        { intent_id: "i1", ok: true, status: "approved", turn_id: "turn_a" },
        { intent_id: "i3", ok: false, reason: "依赖未就绪" },
      ],
    });
    mocks.api.confirmSingleIntent.mockResolvedValue({
      results: [{ intent_id: "i3", ok: true, status: "approved", turn_id: "turn_b" }],
    });
    const wrapper = mountLayer([
      view({ intent_id: "i1", status: "proposed" }),
      view({ intent_id: "i3", status: "awaiting_confirmation" }),
    ]);
    for (const id of ["i1", "i3"]) {
      await cardOf(wrapper, id).find('[data-im="intent-check"]').setValue(true);
    }
    await flushPromises();
    await wrapper.get('[data-im="intent-batch-confirm"]').trigger("click");
    await flushPromises();

    expect(wrapper.get('[data-im="intent-confirm-partial-title"]').text()).toBe("部分完成");
    expect(wrapper.get('[data-im="intent-confirm-succeeded"]').text()).toContain("i1");
    expect(wrapper.get('[data-im="intent-confirm-partial"]').text()).toContain("i3");
    const retry = wrapper.get('[data-im="intent-confirm-retry-failed"]');
    expect(retry.text()).toContain("重试失败项");
    await retry.trigger("click");
    await flushPromises();
    expect(mocks.api.confirmSingleIntent, "只重试失败项的显式 id").toHaveBeenCalledWith("i3", "board_1");
    expect(mocks.api.batchConfirmIntents).toHaveBeenCalledTimes(1);

    // 纯逻辑：标题/成功/失败/可重试 id 都来自真实 summary
    const summary = summarizeConfirm(
      { results: [{ intent_id: "a", ok: true, status: "approved" }, { intent_id: "b", ok: false, reason: "x" }] } as never,
      ["a", "b"],
    );
    const partial = partialConfirmView(summary);
    expect(partial.title).toBe("部分完成");
    expect(partial.succeeded).toEqual(["a"]);
    expect(partial.failed).toEqual(["b"]);
    expect(partial.retryable).toEqual(["b"]);
  });
});

describe("#74 意图层批量区：零个可处理项时收起", () => {
  it("没有可处理待确认项时不占一块批量条", async () => {
    const wrapper = mountLayer([view({ intent_id: "i1", status: "needs_update" })]);
    await flushPromises();
    expect(wrapper.find('[data-im="intent-batch-bar"]').exists()).toBe(false);
  });

  it("存在可处理待确认项时显示批量区", async () => {
    const wrapper = mountLayer([view({ intent_id: "i1", status: "proposed" })]);
    await flushPromises();
    expect(wrapper.find('[data-im="intent-batch-bar"]').exists()).toBe(true);
  });
});
