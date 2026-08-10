import type { FloatingEntry } from "./floatingState";

export interface Avoidance {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

const GAP = 12;

/**
 * 计算消息流需要为浮动输入框让出的四条边增量（px）。
 * 贴靠边时精确避让；未贴靠（拖动中/自由态）按最近边估算，避免拖动瞬间遮挡消息。
 */
export function computeAvoidance(
  entry: Pick<FloatingEntry, "dockedTo" | "x" | "y" | "width" | "height">,
  viewport: { width: number; height: number },
  gap = GAP,
): Avoidance {
  const none: Avoidance = { top: 0, right: 0, bottom: 0, left: 0 };
  if (!entry.width || !entry.height) return none;
  switch (entry.dockedTo) {
    case "bottom":
      return { ...none, bottom: entry.height + gap };
    case "right":
      return { ...none, right: entry.width + gap };
    case "left":
      return { ...none, left: entry.width + gap };
    case "top":
      return { ...none, top: entry.height + gap };
    default:
      break;
  }
  const cx = entry.x + entry.width / 2;
  const cy = entry.y + entry.height / 2;
  const dTop = cy;
  const dBottom = viewport.height - cy;
  const dLeft = cx;
  const dRight = viewport.width - cx;
  const nearest = Math.min(dTop, dBottom, dLeft, dRight);
  if (nearest === dBottom) return { ...none, bottom: entry.height + gap };
  if (nearest === dRight) return { ...none, right: entry.width + gap };
  if (nearest === dLeft) return { ...none, left: entry.width + gap };
  return { ...none, top: entry.height + gap };
}
