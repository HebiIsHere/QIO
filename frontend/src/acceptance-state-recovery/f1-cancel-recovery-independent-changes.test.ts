/**
 * 【独立验收 D · 第八轮】F1：取消影响确认后，恢复读取还在飞时的独立操作
 * 不许「携带被取消的正文」，也不许丢失。
 *
 * 触发（契约 §5 / 本轮反例 F1）：
 *   编辑运行任务依赖的材料 → 出现确认 → 取消 → 恢复 GET 还未返回时移动卡片 → GET 返回。
 * 基线现状：位置移动保留了，但被取消掉的正文还在；这次移动候选是从**尚未恢复**的板面复制的。
 * 正确行为：
 *   - 取消只撤回对应的候选：被取消的正文不得留在正式板面，也不得被后续保存写回；
 *   - 等待期间的独立变化（位置 / 勾选 / 新卡片）保留；
 *   - 恢复失败如实显示原因、保留可处理状态，不宣称撤回成功。
 *
 * 本文件在 b3245e5 上必须失败（红）；修复后同一份文件必须全部通过。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import type { BoardState } from "../interactive/types";
import { apiError, card, clone, deferred, intent, payload, state } from "./support/harness";

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

const SERVER_TEXT = "服务器原文";
const CANCELLED_TEXT = "被取消掉的新正文";

function serverBoard(): never {
  return payload(3, [card("c1", SERVER_TEXT), card("c2", "第二张")]);
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(serverBoard());
  vi.mocked(api.saveBoardState).mockResolvedValue({
    ok: true,
    seq: 4,
    savedAt: "t",
    state: state(4, [card("c1", SERVER_TEXT)]),
  } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  // 有执行中任务，保存前会走影响预判；预判命中 → 进入「等待用户确认」，不落库。
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk_f1",
    stateVersion: 3,
    affected: [
      { intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停并保留进度" },
    ],
    impactConfirmationRequired: true,
  } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [intent("A")],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("F1 取消恢复等待期间的独立操作", () => {
  it("取消的正文不许留下、不许被写回；等待期间的独立移动必须保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    expect(store.board!.cards.map((c) => c.content)).toEqual([SERVER_TEXT, "第二张"]);

    // 1) 用户改动了运行任务依赖的材料 → 保存被影响门拦下，等用户决定
    store.commit(state(3, [card("c1", CANCELLED_TEXT), card("c2", "第二张")]), "改正文");
    await store.saveNow();
    expect(store.pendingImpact).not.toBeNull();
    expect(api.saveBoardState).not.toHaveBeenCalled();

    // 2) 用户取消；恢复 GET 被挂起（真实网络还在飞）
    const read = deferred();
    vi.mocked(api.fetchBoardState).mockImplementation(() => read.promise as never);
    const cancelling = store.cancelImpact();
    await flushPromises();

    // 3) 恢复还没返回时，用户移动了另一张卡片（真实的移动是从**当前**板面复制的，
    //    而当前板面此刻仍然带着被取消的正文 —— 这正是基线的问题所在）
    const moved = clone(store.board!);
    moved.cards = moved.cards.map((c) => (c.id === "c2" ? { ...c, x: 640, y: 320 } : c));
    store.commit(moved, "移动卡片");
    expect(store.board!.cards.find((c) => c.id === "c2")!.x).toBe(640);

    // 4) 恢复 GET 这时才返回：服务器上是取消前的已保存内容，且不知道刚才的移动
    read.resolve(serverBoard());
    await cancelling;
    await flushPromises();

    // 正确行为 A：被取消的正文不许留在正式板面上
    expect(
      store.board!.cards.find((c) => c.id === "c1")!.content,
      "取消只撤回了版本记账，被取消的正文仍留在板面上（F1）",
    ).toBe(SERVER_TEXT);
    // 正确行为 B：等待期间的独立移动保留
    expect(store.board!.cards.find((c) => c.id === "c2")!.x, "等待期间的独立移动被丢弃").toBe(640);
    // 正确行为 C：移动仍是未保存的候选，状态如实
    expect(store.dirty).toBe(true);

    // 正确行为 D：随后真的保存一次，取消掉的正文不得重新落库
    let saved: BoardState | null = null;
    vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
      saved = putState as BoardState;
      return { ok: true, seq: 4, savedAt: "t", state: putState } as never;
    });
    await store.saveNow();
    await flushPromises();
    expect(saved, "这次保存没有真的发出").not.toBeNull();
    expect(
      saved!.cards.find((c) => c.id === "c1")!.content,
      "取消掉的正文被后续保存重新写了回去（F1）",
    ).toBe(SERVER_TEXT);
    expect(saved!.cards.find((c) => c.id === "c2")!.x, "独立移动没有被保存").toBe(640);
    expect(store.dirty).toBe(false);
  });

  it("恢复回读失败：如实显示真实原因、保留可处理状态，不宣称撤回成功", async () => {
    const store = useInteractiveStore();
    await store.load();

    store.commit(state(3, [card("c1", CANCELLED_TEXT), card("c2", "第二张")]), "改正文");
    await store.saveNow();
    expect(store.pendingImpact).not.toBeNull();

    vi.mocked(api.fetchBoardState).mockRejectedValue(
      apiError(503, { error: "read_failed", reason: "服务端暂时不可用（503）" }),
    );
    await store.cancelImpact();
    await flushPromises();

    // 正确行为：不假装撤回成功 —— 状态是失败、原因可见、候选仍按「未保存」保留可重试
    expect(store.saveStatus).toBe("error");
    expect(String(store.saveError ?? "")).toContain("503");
    expect(store.dirty).toBe(true);
  });
});
