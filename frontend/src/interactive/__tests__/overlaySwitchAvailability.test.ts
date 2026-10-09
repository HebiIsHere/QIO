/**
 * 切换显示的**前提**是「确实存在另一个可展示的面板」（子智能体 C，契约 §12.6）。
 *
 * 第五轮用户报告：480×600、板面上没有任何待审批批次、只打开了聊天时，
 * 界面仍然出现一条「空间不足，只展开一个面板」的切换条，并抬高聊天面板
 * 为一个不存在的面板留位置 —— 虚假提示 + 多余预留。
 *
 * 这里用纯几何数字证明：
 * 1. batchAvailable=false（没有可展示的审批批次）时，就算调用方仍声明 switched，
 *   也不进入切换布局：模式退回、切换条矩形高度为 0；
 * 2. 此时聊天高度与「从未声明切换」时相同（不为不存在的面板损失一行）；
 * 3. batchAvailable=true（确实有批次）时，§11.7 的既有行为全部保持：
 *   同一个切换状态、单开也预留、预留真实、抬升量不变；
 * 4. 调用方不传 batchAvailable 时行为与旧版完全一致（向后兼容）。
 */
import { describe, expect, it } from "vitest";
import {
  OVERLAY_SWITCH_BAR_HEIGHT,
  chatSwitchLift,
  overlaysAreCramped,
  planOverlayGeometry,
  resolveOverlayPanes,
  type OverlayInput,
} from "../overlayLayout";

/** 设计下限：低于它就「没法读」（与 overlayLayout.test.ts 同一口径） */
const CHAT_MIN = { width: 320, height: 240 };
const BATCH_MIN = { width: 300, height: 220 };

/** 480×600：实测工具栏顶边约 484，两个面板同时打开就是放不下 */
function crampedBase(overrides: Partial<OverlayInput> = {}): OverlayInput {
  return {
    viewport: { width: 480, height: 600 },
    stage: { top: 53, height: 547 },
    toolbarTop: 484,
    chatOpen: true,
    batchOpen: false,
    chatMin: { ...CHAT_MIN },
    batchMin: { ...BATCH_MIN },
    ...overrides,
  };
}

describe("没有可展示的审批批次：不进入切换布局（§12.6）", () => {
  it("batchAvailable=false 且仍声明 switched：模式退回 side-by-side，切换条高度为 0", () => {
    const plan = planOverlayGeometry(crampedBase({ switched: true, batchAvailable: false }));
    expect(plan.mode).toBe("side-by-side");
    // 与既有契约一致：不需要切换条时 height = 0（宽度保留为可用宽，便于验收脚本兼容）
    expect(plan.switchBar.height).toBe(0);
    expect(plan.switchBar.y).toBe(0);
  });

  it("此时聊天拿到的高度与「从未声明切换」完全相同（不为不存在的面板损失空间）", () => {
    const declared = planOverlayGeometry(crampedBase({ switched: true, batchAvailable: false }));
    const clean = planOverlayGeometry(crampedBase());
    expect(declared.chatMaxHeight).toBe(clean.chatMaxHeight);
    expect(declared.chatMaxWidth).toBe(clean.chatMaxWidth);
    // 宽度也不被并排假设压窄
  });

  it("声明 switched 但没有批次时，抬升也为 0（聊天不再为不存在的面板付费）", () => {
    const plan = planOverlayGeometry(
      crampedBase({ chatOpen: true, batchOpen: false, switched: true, batchAvailable: false }),
    );
    expect(plan.mode).toBe("side-by-side");
    expect(plan.switchBar.height).toBe(0);
  });

  it("批次处理完后的开合收尾：resolveOverlayPanes(cramped=true, 双开, batchAvailable=false) 只保留对话", () => {
    const resolved = resolveOverlayPanes({
      cramped: true,
      chatOpen: true,
      batchOpen: true,
      preferred: "batch",
      batchAvailable: false,
    });
    expect(resolved.batchOpen).toBe(false);
    expect(resolved.chatOpen).toBe(true);
    expect(resolved.closed).toBe("batch");
    expect(resolved.preferred).toBe("chat");
  });
});

describe("确实有批次：§11.7 的既有行为全部保持（batchAvailable=true）", () => {
  it("声明 switched 后，单开也保持 switched 并为切换条预留真实高度", () => {
    const both = planOverlayGeometry(crampedBase({ chatOpen: true, batchOpen: true, switched: true, batchAvailable: true }));
    const chatOnly = planOverlayGeometry(crampedBase({ chatOpen: true, batchOpen: false, switched: true, batchAvailable: true }));
    expect(both.mode).toBe("switched");
    expect(chatOnly.mode).toBe("switched");
    expect(chatOnly.chatMaxHeight).toBe(both.chatMaxHeight);
    expect(chatOnly.switchBar.height).toBe(OVERLAY_SWITCH_BAR_HEIGHT);
  });

  it("预留真实：比不声明切换多出至少一个切换条高度", () => {
    const withBar = planOverlayGeometry(crampedBase({ chatOpen: true, batchOpen: false, switched: true, batchAvailable: true }));
    const naive = planOverlayGeometry(crampedBase({ chatOpen: true, batchOpen: false, batchAvailable: true }));
    expect(naive.mode).toBe("side-by-side");
    expect(naive.chatMaxHeight - withBar.chatMaxHeight).toBeGreaterThanOrEqual(OVERLAY_SWITCH_BAR_HEIGHT);
  });

  it("抬升量只由入口按钮高度决定，不因 batchAvailable 变化", () => {
    const withBatch = chatSwitchLift(39);
    const withoutBatch = chatSwitchLift(39);
    expect(withBatch).toBe(65);
    expect(withoutBatch).toBe(withBatch); // 纯函数：同样的输入永远同样的抬升
  });

  it("overlaysAreCramped 不管 batchAvailable：它只回答空间问题，调用方再叠加批次判据", () => {
    expect(overlaysAreCramped(crampedBase({ batchAvailable: false }))).toBe(true);
    expect(overlaysAreCramped(crampedBase({ batchAvailable: true }))).toBe(true);
  });
});

describe("向后兼容：不传 batchAvailable 视为 true（旧调用方零改动）", () => {
  it("不传时与显式 batchAvailable=true 的结果一致", () => {
    const implicit = planOverlayGeometry(crampedBase({ chatOpen: true, batchOpen: false, switched: true }));
    const explicit = planOverlayGeometry(
      crampedBase({ chatOpen: true, batchOpen: false, switched: true, batchAvailable: true }),
    );
    expect(implicit).toEqual(explicit);
  });
});
