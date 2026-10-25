/**
 * F10 行为回归：意图位置保存失败时显示与持久化一致。
 *
 * 基线缺陷：IntentLayer 的 localDisplay（拖动乐观值）永远覆盖父级 displayOverrides，
 * 保存失败后父级整块回滚也没用（卡片停在未保存的位置）；
 * InteractiveView.onIntentDisplayChange 失败时整块恢复旧对象，并写回 localStorage，
 * 既会误回滚其他卡片的并发改动，也会让本机缓存长期覆盖更新的服务器位置。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  api: {
    fetchBoardState: vi.fn(),
    fetchVisibleRange: vi.fn(),
    fetchIntents: vi.fn(),
    fetchIntentViews: vi.fn(),
    fetchUnderstanding: vi.fn(),
    fetchExecutions: vi.fn(),
    fetchDrafts: vi.fn(),
    saveBoardState: vi.fn(),
    checkMaterialImpact: vi.fn(),
    submitBoard: vi.fn(),
    fetchAcceptance: vi.fn(),
    createIntents: vi.fn(),
    saveDrafts: vi.fn(),
    approveIntent: vi.fn(),
    rejectIntent: vi.fn(),
    batchDecide: vi.fn(),
    updateIntentPreview: vi.fn(),
    advanceIntent: vi.fn(),
    pauseIntent: vi.fn(),
    resumeIntent: vi.fn(),
    cancelIntent: vi.fn(),
    previewMaterialImpact: vi.fn(),
    retryUnderstanding: vi.fn(),
    saveIntentDisplay: vi.fn(),
  },
}));
vi.mock("../../../../services/interactive", () => mocks.api);

import { useInteractiveStore } from "../../../../stores/interactive";
import { IntentDisplayAuthority } from "../intentDisplayState";
import { VIEW_STUBS, byIm } from "../../../../views/__tests__/i2v2-a-harness";
import InteractiveView from "../../../../views/InteractiveView.vue";
import { mount as mountView } from "@vue/test-utils";

const STAMP = "2026-10-25T00:00:00.000Z";

function emptyPayload() {
  return {
    board: { id: "board_default", title: "互动板面", seq: 1 },
    seq: 1,
    state: { boardId: "board_default", seq: 1, cards: [], groups: [], links: [], updatedAt: STAMP },
    submissions: [],
    drafts: { drafts: {}, updatedAt: null, rev: 1, keyVersions: {} },
  };
}

function view(intentId: string, x: number, y: number) {
  return {
    intent_id: intentId,
    board_id: "board_default",
    version: 1,
    status: "proposed",
    title: "意图 " + intentId,
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    display: { x, y },
    created_at: STAMP,
    updated_at: STAMP,
  };
}

function mountPage() {
  return mountView(InteractiveView, {
    attachTo: document.body,
    global: { stubs: VIEW_STUBS as never },
  });
}

function positionOf(wrapper: ReturnType<typeof mountPage>, intentId: string): { x: number; y: number } {
  const card = wrapper
    .findAll('[data-im="virtual-intent"]')
    .find((item) => item.attributes("data-intent-id") === intentId);
  const style = card?.attributes("style") ?? "";
  const read = (name: string) => Number(new RegExp(name + ":\\s*(-?[0-9.]+)px").exec(style)?.[1] ?? NaN);
  return { x: read("left"), y: read("top") };
}

function drag(
  wrapper: ReturnType<typeof mountPage>,
  intentId: string,
  from: { x: number; y: number },
  to: { x: number; y: number },
): void {
  const card = wrapper
    .findAll('[data-im="virtual-intent"]')
    .find((item) => item.attributes("data-intent-id") === intentId)!;
  card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: from.x, clientY: from.y, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointermove", { clientX: to.x, clientY: to.y, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointerup", { clientX: to.x, clientY: to.y, bubbles: true }));
}

function cancelDrag(
  wrapper: ReturnType<typeof mountPage>,
  intentId: string,
  from: { x: number; y: number },
  to: { x: number; y: number },
): void {
  const card = wrapper
    .findAll('[data-im="virtual-intent"]')
    .find((item) => item.attributes("data-intent-id") === intentId)!;
  card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: from.x, clientY: from.y, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointermove", { clientX: to.x, clientY: to.y, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointercancel", { clientX: to.x, clientY: to.y, bubbles: true }));
}

beforeEach(() => {
  vi.resetAllMocks();
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  localStorage.clear();
  const api = mocks.api;
  api.fetchBoardState.mockResolvedValue(emptyPayload());
  api.fetchVisibleRange.mockResolvedValue({ visibleRange: { cardIds: [], groupIds: [] } });
  api.fetchIntents.mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } });
  api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [] });
  api.fetchUnderstanding.mockResolvedValue({ understandings: [] });
  api.fetchExecutions.mockResolvedValue({ executions: [] });
  api.fetchDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.saveDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
});

describe("F10 位置状态归属（纯逻辑）", () => {
  it("区分拖动临时位置、等待保存位置、已保存位置", () => {
    const authority = new IntentDisplayAuthority();
    authority.applyServerViews([{ intent_id: "a", display: { x: 100, y: 100 } }]);
    expect(authority.snapshot().overrides.a).toEqual({ x: 100, y: 100 });
    expect(authority.snapshot().states.a).toBeUndefined();

    const op = authority.begin("a", { x: 300, y: 300 });
    expect(authority.snapshot().overrides.a).toEqual({ x: 300, y: 300 });
    expect(authority.snapshot().states.a?.phase).toBe("pending");

    authority.settle("a", op, { x: 300, y: 300 });
    expect(authority.snapshot().states.a).toBeUndefined();
    expect(authority.snapshot().overrides.a).toEqual({ x: 300, y: 300 });
  });

  it("保存失败恢复「这次操作前」的位置并给出原因", () => {
    const authority = new IntentDisplayAuthority();
    authority.applyServerViews([{ intent_id: "a", display: { x: 100, y: 100 } }]);
    const op = authority.begin("a", { x: 300, y: 300 });
    authority.fail("a", op, "网络断开");
    expect(authority.snapshot().overrides.a).toEqual({ x: 100, y: 100 });
    expect(authority.snapshot().states.a).toEqual({ phase: "failed", error: "网络断开" });
    expect(authority.snapshot().failures).toEqual([{ intentId: "a", error: "网络断开" }]);
  });

  it("旧失败不能撤销新成功；一张卡失败不回退其他卡", () => {
    const authority = new IntentDisplayAuthority();
    authority.applyServerViews([
      { intent_id: "a", display: { x: 100, y: 100 } },
      { intent_id: "b", display: { x: 200, y: 200 } },
    ]);
    const firstOp = authority.begin("a", { x: 300, y: 300 });
    const secondOp = authority.begin("a", { x: 400, y: 400 });
    const otherOp = authority.begin("b", { x: 500, y: 500 });
    authority.settle("a", secondOp, { x: 400, y: 400 });
    authority.settle("b", otherOp, { x: 500, y: 500 });
    authority.fail("a", firstOp, "旧请求失败");
    expect(authority.snapshot().overrides.a).toEqual({ x: 400, y: 400 });
    expect(authority.snapshot().overrides.b).toEqual({ x: 500, y: 500 });
    expect(authority.snapshot().failures).toEqual([]);
  });

  it("服务器读取到的位置优先于本机缓存；意图不在清单里时清理本地记录", () => {
    const authority = new IntentDisplayAuthority({ a: { x: 10, y: 10 } });
    authority.applyServerViews([{ intent_id: "a", display: { x: 200, y: 200 } }]);
    expect(authority.snapshot().overrides.a).toEqual({ x: 200, y: 200 });
    // 服务端这次没给 display（旧映射）：保留已知位置，不退回本机旧缓存
    authority.applyServerViews([{ intent_id: "a" }]);
    expect(authority.snapshot().overrides.a).toEqual({ x: 200, y: 200 });
    // 意图已经不在清单里：本地位置一并清理（不会套到别的新意图上）
    authority.applyServerViews([]);
    expect(authority.snapshot().overrides.a).toBeUndefined();
  });
});

describe("F10 页面行为：失败回退 / 服务器位置生效 / 取消拖动", () => {
  it("保存失败时卡片回到这次操作前的位置，并如实说明（不再卡在未保存位置）", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [view("a", 100, 100)] });
    mocks.api.saveIntentDisplay.mockRejectedValueOnce(new Error("位置保存接口不可用"));
    const wrapper = mountPage();
    await flushPromises();
    expect(positionOf(wrapper, "a")).toEqual({ x: 100, y: 100 });

    drag(wrapper, "a", { x: 110, y: 110 }, { x: 310, y: 310 });
    await flushPromises();
    expect(mocks.api.saveIntentDisplay).toHaveBeenCalledWith("a", "board_default", 300, 300);
    expect(positionOf(wrapper, "a"), "失败必须回到操作前位置").toEqual({ x: 100, y: 100 });
    expect(byIm(wrapper, "intent-display-error")[0].text()).toContain("位置保存失败");
    wrapper.unmount();
  });

  it("后续服务器读取的位置能生效（本机旧覆盖不许长期压住服务器事实）", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [view("a", 100, 100)] });
    mocks.api.saveIntentDisplay.mockResolvedValue({ ok: true, display: { x: 300, y: 300 } });
    const wrapper = mountPage();
    await flushPromises();
    drag(wrapper, "a", { x: 110, y: 110 }, { x: 310, y: 310 });
    await flushPromises();
    expect(positionOf(wrapper, "a")).toEqual({ x: 300, y: 300 });

    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [view("a", 40, 50)] });
    await useInteractiveStore().loadIntentViews();
    await flushPromises();
    expect(positionOf(wrapper, "a"), "服务器事实必须能覆盖已结束操作的临时位置").toEqual({ x: 40, y: 50 });
    wrapper.unmount();
  });

  it("快速拖不同意图：一张保存失败不回退另一张的成功位置", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [view("a", 100, 100), view("b", 200, 200)],
    });
    mocks.api.saveIntentDisplay.mockImplementation(async (intentId: string) => {
      if (intentId === "a") throw new Error("这张卡保存失败");
      return { ok: true, display: { x: 500, y: 500 } };
    });
    const wrapper = mountPage();
    await flushPromises();
    drag(wrapper, "a", { x: 110, y: 110 }, { x: 310, y: 310 });
    drag(wrapper, "b", { x: 210, y: 210 }, { x: 510, y: 510 });
    await flushPromises();
    expect(positionOf(wrapper, "a")).toEqual({ x: 100, y: 100 });
    expect(positionOf(wrapper, "b")).toEqual({ x: 500, y: 500 });
    wrapper.unmount();
  });

  it("取消拖动恢复本次手势开始位置（不是更早的默认位置）", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [view("a", 100, 100)] });
    mocks.api.saveIntentDisplay.mockRejectedValueOnce(new Error("先失败一次"));
    const wrapper = mountPage();
    await flushPromises();
    drag(wrapper, "a", { x: 110, y: 110 }, { x: 310, y: 310 });
    await flushPromises();
    const before = positionOf(wrapper, "a");
    cancelDrag(wrapper, "a", { x: before.x + 10, y: before.y + 10 }, { x: before.x + 200, y: before.y + 200 });
    await flushPromises();
    expect(positionOf(wrapper, "a")).toEqual(before);
    wrapper.unmount();
  });
});
