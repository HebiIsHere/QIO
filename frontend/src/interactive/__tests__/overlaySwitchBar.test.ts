/**
 * 面板切换条的高度预留与定位（子智能体 C 负责，契约 §11.7 / §11.8）。
 *
 * 上一轮的缺陷：竖直区间只在「两个面板都开着」时按切换显示预留切换条那一行；
 * 用户关掉其中一个之后，单开分支按**不预留**的区间算高度，切换条就压在面板底边上。
 *
 * 这里用纯几何数字证明三件事：
 * 1. 只要处于「需要切换显示」的状态，无论当前开着几个面板，竖直区间都按**同一个状态**算；
 * 2. 预留是**真实**的（不预留时会多出至少一个切换条的高度），不是 0 或象征值；
 * 3. 切换条有自己的矩形：在视口内、在工具栏上方、与面板（及其间距）都不相交。
 */
import { describe, expect, it } from "vitest";
import {
  CHAT_DOCK_TOGGLE_GAP,
  OVERLAY_CHAT_DOCK_LIFT,
  OVERLAY_EDGE,
  OVERLAY_GAP,
  OVERLAY_SWITCH_BAR_HEIGHT,
  chatSwitchLift,
  intersectionArea,
  overlaysAreCramped,
  overlayRects,
  planOverlayGeometry,
  type OverlayInput,
} from "../overlayLayout";

/** 设计下限：低于它就「没法读」（与 overlayLayout.test.ts 同一口径） */
const CHAT_MIN = { width: 320, height: 240 };
const BATCH_MIN = { width: 300, height: 220 };

/** 480×600：上一轮实测工具栏顶边约 423，两个面板都放不下 */
function crampedBase(overrides: Partial<OverlayInput> = {}): OverlayInput {
  return {
    viewport: { width: 480, height: 600 },
    stage: { top: 53, height: 547 },
    toolbarTop: 423,
    chatOpen: true,
    batchOpen: true,
    chatMin: { ...CHAT_MIN },
    batchMin: { ...BATCH_MIN },
    ...overrides,
  };
}

/** 1440×900：空间充足 */
function wideBase(overrides: Partial<OverlayInput> = {}): OverlayInput {
  return {
    viewport: { width: 1440, height: 900 },
    stage: { top: 53, height: 847 },
    toolbarTop: 774,
    chatOpen: true,
    batchOpen: true,
    chatMin: { ...CHAT_MIN },
    batchMin: { ...BATCH_MIN },
    ...overrides,
  };
}

describe("切换显示状态：同一个布局状态算可用空间", () => {
  it("关掉一个面板后仍为切换条预留真实高度（与两面板都开时完全相同）", () => {
    const both = planOverlayGeometry(crampedBase());
    expect(both.mode).toBe("switched");

    const chatOnlyInput = crampedBase({ chatOpen: true, batchOpen: false, switched: true });
    const batchOnlyInput = crampedBase({ chatOpen: false, batchOpen: true, switched: true });
    const chatOnly = planOverlayGeometry(chatOnlyInput);
    const batchOnly = planOverlayGeometry(batchOnlyInput);

    // 同一个明确布局状态：只开一个也仍然是 switched，不是退回 side-by-side
    expect(chatOnly.mode).toBe("switched");
    expect(batchOnly.mode).toBe("switched");
    // 竖直区间与两面板都开时一致（不因开合变化而漂移）
    expect(chatOnly.chatMaxHeight).toBe(both.chatMaxHeight);
    expect(batchOnly.batchMaxHeight).toBe(both.batchMaxHeight);
    // 面板与切换条各自一块，高度不互相偷
    expect(chatOnly.batchMaxHeight).toBe(both.batchMaxHeight);
    expect(batchOnly.chatMaxHeight).toBe(both.chatMaxHeight);
  });

  it("预留是真实的：不声明切换条时会多出至少一个切换条的高度（反例证明它不是象征值）", () => {
    const withBar = planOverlayGeometry(crampedBase({ chatOpen: true, batchOpen: false, switched: true }));
    const naive = planOverlayGeometry(crampedBase({ chatOpen: true, batchOpen: false }));
    expect(naive.mode).toBe("side-by-side");
    expect(naive.chatMaxHeight - withBar.chatMaxHeight).toBeGreaterThanOrEqual(OVERLAY_SWITCH_BAR_HEIGHT);
  });

  it("切换条矩形不随当前开着哪个面板变化（同一个状态 → 同一行）", () => {
    const both = planOverlayGeometry(crampedBase()).switchBar;
    const chatOnly = planOverlayGeometry(crampedBase({ batchOpen: false, switched: true })).switchBar;
    const batchOnly = planOverlayGeometry(crampedBase({ chatOpen: false, switched: true })).switchBar;
    expect(chatOnly).toEqual(both);
    expect(batchOnly).toEqual(both);
    expect(both.height).toBe(OVERLAY_SWITCH_BAR_HEIGHT);
    expect(both.height).toBeGreaterThan(0);
  });

  it("overlaysAreCramped 只看空间，不看调用方声明的切换状态（不会自我实现）", () => {
    expect(overlaysAreCramped(crampedBase())).toBe(true);
    expect(overlaysAreCramped(crampedBase({ switched: true }))).toBe(true);
    expect(overlaysAreCramped(wideBase())).toBe(false);
    expect(overlaysAreCramped(wideBase({ switched: true }))).toBe(false);
  });
});

describe("聊天面板的抬升：底边由 ChatDock 自己锚定，必须真的抬到切换条上方", () => {
  it("抬升量 = 预留底边与面板真实底边之差（按入口按钮高度算）", () => {
    // 真实底边 = 工具栏顶边 - 24 - 8 - 入口按钮高度；预留底边 = 工具栏顶边 - 78 - 34 - 2*间距
    expect(chatSwitchLift(34)).toBe(70);
    expect(chatSwitchLift(39)).toBe(65); // 480×600 实机实测的入口按钮高度
  });

  it("量不到入口按钮高度时取最大值：宁可多抬几像素，也不许压住切换条", () => {
    expect(chatSwitchLift(Number.NaN)).toBe(104);
    expect(chatSwitchLift(0)).toBe(104);
    expect(chatSwitchLift(200)).toBe(0); // 入口按钮比预留还高时不倒扣
  });

  it("抬升后的面板底边正好停在切换条上方一个间距，面板顶边仍在可用区内", () => {
    const base = crampedBase({ chatOpen: true, batchOpen: false, switched: true });
    const geometry = planOverlayGeometry(base);
    const toggleHeight = 39;
    const realBottom = base.toolbarTop - OVERLAY_CHAT_DOCK_LIFT - CHAT_DOCK_TOGGLE_GAP - toggleHeight;
    const liftedBottom = realBottom - chatSwitchLift(toggleHeight);
    expect(geometry.switchBar.y - liftedBottom).toBe(OVERLAY_GAP);
    expect(liftedBottom - geometry.chatMaxHeight).toBeGreaterThanOrEqual(base.stage.top + OVERLAY_EDGE);
  });
});

describe("切换条与面板、工具栏、视口的几何关系", () => {
  const cases: Array<[string, OverlayInput]> = [
    ["两个面板都开", crampedBase()],
    ["只开对话", crampedBase({ batchOpen: false, switched: true })],
    ["只开审批列表", crampedBase({ chatOpen: false, switched: true })],
  ];

  it("每一块面板都与切换条不相交，并保持一个间距", () => {
    for (const [name, base] of cases) {
      const geometry = planOverlayGeometry(base);
      const rects = overlayRects(base, geometry);
      const bar = geometry.switchBar;
      expect(bar.height, name).toBe(OVERLAY_SWITCH_BAR_HEIGHT);
      expect(intersectionArea(rects.chat, bar), name + " 对话面板压到切换条").toBe(0);
      expect(intersectionArea(rects.batch, bar), name + " 审批列表压到切换条").toBe(0);
      expect(bar.y - (rects.chat.y + rects.chat.height), name + " 对话面板与切换条的间距").toBeGreaterThanOrEqual(OVERLAY_GAP);
      expect(bar.y - (rects.batch.y + rects.batch.height), name + " 审批列表与切换条的间距").toBeGreaterThanOrEqual(OVERLAY_GAP);
    }
  });

  it("切换条在视口内、在工具栏上方，并且不越出可用宽度", () => {
    for (const [name, base] of cases) {
      const bar = planOverlayGeometry(base).switchBar;
      expect(bar.x, name).toBeGreaterThanOrEqual(OVERLAY_EDGE);
      expect(bar.x + bar.width, name).toBeLessThanOrEqual(base.viewport.width - OVERLAY_EDGE);
      expect(bar.y, name).toBeGreaterThanOrEqual(base.stage.top + OVERLAY_EDGE);
      expect(bar.y + bar.height, name).toBeLessThanOrEqual(base.toolbarTop - OVERLAY_EDGE);
    }
  });

  it("单开批量列表时不盖住右下角的聊天入口按钮（两块面板贴同一列）", () => {
    const base = crampedBase({ viewport: { width: 800, height: 600 }, stage: { top: 53, height: 547 }, toolbarTop: 423, chatOpen: false, batchOpen: true });
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("side-by-side");
    const { batch } = overlayRects(base, geometry);
    // 聊天入口按钮在工具栏顶边往上 58px 起、高 39px（实机实测）：面板底边必须停在它上面
    expect(batch.y + batch.height).toBeLessThanOrEqual(base.toolbarTop - 58 - 39);
    expect(batch.x + batch.width).toBe(base.viewport.width - OVERLAY_EDGE);
  });

  it("宽窗口（并排）不需要切换条：矩形高度为 0，也不占竖直区间", () => {
    const base = wideBase();
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("side-by-side");
    expect(geometry.switchBar.height).toBe(0);
    const rects = overlayRects(base, geometry);
    // 并排时两块面板仍然不相交（回归：预留逻辑不能破坏原有结论）
    expect(intersectionArea(rects.chat, rects.batch)).toBe(0);
  });

  it("量不到尺寸（0 / NaN）时不产生负数或 NaN 的切换条矩形", () => {
    const base = crampedBase({
      viewport: { width: 0, height: 0 },
      stage: { top: 0, height: 0 },
      toolbarTop: Number.NaN,
      switched: true,
    });
    const bar = planOverlayGeometry(base).switchBar;
    for (const value of [bar.x, bar.y, bar.width, bar.height]) {
      expect(Number.isFinite(value)).toBe(true);
      expect(value).toBeGreaterThanOrEqual(0);
    }
  });

  it("整片扫描：需要切换显示时切换条始终落在面板与工具栏之间", () => {
    let checked = 0;
    for (const width of [360, 420, 480, 560, 640, 800]) {
      for (const height of [480, 560, 600, 720, 900]) {
        for (const toolbarOffset of [-120, -80, 0]) {
          const stageTop = Math.min(53, Math.max(0, Math.round(height * 0.06)));
          const base: OverlayInput = {
            viewport: { width, height },
            stage: { top: stageTop, height: Math.max(0, height - stageTop) },
            toolbarTop: height + toolbarOffset,
            chatOpen: true,
            batchOpen: false,
            switched: true,
            chatMin: { ...CHAT_MIN },
            batchMin: { ...BATCH_MIN },
          };
          const geometry = planOverlayGeometry(base);
          const rects = overlayRects(base, geometry);
          const bar = geometry.switchBar;
          expect(bar.y + bar.height).toBeLessThanOrEqual(Math.max(base.stage.top, base.toolbarTop));
          expect(intersectionArea(rects.chat, bar)).toBe(0);
          expect(bar.y).toBeGreaterThanOrEqual(base.stage.top);
          checked += 1;
        }
      }
    }
    expect(checked).toBe(6 * 5 * 3);
  });
});
