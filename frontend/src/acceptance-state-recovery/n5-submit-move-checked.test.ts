/**
 * 【独立验收 D · 第八轮】N5：提交成功带来的勾选清理，不许被「保护本地移动」挡掉，
 * 也不许把旧勾选写回、不许自动再提交。
 *
 * 触发（契约 §1.1/§1.2 / 本轮反例 N5）：
 *   注释已勾选 → 开始提交 → 等待返回时仅移动卡片 → 服务端成功并取消勾选 →
 *   本页保护未保存移动、拒绝整板回读 → 后续保存仍携带旧 checked=true。
 * 正确行为：区分本次提交选择的清理与后来编辑；按对象与选择版本应用 checkedCleared；
 *   用户后来主动重新勾选的新版不被旧回执取消；不把旧选择写回；禁止自动再提交。
 *
 * 本文件在 b3245e5 上必须失败（红）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import type { BoardState } from "../interactive/types";
import { card, clone, deferred, payload, state } from "./support/harness";

vi.mock("../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
}));

const MOVED_X = 512;

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(
    payload(3, [card("c1", "注释正文", { checked: true }), card("c2", "第二张")]),
  );
  vi.mocked(api.saveBoardState).mockImplementation(
    async (_boardId, putState) =>
      ({ ok: true, seq: 4, savedAt: "t", state: putState }) as never,
  );
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk",
    stateVersion: 3,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("N5 提交成功时的独立移动与勾选清理", () => {
  it("服务端清掉的勾选要生效、本地移动要保留，且不许把旧勾选写回或自动再提交", async () => {
    const store = useInteractiveStore();
    await store.load();
    expect(store.board!.cards.find((c) => c.id === "c1")!.checked).toBe(true);

    const submitting = deferred();
    vi.mocked(api.submitBoard).mockImplementation(() => submitting.promise as never);

    const submitPromise = store.submit();
    await flushPromises();
    expect(api.submitBoard).toHaveBeenCalledTimes(1);

    // 提交在飞期间：用户移动 c1，并主动勾选了 c2（都是这次提交之外的独立选择）
    const moved = clone(store.board!);
    moved.cards = moved.cards.map((c) => {
      if (c.id === "c1") return { ...c, x: MOVED_X };
      if (c.id === "c2") return { ...c, checked: true };
      return c;
    });
    store.commit(moved, "移动卡片并勾选第二张");

    // 服务端提交成功：它按这次提交的选择清掉了 c1 的勾选（板面版本前进到 4）
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      payload(4, [card("c1", "注释正文", { checked: false }), card("c2", "第二张")]),
    );
    submitting.resolve({
      status: "succeeded",
      submission: { id: "s1", seq: 4, status: "succeeded", createdAt: "" },
      before: { cards: [], groups: [], links: [], selection: [], empty: false },
      after: { cards: [], groups: [], links: [], selection: [], empty: false },
      expressions: [],
      baseline: { updated: true, previousSeq: 3, seq: 4 },
      delivery: { delivered: false, reason: "", detail: "" },
      visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
      checkedCleared: ["c1"],
    } as never);
    await submitPromise;
    await flushPromises();

    const c1 = store.board!.cards.find((c) => c.id === "c1")!;
    const c2 = store.board!.cards.find((c) => c.id === "c2")!;
    // 正确行为 A：这次提交真实清掉的勾选要生效（不能因为本地有未保存移动就整块拒绝服务器事实）
    expect(c1.checked, "提交真实清掉的勾选没有生效：本页仍带着旧勾选（N5）").toBe(false);
    // 正确行为 B：等待期间的独立移动必须保留
    expect(c1.x, "提交期间的独立移动被丢弃").toBe(MOVED_X);
    // 正确行为 C：用户后来主动勾选的新版不被旧回执取消
    expect(c2.checked, "用户后来主动勾选的选择被旧的回执取消了").toBe(true);
    // 正确行为 D：不许自动再提交
    expect(api.submitBoard).toHaveBeenCalledTimes(1);

    // 后续保存不得把旧勾选写回去
    const puts: BoardState[] = [];
    vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
      puts.push(putState as BoardState);
      return { ok: true, seq: 5, savedAt: "t", state: putState } as never;
    });
    await store.saveNow();
    await flushPromises();
    expect(puts.length, "这次保存没有真的发出").toBeGreaterThan(0);
    const last = puts[puts.length - 1];
    expect(
      last.cards.find((c) => c.id === "c1")!.checked,
      "后续保存把旧勾选 checked=true 写了回去（N5）",
    ).toBe(false);
    expect(last.cards.find((c) => c.id === "c1")!.x).toBe(MOVED_X);
    expect(last.cards.find((c) => c.id === "c2")!.checked).toBe(true);
    expect(api.submitBoard).toHaveBeenCalledTimes(1);
  });
});
