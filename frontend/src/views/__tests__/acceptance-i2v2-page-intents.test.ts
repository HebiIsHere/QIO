/**
 * D 组独立验收 · F02 / F06 / F10：意图来源、读取失败与位置保存（页面级）。
 *
 * 行为契约：
 * - F02：页面上同一意图只能有**一份**表示（新链虚拟意图接管后，旧链预览不能再来一份）；
 *   延迟理解到达后意图列表自动同步（不需要手动刷新），乱序响应不得让旧列表覆盖新列表；
 * - F06：意图读取失败时保留最后一次成功内容并给出重试，旧失败不得清空列表、也不得覆盖新成功；
 * - F10：意图位置保存失败时，显示位置必须与已持久化位置一致，并如实说明没有保存；
 *   乱序（后发保存先成功、先发保存后失败）不得回退到旧位置、也不得误报失败。
 *
 * 基线问题：页面自己维护一份 intentViews（失败就清空、只在 lastSubmission 变化时刷新），
 * 与 store 的意图事实不同步；IntentLayer 的 localDisplay 又永远压过父级回滚。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Intent } from "../../interactive/types";
import type { IntentViewWithDisplay } from "../../components/interactive/v2/intentDisplay";
import {
  mountInteractivePage,
  resetServices,
  settlePage,
  stubServices,
  virtualIntent,
  type PageHarness,
} from "./acceptance-i2v2-page-harness";

const STAMP = "2026-10-25T00:00:00.000Z";

function view(id = "i1", overrides: Partial<IntentViewWithDisplay> = {}): IntentViewWithDisplay {
  return {
    intent_id: id,
    board_id: "board_t",
    version: 1,
    status: "proposed",
    title: "整理材料",
    goal: "把材料整理成清单",
    expected: "得到清单",
    scope: { read_card_ids: [], allowed_actions: [] },
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    operations_summary: { applied: 0, pending: 1 },
    display: { x: 100, y: 100 },
    created_at: STAMP,
    updated_at: STAMP,
    ...overrides,
  } as unknown as IntentViewWithDisplay;
}

function legacyIntent(id = "i1"): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    title: "整理材料",
    summary: "",
    status: "pending",
    preview: {
      cards: [{ id: "pc1", kind: "text", content: "预览卡", meta: {}, x: 120, y: 120, w: 160, h: 90 }],
      groups: [],
      links: [],
    },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  } as unknown as Intent;
}

function payload(intents: IntentViewWithDisplay[]) {
  return {
    available: true,
    source: "service",
    intents,
    batch_confirm: { min: 2, eligible_ids: [], available: true },
  } as never;
}

let harness: PageHarness | null = null;

beforeEach(() => {
  resetServices();
});

afterEach(() => {
  try {
    harness?.wrapper.unmount();
  } catch {
    /* 已经卸载 */
  }
  harness = null;
  resetServices();
  document.body.innerHTML = "";
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function dragIntent(wrapper: PageHarness["wrapper"], intentId: string, from: { x: number; y: number }, delta: { dx: number; dy: number }) {
  const card = virtualIntent(wrapper, intentId);
  expect(card, "虚拟意图卡片必须先渲染出来").toBeTruthy();
  card!.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: from.x, clientY: from.y, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointermove", { clientX: from.x + delta.dx, clientY: from.y + delta.dy, bubbles: true }));
  window.dispatchEvent(new MouseEvent("pointerup", { clientX: from.x + delta.dx, clientY: from.y + delta.dy, bubbles: true }));
}

describe("F02 唯一意图来源：同一意图只有一份表示，延迟理解自动同步", () => {
  it("新链虚拟意图与旧链预览同时具备数据时，页面上同一意图只表示一次", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue(payload([view("i1")]));
    harness = mountInteractivePage(undefined, { realBoard: true });
    await settlePage(harness.wrapper);
    // 旧链事实同时存在（与真实页面一致：store.load 会读入旧链意图）
    harness.store.intents = [legacyIntent("i1")];
    await settlePage(harness.wrapper);

    const virtual = harness.wrapper.findAll('[data-im="virtual-intent"][data-intent-id="i1"]');
    const legacyPreview = harness.wrapper.findAll('[data-im="preview"][data-intent-id="i1"]');
    expect(virtual.length, "新链虚拟意图是唯一可操作表示").toBe(1);
    expect(virtual.length + legacyPreview.length, "同一意图不能同时以新旧两份列表出现").toBe(1);
  });

  it("延迟理解到达（store 的意图事实更新）后页面自动同步，不需要手动刷新", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValueOnce(payload([]));
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    expect(harness.wrapper.findAll('[data-im="virtual-intent"]')).toHaveLength(0);
    const callsBefore = services.fetchIntentViews.mock.calls.length;

    // 理解完成：store 收到新的意图事实（与真实提交后的同步路径一致）
    services.fetchIntentViews.mockResolvedValue(payload([view("i_after", { title: "理解后出现的任务" })]));
    harness.store.understandingStatus = "understood";
    harness.store.intents = [legacyIntent("i_after")];
    await settlePage(harness.wrapper);

    expect(services.fetchIntentViews.mock.calls.length, "页面必须跟随事实重新读取意图视图").toBeGreaterThan(callsBefore);
    expect(virtualIntent(harness.wrapper, "i_after"), "延迟理解产生的意图必须自动出现在板面上").toBeTruthy();
  });

  it("两次读取乱序：先发出的旧回执后到，不得覆盖后发出的新列表", async () => {
    const services = stubServices();
    const first = deferred<never>();
    const second = deferred<never>();
    services.fetchIntentViews.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    expect(services.fetchIntentViews, "首屏已经发起第一次读取").toHaveBeenCalledTimes(1);

    // 第二次读取（提交后刷新同一条路径）先返回新列表
    harness.store.lastSubmission = { submission: { id: "sub_2" } } as never;
    await harness.wrapper.vm.$nextTick();
    expect(services.fetchIntentViews, "事实变化必须发起第二次读取").toHaveBeenCalledTimes(2);
    second.resolve(payload([view("i_new", { title: "新列表里的任务" })]));
    await settlePage(harness.wrapper);
    expect(virtualIntent(harness.wrapper, "i_new")).toBeTruthy();

    // 先发出的旧读取这时才回来：不许把新列表覆盖成旧的
    first.resolve(payload([view("i_old", { title: "旧列表里的任务" })]));
    await settlePage(harness.wrapper);
    expect(virtualIntent(harness.wrapper, "i_new"), "旧回执不得覆盖新列表").toBeTruthy();
    expect(virtualIntent(harness.wrapper, "i_old")).toBeFalsy();
  });
});

describe("F06 意图读取失败：保留旧内容 + 重试，旧失败不覆盖新成功", () => {
  it("刷新失败时保留最后一次成功内容，并给出可重试的失败说明", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValueOnce(payload([view("i1")]));
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    expect(virtualIntent(harness.wrapper, "i1")).toBeTruthy();

    services.fetchIntentViews.mockRejectedValueOnce(new Error("网络断了"));
    harness.store.lastSubmission = { submission: { id: "sub_2" } } as never;
    await settlePage(harness.wrapper);
    await harness.wrapper.vm.$nextTick();
    await settlePage(harness.wrapper);

    expect(virtualIntent(harness.wrapper, "i1"), "读取失败不是意图消失，旧内容必须保留").toBeTruthy();
    const text = harness.wrapper.text();
    expect(text).toContain("失败");
    expect(harness.wrapper.findAll("button").some((button) => button.text().includes("重试")), "必须给重试入口").toBe(true);
  });

  it("重试成功后显示新内容并清掉旧失败提示（旧失败不覆盖新成功）", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValueOnce(payload([view("i1")]));
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);

    services.fetchIntentViews.mockRejectedValueOnce(new Error("网络断了"));
    harness.store.lastSubmission = { submission: { id: "sub_2" } } as never;
    await settlePage(harness.wrapper);

    services.fetchIntentViews.mockResolvedValueOnce(payload([view("i1"), view("i2", { title: "重试后读到的任务" })]));
    const retry = harness.wrapper.findAll("button").find((button) => button.text().includes("重试"));
    expect(retry, "失败提示里必须有重试按钮").toBeTruthy();
    await retry!.trigger("click");
    await settlePage(harness.wrapper);

    expect(virtualIntent(harness.wrapper, "i2")).toBeTruthy();
    expect(harness.wrapper.text()).not.toContain("网络断了");
  });
});

describe("F10 意图位置保存：失败与乱序时显示和持久化一致", () => {
  it("位置保存失败：卡片回到已持久化位置，并如实说明没有保存", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue(payload([view("i1", { display: { x: 100, y: 100 } })]));
    harness = mountInteractivePage(undefined, { realBoard: true });
    await settlePage(harness.wrapper);
    expect(virtualIntent(harness.wrapper, "i1")).toBeTruthy();

    const save = deferred<never>();
    services.saveIntentDisplay.mockReturnValue(save.promise);
    dragIntent(harness.wrapper, "i1", { x: 120, y: 120 }, { dx: 60, dy: 40 });
    await settlePage(harness.wrapper);
    expect(services.saveIntentDisplay, "松手必须尝试持久化位置").toHaveBeenCalled();

    save.reject(new Error("网络断了"));
    await settlePage(harness.wrapper);

    const style = virtualIntent(harness.wrapper, "i1")!.attributes("style") ?? "";
    expect(style, "保存失败后显示位置必须回到已持久化位置").toContain("left: 100px");
    expect(style).toContain("top: 100px");
    expect(harness.wrapper.text()).toContain("没有保存");
  });

  it("乱序：后发保存先成功、先发保存后失败，不回退位置也不误报失败", async () => {
    const services = stubServices();
    services.fetchIntentViews.mockResolvedValue(payload([view("i1", { display: { x: 100, y: 100 } })]));
    harness = mountInteractivePage(undefined, { realBoard: true });
    await settlePage(harness.wrapper);

    const first = deferred<never>();
    const second = deferred<never>();
    services.saveIntentDisplay.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    dragIntent(harness.wrapper, "i1", { x: 120, y: 120 }, { dx: 40, dy: 20 });
    await settlePage(harness.wrapper);
    dragIntent(harness.wrapper, "i1", { x: 160, y: 150 }, { dx: 40, dy: 20 });
    await settlePage(harness.wrapper);

    // 后发的那次保存成功，随后先发的那次才失败（迟到失败）
    second.resolve({ ok: true, display: { x: 180, y: 140 } } as never);
    await settlePage(harness.wrapper);
    first.reject(new Error("网络断了"));
    await settlePage(harness.wrapper);

    const style = virtualIntent(harness.wrapper, "i1")!.attributes("style") ?? "";
    expect(style, "后发且已成功的位置不能被迟到的旧失败回退").toContain("left: 180px");
    expect(harness.wrapper.text(), "迟到的旧失败不是当前事实，不许误报").not.toContain("没有保存");
  });
});
