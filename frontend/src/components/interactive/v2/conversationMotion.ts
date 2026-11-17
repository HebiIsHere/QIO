/**
 * 互动模式底部操作区的开合动效 / 线框几何纯函数（motion-core）。
 *
 * 来源：参考预览 `QIO_Animated_Chat_Preview.html` 的 approach()/drawFrame()，
 * 改写为无 DOM、无副作用、可离线单测的解析实现；冻结契约见
 * `_lead-logs/interactive-action/PLAN.md` §3.1。
 *
 * 边界：
 * - 只做算术与字符串推导，不读 DOM、不读时钟、不发请求、不碰会话状态；
 * - 唯一的原地更新入口是 stepMotionAxis（按帧推进单个轴），其余函数均为纯函数；
 * - 几何输出统一量化（路径坐标 2 位、长度 3 位、比率 4 位小数），避免浮点噪声进入
 *   SVG 属性与 data-* 观测值；非法输入回退到安全值，绝不输出 NaN。
 */

/** 单轴临界阻尼状态：value 当前值、velocity 当前速度、target 目标值。 */
export interface MotionAxis {
  value: number;
  velocity: number;
  target: number;
}

/**
 * 开合动效常量（冻结）：
 * - openRate 展开 11 s⁻¹（慢，接近预览的舒展感）；
 * - closeRate 常规收起 21 s⁻¹（快，减少等待）；
 * - dragRate 拖动跟手 45 s⁻¹（更硬，跟指针走）；
 * - dtMax 单帧最大积分步长 0.04s（掉帧/后台恢复时不会一步跳太远）；
 * - heightEpsilon 高度轴吸附阈值（像素）；
 * - revealEpsilon 揭示轴吸附阈值（比例）。
 */
export const CONVERSATION_MOTION = {
  openRate: 11,
  closeRate: 21,
  dragRate: 45,
  dtMax: 0.04,
  heightEpsilon: 0.25,
  revealEpsilon: 0.0015,
} as const;

/** 路径坐标量化位数。 */
const COORD_DIGITS = 2;
/** 长度量化位数。 */
const LENGTH_DIGITS = 3;
/** 比率（透明度 / 角揭示比例）量化位数。 */
const RATIO_DIGITS = 4;
/** 圆角与描边边距的默认值（与预览 drawFrame() 一致）。 */
const DEFAULT_EDGE = 0.75;
const DEFAULT_CORNER = 14;
const DEFAULT_LEVER_INSET = 4;

/** 有限数兜底：非有限（NaN / ±Infinity / 非 number）一律回退 fallback。 */
function finiteOr(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** 有限非负数：非法值与负数都夹到 0。 */
function finiteNonNegative(value: number | undefined): number {
  return Math.max(0, finiteOr(value, 0));
}

/** 比率夹到 [0,1]；NaN 视为 0。 */
function clamp01(value: number | undefined): number {
  const safe = finiteOr(value, 0);
  if (safe <= 0) return 0;
  if (safe >= 1) return 1;
  return safe;
}

/** 量化到指定小数位；非法值归 0，且不产生 -0（避免 toBe / 序列化口径不一致）。 */
function quantize(value: number, digits: number): number {
  if (!Number.isFinite(value)) return 0;
  const factor = 10 ** digits;
  const scaled = value * factor;
  // 极大值不做量化（但仍保持有限），避免 Math.round(Infinity) 产生非法输出
  const rounded = Number.isFinite(scaled) ? Math.round(scaled) / factor : value;
  return Object.is(rounded, -0) ? 0 : rounded;
}

/** 轴工厂：默认 value=target=0；只给 value 时 target 跟随 value（初始静止）。 */
export function createMotionAxis(value = 0, target = value): MotionAxis {
  const safeValue = finiteOr(value, 0);
  return {
    value: safeValue,
    velocity: 0,
    target: finiteOr(target, safeValue),
  };
}

/**
 * 单步临界阻尼逼近（预览 approach() 的 TS 版）：
 * - dt 先夹到 [0, dtMax]，负值/NaN 视为 0；
 * - 只有「越过 target」或「误差与速度都足够小」时才吸附，吸附后 value===target、velocity===0；
 * - 返回 true 表示本步已 settled（调用方可以停止 rAF 循环）。
 */
export function stepMotionAxis(axis: MotionAxis, dtSeconds: number, rate: number, epsilon: number): boolean {
  const dt = Math.min(CONVERSATION_MOTION.dtMax, Math.max(0, finiteOr(dtSeconds, 0)));
  const safeRate = finiteOr(rate, 0);
  const safeEpsilon = finiteNonNegative(epsilon);

  const offset = axis.value - axis.target;
  const momentum = axis.velocity + safeRate * offset;
  const decay = Math.exp(-safeRate * dt);
  const next = (offset + momentum * dt) * decay;
  axis.velocity = (axis.velocity - safeRate * momentum * dt) * decay;

  if (offset * next <= 0 || (Math.abs(next) < safeEpsilon && Math.abs(axis.velocity) < safeEpsilon * 20)) {
    axis.value = axis.target;
    axis.velocity = 0;
    return true;
  }
  axis.value = axis.target + next;
  return false;
}

/** 按当前状态选 rate：拖动优先（45），其次展开（11），其余收起（21）。 */
export function motionRateFor(state: { opening: boolean; dragging: boolean }): number {
  if (state.dragging) return CONVERSATION_MOTION.dragRate;
  if (state.opening) return CONVERSATION_MOTION.openRate;
  return CONVERSATION_MOTION.closeRate;
}

/**
 * 临界阻尼解析进度：1-(1+rt)e^{-rt}。
 * t<=0（含 NaN）返回 0；rate<=0 视为瞬间完成返回 1；结果夹在 [0,1]。
 * 该函数与 stepMotionAxis 的离散步进在零初速下逐帧一致，可用于离线校准。
 */
export function criticallyDampedProgress(elapsedSeconds: number, rate: number): number {
  const elapsed = finiteOr(elapsedSeconds, 0);
  if (!(elapsed > 0)) return 0;
  if (!(rate > 0)) return 1;
  const x = rate * elapsed;
  if (!Number.isFinite(x)) return 1; // 溢出（rate/t 极大）视作已完成
  const remaining = (1 + x) * Math.exp(-x);
  return Math.min(1, Math.max(0, 1 - remaining));
}

/**
 * criticallyDampedProgress 的反解：给定进度 p，求 t 使 1-(1+rt)e^{-rt}=p。
 * progress<=0（含 NaN）→ 0；progress>=1 或 rate<=0 → Infinity（永不完成）。
 * 用二分法反解（进度单调），精度到双精度浮点极限。
 */
export function criticallyDampedDuration(progress: number, rate: number): number {
  const target = finiteOr(progress, 0);
  if (!(target > 0)) return 0;
  if (!(rate > 0) || !Number.isFinite(rate)) return Number.POSITIVE_INFINITY;
  if (target >= 1) return Number.POSITIVE_INFINITY;

  // 先把上界扩到进度覆盖 target 的 x=rt 处
  let low = 0;
  let high = 2;
  for (let guard = 0; guard < 64 && criticallyDampedProgress(high / rate, rate) < target; guard += 1) {
    high *= 2;
  }
  for (let iterate = 0; iterate < 100; iterate += 1) {
    const middle = (low + high) / 2;
    if (criticallyDampedProgress(middle / rate, rate) < target) low = middle;
    else high = middle;
  }
  return (low + high) / 2 / rate;
}

/** 线框几何输入（组件每次测量后传入；单位 px）。 */
export interface ConversationFrameInput {
  /** 面板 / 操作区宽度（SVG 宽度）。 */
  panelWidth: number;
  /** 把手（胶囊形拖动条）宽度。 */
  leverWidth: number;
  /** 把手相对面板顶部偏移。 */
  leverTop: number;
  /** 把手高度。 */
  leverHeight: number;
  /** 胶囊输入框相对面板顶部偏移。 */
  inputTop: number;
  /** 胶囊输入框高度。 */
  inputHeight: number;
  /** 揭示进度（0 收起 / 1 完全展开），内部夹到 [0,1]。 */
  reveal: number;
  /** 描边离面板左右边缘的距离，默认 0.75。 */
  edge?: number;
  /** 圆角半径，默认 14（复用 --qio-ui-radius）。 */
  corner?: number;
  /** 把手端点到线头的内缩，默认 4。 */
  leverInset?: number;
}

/** 线框几何输出：镜像 SVG 路径 + 解析弧长/揭示 + 玻璃裁剪 + 渐变高度。 */
export interface ConversationFrameGeometry {
  /** SVG 宽度（= panelWidth）。 */
  width: number;
  /** SVG 高度（= endY，胶囊中线）。 */
  height: number;
  /** 面板中线（= panelWidth/2）。 */
  center: number;
  /** 把手中线 y。 */
  leverY: number;
  /** 把手半宽（= max(0, leverWidth/2 - leverInset)）。 */
  halfHandle: number;
  /** 竖边终点 y（胶囊中线，夹到 >= leverY+corner）。 */
  endY: number;
  /** 实际采用的圆角半径。 */
  corner: number;
  /** 实际采用的描边边距。 */
  edge: number;
  /** 左半线框路径（从把手左端点 → 左边缘圆角 → 向下到胶囊中线）。 */
  leftPath: string;
  /** 右半线框路径（左半路径关于 center 的镜像）。 */
  rightPath: string;
  /** 横向段长度。 */
  horizontalLength: number;
  /** 圆角弧长（解析值 π·corner/2，等价于预览里 getTotalLength 的弧段）。 */
  arcLength: number;
  /** 竖向段长度。 */
  verticalLength: number;
  /** 单侧完整路径长度。 */
  pathLength: number;
  /** 当前揭示的可见长度（= pathLength·reveal）。 */
  visibleLength: number;
  /** SVG stroke-dasharray：同一长度喂给两侧路径。 */
  strokeDasharray: string;
  /** 玻璃层左右裁剪内缩（px）。 */
  glassInset: number;
  /** 玻璃层不透明度（= reveal）。 */
  glassOpacity: number;
  /** 描边渐变起止 y。 */
  fadeY1: number;
  fadeY2: number;
  /** 横线走完、开始进入圆角的揭示比例。 */
  cornerReveal: number;
  /** 线框是否可见（reveal>0）。 */
  visible: boolean;
}

/** 面板宽度非法时的安全零几何：全部为有限零值，不抛错、不出 NaN。 */
function emptyFrameGeometry(): ConversationFrameGeometry {
  return {
    width: 0,
    height: 0,
    center: 0,
    leverY: 0,
    halfHandle: 0,
    endY: 0,
    corner: 0,
    edge: 0,
    leftPath: "",
    rightPath: "",
    horizontalLength: 0,
    arcLength: 0,
    verticalLength: 0,
    pathLength: 0,
    visibleLength: 0,
    strokeDasharray: "none",
    glassInset: 0,
    glassOpacity: 0,
    fadeY1: 0,
    fadeY2: 0,
    cornerReveal: 0,
    visible: false,
  };
}

/**
 * 由实测几何 + 揭示进度推导线框 / 玻璃参数（预览 drawFrame() 的解析化）。
 *
 * 路径口径（左右镜像）：
 *   M center∓halfHandle leverY
 *   H edge+corner            （横线）
 *   Q edge leverY edge leverY+corner  （圆角）
 *   V endY                   （竖边）
 * 揭示按 pathLength 线性推进（dasharray），因此「横线到角」发生在
 * cornerReveal = horizontalLength/pathLength 处，可用 criticallyDampedDuration 反解到角时刻。
 */
export function conversationFrameGeometry(input: ConversationFrameInput): ConversationFrameGeometry {
  const panelWidth = finiteOr(input.panelWidth, 0);
  const reveal = clamp01(input.reveal);
  // 宽度量不到（首帧 / 收起 / 隐藏）时返回全零几何，组件据此隐藏 SVG
  if (!(panelWidth > 0)) return emptyFrameGeometry();

  const leverWidth = finiteNonNegative(input.leverWidth);
  const leverTop = finiteOr(input.leverTop, 0);
  const leverHeight = finiteNonNegative(input.leverHeight);
  const inputTop = finiteOr(input.inputTop, 0);
  const inputHeight = finiteNonNegative(input.inputHeight);
  const edge = Math.max(0, finiteOr(input.edge, DEFAULT_EDGE));
  const corner = Math.max(0, finiteOr(input.corner, DEFAULT_CORNER));
  const leverInset = Math.max(0, finiteOr(input.leverInset, DEFAULT_LEVER_INSET));

  const center = panelWidth / 2;
  const halfHandle = Math.max(0, leverWidth / 2 - leverInset);
  const leverY = leverTop + leverHeight / 2;
  // 竖边终点：胶囊中线；但不得低于圆角起点，保证 verticalLength 非负
  const endY = Math.max(leverY + corner, inputTop + inputHeight / 2);
  const verticalLength = endY - (leverY + corner);
  const horizontalLength = Math.max(0, center - halfHandle - (edge + corner));
  const arcLength = (Math.PI * corner) / 2;
  const pathLength = horizontalLength + arcLength + verticalLength;
  const visibleLengthRaw = pathLength * reveal;

  const pathLengthOut = quantize(pathLength, LENGTH_DIGITS);
  const visibleLength = quantize(visibleLengthRaw, LENGTH_DIGITS);
  const dashGap = quantize(pathLength + 1, LENGTH_DIGITS);
  const strokeDasharray = reveal <= 0 ? `0 ${dashGap}` : reveal >= 1 ? "none" : `${visibleLength} ${dashGap}`;

  const coord = (value: number): string => String(quantize(value, COORD_DIGITS));
  const radiusY = leverY + corner;
  const leftPath = `M ${coord(center - halfHandle)} ${coord(leverY)} H ${coord(edge + corner)} Q ${coord(edge)} ${coord(leverY)} ${coord(edge)} ${coord(radiusY)} V ${coord(endY)}`;
  const rightPath = `M ${coord(center + halfHandle)} ${coord(leverY)} H ${coord(panelWidth - edge - corner)} Q ${coord(panelWidth - edge)} ${coord(leverY)} ${coord(panelWidth - edge)} ${coord(radiusY)} V ${coord(endY)}`;

  return {
    width: quantize(panelWidth, COORD_DIGITS),
    height: quantize(endY, COORD_DIGITS),
    center: quantize(center, COORD_DIGITS),
    leverY: quantize(leverY, COORD_DIGITS),
    halfHandle: quantize(halfHandle, COORD_DIGITS),
    endY: quantize(endY, COORD_DIGITS),
    corner: quantize(corner, COORD_DIGITS),
    edge: quantize(edge, COORD_DIGITS),
    leftPath,
    rightPath,
    horizontalLength: quantize(horizontalLength, LENGTH_DIGITS),
    arcLength: quantize(arcLength, LENGTH_DIGITS),
    verticalLength: quantize(verticalLength, LENGTH_DIGITS),
    pathLength: pathLengthOut,
    visibleLength,
    strokeDasharray,
    glassInset: quantize(Math.max(0, center - halfHandle - visibleLength), LENGTH_DIGITS),
    glassOpacity: quantize(reveal, RATIO_DIGITS),
    fadeY1: quantize(inputTop - 10, COORD_DIGITS),
    fadeY2: quantize(endY, COORD_DIGITS),
    cornerReveal: quantize(pathLength > 0 ? Math.min(1, horizontalLength / pathLength) : 1, RATIO_DIGITS),
    visible: reveal > 0,
  };
}
