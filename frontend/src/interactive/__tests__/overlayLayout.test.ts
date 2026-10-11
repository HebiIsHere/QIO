/**
 * 跨浮层几何的纯函数用例（子智能体 D）。
 *
 * 契约 §9.6：两面板相交面积必须为 0；较宽窗口优先并排；空间不足要明确「上下」或「二选一」；
 * 按**实际可用区域**算，不许用「窗口高度减固定值」。
 *
 * 这里只用几何数字，不挂 DOM：组件侧（IntentBatchTray）负责把量到的数字传进来。
 */
import { describe, expect, it } from "vitest";
import {
  BATCH_PREFERRED_HEIGHT,
  BATCH_PREFERRED_WIDTH,
  CHAT_PREFERRED_WIDTH,
  OVERLAY_EDGE,
  OVERLAY_GAP,
  chatHeightRelaxation,
  intersectionArea,
  overlayRects,
  planOverlayGeometry,
  type OverlayGeometry,
  type OverlayInput,
} from "../overlayLayout";

/** 设计下限：低于它就「没法读」，几何必须保证不小于它（除非整块区域都不够） */
const CHAT_MIN = { width: 320, height: 240 };
const BATCH_MIN = { width: 300, height: 220 };

function input(overrides: Partial<OverlayInput> = {}): OverlayInput {
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

/** 800×600：舞台更矮、工具栏更高（实测工具栏顶边约 423） */
function narrow(overrides: Partial<OverlayInput> = {}): OverlayInput {
  return input({
    viewport: { width: 800, height: 600 },
    stage: { top: 53, height: 547 },
    toolbarTop: 423,
    ...overrides,
  });
}

function areaOf(geometry: OverlayGeometry, base: OverlayInput): number {
  const { chat, batch } = overlayRects(base, geometry);
  return intersectionArea(chat, batch);
}

describe("宽窗口：优先并排", () => {
  it("1440×900 两面板都开 → side-by-side，各拿偏好宽度，批量列表整体推到聊天左边", () => {
    const base = input();
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("side-by-side");
    expect(geometry.chatMaxWidth).toBe(CHAT_PREFERRED_WIDTH);
    expect(geometry.batchMaxWidth).toBe(BATCH_PREFERRED_WIDTH);
    expect(geometry.chatRight).toBe(OVERLAY_EDGE);
    // 批量的右边 = 左边距 + 聊天宽 + 间距：两块的间隙正好是 gap
    expect(geometry.batchRight).toBe(OVERLAY_EDGE + CHAT_PREFERRED_WIDTH + OVERLAY_GAP);
    expect(areaOf(geometry, base)).toBe(0);

    const { chat, batch } = overlayRects(base, geometry);
    expect(batch.x + batch.width).toBe(chat.x - OVERLAY_GAP);
    // 两块都在工具栏顶边之上（不遮提交区）
    expect(chat.y + chat.height).toBeLessThanOrEqual(base.toolbarTop);
    expect(batch.y + batch.height).toBeLessThanOrEqual(base.toolbarTop);
  });

  it("800×600 两面板都开 → 仍然并排（宽度够），相交面积为 0", () => {
    const base = narrow();
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("side-by-side");
    expect(geometry.chatMaxWidth).toBe(CHAT_PREFERRED_WIDTH);
    // 剩下的宽度给批量列表：768 - 12 - 420 = 336（仍高于 300 的下限）
    expect(geometry.batchMaxWidth).toBe(336);
    expect(geometry.batchMaxWidth).toBeGreaterThanOrEqual(BATCH_MIN.width);
    expect(areaOf(geometry, base)).toBe(0);

    const { chat, batch } = overlayRects(base, geometry);
    // 上一轮实测的两块重叠（右侧同列上下压住）在这套几何下不再出现
    expect(batch.x + batch.width).toBeLessThanOrEqual(chat.x);
  });
});

describe("窄窗口：明确切换或上下，不先遮挡", () => {
  it("480×600 两面板都开 → switched（宽度与高度都不够），两块各自都不越过自己的区间", () => {
    const base = narrow({ viewport: { width: 480, height: 600 }, stage: { top: 53, height: 547 } });
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("switched");
    // 二选一：同一时间只显示一个，所以两块的最坏矩形允许重叠（真实界面里只会有一块在 DOM 中可见）；
    // 这里要证明的是**每一块都能单独完整放下**，切换过去之后不会被裁掉。
    const { chat, batch } = overlayRects(base, geometry);
    expect(chat.y).toBeGreaterThanOrEqual(base.stage.top + OVERLAY_EDGE);
    expect(chat.y + chat.height).toBeLessThanOrEqual(base.toolbarTop);
    expect(batch.y).toBeGreaterThanOrEqual(base.stage.top + OVERLAY_EDGE);
    expect(batch.y + batch.height).toBeLessThanOrEqual(base.toolbarTop);
    expect(chat.x).toBeGreaterThanOrEqual(OVERLAY_EDGE);
    expect(batch.x).toBeGreaterThanOrEqual(OVERLAY_EDGE);
  });

  it("600×900 两面板都开 → 宽度不够但高度够 → stacked，两块上限之和 + 间距 ≤ 可用高度", () => {
    const base = input({ viewport: { width: 600, height: 900 }, stage: { top: 53, height: 847 } });
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("stacked");
    const { chat, batch } = overlayRects(base, geometry);
    expect(batch.y + batch.height + OVERLAY_GAP).toBeLessThanOrEqual(chat.y);
    expect(areaOf(geometry, base)).toBe(0);
    // 上下模式两块都在同一列（右对齐），不并排
    expect(batch.x + batch.width).toBe(chat.x + chat.width);
    expect(geometry.batchRight).toBe(OVERLAY_EDGE);
  });
});

describe("最小尺寸与退化输入", () => {
  it("并排时两个面板都不小于各自的最小宽度", () => {
    for (let width = 660; width <= 1600; width += 20) {
      const base = input({ viewport: { width, height: 900 } });
      const geometry = planOverlayGeometry(base);
      if (geometry.mode !== "side-by-side") continue;
      expect(geometry.chatMaxWidth).toBeGreaterThanOrEqual(CHAT_MIN.width);
      expect(geometry.batchMaxWidth).toBeGreaterThanOrEqual(BATCH_MIN.width);
      expect(geometry.chatMaxWidth + geometry.batchMaxWidth + OVERLAY_GAP).toBeLessThanOrEqual(width - 2 * OVERLAY_EDGE);
    }
  });

  it("上下模式两个面板都不小于各自的最小高度", () => {
    const base = input({ viewport: { width: 600, height: 900 }, stage: { top: 53, height: 847 } });
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("stacked");
    expect(geometry.chatMaxHeight).toBeGreaterThanOrEqual(CHAT_MIN.height);
    expect(geometry.batchMaxHeight).toBeGreaterThanOrEqual(BATCH_MIN.height);
  });

  it("单开时不为另一个「没打开」的面板预留宽度", () => {
    const base = narrow({ chatOpen: true, batchOpen: false, viewport: { width: 480, height: 600 } });
    const geometry = planOverlayGeometry(base);
    // 480 - 32 = 448 的可用宽度全部给聊天（受它自己的 420 偏好限制），不被批量最小宽度吃掉
    expect(geometry.chatMaxWidth).toBe(CHAT_PREFERRED_WIDTH);
    const rects = overlayRects(base, geometry);
    expect(rects.chat.width).toBe(CHAT_PREFERRED_WIDTH);
  });

  it("视口/工具栏量不到（0 或 NaN）时不产生负数与 NaN", () => {
    const base = input({
      viewport: { width: 0, height: 0 },
      stage: { top: 0, height: 0 },
      toolbarTop: Number.NaN,
    });
    const geometry = planOverlayGeometry(base);
    for (const value of [
      geometry.chatMaxWidth,
      geometry.chatMaxHeight,
      geometry.chatRight,
      geometry.batchMaxWidth,
      geometry.batchMaxHeight,
      geometry.batchRight,
      geometry.gap,
    ]) {
      expect(Number.isFinite(value)).toBe(true);
      expect(value).toBeGreaterThanOrEqual(0);
    }
    expect(areaOf(geometry, base)).toBe(0);
  });

  it("工具栏顶边低于舞台下沿时被钳制在舞台里", () => {
    const base = input({ stage: { top: 53, height: 400 }, toolbarTop: 9999 });
    const geometry = planOverlayGeometry(base);
    const { chat } = overlayRects(base, geometry);
    expect(chat.y + chat.height).toBeLessThanOrEqual(53 + 400);
  });
});

describe("相交面积为 0 的边界：整片扫描", () => {
  it("视口 320–2000 宽 × 480–1200 高、工具栏顶边上下浮动，两面板相交面积恒为 0", () => {
    const widths = [320, 360, 420, 480, 560, 600, 640, 660, 700, 760, 800, 900, 1024, 1200, 1440, 1600, 2000];
    const heights = [480, 520, 560, 600, 680, 768, 800, 900, 1080, 1200];
    let checked = 0;
    const modes = new Set<string>();
    for (const width of widths) {
      for (const height of heights) {
        for (const toolbarOffset of [-260, -180, -120, -80, 0]) {
          const stageTop = Math.min(53, Math.max(0, Math.round(height * 0.06)));
          const toolbarTop = height + toolbarOffset;
          const base = input({
            viewport: { width, height },
            stage: { top: stageTop, height: Math.max(0, height - stageTop) },
            toolbarTop,
          });
          const geometry = planOverlayGeometry(base);
          modes.add(geometry.mode);
          if (geometry.mode !== "switched") {
            // 并排 / 上下都必须两块同时看得见且不相交
            expect(areaOf(geometry, base)).toBe(0);
          } else {
            // 二选一：靠「同一时间只显示一个」保证真实界面不相交，几何上只要求各自放得下
            const { chat, batch } = overlayRects(base, geometry);
            expect(chat.y + chat.height).toBeLessThanOrEqual(base.toolbarTop);
            expect(batch.y + batch.height).toBeLessThanOrEqual(base.toolbarTop);
          }
          checked += 1;
        }
      }
    }
    expect(checked).toBe(widths.length * heights.length * 5);
    // 三档模式都真的被覆盖到（否则这片扫描没有说服力）
    expect([...modes].sort()).toEqual(["side-by-side", "stacked", "switched"]);
  });

  it("上下模式的临界值：可用高度刚好等于两块下限 + 间距时仍然不相交", () => {
    // 可用高度 = chatBand；让 chatBand == 240 + 12 + 220 = 472
    const stageTop = 53;
    // 可用高度 = 聊天区间高度；要它正好等于 240 + 12 + 220 = 472
    const toolbarTop = 472 + 78 + (stageTop + OVERLAY_EDGE); // = 619
    const chatBand = toolbarTop - 78 - (stageTop + OVERLAY_EDGE);
    expect(chatBand).toBe(240 + OVERLAY_GAP + 220);
    const base = input({
      viewport: { width: 600, height: 700 },
      stage: { top: stageTop, height: 647 },
      toolbarTop,
    });
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("stacked");
    expect(geometry.chatMaxHeight).toBe(CHAT_MIN.height);
    expect(geometry.batchMaxHeight).toBe(BATCH_MIN.height);
    expect(areaOf(geometry, base)).toBe(0);
  });

  it("偏好高度小于可用高度时，批量列表不越过偏好值（聊天拿到余下的全部高度）", () => {
    const base = input({ viewport: { width: 600, height: 1200 }, stage: { top: 53, height: 1147 }, toolbarTop: 1100 });
    const geometry = planOverlayGeometry(base);
    expect(geometry.mode).toBe("stacked");
    expect(geometry.batchMaxHeight).toBe(BATCH_PREFERRED_HEIGHT);
    expect(geometry.chatMaxHeight).toBeGreaterThan(CHAT_MIN.height);
    expect(areaOf(geometry, base)).toBe(0);
  });
});

describe("反例：旧实现（窗口高度减固定值）过不了", () => {
  it("800×600 下两个面板都贴右、高度各按 60vh / 62vh 算 → 相交面积远大于 0", () => {
    const width = 800;
    const height = 600;
    const toolbarTop = 423;
    // 旧实现：聊天面板 height = min(62vh, 520)，批量面板 height = min(60vh, 520)，两块都 right: 16（宽 420 / 400）
    const chat = { x: width - 16 - 420, y: toolbarTop - 24 - 34 - 8 - Math.min(0.62 * height, 520), width: 420, height: Math.min(0.62 * height, 520) };
    const batch = { x: width - 16 - 400, y: 53 + 16 + 34 + 8, width: 400, height: Math.min(0.6 * height, 520) };
    const naive = intersectionArea(chat, batch);
    expect(naive).toBeGreaterThan(0);
    // 同一尺寸下新实现为 0 —— 这条用例就是为了证明「旧算法必须改」
    const base = narrow();
    expect(areaOf(planOverlayGeometry(base), base)).toBe(0);
  });
});

describe("聊天页脚的保守值与放宽", () => {
  it("量到 34px 的入口按钮时可以放宽（78 - 24 - 12 - 34 = 8px），量到更高的按钮时不收紧", () => {
    expect(chatHeightRelaxation(34)).toBe(8);
    expect(chatHeightRelaxation(46)).toBe(0);
    expect(chatHeightRelaxation(80)).toBe(0);
    expect(chatHeightRelaxation(Number.NaN)).toBe(0);
  });
});
