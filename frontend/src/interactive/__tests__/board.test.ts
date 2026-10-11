/**
 * 板面纯函数用例（与 backend/tests/test_interactive_board.py 同一批规则）。
 *
 * 契约：docs/interactive-mode-contract.md §1.2（G1..G8）/ §1.3 / §4.2。
 * 这里只测纯函数：不挂载组件、不碰网络。
 */
import { describe, expect, it } from "vitest";
import * as board from "../board";
import {
  emptyBoardState,
  type BoardCard,
  type BoardGroup,
  type BoardLink,
  type BoardState,
  type CardKind,
} from "../types";

function mkCard(id: string, x = 0, y = 0, patch: Partial<BoardCard> = {}): BoardCard {
  const stamp = "2026-10-06T00:00:00.000Z";
  return {
    id,
    kind: (patch.kind ?? "text") as CardKind,
    content: patch.content ?? "",
    meta: patch.meta ?? {},
    x,
    y,
    w: patch.w ?? 100,
    h: patch.h ?? 60,
    checked: patch.checked ?? false,
    hidden: patch.hidden ?? false,
    folded: patch.folded ?? false,
    bookmarked: patch.bookmarked ?? false,
    deleted: patch.deleted ?? false,
    createdAt: stamp,
    updatedAt: stamp,
  };
}

function mkGroup(id: string, members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  const stamp = "2026-10-06T00:00:00.000Z";
  return {
    id,
    name: patch.name ?? "默认组名",
    defaultName: patch.defaultName ?? true,
    ordered: patch.ordered ?? false,
    x: patch.x ?? 0,
    y: patch.y ?? 0,
    w: patch.w ?? 1,
    h: patch.h ?? 1,
    members: [...members],
    deleted: patch.deleted ?? false,
    createdAt: stamp,
    updatedAt: stamp,
  };
}

function mkLink(src: string, dst: string, patch: Partial<BoardLink> = {}): BoardLink {
  const stamp = "2026-10-06T00:00:00.000Z";
  return {
    id: patch.id ?? "l_" + src + dst,
    src,
    dst,
    direction: patch.direction ?? false,
    meaning: patch.meaning ?? "",
    deleted: patch.deleted ?? false,
    createdAt: stamp,
    updatedAt: stamp,
  };
}

function mkState(cards: BoardCard[] = [], groups: BoardGroup[] = [], links: BoardLink[] = [], selection: string[] = []): BoardState {
  return { ...emptyBoardState("board_t"), cards, groups, links, selection };
}

function membersOf(state: BoardState, groupId: string): string[] {
  return state.groups.find((group) => group.id === groupId)?.members ?? [];
}

function groupIds(state: BoardState): string[] {
  return state.groups.map((group) => group.id);
}

function twoFreeCards(): BoardState {
  return mkState([mkCard("a", 0, 0), mkCard("b", 240, 0)]);
}

describe("normalizeState：契约 §1.2 的 G1..G8", () => {
  it("G1 一张卡最多属于一个组，成员不重复", () => {
    const state = mkState(
      [mkCard("a"), mkCard("b", 200, 0)],
      [mkGroup("g1", ["a", "a", "b"]), mkGroup("g2", ["b", "a"])],
    );
    const result = board.normalizeState(state);
    expect(membersOf(result, "g1")).toEqual(["a", "b"]);
    expect(groupIds(result)).toEqual(["g1"]);
  });

  it("G1 第二个组保留没被第一个组占用的成员", () => {
    const result = board.normalizeState(mkState([mkCard("a"), mkCard("b", 200, 0)], [mkGroup("g1", ["a"]), mkGroup("g2", ["a", "b"])]));
    expect(membersOf(result, "g1")).toEqual(["a"]);
    expect(membersOf(result, "g2")).toEqual(["b"]);
  });

  it("G2 有序组 members 顺序即序号且连续", () => {
    const result = board.normalizeState(
      mkState([mkCard("a"), mkCard("b"), mkCard("c")], [mkGroup("g1", ["c", "a", "b"], { ordered: true })]),
    );
    expect(membersOf(result, "g1")).toEqual(["c", "a", "b"]);
    expect(result.groups[0].ordered).toBe(true);
    const afterDelete = board.normalizeState(
      mkState([mkCard("a"), mkCard("b", 0, 0, { deleted: true }), mkCard("c")], [mkGroup("g1", ["a", "b", "c"], { ordered: true })]),
    );
    expect(membersOf(afterDelete, "g1")).toEqual(["a", "c"]);
  });

  it("G3 成员全被移除或删除的组自动消失", () => {
    const result = board.normalizeState(
      mkState([mkCard("a", 0, 0, { deleted: true }), mkCard("b")], [mkGroup("g1", ["a"]), mkGroup("g2", ["b"])]),
    );
    expect(groupIds(result)).toEqual(["g2"]);
  });

  it("G4 链接端点必须存在且未删除，同一对端点只保留一条", () => {
    const result = board.normalizeState(
      mkState(
        [mkCard("a"), mkCard("b"), mkCard("c", 0, 0, { deleted: true })],
        [],
        [
          mkLink("a", "b", { id: "l1", meaning: "第一条" }),
          mkLink("a", "b", { id: "l2", meaning: "重复" }),
          mkLink("b", "a", { id: "l3", meaning: "反向重复" }),
          mkLink("a", "c", { id: "l4" }),
          mkLink("a", "ghost", { id: "l5" }),
          mkLink("a", "b", { id: "l6", deleted: true }),
        ],
      ),
    );
    expect(result.links).toHaveLength(1);
    expect(result.links[0].meaning).toBe("第一条");
  });

  it("G5 删除的卡片不进组、不进链接、不参与选择", () => {
    const result = board.normalizeState(
      mkState([mkCard("a"), mkCard("b", 0, 0, { deleted: true })], [mkGroup("g1", ["a", "b"])], [mkLink("a", "b")], ["a", "b"]),
    );
    expect(membersOf(result, "g1")).toEqual(["a"]);
    expect(result.links).toEqual([]);
    expect(result.selection).toEqual(["a"]);
  });

  it("G6 hidden 与 checked 互斥；G7 reply 不参与勾选", () => {
    const result = board.normalizeState(
      mkState([mkCard("a", 0, 0, { checked: true, hidden: true }), mkCard("b", 0, 0, { checked: true }), mkCard("r", 0, 0, { kind: "reply", checked: true })]),
    );
    expect(result.cards[0].checked).toBe(false);
    expect(result.cards[0].hidden).toBe(true);
    expect(result.cards[1].checked).toBe(true);
    expect(result.cards[2].checked).toBe(false);
  });

  it("G8 selection 只含存在且未删除的卡片，并去重", () => {
    const result = board.normalizeState(mkState([mkCard("a"), mkCard("b", 0, 0, { deleted: true })], [], [], ["a", "a", "b", "ghost"]));
    expect(result.selection).toEqual(["a"]);
  });

  it("返回新状态且不原地修改输入", () => {
    const state = mkState([mkCard("a", 0, 0, { checked: true, hidden: true })], [mkGroup("g1", ["a", "ghost"])], [], ["ghost"]);
    const snapshot = JSON.stringify(state);
    const result = board.normalizeState(state);
    expect(JSON.stringify(state)).toBe(snapshot);
    expect(result).not.toBe(state);
    expect(result.cards[0]).not.toBe(state.cards[0]);
  });

  it("组框跟着成员走", () => {
    const result = board.normalizeState(
      mkState([mkCard("a", 100, 50), mkCard("b", 300, 200)], [mkGroup("g1", ["a", "b"], { x: 0, y: 0, w: 1, h: 1 })]),
    );
    const frame = result.groups[0];
    expect(frame.x).toBeLessThanOrEqual(100);
    expect(frame.y).toBeLessThanOrEqual(50);
    expect(frame.x + frame.w).toBeGreaterThanOrEqual(400);
    expect(frame.y + frame.h).toBeGreaterThanOrEqual(260);
  });
});

describe("previewDrop：拖动期间只预演", () => {
  it("与未分组卡片重叠时报出那张卡片", () => {
    const preview = board.previewDrop(twoFreeCards(), "a", 250, 10);
    expect(preview.groupId).toBeNull();
    expect(preview.mergesWith).toBe("b");
    expect(preview.index).toBeNull();
  });

  it("拖入已有组时报出组与插入位置", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0)],
      [mkGroup("g1", ["a", "b"], { name: "材料整理", defaultName: false, ordered: true })],
    );
    const preview = board.previewDrop(state, "c", 300, 30);
    expect(preview.groupId).toBe("g1");
    expect(preview.index).toBe(2);
    expect(preview.mergesWith).toBeNull();
  });

  it("组中卡片拖到另一组时报出将合并的组", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0)],
      [mkGroup("g1", ["a", "b"], { name: "第一组", defaultName: false }), mkGroup("g2", ["c"], { name: "第二组", defaultName: false })],
    );
    const preview = board.previewDrop(state, "c", 100, 30);
    expect(preview.groupId).toBe("g1");
    expect(preview.mergesWith).toBe("g2");
    expect(preview.index).toBe(1);
  });

  it("空白处落下没有预演内容，且预演不改状态", () => {
    const state = twoFreeCards();
    const snapshot = JSON.stringify(state);
    expect(board.previewDrop(state, "a", 2000, 2000)).toEqual({ groupId: null, index: null, mergesWith: null });
    expect(JSON.stringify(state)).toBe(snapshot);
  });

  it("已删除或不存在的卡片没有预演内容", () => {
    const state = mkState([mkCard("a", 0, 0, { deleted: true })]);
    expect(board.previewDrop(state, "a", 0, 0)).toEqual({ groupId: null, index: null, mergesWith: null });
    expect(board.previewDrop(state, "ghost", 0, 0)).toEqual({ groupId: null, index: null, mergesWith: null });
  });
});

describe("dropCard：放下才完成操作", () => {
  it("两张未分组卡片重叠自动成组，默认名「默认组名」（反例：旧实现给「组 1」）", () => {
    const result = board.dropCard(twoFreeCards(), "a", 250, 10);
    expect(result.merged).toBe(false);
    expect(result.groupId).not.toBeNull();
    const group = board.groupById(result.state, result.groupId as string);
    expect(group?.name).toBe("默认组名");
    expect(group?.defaultName).toBe(true);
    expect(group?.ordered).toBe(false);
    expect([...(group?.members ?? [])].sort()).toEqual(["a", "b"]);
    expect(board.cardById(result.state, "a")?.x).toBe(250);
  });

  it("默认名不再编号：已有「组 7」也照样用「默认组名」（反例：旧实现给「组 8」）", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 240, 0), mkCard("c", 2000, 0), mkCard("d", 2040, 0)],
      [mkGroup("g7", ["c", "d"], { name: "组 7" })],
    );
    const result = board.dropCard(state, "a", 250, 10);
    const created = board.groupById(result.state, result.groupId as string);
    expect(created?.name).toBe("默认组名");
    expect(created?.defaultName).toBe(true);
    // 两个组同名并存：组身份按 id，不因重名被合并或丢弃（契约 §9.1）
    expect(result.state.groups.map((group) => group.id).sort()).toEqual(["g7", created?.id].sort());
    expect(result.state.groups.filter((group) => group.name === "默认组名")).toHaveLength(1);
  });

  it("未分组卡片拖入已有组：保留原组名并按落点插入", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0)],
      [mkGroup("g1", ["a", "b"], { name: "发布计划", defaultName: false, ordered: true })],
    );
    const result = board.dropCard(state, "c", 300, 30);
    expect(result.groupId).toBe("g1");
    expect(result.merged).toBe(false);
    expect(result.index).toBe(2);
    const group = board.groupById(result.state, "g1");
    expect(group?.name).toBe("发布计划");
    expect(group?.members).toEqual(["a", "b", "c"]);
  });

  it("插到最前时序号从 1 重新连续", () => {
    const state = mkState(
      [mkCard("a", 100, 0), mkCard("b", 300, 0), mkCard("c", 900, 0)],
      [mkGroup("g1", ["a", "b"], { ordered: true })],
    );
    const result = board.dropCard(state, "c", 100, 30);
    expect(membersOf(result.state, "g1")).toEqual(["c", "a", "b"]);
    expect(result.index).toBe(0);
  });

  it("两个已有组重叠合并：原组不保留，新组用默认名，被拖入组成员连续插入", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0), mkCard("d", 800, 0)],
      [mkGroup("g1", ["a", "b"], { name: "目标组", defaultName: false }), mkGroup("g2", ["c", "d"], { name: "被拖动组", defaultName: false })],
    );
    const result = board.dropCard(state, "c", 100, 30);
    expect(result.merged).toBe(true);
    expect(groupIds(result.state)).toEqual(["g1"]);
    const group = board.groupById(result.state, "g1");
    expect(group?.name).toBe("默认组名");
    expect(group?.defaultName).toBe(true);
    expect(group?.members).toEqual(["a", "c", "d", "b"]);
  });

  it("两个有序组合并保留各自内部顺序并统一编号", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0), mkCard("d", 800, 0), mkCard("e", 1000, 0)],
      [mkGroup("g1", ["a", "b"], { ordered: true, name: "第一批", defaultName: false }), mkGroup("g2", ["c", "d", "e"], { ordered: true, name: "第二批", defaultName: false })],
    );
    const result = board.dropCard(state, "d", 100, 30);
    const group = board.groupById(result.state, "g1");
    expect(group?.ordered).toBe(true);
    expect(group?.members).toEqual(["a", "c", "d", "e", "b"]);
    expect(group?.name).toBe("默认组名");
  });

  it("有序组与普通组合并后成为普通组，可重新设为有序", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0), mkCard("d", 800, 0)],
      [mkGroup("g1", ["a", "b"], { ordered: true }), mkGroup("g2", ["c", "d"], { ordered: false, name: "普通组", defaultName: false })],
    );
    const result = board.dropCard(state, "c", 100, 30);
    const group = board.groupById(result.state, "g1");
    expect(group?.ordered).toBe(false);
    expect(group?.members).toEqual(["a", "c", "d", "b"]);
    expect(board.groupById(board.setGroupOrdered(result.state, "g1", true), "g1")?.ordered).toBe(true);
  });

  it("把卡片拖出组即移出；最后一名成员离开后组消失", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 200, 0)], [mkGroup("g1", ["a", "b"])]);
    const result = board.dropCard(state, "a", 2000, 2000);
    expect(result.groupId).toBeNull();
    expect(membersOf(result.state, "g1")).toEqual(["b"]);

    const single = mkState([mkCard("a", 0, 0)], [mkGroup("g1", ["a"])]);
    const moved = board.dropCard(single, "a", 2000, 2000);
    expect(groupIds(moved.state)).toEqual([]);
  });

  it("在组内拖动只调整顺序", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 400, 0)],
      [mkGroup("g1", ["a", "b", "c"], { ordered: true })],
    );
    const result = board.dropCard(state, "c", 150, 30);
    expect(result.groupId).toBe("g1");
    expect(result.index).toBe(1);
    expect(membersOf(result.state, "g1")).toEqual(["a", "c", "b"]);
  });

  it("拖到已分组卡片上加入它的组，而不是新建组", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("z", 2000, 0)],
      [mkGroup("g1", ["a", "b"], { name: "已有组", defaultName: false })],
    );
    const result = board.dropCard(state, "z", 100, 30);
    expect(result.groupId).toBe("g1");
    expect(board.groupById(result.state, "g1")?.name).toBe("已有组");
    expect([...(board.groupById(result.state, "g1")?.members ?? [])].sort()).toEqual(["a", "b", "z"]);
  });

  it("单独落下只移动位置", () => {
    const result = board.dropCard(twoFreeCards(), "a", 2000, 2000);
    expect(result.groupId).toBeNull();
    expect(result.merged).toBe(false);
    expect(result.index).toBeNull();
    expect(board.cardById(result.state, "a")?.x).toBe(2000);
    expect(groupIds(result.state)).toEqual([]);
  });

  it("一叠卡片里只认一个明确目标：不是整堆一起成组", () => {
    // a 在 0..100、b 在 100..200，c 落在 100 → 与两张的重叠比例都是 0.6（完全并列）。
    // 并列时按卡片顺序取第一个（确定性目标），绝不把一叠卡片整堆合并。
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 100, 0), mkCard("c", 300, 0)]);
    const result = board.dropCard(state, "c", 50, 0);
    const group = board.groupById(result.state, result.groupId as string);
    expect([...(group?.members ?? [])].sort()).toEqual(["a", "c"]);
  });

  it("一叠卡片里落点明确压在中间那张上时只与中间那张成组", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 120, 0), mkCard("c", 240, 0)]);
    // b 的矩形是 120..220；c 落在 130 → 与 b 的重叠是 90/100 = 0.9，与 a 的重叠是 0
    const result = board.dropCard(state, "c", 130, 0);
    const group = board.groupById(result.state, result.groupId as string);
    expect([...(group?.members ?? [])].sort()).toEqual(["b", "c"]);
  });

  it("已删除或不存在的卡片放下不改状态", () => {
    const state = mkState([mkCard("a", 0, 0, { deleted: true }), mkCard("b", 2000, 0)]);
    const before = board.normalizeState(state);
    expect(board.dropCard(state, "a", 0, 0).state).toEqual(before);
    expect(board.dropCard(state, "ghost", 0, 0).state).toEqual(before);
  });
});

describe("分组与顺序操作", () => {
  it("joinGroup 按 index 插入并离开原来的组", () => {
    const state = mkState(
      [mkCard("a"), mkCard("b"), mkCard("c")],
      [mkGroup("g1", ["a", "b"]), mkGroup("g2", ["c"], { name: "第二组", defaultName: false })],
    );
    const joined = board.joinGroup(state, "c", "g1", 1);
    expect(membersOf(joined, "g1")).toEqual(["a", "c", "b"]);
    expect(groupIds(joined)).toEqual(["g1"]);
    expect(membersOf(board.joinGroup(state, "c", "g1"), "g1")).toEqual(["a", "b", "c"]);
  });

  it("joinGroup 的 index 会被收敛到两端", () => {
    const state = mkState([mkCard("a"), mkCard("b"), mkCard("c")], [mkGroup("g1", ["a", "b"])]);
    expect(membersOf(board.joinGroup(state, "c", "g1", 99), "g1")).toEqual(["a", "b", "c"]);
    expect(membersOf(board.joinGroup(state, "c", "g1", -5), "g1")).toEqual(["c", "a", "b"]);
  });

  it("createGroup 把卡片移出新组，旧组空了会消失", () => {
    const state = mkState([mkCard("a"), mkCard("b"), mkCard("c")], [mkGroup("g1", ["a", "b"])]);
    const created = board.createGroup(state, ["a", "b", "c"]);
    const newId = groupIds(created).find((id) => id !== "g1") as string;
    expect(membersOf(created, newId)).toEqual(["a", "b", "c"]);
    expect(groupIds(created)).toEqual([newId]);
    expect(board.groupById(created, newId)?.name).toBe("默认组名");
    expect(board.groupById(created, newId)?.defaultName).toBe(true);
    expect(board.createGroup(mkState([mkCard("a", 0, 0, { deleted: true })]), ["a", "ghost"]).groups).toEqual([]);
  });

  it("removeFromGroup 与 dissolveGroup", () => {
    const state = mkState(
      [mkCard("a"), mkCard("b"), mkCard("c")],
      [mkGroup("g1", ["a", "b"]), mkGroup("g2", ["c"], { name: "第二组", defaultName: false })],
    );
    expect(membersOf(board.removeFromGroup(state, "a"), "g1")).toEqual(["b"]);
    const dissolved = board.dissolveGroup(state, "g1");
    expect(groupIds(dissolved)).toEqual(["g2"]);
    expect(dissolved.cards[0].id).toBe("a");
  });

  it("renameGroup：输入即用；留空 / 取消不改动，组仍成立并保留默认名", () => {
    const state = mkState([mkCard("a")], [mkGroup("g1", ["a"])]);
    const renamed = board.renameGroup(state, "g1", "  发布计划  ");
    expect(renamed.groups[0].name).toBe("发布计划");
    expect(renamed.groups[0].defaultName).toBe(false);
    // 改回系统默认名 / 历史「组 N」形态都算默认名（isDefaultGroupName 同时认两种）
    expect(board.renameGroup(renamed, "g1", "默认组名").groups[0].defaultName).toBe(true);
    expect(board.renameGroup(renamed, "g1", "组 3").groups[0].defaultName).toBe(true);
    // 留空 = 不改动：默认名的组保留「默认组名」，组仍然成立
    const blank = board.renameGroup(state, "g1", "   ");
    expect(blank.groups[0].name).toBe("默认组名");
    expect(blank.groups[0].defaultName).toBe(true);
    expect(blank.groups[0].members).toEqual(["a"]);
    // 已经改过名的组，留空同样不改动（不把用户写的名字抹掉）
    expect(board.renameGroup(renamed, "g1", "").groups[0].name).toBe("发布计划");
  });

  it("moveWithinGroup 越界收敛，组外卡片不动", () => {
    const state = mkState([mkCard("a"), mkCard("b"), mkCard("c")], [mkGroup("g1", ["a", "b", "c"])]);
    expect(membersOf(board.moveWithinGroup(state, "g1", "c", 0), "g1")).toEqual(["c", "a", "b"]);
    expect(membersOf(board.moveWithinGroup(state, "g1", "a", 99), "g1")).toEqual(["b", "c", "a"]);
    expect(membersOf(board.moveWithinGroup(state, "g1", "zzz", 0), "g1")).toEqual(["a", "b", "c"]);
  });

  it("mergeGroups 保留目标 id、用默认名、连续插入", () => {
    const state = mkState(
      [mkCard("a"), mkCard("b"), mkCard("c"), mkCard("d")],
      [mkGroup("g1", ["a", "b"], { name: "目标", defaultName: false }), mkGroup("g2", ["c", "d"], { name: "来源", defaultName: false })],
    );
    const merged = board.mergeGroups(state, "g2", "g1", 1);
    expect(groupIds(merged)).toEqual(["g1"]);
    const group = board.groupById(merged, "g1");
    expect(group?.members).toEqual(["a", "c", "d", "b"]);
    expect(group?.name).toBe("默认组名");
    expect(group?.defaultName).toBe(true);
    expect(membersOf(board.mergeGroups(state, "g2", "g1"), "g1")).toEqual(["a", "b", "c", "d"]);
  });
});

describe("关系链接", () => {
  it("记录用户写明的方向与含义", () => {
    const linked = board.addLink(mkState([mkCard("a"), mkCard("b")]), "a", "b", false, "同一批材料");
    expect(linked.links).toHaveLength(1);
    expect(linked.links[0].src).toBe("a");
    expect(linked.links[0].dst).toBe("b");
    expect(linked.links[0].direction).toBe(false);
    expect(linked.links[0].meaning).toBe("同一批材料");
    // 系统不补写任何解释性文字
    expect(JSON.stringify(linked.links[0])).not.toContain("因果");
  });

  it("端点不合法或自环不建立关系", () => {
    const state = mkState([mkCard("a"), mkCard("b", 0, 0, { deleted: true })]);
    expect(board.addLink(state, "a", "b", false, "x").links).toEqual([]);
    expect(board.addLink(state, "a", "ghost", false, "x").links).toEqual([]);
    expect(board.addLink(state, "a", "a", false, "自环").links).toEqual([]);
  });

  it("同一对端点重复建立只更新一条", () => {
    const once = board.addLink(mkState([mkCard("a"), mkCard("b")]), "a", "b", false, "同一批材料");
    const twice = board.addLink(once, "b", "a", true, "先看 b 再看 a");
    expect(twice.links).toHaveLength(1);
    expect(twice.links[0].direction).toBe(true);
    expect(twice.links[0].meaning).toBe("先看 b 再看 a");
  });

  it("updateLink 与 removeLink", () => {
    const linked = board.addLink(mkState([mkCard("a"), mkCard("b")]), "a", "b", false, "关联");
    const linkId = linked.links[0].id;
    const updated = board.updateLink(linked, linkId, { meaning: "用户改写的含义", direction: true });
    expect(updated.links[0].meaning).toBe("用户改写的含义");
    expect(updated.links[0].direction).toBe(true);
    expect(board.removeLink(updated, linkId).links).toEqual([]);
  });

  it("合并组不影响卡片之间的链接", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0)],
      [mkGroup("g1", ["a", "b"]), mkGroup("g2", ["c"])],
    );
    const linked = board.addLink(state, "a", "c", false, "关联");
    const merged = board.mergeGroups(linked, "g2", "g1", 0);
    expect(merged.links).toHaveLength(1);
    expect(membersOf(merged, "g1")).toEqual(["c", "a", "b"]);
  });
});

describe("卡片：添加 / 编辑 / 删除 / 复制", () => {
  it("addCard 默认值完整，五类材料都能加", () => {
    let state = board.emptyState("board_t");
    state = board.addCard(state, { kind: "text", content: "一条注释" });
    expect(state.cards[0].kind).toBe("text");
    expect(state.cards[0].content).toBe("一条注释");
    expect(state.cards[0].checked).toBe(false);
    expect(state.cards[0].deleted).toBe(false);
    for (const kind of ["file", "image", "code", "url", "reply"] as CardKind[]) {
      state = board.addCard(state, { kind, content: "x" });
    }
    expect(new Set(state.cards.map((card) => card.kind))).toEqual(new Set(["text", "file", "image", "code", "url", "reply"]));
  });

  it("updateCard 改内容与元信息，且不改原状态", () => {
    const state = mkState([mkCard("a", 0, 0, { content: "旧" })]);
    const updated = board.updateCard(state, "a", { content: "新", meta: { language: "python" } });
    expect(board.cardById(updated, "a")?.content).toBe("新");
    expect(board.cardById(updated, "a")?.meta).toEqual({ language: "python" });
    expect(board.cardById(state, "a")?.content).toBe("旧");
  });

  it("removeCard 只标记删除并清理关系", () => {
    const state = mkState([mkCard("a"), mkCard("b")], [mkGroup("g1", ["a", "b"])], [mkLink("a", "b")], ["a", "b"]);
    const removed = board.removeCard(state, "a");
    expect(board.cardById(removed, "a")?.deleted).toBe(true);
    expect(membersOf(removed, "g1")).toEqual(["b"]);
    expect(removed.links).toEqual([]);
    expect(removed.selection).toEqual(["b"]);
  });

  it("duplicateCard 错开位置、重置勾选、留在原来的组里", () => {
    const state = mkState([mkCard("a", 10, 20, { checked: true, bookmarked: true })], [mkGroup("g1", ["a"])]);
    const copied = board.duplicateCard(state, "a");
    expect(copied.cards).toHaveLength(2);
    const copy = copied.cards.find((card) => card.id !== "a");
    expect(copy?.content).toBe(board.cardById(state, "a")?.content);
    expect([copy?.x, copy?.y]).toEqual([42, 52]);
    expect(copy?.checked).toBe(false);
    expect(membersOf(copied, "g1")).toEqual(["a", copy?.id]);
  });
});

describe("选择 / 勾选 / 隐藏 / 折叠 / 书签 / 搜索", () => {
  it("setSelection 去重并过滤已删除", () => {
    const state = mkState([mkCard("a"), mkCard("b", 0, 0, { deleted: true })]);
    expect(board.setSelection(state, ["a", "a", "b", "ghost"]).selection).toEqual(["a"]);
  });

  it("selectInRect 替换或并入选择", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 400, 0), mkCard("c", 800, 0)]);
    const rect = { x: -10, y: -10, w: 300, h: 200 };
    expect(board.selectInRect(state, rect, false).selection).toEqual(["a"]);
    expect(board.selectInRect(mkState(state.cards, [], [], ["c"]), rect, true).selection).toEqual(["c", "a"]);
  });

  it("勾选只对文字注释有效：材料与 reply 没有勾选框", () => {
    const state = mkState([
      mkCard("t", 0, 0, { hidden: true }),
      mkCard("f", 0, 0, { kind: "file", meta: { name: "a.pdf" } }),
      mkCard("r", 0, 0, { kind: "reply" }),
    ]);
    const checked = board.setChecked(state, "t", true);
    expect(board.cardById(checked, "t")?.checked).toBe(true);
    expect(board.cardById(checked, "t")?.hidden).toBe(false);
    expect(board.cardById(board.setChecked(state, "f", true), "f")?.checked).toBe(false);
    expect(board.cardById(board.setChecked(state, "r", true), "r")?.checked).toBe(false);
    expect(board.cardById(board.setChecked(checked, "t", false), "t")?.checked).toBe(false);
  });

  it("隐藏会清掉勾选，取消隐藏不会重新勾选", () => {
    const state = mkState([mkCard("t", 0, 0, { checked: true })]);
    const hidden = board.setHidden(state, "t", true);
    expect(board.cardById(hidden, "t")?.hidden).toBe(true);
    expect(board.cardById(hidden, "t")?.checked).toBe(false);
    const shown = board.setHidden(hidden, "t", false);
    expect(board.cardById(shown, "t")?.hidden).toBe(false);
    expect(board.cardById(shown, "t")?.checked).toBe(false);
  });

  it("折叠与书签只影响显示", () => {
    const state = mkState([mkCard("t", 0, 0, { checked: true })]);
    const folded = board.setFolded(state, "t", true);
    expect(board.cardById(folded, "t")?.folded).toBe(true);
    expect(board.cardById(folded, "t")?.checked).toBe(true);
    expect(board.cardById(board.setBookmark(folded, "t", true), "t")?.bookmarked).toBe(true);
  });

  it("板内搜索能查到未勾选注释，书签优先，且能搜 meta", () => {
    const state = mkState([
      mkCard("n1", 0, 0, { content: "发布节奏需要确认", checked: false }),
      mkCard("n2", 0, 0, { content: "发布前检查清单", bookmarked: true }),
      mkCard("u1", 0, 0, { kind: "url", meta: { href: "https://example.com/roadmap", title: "路线图" } }),
      mkCard("d1", 0, 0, { content: "发布节奏已删除", deleted: true }),
    ]);
    expect(board.searchCards(state, "发布")).toEqual(["n2", "n1"]);
    expect(board.searchCards(state, "ROADMAP")).toEqual(["u1"]);
    expect(board.searchCards(state, "   ")).toEqual([]);
    expect(board.searchCards(state, "不存在的词")).toEqual([]);
  });
});

// --- 重叠成组的阈值判定（契约 §1.3 / §8.2「卡片重叠」）----------------------
// 规则：靠近不成组、边框相碰不擅自合并；只有**明确目标**才提示「松开后合并成组」。

describe("重叠阈值：rectOverlapRatio / rectCenterCovered / bestMergeTarget", () => {
  it("重叠比例按**较小矩形**算，只碰边为 0", () => {
    const a = { x: 0, y: 0, w: 100, h: 100 };
    expect(board.rectOverlapRatio(a, { x: 50, y: 0, w: 100, h: 100 })).toBeCloseTo(0.5, 10);
    expect(board.rectOverlapRatio(a, { x: 100, y: 0, w: 100, h: 100 })).toBe(0);
    expect(board.rectOverlapRatio(a, { x: 0, y: 100, w: 100, h: 100 })).toBe(0);
    expect(board.rectOverlapRatio(a, { x: 25, y: 25, w: 50, h: 50 })).toBeCloseTo(1, 10);
    expect(board.rectOverlapRatio({ x: 0, y: 0, w: 0, h: 0 }, a)).toBe(0);
  });

  it("中心覆盖：落点在目标里，或目标中心在被拖动矩形里，都算明确", () => {
    const dragged = { x: 20, y: 20, w: 100, h: 100 };
    const target = { x: 0, y: 0, w: 100, h: 100 };
    // 落点 (20,20) 在目标矩形里
    expect(board.rectCenterCovered(dragged, target)).toBe(true);
    // 落点在目标外，但目标中心 (50,50) 在被拖动矩形里
    expect(board.rectCenterCovered({ x: 40, y: 40, w: 100, h: 100 }, target)).toBe(true);
    // 两个方向都不成立
    expect(board.rectCenterCovered({ x: 300, y: 300, w: 100, h: 100 }, target)).toBe(false);
  });

  it("边框相碰不算明确目标（不擅自合并）", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 100, 0), mkCard("c", 500, 0)]);
    // c 落到 100：与 b 完全重合 → 明确；与 a 只碰到边 → 不算
    expect(board.bestMergeTarget(state, "c", 100, 0)?.cardId).toBe("b");
    // c 落到 200：与谁都不碰 → 不成组
    expect(board.bestMergeTarget(state, "c", 200, 0)).toBeNull();
  });

  it("靠近不成组：距离再近也不给目标", () => {
    // a 在 0..100、b 在 200..300；c 落到 96 → 与 a 差 4px、与 b 差 4px，谁都不重叠
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 500, 0)]);
    expect(board.bestMergeTarget(state, "c", 96, 0)).toBeNull();
    // 只碰到 a 的右边（c 的右边界 = a 的左边界）也不算
    expect(board.bestMergeTarget(state, "c", 100, 0)).toBeNull();
  });

  it("只有一条细缝重叠不算明确目标（面积比例低于阈值）", () => {
    // 卡片 100x60：重叠宽 10 → 10*60/(100*60) = 0.1 < 0.25
    const state = mkState([mkCard("a", 0, 0), mkCard("c", 500, 0)]);
    expect(board.bestMergeTarget(state, "c", 90, 0)).toBeNull();
    // 重叠宽 30 → 0.3 ≥ 0.25 且中心覆盖 → 明确
    expect(board.bestMergeTarget(state, "c", 70, 0)?.cardId).toBe("a");
  });

  it("已分组的卡片不是成组目标；阈值常量可读且写清", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 100, 0), mkCard("c", 500, 0)], [mkGroup("g1", ["a"])]);
    expect(board.bestMergeTarget(state, "c", 50, 0)?.cardId).toBe("b");
    expect(board.bestMergeTarget(state, "c", 0, 0)).toBeNull();
    expect(board.MERGE_MIN_AREA_RATIO).toBe(0.25);
    expect(board.MERGE_REQUIRE_CENTER_COVER).toBe(true);
  });

  it("并列时取卡片顺序第一个，不会一次合并一叠", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 100, 0), mkCard("c", 300, 0)]);
    const best = board.bestMergeTarget(state, "c", 50, 0);
    expect(best?.cardId).toBe("a");
    expect(best?.ratio).toBeCloseTo(0.5, 10);
  });

  it("删除的卡片不是成组目标", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 100, 0, { deleted: true }), mkCard("c", 500, 0)]);
    // 与 a 重叠不足（10/60 < 0.25），与 b 完全重合但 b 已删除 → 没有明确目标
    expect(board.bestMergeTarget(state, "c", 90, 0)).toBeNull();
    // 与 a 明显重叠时仍然有目标
    expect(board.bestMergeTarget(state, "c", 30, 0)?.cardId).toBe("a");
  });

  it("预览与放下的判定一致：有目标才提示、松手才成组", () => {
    const state = mkState([mkCard("a", 0, 0), mkCard("b", 140, 0)]);
    // 落到 50：与 a 的重叠 50x50 → 比例 0.417 ≥ 0.25，明确目标
    expect(board.previewDrop(state, "b", 50, 10).mergesWith).toBe("a");
    // 落到 120：与 a 的重叠只有 20x50 → 比例 0.167 < 0.25，不算明确目标
    expect(board.previewDrop(state, "b", 120, 10).mergesWith).toBeNull();
    // 预演不改状态
    expect(state.cards.find((card) => card.id === "b")?.x).toBe(140);
    // 松手才成组，且只产生一次成组
    const dropped = board.dropCard(state, "b", 50, 10);
    expect(dropped.state.groups).toHaveLength(1);
    expect(dropped.state.groups[0].members.sort()).toEqual(["a", "b"]);
  });
});
// --- 契约 §9.1：默认组名是「默认组名」（覆盖 §1.3 / §6.1 的「组 N」）---------

describe("默认组名：系统命名路径统一用「默认组名」（契约 §9.1）", () => {
  it("isDefaultGroupName 同时认「默认组名」与历史「组 N」", () => {
    expect(board.isDefaultGroupName("默认组名")).toBe(true);
    expect(board.isDefaultGroupName("  默认组名  ")).toBe(true);
    expect(board.isDefaultGroupName("组 1")).toBe(true);
    expect(board.isDefaultGroupName("组12")).toBe(true);
    expect(board.isDefaultGroupName("发布计划")).toBe(false);
    expect(board.isDefaultGroupName("默认组名 2")).toBe(false);
    expect(board.isDefaultGroupName("")).toBe(false);
  });

  it("nextDefaultGroupName 固定返回「默认组名」，不再编号（默认名可以重复）", () => {
    expect(board.nextDefaultGroupName([])).toBe("默认组名");
    expect(board.nextDefaultGroupName(["组 7", "默认组名"])).toBe("默认组名");
    expect(board.DEFAULT_GROUP_NAME_PREFIX).toBe("默认组名");
    expect(board.DEFAULT_GROUP_NAME).toBe("默认组名");
  });

  it("三条系统命名路径（重叠成组 / 手动新建组 / 合并组）都用「默认组名」", () => {
    // ① 两张未分组卡片重叠自动成组
    const dropped = board.dropCard(twoFreeCards(), "a", 250, 10);
    const auto = board.groupById(dropped.state, dropped.groupId as string);
    expect(auto?.name).toBe("默认组名");
    expect(auto?.defaultName).toBe(true);
    // ② 手动新建组
    const manual = board.createGroup(mkState([mkCard("a"), mkCard("b")]), ["a", "b"]);
    expect(manual.groups[0].name).toBe("默认组名");
    expect(manual.groups[0].defaultName).toBe(true);
    // ③ 两个已有组合并
    const merged = board.mergeGroups(
      mkState(
        [mkCard("a"), mkCard("b"), mkCard("c"), mkCard("d")],
        [
          mkGroup("g1", ["a", "b"], { name: "目标组", defaultName: false }),
          mkGroup("g2", ["c", "d"], { name: "来源组", defaultName: false }),
        ],
      ),
      "g2",
      "g1",
    );
    expect(merged.groups[0].name).toBe("默认组名");
    expect(merged.groups[0].defaultName).toBe(true);
  });

  it("历史「组 N」不被批量覆盖：重新读取保留原名字与 defaultName", () => {
    const state = mkState(
      [mkCard("a"), mkCard("b"), mkCard("c"), mkCard("d")],
      [
        mkGroup("g1", ["a", "b"], { name: "组 7", defaultName: true }),
        mkGroup("g2", ["c"], { name: "组 3", defaultName: false }),
        mkGroup("g3", ["d"], { name: "发布计划", defaultName: false }),
      ],
    );
    const result = board.normalizeState(state);
    expect(result.groups.map((group) => group.name)).toEqual(["组 7", "组 3", "发布计划"]);
    expect(result.groups.map((group) => group.defaultName)).toEqual([true, false, false]);
    // defaultName 缺失时才按名字形态推断：两种默认名形态都算「系统给的名字」
    const inferred = board.normalizeState(
      mkState(
        [mkCard("a"), mkCard("b")],
        [
          { ...mkGroup("g1", ["a"], { name: "默认组名" }), defaultName: undefined as unknown as boolean },
          { ...mkGroup("g2", ["b"], { name: "组 5" }), defaultName: undefined as unknown as boolean },
        ],
      ),
    );
    expect(inferred.groups.map((group) => group.defaultName)).toEqual([true, true]);
  });

  it("组身份按 id：两个「默认组名」的组各自保留成员与顺序", () => {
    const state = mkState(
      [mkCard("a"), mkCard("b"), mkCard("c"), mkCard("d")],
      [
        mkGroup("g1", ["a", "b"], { name: "默认组名", defaultName: true, ordered: true }),
        mkGroup("g2", ["c", "d"], { name: "默认组名", defaultName: true, ordered: true }),
      ],
    );
    const result = board.normalizeState(state);
    expect(result.groups).toHaveLength(2);
    expect(membersOf(result, "g1")).toEqual(["a", "b"]);
    expect(membersOf(result, "g2")).toEqual(["c", "d"]);
    // 按 id 操作：改名 / 解除只影响目标组，同名组不受影响
    const renamed = board.renameGroup(result, "g1", "发布计划");
    expect(membersOf(renamed, "g2")).toEqual(["c", "d"]);
    expect(board.groupById(renamed, "g2")?.name).toBe("默认组名");
    const dissolved = board.dissolveGroup(renamed, "g1");
    expect(groupIds(dissolved)).toEqual(["g2"]);
  });

  it("加入已有组保留已有组名；改名 / 留空不破坏成员、链接与顺序", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 200, 0), mkCard("c", 600, 0)],
      [mkGroup("g1", ["a", "b"], { name: "发布计划", defaultName: false, ordered: true })],
    );
    const linked = board.addLink(state, "a", "c", false, "同一批材料");
    // 加入已有组：保留已有组名
    const joined = board.joinGroup(linked, "c", "g1", 1);
    expect(board.groupById(joined, "g1")?.name).toBe("发布计划");
    expect(membersOf(joined, "g1")).toEqual(["a", "c", "b"]);
    expect(joined.links).toHaveLength(1);
    // 改名：成员、链接、顺序都不动
    const renamed = board.renameGroup(joined, "g1", "发布计划（第二轮）");
    expect(membersOf(renamed, "g1")).toEqual(["a", "c", "b"]);
    expect(renamed.links).toHaveLength(1);
    expect(board.groupById(renamed, "g1")?.ordered).toBe(true);
    // 留空：不改动任何东西（组仍然成立、名字保留）
    const blank = board.renameGroup(renamed, "g1", "   ");
    expect(board.groupById(blank, "g1")?.name).toBe("发布计划（第二轮）");
    expect(membersOf(blank, "g1")).toEqual(["a", "c", "b"]);
    expect(blank.links).toHaveLength(1);
    // 系统默认名的组留空后仍然是「默认组名」
    const fresh = board.createGroup(mkState([mkCard("x"), mkCard("y")]), ["x", "y"]);
    expect(board.renameGroup(fresh, fresh.groups[0].id, "").groups[0].name).toBe("默认组名");
  });
});
