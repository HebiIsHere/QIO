/**
 * 互动页 v2 · Phase 2-A：统一几何 / 确定性避让 / 组缩放（纯函数）。
 *
 * 契约依据：
 * - docs/interactive-page-v2-contract.md §5.2（图片比例约束统一走几何）、§5.5（A 只新增本模块）；
 * - docs/interactive-mode-contract.md §8.2（画布边界）、§11.10（拖动中实时显示：连线/组框跟随，
 *   松手前不改板面状态）、§15（三层状态：容器尺寸 / 查看缩放 / 显示态互不写回）、
 *   §17.1（正式几何唯一权威：只有板面坐标一套；视野永远不是几何上限）。
 *
 * 这里只测纯函数：不挂 DOM、不读 store、不联网。硬规则：
 * 1. 输入一个字段都不改，输出全是新对象；同样的输入永远得到同样的输出（确定性）。
 * 2. 显示矩形是**显示态**：拖动/命中/连线都读同一份 resolveDisplayFrames，不产生第二套位置。
 * 3. 避让有上限（轮数 + 总位移），到不了就如实报告 limited，绝不无限推挤。
 * 4. 组缩放：图片成员的槽位严格保持原图比例；普通成员适配但不重叠；不小于最小尺寸；
 *    缩小后贴边排布，不在组框内留下成片空白。
 */
import { describe, expect, it } from "vitest";
import {
  BOARD_VIEW_MAX_SCALE,
  BOARD_VIEW_MIN_SCALE,
  GROUP_RESIZE_MIN_FRAME,
  avoidOverlaps,
  avoidsOverlap,
  boardRectToGroupLocal,
  boardToGroupLocal,
  boardViewport,
  clampFrameToBounds,
  groupResizeFrameChanges,
  normalizeFrame,
  pointInRect,
  buildFramePlan,
  edgeAnchorToward,
  frameEdgePoint,
  groupLocalRectToBoard,
  groupLocalToBoard,
  hitTestFrames,
  planGroupResize,
  rectsOverlap,
  resolveDisplayFrames,
  toBoardPoint,
  toBoardRect,
  toScreenPoint,
  toScreenRect,
  applyFramePlan,
  type AvoidObject,
  type FrameObject,
  type V2Rect,
} from "../geometry";

function rect(x: number, y: number, w: number, h: number): V2Rect {
  return { x, y, w, h };
}

function expectRect(actual: V2Rect, expected: V2Rect, eps = 1e-6): void {
  expect(actual.x).toBeCloseTo(expected.x, 6);
  expect(actual.y).toBeCloseTo(expected.y, 6);
  expect(actual.w).toBeCloseTo(expected.w, 6);
  expect(actual.h).toBeCloseTo(expected.h, 6);
}

function card(id: string, frame: V2Rect, groupId: string | null = null): FrameObject {
  return { id, kind: "card", frame, groupId };
}

function group(id: string, frame: V2Rect): FrameObject {
  return { id, kind: "group", frame, groupId: null };
}

function avoidObj(id: string, frame: V2Rect, kind: "card" | "group" = "card", groupId: string | null = null): AvoidObject {
  return { id, kind, frame, groupId };
}

describe("坐标换算：screen ⇄ board ⇄ group-local", () => {
  it("板面视口：屏幕 = 板面 × scale + 偏移（偏移含画布原点与平移）", () => {
    const view = boardViewport({ scale: 2, surfaceLeft: 100, surfaceTop: 50, panX: 20, panY: -10 });
    expect(view.scale).toBe(2);
    expect(view.offsetX).toBe(120);
    expect(view.offsetY).toBe(40);
    expect(toScreenPoint(view, { x: 0, y: 0 })).toEqual({ x: 120, y: 40 });
    expect(toScreenPoint(view, { x: 30, y: 20 })).toEqual({ x: 180, y: 80 });
  });

  it("滚动量参与偏移：surface 起点减滚动 = 板面原点的真实屏幕位置", () => {
    const view = boardViewport({ scale: 1, surfaceLeft: 40, surfaceTop: 10, scrollLeft: 15, scrollTop: 5, panX: 3, panY: 2 });
    expect(view.offsetX).toBe(28);
    expect(view.offsetY).toBe(7);
  });

  it("往返换算稳定：先转屏幕再转回板面，逐字段回到原值", () => {
    const view = boardViewport({ scale: 1.75, surfaceLeft: -30, surfaceTop: 12, panX: 6, panY: -9 });
    for (const point of [{ x: 0, y: 0 }, { x: 37.5, y: -12.25 }, { x: 800, y: 600 }]) {
      const back = toBoardPoint(view, toScreenPoint(view, point));
      expect(back.x).toBeCloseTo(point.x, 9);
      expect(back.y).toBeCloseTo(point.y, 9);
    }
  });

  it("矩形整体换算：板面矩形 → 屏幕矩形 → 板面矩形", () => {
    const view = boardViewport({ scale: 0.5, surfaceLeft: 10, surfaceTop: 20 });
    const source = rect(100, 200, 300, 120);
    const screen = toScreenRect(view, source);
    expectRect(screen, rect(60, 120, 150, 60));
    expectRect(toBoardRect(view, screen), source, 1e-9);
  });

  it("缩放越界收敛到 [0.25, 2.5]，非法缩放按 1 处理", () => {
    expect(boardViewport({ scale: 0.01 }).scale).toBe(BOARD_VIEW_MIN_SCALE);
    expect(boardViewport({ scale: 99 }).scale).toBe(BOARD_VIEW_MAX_SCALE);
    expect(boardViewport({ scale: Number.NaN }).scale).toBe(1);
    expect(BOARD_VIEW_MIN_SCALE).toBe(0.25);
    expect(BOARD_VIEW_MAX_SCALE).toBe(2.5);
  });

  it("坏坐标不产生 NaN：缺省/非法数按 0 处理", () => {
    const view = boardViewport({});
    const point = toBoardPoint(view, { x: Number.NaN, y: Number.POSITIVE_INFINITY });
    expect(point.x).toBe(0);
    expect(point.y).toBe(0);
    expect(Number.isFinite(point.x)).toBe(true);
  });

  it("组局部坐标：相对组框左上角，往返回到板面坐标（含矩形）", () => {
    const frame = rect(100, 50, 400, 300);
    expect(boardToGroupLocal(frame, { x: 130, y: 80 })).toEqual({ x: 30, y: 30 });
    expect(groupLocalToBoard(frame, { x: 30, y: 30 })).toEqual({ x: 130, y: 80 });

    const local = rect(30, 30, 180, 96);
    expectRect(groupLocalRectToBoard(frame, local), rect(130, 80, 180, 96));
    expectRect(boardRectToGroupLocal(frame, rect(130, 80, 180, 96)), local);
  });

  it("归一化与边界：坏数得到最小合法帧，钳边界不越出画布", () => {
    expect(normalizeFrame({ x: Number.NaN, y: Number.NaN, w: 0, h: -5 })).toEqual({ x: 0, y: 0, w: 1, h: 1 });
    expectRect(clampFrameToBounds(rect(-50, -60, 200, 100), rect(0, 0, 1000, 1000)), rect(0, 0, 200, 100));
    expectRect(clampFrameToBounds(rect(900, 950, 200, 100), rect(0, 0, 1000, 1000)), rect(800, 900, 200, 100));
    expect(pointInRect({ x: 100, y: 100 }, rect(0, 0, 100, 100))).toBe(true);
    expect(pointInRect({ x: 100.5, y: 50 }, rect(0, 0, 100, 100))).toBe(false);
  });
});

describe("最终显示矩形：拖动中的临时矩形（连线与命中同源）", () => {
  const objects: FrameObject[] = [
    group("g1", rect(400, 100, 400, 300)),
    card("m1", rect(420, 120, 180, 96), "g1"),
    card("a", rect(0, 0, 200, 100)),
  ];

  it("没有拖动时：显示矩形逐字段等于正式矩形", () => {
    const { frames } = resolveDisplayFrames({ objects });
    expectRect(frames.a, rect(0, 0, 200, 100));
    expectRect(frames.g1, rect(400, 100, 400, 300));
    expectRect(frames.m1, rect(420, 120, 180, 96));
  });

  it("拖动一张自由卡：只有它按位移显示，其它对象不动", () => {
    const { frames, movingIds } = resolveDisplayFrames({
      objects,
      drag: { id: "a", deltaX: 30, deltaY: -20 },
    });
    expectRect(frames.a, rect(30, -20, 200, 100));
    expectRect(frames.g1, rect(400, 100, 400, 300));
    expectRect(frames.m1, rect(420, 120, 180, 96));
    expect(movingIds).toEqual(["a"]);
  });

  it("拖动一个组：组框与它的成员一起按同一位移显示（成员不丢相对位置）", () => {
    const { frames, movingIds } = resolveDisplayFrames({
      objects,
      drag: { id: "g1", deltaX: 10, deltaY: 15 },
    });
    expectRect(frames.g1, rect(410, 115, 400, 300));
    expectRect(frames.m1, rect(430, 135, 180, 96));
    expectRect(frames.a, rect(0, 0, 200, 100));
    expect(movingIds.slice().sort()).toEqual(["g1", "m1"]);
  });

  it("拖动有边界时：显示矩形钳进板面画布（不产生负坐标/出界）", () => {
    const bounds = rect(0, 0, 2400, 1600);
    const { frames } = resolveDisplayFrames({
      objects,
      drag: { id: "a", deltaX: -500, deltaY: -500 },
      bounds,
    });
    expectRect(frames.a, rect(0, 0, 200, 100));
  });

  it("显示矩形绝不出 NaN：非法位移按 0 处理", () => {
    const { frames } = resolveDisplayFrames({
      objects,
      drag: { id: "a", deltaX: Number.NaN, deltaY: Number.POSITIVE_INFINITY },
    });
    expect(Number.isFinite(frames.a.x)).toBe(true);
    expect(Number.isFinite(frames.a.y)).toBe(true);
  });

  it("纯度：计算显示矩形不改输入对象", () => {
    const snapshot = JSON.parse(JSON.stringify(objects));
    resolveDisplayFrames({ objects, drag: { id: "g1", deltaX: -40, deltaY: 25 } });
    expect(objects).toEqual(snapshot);
  });

  it("命中：按渲染顺序取最上面的对象（后画者优先），边界算命中", () => {
    const { frames } = resolveDisplayFrames({
      objects: [card("a", rect(0, 0, 100, 100)), card("b", rect(50, 50, 100, 100))],
    });
    expect(hitTestFrames(frames, { x: 75, y: 75 })).toBe("b");
    expect(hitTestFrames(frames, { x: 25, y: 25 })).toBe("a");
    expect(hitTestFrames(frames, { x: 0, y: 0 })).toBe("a");
    expect(hitTestFrames(frames, { x: 500, y: 500 })).toBeNull();
  });

  it("连线端点在显示边缘上：拖动后端点跟着走（拖动中线与卡片同一坐标）", () => {
    const { frames } = resolveDisplayFrames({ objects, drag: { id: "a", deltaX: 120, deltaY: 0 } });
    const anchor = edgeAnchorToward(frames.a, frames.g1);
    expect(anchor.x).toBeCloseTo(frames.a.x + frames.a.w, 6);
    expect(anchor.y).toBeCloseTo(frames.a.y + frames.a.h / 2, 6);
    expect(frameEdgePoint(frames.a, "right")).toEqual(anchor);
  });

  it("四向边缘锚点：朝向目标中心的那条边，中点即锚点（同中心时确定性落到右边）", () => {
    const from = rect(100, 100, 200, 100);
    expect(frameEdgePoint(from, "left")).toEqual({ x: 100, y: 150 });
    expect(frameEdgePoint(from, "right")).toEqual({ x: 300, y: 150 });
    expect(frameEdgePoint(from, "top")).toEqual({ x: 200, y: 100 });
    expect(frameEdgePoint(from, "bottom")).toEqual({ x: 200, y: 200 });
    expect(edgeAnchorToward(from, rect(1000, 120, 200, 100))).toEqual({ x: 300, y: 150 });
    expect(edgeAnchorToward(from, rect(120, 900, 200, 100))).toEqual({ x: 200, y: 200 });
    expect(edgeAnchorToward(from, rect(80, 80, 200, 100))).toEqual({ x: 100, y: 150 });
    expect(edgeAnchorToward(from, rect(0, 0, 0, 0))).toEqual({ x: 300, y: 150 });
    expect(edgeAnchorToward(from, from)).toEqual({ x: 300, y: 150 });
  });
});

describe("确定性最小避让", () => {
  it("不重叠：原样返回，不做任何移动", () => {
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(0, 0, 200, 100)),
      others: [avoidObj("b", rect(400, 400, 200, 100))],
    });
    expect(result.adjusted).toBe(false);
    expect(result.limited).toBe(false);
    expect(result.blockers).toEqual([]);
    expectRect(result.frame, rect(0, 0, 200, 100));
  });

  it("卡-卡：选位移最小的一侧让开，最终间距正好是 gap", () => {
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(100, 100, 200, 100)),
      others: [avoidObj("b", rect(250, 100, 200, 100))],
      gap: 16,
    });
    expect(result.adjusted).toBe(true);
    expect(result.limited).toBe(false);
    expect(result.blockers).toContain("b");
    expectRect(result.frame, rect(34, 100, 200, 100));
    expect(result.totalShift.x).toBeCloseTo(-66, 6);
    expect(avoidsOverlap(result.frame, [rect(250, 100, 200, 100)], 16)).toBe(true);
  });

  it("卡-卡：垂直重叠时同样取最小位移（向上让开 66px）", () => {
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(0, 0, 200, 100)),
      others: [avoidObj("b", rect(0, 50, 200, 100))],
      gap: 16,
    });
    expect(result.adjusted).toBe(true);
    expectRect(result.frame, rect(0, -66, 200, 100));
    expect(avoidsOverlap(result.frame, [rect(0, 50, 200, 100)], 16)).toBe(true);
  });

  it("卡-组：组框是障碍，卡片让开组框（卡-组）", () => {
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(380, 100, 200, 100), "card"),
      others: [avoidObj("g1", rect(400, 60, 400, 300), "group")],
      gap: 16,
    });
    expect(result.adjusted).toBe(true);
    expect(result.blockers).toContain("g1");
    expect(avoidsOverlap(result.frame, [rect(400, 60, 400, 300)], 16)).toBe(true);
  });

  it("组-组：只按组框互算，不做成员级二次推挤", () => {
    const result = avoidOverlaps({
      moved: avoidObj("g1", rect(0, 0, 400, 300), "group"),
      others: [
        avoidObj("g2", rect(500, 0, 400, 300), "group"),
        avoidObj("g2m", rect(520, 20, 180, 96), "card", "g2"),
      ],
      gap: 16,
    });
    expect(result.adjusted).toBe(false);
  });

  it("组移动：自己的成员不算障碍（组与自身成员不互算）", () => {
    const result = avoidOverlaps({
      moved: avoidObj("g1", rect(0, 0, 400, 300), "group"),
      others: [
        avoidObj("m1", rect(20, 20, 180, 96), "card", "g1"),
        avoidObj("m2", rect(220, 20, 180, 96), "card", "g1"),
      ],
      gap: 16,
    });
    expect(result.adjusted).toBe(false);
    expect(result.blockers).toEqual([]);
  });

  it("组内卡片移动：自己的组框与同组成员都不算障碍", () => {
    const result = avoidOverlaps({
      moved: avoidObj("m1", rect(20, 20, 180, 96), "card", "g1"),
      others: [
        avoidObj("g1", rect(0, 0, 400, 300), "group"),
        avoidObj("m2", rect(180, 40, 180, 96), "card", "g1"),
      ],
      gap: 16,
    });
    expect(result.adjusted).toBe(false);
  });

  it("被边界挡住的一侧不可用：退到另一侧而不是原地打转", () => {
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(0, 0, 200, 100)),
      others: [avoidObj("b", rect(20, 0, 200, 100))],
      gap: 16,
      bounds: rect(0, 0, 1000, 1000),
    });
    expect(result.adjusted).toBe(true);
    expectRect(result.frame, rect(0, 116, 200, 100));
  });

  it("连续障碍：一轮让不开就继续让，最终与全部障碍都留出 gap", () => {
    const others = [
      avoidObj("b", rect(200, 0, 200, 1000)),
      avoidObj("c", rect(416, 0, 200, 1000)),
    ];
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(0, 100, 200, 100)),
      others,
      gap: 16,
      bounds: rect(0, 0, 2000, 1000),
    });
    expect(result.adjusted).toBe(true);
    expect(result.limited).toBe(false);
    expect(result.rounds).toBeGreaterThanOrEqual(1);
    for (const obstacle of others) {
      expect(avoidsOverlap(result.frame, [obstacle.frame], 16)).toBe(true);
    }
  });

  it("有上限：位移超过 maxShift 就停手并如实报告 limited（不无限推挤）", () => {
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(0, 0, 200, 100)),
      others: [avoidObj("b", rect(20, 0, 200, 1000))],
      gap: 16,
      maxShift: 50,
      bounds: rect(0, 0, 2000, 1000),
    });
    expect(result.limited).toBe(true);
    expect(result.adjusted).toBe(false);
    expect(Math.abs(result.totalShift.x)).toBeLessThanOrEqual(50);
  });

  it("有上限：轮数用完就停手（同一输入下轮数是确定的）", () => {
    const others = [
      avoidObj("b", rect(200, 0, 200, 1000)),
      avoidObj("c", rect(416, 0, 200, 1000)),
      avoidObj("d", rect(632, 0, 200, 1000)),
    ];
    const result = avoidOverlaps({
      moved: avoidObj("a", rect(0, 100, 200, 100)),
      others,
      gap: 16,
      maxRounds: 1,
      bounds: rect(0, 0, 2000, 1000),
    });
    expect(result.limited).toBe(true);
    expect(result.rounds).toBeLessThanOrEqual(1);
  });

  it("确定性：同一输入重复调用得到逐字段相同的结果", () => {
    const input = {
      moved: avoidObj("a", rect(100, 100, 200, 100)),
      others: [
        avoidObj("b", rect(250, 100, 200, 100)),
        avoidObj("c", rect(120, 260, 200, 100)),
      ],
      gap: 16,
    };
    const first = avoidOverlaps(input);
    const second = avoidOverlaps(input);
    expect(second).toEqual(first);
  });

  it("坏数据不产生 NaN", () => {
    const result = avoidOverlaps({
      moved: avoidObj("a", { x: Number.NaN, y: Number.NaN, w: Number.NaN, h: Number.NaN }),
      others: [avoidObj("b", rect(0, 0, 200, 100))],
      gap: Number.NaN,
    });
    expect(Number.isFinite(result.frame.x)).toBe(true);
    expect(Number.isFinite(result.frame.y)).toBe(true);
    expect(result.frame.w).toBeGreaterThan(0);
    expect(result.frame.h).toBeGreaterThan(0);
  });
});

describe("组缩放：槽位 / 比例 / 最小尺寸 / 不留空白", () => {
  const twoCards = [
    { id: "c1", frame: rect(16, 16, 180, 96) },
    { id: "c2", frame: rect(212, 16, 180, 96) },
  ];

  it("放大：成员槽位按比例放大并填满组框（贴边、不重叠）", () => {
    const result = planGroupResize({
      frame: rect(0, 0, 408, 150),
      nextFrame: rect(0, 0, 800, 600),
      members: twoCards,
      gap: 16,
      padding: 16,
    });
    expect(result.frame.w).toBeCloseTo(800, 6);
    expect(result.frame.h).toBeCloseTo(600, 6);
    expect(result.issues).toEqual([]);
    const first = result.slots.c1;
    const second = result.slots.c2;
    expect(first.x).toBeCloseTo(16, 6);
    expect(second.x + second.w).toBeCloseTo(800 - 16, 6);
    expect(rectsOverlap(first, second)).toBe(false);
    expect(first.w).toBeGreaterThanOrEqual(180);
    expect(first.h).toBeGreaterThanOrEqual(96);
    expect(result.order).toEqual(["c1", "c2"]);
  });

  it("缩小：不低于成员最小尺寸，组框被最小尺寸撑住并如实报告 min_clamped", () => {
    const result = planGroupResize({
      frame: rect(0, 0, 408, 150),
      nextFrame: rect(0, 0, 200, 150),
      members: twoCards,
      gap: 16,
      padding: 16,
    });
    expect(result.issues).toContain("min_clamped");
    expect(result.frame.w).toBeCloseTo(408, 6);
    expect(result.frame.h).toBeGreaterThanOrEqual(96 + 32);
    for (const id of ["c1", "c2"]) {
      expect(result.slots[id].w).toBeGreaterThanOrEqual(180 - 1e-6);
      expect(result.slots[id].h).toBeGreaterThanOrEqual(96 - 1e-6);
    }
    expect(rectsOverlap(result.slots.c1, result.slots.c2)).toBe(false);
  });

  it("图片成员严格保持原图比例（放大、非等比、缩小三种情况）", () => {
    const ratio = 16 / 9;
    const member = [{ id: "img", frame: rect(16, 16, 320, 180), aspectRatio: ratio }];
    const base = { frame: rect(0, 0, 352, 212), members: member, gap: 16, padding: 16 } as const;

    for (const next of [rect(0, 0, 704, 424), rect(0, 0, 528, 212), rect(0, 0, 300, 300)]) {
      const result = planGroupResize({ ...base, nextFrame: next });
      const slot = result.slots.img;
      expect(slot.w / slot.h).toBeCloseTo(ratio, 6);
      expect(slot.w).toBeGreaterThanOrEqual(180 - 1e-6);
      expect(slot.h).toBeGreaterThanOrEqual(96 - 1e-6);
      expect(slot.x).toBeGreaterThanOrEqual(16 - 1e-6);
      expect(slot.y).toBeGreaterThanOrEqual(16 - 1e-6);
      expect(slot.x + slot.w).toBeLessThanOrEqual(result.frame.w - 16 + 1e-6);
      expect(slot.y + slot.h).toBeLessThanOrEqual(result.frame.h - 16 + 1e-6);
    }
  });

  it("图片比例未知 / 非法：按普通成员处理，不写 NaN 也不猜比例", () => {
    for (const aspectRatio of [null, undefined, 0, -2, Number.NaN, Number.POSITIVE_INFINITY]) {
      const result = planGroupResize({
        frame: rect(0, 0, 400, 300),
        nextFrame: rect(0, 0, 400, 300),
        members: [
          { id: "m", frame: rect(16, 16, 200, 100), aspectRatio },
          { id: "n", frame: rect(16, 130, 200, 100) },
        ],
        gap: 16,
        padding: 16,
      });
      for (const slot of Object.values(result.slots)) {
        expect(Number.isFinite(slot.x)).toBe(true);
        expect(Number.isFinite(slot.y)).toBe(true);
        expect(Number.isFinite(slot.w)).toBe(true);
        expect(Number.isFinite(slot.h)).toBe(true);
        expect(slot.w).toBeGreaterThan(0);
        expect(slot.h).toBeGreaterThan(0);
      }
    }
  });

  it("两行成员：保持行结构，顺序稳定，缩小后四边贴齐（不在组框里留下成片空白）", () => {
    const members = [
      { id: "a", frame: rect(16, 16, 180, 96) },
      { id: "b", frame: rect(212, 16, 180, 96) },
      { id: "c", frame: rect(16, 128, 180, 96) },
    ];
    const result = planGroupResize({
      frame: rect(0, 0, 408, 240),
      nextFrame: rect(0, 0, 260, 200),
      members,
      gap: 16,
      padding: 16,
    });
    expect(result.order).toEqual(["a", "b", "c"]);
    expect(result.slots.a.x).toBeCloseTo(16, 6);
    expect(result.slots.c.y + result.slots.c.h).toBeCloseTo(result.frame.h - 16, 6);
    expect(result.slots.b.x + result.slots.b.w).toBeCloseTo(result.frame.w - 16, 6);
    expect(rectsOverlap(result.slots.a, result.slots.b)).toBe(false);
    expect(rectsOverlap(result.slots.a, result.slots.c)).toBe(false);
    expect(rectsOverlap(result.slots.b, result.slots.c)).toBe(false);
  });

  it("非等比缩小时普通成员适配但不重叠", () => {
    const result = planGroupResize({
      frame: rect(0, 0, 600, 300),
      nextFrame: rect(0, 0, 900, 180),
      members: [
        { id: "a", frame: rect(16, 16, 260, 96) },
        { id: "b", frame: rect(300, 16, 260, 96) },
      ],
      gap: 16,
      padding: 16,
    });
    expect(rectsOverlap(result.slots.a, result.slots.b)).toBe(false);
    expect(result.slots.a.x).toBeCloseTo(16, 6);
    expect(result.slots.b.x + result.slots.b.w).toBeCloseTo(result.frame.w - 16, 6);
  });

  it("单成员组：缩放不越界，尺寸仍受最小/最大约束", () => {
    const result = planGroupResize({
      frame: rect(0, 0, 260, 150),
      nextFrame: rect(0, 0, 2000, 1600),
      members: [{ id: "only", frame: rect(16, 16, 230, 120) }],
      gap: 16,
      padding: 16,
    });
    expect(result.slots.only.x).toBeCloseTo(16, 6);
    expect(result.slots.only.x + result.slots.only.w).toBeLessThanOrEqual(result.frame.w - 16 + 1e-6);
    expect(result.slots.only.w).toBeLessThanOrEqual(1600 + 1e-6);
    expect(result.slots.only.h).toBeLessThanOrEqual(1200 + 1e-6);
  });

  it("坏输入：组框 0 宽 / 成员为空 / 非有限数都不抛错", () => {
    const empty = planGroupResize({
      frame: rect(0, 0, 0, 0),
      nextFrame: rect(0, 0, Number.NaN, 200),
      members: [],
      padding: 16,
      gap: 16,
    });
    expect(empty.order).toEqual([]);
    expect(empty.slots).toEqual({});
    expect(Number.isFinite(empty.frame.w)).toBe(true);
    expect(empty.frame.w).toBeGreaterThan(0);
    expect(GROUP_RESIZE_MIN_FRAME.w).toBeGreaterThan(0);
  });

  it("目标组框是坏数据时如实报 frame_normalized（不抛错、不产生 NaN）", () => {
    const result = planGroupResize({
      frame: rect(0, 0, 408, 150),
      nextFrame: rect(0, 0, Number.NaN, 0),
      members: [{ id: "a", frame: rect(16, 16, 180, 96) }],
      padding: 16,
      gap: 16,
    });
    expect(result.issues).toContain("frame_normalized");
    expect(Number.isFinite(result.frame.w)).toBe(true);
    expect(result.frame.w).toBeGreaterThan(0);
  });

  it("缩放结果可以摊成一份原子清单（组框 + 全部成员一起提交）", () => {
    const result = planGroupResize({
      frame: rect(0, 0, 408, 150),
      nextFrame: rect(0, 0, 800, 600),
      members: twoCards,
      gap: 16,
      padding: 16,
    });
    const changes = groupResizeFrameChanges("g1", result);
    expect(changes.map((change) => change.id)).toEqual(["g1", "c1", "c2"]);
    expect(changes[0].kind).toBe("group");
    expect(changes[1].kind).toBe("card");
    const plan = buildFramePlan({ changes });
    expect(plan.ok).toBe(true);
  });

  it("纯度与确定性：不改输入、重复调用结果一致", () => {
    const input = {
      frame: rect(0, 0, 408, 240),
      nextFrame: rect(0, 0, 640, 360),
      members: [
        { id: "a", frame: rect(16, 16, 180, 96) },
        { id: "b", frame: rect(212, 16, 180, 96) },
      ],
      gap: 16,
      padding: 16,
    };
    const snapshot = JSON.parse(JSON.stringify(input));
    const first = planGroupResize(input);
    const second = planGroupResize(input);
    expect(input).toEqual(snapshot);
    expect(second).toEqual(first);
  });
});

describe("原子变更计划", () => {
  it("合法输入：一次给出全部变更与逐 id 帧索引", () => {
    const result = buildFramePlan({
      changes: [
        { id: "a", kind: "card", frame: rect(10, 20, 200, 100) },
        { id: "g1", kind: "group", frame: rect(0, 0, 400, 300) },
      ],
      currentFrames: { a: rect(10, 20, 200, 100), g1: rect(0, 0, 400, 300) },
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.plan.changes).toHaveLength(2);
    expectRect(result.plan.frameOf.a, rect(10, 20, 200, 100));
  });

  it("任一项非法则整份计划失败（不产生半份变更）", () => {
    const result = buildFramePlan({
      changes: [
        { id: "a", kind: "card", frame: rect(10, 20, 200, 100) },
        { id: "bad", kind: "card", frame: rect(Number.NaN, 0, 200, 100) },
      ],
    });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues.some((issue) => issue.code === "invalid_frame")).toBe(true);
    expect(result.issues[0].id).toBe("bad");
  });

  it("重复 id / 低于最小尺寸 / 超出边界都拒绝（原子性）", () => {
    const duplicate = buildFramePlan({
      changes: [
        { id: "a", kind: "card", frame: rect(0, 0, 200, 100) },
        { id: "a", kind: "card", frame: rect(0, 0, 200, 100) },
      ],
    });
    expect(duplicate.ok).toBe(false);

    const tooSmall = buildFramePlan({
      changes: [{ id: "a", kind: "card", frame: rect(0, 0, 10, 10) }],
    });
    expect(tooSmall.ok).toBe(false);

    const outOfBounds = buildFramePlan({
      changes: [{ id: "a", kind: "card", frame: rect(2300, 0, 200, 100) }],
      bounds: rect(0, 0, 2400, 1600),
    });
    expect(outOfBounds.ok).toBe(false);
  });

  it("应用计划：返回新数组、只改计划内的项、不改输入", () => {
    const items = [
      { id: "a", frame: rect(0, 0, 200, 100), title: "A" },
      { id: "b", frame: rect(400, 0, 200, 100), title: "B" },
    ];
    const planResult = buildFramePlan({
      changes: [{ id: "a", kind: "card", frame: rect(50, 60, 200, 100) }],
    });
    expect(planResult.ok).toBe(true);
    if (!planResult.ok) return;

    const snapshot = JSON.parse(JSON.stringify(items));
    const next = applyFramePlan(items, planResult.plan);
    expect(items).toEqual(snapshot);
    expect(next).not.toBe(items);
    expectRect(next[0].frame, rect(50, 60, 200, 100));
    expect(next[1].frame).toEqual(items[1].frame);
    expect(next[0].title).toBe("A");
  });
});
