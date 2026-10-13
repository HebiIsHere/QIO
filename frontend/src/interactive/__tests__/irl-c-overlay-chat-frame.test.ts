/**
 * IRL-C：overlayLayout 的 chatFrame 可选入参（任务书 §7.2）。
 *
 * 三态是硬约束：
 * - 不传 → 与既有行为**逐字段一致**（既有测试一条都不用改）；
 * - null（对话窗口收起 / 不存在）→ 右侧那一列还给批量列表；
 * - 传真实矩形 → 批量列表按它避让，绝不与它相交。
 *
 * 注：Lead 后续说 IRL-D 最终改用 planDockStrip 避让，这条入参可以不做；
 * 因为已经实现且缺省逐字节不变，这里保留并钉住它的语义。
 */
import { describe, expect, it } from "vitest";
import {
  BATCH_PREFERRED_WIDTH,
  OVERLAY_EDGE,
  intersectionArea,
  overlayRects,
  planOverlayGeometry,
  type OverlayInput,
} from "../overlayLayout";

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

function narrow(overrides: Partial<OverlayInput> = {}): OverlayInput {
  return input({
    viewport: { width: 800, height: 600 },
    stage: { top: 53, height: 547 },
    toolbarTop: 423,
    ...overrides,
  });
}

describe("chatFrame 三态", () => {
  it("不传 / undefined：与既有行为逐字段一致", () => {
    const base = input();
    expect(planOverlayGeometry({ ...base, chatFrame: undefined })).toEqual(planOverlayGeometry(base));
    const cramped = narrow({ switched: true });
    expect(planOverlayGeometry({ ...cramped, chatFrame: undefined })).toEqual(planOverlayGeometry(cramped));
  });

  it("null（对话窗口收起）：右侧整列还给批量列表", () => {
    const base = input({ chatOpen: false, batchOpen: true });
    const geometry = planOverlayGeometry({ ...base, chatFrame: null });
    expect(geometry.batchRight).toBe(OVERLAY_EDGE);
    expect(geometry.batchMaxWidth).toBe(BATCH_PREFERRED_WIDTH);
    const { batch } = overlayRects({ ...base, chatFrame: null }, geometry);
    // batchRight 是「离视口右边的距离」：批量列表重新贴到右边（不再为对话窗口留列）
    expect(batch.x + batch.width).toBe(base.viewport.width - OVERLAY_EDGE);
  });

  it("传矩形但本来就不相交：几何一个字段都不动（用户把窗口拖到别处时列表不该跳）", () => {
    const base = input({ chatOpen: false, batchOpen: true });
    const plain = planOverlayGeometry(base);
    const far = planOverlayGeometry({ ...base, chatFrame: { x: 0, y: 400, width: 120, height: 120 } });
    expect(far).toEqual(plain);
  });

  it("传矩形且压在批量列表上：批量列表整体挪到它左边，两者不相交", () => {
    const base = input({ chatOpen: false, batchOpen: true });
    const { batch } = overlayRects(base, planOverlayGeometry(base));
    const chat = { x: batch.x, y: batch.y, width: batch.width, height: batch.height };
    const geometry = planOverlayGeometry({ ...base, chatFrame: chat });
    const after = overlayRects({ ...base, chatFrame: chat }, geometry).batch;
    expect(intersectionArea(after, chat)).toBe(0);
    expect(after.x + after.width).toBeLessThanOrEqual(chat.x);
  });

  it("两边都放不下：如实变窄（不假装放得下）", () => {
    const base = narrow({ chatOpen: false, batchOpen: true });
    const { batch } = overlayRects(base, planOverlayGeometry(base));
    // 对话窗口从左下一直铺到批量列表下方：左边没有宽度、上边没有高度
    const chat = { x: 0, y: 100, width: 400, height: 400 };
    const geometry = planOverlayGeometry({ ...base, chatFrame: chat });
    const after = overlayRects({ ...base, chatFrame: chat }, geometry).batch;
    expect(intersectionArea(after, chat)).toBe(0);
    expect(after.height).toBeLessThan(batch.height);
    expect(after.height).toBeGreaterThanOrEqual(0);
  });

  it("无效矩形（零尺寸 / NaN）按「没有量到」处理：保持既有几何", () => {
    const base = input({ chatOpen: false, batchOpen: true });
    const plain = planOverlayGeometry(base);
    expect(planOverlayGeometry({ ...base, chatFrame: { x: Number.NaN, y: 0, width: 0, height: 0 } })).toEqual(plain);
  });
});
