/**
 * overlayAvoid —— 浮层之间的「让开」几何（互动模式契约 2026-10-13 · IRL §7.4 / §5.4）
 *
 * 为什么需要单独一层：对话窗口现在可以被用户**自由移动与调整尺寸**，
 * 审批批量列表与切换条的旧几何里「聊天永远占右侧固定一列」这个前提不再成立。
 * 两个面板都贴右边、都可能落在下半屏，必须按**当前真实**的对话窗口矩形让开。
 *
 * 纯函数：不认识 DOM、不写状态、不做任何保存。
 */

export interface Band {
  left: number;
  right: number;
}

export interface RectLike {
  x: number;
  y: number;
  w: number;
  h: number;
}

function intersectsVertically(a: { top: number; bottom: number }, b: RectLike): boolean {
  return !(a.bottom <= b.y || a.top >= b.y + b.h);
}

/**
 * 在一条水平带里找出「避开所有障碍之后」最宽的一段可用区间。
 *
 * 返回 null 表示**真的没有位置**：障碍（含 gap）把整条带吃光了。
 * 调用方据此进入「同一时间只显示一个面板」的切换布局，而不是硬叠上去 ——
 * 任务书 §5.4「多个面板不得互相遮挡」不允许用一个重叠换另一个重叠。
 */
export function widestFreeBand(
  full: Band,
  obstacles: readonly RectLike[],
  gap: number,
  vertical?: { top: number; bottom: number },
): Band | null {
  const active = (vertical ? obstacles.filter((item) => intersectsVertically(vertical, item)) : obstacles)
    .map((item) => ({ left: item.x - gap, right: item.x + item.w + gap }))
    .filter((item) => item.right > full.left && item.left < full.right)
    .sort((a, b) => a.left - b.left);

  if (!active.length) return { left: Math.min(full.left, full.right), right: Math.max(full.left, full.right) };

  // 从最左的障碍开始扫，记录每两个障碍之间的空隙，以及两端剩余的空间。
  let cursor = full.left;
  let best: Band | null = null;
  for (const item of active) {
    if (item.left > cursor) {
      const candidate = { left: cursor, right: Math.min(item.left, full.right) };
      if (!best || candidate.right - candidate.left > best.right - best.left) best = candidate;
    }
    cursor = Math.max(cursor, item.right);
    if (cursor >= full.right) break;
  }
  if (cursor < full.right) {
    const candidate = { left: cursor, right: full.right };
    if (!best || candidate.right - candidate.left > best.right - best.left) best = candidate;
  }
  if (!best || best.right - best.left <= 0) return null;
  return best;
}
