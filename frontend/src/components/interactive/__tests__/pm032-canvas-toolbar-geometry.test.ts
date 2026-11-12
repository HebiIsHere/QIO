/**
 * #04 卡片工具栏几何（PM-QIO032）：按实际可用矩形换边 / 内收。
 *
 * 验收口径（BRIEF #04）：靠左、右、上边以及平移缩放后，编辑按钮完整可点；
 * 工具栏避开标题、编辑区和连接点。这里直接钉纯函数的落点：
 * - 可用矩形量不到 → 原行为逐字段不变（后端 / 测试环境不因「没有布局」改变布局）；
 * - 贴上边 → 换到下方；贴左 / 右 / 下方 → 沿边滑动或换边，所有按钮仍完整落在可用矩形内；
 * - 按钮外框与卡片边缘留 TOOLBAR_BUTTON_GAP（大于连接点半径），不压连接点。
 */
import { describe, expect, it } from "vitest";
import {
  TOOLBAR_BUTTON_COUNT,
  TOOLBAR_BUTTON_GAP,
  TOOLBAR_BUTTON_SIZE,
  TOOLBAR_BUTTON_STEP,
  toolbarButtonLayout,
  type ToolbarButtonBox,
} from "../../../interactive/overlayPlacement";

const VISIBLE = { x: 0, y: 0, w: 1200, h: 800 };

function card(x: number, y: number, w: number, h: number) {
  return { x, y, w, h };
}

/** 按钮的屏幕（板面）外框。 */
function screenBox(box: ToolbarButtonBox, c: { x: number; y: number }) {
  return { left: c.x + box.x, top: c.y + box.y, right: c.x + box.x + box.w, bottom: c.y + box.y + box.h };
}

function expectInsideVisible(boxes: ToolbarButtonBox[], c: { x: number; y: number }, visible = VISIBLE): void {
  for (const box of boxes) {
    const rect = screenBox(box, c);
    expect(rect.left, "按钮左缘在可用矩形外").toBeGreaterThanOrEqual(visible.x);
    expect(rect.top, "按钮上缘在可用矩形外").toBeGreaterThanOrEqual(visible.y);
    expect(rect.right, "按钮右缘在可用矩形外").toBeLessThanOrEqual(visible.x + visible.w);
    expect(rect.bottom, "按钮下缘在可用矩形外").toBeLessThanOrEqual(visible.y + visible.h);
  }
}

describe("#04 工具栏按钮：可用矩形量不到时保持既有口径", () => {
  it("null / 零尺寸可用矩形：回退既有分列（窄卡前两钮贴左、其余贴上）", () => {
    const c = card(400, 300, 260, 170);
    const boxes = toolbarButtonLayout({ card: c, visible: null });
    expect(boxes).toHaveLength(TOOLBAR_BUTTON_COUNT);
    // 既有口径：w=260 → 上边容量 3，前 2 个按钮改贴左边
    expect(boxes[0]).toMatchObject({ x: -78, y: 8, side: "left" });
    expect(boxes[1]).toMatchObject({ x: -42, y: 8, side: "left" });
    expect(boxes[2]).toMatchObject({ x: 8, y: -42, side: "above" });
    expect(boxes[4]).toMatchObject({ x: 80, y: -42, side: "above" });

    const zero = toolbarButtonLayout({ card: c, visible: { x: 0, y: 0, w: 0, h: 0 } });
    expect(zero).toEqual(boxes);
  });
});

describe("#04 工具栏按钮：贴边换边 / 内收", () => {
  it("卡片在板面中部：正常贴上边，5 个按钮全部完整落在可用矩形里", () => {
    const c = card(400, 300, 900, 170);
    const boxes = toolbarButtonLayout({ card: c, visible: VISIBLE });
    expect(boxes.every((box) => box.side === "above")).toBe(true);
    expect(boxes[0].y).toBe(-TOOLBAR_BUTTON_SIZE - TOOLBAR_BUTTON_GAP);
    expectInsideVisible(boxes, c);
  });

  it("卡片贴上边：工具栏换到卡片下方，编辑按钮不再被裁掉", () => {
    const c = card(400, 0, 260, 170);
    const boxes = toolbarButtonLayout({ card: c, visible: VISIBLE });
    expect(boxes.some((box) => box.side === "below")).toBe(true);
    const below = boxes.filter((box) => box.side === "below");
    expect(below.length).toBeGreaterThan(0);
    expect(below[0].y).toBe(c.h + TOOLBAR_BUTTON_GAP);
    expectInsideVisible(boxes, c);
  });

  it("卡片贴左边且很宽：整排按钮沿边向内滑动，全部留在可用矩形内", () => {
    const c = card(-200, 300, 1100, 170);
    const boxes = toolbarButtonLayout({ card: c, visible: VISIBLE });
    expectInsideVisible(boxes, c);
    // 内收之后按钮仍在卡片上方（不因贴左边就改成别的边）
    expect(boxes.every((box) => box.side === "above")).toBe(true);
    expect(screenBox(boxes[0], c).left).toBe(VISIBLE.x + 4);
  });

  it("卡片贴右边：按钮整排向左内收，不越出右缘", () => {
    const c = card(1000, 300, 240, 170);
    const boxes = toolbarButtonLayout({ card: c, visible: VISIBLE });
    expectInsideVisible(boxes, c);
  });

  it("放大到卡片比可视区还大：按钮向内调整到可视区内，编辑按钮仍可点", () => {
    const visible = { x: 0, y: 0, w: 300, h: 200 };
    const c = card(-200, -150, 1600, 1200);
    const boxes = toolbarButtonLayout({ card: c, visible });
    expect(boxes).toHaveLength(TOOLBAR_BUTTON_COUNT);
    expect(boxes.every((box) => box.side === "inside")).toBe(true);
    expectInsideVisible(boxes, c, visible);
  });

  it("卡片外侧的按钮与卡片边缘留出 TOOLBAR_BUTTON_GAP（大于连接点半径，不压连接点）", () => {
    const c = card(400, 300, 260, 170);
    const boxes = toolbarButtonLayout({ card: c, visible: VISIBLE });
    const cardRight = c.x + c.w;
    const cardBottom = c.y + c.h;
    for (const box of boxes) {
      const rect = screenBox(box, c);
      if (box.side === "above") expect(c.y - rect.bottom, "贴上边时压住连接点 / 卡片").toBe(TOOLBAR_BUTTON_GAP);
      else if (box.side === "below") expect(rect.top - cardBottom).toBe(TOOLBAR_BUTTON_GAP);
      else if (box.side === "left") expect(c.x - rect.right).toBe(TOOLBAR_BUTTON_GAP);
      else if (box.side === "right") expect(rect.left - cardRight).toBe(TOOLBAR_BUTTON_GAP);
      else expect(box.side).toBe("inside");
    }
  });
});

describe("#04 工具栏按钮：向内调整时避开连接点", () => {
  it("可视区里正好有连接点时，按钮整体让开它", () => {
    const visible = { x: 0, y: 0, w: 300, h: 300 };
    const c = card(0, 0, 300, 300);
    // 卡片本地坐标里的连接点（左上角附近），正好在向内调整的首个落点附近
    const boxes = toolbarButtonLayout({ card: c, visible, ports: [{ x: 30, y: 20, r: 8 }] });
    for (const box of boxes) {
      if (box.side !== "inside") continue;
      const nearestX = Math.max(box.x, Math.min(30, box.x + box.w));
      const nearestY = Math.max(box.y, Math.min(20, box.y + box.h));
      const distance = Math.hypot(30 - nearestX, 20 - nearestY);
      expect(distance, "向内调整的按钮压住了连接点").toBeGreaterThanOrEqual(8);
    }
  });
});