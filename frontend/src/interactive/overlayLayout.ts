/**
 * 跨浮层的可用区域与并排策略（子智能体 D 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §9.6。
 *
 * 为什么要有这一层：底部工具栏、右下聊天、右上批量列表、卡片局部工具栏与中央确认框
 * 过去各自用「窗口高度减一个固定值」算位置，窄窗口里必然互相遮挡（上一轮实测两面板相交约 45080px²）。
 * 这里统一按**实际可用区域**算出每个浮层的尺寸上限与位置偏移，并把「并排 / 上下 / 二选一」的模式显式化，
 * 组件只消费结果，不各自猜。
 *
 * 纯函数、无副作用、可单测；不读 DOM，由调用方把量到的数字传进来。
 */

export interface OverlayInput {
  /** 视口尺寸（CSS 像素） */
  viewport: { width: number; height: number };
  /** 板面舞台相对视口的位置与高度（浮层的定位父级） */
  stage: { top: number; height: number };
  /** 底部工具栏顶边（相对视口），浮层不许越过它 */
  toolbarTop: number;
  chatOpen: boolean;
  batchOpen: boolean;
  /** 两个面板各自的最小可用尺寸（低于它就没法读） */
  chatMin: { width: number; height: number };
  batchMin: { width: number; height: number };
}

export interface OverlayGeometry {
  /** side-by-side：宽窗口并排；stacked：上下叠放；switched：空间不足，同一时间只显示一个 */
  mode: "side-by-side" | "stacked" | "switched";
  chatMaxWidth: number;
  chatMaxHeight: number;
  chatRight: number;
  batchMaxWidth: number;
  batchMaxHeight: number;
  batchRight: number;
  /** 两个浮层之间的间距 */
  gap: number;
}

export function planOverlayGeometry(input: OverlayInput): OverlayGeometry {
  const gap = 12;
  const available = Math.max(120, Math.round(input.toolbarTop - input.stage.top - gap));
  return {
    mode: "stacked",
    chatMaxWidth: input.chatMin.width,
    chatMaxHeight: Math.max(input.chatMin.height, available),
    chatRight: gap,
    batchMaxWidth: input.batchMin.width,
    batchMaxHeight: Math.max(input.batchMin.height, available),
    batchRight: gap,
    gap,
  };
}
