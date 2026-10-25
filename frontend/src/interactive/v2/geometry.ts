/**
 * 互动页 v2 · 统一几何（Phase 2-A，纯函数）。
 *
 * 契约依据：
 * - docs/interactive-page-v2-contract.md §5.2（图片比例约束统一走几何）、§5.5（A 只新增本模块）；
 * - docs/interactive-mode-contract.md §8.2（画布边界）、§11.10（拖动中的实时显示）、
 *   §15（三层状态彼此独立）、§17.1（正式几何是唯一权威）。
 *
 * 边界（与 interactive/geometry.ts、interactive/resize.ts 同一条）：
 * - 只做算术：不读 DOM、不读 store、不排队保存、不调用模型、不产生表达；
 * - 输入里的对象一个字段都不改，返回的矩形 / 帧索引永远是新的普通对象；
 * - 同样的输入永远得到同样的输出（确定性，便于测试与复现）。
 *
 * 本模块提供四组能力（Lead 接线时按组整体使用，不要各取一半）：
 * 1. 坐标：screen / board / group-local 三套坐标的换算（与 viewport.ts 同一物理含义）；
 * 2. 显示帧：拖动中的临时矩形（显示态）—— 连线端点、命中测试都读同一份，绝不产生第二套位置；
 * 3. 避让：卡-卡 / 卡-组 / 组-组 的确定性最小位移；组与自身成员不互算障碍；有轮数与位移上限；
 * 4. 组缩放：组框缩放后成员的槽位（图片严格保持原图比例、普通成员适配不重叠、不低于最小尺寸、
 *    缩小后贴边排布不留成片空白）与一次原子提交的变更计划。
 */
import { CARD_MAX_SIZE, CARD_MIN_SIZE, DEFAULT_PLACEMENT_GAP } from "../geometry";
import { MAX_SCALE, MIN_SCALE } from "../viewport";

// ---------------------------------------------------------------------------
// 基础类型与常量
// ---------------------------------------------------------------------------

export interface V2Point {
  x: number;
  y: number;
}

export interface V2Size {
  w: number;
  h: number;
}

/** 板面坐标矩形（与 board.cards 同一坐标系）。 */
export interface V2Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type FrameKind = "card" | "group";

/** 板面视口的缩放上下限：与 interactive/viewport.ts 同一份（这里只是转出，避免第二套数字）。 */
export const BOARD_VIEW_MIN_SCALE = MIN_SCALE;
export const BOARD_VIEW_MAX_SCALE = MAX_SCALE;

/** 组缩放缺省的成员间距 / 组框内边距（与卡片避让间距同源）。 */
export const GROUP_RESIZE_GAP = DEFAULT_PLACEMENT_GAP;
export const GROUP_RESIZE_PADDING = 16;

/** 组框的最小尺寸：一个最小卡片 + 两侧内边距。 */
export const GROUP_RESIZE_MIN_FRAME: V2Size = {
  w: CARD_MIN_SIZE.w + GROUP_RESIZE_PADDING * 2,
  h: CARD_MIN_SIZE.h + GROUP_RESIZE_PADDING * 2,
};

/** 帧的最小边长：任何几何对象都不允许 0 / 负尺寸（坏数据也不许产生空帧）。 */
const MIN_FRAME_SIZE = 1;

function finite(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  if (max < min) return min;
  return Math.max(min, Math.min(max, value));
}

/** 归一化矩形：非法数按 0 / 最小边长处理，宽高不为负也不为 0。 */
export function normalizeFrame(rect?: Partial<V2Rect> | null): V2Rect {
  return {
    x: finite(rect?.x, 0),
    y: finite(rect?.y, 0),
    w: Math.max(MIN_FRAME_SIZE, finite(rect?.w, MIN_FRAME_SIZE)),
    h: Math.max(MIN_FRAME_SIZE, finite(rect?.h, MIN_FRAME_SIZE)),
  };
}

function normalizeSize(size: Partial<V2Size> | undefined | null, fallback: V2Size): V2Size {
  return {
    w: Math.max(MIN_FRAME_SIZE, finite(size?.w, fallback.w)),
    h: Math.max(MIN_FRAME_SIZE, finite(size?.h, fallback.h)),
  };
}

function sameFrame(a: V2Rect, b: V2Rect, epsilon = 1e-6): boolean {
  return (
    Math.abs(a.x - b.x) <= epsilon &&
    Math.abs(a.y - b.y) <= epsilon &&
    Math.abs(a.w - b.w) <= epsilon &&
    Math.abs(a.h - b.h) <= epsilon
  );
}

function translateFrame(frame: V2Rect, dx: number, dy: number): V2Rect {
  return { x: frame.x + finite(dx, 0), y: frame.y + finite(dy, 0), w: frame.w, h: frame.h };
}

/** 两个矩形是否有**面积**重叠（只碰到边不算）。 */
export function rectsOverlap(a: V2Rect, b: V2Rect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** 两个矩形在 gap 间距下是否仍然贴得太近（正好留出 gap 时算「已经让开」）。 */
function overlapsWithGap(a: V2Rect, b: V2Rect, gap: number): boolean {
  const g = Math.max(0, finite(gap, 0));
  return (
    a.x < b.x + b.w + g &&
    b.x < a.x + a.w + g &&
    a.y < b.y + b.h + g &&
    b.y < a.y + a.h + g
  );
}

/** 与全部障碍都至少留出 gap（只碰到边的「正好 gap」算让开）。 */
export function avoidsOverlap(frame: V2Rect, obstacles: readonly V2Rect[], gap = 0): boolean {
  const self = normalizeFrame(frame);
  return !obstacles.some((obstacle) => overlapsWithGap(self, normalizeFrame(obstacle), gap));
}

/** 把矩形钳进边界；边界比矩形还小时至少保证左上角在边界内。 */
export function clampFrameToBounds(frame: V2Rect, bounds: V2Rect): V2Rect {
  const box = normalizeFrame(bounds);
  const self = normalizeFrame(frame);
  const maxX = box.x + Math.max(0, box.w - self.w);
  const maxY = box.y + Math.max(0, box.h - self.h);
  return {
    x: clamp(self.x, box.x, maxX),
    y: clamp(self.y, box.y, maxY),
    w: self.w,
    h: self.h,
  };
}

// ---------------------------------------------------------------------------
// 1. 坐标换算：screen ⇄ board ⇄ group-local
// ---------------------------------------------------------------------------

/**
 * 板面渲染参数：屏幕 = 板面 × scale + offset。
 *
 * offset 已经包含**画布内容原点**在屏幕上的位置（surfaceRect 左上角）、以及平移 / 滚动量 ——
 * 与 viewport.ts 的 effectiveRect + Viewport{x,y} 是同一件事，只是合成一次，调用方不再自己减。
 */
export interface BoardViewport {
  scale: number;
  offsetX: number;
  offsetY: number;
}

export interface BoardViewportInput {
  scale?: number;
  /** 画布内容容器的 boundingClientRect().left / top */
  surfaceLeft?: number;
  surfaceTop?: number;
  /** 容器的滚动量（scrollLeft / scrollTop） */
  scrollLeft?: number;
  scrollTop?: number;
  /** translate 形式的平移量 */
  panX?: number;
  panY?: number;
}

/** 由「容器在屏幕上的位置 + 滚动 + 平移 + 缩放」合成一个板面视口。 */
export function boardViewport(input: BoardViewportInput = {}): BoardViewport {
  const rawScale = typeof input.scale === "number" ? input.scale : 1;
  const scale = Number.isFinite(rawScale) ? clamp(rawScale, BOARD_VIEW_MIN_SCALE, BOARD_VIEW_MAX_SCALE) : 1;
  return {
    scale,
    offsetX: finite(input.surfaceLeft, 0) - finite(input.scrollLeft, 0) + finite(input.panX, 0),
    offsetY: finite(input.surfaceTop, 0) - finite(input.scrollTop, 0) + finite(input.panY, 0),
  };
}

function scaleOf(view: BoardViewport | null | undefined): number {
  const raw = typeof view?.scale === "number" ? view.scale : 1;
  return Number.isFinite(raw) ? clamp(raw, BOARD_VIEW_MIN_SCALE, BOARD_VIEW_MAX_SCALE) : 1;
}

/** 板面坐标 → 屏幕坐标。 */
export function toScreenPoint(view: BoardViewport, point: V2Point): V2Point {
  const scale = scaleOf(view);
  return {
    x: finite(point?.x, 0) * scale + finite(view?.offsetX, 0),
    y: finite(point?.y, 0) * scale + finite(view?.offsetY, 0),
  };
}

/** 屏幕坐标 → 板面坐标（toScreenPoint 的逆运算）。 */
export function toBoardPoint(view: BoardViewport, point: V2Point): V2Point {
  const scale = scaleOf(view);
  return {
    x: (finite(point?.x, 0) - finite(view?.offsetX, 0)) / scale,
    y: (finite(point?.y, 0) - finite(view?.offsetY, 0)) / scale,
  };
}

/** 板面矩形 → 屏幕矩形。 */
export function toScreenRect(view: BoardViewport, rect: V2Rect): V2Rect {
  const scale = scaleOf(view);
  const origin = toScreenPoint(view, { x: finite(rect?.x, 0), y: finite(rect?.y, 0) });
  return {
    x: origin.x,
    y: origin.y,
    w: Math.max(0, finite(rect?.w, 0)) * scale,
    h: Math.max(0, finite(rect?.h, 0)) * scale,
  };
}

/** 屏幕矩形 → 板面矩形。 */
export function toBoardRect(view: BoardViewport, rect: V2Rect): V2Rect {
  const scale = scaleOf(view);
  const origin = toBoardPoint(view, { x: finite(rect?.x, 0), y: finite(rect?.y, 0) });
  return {
    x: origin.x,
    y: origin.y,
    w: Math.max(0, finite(rect?.w, 0)) / scale,
    h: Math.max(0, finite(rect?.h, 0)) / scale,
  };
}

/** 板面点 → 组局部点（相对组框左上角）。 */
export function boardToGroupLocal(frame: V2Rect, point: V2Point): V2Point {
  const group = normalizeFrame(frame);
  return { x: finite(point?.x, 0) - group.x, y: finite(point?.y, 0) - group.y };
}

/** 组局部点 → 板面点。 */
export function groupLocalToBoard(frame: V2Rect, point: V2Point): V2Point {
  const group = normalizeFrame(frame);
  return { x: group.x + finite(point?.x, 0), y: group.y + finite(point?.y, 0) };
}

/** 板面矩形 → 组局部矩形。 */
export function boardRectToGroupLocal(frame: V2Rect, rect: V2Rect): V2Rect {
  const group = normalizeFrame(frame);
  return {
    x: finite(rect?.x, 0) - group.x,
    y: finite(rect?.y, 0) - group.y,
    w: Math.max(0, finite(rect?.w, 0)),
    h: Math.max(0, finite(rect?.h, 0)),
  };
}

/** 组局部矩形 → 板面矩形（与 boardRectToGroupLocal 互逆）。 */
export function groupLocalRectToBoard(frame: V2Rect, rect: V2Rect): V2Rect {
  const group = normalizeFrame(frame);
  return {
    x: group.x + finite(rect?.x, 0),
    y: group.y + finite(rect?.y, 0),
    w: Math.max(0, finite(rect?.w, 0)),
    h: Math.max(0, finite(rect?.h, 0)),
  };
}

// ---------------------------------------------------------------------------
// 2. 显示帧（拖动中的临时矩形；连线 / 命中同源）
// ---------------------------------------------------------------------------

/** 一个参与渲染的几何对象（卡片或组）。卡片用 groupId 指向所属组；组自身 groupId 为 null。 */
export interface FrameObject {
  id: string;
  kind: FrameKind;
  frame: V2Rect;
  groupId?: string | null;
}

export interface DragState {
  /** 当前被拖动的对象 id（卡片或组） */
  id: string;
  deltaX: number;
  deltaY: number;
}

export interface DisplayFramesInput {
  objects: readonly FrameObject[];
  drag?: DragState | null;
  /** 板面画布边界（0..BOARD_SURFACE_W/H）；给了就钳住被拖动对象，绝不让它跑到不可见处 */
  bounds?: V2Rect | null;
}

export interface DisplayFramesResult {
  /** 每个对象这一帧画在哪（板面坐标，新的普通对象） */
  frames: Record<string, V2Rect>;
  /** 这次跟着动的对象 id（按字典序，确定性）：拖动组时包含它的成员 */
  movingIds: string[];
}

/**
 * 计算这一帧的显示矩形（显示态，绝不写回板面状态）。
 *
 * 拖动卡片：只有它按位移显示；拖动组：组框与它的成员按同一位移显示（成员不丢相对位置）。
 * 给了 bounds 时，被拖动的主对象先被钳进画布，随动成员跟随钳制后的实际位移。
 */
export function resolveDisplayFrames(input: DisplayFramesInput): DisplayFramesResult {
  const objects = (input?.objects ?? []).filter((item) => item && typeof item.id === "string" && item.id);
  const byId = new Map<string, FrameObject>();
  for (const item of objects) {
    if (!byId.has(item.id)) byId.set(item.id, item);
  }

  const frames: Record<string, V2Rect> = {};
  for (const item of objects) {
    if (!(item.id in frames)) frames[item.id] = normalizeFrame(item.frame);
  }

  const dragged = input?.drag ? byId.get(input.drag.id) : undefined;
  if (!dragged || !input?.drag) return { frames, movingIds: [] };

  const observed = { x: frames[dragged.id].x, y: frames[dragged.id].y };
  const desired = translateFrame(
    frames[dragged.id],
    finite(input.drag.deltaX, 0),
    finite(input.drag.deltaY, 0),
  );
  const bounded = input.bounds ? clampFrameToBounds(desired, input.bounds) : desired;
  const appliedDx = bounded.x - observed.x;
  const appliedDy = bounded.y - observed.y;

  const moving = new Set<string>([dragged.id]);
  if (dragged.kind === "group") {
    for (const item of objects) {
      if (item.groupId === dragged.id) moving.add(item.id);
    }
  }

  for (const id of moving) {
    frames[id] = translateFrame(frames[id], appliedDx, appliedDy);
  }
  return { frames, movingIds: [...moving].sort() };
}

/** 点是否在矩形内（含边界）。 */
export function pointInRect(point: V2Point, rect: V2Rect): boolean {
  const self = normalizeFrame(rect);
  return (
    finite(point?.x, 0) >= self.x &&
    finite(point?.x, 0) <= self.x + self.w &&
    finite(point?.y, 0) >= self.y &&
    finite(point?.y, 0) <= self.y + self.h
  );
}

/**
 * 命中测试：按渲染顺序取最上面的对象（order 里靠后的先被看到）。
 *
 * 连线端点与指针命中都必须走这一份显示帧 —— 两者读同一个函数，才不会出现
 * 「线在旧位置、点击命中新位置」。
 */
export function hitTestFrames(
  frames: Record<string, V2Rect>,
  point: V2Point,
  order?: readonly string[],
): string | null {
  const ids = order ?? Object.keys(frames ?? {});
  for (let index = ids.length - 1; index >= 0; index -= 1) {
    const id = ids[index];
    const frame = frames?.[id];
    if (frame && pointInRect(point, frame)) return id;
  }
  return null;
}

export type EdgeSide = "left" | "right" | "top" | "bottom";

/** 矩形某条边的中点（连线端点落在**显示边缘**上，不是卡片中心）。 */
export function frameEdgePoint(frame: V2Rect, side: EdgeSide): V2Point {
  const self = normalizeFrame(frame);
  switch (side) {
    case "left":
      return { x: self.x, y: self.y + self.h / 2 };
    case "top":
      return { x: self.x + self.w / 2, y: self.y };
    case "bottom":
      return { x: self.x + self.w / 2, y: self.y + self.h };
    case "right":
    default:
      return { x: self.x + self.w, y: self.y + self.h / 2 };
  }
}

/**
 * 连线锚点：在 from 的显示边缘上，朝 toward 的中心方向取那条边的中点。
 *
 * 目标退化（宽高为 0）或与自身同心时确定性落到右边 —— 不产生 NaN，也不在两轴之间抖动。
 */
export function edgeAnchorToward(from: V2Rect, toward: V2Rect): V2Point {
  const self = normalizeFrame(from);
  const target = toward ?? { x: 0, y: 0, w: 0, h: 0 };
  const targetW = finite(target.w, 0);
  const targetH = finite(target.h, 0);
  if (targetW <= 0 || targetH <= 0) return frameEdgePoint(self, "right");

  const dx = finite(target.x, 0) + targetW / 2 - (self.x + self.w / 2);
  const dy = finite(target.y, 0) + targetH / 2 - (self.y + self.h / 2);
  if (dx === 0 && dy === 0) return frameEdgePoint(self, "right");
  if (Math.abs(dx) >= Math.abs(dy)) return frameEdgePoint(self, dx > 0 ? "right" : "left");
  return frameEdgePoint(self, dy > 0 ? "bottom" : "top");
}

// ---------------------------------------------------------------------------
// 3. 确定性最小避让（卡-卡 / 卡-组 / 组-组）
// ---------------------------------------------------------------------------

export interface AvoidObject extends FrameObject {}

export interface AvoidInput {
  moved: AvoidObject;
  others: readonly AvoidObject[];
  /** 与障碍保持的间距，默认 DEFAULT_PLACEMENT_GAP（16） */
  gap?: number;
  /** 最多让几轮，默认 8；到达上限如实报告 limited */
  maxRounds?: number;
  /** 总位移上限（板面单位），默认 2000；超过就停手，绝不无限推挤 */
  maxShift?: number;
  /** 板面画布边界；给了就不把对象推到画布外 */
  bounds?: V2Rect | null;
}

export interface AvoidResult {
  /** 让开后的矩形（新对象；没让开时等于归一化后的输入） */
  frame: V2Rect;
  adjusted: boolean;
  /** 实际让开的轮数（= 0 表示一开始就没有障碍） */
  rounds: number;
  totalShift: V2Point;
  /** true = 到达轮数 / 位移上限仍未完全让开（如实报告，不假装让开） */
  limited: boolean;
  /** 这次参与阻挡的障碍 id（按首次命中顺序，确定性） */
  blockers: string[];
}

/** 组与自身成员不互算障碍：移动组时跳过自己的成员，移动组内卡片时跳过自己的组与同组成员。 */
function isSelfOwned(moved: AvoidObject, other: AvoidObject): boolean {
  if (!other || other.id === moved.id) return true;
  if (moved.kind === "group") {
    // 自己的成员随组一起动；其他组的成员由各自的组框代表（不做成员级二次推挤）
    return Boolean(other.groupId);
  }
  if (moved.kind === "card" && moved.groupId) {
    if (other.id === moved.groupId) return true; // 自己的组框
    if (other.groupId === moved.groupId) return true; // 同组成员
  }
  return false;
}

/**
 * 确定性最小避让：每次只挑「让开这个障碍的最小位移」，多轮迭代直到不再重叠。
 *
 * - 最小位移：四个方向里取让开当前障碍所需位移最小的一侧（正好留出 gap）；
 * - 确定性：候选按固定顺序生成，冲突时按「剩余碰撞少 → 位移小 → 生成顺序」选；
 * - 不无限推挤：轮数（maxRounds）与总位移（maxShift）都有上限，到不了就 limited；
 * - 组语义：组框与自身成员不互算障碍（isSelfOwned）。
 */
export function avoidOverlaps(input: AvoidInput): AvoidResult {
  const original = normalizeFrame(input?.moved?.frame);
  let frame: V2Rect = { ...original };
  const gap = Math.max(0, finite(input?.gap, DEFAULT_PLACEMENT_GAP));
  const maxRounds = Math.max(1, Math.round(finite(input?.maxRounds, 8)));
  const maxShift = Math.max(0, finite(input?.maxShift, 2000));
  const bounds = input?.bounds ? normalizeFrame(input.bounds) : null;

  const moved: AvoidObject = {
    id: typeof input?.moved?.id === "string" ? input.moved.id : "",
    kind: input?.moved?.kind === "group" ? "group" : "card",
    frame: original,
    groupId: input?.moved?.groupId ?? null,
  };
  const obstacles = (input?.others ?? [])
    .filter((item): item is AvoidObject => Boolean(item) && typeof item?.id === "string" && item.id.length > 0)
    .filter((item) => !isSelfOwned(moved, item))
    .map((item) => ({
      id: item.id,
      kind: item.kind === "group" ? ("group" as const) : ("card" as const),
      frame: normalizeFrame(item.frame),
      groupId: item.groupId ?? null,
    }));

  const blockers: string[] = [];
  const totalShift: V2Point = { x: 0, y: 0 };
  let rounds = 0;
  let limited = false;

  for (let round = 0; round < maxRounds; round += 1) {
    const hits = obstacles.filter((item) => overlapsWithGap(frame, item.frame, gap));
    if (!hits.length) break;
    for (const hit of hits) {
      if (!blockers.includes(hit.id)) blockers.push(hit.id);
    }

    type Candidate = { dx: number; dy: number; next: V2Rect; hitsAfter: number; shift: number };
    const candidates: Candidate[] = [];
    for (const hit of hits) {
      const options: { dx: number; dy: number }[] = [
        { dx: hit.frame.x + hit.frame.w + gap - frame.x, dy: 0 },
        { dx: 0, dy: hit.frame.y + hit.frame.h + gap - frame.y },
        { dx: hit.frame.x - gap - (frame.x + frame.w), dy: 0 },
        { dx: 0, dy: hit.frame.y - gap - (frame.y + frame.h) },
      ];
      for (const option of options) {
        if (!Number.isFinite(option.dx) || !Number.isFinite(option.dy)) continue;
        if (option.dx === 0 && option.dy === 0) continue;
        let next = translateFrame(frame, option.dx, option.dy);
        if (bounds) next = clampFrameToBounds(next, bounds);
        if (sameFrame(next, frame)) continue;
        const hitsAfter = obstacles.filter((item) => overlapsWithGap(next, item.frame, gap)).length;
        candidates.push({
          dx: next.x - frame.x,
          dy: next.y - frame.y,
          next,
          hitsAfter,
          shift: Math.abs(next.x - frame.x) + Math.abs(next.y - frame.y),
        });
      }
    }
    if (!candidates.length) {
      limited = true;
      break;
    }

    let best = candidates[0];
    for (const candidate of candidates.slice(1)) {
      if (candidate.hitsAfter !== best.hitsAfter) {
        if (candidate.hitsAfter < best.hitsAfter) best = candidate;
        continue;
      }
      if (candidate.shift < best.shift - 1e-9) best = candidate;
    }

    const shifted = Math.abs(totalShift.x + best.dx) + Math.abs(totalShift.y + best.dy);
    if (shifted > maxShift + 1e-9) {
      limited = true;
      break;
    }

    frame = best.next;
    totalShift.x += best.dx;
    totalShift.y += best.dy;
    rounds = round + 1;
  }

  if (obstacles.some((item) => overlapsWithGap(frame, item.frame, gap))) limited = true;

  return {
    frame,
    adjusted: !sameFrame(frame, original),
    rounds,
    totalShift,
    limited,
    blockers,
  };
}

// ---------------------------------------------------------------------------
// 4. 组缩放：成员槽位 / 图片比例 / 最小尺寸 / 不留空白
// ---------------------------------------------------------------------------

export interface GroupMemberGeometry {
  id: string;
  /** 成员当前正式矩形（板面坐标） */
  frame: V2Rect;
  /**
   * 图片成员：宽/高（> 0 的有限数才有意义）。缺省 / null / 非法值 = 普通成员（无比例约束）。
   * 对应契约 §5.2 的 meta.display.aspectRatio；未知时不猜比例。
   */
  aspectRatio?: number | null;
}

export interface GroupResizeInput {
  /** 组框起点（当前正式矩形） */
  frame: V2Rect;
  /** 用户拖出的目标组框（未约束；可以是任意方向拖出来的结果） */
  nextFrame: V2Rect;
  members: readonly GroupMemberGeometry[];
  /** 成员间距，默认 GROUP_RESIZE_GAP（16） */
  gap?: number;
  /** 组框内边距，默认 GROUP_RESIZE_PADDING（16） */
  padding?: number;
  /** 卡片最小尺寸，默认 CARD_MIN_SIZE（180×96） */
  minSize?: V2Size;
  /** 卡片最大尺寸，默认 CARD_MAX_SIZE（1600×1200） */
  maxSize?: V2Size;
}

export type GroupResizeIssue =
  /** 目标组框小于成员最小布局所需，组框被撑到最小可行尺寸 */
  | "min_clamped"
  /** 组框 / 目标框是坏数据，已按最小合法值归一化 */
  | "frame_normalized";

export interface GroupResizeResult {
  /** 实际组框：不小于目标框，也不小于成员最小布局所需（可能比 nextFrame 大） */
  frame: V2Rect;
  /** 每个成员的正式矩形（板面坐标，保证组框内、互不重叠、不小于最小尺寸） */
  slots: Record<string, V2Rect>;
  /** 成员的排布顺序（行优先，稳定；与输入顺序无关地按当前布局推出） */
  order: string[];
  issues: GroupResizeIssue[];
}

/** 合法图片比例：只有正有限数才算（0 / 负数 / NaN / Infinity 一律视为未知）。 */
function validAspectRatio(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

type SizedMember = {
  id: string;
  frame: V2Rect;
  ratio: number | null;
  base: V2Size;
  index: number;
};

/** 按比例把尺寸收敛到 [min, max]；比例严格保持（除极端比例撞上最大边长时以比例为准）。 */
function fitRatioSize(size: V2Size, ratio: number, min: V2Size, max: V2Size): V2Size {
  let w = Math.max(size.w, min.w);
  let h = w / ratio;
  if (h < min.h) {
    h = min.h;
    w = h * ratio;
  }
  if (w > max.w) {
    w = max.w;
    h = w / ratio;
  }
  if (h > max.h) {
    h = max.h;
    w = h * ratio;
  }
  return { w, h };
}

/** 普通成员：直接夹进 [min, max]。 */
function fitPlainSize(size: V2Size, min: V2Size, max: V2Size): V2Size {
  return {
    w: clamp(size.w, min.w, max.w),
    h: clamp(size.h, min.h, max.h),
  };
}

/**
 * 组框缩放：把成员重新排成组框内的槽位。
 *
 * 语义（契约 §5.2 / §17.1，纯显示口径，不写回板面）：
 * 1. 沿用当前**行结构**（按当前 y 分行的成员仍在一行，行内按当前 x 排序）—— 用户安排不被打乱；
 * 2. 目标尺寸 = 当前尺寸 × (目标组框 / 当前组框)，再夹进最小 / 最大尺寸；
 *    图片成员在这之后仍**严格保持原图比例**（不拉伸、不猜比例）；
 * 3. 组框不小于成员最小布局所需（requiredW/H + 两侧 padding），不够就撑大并报 min_clamped；
 * 4. 横向 / 纵向的富余空间按行平均分掉（行高增加、行内间距按 space-between 分配），
 *    缩小后成员贴边排布 —— 不在组框里留下成片空白；
 * 5. 成员间始终至少留 gap，绝不重叠。
 */
export function planGroupResize(input: GroupResizeInput): GroupResizeResult {
  const start = normalizeFrame(input?.frame);
  const desired = normalizeFrame(input?.nextFrame);
  const gap = Math.max(0, finite(input?.gap, GROUP_RESIZE_GAP));
  const padding = Math.max(0, finite(input?.padding, GROUP_RESIZE_PADDING));
  const minSize = normalizeSize(input?.minSize, CARD_MIN_SIZE);
  const maxSize = normalizeSize(input?.maxSize, CARD_MAX_SIZE);
  const issues: GroupResizeIssue[] = [];

  const rawNext = input?.nextFrame;
  const nextIsBad =
    !rawNext ||
    !Number.isFinite(rawNext.x) ||
    !Number.isFinite(rawNext.y) ||
    !Number.isFinite(rawNext.w) ||
    !Number.isFinite(rawNext.h) ||
    rawNext.w < MIN_FRAME_SIZE ||
    rawNext.h < MIN_FRAME_SIZE;
  if (nextIsBad) issues.push("frame_normalized");
  const rawStart = input?.frame;
  const startIsBad =
    !rawStart ||
    !Number.isFinite(rawStart.x) ||
    !Number.isFinite(rawStart.y) ||
    !Number.isFinite(rawStart.w) ||
    !Number.isFinite(rawStart.h) ||
    rawStart.w < MIN_FRAME_SIZE ||
    rawStart.h < MIN_FRAME_SIZE;
  if (startIsBad && !issues.includes("frame_normalized")) issues.push("frame_normalized");

  const seen = new Set<string>();
  const members: SizedMember[] = [];
  for (const [index, member] of (input?.members ?? []).entries()) {
    if (!member || typeof member.id !== "string" || !member.id || seen.has(member.id)) continue;
    seen.add(member.id);
    members.push({
      id: member.id,
      frame: normalizeFrame(member.frame),
      ratio: validAspectRatio(member.aspectRatio),
      base: { w: 0, h: 0 },
      index,
    });
  }
  if (!members.length) {
    return { frame: desired, slots: {}, order: [], issues };
  }

  // 行结构：按当前布局（y, x, 原顺序）排序，再按竖直相交分行。
  const sorted = [...members].sort((a, b) => {
    if (a.frame.y !== b.frame.y) return a.frame.y - b.frame.y;
    if (a.frame.x !== b.frame.x) return a.frame.x - b.frame.x;
    return a.index - b.index;
  });
  const rows: SizedMember[][] = [];
  for (const member of sorted) {
    const row = rows[rows.length - 1];
    if (row) {
      const bandTop = Math.min(...row.map((item) => item.frame.y));
      const bandBottom = Math.max(...row.map((item) => item.frame.y + item.frame.h));
      if (member.frame.y < bandBottom - 1e-6 && member.frame.y + member.frame.h > bandTop + 1e-6) {
        row.push(member);
        continue;
      }
    }
    rows.push([member]);
  }
  for (const row of rows) row.sort((a, b) => a.frame.x - b.frame.x || a.index - b.index);

  // 目标尺寸：先按组框比例缩放，再按最小 / 最大尺寸与图片比例收敛。
  const scaleX = desired.w / start.w;
  const scaleY = desired.h / start.h;
  for (const member of sorted) {
    const scaled: V2Size = { w: member.frame.w * scaleX, h: member.frame.h * scaleY };
    member.base = member.ratio
      ? fitRatioSize(scaled, member.ratio, minSize, maxSize)
      : fitPlainSize(scaled, minSize, maxSize);
  }

  const rowBaseHeights = rows.map((row) => Math.max(...row.map((member) => member.base.h)));
  const rowWidths = rows.map(
    (row) => row.reduce((sum, member) => sum + member.base.w, 0) + gap * Math.max(0, row.length - 1),
  );
  const requiredW = Math.max(...rowWidths);
  const requiredH = rowBaseHeights.reduce((sum, value) => sum + value, 0) + gap * Math.max(0, rows.length - 1);

  const frameW = Math.max(desired.w, requiredW + padding * 2);
  const frameH = Math.max(desired.h, requiredH + padding * 2);
  if (frameW > desired.w + 1e-6 || frameH > desired.h + 1e-6) issues.push("min_clamped");

  const innerW = Math.max(1, frameW - padding * 2);
  const innerH = Math.max(1, frameH - padding * 2);
  const extraH = Math.max(0, innerH - requiredH);
  const rowHeights = rowBaseHeights.map((height) => height + extraH / rows.length);

  const slots: Record<string, V2Rect> = {};
  const order: string[] = [];
  let rowTop = padding;

  for (const [rowIndex, row] of rows.entries()) {
    const rowHeight = rowHeights[rowIndex];
    // 先定这一行每个成员的实际尺寸（图片按比例装进行高，普通成员适配行高）。
    const widths: number[] = [];
    const heights: number[] = [];
    for (const member of row) {
      let width: number;
      let height: number;
      if (member.ratio) {
        width = Math.min(member.base.w, innerW);
        height = width / member.ratio;
        if (height > rowHeight) {
          height = rowHeight;
          width = height * member.ratio;
        }
        if (height < minSize.h) {
          height = minSize.h;
          width = height * member.ratio;
        }
        if (width > innerW) {
          width = innerW;
          height = width / member.ratio;
        }
        if (width < minSize.w) {
          width = minSize.w;
          height = width / member.ratio;
        }
      } else {
        width = Math.min(member.base.w, innerW);
        height = Math.min(rowHeight, maxSize.h);
      }
      widths.push(Math.max(MIN_FRAME_SIZE, width));
      heights.push(Math.max(MIN_FRAME_SIZE, height));
    }

    // 行内富余空间走间距（space-between 口径）：首成员贴左边、末成员贴右边，不在组框里留白。
    const usedWidth = widths.reduce((sum, value) => sum + value, 0);
    const extraW = Math.max(0, innerW - usedWidth - gap * Math.max(0, row.length - 1));
    const distributedGap = row.length > 1 ? gap + extraW / (row.length - 1) : 0;
    let cursor = padding;

    for (const [memberIndex, member] of row.entries()) {
      const width = widths[memberIndex];
      const height = heights[memberIndex];
      const x = row.length > 1 ? cursor : padding;
      const y = rowTop + Math.max(0, (rowHeight - height) / 2);
      slots[member.id] = { x, y, w: width, h: height };
      order.push(member.id);
      cursor += width + distributedGap;
    }

    rowTop += rowHeight + gap;
  }

  const frame: V2Rect = { x: desired.x, y: desired.y, w: frameW, h: frameH };
  const boardSlots: Record<string, V2Rect> = {};
  for (const [id, local] of Object.entries(slots)) {
    boardSlots[id] = {
      x: frame.x + local.x,
      y: frame.y + local.y,
      w: local.w,
      h: local.h,
    };
  }
  return { frame, slots: boardSlots, order, issues };
}

/**
 * 把一次组缩放结果摊成**一份**原子变更清单：组框与全部成员槽位一起提交。
 *
 * 为什么要有它：组缩放如果分成「先改组框、再改成员」两次写回，中间态会露出
 * 组框与成员错位 / 成员压在组框外的一帧；这份清单让调用方一次 commit 全部改完。
 */
export function groupResizeFrameChanges(
  groupId: string,
  result: GroupResizeResult,
  memberIds: readonly string[] = Object.keys(result?.slots ?? {}),
): FrameChangeInput[] {
  const changes: FrameChangeInput[] = [];
  const id = typeof groupId === "string" && groupId ? groupId : "";
  if (id && result?.frame) changes.push({ id, kind: "group", frame: { ...result.frame } });
  for (const memberId of memberIds) {
    const frame = result?.slots?.[memberId];
    if (!frame) continue;
    changes.push({ id: memberId, kind: "card", frame: { ...frame } });
  }
  return changes;
}

// ---------------------------------------------------------------------------
// 5. 原子变更计划（一次提交全部几何变化，不给半份变更）
// ---------------------------------------------------------------------------

export interface FrameChangeInput {
  id: string;
  kind: FrameKind;
  frame: V2Rect;
}

export interface FrameChange {
  id: string;
  kind: FrameKind;
  frame: V2Rect;
}

export interface FramePlan {
  /** 全部变更（顺序与输入一致；同一 id 只会出现一次） */
  changes: FrameChange[];
  /** 逐 id 的最终帧（供渲染 / 提交直接读） */
  frameOf: Record<string, V2Rect>;
}

export type FramePlanIssueCode =
  | "duplicate_id"
  | "invalid_frame"
  | "below_min_size"
  | "out_of_bounds"
  | "unknown_kind";

export interface FramePlanIssue {
  code: FramePlanIssueCode;
  id: string;
}

export type FramePlanResult =
  | { ok: true; plan: FramePlan }
  | { ok: false; issues: FramePlanIssue[] };

export interface BuildFramePlanInput {
  changes: readonly FrameChangeInput[];
  /** 可选的当前帧：仅用于拒绝「同一对象两次变更」这类自相矛盾的输入 */
  currentFrames?: Record<string, V2Rect> | null;
  /** 可选画布边界：出界就整份拒绝（不偷偷夹取） */
  bounds?: V2Rect | null;
  /** 卡片最小尺寸，默认 CARD_MIN_SIZE；组的最小尺寸默认 1×1 */
  minSize?: V2Size;
}

/**
 * 原子变更计划：**要么全部合法、要么整份拒绝**。
 *
 * 调用方拿到 ok=true 的 plan 后一次性写回（store 一次 commit / 一次保存），
 * 不会出现「组框改了、成员还没改」的中间态。任何一项非法（坏数、重复 id、
 * 低于最小尺寸、出界、未知 kind）都返回 ok=false 并列出全部问题。
 */
export function buildFramePlan(input: BuildFramePlanInput): FramePlanResult {
  const changesInput = input?.changes ?? [];
  const minSize = normalizeSize(input?.minSize, CARD_MIN_SIZE);
  const bounds = input?.bounds ? normalizeFrame(input.bounds) : null;
  const issues: FramePlanIssue[] = [];
  const seen = new Set<string>();
  const changes: FrameChange[] = [];

  for (const item of changesInput) {
    const id = typeof item?.id === "string" ? item.id : "";
    if (!id) {
      issues.push({ code: "invalid_frame", id });
      continue;
    }
    if (seen.has(id)) {
      issues.push({ code: "duplicate_id", id });
      continue;
    }
    seen.add(id);
    const kind: FrameKind | null = item.kind === "card" || item.kind === "group" ? item.kind : null;
    if (!kind) {
      issues.push({ code: "unknown_kind", id });
      continue;
    }
    const raw = item.frame;
    const hasBadNumber =
      !raw ||
      !Number.isFinite(raw.x) ||
      !Number.isFinite(raw.y) ||
      !Number.isFinite(raw.w) ||
      !Number.isFinite(raw.h) ||
      raw.w <= 0 ||
      raw.h <= 0;
    if (hasBadNumber) {
      issues.push({ code: "invalid_frame", id });
      continue;
    }
    const frame = { x: raw.x, y: raw.y, w: raw.w, h: raw.h };
    if (kind === "card" && (frame.w < minSize.w - 1e-9 || frame.h < minSize.h - 1e-9)) {
      issues.push({ code: "below_min_size", id });
      continue;
    }
    if (bounds) {
      const inside =
        frame.x >= bounds.x - 1e-9 &&
        frame.y >= bounds.y - 1e-9 &&
        frame.x + frame.w <= bounds.x + bounds.w + 1e-9 &&
        frame.y + frame.h <= bounds.y + bounds.h + 1e-9;
      if (!inside) {
        issues.push({ code: "out_of_bounds", id });
        continue;
      }
    }
    changes.push({ id, kind, frame });
  }

  if (issues.length) return { ok: false, issues };

  const frameOf: Record<string, V2Rect> = {};
  for (const change of changes) frameOf[change.id] = { ...change.frame };
  return { ok: true, plan: { changes, frameOf } };
}

/**
 * 应用计划：返回**新数组**，只替换计划里出现的对象帧，其它对象保持原引用。
 * 输入数组与对象都不被修改（原子计划的「应用」这一步同样是纯的）。
 */
export function applyFramePlan<T extends { id: string; frame: V2Rect }>(
  items: readonly T[],
  plan: FramePlan,
): T[] {
  return items.map((item) => {
    const next = plan?.frameOf?.[item.id];
    if (!next) return item;
    return { ...item, frame: { ...next } };
  });
}
