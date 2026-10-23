/**
 * Phase 2 集成（store 侧）· 板面纯函数：组收起 / 组端点连线 / 参与唯一事实源 / 版本保留。
 *
 * 契约：docs/interactive-page-v2-contract.md §5.1 / §5.3 / §5.4。
 * 这里只测纯函数（不碰 store / 网络）：
 * - group.collapsed 是纯显示状态，持久化但**不参与语义版本**；
 * - link 端点支持 group:<id>，组端点校验「存在 / 未删 / 至少一名存活成员」；
 *   解散组删除组端点线、保留成员卡片线；
 * - participation 是参与范围的唯一事实源，旧 checked/hidden 只是兼容镜像；
 *   selection 不改变参与，组批量改成员，提交后不清除；
 * - card/group/link 的 version 在归一化里原样保留；纯布局 / meta.display 不推进语义版本。
 */
import { describe, expect, it } from "vitest";
import * as board from "../board";
import { applyParticipation, participationOf } from "../board";
import type { BoardCard, BoardGroup, BoardLink, BoardState } from "../types";

const STAMP = "2026-10-16T00:00:00.000Z";

function mkCard(id: string, patch: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
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
    createdAt: STAMP,
    updatedAt: STAMP,
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
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function mkLink(id: string, src: string, dst: string, patch: Partial<BoardLink> = {}): BoardLink {
  return {
    id,
    src,
    dst,
    direction: false,
    meaning: "用户写的关系",
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function state(patch: Partial<BoardState> = {}): BoardState {
  return { ...board.emptyState("board_t"), ...patch };
}

describe("group.collapsed：纯显示状态，持久化但不参与语义版本（§5.4）", () => {
  it("旧数据缺省 = 展开；显式 true 被归一化保留", () => {
    const fresh = board.normalizeState(state({ cards: [mkCard("a")], groups: [mkGroup("g1", ["a"])] }));
    // 旧数据不写 collapsed 字段（缺省即展开，避免无意义 diff）
    expect(fresh.groups[0].collapsed).toBeFalsy();
    const folded = board.normalizeState(
      state({ cards: [mkCard("a")], groups: [mkGroup("g1", ["a"], { collapsed: true })] }),
    );
    expect(folded.groups[0].collapsed).toBe(true);
  });

  it("收起 / 展开可持久，且 version 一个字都不动", () => {
    const seed = state({
      cards: [mkCard("a")],
      groups: [mkGroup("g1", ["a"], { version: 7 })],
    });
    const collapsed = board.setGroupCollapsed(seed, "g1", true);
    expect(collapsed.groups[0].collapsed).toBe(true);
    expect(collapsed.groups[0].version).toBe(7);
    expect(collapsed.groups[0].members).toEqual(["a"]);
    const expanded = board.setGroupCollapsed(collapsed, "g1", false);
    // 展开 = 缺省（不写字段），语义上就是展开
    expect(expanded.groups[0].collapsed).toBeFalsy();
    expect(expanded.groups[0].version).toBe(7);
    // 已经是这个状态时不制造无意义改动：updatedAt 不被推进
    const again = board.setGroupCollapsed(expanded, "g1", false);
    expect(again.groups[0].collapsed).toBeFalsy();
    expect(again.groups[0].updatedAt).toBe(expanded.groups[0].updatedAt);
  });
});

describe("组端点连线（§5.3）", () => {
  const cards = [mkCard("a"), mkCard("b"), mkCard("c")];
  const groups = [mkGroup("g1", ["a", "b"])];

  it("归一化接受 group:<id> 并规范化空白；旧裸 card id 原样保留", () => {
    const normalized = board.normalizeState(
      state({
        cards,
        groups,
        links: [mkLink("l1", " a ", "group: g1 "), mkLink("l2", "a", "c")],
      }),
    );
    expect(normalized.links.map((link) => [link.id, link.src, link.dst])).toEqual([
      ["l1", "a", "group:g1"],
      ["l2", "a", "c"],
    ]);
  });

  it("组不存在 / 已删除 / 没有存活成员 / 非法端点：整条丢弃，不画假线", () => {
    const base = { cards: [...cards, mkCard("dead", { deleted: true })], groups };
    const normalized = board.normalizeState(
      state({
        ...base,
        links: [
          mkLink("missing-group", "a", "group:ghost"),
          mkLink("dead-card", "dead", "a"),
          mkLink("bad-prefix", "bad:thing", "a"),
          mkLink("self", "a", "a"),
          mkLink("empty-group", "a", "group:g2"),
        ],
        groups: [...groups, mkGroup("g2", ["dead"])],
      }),
    );
    expect(normalized.links).toEqual([]);
  });

  it("addLink 接受组端点；未知组 / 非法端点 / 自连一律拒绝", () => {
    const seed = board.normalizeState(state({ cards, groups }));
    const added = board.addLink(seed, "group:g1", "c", true, "用户写的关系");
    expect(added.links).toHaveLength(1);
    expect(added.links[0]).toMatchObject({ src: "group:g1", dst: "c", direction: true });
    // 同一对端点（无向）重复建立 → 更新已有那条，不新增
    const updated = board.addLink(added, "c", "group:g1", false, "改成无方向");
    expect(updated.links).toHaveLength(1);
    expect(updated.links[0]).toMatchObject({ direction: false, meaning: "改成无方向" });

    expect(board.addLink(seed, "group:ghost", "c", false, "x").links).toEqual([]);
    expect(board.addLink(seed, "a", "a", false, "x").links).toEqual([]);
    expect(board.addLink(seed, "bad:thing", "c", false, "x").links).toEqual([]);
  });

  it("updateLink 可以把卡片端点改成组端点；非法目标整次拒绝（不改任何字段）", () => {
    const seed = board.normalizeState(
      state({ cards, groups, links: [mkLink("l1", "a", "c", { meaning: "保持原样" })] }),
    );
    const retargeted = board.updateLink(seed, "l1", { dst: "group:g1" });
    expect(retargeted.links[0]).toMatchObject({ src: "a", dst: "group:g1", meaning: "保持原样" });

    const rejected = board.updateLink(retargeted, "l1", { dst: "group:ghost" });
    expect(rejected.links[0]).toMatchObject({ src: "a", dst: "group:g1" });
    const self = board.updateLink(retargeted, "l1", { src: "group:g1" });
    expect(self.links[0]).toMatchObject({ src: "a", dst: "group:g1" });
  });

  it("解散组：删除以组为端点的线，成员卡片之间的线保留", () => {
    const seed = board.normalizeState(
      state({
        cards,
        groups,
        links: [mkLink("group-line", "group:g1", "c"), mkLink("card-line", "a", "b")],
      }),
    );
    const dissolved = board.dissolveGroup(seed, "g1");
    expect(dissolved.links.map((link) => link.id)).toEqual(["card-line"]);
    expect(dissolved.groups).toEqual([]);
  });

  it("组端点线的可见性：组名 / 成员不通过端点泄露到范围之外（范围只含获准成员）", () => {
    const seed = board.normalizeState(
      state({
        cards: [mkCard("a", { checked: true }), mkCard("b", { checked: false }), mkCard("c", { checked: true })],
        groups,
        links: [mkLink("group-line", "group:g1", "c")],
      }),
    );
    const range = board.participationVisibleRange(seed);
    expect(range.groups).toHaveLength(1);
    expect(range.groups[0].members).toEqual(["a"]);
    expect(range.links.map((link) => link.id)).toEqual(["group-line"]);
  });
});

describe("participation：唯一事实源 + 旧字段兼容（§5.1）", () => {
  it("迁移口径：hidden → excluded；text/reply 保留 checked 意图；file 默认 included；code/url 永远 excluded", () => {
    expect(participationOf(mkCard("h", { hidden: true, checked: true }))).toBe("excluded");
    expect(participationOf(mkCard("t1", { checked: true }))).toBe("included");
    expect(participationOf(mkCard("t2", { checked: false }))).toBe("excluded");
    expect(participationOf(mkCard("f", { kind: "file", checked: false }))).toBe("included");
    expect(participationOf(mkCard("r", { kind: "reply", checked: true }))).toBe("included");
    expect(participationOf(mkCard("code", { kind: "code", checked: true }))).toBe("excluded");
  });

  it("显式 participation 优先，但 code/url 即使显式 included 也不可交付", () => {
    expect(participationOf(mkCard("x", { participation: "excluded", checked: true }))).toBe("excluded");
    expect(participationOf(mkCard("y", { participation: "included", checked: false }))).toBe("included");
    expect(participationOf(mkCard("z", { kind: "url", participation: "included", checked: true }))).toBe("excluded");
  });

  it("归一化后旧镜像与 participation 同口径（旧服务端仍读 checked/hidden）", () => {
    const normalized = board.normalizeState(
      state({
        cards: [
          mkCard("t", { checked: false }),
          mkCard("f", { kind: "file", checked: false }),
          mkCard("e", { participation: "excluded", checked: true }),
        ],
      }),
    );
    const byId = (id: string) => normalized.cards.find((card) => card.id === id)!;
    expect(byId("t")).toMatchObject({ participation: "excluded", checked: false });
    expect(byId("f")).toMatchObject({ participation: "included", hidden: false });
    // 显式 participation 是权威：历史 checked 不被反向改写（旧界面仍按它显示，但不再决定可见性）
    expect(byId("e")).toMatchObject({ participation: "excluded", checked: true });
  });

  it("setParticipation 同步旧镜像；材料不参与时用 hidden 表达（旧规则）", () => {
    const seed = board.normalizeState(
      state({ cards: [mkCard("t", { checked: true }), mkCard("f", { kind: "file" })] }),
    );
    const off = board.setParticipation(seed, "t", false);
    expect(board.cardById(off, "t")).toMatchObject({ participation: "excluded", checked: false });
    const fileOff = board.setParticipation(seed, "f", false);
    expect(board.cardById(fileOff, "f")).toMatchObject({ participation: "excluded", hidden: true });
    const fileOn = board.setParticipation(fileOff, "f", true);
    expect(board.cardById(fileOn, "f")).toMatchObject({ participation: "included", hidden: false });
    // 遗留不可交付卡不受影响（显式打开也不放行）
    const legacy = board.normalizeState(state({ cards: [mkCard("code", { kind: "code" })] }));
    const legacyAfter = board.setParticipation(legacy, "code", true);
    expect(legacyAfter.cards[0].participation).toBe("excluded");
    expect(legacyAfter.cards[0].version).toBeUndefined();
    expect(legacyAfter.cards[0].updatedAt).toBe(legacy.cards[0].updatedAt);
  });

  it("hidden 优先级最高：显式 included + hidden 仍然 excluded（与后端 D 同口径）", () => {
    expect(participationOf(mkCard("h", { participation: "included", hidden: true, checked: true }))).toBe("excluded");
    const normalized = board.normalizeState(
      state({ cards: [mkCard("h", { participation: "included", hidden: true, checked: true })] }),
    );
    expect(normalized.cards[0].participation).toBe("excluded");
    expect(normalized.cards[0].hidden).toBe(true);
  });

  it("旧 code / url 迁移来的文字卡（meta.language / meta.url）默认 excluded", () => {
    expect(participationOf(mkCard("code-text", { meta: { language: "python" } }))).toBe("excluded");
    expect(participationOf(mkCard("url-text", { meta: { url: { href: "https://example.com" } } }))).toBe("excluded");
    const normalized = board.normalizeState(
      state({
        cards: [
          mkCard("code-text", { meta: { language: "python" }, checked: true }),
          mkCard("url-text", { meta: { url: { href: "https://example.com" }, checked: true } }),
        ],
      }),
    );
    expect(normalized.cards.map((card) => card.participation)).toEqual(["excluded", "excluded"]);
  });

  it("组批量改成员；组本身没有独立 participation；选择不改变参与", () => {
    const seed = board.normalizeState(
      state({
        cards: [mkCard("a", { checked: true }), mkCard("b", { checked: false }), mkCard("c", { checked: false })],
        groups: [mkGroup("g1", ["a", "b"])],
      }),
    );
    expect(board.groupParticipation(seed, "g1")).toBe("partial");
    const off = board.setGroupParticipation(seed, "g1", false);
    expect(board.groupParticipation(off, "g1")).toBe("none");
    const on = board.setGroupParticipation(off, "g1", true);
    expect(board.groupParticipation(on, "g1")).toBe("all");
    // 未被选中的 c 不受影响
    expect(board.cardById(on, "c")!.participation).toBe("excluded");

    const selected = board.setSelection(on, ["a", "b", "c"]);
    expect(selected.selection).toEqual(["a", "b", "c"]);
    for (const id of ["a", "b", "c"]) {
      expect(board.cardById(selected, id)!.participation).toBe(
        board.cardById(on, id)!.participation,
      );
    }
  });

  it("提交后不清除参与：participation 在归一化 / 保存往返里保持不变", () => {
    const seed = board.normalizeState(state({ cards: [mkCard("a", { participation: "included" })] }));
    expect(seed.cards[0].participation).toBe("included");
    // 模拟「服务端回执清了 checked」：participation 仍是唯一事实源
    const roundTrip = board.normalizeState({ ...seed, cards: [{ ...seed.cards[0], checked: false }] });
    expect(roundTrip.cards[0].participation).toBe("included");
    // 显式 participation 是权威；checked 只是历史字段，不被反向改写
    expect(roundTrip.cards[0].checked).toBe(false);
  });

  it("一次性迁移标记 participationVersion=1；applyParticipation 不补时间戳", () => {
    const migrated = board.migrateParticipation(state({ cards: [mkCard("a", { checked: true })] }));
    expect(migrated.participationVersion).toBe(1);
    const stable = board.migrateParticipation(migrated);
    expect(stable.participationVersion).toBe(1);

    const aligned = applyParticipation(
      state({
        cards: [{ ...mkCard("a", { checked: true }), updatedAt: "2020-01-01T00:00:00.000Z" }],
      }),
    );
    expect(aligned.cards[0].updatedAt).toBe("2020-01-01T00:00:00.000Z");
    expect(aligned.cards[0].participation).toBe("included");
    expect(aligned.participationVersion).toBe(1);
  });

  it("本地可见范围按 participation 计算：只列参与成员 / 两端都在范围内才留关系", () => {
    const withCard = board.normalizeState(
      state({
        cards: [
          mkCard("on", { participation: "included", checked: true }),
          mkCard("off", { participation: "excluded", checked: false }),
          mkCard("on2", { participation: "included", checked: true }),
        ],
        groups: [mkGroup("g1", ["on", "off"])],
        links: [mkLink("ok", "on", "on2"), mkLink("out", "on", "off")],
        selection: ["on", "off"],
      }),
    );
    const range = board.participationVisibleRange(withCard);
    expect(range.cards.map((card) => card.id)).toEqual(["on", "on2"]);
    expect(range.groups[0].members).toEqual(["on"]);
    expect(range.links.map((link) => link.id)).toEqual(["ok"]);
    expect(range.selection).toEqual(["on"]);
    expect(range.notVisibleCount).toBe(1);
    expect(range.empty).toBe(false);
    expect(board.participationVisibleRange(null).empty).toBe(true);
  });
});

describe("端点兼容写法 card:<id>（与后端 D 同口径）", () => {
  const cards = [mkCard("a"), mkCard("c"), mkCard("d")];
  const groups = [mkGroup("g1", ["a"])];

  it("归一化把 card:<id> 落成裸 id；组端点照旧 group:<id>", () => {
    const normalized = board.normalizeState(
      state({
        cards,
        groups,
        links: [mkLink("l1", "card:a", "group:g1"), mkLink("l2", " card:c ", "a")],
      }),
    );
    expect(normalized.links.map((link) => [link.src, link.dst])).toEqual([
      ["a", "group:g1"],
      ["c", "a"],
    ]);
  });

  it("addLink / updateLink 接受 card:<id>；超长端点整条拒绝", () => {
    const seed = board.normalizeState(state({ cards, groups }));
    const added = board.addLink(seed, "card:a", "c", false, "兼容写法");
    expect(added.links[0]).toMatchObject({ src: "a", dst: "c" });
    const tooLong = "a".repeat(201);
    expect(board.addLink(seed, tooLong, "c", false, "x").links).toEqual([]);

    // 改成 c→c 自连：整次拒绝，原关系一个字不动
    const self = board.updateLink(added, added.links[0].id, { src: "card:c" });
    expect(self.links[0]).toMatchObject({ src: "a", dst: "c" });
    // 合法改端点仍然接受（把起点换成 d）
    const retarget = board.updateLink(added, added.links[0].id, { src: "card:d" });
    expect(retarget.links[0]).toMatchObject({ src: "d", dst: "c" });
  });
});

describe("版本保留与 meta.display（§5.2 / §5.4）", () => {
  const display = { width: 800, height: 600, aspectRatio: 4 / 3 };

  it("card / group / link 的 version 在归一化里原样保留", () => {
    const normalized = board.normalizeState(
      state({
        cards: [
          mkCard("a", { kind: "file", version: 5, meta: { display } }),
          mkCard("a2", { version: 2 }),
        ],
        groups: [mkGroup("g1", ["a"], { version: 9 })],
        links: [mkLink("l1", "a", "a2", { version: 3 })],
      }),
    );
    expect(normalized.cards[0].version).toBe(5);
    const block = normalized.cards[0].meta.display as {
      width: number;
      height: number;
      aspectRatio: number;
    };
    expect(block.width).toBe(800);
    expect(block.height).toBe(600);
    expect(block.aspectRatio).toBeCloseTo(4 / 3, 5);
    expect(normalized.groups[0].version).toBe(9);
    expect(normalized.links[0].version).toBe(3);
  });

  it("布局 / 书签 / 折叠 / 收起 / 参与都不推进语义版本（前端不自增 version）", () => {
    const seed = board.normalizeState(
      state({
        cards: [mkCard("a", { kind: "file", version: 5, meta: { display } })],
        groups: [mkGroup("g1", ["a"], { version: 9 })],
      }),
    );
    const moved = board.updateCard(seed, "a", { x: 400, y: 300, bookmarked: true, folded: true });
    expect(moved.cards[0].version).toBe(5);
    const participation = board.setParticipation(moved, "a", false);
    expect(participation.cards[0].version).toBe(5);
    const collapsed = board.setGroupCollapsed(participation, "g1", true);
    expect(collapsed.groups[0].version).toBe(9);
    // meta.display 只是显示信息：仍原样保留（规范化后的同一比例），且不会被 layout 操作改写
    const block = collapsed.cards[0].meta.display as { aspectRatio: number };
    expect(block.aspectRatio).toBeCloseTo(display.aspectRatio, 5);
  });

  it("合法 display 被规范化，并以 width 为准收敛 height（与后端 apply_card_display 一致）", () => {
    const normalized = board.normalizeState(
      state({
        cards: [
          mkCard("img", { kind: "file", w: 800, h: 300, version: 4, meta: { display } }),
          mkCard("img2", { kind: "file", w: 240, h: 96, version: 4, meta: { display } }),
        ],
      }),
    );
    const img = normalized.cards.find((card) => card.id === "img")!;
    expect(img.w).toBe(800);
    expect(img.h).toBe(600); // 800 / (4/3)
    const block = img.meta.display as { width: number; height: number; aspectRatio: number };
    expect([block.width, block.height]).toEqual([800, 600]);
    expect(block.aspectRatio).toBeCloseTo(display.aspectRatio, 5);
    expect(img.version).toBe(4); // 显示收敛不推进语义版本
    const img2 = normalized.cards.find((card) => card.id === "img2")!;
    expect(img2.w).toBe(240);
    expect(img2.h).toBe(180);
    expect(img2.version).toBe(4);
  });

  it("非法 display（0 / NaN / 非正数）被删除，不写 0，也不猜比例", () => {
    const normalized = board.normalizeState(
      state({
        cards: [
          mkCard("bad", { kind: "file", w: 240, h: 96, meta: { display: { width: 0, height: 600 } } }),
          mkCard("nan", { kind: "file", w: 240, h: 96, meta: { display: { width: Number.NaN, height: 600 } } }),
          mkCard("neg", { kind: "file", w: 240, h: 96, meta: { display: { width: -800, height: 600 } } }),
        ],
      }),
    );
    for (const id of ["bad", "nan", "neg"]) {
      const card = normalized.cards.find((item) => item.id === id)!;
      expect(Object.prototype.hasOwnProperty.call(card.meta, "display")).toBe(false);
      expect([card.w, card.h]).toEqual([240, 96]); // 几何保持原值，不被猜测的比例改写
    }
  });

  it("只有 aspectRatio 的 display 被接受并只保留比例；宽高只给一个则丢弃（与后端一致）", () => {
    const ratioOnly = board.normalizeState(
      state({
        cards: [
          mkCard("ratio", { kind: "file", w: 400, h: 400, meta: { display: { aspectRatio: 2 } } }),
          mkCard("half", { kind: "file", w: 240, h: 96, meta: { display: { width: 800 } } }),
        ],
      }),
    );
    const ratio = ratioOnly.cards.find((card) => card.id === "ratio")!;
    expect(ratio.meta.display).toEqual({ aspectRatio: 2 });
    expect(ratio.w).toBe(400);
    expect(ratio.h).toBe(200); // 400 / 2
    const half = ratioOnly.cards.find((card) => card.id === "half")!;
    expect(Object.prototype.hasOwnProperty.call(half.meta, "display")).toBe(false);
    expect([half.w, half.h]).toEqual([240, 96]);
  });

  it("非 file 卡上的 display 被清掉（与后端 clean_card_display 同一口径）", () => {
    const normalized = board.normalizeState(
      state({
        cards: [mkCard("text", { meta: { display } }), mkCard("reply", { kind: "reply", meta: { display } })],
      }),
    );
    for (const card of normalized.cards) {
      expect(Object.prototype.hasOwnProperty.call(card.meta, "display")).toBe(false);
      expect([card.w, card.h]).toEqual([240, 96]);
    }
  });

  it("display 收敛不丢其它 meta 字段，也不改 updatedAt", () => {
    const stamp = "2020-01-01T00:00:00.000Z";
    const normalized = board.normalizeState(
      state({
        cards: [
          mkCard("img", {
            kind: "file",
            w: 800,
            h: 300,
            updatedAt: stamp,
            meta: { name: "照片.png", file: { attachmentId: "att_1" }, display },
          }),
        ],
      }),
    );
    const img = normalized.cards[0];
    expect(img.meta.name).toBe("照片.png");
    expect((img.meta.file as { attachmentId: string }).attachmentId).toBe("att_1");
    expect(img.updatedAt).toBe(stamp);
  });

  it("clampBoardBounds 只夹坐标，不动 version / meta / participation", () => {
    const card = { ...mkCard("a", { kind: "file", version: 4, meta: { display } }), participation: "included" as const };
    const clamped = board.clampBoardBounds(state({ cards: [{ ...card, x: 1e12 }] }));
    expect(clamped.cards[0]).toMatchObject({ participation: "included", version: 4 });
    expect(clamped.cards[0].meta.display).toEqual(display);
  });
});
