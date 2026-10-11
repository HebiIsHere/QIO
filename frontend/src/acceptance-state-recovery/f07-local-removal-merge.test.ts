/**
 * 【审计 8fa8452 · F07】本地删除的组与关联被服务器合并复活。
 *
 * 反例（基线 serverFacts.mergeGroup / mergeLink）：`normalizeState` 会把解散的组、
 * 删除的关联**从数组里去掉**，于是「本地没有」与「服务器新增的」形状相同；
 * 基线对 `!local` 一律返回服务器那份 —— 用户明明删掉的对象又被服务器事实带回来了。
 *
 * 正确行为（按 base 三路判定）：
 * - base 有、本地没有、服务器与 base 相同 → 本地删除成立，保持删除；
 * - base 有、本地没有、服务器与 base 不同 → 相撞：保留依据 + 等用户明确选择（不静默复活，也不丢对方修改）；
 * - base 没有、服务器有 → 服务器新增，保留。
 *
 * 既有单元层（纯函数），也有 store 层（adoptServerFacts / saveNow 的门 / 解决入口）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import { mergeServerInto } from "../interactive/serverFacts";
import type { BoardGroup, BoardLink, BoardState } from "../interactive/types";
import { card, payload, state } from "./support/harness";

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

function group(id: string, members: string[], extra: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: "组-" + id,
    defaultName: false,
    ordered: false,
    x: 0,
    y: 0,
    w: 10,
    h: 10,
    members,
    deleted: false,
    createdAt: "2026-10-10T00:00:00Z",
    updatedAt: "2026-10-10T00:00:00Z",
    ...extra,
  };
}

function link(id: string, src: string, dst: string, extra: Partial<BoardLink> = {}): BoardLink {
  return {
    id,
    src,
    dst,
    direction: false,
    meaning: "关联",
    deleted: false,
    createdAt: "2026-10-10T00:00:00Z",
    updatedAt: "2026-10-10T00:00:00Z",
    ...extra,
  };
}

function board(
  seq: number,
  cards: BoardState["cards"],
  groups: BoardGroup[],
  links: BoardLink[],
  extra: Partial<BoardState> = {},
): BoardState {
  return { boardId: "board_default", seq, updatedAt: "2026-10-10T00:00:00Z", cards, groups, links, selection: [], ...extra };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t", rev: 1 } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("F07 · 纯函数三路合并", () => {
  it("服务器只改无关卡片：本地删除的组与关联保持删除", () => {
    const cards = [card("c1", "正文"), card("c2", "第二张")];
    const g = group("g1", ["c1", "c2"]);
    const l = link("l1", "c1", "c2");
    const base = board(3, cards, [g], [l]);
    // 用户解散了组、删掉了关联（normalizeState 之后它们从数组里消失）
    const local = board(3, cards, [], []);
    // 服务器只把 c2 移动了（无关对象），组与关联原样
    const server = board(4, [cards[0], { ...cards[1], x: 500 }], [g], [l]);

    const outcome = mergeServerInto(local, server, base);
    expect(outcome.state.groups, "本地解散的组被服务器事实复活了").toEqual([]);
    expect(outcome.state.links, "本地删除的关联被服务器事实复活了").toEqual([]);
    expect(outcome.state.cards.find((item) => item.id === "c2")?.x, "服务器对无关卡片的修改必须协调进来").toBe(500);
    expect(outcome.removalConflicts, "服务器只是改了无关卡片，不该产生删除冲突").toEqual([]);
  });

  it("服务器新增的组与关联必须保留（不能当成「本地删除」误删）", () => {
    const cards = [card("c1", "正文"), card("c2", "第二张")];
    const base = board(3, cards, [], []);
    const local = board(3, cards, [], []);
    const server = board(4, cards, [group("gNew", ["c1", "c2"])], [link("lNew", "c1", "c2")]);

    const outcome = mergeServerInto(local, server, base);
    expect(outcome.state.groups.map((item) => item.id)).toEqual(["gNew"]);
    expect(outcome.state.links.map((item) => item.id)).toEqual(["lNew"]);
    expect(outcome.removalConflicts).toEqual([]);
  });

  it("删除与服务器对同一关联的修改相撞：保留依据，不静默复活也不丢对方修改", () => {
    const cards = [card("c1", "正文"), card("c2", "第二张")];
    const base = board(3, cards, [], [link("l1", "c1", "c2", { meaning: "旧含义" })]);
    const local = board(3, cards, [], []);
    const server = board(4, cards, [], [link("l1", "c1", "c2", { meaning: "对方改过的含义" })]);

    const outcome = mergeServerInto(local, server, base);
    expect(outcome.state.links, "相撞时不许静默复活").toEqual([]);
    expect(outcome.removalConflicts).toEqual([
      {
        kind: "link",
        id: "l1",
        local: "removed",
        server: expect.objectContaining({ id: "l1", meaning: "对方改过的含义" }),
      },
    ]);
  });
});

describe("F07 · store 层：删除不许被复活，相撞要用户明确选择", () => {
  const cards = [card("c1", "正文"), card("c2", "第二张")];

  function mockServerSeq(seq: number, groups: BoardGroup[], links: BoardLink[], movedCard = false) {
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      payload(seq, [cards[0], movedCard ? { ...cards[1], x: 999 } : cards[1]], {}, {
        state: board(seq, [cards[0], movedCard ? { ...cards[1], x: 999 } : cards[1]], groups, links),
      }) as never,
    );
  }

  it("服务器只改无关卡片：本地解散的组与删除的关联保持；成员清理与选择不受影响", async () => {
    const g = group("g1", ["c1", "c2"]);
    const l = link("l1", "c1", "c2");
    mockServerSeq(3, [g], [l]);
    const store = useInteractiveStore();
    await store.load();
    expect(store.board?.groups.map((item) => item.id)).toEqual(["g1"]);

    // 用户解散组 + 删除关联（真实路径由 board.ts 产生：对象从数组里消失）
    store.commit(board(3, cards, [], [], { selection: ["c1"] }), "解散组并删除关联");
    expect(store.dirty).toBe(true);

    // 另一页面只移动了无关卡片并保存到 seq=4
    mockServerSeq(4, [g], [l], true);
    await store.refreshBoardFromServer();
    await flushPromises();

    expect(store.board?.groups, "本地解散的组被服务器事实复活了（F07）").toEqual([]);
    expect(store.board?.links, "本地删除的关联被服务器事实复活了（F07）").toEqual([]);
    expect(store.board?.cards.find((item) => item.id === "c2")?.x, "服务器对无关卡片的修改丢了").toBe(999);
    expect(store.board?.selection, "选择状态被合并破坏").toEqual(["c1"]);
    expect(store.boardRemovalConflictFor("g1"), "无关卡片改动不该产生删除冲突").toBeNull();
  });

  it("删除与服务器对同一对象的修改相撞：登记依据、保存被挡住，两种选择都可用", async () => {
    const l = link("l1", "c1", "c2", { meaning: "旧含义" });
    mockServerSeq(3, [], [l]);
    const store = useInteractiveStore();
    await store.load();

    store.commit(board(3, cards, [], []), "删除关联");
    // 对方修改了同一条关联
    mockServerSeq(4, [], [link("l1", "c1", "c2", { meaning: "对方改过的含义" })]);
    await store.refreshBoardFromServer();
    await flushPromises();

    const conflict = store.boardRemovalConflictFor("l1");
    expect(conflict, "相撞必须保留依据").not.toBeNull();
    expect(conflict?.server.meaning, "依据里必须带着对方的那份修改").toBe("对方改过的含义");
    expect(store.board?.links, "相撞时不许静默复活").toEqual([]);

    // 未选择之前：保存被挡住，且说明里点出对象与解决办法
    await store.saveNow();
    await flushPromises();
    expect(store.saveStatus).toBe("error");
    expect(store.saveError ?? "").toContain("l1");
    expect(api.saveBoardState, "未选择之前不许把删除写上去（会丢对方的修改）").not.toHaveBeenCalled();

    // 选择「恢复服务器版」：按服务器事实放回候选，并排一次保存
    const puts: BoardState[] = [];
    vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
      const put = putState as BoardState;
      puts.push(put);
      return { ok: true, seq: 5, savedAt: "t", state: { ...put, seq: 5 } } as never;
    });
    store.resolveBoardRemovalConflict("l1", "restore-server");
    expect(store.boardRemovalConflictFor("l1")).toBeNull();
    expect(store.board?.links.map((item) => item.id)).toEqual(["l1"]);
    await store.saveNow();
    await flushPromises();
    expect(puts.length).toBeGreaterThan(0);
    expect(puts[puts.length - 1].links.map((item) => item.id), "恢复服务器版之后保存必须带上它").toEqual(["l1"]);
  });

  it("选择「保持删除」：删除生效、冲突解除、对方那份按用户明确决定放弃", async () => {
    const l = link("l1", "c1", "c2", { meaning: "旧含义" });
    mockServerSeq(3, [], [l]);
    const store = useInteractiveStore();
    await store.load();
    store.commit(board(3, cards, [], []), "删除关联");
    mockServerSeq(4, [], [link("l1", "c1", "c2", { meaning: "对方改过的含义" })]);
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.boardRemovalConflictFor("l1")).not.toBeNull();

    const puts: BoardState[] = [];
    vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
      const put = putState as BoardState;
      puts.push(put);
      return { ok: true, seq: 5, savedAt: "t", state: { ...put, seq: 5 } } as never;
    });
    store.resolveBoardRemovalConflict("l1", "keep-deleted");
    expect(store.boardRemovalConflictFor("l1")).toBeNull();
    expect(store.board?.links).toEqual([]);
    await store.saveNow();
    await flushPromises();
    expect(puts.length).toBeGreaterThan(0);
    expect(puts[puts.length - 1].links, "保持删除之后保存必须仍是不含关联").toEqual([]);
    expect(store.saveStatus).toBe("saved");
  });
});
