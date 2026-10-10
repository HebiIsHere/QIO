/**
 * 【独立验收 D · 第八轮】N1：保存前检查乱序，被替代的检查不许再发出旧候选。
 *
 * 触发（契约 §1.3 / 本轮反例 N1）：
 *   第一版检查未结束时完成第二版；第二次检查先结束并保存第二版；第一次检查迟到后仍保存第一版。
 * 基线现状：第一版的检查迟到后照样发出 PUT，用旧候选覆盖刚保存成功的新版。
 * 正确行为：任何 await 之后若候选已不是当前候选（boardLocalRev 变了），这次保存不得再发出旧候选；
 *   服务端同时按真实版本事实保护普通整板写入（409 stale_state，不落库）——后者由后端反例覆盖。
 *
 * 本文件在 b3245e5 上必须失败（红）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import type { BoardState } from "../interactive/types";
import { card, deferred, intent, payload, state } from "./support/harness";

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

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", "服务器原文")]));
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
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

describe("N1 保存前检查乱序", () => {
  it("被第二版取代的第一版检查迟到后，不得再发出第一版候选", async () => {
    const firstCheck = deferred();
    const secondCheck = deferred();
    const checks = [firstCheck, secondCheck];
    vi.mocked(api.checkMaterialImpact).mockImplementation(
      () => checks.shift()!.promise as never,
    );
    const puts: BoardState[] = [];
    vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
      const put = putState as BoardState;
      puts.push(put);
      return { ok: true, seq: 4 + puts.length, savedAt: "t", state: put } as never;
    });

    const store = useInteractiveStore();
    await store.load();

    // 第一版：检查开始、还没结束
    store.commit(state(3, [card("c1", "第一版")]), "第一版");
    const saving = store.saveNow();
    await flushPromises();

    // 第一版检查在飞时用户完成了第二版
    store.commit(state(3, [card("c1", "第二版")]), "第二版");
    const savingSecond = store.saveNow();
    await flushPromises();

    // 第二次检查先结束 → 第二版保存成功
    secondCheck.resolve({
      ok: true,
      checkId: "chk_second",
      stateVersion: 3,
      affected: [],
      impactConfirmationRequired: false,
    });
    await flushPromises();
    expect(puts.map((p) => p.cards[0].content)).toEqual(["第二版"]);

    // 第一次检查迟到结束：它对应的候选（第一版）已经被第二版取代
    firstCheck.resolve({
      ok: true,
      checkId: "chk_first",
      stateVersion: 3,
      affected: [],
      impactConfirmationRequired: false,
    });
    await Promise.all([saving, savingSecond]);
    await flushPromises();

    expect(
      puts.map((p) => p.cards[0].content),
      "被第二版取代的第一版检查迟到后仍然发出了旧候选（N1）",
    ).toEqual(["第二版"]);
  });
});
