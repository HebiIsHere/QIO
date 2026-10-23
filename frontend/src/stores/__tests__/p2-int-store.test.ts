/**
 * Phase 2 集成（store 侧）：participation / group.collapsed / 组端点 / 版本保留的**真实接线**。
 *
 * 契约：docs/interactive-page-v2-contract.md §5.1 / §5.3 / §5.4。
 * 这里用受控的 services/interactive 替身驱动真实 store：
 * - 服务器事实里的旧 checked/hidden 会被迁移成 participation（participationVersion=1）；
 * - 选择 / 多选 / 框选不改变参与；组批量改成员；提交后不清除参与；
 * - group.collapsed 持久化且不推进语义版本；
 * - 保存回执里的权威 version 会被吸收（含「被更新候选接手的旧回执」），CAS 不退化。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import {
  emptyState,
  setGroupCollapsed,
  setGroupParticipation,
  setParticipation,
  setSelection,
  updateCard,
} from "../../interactive/board";
import type { BoardCard as BoardCardModel, BoardGroup, BoardState } from "../../interactive/types";

vi.mock("../../services/interactive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/interactive")>();
  return {
    ...actual,
    fetchBoardState: vi.fn(),
    saveBoardState: vi.fn(),
    saveDrafts: vi.fn(),
    fetchVisibleRange: vi.fn(),
    previewMaterialImpact: vi.fn(),
    checkMaterialImpact: vi.fn(),
    fetchIntents: vi.fn(),
    fetchUnderstanding: vi.fn(),
    fetchExecutions: vi.fn(),
    createIntents: vi.fn(),
    submitBoard: vi.fn(),
    fetchAcceptance: vi.fn(),
  };
});

const NOW = "2026-10-16T00:00:00.000Z";

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "注释 " + id,
    meta: {},
    x: 40,
    y: 40,
    w: 240,
    h: 96,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: NOW,
    updatedAt: NOW,
    ...patch,
  };
}

function mkGroup(id: string, members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: "发布计划",
    defaultName: false,
    ordered: false,
    x: 24,
    y: 0,
    w: 400,
    h: 200,
    members,
    deleted: false,
    createdAt: NOW,
    updatedAt: NOW,
    ...patch,
  };
}

function serverState(over: Partial<BoardState> = {}): BoardState {
  return { ...emptyState("board_t"), seq: 5, updatedAt: NOW, ...over } as BoardState;
}

function serverPayload(state: BoardState, seq = state.seq) {
  return {
    board: { id: "board_t", title: "互动板面" },
    state,
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: NOW },
    pending: [],
  };
}

const emptyRange = () => ({ cards: [], groups: [], links: [], selection: [], empty: true });

function submitResult(over: Record<string, unknown> = {}) {
  return {
    status: "succeeded",
    submission: { id: "s1", seq: 5, status: "succeeded", createdAt: NOW },
    before: emptyRange(),
    after: emptyRange(),
    expressions: [],
    baseline: { updated: true, previousSeq: 4, seq: 5 },
    delivery: { delivered: true, reason: "", detail: "", understandingId: "u1", status: "accepted" },
    visibleRange: emptyRange(),
    checkedCleared: [],
    ...over,
  };
}

async function seedStore(state: BoardState) {
  const api = await import("../../services/interactive");
  vi.mocked(api.fetchBoardState).mockResolvedValue(serverPayload(state) as never);
  vi.mocked(api.saveBoardState).mockImplementation(
    async (_boardId: string, putState: unknown) =>
      ({ ok: true, seq: 5, savedAt: NOW, state: putState }) as never,
  );
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptyRange() } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk",
    stateVersion: 5,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.createIntents).mockResolvedValue({ created: [], demo: false } as never);
  vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null } as never);
  const store = useInteractiveStore();
  await store.load();
  return store;
}

function cardOf(store: ReturnType<typeof useInteractiveStore>, id: string): BoardCardModel {
  return store.board!.cards.find((card) => card.id === id)!;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe("服务器事实 → participation 迁移（§5.1）", () => {
  it("旧 checked / hidden / file / code 按一次性规则迁移，并写入 participationVersion=1", async () => {
    const state = serverState({
      cards: [
        mkCard("checked", { checked: true }),
        mkCard("unchecked", { checked: false }),
        mkCard("hidden", { hidden: true, checked: true }),
        mkCard("file", { kind: "file", checked: false }),
        mkCard("reply", { kind: "reply", checked: false }),
        mkCard("code", { kind: "code", checked: true }),
      ],
    });
    const store = await seedStore(state);
    expect(store.board!.participationVersion).toBe(1);
    expect(cardOf(store, "checked").participation).toBe("included");
    expect(cardOf(store, "unchecked").participation).toBe("excluded");
    expect(cardOf(store, "hidden").participation).toBe("excluded");
    expect(cardOf(store, "file").participation).toBe("included");
    expect(cardOf(store, "reply").participation).toBe("excluded");
    expect(cardOf(store, "code").participation).toBe("excluded");
    // 旧镜像与参与同口径
    expect(cardOf(store, "hidden").checked).toBe(false);
    expect(cardOf(store, "unchecked").checked).toBe(false);
  });

  it("本地可见范围（participationRange）与 participation 同源", async () => {
    const store = await seedStore(
      serverState({
        cards: [
          mkCard("on", { checked: true }),
          mkCard("off", { checked: false }),
          mkCard("off2", { checked: false }),
        ],
      }),
    );
    expect(store.participationRange.cards.map((card) => card.id)).toEqual(["on"]);
    expect(store.participationRange.notVisibleCount).toBe(2);
  });
});

describe("选择 / 组批量 / 提交后的参与（§5.1）", () => {
  it("选中 / 多选不改变参与", async () => {
    const store = await seedStore(
      serverState({
        cards: [mkCard("on", { checked: true }), mkCard("off", { checked: false })],
      }),
    );
    store.commit(setSelection(store.board!, ["on", "off"]), "框选");
    expect(store.board!.selection).toEqual(["on", "off"]);
    expect(cardOf(store, "on").participation).toBe("included");
    expect(cardOf(store, "off").participation).toBe("excluded");
  });

  it("组批量改成员参与；组本身没有独立参与字段", async () => {
    const store = await seedStore(
      serverState({
        cards: [
          mkCard("a", { checked: true }),
          mkCard("b", { checked: false }),
          mkCard("c", { checked: false }),
        ],
        groups: [mkGroup("g1", ["a", "b"])],
      }),
    );
    store.commit(setGroupParticipation(store.board!, "g1", false), "整组不参与");
    expect(cardOf(store, "a").participation).toBe("excluded");
    expect(cardOf(store, "b").participation).toBe("excluded");
    expect(cardOf(store, "c").participation).toBe("excluded");
    expect((store.board!.groups[0] as BoardGroup & { participation?: unknown }).participation).toBeUndefined();

    store.commit(setGroupParticipation(store.board!, "g1", true), "整组参与");
    expect(cardOf(store, "a").participation).toBe("included");
    expect(cardOf(store, "b").participation).toBe("included");
    // 提交前预览跟着 store.board 走
    expect(store.participationRange.cards.map((card) => card.id).sort()).toEqual(["a", "b"]);
  });

  it("提交成功（服务端回执 checkedCleared）不清除参与，随后以服务端 participation 事实为准", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore(serverState({ cards: [mkCard("t1", { checked: true })] }));
    expect(cardOf(store, "t1").participation).toBe("included");

    vi.mocked(api.submitBoard).mockResolvedValue(submitResult({ checkedCleared: ["t1"] }) as never);
    await store.submit();
    await flushPromises();
    expect(store.submitStatus).toBe("succeeded");
    expect(cardOf(store, "t1").participation).toBe("included");
    expect(cardOf(store, "t1").checked).toBe(true); // 旧镜像与参与同口径

    // 服务端事实（D 的 participation 字段）说这张卡仍在范围内 → 参与保持
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      serverPayload(serverState({ cards: [mkCard("t1", { participation: "included", checked: true })] })) as never,
    );
    await store.refreshBoardFromServer();
    expect(cardOf(store, "t1").participation).toBe("included");
  });
});

describe("group.collapsed 持久化（§5.4）", () => {
  it("服务器上的收起状态原样保留；切换只改 collapsed，不动语义 version", async () => {
    const store = await seedStore(
      serverState({
        cards: [mkCard("a", { checked: true })],
        groups: [mkGroup("g1", ["a"], { collapsed: true, version: 9 })],
      }),
    );
    expect(store.board!.groups[0].collapsed).toBe(true);
    expect(store.board!.groups[0].version).toBe(9);

    store.commit(setGroupCollapsed(store.board!, "g1", false), "展开组");
    expect(store.board!.groups[0].collapsed).toBeFalsy();
    expect(store.board!.groups[0].version).toBe(9);
    expect(cardOf(store, "a").participation).toBe("included");
  });

  it("组端点关系随保存往返保留；解散组后组端点线消失、卡线保留", async () => {
    const store = await seedStore(
      serverState({
        cards: [mkCard("a", { checked: true }), mkCard("c", { checked: true })],
        groups: [mkGroup("g1", ["a"])],
        links: [
          {
            id: "group-line",
            src: "group:g1",
            dst: "c",
            direction: false,
            meaning: "组与卡片",
            deleted: false,
            createdAt: NOW,
            updatedAt: NOW,
          },
        ],
      }),
    );
    expect(store.board!.links.map((link) => [link.src, link.dst])).toEqual([["group:g1", "c"]]);

    const { dissolveGroup } = await import("../../interactive/board");
    store.commit(dissolveGroup(store.board!, "g1"), "解散组");
    expect(store.board!.links).toEqual([]);
    expect(cardOf(store, "a").participation).toBe("included");
  });
});

describe("version 保留与 CAS（§5.4）", () => {
  it("读取保留服务端 version；纯布局改动不自增；保存回执吸收更高版本", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore(
      serverState({ cards: [mkCard("a", { checked: true, version: 5 })], groups: [] }),
    );
    expect(cardOf(store, "a").version).toBe(5);

    store.commit(updateCard(store.board!, "a", { x: 300, y: 300 }), "移动卡片");
    expect(cardOf(store, "a").version).toBe(5);

    // 服务端接受的版本是 6：回执里的权威 version 必须被吸收
    vi.mocked(api.saveBoardState).mockImplementation(
      async (_boardId: string, putState: unknown) => {
        const state = JSON.parse(JSON.stringify(putState)) as BoardState;
        state.seq = 6;
        state.cards = state.cards.map((card) => ({ ...card, version: 6 }));
        return { ok: true, seq: 6, savedAt: NOW, state } as never;
      },
    );
    await store.saveNow();
    await flushPromises();
    expect(cardOf(store, "a").version).toBe(6);
    expect(store.board!.seq).toBe(6);
  });

  it("候选被更新编辑接手时，旧回执仍吸收权威 version（不覆盖正文）", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore(
      serverState({ cards: [mkCard("a", { checked: true, version: 5 })] }),
    );
    let release!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    vi.mocked(api.saveBoardState).mockImplementation(async () => {
      await pending;
      return {
        ok: true,
        seq: 6,
        savedAt: NOW,
        state: serverState({
          seq: 6,
          cards: [mkCard("a", { checked: true, version: 6, content: "服务器正文" })],
        }),
      } as never;
    });

    store.commit(updateCard(store.board!, "a", { x: 300, y: 300 }), "移动卡片");
    const saving = store.saveNow();
    await flushPromises();
    // 回执在飞期间用户继续编辑（候选被更新）
    store.commit(updateCard(store.board!, "a", { x: 320, y: 320 }), "继续移动");
    release(null);
    await saving;
    await flushPromises();

    // 正文与位置是用户的候选，不被旧回执覆盖；但 version 必须吸收服务器的权威值
    expect(cardOf(store, "a").x).toBe(320);
    expect(cardOf(store, "a").content).toBe("注释 a");
    expect(cardOf(store, "a").version).toBe(6);
    expect(store.board!.seq).toBe(6);
  });

  it("合并服务器事实时也吸收更高 version（候选仍在编辑）", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore(
      serverState({ seq: 5, cards: [mkCard("a", { checked: true, version: 5 })] }),
    );
    store.commit(updateCard(store.board!, "a", { x: 300, y: 300 }), "移动卡片");
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      serverPayload(
        serverState({
          seq: 9,
          cards: [mkCard("a", { checked: true, version: 9, content: "服务器正文" })],
        }),
        9,
      ) as never,
    );
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.board!.seq).toBe(9);
    expect(cardOf(store, "a").version).toBe(9);
    // 用户自己的布局改动保留
    expect(cardOf(store, "a").x).toBe(300);
  });
});
