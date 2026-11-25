/**
 * 互动模式底部操作区的开合动效 / 线框几何纯函数（motion-core）。
 *
 * 来源：参考预览 `QIO_Animated_Chat_Preview.html` 的 approach()/drawFrame()，
 * 改写为无 DOM、无副作用、可离线单测的解析实现；
 * 2026-11-17 按用户确认的演示修订：线框出现 / 消失同速且共用一条
 * easeInOutCubic（慢-快-慢）时间轴，并新增「消息随下沉虚化」的纯函数映射。
 *
 * 边界：
 * - 只做算术与字符串推导，不读 DOM、不读时钟、不发请求、不碰会话状态；
 * - 全部导出函数均为纯函数（组件只读取返回值驱动 rAF 时间轴）；
 * - 几何输出统一量化（路径坐标 2 位、长度 3 位、比率 4 位小数），避免浮点噪声进入
 *   SVG 属性与 data-* 观测值；非法输入回退到安全值，绝不输出 NaN。
 */

/**
 * 开合时间轴常量（用户 2026-11-17 确认的演示口径，冻结）：
 * - durationMs 完整「出现 / 消失」标称时长：两个方向相同（650ms），
 *   中途反向时按剩余距离等比缩短 —— 出现与消失共用同一条时间轴；
 * - progressEpsilon 归一化进度吸附阈值；
 * - mistBlurPx / mistStartProgress / mistEndProgress：消息内容随面板收起
 *   「下沉虚化」的最大模糊半径与进度窗口（start 之前不虚化，end 之后完全消失）。
 */
export const CONVERSATION_MOTION = {
  durationMs: 650,
  progressEpsilon: 0.0015,
  mistBlurPx: 6,
  mistStartProgress: 0.5,
  mistEndProgress: 0.15,
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

/** 归一化进度夹到 [0,1]；非有限值视为 0。 */
export function clampProgress(value: number | undefined): number {
  return clamp01(value);
}

/** 三次缓入缓出（慢-快-慢）：f(0)=0、f(0.5)=0.5、f(1)=1，两端一阶导为 0。 */
export function easeInOutCubic(u: number): number {
  const x = clamp01(u);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

/**
 * 线性时间 → 缓动进度：把 elapsedMs/durationMs 夹到 [0,1] 后过 easeInOutCubic。
 * duration<=0 视为瞬间完成（返回 1）；非法输入按 0 处理。
 */
export function easedProgress(elapsedMs: number, durationMs: number): number {
  const duration = finiteOr(durationMs, 0);
  const elapsed = finiteOr(elapsedMs, 0);
  if (!(duration > 0)) return 1;
  return easeInOutCubic(elapsed / duration);
}

/** 消息内容虚化输出：mist=0 完全清晰、mist=1 完全虚化（透明度 0 + 最大模糊）。 */
export interface ConversationContentMist {
  /** 0..1 虚化程度。 */
  mist: number;
  /** 内容不透明度（1 - mist）。 */
  opacity: number;
  /** 模糊半径（px）。 */
  blurPx: number;
}

/**
 * 按开合进度计算消息内容虚化：进度 >= mistStartProgress 完全不虚化，
 * <= mistEndProgress 完全消失，中间用 smoothstep 过渡。
 * 展开 / 收起共用同一条映射，所以「下沉时虚化」与「升起时恢复」严格对称。
 */
export function conversationContentMist(progress: number | undefined): ConversationContentMist {
  const value = clamp01(progress);
  const start = CONVERSATION_MOTION.mistStartProgress;
  const end = CONVERSATION_MOTION.mistEndProgress;
  const span = Math.max(1e-6, start - end);
  const raw = clamp01((start - value) / span);
  const mist = raw * raw * (3 - 2 * raw);
  return {
    mist: quantize(mist, RATIO_DIGITS),
    opacity: quantize(1 - mist, RATIO_DIGITS),
    blurPx: quantize(mist * CONVERSATION_MOTION.mistBlurPx, 2),
  };
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
