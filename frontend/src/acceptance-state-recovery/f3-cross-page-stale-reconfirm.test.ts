/**
 * 【独立验收 D · 第八轮】F3：跨页面保存让本页确认过期时，本页必须能重新取得有效确认并保存。
 *
 * 触发（契约 §1.4/§1.5 / 本轮反例 F3）：
 *   本页版本 3 有正文候选与确认 → 另一页面独立移动并保存到 4 → 本页确认被拒 →
 *   本页读取最新服务器事实并重试。
 * 基线现状：被拒后仍以旧 seq 预判；保留候选时连最新服务器事实也不接收，拿不到有效的新确认。
 * 正确行为：保存本页候选；安全接收并核对最新服务器事实；独立变化可协调，
 *   不能只把 seq 改大再整板覆盖另一页面的改动；不得无限旧版预判或无解释等待。
 *
 * 本文件在 b3245e5 上必须失败（红）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import type { BoardState } from "../interactive/types";
import { apiError, card, intent, payload, state } from "./support/harness";

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

const MY_TEXT = "本页候选正文";
const OTHER_PAGE_X = 777;

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [intent("A")],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("F3 跨页面保存使确认过期", () => {
  it("被拒后必须读取最新事实、按最新版本重新确认并保存，且不覆盖另一页面的独立改动", async () => {
    // 服务端版本事实：本页开始时的 seq=3；另一页面随后独立移动并保存到 4
    let serverSeq = 3;
    const seq3Board = (): never => payload(3, [card("c1", "服务器已保存正文"), card("c2", "第二张")]);
    const seq4Board = (): never =>
      payload(4, [card("c1", "服务器已保存正文"), card("c2", "第二张", { x: OTHER_PAGE_X })]);
    vi.mocked(api.fetchBoardState).mockImplementation(async () => (serverSeq === 3 ? seq3Board() : seq4Board()));

    // 影响预判总是命中运行中的任务 A：所以每次都要用户明确确认才会真正保存
    vi.mocked(api.checkMaterialImpact).mockImplementation(
      async (_boardId, stateVersion) =>
        ({
          ok: true,
          checkId: "chk_" + String(stateVersion),
          stateVersion,
          affected: [
            { intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停并保留进度" },
          ],
          impactConfirmationRequired: true,
        }) as never,
    );

    const puts: BoardState[] = [];
    vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
      const put = putState as BoardState;
      puts.push(put);
      if (put.seq !== serverSeq) {
        throw apiError(409, {
          error: put.seq < serverSeq ? "stale_check" : "stale_state",
          reason: "服务端已有更新的已保存版本：" + String(serverSeq),
          currentSeq: serverSeq,
          affectedTasks: [
            { intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停并保留进度" },
          ],
        });
      }
      const savedSeq = serverSeq + 1;
      return { ok: true, seq: savedSeq, savedAt: "t", state: { ...put, seq: savedSeq } } as never;
    });

    const store = useInteractiveStore();
    await store.load();
    expect(store.board!.seq).toBe(3);

    // 本页产生正文候选（影响确认后等待用户决定）
    store.commit(state(3, [card("c1", MY_TEXT), card("c2", "第二张")]), "改正文");
    await store.saveNow();
    expect(store.pendingImpact, "影响门应当拦下这次保存并等用户确认").not.toBeNull();
    expect(puts.length).toBe(0);

    // 另一页面在等待期间独立移动 c2 并保存成功 → 服务端版本前进到 4
    serverSeq = 4;

    // 本页用户点「继续」→ 服务端因版本过期拒绝
    await store.confirmImpact();
    await flushPromises();

    // 正确行为 A：被拒之后前端必须重新取得可用的确认入口（不是无解释地停在失败上）
    expect(
      store.pendingImpact,
      "确认被服务端按真实版本事实拒绝后，前端没有重新读取最新事实并重新展示范围（F3）",
    ).not.toBeNull();

    // 本页用户再次确认 → 必须带上**最新**的服务器版本事实，且不得覆盖另一页面的独立改动
    await store.confirmImpact();
    await flushPromises();

    expect(puts.length, "重新确认之后仍然没有发出可用的保存").toBeGreaterThanOrEqual(2);
    const finalPut = puts[puts.length - 1];
    expect(
      finalPut.seq,
      "重新确认仍然用旧 seq 预判（不能只把 seq 改大，也不能绕过版本事实）",
    ).toBe(4);
    expect(
      finalPut.cards.find((c) => c.id === "c1")!.content,
      "本页候选正文没有被保存",
    ).toBe(MY_TEXT);
    expect(
      finalPut.cards.find((c) => c.id === "c2")!.x,
      "整板覆盖丢掉了另一页面的独立移动",
    ).toBe(OTHER_PAGE_X);

    // 正确行为 B：收敛 —— 不再有待确认、不再有未保存候选
    expect(store.pendingImpact).toBeNull();
    expect(store.dirty).toBe(false);
    expect(store.saveStatus).toBe("saved");
    expect(store.board!.seq).toBe(5);
  });
});
