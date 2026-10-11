/**
 * 【审计 8fa8452 · F08】保存已确认，旧读取却回退版本。
 *
 * 反例（基线 saveNow + knownServerSeq）：已知服务器版本只在「这次保存没有更新候选」时才推进。
 * 于是「保存被更新的候选接手」的那一次（服务端其实已经接受、seq 前进）不会记录新的已确认版本；
 * 随后一个**更旧**的 GET 会被当成「更新的读取」，把板面退回旧版本并造出针对旧正文的假冲突。
 *
 * 正确行为：服务器已确认的最高版本独立于候选正文、始终单调推进；旧于已确认事实的 GET
 * 不许退回板面版本 / 清洁基线，也不许制造内容冲突。
 *
 * 验收场景（任务书 F08）：seq=3 → 保存 version 1 → 本地 version 2 → 保存确认 seq=4 →
 * 旧 GET seq=3 晚到；最终保留 seq=4 与 version 2，且不产生针对旧正文的假冲突。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import type { BoardState } from "../interactive/types";
import { card, deferred, payload } from "./support/harness";

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

const OLD_TEXT = "服务器 seq3 的旧正文";
const V1 = "本地第一版正文";
const V2 = "本地第二版正文";

function seq3(): BoardState {
  return {
    boardId: "board_default",
    seq: 3,
    updatedAt: "t3",
    cards: [card("c1", OLD_TEXT)],
    groups: [],
    links: [],
    selection: [],
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t", rev: 1 } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", OLD_TEXT)]) as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("F08 已确认版本单调推进，旧 GET 不许回退", () => {
  it("保存被更新候选接手（服务端已确认 seq=4）后，迟到的 seq=3 读取不许回退板面或制造假冲突", async () => {
    const store = useInteractiveStore();
    await store.load();
    expect(store.board?.seq).toBe(3);

    // 本地第一版候选（version 1）开始保存，请求在飞
    const put = deferred<never>();
    vi.mocked(api.saveBoardState).mockReturnValue(put.promise as never);
    store.commit({ ...store.board!, cards: [card("c1", V1)] } as BoardState, "改正文");
    const saving = store.saveNow();
    await flushPromises();

    // 请求在飞期间用户继续编辑（本地 version 2）
    store.commit({ ...store.board!, cards: [card("c1", V2)] } as BoardState, "继续改");
    expect(store.dirty).toBe(true);

    // 服务端接受的是第一版：回执 seq=4（已确认事实）
    put.resolve({ ok: true, seq: 4, savedAt: "t4", state: { ...seq3(), seq: 4, cards: [card("c1", V1)] } } as never);
    await saving;
    await flushPromises();
    expect(store.board?.seq, "服务端已确认的版本事实必须被吸收").toBe(4);
    expect(store.board?.cards[0].content, "更新的候选正文不被旧回执覆盖").toBe(V2);

    // 旧 GET（seq=3，正文也是旧的）现在才返回
    vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", OLD_TEXT)]) as never);
    await store.refreshBoardFromServer();
    await flushPromises();

    expect(store.board?.seq, "旧读取把板面版本退回了 seq=3（F08）").toBe(4);
    expect(store.board?.cards[0].content, "旧读取把较新的候选正文盖掉了").toBe(V2);
    expect(
      store.boardContentConflictFor("c1"),
      "旧读取造出了针对旧正文的假冲突（F08）",
    ).toBeNull();
    expect(store.dirty, "较新的候选仍然是未保存状态").toBe(true);
  });

  it("乱序 GET：先发起的旧读取晚到，不许把已经落地的更新版本退回", async () => {
    vi.mocked(api.fetchBoardState).mockResolvedValue(payload(4, [card("c1", "seq4 正文")]) as never);
    const store = useInteractiveStore();
    await store.load();
    expect(store.board?.seq).toBe(4);

    // 先发起（慢）的 GET：稍后返回旧版本 seq=3
    const slow = deferred<never>();
    const slowCall = vi.mocked(api.fetchBoardState).mockReturnValueOnce(slow.promise as never);
    const slowRefresh = store.refreshBoardFromServer();

    // 后发起（快）的 GET 先返回 seq=5 —— 这是更新的服务器事实
    vi.mocked(api.fetchBoardState).mockResolvedValue(payload(5, [card("c1", "seq5 正文")]) as never);
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.board?.seq).toBe(5);

    // 慢的那个现在才返回旧版本
    slow.resolve(payload(3, [card("c1", OLD_TEXT)]) as never);
    await slowRefresh;
    await flushPromises();
    expect(store.board?.seq, "迟到的旧读取把板面版本退回去了").toBe(5);
    expect(store.board?.cards[0].content).toBe("seq5 正文");
    // 慢请求确实发出过（不是被 mock 吃掉）
    expect(slowCall).toBeDefined();
  });
});
