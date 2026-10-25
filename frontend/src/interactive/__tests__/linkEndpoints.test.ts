/**
 * Phase 2-B 组端点 / 连线显示几何 / 组框折叠槽位（纯函数）。
 *
 * 契约：docs/interactive-page-v2-contract.md §5.3。
 * 这里钉住四件事：
 * 1. 端点身份：卡片 = 裸 card id，组 = `group:<id>`；解析/归一化/校验是纯函数，
 *    旧数据（裸 card id）不需要迁移；同一对端点只保留一条（G4）。
 * 2. 显示锚点：连线落在两张最终显示矩形的**边缘**上（不是中心点）。
 * 3. 组收起：成员的对外连线视觉汇聚到组边缘，但 `src/dst` 的真实身份一个字不改；
 *    展开后恢复成员边缘；组内成员之间的线在收起期间不画（不是对外关系）。
 * 4. 隐私：组端点的可见性按参与范围校验；无障碍文案只用组名 / 「卡片」，绝不列成员名或正文。
 */
import { describe, expect, it } from "vitest";
import type { BoardCard, BoardGroup, BoardLink } from "../types";
import {
  GROUP_ENDPOINT_PREFIX,
  GROUP_HEADER_HEIGHT,
  GROUP_OVERVIEW_MAX_SLOTS,
  GROUP_OVERVIEW_SLOT,
  GROUP_PAD_X,
  GROUP_RESIZE_EDGES,
  buildDisplayLinks,
  checkGroupEndpoint,
  edgeAnchor,
  endpointInVisibleRange,
  endpointKey,
  endpointValue,
  cardEdgeAnchors,
  groupEdgeAnchors,
  groupEndpoint,
  groupOverviewSlots,
  groupResizeHandles,
  linkAnchor,
  normalizeBoardLinks,
  parseEndpoint,
  rectCenter,
  resolveEndpoint,
  sameEndpoint,
} from "../linkEndpoints";

const STAMP = "2026-10-16T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x,
    y,
    w: 120,
    h: 80,
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
    x: 80,
    y: 40,
    w: 360,
    h: 160,
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

const cards: BoardCard[] = [
  mkCard("a", 100, 100),
  mkCard("b", 400, 100),
  mkCard("c", 100, 400),
  mkCard("dead", 0, 0, { deleted: true }),
  mkCard("excluded", 700, 100),
];
const groups: BoardGroup[] = [
  mkGroup("g1", ["a", "b"]),
  mkGroup("g2", ["a"], { deleted: true }),
  mkGroup("g3", ["dead"]),
  mkGroup("g4", ["excluded"], { x: 660, y: 40, w: 220, h: 160 }),
];
const ctx = { cards, groups };

describe("端点解析与归一化（卡片裸 id / 组 group:<id>）", () => {
  it("卡片是裸 id，组是 group:<id>；两侧空白被规范化", () => {
    expect(parseEndpoint(" c1 ")).toMatchObject({ ok: true, endpoint: { kind: "card", id: "c1", value: "c1" } });
    expect(parseEndpoint("group:g1")).toMatchObject({ ok: true, endpoint: { kind: "group", id: "g1", value: "group:g1" } });
    expect(parseEndpoint(" GROUP:g1 ")).toMatchObject({ ok: true, endpoint: { kind: "group", id: "g1", value: "group:g1" } });
    expect(endpointValue(" group:g1 ")).toBe("group:g1");
    expect(endpointValue(42)).toBe("42");
  });

  it("空值 / 缺 id / 含冒号的未知前缀 / id 内有空白都判为无效，不猜", () => {
    expect(parseEndpoint("")).toMatchObject({ ok: false, reason: "empty" });
    expect(parseEndpoint("   ")).toMatchObject({ ok: false, reason: "empty" });
    expect(parseEndpoint(null)).toMatchObject({ ok: false, reason: "empty" });
    expect(parseEndpoint(undefined)).toMatchObject({ ok: false, reason: "empty" });
    expect(parseEndpoint("group:")).toMatchObject({ ok: false, reason: "missing-id" });
    expect(parseEndpoint("group:    ")).toMatchObject({ ok: false, reason: "missing-id" });
    expect(parseEndpoint("group:g 1")).toMatchObject({ ok: false, reason: "invalid-id" });
    expect(parseEndpoint("card:c1")).toMatchObject({ ok: false, reason: "unknown-prefix" });
  });

  it("规范化后的键用于同一对端点的去重，卡片与组同 id 也判为不同端点", () => {
    expect(endpointKey("a")).toBe("card:a");
    expect(endpointKey(" group:g1 ")).toBe("group:g1");
    expect(sameEndpoint("a", " a ")).toBe(true);
    expect(sameEndpoint("a", "group:a")).toBe(false);
    expect(endpointValue("bad:thing")).toBe("");
  });
});

describe("端点校验：存在 / 未删 / 获准成员", () => {
  it("卡片端点必须存在且未删除", () => {
    const resolved = resolveEndpoint("a", ctx);
    expect(resolved).not.toBeNull();
    expect(resolved).toMatchObject({ kind: "card", id: "a", memberIds: ["a"] });
    expect(resolved!.rect).toEqual({ x: 100, y: 100, w: 120, h: 80 });
    expect(resolveEndpoint("dead", ctx)).toBeNull();
    expect(resolveEndpoint("missing", ctx)).toBeNull();
  });

  it("组端点必须组存在、未删、至少一名存活成员", () => {
    const resolved = resolveEndpoint("group:g1", ctx);
    expect(resolved).not.toBeNull();
    expect(resolved).toMatchObject({ kind: "group", id: "g1", memberIds: ["a", "b"] });
    expect(resolved!.rect).toEqual({ x: 80, y: 40, w: 360, h: 160 });
    expect(resolveEndpoint("group:g2", ctx)).toBeNull(); // 已删除的组
    expect(resolveEndpoint("group:g3", ctx)).toBeNull(); // 成员全被删除
    expect(resolveEndpoint("group:missing", ctx)).toBeNull();
    expect(resolveEndpoint("bad:thing", ctx)).toBeNull();
  });

  it("给了参与范围时，组端点只认获准成员；一个都没有就不成立", () => {
    const included = { ...ctx, includedCardIds: ["a"] };
    expect(resolveEndpoint("group:g1", included)!.memberIds).toEqual(["a"]);
    expect(resolveEndpoint("group:g4", included)).toBeNull(); // 唯一成员未参与 → 组端点不成立
    // 卡片端点：用户视野里仍能显示（是否可交给 QIO 由 endpointInVisibleRange 判定）
    expect(resolveEndpoint("excluded", included)).not.toBeNull();
    expect(endpointInVisibleRange("excluded", included)).toMatchObject({ ok: false, reason: "not-included" });
  });

  it("可见范围校验：卡片按参与判定，组按至少一名获准成员判定", () => {
    expect(endpointInVisibleRange("a", ctx)).toEqual({ ok: true });
    expect(endpointInVisibleRange("group:g1", ctx)).toEqual({ ok: true });
    expect(endpointInVisibleRange("dead", ctx)).toMatchObject({ ok: false, reason: "missing-card" });
    expect(endpointInVisibleRange("group:g2", ctx)).toMatchObject({ ok: false, reason: "missing-group" });
    expect(endpointInVisibleRange("group:g3", ctx)).toMatchObject({ ok: false, reason: "no-members" });
    expect(endpointInVisibleRange("bad:thing", ctx)).toMatchObject({ ok: false, reason: "invalid" });

    const included = { ...ctx, includedCardIds: ["a", "c"] };
    expect(endpointInVisibleRange("a", included)).toEqual({ ok: true });
    expect(endpointInVisibleRange("b", included)).toMatchObject({ ok: false, reason: "not-included" });
    expect(endpointInVisibleRange("group:g1", included)).toEqual({ ok: true });
    expect(endpointInVisibleRange("group:g4", included)).toMatchObject({ ok: false, reason: "no-included-members" });
    expect(checkGroupEndpoint("g4", included)).toMatchObject({ ok: false, reason: "no-included-members" });
  });
});

describe("连线锚定最终显示边缘（不是中心点）", () => {
  const rect = { x: 0, y: 0, w: 100, h: 60 };

  it("正右 / 正左 / 正上 / 正下都落在对应边中点上", () => {
    expect(rectCenter(rect)).toEqual({ x: 50, y: 30 });
    expect(edgeAnchor(rect, { x: 200, y: 30 })).toEqual({ x: 100, y: 30 });
    expect(edgeAnchor(rect, { x: -100, y: 30 })).toEqual({ x: 0, y: 30 });
    expect(edgeAnchor(rect, { x: 50, y: -100 })).toEqual({ x: 50, y: 0 });
    expect(edgeAnchor(rect, { x: 50, y: 200 })).toEqual({ x: 50, y: 60 });
  });

  it("斜向落在边界上（含角点）；目标与中心重合时退化为右侧边中点，不产生 NaN", () => {
    const corner = edgeAnchor(rect, { x: 150, y: 90 });
    expect(corner.x).toBeCloseTo(100);
    expect(corner.y).toBeCloseTo(60);
    const fallback = edgeAnchor(rect, { x: 50, y: 30 });
    expect(fallback).toEqual({ x: 100, y: 30 });

    for (const angle of [0.1, 0.7, 1.4, 2.3, 3.1, 4.2, 5.5]) {
      const toward = { x: 50 + Math.cos(angle) * 500, y: 30 + Math.sin(angle) * 500 };
      const anchor = edgeAnchor(rect, toward);
      const onEdge =
        Math.abs(anchor.x - 0) < 1e-6 ||
        Math.abs(anchor.x - 100) < 1e-6 ||
        Math.abs(anchor.y - 0) < 1e-6 ||
        Math.abs(anchor.y - 60) < 1e-6;
      expect(onEdge, "角度 " + angle + " 的锚点应在矩形边界上：" + JSON.stringify(anchor)).toBe(true);
      expect(Number.isFinite(anchor.x) && Number.isFinite(anchor.y)).toBe(true);
    }
  });

  it("两个矩形之间：两端各自锚在自己的边缘上", () => {
    const a = { x: 0, y: 0, w: 100, h: 60 };
    const b = { x: 300, y: 0, w: 100, h: 60 };
    expect(linkAnchor(a, b)).toEqual({ start: { x: 100, y: 30 }, end: { x: 300, y: 30 } });

    const below = { x: 0, y: 300, w: 100, h: 60 };
    expect(linkAnchor(a, below)).toEqual({ start: { x: 50, y: 60 }, end: { x: 50, y: 300 } });
  });
});

describe("显示层：真实端点身份不变，显示锚点可汇聚", () => {
  const base = { cards, groups };

  it("卡片—卡片：锚在边缘，标签在中点，输出没有汇聚标记", () => {
    const result = buildDisplayLinks({ ...base, links: [mkLink("l1", "a", "b")] });
    expect(result.skipped).toEqual([]);
    expect(result.links).toHaveLength(1);
    const line = result.links[0];
    expect(line).toMatchObject({
      id: "l1",
      src: "a",
      dst: "b",
      srcKind: "card",
      dstKind: "card",
      displaySrcId: "a",
      displayDstId: "b",
      aggregatedSrc: false,
      aggregatedDst: false,
    });
    expect({ x1: line.x1, y1: line.y1 }).toEqual({ x1: 226, y1: 140 }); // a 的右边 + 固定外距
    expect({ x2: line.x2, y2: line.y2 }).toEqual({ x2: 394, y2: 140 }); // b 的左边 - 固定外距
    expect(line.labelX).toBe(310);
    expect(line.labelY).toBe(140);
    expect(line.direction).toBe(false);
    expect(line.meaning).toBe("用户写的关系");
  });

  it("组端点：锚在组框边缘，成员集合来自当前组，而不是复制成多条线", () => {
    const result = buildDisplayLinks({ ...base, links: [mkLink("l2", "group:g1", "c")] });
    expect(result.skipped).toEqual([]);
    const line = result.links[0];
    expect(line.srcKind).toBe("group");
    expect(line.displaySrcId).toBe("group:g1");
    // 组框 y=40,h=160 → 下边缘 y=200
    expect(line.y1).toBe(200);
    expect(line.x1).toBeGreaterThanOrEqual(80);
    expect(line.x1).toBeLessThanOrEqual(440);
    // 同一个组端点永远只有一条线（当前成员集合由组框表达，不按成员展开）
    expect(result.links.filter((item) => item.displaySrcId === "group:g1")).toHaveLength(1);
  });

  it("组收起：成员对外线汇聚到组边缘，但 src/dst 真实身份不变；展开后恢复成员边缘", () => {
    const folded = buildDisplayLinks({
      ...base,
      links: [mkLink("l3", "a", "c")],
      foldedGroupIds: ["g1"],
    });
    expect(folded.skipped).toEqual([]);
    const line = folded.links[0];
    expect(line.src).toBe("a"); // 真实端点身份不变
    expect(line.displaySrcId).toBe("group:g1");
    expect(line.aggregatedSrc).toBe(true);
    expect(line.srcKind).toBe("card");
    expect(line.y1).toBe(200); // 组框下边缘

    const expanded = buildDisplayLinks({ ...base, links: [mkLink("l3", "a", "c")], foldedGroupIds: [] });
    const back = expanded.links[0];
    expect(back.displaySrcId).toBe("a");
    expect(back.aggregatedSrc).toBe(false);
    // a 与 c 在同一条竖线上：展开后锚回 a 的下边缘中点
    expect({ x1: back.x1, y1: back.y1 }).toEqual({ x1: 160, y1: 186 });
  });

  it("组收起时组内成员之间的线不画（不是对外关系），展开后恢复", () => {
    const folded = buildDisplayLinks({
      ...base,
      links: [mkLink("internal", "a", "b")],
      foldedGroupIds: ["g1"],
    });
    expect(folded.links).toEqual([]);
    expect(folded.skipped).toEqual([{ id: "internal", reason: "collapsed-internal" }]);

    const expanded = buildDisplayLinks({ ...base, links: [mkLink("internal", "a", "b")] });
    expect(expanded.links).toHaveLength(1);
  });

  it("端点不存在 / 已删除 / 自己连自己 / 垃圾字符串：如实跳过，不画假线", () => {
    const result = buildDisplayLinks({
      ...base,
      links: [
        mkLink("gone", "a", "dead"),
        mkLink("unknown-group", "group:missing", "a"),
        mkLink("self", "a", "a"),
        mkLink("junk", "bad:thing", "a"),
      ],
    });
    expect(result.links).toEqual([]);
    expect(result.skipped.map((item) => [item.id, item.reason])).toEqual([
      ["gone", "missing-endpoint"],
      ["unknown-group", "missing-endpoint"],
      ["self", "self"],
      ["junk", "invalid-endpoint"],
    ]);
  });

  it("只输出可交给 QIO 的线时：未参与卡片与无获准成员的组端点都被排除", () => {
    const result = buildDisplayLinks({
      ...base,
      links: [
        mkLink("ok", "a", "c"),
        mkLink("excluded", "excluded", "c"),
        mkLink("group-ok", "group:g1", "c"),
        mkLink("group-out", "group:g4", "a"),
      ],
      includedCardIds: ["a", "c"],
      onlyInRange: true,
    });
    expect(result.links.map((item) => item.id)).toEqual(["ok", "group-ok"]);
    expect(result.links.every((item) => item.rangeVisible)).toBe(true);
    expect(result.skipped).toEqual([
      { id: "excluded", reason: "out-of-range" },
      { id: "group-out", reason: "out-of-range" },
    ]);
  });

  it("隐私：组端点的无障碍文案只用组名与「卡片」，不列成员名 / 正文 / id", () => {
    const secretCards = [mkCard("a", 100, 100, { content: "秘密正文不该出现在关系里" }), mkCard("c", 400, 100)];
    const result = buildDisplayLinks({
      cards: secretCards,
      groups: [mkGroup("g1", ["a"], { name: "发布计划" })],
      links: [mkLink("l", "group:g1", "c")],
    });
    const aria = result.links[0].ariaLabel;
    expect(aria).toContain("发布计划");
    expect(aria).toContain("关系");
    expect(aria).not.toContain("秘密正文");
    expect(aria).not.toContain("卡片 a");
    expect(aria).not.toContain("a");
  });

  it("解散组：组端点线消失，成员卡片之间的线保留（契约 §5.3）", () => {
    // g1 已解散（不在 live groups 里）：group:g1 端点解析失败，但 a→b 的卡线不受影响
    const dissolved = { ...base, groups: [] as typeof groups };
    const display = buildDisplayLinks({
      ...dissolved,
      cards,
      links: [mkLink("group-line", "group:g1", "c"), mkLink("card-line", "a", "b")],
    });
    expect(display.links.map((item) => item.id)).toEqual(["card-line"]);
    expect(display.skipped).toEqual([{ id: "group-line", reason: "missing-endpoint" }]);
  });

  it("组端点线跟着当前成员集合的组框走：成员变化后仍只有一条线，锚点随组框更新", () => {
    const before = buildDisplayLinks({ ...base, links: [mkLink("l2", "group:g1", "c")] });
    const after = buildDisplayLinks({
      ...base,
      links: [mkLink("l2", "group:g1", "c")],
      groups: [mkGroup("g1", ["a", "b"], { x: 80, y: 40, w: 520, h: 200 })],
    });
    expect(before.links).toHaveLength(1);
    expect(after.links).toHaveLength(1);
    expect(after.links[0].y1).toBe(240); // 新组框下边缘 y = 40 + 200
    expect(after.links[0].y1).not.toBe(before.links[0].y1);
  });

  it("同样输入永远得到同样输出（确定性）", () => {
    const input = {
      ...base,
      links: [mkLink("l1", "a", "b"), mkLink("l2", "group:g1", "c")],
      foldedGroupIds: ["g1"],
      activeLinkId: "l2",
    };
    expect(JSON.stringify(buildDisplayLinks(input))).toBe(JSON.stringify(buildDisplayLinks(input)));
    // 组内 a→b 在收起时被跳过，l2 是唯一画出来的线
    expect(buildDisplayLinks(input).links).toHaveLength(1);
    expect(buildDisplayLinks(input).links[0].active).toBe(true);
  });
});

describe("组端点锚点：只画被关系实际使用的边缘位置", () => {
  const base = { cards, groups };

  it("组端点关系只产生一个锚点，位置与线端点一致（不是四条边各一个端口）", () => {
    const anchors = groupEdgeAnchors({ ...base, links: [mkLink("l2", "group:g1", "c")] });
    expect(anchors).toHaveLength(1);
    expect(anchors[0]).toMatchObject({
      groupId: "g1",
      endpoint: "group:g1",
      groupName: "发布计划",
      side: "bottom",
      linkIds: ["l2"],
    });
    // 组框 80..440 x 40..200；c 中心 (160,440) → 下边缘 x=235,y=200
    expect(anchors[0].x).toBeCloseTo(260, 6);
    expect(anchors[0].y).toBe(200);
  });

  it("不同方向的关系各留各的锚点；没有组端点关系时一个都不出现", () => {
    const anchors = groupEdgeAnchors({
      ...base,
      links: [mkLink("l2", "group:g1", "c"), mkLink("l3", "group:g1", "excluded")],
    });
    expect(anchors.map((item) => [item.side, item.linkIds])).toEqual([
      ["bottom", ["l2"]],
      ["right", ["l3"]],
    ]);
    expect(anchors[1].x).toBe(440); // 组框右边

    const cardOnly = groupEdgeAnchors({ ...base, links: [mkLink("l1", "a", "b")] });
    expect(cardOnly).toEqual([]);
    const missingGroup = groupEdgeAnchors({ ...base, links: [mkLink("gone", "group:g2", "c")] });
    expect(missingGroup).toEqual([]);
  });

  it("组收起时成员线的汇聚不是组端点：不凭空长组锚点，真实身份仍是成员卡", () => {
    const folded = groupEdgeAnchors({
      ...base,
      links: [mkLink("l3", "a", "c")],
      foldedGroupIds: ["g1"],
    });
    expect(folded).toEqual([]);
    const display = buildDisplayLinks({
      ...base,
      links: [mkLink("l3", "a", "c")],
      foldedGroupIds: ["g1"],
    });
    expect(display.links[0]).toMatchObject({ src: "a", displaySrcId: "group:g1", aggregatedSrc: true });
  });

  it("组端点的锚点仍保留 curve 字段与既有几何（锚点不另算一套线）", () => {
    const curve = { x: 18, y: -12 };
    const anchors = groupEdgeAnchors({ ...base, links: [mkLink("l2", "group:g1", "c", { curve })] });
    const display = buildDisplayLinks({ ...base, links: [mkLink("l2", "group:g1", "c", { curve })] });
    expect(display.links[0].curve).toEqual(curve);
    expect({ x: anchors[0].x, y: anchors[0].y }).toEqual({ x: display.links[0].x1, y: display.links[0].y1 });
  });
});

describe("卡片端点锚点：真实 src/dst 与线端点同源", () => {
  const base = { cards, groups };

  it("卡片—卡片关系只产生两个真实卡片锚点，不产生组锚点", () => {
    const anchors = cardEdgeAnchors({ ...base, links: [mkLink("l1", "a", "b")] });
    expect(anchors).toHaveLength(2);
    expect(anchors.map((item) => item.endpoint)).toEqual(["a", "b"]);
    expect(anchors.map((item) => item.side)).toEqual(["right", "left"]);
    expect(anchors.map((item) => item.linkIds)).toEqual([["l1"], ["l1"]]);
  });

  it("组端点关系只为另一端真实卡片产生锚点", () => {
    const anchors = cardEdgeAnchors({ ...base, links: [mkLink("l2", "group:g1", "c")] });
    expect(anchors.map((item) => item.endpoint)).toEqual(["c"]);
  });

  it("组收起时成员线的视觉汇聚不是卡片端点", () => {
    const folded = cardEdgeAnchors({
      ...base,
      links: [mkLink("l3", "a", "c")],
      foldedGroupIds: ["g1"],
    });
    // c 仍是真实卡片；a 被汇聚到组，不能在国内层冒充卡片端点
    expect(folded.map((item) => item.endpoint)).toEqual(["c"]);
  });
});

describe("归一化：旧裸 card id 不变，组端点写 group:<id>，同一对端点只留一条", () => {
  it("规范化 src/dst、保留未知字段（如 version），并按无向对去重", () => {
    const input = [
      { ...mkLink("l1", " a ", "group: g1 "), version: 3 } as BoardLink & { version: number },
      mkLink("l2", "group:g1", "a"), // 同一对端点（无向）：重复
      mkLink("self", "a", "a"),
      mkLink("bad", "group:", "a"),
      mkLink("dead", "a", "dead"),
    ];
    const result = normalizeBoardLinks(input, ctx);
    expect(result.links).toHaveLength(1);
    expect(result.links[0]).toMatchObject({ id: "l1", src: "a", dst: "group:g1" });
    expect((result.links[0] as BoardLink & { version: number }).version).toBe(3);
    expect(result.rejected.map((item) => [item.id, item.reason])).toEqual([
      ["l2", "duplicate"],
      ["self", "self"],
      ["bad", "missing-endpoint"],
      ["dead", "missing-endpoint"],
    ]);
  });

  it("没有上下文时只做语法归一化，不做存在性校验", () => {
    const result = normalizeBoardLinks([mkLink("l", "x", "group:y")]);
    expect(result.links).toHaveLength(1);
    expect(result.links[0]).toMatchObject({ src: "x", dst: "group:y" });
    expect(result.rejected).toEqual([]);
  });
});

describe("组框交互与收起概览的纯几何", () => {
  it("缩放把手固定为 右 / 下 / 右下 三个方向，位置在组框边缘", () => {
    const handles = groupResizeHandles({ x: 10, y: 20, w: 200, h: 100 });
    expect(handles.map((item) => item.edge)).toEqual([...GROUP_RESIZE_EDGES]);
    expect(handles[0]).toMatchObject({ edge: "e", x: 210, y: 70, cursor: "ew-resize" });
    expect(handles[1]).toMatchObject({ edge: "s", x: 110, y: 120, cursor: "ns-resize" });
    expect(handles[2]).toMatchObject({ edge: "se", x: 210, y: 120, cursor: "nwse-resize" });
  });

  it("收起概览用固定槽位：成员不足也保留空槽，多了如实报 overflow", () => {
    const group = mkGroup("g5", ["a", "b", "c"], { x: 0, y: 0, w: 300, h: 200 });
    const overview = groupOverviewSlots({ group, cards });
    expect(overview.inner).toEqual({
      x: GROUP_PAD_X,
      y: GROUP_HEADER_HEIGHT,
      w: 300 - GROUP_PAD_X * 2,
      h: 200 - GROUP_HEADER_HEIGHT - GROUP_PAD_X,
    });
    expect(overview.slotCount).toBe(GROUP_OVERVIEW_MAX_SLOTS);
    expect(overview.slots).toHaveLength(GROUP_OVERVIEW_MAX_SLOTS);
    expect(overview.slots.map((slot) => slot.cardId)).toEqual(["a", "b", "c", null, null]);
    expect(overview.overflow).toBe(0);
    expect(overview.slots[0]).toMatchObject({ x: GROUP_PAD_X, y: GROUP_HEADER_HEIGHT, w: GROUP_OVERVIEW_SLOT.w, h: GROUP_OVERVIEW_SLOT.h });
    // 位置只由槽位序号决定，与卡片 id 无关（固定槽位）
    expect(overview.slots[1].x).toBe(GROUP_PAD_X + GROUP_OVERVIEW_SLOT.w + GROUP_OVERVIEW_SLOT.gap);
  });

  it("成员多于槽位时报出未显示数量；有序 / 普通组的槽位完全一致（槽位不代表执行顺序）", () => {
    const many = Array.from({ length: 8 }, (_, index) => mkCard("m" + index, index * 10, 0));
    const group = mkGroup("g6", many.map((card) => card.id), { x: 0, y: 0, w: 300, h: 200 });
    const overview = groupOverviewSlots({ group, cards: many });
    expect(overview.overflow).toBe(8 - GROUP_OVERVIEW_MAX_SLOTS);
    expect(overview.slots.every((slot) => slot.cardId !== null)).toBe(true);

    const ordered = groupOverviewSlots({ group: { ...group, ordered: true }, cards: many });
    expect(ordered.slots).toEqual(overview.slots);
  });

  it("成员缺失 / 已删除时只按存活卡片填槽，不凭空造成员", () => {
    const group = mkGroup("g7", ["a", "ghost", "dead"], { x: 0, y: 0, w: 300, h: 200 });
    const overview = groupOverviewSlots({ group, cards });
    expect(overview.slots.map((slot) => slot.cardId)).toEqual(["a", null, null, null, null]);
    expect(overview.overflow).toBe(0);
  });

  it("组框太小放不下槽位时返回 0 槽并如实计入 overflow，不产生负尺寸", () => {
    const group = mkGroup("g8", ["a", "b"], { x: 0, y: 0, w: 60, h: 80 });
    const overview = groupOverviewSlots({ group, cards });
    expect(overview.slotCount).toBe(0);
    expect(overview.slots).toEqual([]);
    expect(overview.overflow).toBe(2);
    expect(overview.inner.w).toBeGreaterThanOrEqual(0);
    expect(overview.inner.h).toBeGreaterThanOrEqual(0);
  });
});

describe("公开常量与构造器", () => {
  it("端点前缀 / 构造器对组件层公开，组件不再自己拼一份前缀", () => {
    expect(GROUP_ENDPOINT_PREFIX).toBe("group:");
    expect(groupEndpoint("g1")).toMatchObject({ kind: "group", id: "g1", value: "group:g1" });
    expect(groupEndpoint("  ")).toBeNull();
  });
});
