<script setup lang="ts">
/**
 * 星球页（全屏覆盖层）：3D 球体（左/中）+ 右侧面板（列表/详情/从这里开始）。
 * usePlanetScene 在本页内实例化渲染全屏星球（悬浮球为 SVG 微缩星球，不共享同一
 * WebGL 场景）；相机状态机 overview|planet|focus 语义与悬浮球保持一致：
 * - 打开（悬浮球点击）：场景推进到 planet（有锚点时直接聚焦话题）；
 * - 关闭（✕/Esc/从这里开始）：相机先拉回 overview（悬浮球位置/朝向）再收起覆盖层，
 *   拉回窗口内交互一律禁用。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { usePlanetScene } from "../composables/usePlanetScene";
import { api, type PlanetTopicSummary, type TopicDetail, type TopicFingerprint } from "../services/api";
import { PlanetBrowseSession, VISIBLE_CAPACITY } from "../planet/browseSession";
import { spreadPositions } from "../planet/layoutSlots";
import { LEVEL_HW } from "../planet/topicData";
import { useSessionStore } from "../stores/session";
import { useActionFeedback } from "../composables/useActionFeedback";
import QInput from "../components/ui/QInput.vue";
import KnowledgePanel from "../components/planet/KnowledgePanel.vue";
import EntityPanel from "../components/planet/EntityPanel.vue";
import { useUiStore } from "../stores/ui";
import { prefersReducedMotion } from "../utils/motion";
import { CSS_SETTLE_FALLBACK, easePointsFromCssValue, evalCubicBezier } from "../utils/easing";
import { anchorSignatureOf, planetSession } from "../composables/planetSession";
import {
  abandonContinuum,
  advanceTo,
  beginClose,
  beginOpen,
  ENTRY_SELECTOR,
  isCurrent,
  planetContinuum,
  readEntryOrigin,
  resetContinuum,
  type EntryOrigin,
} from "../composables/planetContinuum";
import QConfirm from "../components/ui/QConfirm.vue";

/**
 * 展开/收起的时间线（演示版，比任务02 的表格更慢一点，用户反馈原时长"过快"）：
 * 展开：先铺底 140ms → 内容淡入与相机小幅收敛同时进行 420ms（不再从小球拉近）
 * 收起：收势 180ms（轻微后撤 + 星球变淡、面板退场）→ 整体淡出 280ms → 卸载
 */
/**
 * 演示开关：把这条时间线整体放慢，方便逐帧观察「铺底 → 长大 → 聚焦 → 收势 → 收拢」的先后顺序。
 *
 * - `?planetdemo=slow` → 3 倍（沿用旧写法）；
 * - `?planetdemo=10` → 10 倍（任意 1–50 的数字都可以）；
 * - 不带参数 → 真实时长。
 *
 * 关键：**CSS 令牌与脚本时长必须用同一个 SPEED**。CSS 走 `--mo-3-*`（下面的 demoStyle），
 * 脚本用 `tokenMs()` 读同一批令牌 —— 两边一起放大，慢放出来的先后关系才和真实速度一致。
 * （踩过的坑：只用 CDP 放慢 CSS、脚本照跑，截图里量到的是两条完全不同的时间线。）
 */
const demoRaw = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("planetdemo") : null;
const slowDemo = demoRaw !== null;
const SPEED = demoRaw === null
  ? 1
  : demoRaw === "slow"
    ? 3
    : Math.max(1, Math.min(50, Number.parseFloat(demoRaw) || 1));
const OPEN_MS = 420 * SPEED;
const SETTLE_MS = 180 * SPEED;
const FADE_MS = 280 * SPEED;
const demoStyle = slowDemo
  ? ({
      "--dur-planet-backdrop": `${140 * SPEED}ms`,
      "--dur-planet-in": `${420 * SPEED}ms`,
      "--dur-planet-settle": `${180 * SPEED}ms`,
      "--dur-planet-out": `${280 * SPEED}ms`,
      // 连续体走的是分层令牌：演示开关必须同时把它们放慢，否则
      // 「逐帧观察星球展开」在第四阶段之后就失效了（只剩铺底被放慢）。
      "--mo-3-prepare": `${180 * SPEED}ms`,
      "--mo-3-expand": `${520 * SPEED}ms`,
      "--mo-3-settle": `${220 * SPEED}ms`,
      "--mo-3-collapse": `${420 * SPEED}ms`,
    } as Record<string, string>)
  : undefined;

/**
 * Planet 连续体（第四阶段）：入口小球与全屏 Planet 是**同一个对象的不同尺度状态**。
 *
 * 进入：A 入口激活 → B 体量展开（从入口矩形的中心与直径连续长大）→ C 场景接管。
 * 退出：先降信息密度与收回浮层 → 体量收缩回**此刻**入口所在的位置 → 交还给小球。
 *
 * 为什么用 CSS transform 缩放整个星球层，而不是「让相机从远处推近」：
 * 相机会改变球体的投影与话题点分布，看起来像「另一个东西在靠近」；
 * 而变换同一层的像素，读起来才是「这一个东西变大了」。
 * 缩放用的变换原点在入口小球的球心，因此起点与小球完全重合（`alignStageToEntry`）。
 */
const stageStyle = ref<Record<string, string>>({});
/**
 * 是否已经打开「星球层的缩放过渡」。
 *
 * 为什么需要它：第一次把星球层摆到入口小球的尺度时，如果过渡已经打开，浏览器会把
 * 这次赋值当成「从 identity 变到小球尺度」的过渡 —— 在冷启动（首次构建 WebGL 场景、
 * 只有几帧每秒）时用户几乎看不到起点，接着又被 B 阶段的赋值打断，实测表现为「没长大」。
 * 所以先把尺度**无过渡**地落到入口位置，下一帧再开启过渡，B 阶段的放大才是干净的
 * 「从入口尺度长大到全屏构图」。
 */
const stageArmed = ref(false);
/** C 阶段：玻璃浮层进入、话题可交互 */
const layersIn = ref(false);
/** 球态的数据是否已经喂过（球态也要真实数据：没有话题，融合环就没有形状） */
let ballDataLoaded = false;
/** 球态数据最后一次加载的时间：球态常驻，太久没刷新会变陈旧，但也不能频繁重拉 */
let ballDataAt = 0;
/** 球态数据的最长保鲜期：超过就趁回到球态时后台刷一次（不阻塞任何动画） */
const BALL_DATA_TTL_MS = 30_000;
/**
 * 球态的信息密度。
 *
 * 必须**只此一处**定义：收起动画的终点与球态用的是同一个值，否则交接那一帧会「变样」
 * （实测过：收缩结束设 0.12、球态 0.55，回到对话页的瞬间话题点与网格会突然冒出来）。
 */
const BALL_REVEAL = 0.55;
/**
 * 收缩尾段从「全窗口画布」切到「球自己的布局」的门槛：
 * 屏幕上球直径 ≤ 入口球直径 × 这个倍数时切换。
 *
 * 取 1.35 的依据（`scripts/baseline/qa/planet-handoff-probe` 那批静态对照图）：
 * 球布局的画布就是 108px（= 球自己的分辨率），放大 1.35 倍仍然连续、只是略软；
 * 而全窗口画布缩到 0.14 倍时，1 设备像素宽的轮廓线会被降采样成**断续的点**。
 * 再往上（1.5 倍以上）球开始明显发虚，所以停在 1.35。
 */
const TAIL_SWITCH_RATIO = 1.35;
/**
 * 铺底（把对话页盖住的那层虚空）。
 *
 * 它必须与「星球长大 / 收拢」**同一刻开始、同一刻结束**：
 * - 进入：阶段 B 开始的那一刻打开（过渡时长 = `--mo-3-expand`，与长大同长）；
 * - 退出：收缩开始的那一刻关闭（过渡时长 = `--mo-3-collapse`，与收拢同长）。
 *
 * 为什么不用 `entered`（阶段 A 就置位）：阶段 A 之后还要等话题数据与渲染就绪，
 * 那段时间长度不确定 —— 铺底会先自己走完，等星球真正开始长大时对话页早就黑掉了，
 * 用户看到的是「先黑屏，再突然出现一个星球」（实测反馈）。
 */
const curtainOn = ref(false);
/**
 * 收缩尾段：星球层切到「入口小球那套布局」渲染（画布 = 球自己的尺寸）。
 *
 * 它只改布局（画布尺寸 + 居中），**不动**与入口小球的关系（z-index / pointer-events /
 * 透明度仍是展开态的那一套）—— 那些属于球态 `.ball`，交接时才切。
 * 见 runCollapseMotion 的说明。
 */
const tailMode = ref(false);
/** 当前这次连续体的代号（与 planetContinuum.epoch 对应） */
let continuumEpoch = 0;

/**
 * 读一个时长令牌（ms）。
 *
 * 优先读星球页自己的元素：`?planetdemo=slow` 会把 `--mo-3-*` 覆盖在 `.planet-view` 上，
 * 如果只读 `:root`，JS 阶段推进仍然是原速 —— 慢放演示就在 B 阶段「演示未完、状态先到」，
 * 逐帧观察反而变得不可靠。reduced-motion 下令牌本身已经被压缩，这里不需要再判断。
 */
function tokenMs(name: string, fallback: number): number {
  try {
    const el = (rootRef.value as HTMLElement | null) ?? document.documentElement;
    const raw = getComputedStyle(el).getPropertyValue(name).trim();
    const v = parseFloat(raw);
    if (!Number.isFinite(v)) return fallback;
    return raw.endsWith("ms") ? v : v * 1000;
  } catch {
    return fallback;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, Math.max(0, ms)));
}

/**
 * 等渲染缓过来再开始「体量展开」。
 *
 * 为什么需要它：首次打开星球时，WebGL 场景初始化、着色器编译、话题数据装配会把主线程
 * 阻塞一段（实测冷启动 1.5–3.4s，软件渲染更久）。如果展开动画在这段时间里跑，浏览器
 * 一帧都画不出来，等主线程恢复时过渡早已「走完」——用户看到的是星球**直接跳出来**，
 * 而不是从小球长大（真实界面探针实测：整段采样没有任何中间态）。
 *
 * 所以：连续几帧的帧间隔恢复正常才继续；超过上限也不硬等（宁可少一点动画，不可卡住）。
 */
async function waitForSmoothFrames(minFrames = 3, maxWaitMs = 1500): Promise<void> {
  const started = performance.now();
  let good = 0;
  let last = performance.now();
  while (performance.now() - started < maxWaitMs) {
    await new Promise<void>((r) => requestAnimationFrame(() => r()));
    const now = performance.now();
    const dt = now - last;
    last = now;
    good = dt <= 120 ? good + 1 : 0;
    if (good >= minFrames) return;
  }
}

/**
 * 把星球层摆到「入口小球此刻所在的位置与尺度」。
 *
 * k = 入口小球半径 / 球体在屏幕上的半径；再平移，使球心落在入口中心上。
 * 拿不到球体真实尺寸（WebGL 不可用 / 场景未就绪）时返回 null，
 * 调用方退化成「不做缩放」——降级不崩、也不假装连续。
 *
 * 坐标一律用**布局值**：星球层自身带着 `scale()`，`getBoundingClientRect()` 给的是缩放后的
 * 位置，第二次打开时会算出错误的起点（详见 usePlanetScene.sphereScreenRect 的说明）。
 */
function alignStageToEntry(origin: EntryOrigin | null): number | null {
  if (!origin) {
    stageStyle.value = {};
    return null;
  }
  const sphere = planet.sphereScreenRect();
  if (!sphere || !Number.isFinite(sphere.radius) || sphere.radius <= 0) {
    stageStyle.value = {};
    return null;
  }
  const k = Math.max(0.02, Math.min(1, origin.r / sphere.radius));
  stageStyle.value = stageStyleForScreenGeometry(origin, sphere, k);
  // 把算出来的缩放回传：调用方（球态）需要它来算环宽补偿，
  // 不能在同一帧里再读一次 transform（那时新的 scale 还没落到样式上）
  return k;
}

/**
 * 让球体在**屏幕上**落在指定的球心与半径上 → 星球层的变换（布局坐标）。
 *
 * 变换以元素左上角为原点：先把球心换算成层内坐标，再把目标位置换算回位移。
 * `clampK` 用来区分两种用法：对齐入口小球时必须 ≤ 1（球态），而收缩尾段换布局后
 * 允许 > 1（球布局的画布只有 108px，屏幕上的球比它大一点）。
 */
function stageStyleForScreenGeometry(
  target: { cx: number; cy: number; r: number },
  sphere: { cx: number; cy: number; radius: number },
  clampK?: number,
): Record<string, string> {
  const { left, top } = layoutOrigin(stageRef.value);
  const raw = target.r / sphere.radius;
  const k = Math.max(0.02, clampK === undefined ? Math.min(8, raw) : Math.min(clampK, raw));
  const tx = target.cx - left - k * (sphere.cx - left);
  const ty = target.cy - top - k * (sphere.cy - top);
  return {
    "--stage-tx": `${tx.toFixed(2)}px`,
    "--stage-ty": `${ty.toFixed(2)}px`,
    "--stage-k": String(k),
  };
}

/** 星球层此刻的缩放与位移：读 computed transform —— 过渡进行中它就是当前插值 */
function currentStageTransform(): { k: number; tx: number; ty: number } | null {
  const stage = stageRef.value;
  if (!stage) return null;
  const m = String(getComputedStyle(stage).transform).match(/matrix\(([^)]+)\)/);
  if (!m) return null;
  const parts = m[1].split(",").map((v) => Number.parseFloat(v));
  if (parts.length < 6 || parts.some((v) => !Number.isFinite(v)) || parts[0] <= 0) return null;
  return { k: parts[0], tx: parts[4], ty: parts[5] };
}

/**
 * 布局坐标点 → 屏幕坐标。
 *
 * 层的变换以**自身左上角**为原点（`transform-origin: 0 0`），而布局坐标是相对视口的，
 * 所以要先减掉层的布局原点。收缩尾段换布局之前，就是靠它把「球此刻在屏幕上的球心」
 * 从当时的变换里读出来。
 */
function layoutToScreen(
  x: number,
  y: number,
  t: { k: number; tx: number; ty: number },
  frame: { left: number; top: number },
): { cx: number; cy: number } {
  return {
    cx: frame.left + t.tx + t.k * (x - frame.left),
    cy: frame.top + t.ty + t.k * (y - frame.top),
  };
}

/** 元素在页面里的布局坐标（不含祖先上的 transform）：累加 offsetParent 链 */
function layoutOrigin(el: HTMLElement | null): { left: number; top: number } {
  let left = 0;
  let top = 0;
  let node: HTMLElement | null = el;
  while (node) {
    left += node.offsetLeft;
    top += node.offsetTop;
    node = node.offsetParent as HTMLElement | null;
  }
  return { left, top };
}

/** 信息密度补间：0 = 抽象态，1 = 完整 Planet。用 rAF 而不是 CSS —— 密度是场景参数 */
function revealRamp(from: number, to: number, ms: number): Promise<void> {
  if (prefersReducedMotion() || ms <= 0) {
    planet.setReveal(to);
    return Promise.resolve();
  }
  const started = performance.now();
  return new Promise((resolve) => {
    const step = (now: number) => {
      const t = Math.min(1, (now - started) / ms);
      // 起步不僵硬、结尾柔和收束：与低频曲线同族（不是线性）
      const eased = 1 - Math.pow(1 - t, 3);
      planet.setReveal(from + (to - from) * eased);
      if (t < 1) requestAnimationFrame(step);
      else resolve();
    };
    requestAnimationFrame(step);
  });
}

/**
 * `seq` 是父级给的「这次打开」的序号：关闭回调把它带回去，
 * 父级只认当前序号 —— 关闭动画期间用户又打开一次时，迟到的旧回调不会关掉新层。
 */
const props = withDefaults(defineProps<{ seq?: number; open?: boolean }>(), { open: true });
const emit = defineEmits<{ close: [seq?: number] }>();
const session = useSessionStore();
const ui = useUiStore();

const canvasRef = ref<HTMLCanvasElement | null>(null);
/** 根元素：时长令牌要从这里读（演示开关把 `--mo-3-*` 覆盖在这一层） */
const rootRef = ref<HTMLElement | null>(null);
/** 星球层容器：缩放加在它身上（入口小球 → 全屏 Planet 的连续体） */
const stageRef = ref<HTMLElement | null>(null);
const planet = usePlanetScene(canvasRef);
/**
 * 球态：没打开星球时，星球层不是消失，而是缩在入口位置**继续渲染真实星球**。
 *
 * 这是原设计（单一 3D 场景 + 相机状态）的落地：入口小球 = 同一个场景、同一个渲染器
 * 缩到入口尺度，平常慢速自转；点击后从入口位置连续长大，结束后聚焦当前在聊的话题点。
 * three.js 是懒加载的：在它就绪前 `planetContinuum.ballLive` 还是 false，
 * 那时入口由 `PlanetOrb`（2D 压缩态）顶着首屏，就绪后再把位置让给真实渲染。
 */
const ballMode = computed(() => !props.open && planetContinuum.ballLive);
/** 入口几何变化（拖动 / 贴边 / 隐藏）的观察器：球态要跟着入口走 */
let entryObserver: MutationObserver | null = null;
let entryResizeObserver: ResizeObserver | null = null;
/** 贴边补间期间的逐帧跟随（见 followSnap 的说明） */
let snapFollowRaf = 0;

/** 球态：入口不透明度（贴边隐藏时对话页那枚球会变淡，真实渲染要跟着一起变） */
const ballOpacity = ref(1);

function entryEl(): HTMLElement | null {
  if (typeof document === "undefined") return null;
  return document.querySelector(ENTRY_SELECTOR) as HTMLElement | null;
}

/** 把球态对齐到入口当前位置与尺度（拖动、贴边、隐藏都走这里） */
function syncBall() {
  if (!ballMode.value) return;
  const k = alignStageToEntry(readEntryOrigin());
  const el = entryEl();
  ballOpacity.value = el ? Number(getComputedStyle(el).opacity || 1) : 1;
  // 球态也用同一套「按球体屏幕直径成比例」的宽度（k≈1 时就是球自身的直径）
  const sphere = planet.sphereScreenRect();
  planet.setRingScreenWidth(ringTargetWidthFor(sphere ? 2 * sphere.radius * (k ?? 1) : null));
  planet.resize(true);
}

/**
 * 球态布局就位后再对齐（最多等几帧）。
 *
 * 现场（2026-09-22 用户反馈「星球打开又缩小后消失了」）：体量收缩会把舞台摆到
 * 「入口半径 ÷ **整窗画布**上的球体半径」这个起点尺度上；如果这时球态接管了
 * （父级把 open 置回 false），而 `.ball` 还没落到 DOM，那么按整窗几何算出来的尺度
 * 就会留在舞台上 —— 画布下一秒变成 `--ball-size`（108px），球被乘成几个像素的点，
 * 看起来就是「星球消失了」（实测 1513 宽的窗口里 k=0.075 → 8px）。
 *
 * 所以球态这一侧先确认「画布已经是球尺寸」再写尺度：没到位就下一帧再看，
 * 上限几帧（正常情况一帧内就切好），不会无限等。
 */
function syncBallWhenLaidOut(attempt = 0): void {
  if (!ballMode.value) return;
  const canvas = canvasRef.value;
  let ballSize = 108;
  try {
    const raw = parseFloat(getComputedStyle(rootRef.value ?? document.documentElement).getPropertyValue("--ball-size"));
    if (Number.isFinite(raw) && raw > 0) ballSize = raw;
  } catch {
    /* 读不到就用 108（与 CSS 令牌同值） */
  }
  const laidOut = !canvas || canvas.offsetWidth <= ballSize * 1.5;
  if (!laidOut && attempt < 4) {
    requestAnimationFrame(() => syncBallWhenLaidOut(attempt + 1));
    return;
  }
  syncBall();
}

/**
 * 屏幕上的环该有多宽。
 *
 * 环宽是按屏幕像素定的（设计值 5/3.25/2px ≈ 球体直径的 0.6%）。整层缩小时像素宽也一起被缩掉，
 * 所以要把目标宽度按当前缩放铺回去 —— 但**目标宽度必须与球体屏幕直径成比例**，
 * 否则就不是「等比例缩小」了：曾经为了「看得见」把它定成固定的 1.7px，
 * 结果在 108px 的球上占了 1.8%（全屏才 0.67%），小球看起来是一圈圈加粗的同心环。
 *
 * 目标 = 球体屏幕直径 × 0.75%（与全屏的 0.6% 同量级），**保底 1.3px** ——
 * 1px 虽然还在（方案 A 之后不再被降采样抹掉），但三层环的半透明叠加会变得太淡、
 * 反而读不出环的结构；1.3px 是「看得见」与「不像加粗同心环」之间的实测折中。
 *
 * 注意这里只算**目标屏幕宽度**，倍数交给渲染器在渲染前按当时几何现算
 * （见 usePlanetScene.setRingScreenWidth）：调用方自己换算倍数踩过坑 ——
 * 尾段换布局的那一帧用旧倍数画出了一圈 12px 的粗环，就是用户看到的「缩小途中闪现」。
 */
function ringTargetWidthFor(sphereDiameterPx: number | null): number {
  const proportional = sphereDiameterPx !== null ? sphereDiameterPx * 0.0075 : LEVEL_HW[0];
  return Math.max(1.3, Math.min(LEVEL_HW[0], proportional));
}

/** 按星球层**当前**的屏幕几何更新环宽目标（缩放由 CSS 补间驱动，所以每帧读一次） */
function applyRingWidth() {
  const k = currentStageTransform()?.k ?? 1;
  const sphere = planet.sphereScreenRect();
  // 球体在**屏幕上**的直径 = 画布内半径 × 当前缩放
  const diameter = sphere ? 2 * sphere.radius * k : null;
  planet.setRingScreenWidth(ringTargetWidthFor(diameter));
}

/** 转场期间跟踪缩放（环宽要跟着变，否则长大过程中环会突然变细/变粗） */
function trackRingWidth(ms: number) {
  if (prefersReducedMotion()) {
    applyRingWidth();
    return;
  }
  const started = performance.now();
  const step = (now: number) => {
    applyRingWidth();
    if (now - started < ms) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/**
 * 球态的数据。
 *
 * 为什么需要：融合环的形状来自**真实话题点**（SDF 融合）。没有话题时 `levelField` 全为
 * 正无穷，小球上只剩一个轮廓空圈 —— 实测就是这样（`windowSize=0`）。
 * 所以球态也要取一次轻量数据（概览 + 第一批窗口），并且**不聚焦任何话题**：
 * 球态要保持慢速自转，聚焦会把它钉在一个点上。
 */
async function loadBallData(force = false) {
  if (ballDataLoaded && !force) return;
  // 保鲜期：球态常驻，数据会陈旧；但也不能每次回到球态都重拉
  if (force && ballDataLoaded && Date.now() - ballDataAt < BALL_DATA_TTL_MS) return;
  ballDataLoaded = true;
  try {
    const [t, overview] = await Promise.all([api.listTopics(), api.planetOverview()]);
    topics.value = t.topics;
    const page = await api.planetBrowse({
      direction: "forward",
      count: VISIBLE_CAPACITY,
      current_topic_id: session.currentTopicId ?? null,
    });
    nextCursor = page.next_cursor;
    sessionSeed = page.seed;
    browse.setSequence(page.items);
    browse.fill(
      performance.now(),
      spreadPositions(VISIBLE_CAPACITY, page.seed).map(
        (d) => [d.x, d.y, d.z] as [number, number, number],
      ),
    );
    const anchorTopic = pickTopicSummary(overview.topics, session.currentTopicId);
    if (anchorTopic && !browse.windowSlots().some((i) => i?.topic_id === anchorTopic.topic_id)) {
      browse.pinTopic(anchorTopic);
    }
    planet.attachBrowse(browse, { seed: page.seed, onWindowChange: syncWindow });
    planet.setTopics([]);
    syncWindow();
    ballDataAt = Date.now();
    // 数据到位后重新对齐一次：环的形状变了，但位置/尺度不变
    syncBall();
  } catch (e) {
    // 失败就让下次进入球态时再试；小球继续用轮廓顶着（不假装有内容）
    ballDataLoaded = false;
    ballDataAt = 0;
    console.warn("[planet] 入口小球的数据加载失败：", e);
  }
}

function startEntryWatch() {
  stopEntryWatch();
  const el = entryEl();
  if (!el) return;
  // 入口是用 left/top 像素定位的（useFloatingWindow 拖动时直接改内联样式），
  // 所以盯 style/class 就能实时跟上；ResizeObserver 管窗口变化。
  if (typeof MutationObserver !== "undefined") {
    entryObserver = new MutationObserver(() => {
      syncBall();
      // 贴边是 **CSS 过渡**：内联 left/top 只在开始时改一次，之后每一帧的位置变化不会触发
      // 属性观察器。只靠观察器的话，星球本体会停在原地，等过渡结束才「闪现」靠边
      // （用户实测反馈：「松开手后标注有动画，星球本身没有」）。所以过渡期间逐帧跟随。
      if (el.classList.contains("fw-snapping")) followSnap();
    });
    entryObserver.observe(el, { attributes: true, attributeFilter: ["style", "class"] });
  }
  if (typeof ResizeObserver !== "undefined") {
    entryResizeObserver = new ResizeObserver(() => syncBall());
    entryResizeObserver.observe(el);
  }
  window.addEventListener("resize", syncBall);
}

/** 贴边补间进行中：每帧把球态对齐到入口当前位置，直到 `fw-snapping` 被摘掉 */
function followSnap() {
  if (snapFollowRaf) return;
  const step = () => {
    syncBall();
    const el = entryEl();
    if (ballMode.value && el?.classList.contains("fw-snapping")) {
      snapFollowRaf = requestAnimationFrame(step);
    } else {
      snapFollowRaf = 0;
      syncBall(); // 收尾再对齐一次，避免停在过渡的中间值上
    }
  };
  snapFollowRaf = requestAnimationFrame(step);
}

function stopEntryWatch() {
  entryObserver?.disconnect();
  entryObserver = null;
  entryResizeObserver?.disconnect();
  entryResizeObserver = null;
  if (snapFollowRaf) cancelAnimationFrame(snapFollowRaf);
  snapFollowRaf = 0;
  window.removeEventListener("resize", syncBall);
}

/**
 * 关闭动画进行中（相机拉回 overview），防止重复关闭/重复交互。
 *
 * 声明位置要在下面的球态 watcher **之前**：那个 watcher 带 `immediate: true`，
 * setup 期间就会同步跑一次「进入球态」分支并写这两个 ref —— 写在后面就是
 * 「Cannot access 'closing' before initialization」：整个球态分支被打断
 * （入口对齐、跟随入口拖动、球态数据刷新全部不生效），而且只有从设置页回来
 * （ballLive 已经是 true、球态 watcher 首次就命中）才会暴露。
 */
const closing = ref(false);
/** 收起第一段「收势」进行中（还没开始整体淡出） */
const settling = ref(false);

/**
 * 球态与展开态之间的切换。
 *
 * 球态 = 真实场景缩到入口尺度常驻：低帧率、慢速自转、只显示抽象态（球 + 融合环）。
 * 它替代了「关掉就不渲染」的旧行为 —— 入口小球从此就是这颗星球本身，而不是另画的一张图。
 */
watch(
  ballMode,
  async (on) => {
    if (on) {
      planet.setPaused(false);
      planet.setLowPower(true);
      planet.setIdleSpin(true);
      // 密度低一档即可（小球不该扛信息），但保留话题点：这样它读起来是「缩小的星球」，
      // 而不是一个只有环的空壳
      planet.setReveal(BALL_REVEAL);
      /**
       * 球态不进 armed（不开过渡）：拖动要跟手，而且**不能**让第一次赋值被当成过渡起点。
       * 踩过的坑：`.planet-view.armed .planet-stage` 的过渡规则写在球态规则之后、特异性相同，
       * 会把它盖掉 —— 于是球态的 scale 停留在过渡起点（1），小球看起来没缩下去。
       */
      stageArmed.value = false;
      layersIn.value = false;
      curtainOn.value = false;
      // 球态自带球布局；尾段那个类到此为止（两者是同一种布局，交接不跳）
      tailMode.value = false;
      closing.value = false;
      settling.value = false;
      // 等 `ball` 类落到 DOM：画布尺寸要变成球的大小，之后测出来的球体半径才是小球的
      // 真实半径（否则会拿全屏尺寸去算尺度，球会被画得很小）
      await nextTick();
      // 再确认一次布局真的落到球尺寸：被打断的收起留下的全屏几何绝不能用来写尺度
      // （否则球会被乘成几个像素的点，用户看到的是「星球消失了」）
      syncBallWhenLaidOut();
      startEntryWatch();
      // 回到球态时顺手刷一次数据（有保鲜期，不会每次都拉）
      void loadBallData(true);
    } else {
      planet.setLowPower(false);
      planet.setIdleSpin(false);
      stopEntryWatch();
    }
  },
  { immediate: true },
);
// 场景返回的是 ref 容器对象（普通对象里的 ref 在模板中不会自动解包），
// 这里显式取值：否则 HUD 会渲染出 ref 对象本身。
const webglOK = computed(() => planet.webglOK.value);
const fpsText = computed(() => planet.fps.value);
const cameraStateText = computed(() => planet.cameraState.value);
/** 指针指向的话题点（场景 raycast 结果）与它的简短名称 */
const hoverTopicId = computed(() => planet.hoverTopicId?.value ?? null);
const hoverLabel = computed(() => planet.hoverLabel?.value ?? null);
/** 悬停标签的定位样式（在脚本里算，模板里不写嵌套模板字符串） */
const hoverLabelStyle = computed(() =>
  hoverLabel.value ? { left: hoverLabel.value.x + "px", top: hoverLabel.value.y + "px" } : undefined,
);
/** 选中话题的固定名称标签（悬停标签优先显示，避免两个标签叠在一起） */
const selectedLabel = computed(() => planet.selectedLabel?.value ?? null);
const selectedLabelStyle = computed(() =>
  selectedLabel.value ? { left: selectedLabel.value.x + "px", top: selectedLabel.value.y + "px" } : undefined,
);
const topics = ref<TopicFingerprint[]>([]);
/**
 * 浏览会话：序列 / 游标 / 展示窗口（见 planet/browseSession.ts）。
 * 星球上「同时显示哪些话题」由它决定，渲染层只负责画；总话题数无上限，
 * 同屏数量固定 ≤ VISIBLE_CAPACITY（融合环 shader 的 uniform 上限是 16）。
 */
const browse = new PlanetBrowseSession({ capacity: VISIBLE_CAPACITY });
/** 当前展示窗口里的 topic_id（用于断言 / 调试，也用于判断话题是否已经可见） */
const windowTopicIds = ref<(string | null)[]>([]);
/** 后端浏览游标：只有它知道「下一批」是什么、以及是否已经绕完一圈 */
let nextCursor: string | null = null;
let sessionSeed: number | null = null;
let prefetching = false;
const detail = ref<TopicDetail | null>(null);
const detailLoading = ref(false);
/** 详情加载失败原因。与「从这里继续」的 anchorError 分开：两者来源不同，不能互相覆盖 */
const detailError = ref("");
/** 详情失败的技术细节（首行只给人话，见 loadErrorDetail 的说明） */
const detailErrorDetail = ref("");
/** 详情请求序号：只接受「最后一次点击」的结果，慢请求返回不得覆盖（竞态防护） */
let detailSeq = 0;
/** 收起动作的代号：期间被重新打开时，旧收尾的续行必须放弃写状态与 emit */
let closeEpoch = 0;
const search = ref("");
const selectedFragmentId = ref<string | null>(null);
/** 「正在收起」= 收势或淡出任一阶段：这段时间里画布/列表/起点等交互都要锁住 */
const isClosing = computed(() => closing.value || settling.value);
/**
 * 星球层正处在缩放中间态（入口小球 ↔ 全屏 Planet 的路上）。
 * 这时画布上的坐标不再对应真实球面（raycast 会把点算偏），所以命中与聚焦必须锁住；
 * 缩放结束后（或根本没能对齐、走的是降级路径）就恢复可交互。
 */
const stageMidScale = computed(() => {
  const k = stageStyle.value["--stage-k"];
  return k !== undefined && k !== "1";
});
/** 入场：铺底之后内容层淡入（用类切换而不是 keyframes，见样式注释） */
const entered = ref(false);
/** 首次数据加载失败原因（话题列表/位置）——不能只留一个空球让用户猜 */
const loadError = ref("");
/**
 * 失败的技术细节（接口路径、状态码、原始响应）。
 *
 * 第四阶段：普通模式的第一行必须是**人话**。实测截图里第一行是
 * 「星球数据加载失败：/api/planet/overview -> 500: {...}」——那是给排查看的。
 * 细节不删除（用户与排查都要用），但折叠在后面，不在第一眼抢注意力。
 */
const loadErrorDetail = ref("");
/** 首次数据仍在加载：长等待要有明确提示，不以空球冒充完整内容 */
const dataLoading = ref(false);
/** 右侧话题边栏：默认收起；点击话题点/展开按钮展开，点击收起按钮收起 */
const panelOpen = ref(false);
/** 面板页签：话题 / 知识 / 实体；知识/实体页签自动进入管理模式（面板 640px 宽） */
const activeTab = ref<"topic" | "knowledge" | "entity">("topic");
const manageMode = ref(false);
/** 实体页签挂载后按 node_id 自动打开的实体卡 */
const entityOpenId = ref("");
/** 画布尺寸观察器：边栏开合/窗口缩放时同步 WebGL 渲染尺寸 */
let canvasObserver: ResizeObserver | null = null;
/** 边栏开合导致画布中心偏移后的重对焦定时器 */
let recenterTimer = 0;

/* ---- 主题同步：utils/theme.ts 写 html[data-theme]，planet.setTheme 换 WebGL 配色 ---- */
/** 读取当前主题：tokens.css 依据 data-theme 切变量，缺省按 dark */
function readTheme(): "dark" | "light" {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

/** 场景是否已初始化（init 之后才有 scene/材质可换色，observer 回调前守卫） */
let planetReady = false;
let themeObserver: MutationObserver | null = null;

function applyPlanetTheme() {
  if (!planetReady) return;
  planet.setTheme(readTheme());
}

function startThemeObserver() {
  if (themeObserver) return;
  themeObserver = new MutationObserver(applyPlanetTheme);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
}

function stopThemeObserver() {
  themeObserver?.disconnect();
  themeObserver = null;
}

/* ---- 右侧话题边栏：画布尺寸同步 + 开合后重对焦 ---- */
function startCanvasObserver() {
  if (typeof ResizeObserver === "undefined" || !canvasRef.value) return;
  canvasObserver = new ResizeObserver(() => planet.resize());
  canvasObserver.observe(canvasRef.value);
}

function stopCanvasObserver() {
  canvasObserver?.disconnect();
  canvasObserver = null;
}

/** 展开/收起右侧边栏（收起为细边 + 展开钮，展开为 340px 面板）。 */
function togglePanel() {
  if (isClosing.value || stageMidScale.value) return;
  panelOpen.value = !panelOpen.value;
  // 画布宽度随边栏动画变化，结束后若仍有焦点话题，重对焦到新画布中心
  const focused = planet.selectedTopicId.value;
  if (focused) scheduleRecenter(focused);
}

/**
 * 边栏开合后把焦点话题重新居中到新画布中心。
 * 旧规则是「等 380ms 动画结束再补一次移动」，用户会看到明显两次位移；
 * 现在改成与 320ms 宽度过渡同时发生的短补间，整个过程只有一次连续移动。
 */
function scheduleRecenter(topicId: string, delay = 0) {
  window.clearTimeout(recenterTimer);
  recenterTimer = window.setTimeout(() => {
    if (isClosing.value) return;
    if (planet.selectedTopicId.value !== topicId) return;
    planet.focusTopic(topicId, [], { duration: 210, wave: false });
  }, delay);
}

/** 面板宽度变化（管理模式开合/页签切换/实体联动）后，把焦点话题重对焦到新画布中心。 */
function onPanelWidthChange() {
  const focused = planet.selectedTopicId.value;
  if (focused) scheduleRecenter(focused);
}

onMounted(async () => {
  window.addEventListener("keydown", onKeydown);
  // 仅开发构建：给本地视觉验收脚本一个只读窗口快照（生产构建里这段不存在）。
  // 不改任何状态，也不暴露任何写入口。
  if (import.meta.env.DEV) {
    (window as unknown as Record<string, unknown>).__qioPlanetWindow = () => ({
      // 直接读渲染层与浏览会话：即使视图层状态没刷新，也能看出数据是否真的换了
      ids: planet.windowTopicIds(),
      refIds: windowTopicIds.value,
      visible: windowTopicIds.value.filter(Boolean).length,
      capacity: VISIBLE_CAPACITY,
      debug: planet.debugState(),
      session: browse.debugSnapshot(),
      sessionIds: browse.windowSlots().map((s) => s?.topic_id ?? null),
    });
    // 连续体诊断（只读）：验收脚本据此判断「小球尺度 → 全屏构图」是否真的连续
    (window as unknown as Record<string, unknown>).__qioPlanetStage = () => ({
      phase: planetContinuum.phase,
      k: stageStyle.value["--stage-k"] ?? null,
      tx: stageStyle.value["--stage-tx"] ?? null,
      ty: stageStyle.value["--stage-ty"] ?? null,
      sphere: planet.sphereScreenRect(),
      origin: planetContinuum.origin,
      target: planetContinuum.target,
      layers: layersIn.value,
      entered: entered.value,
      ball: ballMode.value,
      /** 当前选中的话题（展开后应当聚焦「目前在聊的那个话题」） */
      selected: planet.selectedTopicId.value,
      /**
       * 选中话题在**视口**里的投影锚点（标签就是按它定位的，标签自身有 46px 的 CSS 偏移）。
       * 给验收/探针用：判断「打开后当前话题有没有落在画布正中心」。
       */
      selectedLabel: selectedLabel.value,
      /** 演示倍率（1 = 真实时长；?planetdemo=N 时为 N） */
      demoSpeed: SPEED,
      debug: planet.debugState(),
    });
  }
  planet.init();
  // 一开始就接近最终构图：不再出现「远景小球 → 明显放大」这一段
  planet.primeCamera("planet");
  planetReady = true;
  applyPlanetTheme();
  startThemeObserver();
  startCanvasObserver();
  /**
   * 真实渲染就绪 → 入口小球可以让位给场景本体了。
   * WebGL 不可用（降级环境）时不置位：那时入口继续由 `PlanetOrb`（2D 压缩态）承担，
   * 不假装「这是同一个渲染」。
   */
  if (planet.webglOK.value) planetContinuum.ballLive = true;
  // 对话页常驻的小球状态：不打开也要渲染（缩在入口位置、慢速自转）
  if (!props.open) {
    syncBallWhenLaidOut();
    return;
  }
  await enterWithData();
});

/**
 * 「打开」时的数据与转场编排。
 *
 * 两种情况必须分开，否则要么慢、要么动画被吞：
 *
 * - **入口小球已经喂过数据**（对话页常驻小球，默认情况）：数据已经在场景里了，
 *   不该再等一次刷新 —— 先长大，长大**结束之后**再聚焦当前在聊的话题。
 *   实测：以前这一步会白等一次刷新，点击到开始长大要 1240ms、到场景接管 1891ms
 *   （设计值约 920ms）。顺带这样也正好是「放大后聚焦」的顺序。
 * - **没有球态数据**（首屏直接打开 / WebGL 不可用）：还是得先等数据与渲染就绪，
 *   否则数据装配会和长大抢主线程，动画一帧都画不出来（实测表现为星球直接跳到位）。
 */
async function enterWithData() {
  if (ballDataLoaded) {
    await runEnter();
    return;
  }
  const loading = loadData();
  await runEnter(loading);
  await loading;
}

/**
 * 复用同一个星球实例：关闭时不卸载（省掉每次重建 WebGL 场景的 1.5–3.4s 冷启动），
 * 由父级用 v-show 隐藏，`open` 变化时在这里重置状态。
 */
watch(
  // 同时盯 open 与 seq：收起动画中再次点入口时 open 一直是 true，
  // 只盯 open 会漏掉「打开状态下又打开一次」，界面就会停在上一次的收起状态里。
  () => [props.open, props.seq] as const,
  async ([isOpen], prev) => {
    if (!isOpen) {
      // 关掉不是「停止渲染」，而是回到入口小球状态（球态要一直活着）
      syncBallWhenLaidOut();
      return;
    }
    if (prev && prev[0] === isOpen && prev[1] === props.seq) return;
    await reopen();
  },
);

/** 再次打开：重置上一次的收尾状态，重新入场、重新按落点定位 */
async function reopen() {
  // 作废「进行中的收起」：它的续行还会写 closing 并 emit，必须让它认到已被取代
  closeEpoch += 1;
  // 收起期间发起的「转回朝向」补间也要停掉，否则它会继续转，和这一次展开打架
  planet.cancelAnimation();
  closing.value = false;
  settling.value = false;
  entered.value = false;
  layersIn.value = false;
  anchorError.value = "";
  detailError.value = "";
  anchorBusy.value = false;
  planet.setPaused(false);
  planet.primeCamera("planet"); // 回到接近最终构图，避免从上一帧残留位置开始
  await nextTick();
  planet.resize(true); // 隐藏期间绘图缓冲可能被丢弃：强制重建一次
  await enterWithData();
}

/**
 * 进入编排（三阶段）。`entered` 管铺底、`stageStyle` 管体量、`layersIn` 管玻璃浮层，
 * 三者由同一份阶段状态与同一批时长令牌驱动 —— 不是三个各自计时的动画在赛跑。
 */
async function runEnter(dataReady?: Promise<unknown>) {
  const epoch = beginOpen();
  continuumEpoch = epoch;
  /**
   * 记下「打开前的球体朝向」：收起时要把同一段旋转倒着走完
   * （用户要求：展开与收回都同时有旋转）。必须在这里 —— 紧接着的聚焦会转动球体。
   */
  planet.markReturnOrientation();
  entered.value = false;
  layersIn.value = false;
  curtainOn.value = false;
  stageArmed.value = false;
  planet.setReveal(0);
  /**
   * 立即可见：球态 → 展开态之间不能有「什么都没有」的一帧。
   *
   * 以前这里靠一个 180ms 的「激活阶段」过渡，结果打开变成两段（激活一段、长大一段）——
   * 用户实测反馈「进入星球时有两段动画」。现在同帧可见，激活段只在**冷路径**
   * （数据还没到）里真实存在，作为「小球等着内容」的停顿。
   */
  entered.value = true;
  // 顺序要紧：先等 `ball` / 尾段类撤掉（画布从「球大小」回到全屏尺寸），再重建绘图缓冲、
  // **再**量尺度。反过来的话会拿球大小的画布去算全屏构图，球会被算得很小。
  tailMode.value = false;
  await nextTick();
  planet.resize(true);
  // 起点：把星球层摆到入口小球的位置与尺度 —— 此刻它看起来就是那枚小球
  alignStageToEntry(planetContinuum.origin);
  const expandMs = tokenMs("--mo-3-expand", OPEN_MS);
  /**
   * 冷路径（首次打开 / WebGL 刚就绪）：先等数据，**数据一到就转向当前话题**。
   *
   * 顺序很重要：这一步刻意放在「等下一帧 / 让过渡生效」之前 ——
   * 否则转向会被几帧的等待夹住，读起来就是「先长大、停一下、再回正」两段动作。
   */
  if (!ballDataLoaded && !prefersReducedMotion() && dataReady) {
    await Promise.race([dataReady.catch(() => {}), sleep(2500)]);
    if (!isCurrent(epoch)) return;
    focusInitialTopic(expandMs);
  }
  // 等第一帧真正把「入口尺度」画出来，再打开过渡（见 stageArmed 的说明）
  await new Promise<void>((r) => requestAnimationFrame(() => r()));
  stageArmed.value = true;
  await nextTick();
  // 强制一次样式结算，把「过渡已经生效」固定在一次真实的 style resolution 上。
  // 不这样做的时候：WebGL 冷启动会阻塞主线程，浏览器可能把「类变化（过渡生效）」与
  // 「变量变化（scale 0.13 → 1）」合并成同一次结算 —— 过渡被跳过，星球直接跳到位。
  // 实测探针里出现过整段采样没有任何中间态（0.13 直接到 1.00）。
  if (stageRef.value) void getComputedStyle(stageRef.value).transitionDuration;
    /**
     * 落点聚焦：从这一刻开始转向「当前在聊的话题」，并且与长大用同一时长 ——
     * 两个动作同刻收束，读起来是一段连续动作（先长大、再回正就是两段，用户反馈过）。
     * 热路径（入口小球已经喂过数据）走到这里就直接转向，紧接着长大。
     */
    if (ballDataLoaded) focusInitialTopic(expandMs);
    // 主线程还在冷启动里的话，先等它缓过来（否则这一整段动画一帧都画不出来）
    if (!prefersReducedMotion()) {
      await waitForSmoothFrames(2, 400);
      if (!isCurrent(epoch)) return;
    }
    // B 体量展开：同一个对象连续长大到全屏构图，信息密度 0 → 1
    advanceTo(epoch, "expanding");
    // 对话页与长大同时开始渐隐（同一刻、同一时长）
  curtainOn.value = true;
  stageStyle.value = { "--stage-tx": "0px", "--stage-ty": "0px", "--stage-k": "1" };
  trackRingWidth(expandMs);
  await revealRamp(0, 1, expandMs);
  if (!isCurrent(epoch)) return;
  // C 场景接管：玻璃浮层进入，话题进入可交互密度
  advanceTo(epoch, "ready");
  layersIn.value = true;
}

onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
  if (import.meta.env.DEV) {
    delete (window as unknown as Record<string, unknown>).__qioPlanetWindow;
    delete (window as unknown as Record<string, unknown>).__qioPlanetStage;
  }
  stopThemeObserver();
  stopCanvasObserver();
  stopEntryWatch();
  window.clearTimeout(recenterTimer);
  /**
   * 离开对话页（去设置页 / 路由切换）必须把连续体收干净。
   *
   * 不做的后果（实测）：星球开着时切走，phase 停在 ready，PlanetDock 的 handedOff
   * 永远是 true —— `.dock.handed { opacity: 0; pointer-events: none }`，球看不见也点不到，
   * 而且没有任何路径能把它救回来。同一次调用还作废被打断的转场续行、把入口交回 2D 压缩态。
   */
  abandonContinuum();
});

function onKeydown(e: KeyboardEvent) {
  if (!props.open) return; // 已关闭但实例常驻：不能再响应 Esc
  if (e.key !== "Escape") return;
  const t = e.target as HTMLElement | null;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  // 最上层优先：边栏是用户刚打开的一层，Esc 先收它，再按才收起整个星球
  if (panelOpen.value) {
    panelOpen.value = false;
    return;
  }
  close();
}

async function loadData() {
  loadError.value = "";
  dataLoading.value = true;
  try {
    // 侧栏列表仍然走准确导航（List/Search 的职责），星球本体只取轻量概览 + 第一批窗口。
    const [t, overview] = await Promise.all([api.listTopics(), api.planetOverview()]);
    // 等待期间用户已发起关闭：不再推进相机/加载详情，避免打断拉回 tween
    if (isClosing.value) return;
    topics.value = t.topics;
    const page = await api.planetBrowse({
      direction: "forward",
      count: VISIBLE_CAPACITY,
      current_topic_id: session.currentTopicId ?? null,
    });
    if (isClosing.value) return;
    nextCursor = page.next_cursor;
    sessionSeed = page.seed;
    browse.setSequence(page.items);
    // 打开时的布局：在整个球面上随机铺开（同一会话种子可复现），
    // 之后随着旋转换进来的话题同样在背面随机落点（见 swap 时的 place）。
    browse.fill(
      performance.now(),
      spreadPositions(VISIBLE_CAPACITY, page.seed).map(
        (dir) => [dir.x, dir.y, dir.z] as [number, number, number],
      ),
    );
    // 起点话题必须能被看见：后端已经把它排在第一批首位；
    // 万一它不在这一批（话题刚被创建/被过滤），这里显式注入窗口。
    const anchorTopic = pickTopicSummary(overview.topics, session.currentTopicId);
    if (anchorTopic && !browse.windowSlots().some((item) => item?.topic_id === anchorTopic.topic_id)) {
      browse.pinTopic(anchorTopic);
    }
    planet.attachBrowse(browse, { seed: page.seed, onWindowChange: syncWindow });
    planet.setTopics([]);
    syncWindow();
    // 落点不在这里聚焦：展开路径要「长大 + 转到话题」同时进行（见 runEnter 的 B 阶段），
    // 在这里再聚焦一次会变成两段。重试路径自己补一次（见 retryLoad）。
  } catch (e) {
    if (isClosing.value) return;
    loadError.value = "星球数据加载失败";
    loadErrorDetail.value = (e as Error).message;
  } finally {
    dataLoading.value = false;
  }
}

function pickTopicSummary(
  list: PlanetTopicSummary[],
  topicId: string | null | undefined,
): PlanetTopicSummary | null {
  if (!topicId) return null;
  return list.find((item) => item.topic_id === topicId) ?? null;
}

/**
 * 展示窗口变化后：同步可见话题、预取下一页、把选中态锁在窗口里。
 * 视觉连续性优先于严格数据顺序——这里的顺序不代表任何「相关性」。
 */
function syncWindow() {
  windowTopicIds.value = planet.windowTopicIds();
  if (!prefetching && browse.needsMore() && nextCursor) void prefetchMore();
}

async function prefetchMore() {
  if (prefetching || !nextCursor) return;
  prefetching = true;
  try {
    const page = await api.planetBrowse({
      cursor: nextCursor,
      direction: "forward",
      count: VISIBLE_CAPACITY,
      exclude: browse.windowSlots().filter(Boolean).map((item) => item!.topic_id),
      seed: sessionSeed,
    });
    nextCursor = page.next_cursor;
    browse.appendSequence(page.items);
  } catch (e) {
    // 预取失败不影响已经看到的窗口：下一次滚动会再试
    console.warn("[planet] 预取下一批话题失败：", e);
  } finally {
    prefetching = false;
  }
}

/**
 * 打开时的落点：起点没变就回到上次浏览的话题；起点明确变化则以起点为准。
 * 有落点时一次到位（相机已按最终构图预备，只做朝向收敛），不再「先总览再聚焦」。
 */
/**
 * 落点：起点没变就回到上次浏览的话题，起点明确变化则以起点为准。
 *
 * `duration` 由调用方给：展开时传**长大的时长**，让「长大」与「转到当前话题」
 * 同刻起、同刻止 —— 用户要的是一段连续动作，不是「先长大、再回正」两段。
 */
function focusInitialTopic(duration = OPEN_MS) {
  const anchorId = session.currentTopicId ?? null;
  const signature = anchorSignatureOf(anchorId, session.anchorFragmentId);
  const anchorChanged = planetSession.anchorSignature !== signature;
  planetSession.anchorSignature = signature;

  const browsed = planetSession.browsedTopicId;
  const has = (id: string | null) => Boolean(id) && browse.windowSlots().some((t) => t?.topic_id === id);
  /**
   * 打开时以**当前在聊的话题**为中心（用户实测要求：「打开前它不在最中心，就在打开的过程中转到中心」）。
   *
   * 这里原来是「起点没变就回到上次浏览的话题」—— 于是用户关掉星球前随手看过另一个话题，
   * 再打开时中心是那个「看过的话题」，当前在聊的话题反而留在旁边，看起来像「没有转正」。
   * 现在当前话题优先，浏览记忆只在**它不可见 / 压根没有当前话题**时兜底。
   */
  const target = has(anchorId) ? anchorId : !anchorChanged && has(browsed) ? browsed : anchorId;

    if (has(target)) {
      planet.selectedTopicId.value = target;
      browse.lock(target);
      planet.focusTopic(target!, [], { duration });
    } else {
      planet.go("planet", null, duration);
    }
  }

const filteredTopics = ref<TopicFingerprint[]>([]);
watch([topics, search], () => {
  const q = search.value.trim().toLowerCase();
  filteredTopics.value = q
    ? topics.value.filter((t) => t.title.toLowerCase().includes(q) || t.keywords.some((k) => k.toLowerCase().includes(q)))
    : topics.value;
}, { immediate: true });

async function loadDetail(topicId: string) {
  const seq = ++detailSeq;
  detailLoading.value = true;
  detailError.value = "";
  selectedFragmentId.value = null;
  // 换了话题：收起上一段原文，避免把别的话题的历史显示在当前的详情里
  rawFragmentId.value = null;
  rawMessages.value = [];
  rawTotal.value = 0;
  rawError.value = "";
  try {
    const d = await api.getTopicDetail(topicId);
    // 期间用户已切到别的话题：丢弃过期响应
    if (seq !== detailSeq || planet.selectedTopicId.value !== topicId) return;
    detail.value = d;
  } catch (e) {
    if (seq === detailSeq) {
      detail.value = null;
      // 失败必须自己可见：此时没有详情可展示，错误不能挂在「有详情」的分支里
      detailError.value = "加载话题详情失败";
      detailErrorDetail.value = (e as Error).message;
    }
  } finally {
    if (seq === detailSeq) detailLoading.value = false;
  }
  await revealDetailArea();
}

/**
 * 话题列表很长时，详情（或它的错误）会落在列表下方看不见的位置，
 * 用户点完话题只看到行高亮、不知道结果。这里只把详情区域带回视野：
 * 已经在视野内时 `block:"nearest"` 不会移动，符合「页面变化尽量少」。
 */
async function revealDetailArea() {
  await nextTick();
  const el = document.querySelector<HTMLElement>(".detail, .detail-error, .detail-loading");
  if (!el || typeof el.scrollIntoView !== "function") return;
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  el.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
}

/** 详情加载失败后的重试：重新按当前选中话题拉一次 */
function retryDetail() {
  const id = planet.selectedTopicId.value;
  if (id) void loadDetail(id);
}

/** 首次数据加载失败后的重试：重新取数据，并补一次落点聚焦（展开路径才由 runEnter 负责） */
async function retryLoad() {
  loadError.value = "";
  await loadData();
  if (!loadError.value) focusInitialTopic();
}

function selectTopic(topicId: string) {
  if (isClosing.value || stageMidScale.value) return;
  planet.selectedTopicId.value = topicId;
  ensureTopicInWindow(topicId);
  planet.focusTopic(topicId, []);
}

/**
 * 保证某个话题在展示窗口里（搜索命中 / 列表点击）。
 * 这正是 spec 第 24 条：搜索找到的话题「加入当前/下一组展示窗口」，
 * 而不是要求它本来就待在某个固定地点。
 */
function ensureTopicInWindow(topicId: string) {
  if (browse.windowSlots().some((item) => item?.topic_id === topicId)) return;
  const summary = topics.value.find((item) => item.topic_id === topicId);
  if (!summary) return;
  browse.pinTopic({
    topic_id: summary.topic_id,
    title: summary.title,
    fragment_count: summary.fragment_count,
    last_activity: summary.last_activity,
    summary_preview: summary.summary_preview,
  });
  planet.refreshWindow(performance.now());
  syncWindow();
}

function onCanvasClick(e: MouseEvent) {
  if (isClosing.value || stageMidScale.value) return;
  const focused = planet.handleClick(e.clientX, e.clientY);
  if (focused) {
    // 收起态点击话题点：同时展开边栏，画布中心左移 → 动画结束后重对焦
    const wasCollapsed = !panelOpen.value;
    panelOpen.value = true;
    if (wasCollapsed) scheduleRecenter(planet.selectedTopicId.value ?? "");
  }
}

function onCanvasDblClick() {
  if (!isClosing.value && !stageMidScale.value) planet.go("planet");
}

watch(() => planet.selectedTopicId.value, (id) => {
  // 浏览记忆：重开星球时用它回到上次看的地方（见 planetSession 注释）
  if (id) planetSession.browsedTopicId = id;
  // 选中 = 正在查看：查看期间这个槽位不会被回收（spec 第 52 条）
  browse.lock(id ?? null);
  if (id && id !== detail.value?.topic_id) loadDetail(id);
});

// 知识纠错：修正/删除
const knowledgeEditingId = ref<string | null>(null);
const knowledgeDraft = ref("");
/**
 * 知识修正 / 删除的反馈状态：以前失败只写 console.error，界面什么也不显示，
 * 用户会以为改动生效了。现在失败留在对应条目里，带原因与重试。
 */
const knowledgeFeedback = useActionFeedback();

function startEditKnowledge(k: { id: string; content: string }) {
  knowledgeEditingId.value = k.id;
  knowledgeDraft.value = k.content;
}

async function saveKnowledgeEdit(k: { id: string }) {
  const content = knowledgeDraft.value.trim();
  if (!content) return;
  const key = `knowledge:${k.id}`;
  await knowledgeFeedback.run(
    key,
    async () => {
      await api.reviseKnowledge(k.id, content);
      knowledgeEditingId.value = null;
      if (detail.value) await loadDetail(detail.value.topic_id);
    },
    { okText: "已保存", failText: "修改没有保存，内容未改变" },
  );
}

/**
 * 归档知识条目。
 * 第四阶段：不再用浏览器原生 `window.confirm`（它说不清「动作 + 影响 + 后果」，
 * 而且风格与 QIO 完全无关）。确认由 `QConfirm` 就地展开，说明归档的真实影响。
 */
async function archiveKnowledge(k: { id: string; content: string }) {
  confirmKnowledgeId.value = null;
  const key = `knowledge:${k.id}`;
  await knowledgeFeedback.run(
    key,
    async () => {
      await api.revokeKnowledge(k.id);
      if (detail.value) await loadDetail(detail.value.topic_id);
    },
    { okText: "已归档", failText: "归档没有完成" },
  );
}

/** 正在就地确认归档的知识条目 id（null = 没有确认在进行） */
const confirmKnowledgeId = ref<string | null>(null);

/** 锚点设置进行中 / 失败原因（失败必须可见、可重试，且不得假装已切换） */
const anchorBusy = ref(false);
const anchorError = ref("");

/** 用户当前选中的历史片段（用于「已选择历史位置」提示，不暴露内部 id） */
const selectedFragmentSummary = computed(() => {
  if (!selectedFragmentId.value) return "";
  const frag = detail.value?.fragments.find((f) => f.fragment_id === selectedFragmentId.value);
  const text = (frag?.summary || "").trim().replace(/\s+/g, " ");
  if (!text) return "（该片段暂无摘要）";
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
});

/* ---- 片段原文：按需加载 + 分页（spec 第 9~11 条） ---- */
/** 一页取多少条原文：够看一段，不把整段历史一次拉回来 */
const RAW_PAGE = 20;
/** 当前展开原文的片段（同时只展开一个，长列表才不会被撑爆） */
const rawFragmentId = ref<string | null>(null);
const rawMessages = ref<{ id: string; role: string; content: string; created_at: string }[]>([]);
const rawTotal = ref(0);
const rawLoading = ref(false);
const rawError = ref("");
const rawHasMore = computed(() => rawMessages.value.length < rawTotal.value);

const ROLE_LABELS: Record<string, string> = {
  user: "你",
  assistant: "QIO",
  tool: "工具",
  system: "系统",
};

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/** 知识状态用中文说，不把 draft / pending_review 这类内部状态名丢给用户 */
const KNOWLEDGE_STATES: Record<string, string> = {
  draft: "草稿",
  pending_review: "待确认",
  verified: "已验证",
  active: "已生效",
  expired: "已过期",
  revoked: "已归档",
};

function knowledgeStateLabel(state: string): string {
  return KNOWLEDGE_STATES[state] ?? "状态未知";
}

/**
 * 状态色统一走 `.qio-state` 的语义档（不再各写一套 k-state 变体）：
 * 生效/已验证 = ok，待确认/草稿 = warn，已归档/过期 = quiet（安静的事实，不是警报）。
 */
function knowledgeStateTone(state: string): string {
  if (state === "active" || state === "verified") return "ok";
  if (state === "pending_review" || state === "draft") return "warn";
  return "quiet";
}

/** 「最近活动」这类时间用相对说法更像人话；拿不到就退回日期 */
function formatWhen(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  const p = (n: number) => String(n).padStart(2, "0");
  const clock = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (days <= 0) return `今天 ${clock}`;
  if (days === 1) return `昨天 ${clock}`;
  if (days < 30) return `${days} 天前`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 详情顶部的一行事实：最近活动 / 片段数 / 消息数（都来自真实数据） */
const detailFacts = computed(() => {
  const d = detail.value;
  if (!d) return "";
  const parts: string[] = [];
  if (d.last_activity) parts.push(`最近活动 ${formatWhen(d.last_activity)}`);
  parts.push(`${d.fragments.length} 个片段`);
  if (typeof d.message_count === "number") parts.push(`${d.message_count} 条消息`);
  return parts.join(" · ");
});

/** 展开 / 收起某段的原文；展开时才真正读第一页 */
async function toggleRaw(fragmentId: string) {
  if (rawFragmentId.value === fragmentId) {
    rawFragmentId.value = null;
    return;
  }
  rawFragmentId.value = fragmentId;
  rawMessages.value = [];
  rawTotal.value = 0;
  rawError.value = "";
  await loadRawPage(0);
}

async function loadRawPage(offset: number) {
  const id = rawFragmentId.value;
  if (!id) return;
  rawLoading.value = true;
  rawError.value = "";
  try {
    const r = await api.fragmentMessages(id, offset, RAW_PAGE);
    // 期间用户切到了别的片段：丢弃过期响应（竞态防护）
    if (rawFragmentId.value !== id) return;
    rawMessages.value = offset === 0 ? r.messages : [...rawMessages.value, ...r.messages];
    rawTotal.value = r.total;
  } catch (e) {
    if (rawFragmentId.value === id) {
      rawError.value = `读取原文失败：${(e as Error).message}`;
    }
  } finally {
    rawLoading.value = false;
  }
}

/** 从某一段原文继续：选中它再走既有的「从这里继续」（旧片段不改，新建接续片段） */
async function continueFromFragment(fragmentId: string) {
  if (anchorBusy.value || isClosing.value) return;
  selectedFragmentId.value = fragmentId;
  await startHere();
}

/**
 * 选中的是不是「历史位置」：已封块的片段才是历史；当前开放片段是对话的最新位置，
 * 不该显示成「从这里继续（历史位置）」（与后端 AnchorService.focus_fragment 同一规则）。
 */
const selectedIsHistoric = computed(() => {
  if (!selectedFragmentId.value) return false;
  const frag = detail.value?.fragments.find((f) => f.fragment_id === selectedFragmentId.value);
  return Boolean(frag?.closed_at);
});
const selectedPositionLabel = computed(() =>
  selectedIsHistoric.value ? "已选择历史位置" : "已选择当前片段",
);
const selectedButtonHint = computed(() => {
  if (!selectedFragmentId.value) return "（整个话题）";
  return selectedIsHistoric.value ? "（这个历史位置）" : "（当前片段）";
});
/**
 * 「查看 → 进入」的分界（spec 第 22 / 53 / 54 条）：
 * 选中话题只是浏览，按这个按钮才是真正改变对话位置的动作。
 */
const startButtonLabel = computed(() => {
  if (anchorBusy.value) return "切换中…";
  if (!selectedFragmentId.value) return `进入「${detail.value?.name ?? "这个话题"}」`;
  return `从这里继续${selectedButtonHint.value}`;
});

/**
 * 面板里三个概念必须分开写清（任务05 D）：
 * - 浏览的话题：现在正在看的这个话题（可能只是浏览，不代表起点变了）；
 * - 选中的片段：用户在片段列表里点的那一个（还没生效）；
 * - 已生效的起点：服务端已经接受的起点（来自会话状态，不是本地猜测）。
 */
const browsedTopicName = computed(() => detail.value?.name ?? "（未选择）");
const selectedFragmentLabel = computed(() =>
  selectedFragmentId.value ? selectedFragmentSummary.value : "未选择片段（用这个话题的最新位置）",
);
const anchorLabel = computed(() => {
  const id = session.currentTopicId;
  if (!id) return "未设置（按最新位置继续）";
  const name =
    session.topicName ||
    topics.value.find((t) => t.topic_id === id)?.title ||
    id;
  const fragmentTitle = session.anchorFragment?.title;
  if (fragmentTitle) {
    return `${name} · ${fragmentTitle}${session.anchorHistoric ? "（历史位置）" : "（当前片段）"}`;
  }
  return `${name} · 最新位置`;
});

async function startHere() {
  if (isClosing.value || anchorBusy.value) return;
  if (!detail.value) return;
  const target = detail.value;
  const fragmentId = selectedFragmentId.value;
  anchorBusy.value = true;
  anchorError.value = "";
  try {
    // 只用后端返回的权威结果更新本地（标题 / 是否历史位置），
    // 避免与同一动作触发的 SSE ANCHOR 事件互相覆盖
    //
    // 两个明确不同的动作（spec 第 53 / 54 条）：
    //  - 选中了历史片段 → 从这里继续：旧片段保持不变，后端新建接续片段；
    //  - 没选片段 → 进入这个话题：从最新位置继续。
    const res = fragmentId
      ? await api.continueFromHistory(target.topic_id, fragmentId)
      : await api.setAnchor(target.topic_id, null);
    // setAnchorAndSync：起点真的变了就重新拉取可见消息。
    // 否则「从这里继续」到别的话题后，对话页还停在上一个话题的对话上。
    await session.setAnchorAndSync(
      res.topic_id || target.topic_id,
      res.fragment_id,
      target.name,
      res.fragment_id ? { id: res.fragment_id, title: res.fragment_title } : undefined,
      res.historic,
    );
  } catch (e) {
    // 失败：本地锚点不变、不关闭，用户可重试
    anchorError.value = `切换失败，未切换话题：${(e as Error).message}`;
    anchorBusy.value = false;
    // 星球已经被收起来了：错误必须回到对话页可见，否则用户以为起点已经切好
    if (isClosing.value) session.lastError = anchorError.value;
    return;
  }
  anchorBusy.value = false;
  await close();
}

/* ---- 星球记忆中心：三页签 + 管理模式 + 与知识/实体面板联动 ---- */
function switchTab(tab: "topic" | "knowledge" | "entity") {
  activeTab.value = tab;
  manageMode.value = tab !== "topic";
  entityOpenId.value = "";
  onPanelWidthChange();
}

/** 「管理模式」按钮：手动切换面板宽度并重对焦 */
function toggleManageMode() {
  manageMode.value = !manageMode.value;
  onPanelWidthChange();
}

/** 知识面板「聚焦话题」→ 切回话题页签并聚焦/加载该话题（详情由 selectedTopicId watcher 单一来源加载） */
function focusTopicFromPanel(topicId: string) {
  planet.selectedTopicId.value = topicId;
  ensureTopicInWindow(topicId);
  planet.focusTopic(topicId, []);
  panelOpen.value = true;
  switchTab("topic");
  // 同话题时 watcher 会跳过 reload，这里强制刷新以拿到面板编辑后的最新数据
  if (detail.value?.topic_id === topicId) loadDetail(topicId);
}

/** 话题详情实体标签 → 打开实体页签并自动展开该实体卡（按 node_id 匹配） */
function openEntityByNode(nodeId: string) {
  activeTab.value = "entity";
  manageMode.value = true;
  entityOpenId.value = nodeId;
  onPanelWidthChange();
}

/**
 * 收缩的体量运动：从全屏构图收回**入口小球此刻的位置与尺度**。
 *
 * 为什么要分两段（并且尾段由脚本接管）：
 *
 * 星球层的缩小是用 CSS `transform: scale()` 缩放整层画布实现的，而画布内容在浏览器里
 * 是按它**自己的 CSS 尺寸**栅格化的 —— 整层缩到 0.14 倍时，1 设备像素宽的轮廓线只剩
 * 0.14 个设备像素，采样大部分落空，看起来就是**断续的点**（用户反馈过「缩小后的星球的
 * 轮廓全是像素」）。pilot（docs/release-phase4.md §10.5）量下来：真实动画里现状的
 * 「缩放整层」并不会被吃掉轮廓（与「缩放投影」对照组逐档相同）；只有画面被冻住、
 * 浏览器不再按动画尺度重新栅格化时才会出现断续。所以尾段这段不是为了修一个可见缺陷，
 * 而是**加固**：结构上保证最后一段不可能被降采样。
 *
 * 试过「把绘图缓冲改小」（`setPixelRatio` 缩小）—— 那条路更差，而且坑不只一个：
 * 画布内容是按它自己的 CSS 尺寸绘制的，缓冲小于 CSS 尺寸只会被放大；那次实验还顺手
 * 踩了环宽补偿的换算基准（缓冲一变，`uWidthScale` 该给多少也跟着变），截图里小球
 * 直接变成一圈圈粗环。两点都说明「改缓冲」不是抓手。
 *
 * 真正的 1:1 只有一个办法：让画布的 CSS 尺寸等于它在屏幕上的尺寸 ——
 * 也就是切成**入口小球那套布局**（`.planet-view.tail`，画布 `--ball-size`）。
 *
 * 但换布局会打断 CSS 过渡：直接在球布局里改坐标，等于让过渡从当前位置重起一段，
 * 曲线会在中途折一下（用户对「两段动画」很敏感）。所以尾段由这里接管：
 * ① 用当前 computed transform 算出球**此刻**的屏幕球心与半径（读的是过渡的真实插值，
 *    不是我们自己估的进度）；
 * ② 换布局，并把同一组屏幕几何写回球布局坐标（同一帧完成，中间不会漏出错位的一帧）；
 * ③ 之后每帧用**同一条令牌曲线** `--ease-3-settle` 推进到终点。
 * 视觉路径与原来的 CSS 过渡逐点相同（变换在 (tx,ty,k) 上是线性的），只是换了个坐标系统、
 * 换了个渲染分辨率。终点就是入口小球自己的几何，交接给球态时一帧都不用跳。
 *
 * 返回 false = 这次收尾已被新的打开取代，调用方必须放弃后续步骤。
 */
async function runCollapseMotion(epoch: number, continuum: number): Promise<boolean> {
  const target = planetContinuum.target;
  const collapseMs = tokenMs("--mo-3-collapse", FADE_MS);
  const easeMs = tokenMs("--mo-3-expand", OPEN_MS);
  const ease = easePointsFromCssValue(
    getComputedStyle(rootRef.value ?? document.documentElement).getPropertyValue("--ease-3-settle"),
    CSS_SETTLE_FALLBACK,
  );
  // 全屏构图下球体的布局几何：换布局前量一次，尾段换算屏幕几何要用
  const full = planet.sphereScreenRect();
  const frame = layoutOrigin(stageRef.value);
  /**
   * 与体量收缩**同刻开始**：把球转回「打开前的朝向」。
   *
   * 时长按夹角缩放、封顶在收起窗口（见 usePlanetScene.rotateBack），
   * 所以小角度轻轻转、大角度在窗口内转完，两者都与收缩同时收束。
   */
  planet.rotateBack(collapseMs);
  // 起点：把整层摆到入口（写目标值）。此刻的 CSS 过渡就是「从全屏构图收回入口」。
  alignStageToEntry(target);
  if (!target || !full || !Number.isFinite(full.radius) || full.radius <= 0) {
    // 量不到球体几何（WebGL 不可用 / 场景未就绪）：退化成整层淡出，不假装连续
    trackRingWidth(collapseMs);
    await sleep(collapseMs);
    return epoch === closeEpoch && isCurrent(continuum);
  }

  const startCx = full.cx;
  const startCy = full.cy;
  const startR = full.radius;
  let t0 = 0;
  let switched = false;
  let ball: { cx: number; cy: number; radius: number } | null = null;

  const aborted = () => epoch !== closeEpoch || !isCurrent(continuum);
  const nextFrame = () => new Promise<number>((r) => requestAnimationFrame((now) => r(now)));

  for (;;) {
    const now = await nextFrame();
    if (aborted()) return false;
    // 过渡是在这一帧的样式结算里开始的：用第一帧当起点，尾段的曲线相位就与它对齐
    if (t0 === 0) t0 = now;
    const x = easeMs > 0 ? (now - t0) / easeMs : 1;
    const p = Math.max(0, Math.min(1, evalCubicBezier(ease, Math.min(1, x))));

    if (!switched) {
      applyRingWidth();
      const cur = currentStageTransform();
      if (cur) {
        const radius = cur.k * startR;
        if (radius <= target.r * TAIL_SWITCH_RATIO) {
          // ① 记下此刻的屏幕几何（过渡的真实插值）
          const center = layoutToScreen(startCx, startCy, cur, frame);
          // ② 换布局，并把同一组屏幕几何写回球布局坐标
          tailMode.value = true;
          stageArmed.value = false;
          await nextTick(); // `.tail` 生效：画布盒子变成球自己的尺寸
          if (aborted()) return false;
          ball = planet.sphereScreenRect();
          switched = true;
          if (ball) {
            stageStyle.value = stageStyleForScreenGeometry({ ...center, r: radius }, ball);
          }
          applyRingWidth();
          /**
           * ② 等新坐标落到 DOM，**再**重建绘图缓冲补那一帧。
           *
           * 顺序很重要：环宽补偿现在是渲染前按「画布此刻的 rect / 布局宽度」现算的
           * （见 usePlanetScene.setRingScreenWidth）。如果在这一步之前就补帧，
           * 渲染器读到的还是旧变换（整层缩放 0.13），算出 12px 的环 —— 那一帧看起来
           * 就是「一块实心亮斑」，用户实测反馈的「缩小途中闪现」正是它。
           */
          await nextTick();
          if (aborted()) return false;
          planet.resize(true); // 重建绘图缓冲并补一帧：此时布局、变换、环宽三者一致
        }
      }
    } else if (ball) {
      // ③ 同一条曲线的剩余部分，在球布局坐标里继续走
      const r = startR + (target.r - startR) * p;
      const cx = startCx + (target.cx - startCx) * p;
      const cy = startCy + (target.cy - startCy) * p;
      stageStyle.value = stageStyleForScreenGeometry({ cx, cy, r }, ball);
      applyRingWidth();
    }

    if (now - t0 >= collapseMs) return true;
  }
}

/**

/**
 * 收起（第四阶段：严格反向的连续体）。
 *
 * 1) 收势（`--mo-3-prepare`）：相机轻微后撤、玻璃浮层与面板收回、信息密度降到抽象态；
 * 2) 体量收缩（`--mo-3-collapse`）：同一个对象沿原路径缩回**此刻**入口所在的位置
 *    （悬浮球可能已被拖走或贴边半隐藏，所以终点在这里重新读取，不沿用打开时的坐标）；
 * 3) 交还：恢复常态阶段，悬浮球在同一位置接住它（小球一侧给一次极轻的「接收」反馈）。
 *
 * 关键点：全程**不做整体淡出**。整层淡出会退化成「Planet 直接消失」，
 * 而这里要的是「Planet 收拢成那枚小球」。背景铺底在收缩过程中淡出，
 * 让对话页在对象归位的同时回到眼前。
 * 减少动画时不走这三段，直接交还（但仍保留状态语义与焦点回收）。
 */
async function close() {
  if (!props.open || closing.value || settling.value) return;
  // 捕获「这次关闭属于哪一次打开」：实例常驻后 props.seq 会被下一次打开改写，
  // 若不捕获，迟到的收尾回调会带上新序号、把刚打开的新层关掉。
  const seqAtClose = props.seq;
  const epoch = ++closeEpoch;
  if (prefersReducedMotion()) {
    closing.value = true;
    layersIn.value = false;
    planet.setReveal(0);
    resetContinuum();
    emit("close", seqAtClose);
    return;
  }
  // 连续体阶段：先降低密度与浮层，再收缩体量
  const continuum = beginClose();
  continuumEpoch = continuum;
  settling.value = true;
  layersIn.value = false;
  const prepareMs = tokenMs("--mo-3-prepare", SETTLE_MS);
  /**
   * 收势阶段把密度降到**球态的那一档**（而不是几乎清零）。
   *
   * 这里必须与球态用同一个值：否则收起的最后一帧和静止的小球长得不一样 ——
   * 实测过「收缩结束设 0.12、球态 0.55」，回到对话页的瞬间话题点与网格会突然冒出来，
   * 用户看到的就是「最后一帧与缩小状态完全不同」。
   */
  planet.setReveal(BALL_REVEAL);
  /**
   * 收势阶段**不动相机**。
   *
   * 以前这里有一记 `pullBack(0.45)`（相机轻微后撤）—— 那是旧编排（收势 + 整层淡出）的遗留物。
   * 现在退出是「同一个对象收拢回入口」，相机后撤只会让球体先缩小一次，
   * 接着星球层再缩回小球：用户实测反馈「收起有两段动画，一段先缩小一点，第二段才缩回小星球」。
   * 探针量化过：收势期间球体屏幕半径漂移 16.7%，拆掉后为 0。
   * 收势阶段该发生的只有：玻璃浮层退场、信息密度回到抽象态、对话页开始渐显。
   */
  await sleep(prepareMs);
  // 收势期间用户又打开了：放弃这次收尾，否则会把刚打开的层重新变成「收起中」
  if (epoch !== closeEpoch) return;
  if (!isCurrent(continuum)) return;
  // 体量收缩：终点是入口**此刻**的几何
  advanceTo(continuum, "returning");
  // 同样先等渲染缓过来：收缩被主线程阻塞吞掉的话，用户看到的是「星球直接消失」
  // 收起路径的上限刻意短（250ms）：退出是用户主动动作，宁可少一点动画也不能让它变慢。
  await waitForSmoothFrames(2, 250);
  // 对话页与收拢同时开始重新显出来（同一刻、同一时长）
  curtainOn.value = false;
  const settled = await runCollapseMotion(epoch, continuum);
  if (!settled) return;
  closing.value = true;
  resetContinuum(continuum);
  emit("close", seqAtClose);
}
</script>

<template>
  <div
    ref="rootRef"
    class="planet-view"
    :class="{ entered, closing, settling, layers: layersIn, armed: stageArmed, curtain: curtainOn, ball: ballMode, tail: tailMode }"
    :style="[demoStyle, ballMode ? { opacity: String(ballOpacity) } : null]"
  >
    <!-- 星球层：入口小球长大成 Planet 的**就是这一层**（同一对象的尺度变化），
         所以缩放加在这里，而不是给画布做透明度动画。 -->
    <!-- data-stage-k 是对外可观测的转场状态（真实几何走 CSS 变量；
         这里给验收脚本与测试一个稳定的读数，避免依赖 jsdom 对自定义属性的支持） -->
    <div
      ref="stageRef"
      class="planet-stage"
      :style="stageStyle"
      :data-stage-k="stageStyle['--stage-k'] ?? '1'"
    >
      <canvas
        ref="canvasRef"
        class="planet-canvas"
        :class="{ hovering: hoverTopicId }"
        @click="onCanvasClick"
        @dblclick="onCanvasDblClick"
      ></canvas>
    </div>
    <!-- 指向话题点时显示简短名称：只显示当前指到的那一个，不让标签常驻遮挡 -->
    <div v-if="hoverLabel" class="topic-hint qio-glass qio-glass--chip" :style="hoverLabelStyle">{{ hoverLabel.title }}</div>
    <!-- 选中话题的名称常驻（悬停其它点时让位给悬停标签） -->
    <div v-else-if="selectedLabel" class="topic-hint selected qio-glass qio-glass--chip" :style="selectedLabelStyle">{{ selectedLabel.title }}</div>
    <button
      class="close-btn qio-glass qio-glass--chip"
      type="button"
      :disabled="closing || settling"
      aria-label="收起星球，回到对话"
      @click="close"
    >
      {{ closing || settling ? "收起中…" : "✕ 收起星球" }}
    </button>
    <!-- 图形诊断只在开发者模式出现：正常模式保持「像产品，不像 debugger」 -->
    <div v-if="ui.developerMode" class="hud mono qio-glass qio-glass--chip">
      <span v-if="webglOK">WebGL · {{ fpsText }} fps · 视角: {{ cameraStateText }}</span>
      <span v-else>WebGL 不可用</span>
    </div>
    <!-- WebGL 不可用时不白屏/黑屏：给一段可读的降级说明，管理功能仍可用 -->
    <!-- 只有星球真正打开时才显示这段说明：球态（关着星球）下它压在对话页上，
         会变成「文字叠在聊天内容上」的观感问题（实测截图 planet-70-webgl-fallback） -->
    <div v-if="!webglOK && !ballMode" class="webgl-fallback" role="alert">
      <div class="wf-card qio-card">
        <div class="wf-title serif">无法启用 3D 星球</div>
        <p class="wf-text">
          当前环境不支持 WebGL（或显卡驱动不可用），所以球面视图用不了。
          话题数据不受影响：右侧面板里仍然可以查看话题、片段原文、知识与实体，
          收起星球后也能继续对话。
        </p>
      </div>
    </div>
    <!-- 首次数据加载失败：说明 + 重试，不拿空球冒充完整内容 -->
    <div v-if="loadError" class="load-error qio-glass" role="alert">
      <span class="le-text">{{ loadError }}</span>
      <!-- 技术细节折叠：不在第一眼出现，但一眼可展开（不是删掉） -->
      <details v-if="loadErrorDetail" class="le-detail">
        <summary>技术详情</summary>
        <span class="mono">{{ loadErrorDetail }}</span>
      </details>
      <button class="qio-btn" type="button" @click="retryLoad">重试</button>
    </div>
    <!-- 首次加载中：入口已经响应，长等待要有明确状态 -->
    <div v-else-if="dataLoading" class="planet-loading qio-glass qio-glass--chip" role="status">正在加载话题…</div>

    <aside class="panel" :class="{ open: panelOpen, manage: manageMode }">
      <button
        class="panel-toggle"
        :title="panelOpen ? '收起话题列表' : '展开话题列表'"
        :aria-label="panelOpen ? '收起话题列表' : '展开话题列表'"
        :aria-expanded="panelOpen"
        @click="togglePanel"
      >
        <svg class="chev" :class="{ flip: !panelOpen }" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
          <path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
      <div class="panel-inner">
        <div class="tabs">
          <button class="tab tab-topic" :class="{ active: activeTab === 'topic' }" @click="switchTab('topic')">话题</button>
          <button class="tab tab-knowledge" :class="{ active: activeTab === 'knowledge' }" @click="switchTab('knowledge')">知识</button>
          <button class="tab tab-entity" :class="{ active: activeTab === 'entity' }" @click="switchTab('entity')">实体</button>
          <span class="spacer"></span>
          <button class="mode-btn" @click="toggleManageMode">{{ manageMode ? "✕ 管理模式" : "管理模式" }}</button>
        </div>

        <template v-if="activeTab === 'topic'">
          <div class="panel-head">
            <h2 class="serif">话题</h2>
            <QInput v-model="search" placeholder="搜索话题…" />
          </div>
          <!-- 可滚动中部：话题列表自己滚（高度封顶），详情正文占满剩余空间自己滚；
               底部操作区在两者之外，始终可见且不覆盖内容 -->
          <div class="panel-body">
            <div class="topic-scroll">
              <ul class="topic-list">
                <li
                  v-for="t in filteredTopics"
                  :key="t.topic_id"
                  :class="{ active: t.topic_id === planet.selectedTopicId.value }"
                  tabindex="0"
                  role="option"
                  :aria-selected="t.topic_id === planet.selectedTopicId.value"
                  @click="selectTopic(t.topic_id)"
                  @keydown.enter.prevent="selectTopic(t.topic_id)"
                  @keydown.space.prevent="selectTopic(t.topic_id)"
                >
                  <span class="name serif">{{ t.title }}</span>
                  <span class="meta qio-badge">{{ t.fragment_count }} 片段</span>
                </li>
              </ul>
            </div>

            <div class="detail-scroll">
              <div v-if="detailLoading" class="detail-loading">加载中…</div>
              <!-- 加载失败：没有详情也要能看到失败原因并有重试入口 -->
              <div v-else-if="detailError" class="detail-error" role="alert">
                <p class="err-text">{{ detailError }}</p>
                <details v-if="detailErrorDetail" class="err-detail">
                  <summary>技术详情</summary>
                  <span class="mono">{{ detailErrorDetail }}</span>
                </details>
                <button class="qio-btn" type="button" @click="retryDetail">重试</button>
              </div>
              <div v-else-if="detail" class="detail">
                <h3 class="serif">{{ detail.name }}</h3>
                <!-- 信息层级：标题 → 一句摘要 → 最近活动/片段数/消息数 → 片段历史 → 知识/实体 -->
                <p v-if="detail.summary" class="detail-summary">{{ detail.summary }}</p>
                <p class="detail-facts mono">{{ detailFacts }}</p>
                <div v-if="detail.keywords?.length" class="detail-keywords">
                  <span v-for="k in detail.keywords.slice(0, 6)" :key="k" class="kw">{{ k }}</span>
                </div>

              <!-- 浏览的话题 / 选中的片段 / 已经生效的起点：三者分开写，不混为一谈 -->
              <dl class="detail-identity">
                <div class="id-row">
                  <dt>浏览的话题</dt>
                  <dd>{{ browsedTopicName }}</dd>
                </div>
                <div class="id-row">
                  <dt>选中的片段</dt>
                  <dd>{{ selectedFragmentLabel }}</dd>
                </div>
                <div class="id-row">
                  <dt>已生效的起点</dt>
                  <dd class="mono">{{ anchorLabel }}</dd>
                </div>
              </dl>

              <div class="detail-section">
                <div class="section-title serif">片段历史</div>
                <div
                  v-for="f in detail.fragments"
                  :key="f.fragment_id"
                  class="fragment-item"
                  :class="{ selected: f.fragment_id === selectedFragmentId }"
                  role="button"
                  tabindex="0"
                  :aria-pressed="f.fragment_id === selectedFragmentId"
                  @click="selectedFragmentId = f.fragment_id"
                  @keydown.enter.prevent="selectedFragmentId = f.fragment_id"
                  @keydown.space.prevent="selectedFragmentId = f.fragment_id"
                >
                  <div class="fragment-head">
                    <span class="fragment-when mono">{{ formatWhen(f.created_at ?? f.closed_at) }}</span>
                    <span class="fragment-count mono">{{ f.message_count }} 条消息</span>
                    <span class="fragment-state mono">{{ f.closed_at ? "已封块" : "开放中" }}</span>
                  </div>
                  <div class="fragment-summary">{{ f.summary || "（无摘要）" }}</div>
                  <div class="fragment-actions">
                    <button class="qio-btn mini raw-toggle" type="button" @click.stop="toggleRaw(f.fragment_id)">
                      {{ rawFragmentId === f.fragment_id ? "收起原文" : "查看原文" }}
                    </button>
                    <button
                      class="qio-btn mini frag-continue"
                      type="button"
                      :disabled="closing || anchorBusy"
                      @click.stop="continueFromFragment(f.fragment_id)"
                    >
                      从这里继续
                    </button>
                  </div>
                  <!-- 原文：只读历史，按需分页；明确「这是过去发生过的内容」 -->
                  <div v-if="rawFragmentId === f.fragment_id" class="fragment-raw">
                    <p class="raw-note">
                      以下是当时发生过的内容，只读；当前对话没有停在这里，也不会被改写。
                    </p>
                    <p v-if="rawLoading && !rawMessages.length" class="raw-loading">正在读取原文…</p>
                    <div v-else-if="rawError" class="raw-error" role="alert">
                      <span>{{ rawError }}</span>
                      <button class="qio-btn mini" type="button" @click="loadRawPage(rawMessages.length)">
                        重试
                      </button>
                    </div>
                    <template v-else>
                      <ul class="raw-list">
                        <li v-for="m in rawMessages" :key="m.id" class="raw-item" :class="m.role">
                          <span class="raw-role mono">{{ roleLabel(m.role) }}</span>
                          <span class="raw-content">{{ m.content }}</span>
                        </li>
                      </ul>
                      <button
                        v-if="rawHasMore"
                        class="qio-btn mini raw-more"
                        type="button"
                        :disabled="rawLoading"
                        @click="loadRawPage(rawMessages.length)"
                      >
                        {{ rawLoading ? "读取中…" : `继续读取（已读 ${rawMessages.length}/${rawTotal}）` }}
                      </button>
                      <p v-else-if="rawTotal" class="raw-end mono">已读完这段历史（共 {{ rawTotal }} 条）</p>
                    </template>
                  </div>
                </div>
                <p v-if="!detail.fragments.length" class="hint">暂无片段：这个话题还没有可接续的历史位置。</p>
              </div>

              <div class="detail-section">
                <div class="section-title serif">实体</div>
                <div class="entity-tags">
                  <span v-for="e in detail.entities" :key="e.id" class="entity-tag" @click="openEntityByNode(e.id)">{{ e.name }}</span>
                  <span v-if="!detail.entities.length" class="hint">无</span>
                </div>
              </div>

              <div class="detail-section">
                <div class="section-title serif">知识</div>
                <div v-for="k in detail.knowledge" :key="k.id" class="knowledge-item qio-card qio-card--quiet">
                  <span class="k-state qio-state" :class="knowledgeStateTone(k.state)">{{ knowledgeStateLabel(k.state) }}</span>
                  <template v-if="knowledgeEditingId === k.id">
                    <textarea
                      v-model="knowledgeDraft"
                      class="qio-inline-edit knowledge-edit"
                      rows="3"
                      aria-label="修正知识内容"
                    ></textarea>
                    <div class="k-actions open">
                      <button class="qio-btn mini" type="button" @click="knowledgeEditingId = null">取消</button>
                      <button
                        class="qio-btn mini primary"
                        type="button"
                        :disabled="knowledgeFeedback.stateOf(`knowledge:${k.id}`) === 'busy'"
                        @click="saveKnowledgeEdit(k)"
                      >
                        {{ knowledgeFeedback.stateOf(`knowledge:${k.id}`) === "busy" ? "保存中…" : "保存" }}
                      </button>
                    </div>
                  </template>
                  <template v-else>
                    <span class="k-content">{{ k.content }}</span>
                    <!-- 阅读态：管理动作不在第一眼出现（hover / 键盘聚焦 / 触屏才显示） -->
                    <div class="k-actions">
                      <button class="qio-btn mini quiet" type="button" @click="startEditKnowledge(k)">修正</button>
                      <button
                        class="qio-btn mini quiet"
                        type="button"
                        :disabled="knowledgeFeedback.stateOf(`knowledge:${k.id}`) === 'busy'"
                        @click="confirmKnowledgeId = k.id"
                      >
                        归档
                      </button>
                    </div>
                  </template>
                  <!-- 反馈留在这一条上：成功短暂、失败保留并可重试（不静默失败） -->
                  <span
                    v-if="knowledgeFeedback.stateOf(`knowledge:${k.id}`) === 'ok'"
                    class="k-feedback qio-feedback ok"
                    role="status"
                  >{{ knowledgeFeedback.okTextOf(`knowledge:${k.id}`) }}</span>
                  <p
                    v-if="knowledgeFeedback.stateOf(`knowledge:${k.id}`) === 'failed'"
                    class="k-feedback qio-feedback err"
                    role="alert"
                  >{{ knowledgeFeedback.errorOf(`knowledge:${k.id}`) }}</p>
                  <QConfirm
                    v-if="confirmKnowledgeId === k.id"
                    :open="true"
                    variant="inline"
                    tone="danger"
                    title="归档这条知识？"
                    detail="归档后它不再参与回答，但不会被删除：可以在知识面板里看到它已归档。"
                    confirm-text="归档"
                    @confirm="archiveKnowledge(k)"
                    @cancel="confirmKnowledgeId = null"
                  />
                </div>
                <p v-if="!detail.knowledge.length" class="hint">无知识条目。</p>
              </div>
            </div>
            </div>
          </div>

          <!-- 固定底部操作区：选中的片段、起点失败原因与动作按钮始终可见 -->
          <div v-if="detail" class="detail-actions">
            <p v-if="selectedFragmentId" class="selected-position">
              {{ selectedPositionLabel }}：{{ selectedFragmentSummary }}
            </p>
            <p v-else class="selected-position muted">
              未选片段：将从「{{ detail.name }}」的最新位置继续
            </p>
            <p v-if="anchorError" class="anchor-error" role="alert">{{ anchorError }}</p>
            <button class="start-btn qio-btn primary" :disabled="closing || anchorBusy" @click="startHere">
              {{ startButtonLabel }}
            </button>
          </div>
        </template>
        <KnowledgePanel v-else-if="activeTab === 'knowledge'" @focus-topic="focusTopicFromPanel" />
        <EntityPanel v-else :open-by-node-id="entityOpenId" />
      </div>
    </aside>
  </div>
</template>

<style scoped>
.planet-view {
  position: fixed; inset: 0; z-index: 50; display: flex;
  overflow: hidden;
  /* 入口小球自己的画布尺寸。球态（.ball）与收缩尾段（.tail）共用同一个值：
     尾段要在「球自己的分辨率」下渲染（见 runCollapseMotion），两处必须是同一个数，
     否则交接时仍会差一点点尺寸。 */
  --ball-size: 108px;
  /* 背景必须是透明的：盖住对话页的只有 ::before 那层铺底（它才是被淡入淡出的东西）。
     这里若自带不透明底色（以前是 var(--bg-base)），整层一挂载就把对话页盖死，
     铺底再怎么淡也看不见 —— 实测：星球还只有 0.23 大时，对话页区域对比度已经是 0。 */
  background: transparent;
  /* 默认不可见：只有两种状态允许出现在屏幕上 —— 球态（对话页入口那颗小球）
     或已经进入的星球层。这样场景初始化的那一小段不会把全尺寸星球闪一下。 */
  opacity: 0;
  /* 第四阶段：整层不再做「淡出」—— 退出是「对象收拢回入口」，
     整层淡出会退化成「Planet 直接消失」。淡出交给铺底（::before）单独完成。 */
  transition: opacity var(--dur-exit) var(--ease-1-out);
}
.planet-view.ball,
.planet-view.entered { opacity: 1; }
/* ---- 球态（对话页常驻的入口小球）----
   同一个场景缩到入口尺度常驻：层级降到浮动组件之下、不拦截交互（点击与拖动都交给
   对话页那枚按钮）、跟随入口不补间（拖动要跟手）。 */
.planet-view.ball {
  z-index: 10;
  pointer-events: none;
  background: transparent;
}
.planet-view.ball {
  /* 球态的画布**就是球本身那么大**（不做 CSS 降采样）。
     以前是把全屏渲染缩到 ~10% 显示：那圈轮廓在 WebGL 里是 1 个设备像素宽的线，
     缩小后只剩 0.1px，采样大部分落空 —— 看起来「轮廓全是像素点」。

     108px 这个数不是随手取的：球体在画布高度里占 88.7%（固定 FOV 下的投影比例），
     108 × 0.887 / 2 ≈ 48px，正好等于入口按钮的半径 —— 于是球态的 scale ≈ 1.000，
     既没有降采样也没有放大，展开那一刻的尺寸也严丝合缝。 */
}
.planet-view.ball .planet-stage {
  align-items: center;
  justify-content: center;
}
.planet-view.ball .planet-canvas {
  flex: none;
  width: var(--ball-size);
  height: var(--ball-size);
}
.planet-view.ball .planet-stage {
  transition: none;
  will-change: auto;
}
/* 铺底：半透明地盖住聊天（不是整块纯色），A 阶段淡入、收缩时淡出，
   让「周边界面退后」与「对话页回到眼前」都发生在这一个对象的变化过程中。 */
.planet-view::before {
  /* 铺底用行星场景自己的虚空色（与 WebGL scene.background 同源令牌）：
     转场期间「球体外围」必须是同一个虚空，否则会看到画布边界 */
  content: ""; position: absolute; inset: 0; z-index: 1; background: var(--planet-void);
  opacity: 0;
  /* 打开时与「长大」同长（脚本在 B 阶段置 .curtain），收拢时与「收缩」同长
     （脚本在 return 阶段撤掉 .curtain，这里把退出时长换成 collapse）。 */
  /* 曲线与长大用同一条（settle）：这样「星球长到多大」与「对话页被盖掉多少」是同步的。
     换成 ease-3-in 那种慢起曲线时实测：星球已经长到一半，铺底才 6% —— 渐隐全挤在最后。 */
  transition: opacity var(--mo-3-expand) var(--ease-3-settle);
}
.planet-view.curtain::before { opacity: 1; }
.planet-view.settling::before { transition: opacity var(--mo-3-collapse) var(--ease-3-out); }
/* 内容层压在铺底之上（关闭按钮/诊断/错误条各有自己的 z-index，保持更高） */
.planet-stage, .panel { z-index: 2; }
/* 星球层：入口小球 → 全屏 Planet 的连续体。
   transform-origin 在 (0,0)，位移与尺度由脚本按入口小球的实时几何算出
   （见 alignStageToEntry）：起点与小球完全重合，终点是 scale(1) 的完整构图。
   曲线用低频档的柔性收束（约 3% 单次过冲），这是「优雅预算」的支出处。 */
.planet-stage {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  transform-origin: 0 0;
  transform: translate3d(var(--stage-tx, 0px), var(--stage-ty, 0px), 0) scale(var(--stage-k, 1));
  /* 过渡在 .armed 之后才生效：先把入口尺度无过渡地落位，B 阶段的放大才是
     「从小球尺度长大」，而不是「从 identity 追到小球尺度」 */
  transition: none;
  /* 只在转场期间提升合成层；常驻 will-change 会白占显存 */
  will-change: transform;
}
.planet-view.armed .planet-stage {
  transition: transform var(--mo-3-expand) var(--ease-3-settle);
}
/* 收缩尾段：换成入口小球那套布局（画布 = 球自己的尺寸、居中），体量由脚本每帧写入。
   放在 .armed 之后：这一段的过渡已经由 stageArmed=false 撤掉，这里再兜一层。 */
.planet-view.tail .planet-stage {
  align-items: center;
  justify-content: center;
  transition: none;
}
.planet-view.tail .planet-canvas {
  flex: none;
  width: var(--ball-size);
  height: var(--ball-size);
}
/* 已经回到常态：撤掉 will-change */
.planet-view.layers .planet-stage { will-change: auto; }
.planet-view.closing { opacity: 0; }
/* 收起阶段（整体淡出中）：不再拦截点击与焦点 —— 页面已经在消失，不能继续扣着输入 */
.planet-view.closing { pointer-events: none; }
.planet-canvas { flex: 1 1 auto; min-width: 0; width: 100%; height: 100%; cursor: grab; }
/* 画布本身透明清屏（renderer alpha: true，且不设 scene.background）：
   球体之外的像素是透明的，露出来的是铺底的 `--planet-void`。
   这样任何尺度下都不存在「画布矩形」，「同一个对象长大」才成立 ——
   不再需要按球体位置裁一圈遮罩。 */
.planet-canvas.hovering { cursor: pointer; }
/* 收起过程中（收势 + 收缩）不拦截点击与焦点：对象已经在回去的路上 */
.planet-view.settling { pointer-events: none; }
.close-btn {
  position: absolute; top: 14px; left: 14px; z-index: 60;
  color: var(--text-strong);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  cursor: pointer;
  /* C 阶段才进入：浮层属于「场景接管」之后的信息层 */
  opacity: 0;
  transform: translateY(calc(var(--shift-2) * -1));
  transition: opacity var(--mo-3-settle) var(--ease-3-out), transform var(--mo-3-settle) var(--ease-3-out),
    border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1);
}
.planet-view.layers .close-btn { opacity: 1; transform: none; }
.close-btn:hover { border-color: var(--accent); color: var(--accent); }
.close-btn:disabled { opacity: 0.6; cursor: default; }
.hud {
  position: absolute; bottom: 14px; left: 14px; font-size: 12px; color: var(--text-muted);
  padding: 6px 12px;
}
.webgl-fallback {
  position: absolute; inset: 0;
  /* 必须压在画布之上（.planet-stage 是 z-index: 2）：
     否则这段说明虽然存在、也有底色，但会被画布盖住，屏幕上什么都看不到。 */
  z-index: 60;
  display: flex; align-items: center; justify-content: center; padding: 0 24px;
  pointer-events: none;
}
/* 说明自己是一块有底色的卡片：不透明底 + 内边距 + 阴影。
   之前它只是一段裸文字，直接叠在对话内容上，看起来像界面坏了。 */
.webgl-fallback .wf-card {
  display: flex; flex-direction: column; gap: 10px; text-align: center;
  max-width: 440px; padding: 22px 24px; border-radius: 14px;
  background: var(--bg-elevated); border: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-2);
}
.webgl-fallback .wf-title { font-size: 19px; color: var(--text-strong); }
.webgl-fallback .wf-text { font-size: 13px; color: var(--text-secondary); line-height: 1.75; }
.load-error {
  position: absolute; top: 14px; left: 50%; transform: translateX(-50%);
  z-index: 61; display: flex; align-items: center; gap: 10px; max-width: min(560px, calc(100vw - 200px));
  padding: 8px 12px; border-radius: 10px;
  /* 危险色调通过玻璃自己的令牌表达：直接写 border-color 会覆盖掉左右冷暖边，
     把「透明介质」压成一块普通描边色块（实测：两边颜色变成完全相同的红）。 */
  --glass-border: var(--border-danger);
  --glass-tint-warm: var(--danger-soft);
  --glass-tint-cool: var(--border-danger);
  color: var(--danger);
  font-size: 12.5px;
}
.load-error .le-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 技术详情：默认折叠，展开后是小字等宽（永远不该大过内容字号） */
.load-error .le-detail,
.detail-error .err-detail { font-size: var(--fs-tech); color: var(--text-muted); }
.load-error .le-detail summary,
.detail-error .err-detail summary {
  cursor: pointer; color: var(--text-muted); list-style: none;
}
.load-error .le-detail summary::-webkit-details-marker,
.detail-error .err-detail summary::-webkit-details-marker { display: none; }
.load-error .le-detail[open] > span,
.detail-error .err-detail[open] > span {
  display: block; margin-top: 2px; color: var(--text-secondary); overflow-wrap: anywhere;
}
.planet-loading {
  position: absolute; top: 14px; left: 50%; transform: translateX(-50%); z-index: 61;
  padding: 8px 14px; border-radius: 10px; font-size: 12.5px;
  color: var(--text-secondary);
}
/* 悬停标签：跟随指针，避开指针本身；不可交互，不抢焦点 */
.topic-hint {
  position: absolute; z-index: 58; transform: translate(14px, -50%);
  max-width: 240px; padding: 3px 9px; border-radius: 20px;
  font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  color: var(--text-strong);
  pointer-events: none;
}
/* 选中话题的名称：与「已选中」状态同一套强调色，和悬停标签区分开 */
.topic-hint.selected {
  /* 选中点外面有选中环，标签再往外让一点，避免压在环上 */
  transform: translate(46px, -50%);
  border-color: var(--accent);
  color: var(--accent);
}
.panel {
  position: relative;
  flex: 0 0 auto;
  min-width: 0;
  width: 0;
  height: 100%;
  background: var(--bg-panel);
  border-left: 1px solid transparent;
  opacity: 0;
  /* 侧栏开合 210ms，与 210ms 的重定位补间同时发生（不再串联成两次移动）；
     opacity 负责入场（420ms）与收起时的收势（180ms）。 */
  transition: width var(--dur-panel) var(--ease-out), border-color var(--dur-panel) var(--ease-out),
    opacity var(--dur-planet-in) var(--ease-out);
}
.planet-view.entered .panel { opacity: 1; }
.planet-view.entered.settling .panel {
  /* 收起时右栏要**退场**，不只是变淡：它属于「星球被打开」这件事，
     留着它就会出现「回到对话页了、话题栏还挂在旁边」（用户实测反馈）。 */
  width: 0;
  opacity: 0;
  border-left-color: transparent;
  transform: translateX(var(--shift-8));
  pointer-events: none;
  transition: width var(--mo-3-prepare) var(--ease-3-out), border-color var(--mo-3-prepare) var(--ease-3-out),
    opacity var(--mo-3-prepare) var(--ease-3-out), transform var(--mo-3-prepare) var(--ease-3-out);
}
/* 球态（对话页入口小球）：右栏一律不出现 —— 收起动画结束时承接上面那一段 */
.planet-view.ball .panel {
  width: 0;
  opacity: 0;
  border-left-color: transparent;
  pointer-events: none;
}
.panel.open {
  width: clamp(340px, 32vw, 480px);
  border-left-color: var(--border-subtle);
}
.panel-inner {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
/* 可滚动中部：顶部标题与底部操作区保持稳定，只有这两个区域各自滚动 */
.panel-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 话题多时列表自己滚，且高度封顶：详情永远留在可见区域，不用先滚过整份列表 */
.topic-scroll {
  flex: 0 1 auto;
  max-height: 42%;
  min-height: 0;
  overflow-y: auto;
  border-bottom: 1px solid var(--border-subtle);
}
.detail-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}
/* 固定底部操作区（在正常流里，不覆盖正文） */
.detail-actions {
  flex: 0 0 auto;
  padding: 10px 14px 14px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-panel);
}
.detail-actions .start-btn { margin-top: 10px; }
.detail-identity {
  margin: 8px 0 4px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: var(--bg-inset);
}
.detail-identity .id-row { display: flex; gap: 8px; font-size: 12px; line-height: 1.7; }
.detail-identity dt { color: var(--text-muted); flex: 0 0 84px; margin: 0; }
.detail-identity dd { margin: 0; color: var(--text-primary); min-width: 0; overflow-wrap: anywhere; }
.topic-list li:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.panel.open.manage { width: clamp(360px, 46vw, 640px); }
/* 中等窗口：面板按视口比例收窄，别把星球挤成一条 */
@media (max-width: 1199px) {
  .panel.open { width: 46vw; }
  .panel.open.manage { width: 50vw; }
}
/* 窄窗口（接近 Tauri 最小尺寸）：面板变成覆盖式抽屉，星球保持可用 */
@media (max-width: 899px) {
  .panel.open,
  .panel.open.manage {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(92vw, 520px);
    z-index: 55;
    border-left: 1px solid var(--border-strong);
    box-shadow: var(--shadow-3);
  }
}
.tabs { display: flex; align-items: center; gap: 6px; padding: 10px 14px 6px; border-bottom: 1px solid var(--border-subtle); }
.tab { font-size: 13px; padding: 6px 12px; color: var(--text-secondary); cursor: pointer; background: none; border: none; border-bottom: 2px solid transparent; font-family: var(--sans); }
.tab:hover { color: var(--text-strong); }
.tab.active { color: var(--text-strong); border-bottom-color: var(--accent); font-weight: 600; }
.tabs .spacer { flex: 1; }
.mode-btn { font-size: 11px; padding: 4px 10px; border-radius: 20px; border: 1px solid var(--border-strong); background: none; color: var(--text-secondary); cursor: pointer; }
.mode-btn:hover { color: var(--accent); border-color: var(--accent); }
.panel-toggle {
  position: absolute;
  top: 50%;
  left: -30px;
  transform: translateY(-50%);
  z-index: 3;
  width: 30px;
  height: 78px;
  padding: 0;
  border: 1px solid var(--border-strong);
  border-right: none;
  border-radius: 12px 0 0 12px;
  background: var(--bg-panel);
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.2s ease, color 0.2s ease, border-color 0.2s ease;
}
.panel-toggle:hover {
  background: var(--accent-soft);
  color: var(--accent);
  border-color: var(--accent);
}
.panel-toggle .chev {
  transition: transform 0.3s cubic-bezier(0.22, 0.8, 0.24, 1);
}
.panel-toggle .chev.flip {
  transform: rotate(180deg);
}
.panel-head { padding: 14px 14px 8px; }
.panel-head h2 { font-size: 16px; color: var(--text-strong); margin: 0 0 8px; }
.topic-list { list-style: none; padding: 0 14px; margin: 6px 0; }
.topic-list li {
  display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 10px;
  border-radius: 10px; cursor: pointer; font-size: 13px;
}
.topic-list li:hover { background: var(--accent-soft); }
.topic-list li.active { background: var(--accent-softer); }
.topic-list .name { font-size: 14px; color: var(--text-primary); }
.topic-list .meta { flex-shrink: 0; }
.detail { padding: 0 14px 20px; border-top: 1px solid var(--border-subtle); }
.detail h3 { font-size: 16px; color: var(--text-strong); margin: 12px 0 8px; }
/* 详情顶部：一句摘要 + 一行事实 + 少量关键词（不堆徽章、不把信息平铺） */
.detail-summary {
  margin: 0 0 6px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-primary);
}
.detail-facts {
  margin: 0 0 6px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.detail-keywords {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.detail-keywords .kw {
  font-size: 10.5px;
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 0 8px;
}
.detail-section { margin: 10px 0; }
.section-title { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; letter-spacing: 0.02em; }
.fragment-item {
  border: 1px solid var(--border-subtle); border-radius: 10px; padding: 8px 10px; margin-bottom: 6px;
  cursor: pointer; background: var(--bg-inset);
}
.fragment-item:hover { border-color: var(--border-strong); }
.fragment-item.selected { border-color: var(--accent); background: var(--bg-accent-subtle); }
.fragment-summary { font-size: 13px; color: var(--text-primary); }
.fragment-meta { font-size: 11px; color: var(--text-muted); margin-top: 3px; }
.fragment-item { display: flex; flex-direction: column; gap: 4px; }
.fragment-head {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 10.5px;
  color: var(--text-muted);
}
.fragment-head:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.fragment-when { color: var(--text-secondary); }
.fragment-count { margin-left: auto; }
.fragment-state { opacity: 0.8; }
.fragment-actions { display: flex; gap: 6px; margin-top: 2px; }
.fragment-actions .qio-btn.mini { height: auto; padding: 3px 10px; font-size: 11px; }
/* ---- 片段原文（只读历史 + 分页） ---- */
.fragment-raw {
  margin-top: 6px;
  padding: 8px 10px;
  border-left: 2px solid var(--border-strong);
  background: var(--bg-inset);
  border-radius: 0 8px 8px 0;
}
.raw-note {
  margin: 0 0 6px;
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.6;
}
.raw-loading { margin: 0; font-size: 11.5px; color: var(--text-muted); }
.raw-error {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--danger);
}
.raw-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.raw-item { display: flex; gap: 8px; font-size: 12px; line-height: 1.6; }
.raw-role {
  flex: 0 0 34px;
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  padding-top: 2px;
}
.raw-content { color: var(--text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.raw-item.assistant .raw-content { color: var(--text-primary); }
.raw-more { align-self: flex-start; margin-top: 8px; }
.raw-end { margin: 8px 0 0; font-size: 10.5px; color: var(--text-muted); }
/* 知识条目的操作反馈：留在这一条上 */
.k-feedback { font-size: 11px; }
.k-feedback.ok { color: var(--success); }
.k-feedback.err { flex-basis: 100%; margin: 2px 0 0; color: var(--danger); }
.entity-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.entity-tag { background: var(--bg-accent-subtle); border-radius: 20px; padding: 3px 10px; font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.knowledge-item {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  flex-wrap: wrap;
  margin-bottom: 6px;
  padding: 8px 10px;
  font-size: 12.5px;
}
/* 阅读态优先：内容占满一行并成为视觉主体，管理动作退到下一行且默认不可见 */
.knowledge-item .k-content { flex: 1 1 100%; color: var(--text-primary); line-height: 1.7; }
.knowledge-item .k-actions {
  display: flex;
  gap: 6px;
  margin-left: auto;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-1);
}
.knowledge-item:hover .k-actions,
.knowledge-item:focus-within .k-actions,
.knowledge-item .k-actions.open { opacity: 1; }
/* 触屏没有 hover：管理入口必须默认可见，否则永远点不到 */
@media (hover: none) {
  .knowledge-item .k-actions { opacity: 1; }
}
.knowledge-edit { flex: 1 1 100%; min-width: 160px; height: auto; font-size: 12.5px; resize: vertical; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; border-radius: 8px; }
.qio-btn.mini.danger { color: var(--danger); border-color: var(--border-danger); }
.k-content { color: var(--text-primary); }
.start-btn { width: 100%; height: 40px; margin-top: 12px; font-size: 14px; }
.start-btn:disabled { opacity: 0.6; cursor: default; }
.anchor-error {
  margin-top: 12px; padding: 8px 12px; border-radius: 8px;
  border: 1px solid var(--danger); background: var(--danger-soft);
  color: var(--danger); font-size: 12px; line-height: 1.5;
}
.selected-position {
  margin-top: 12px; font-size: 12px; line-height: 1.6;
  color: var(--text-secondary);
}
.selected-position.muted { color: var(--text-muted); }
.hint { font-size: 12px; color: var(--text-muted); }
.detail-loading { padding: 14px; color: var(--text-muted); font-size: 13px; }
.detail-error {
  display: flex; flex-direction: column; gap: 10px; align-items: flex-start;
  padding: 0 14px 20px; border-top: 1px solid var(--border-subtle);
}
.detail-error .err-text {
  margin-top: 12px; padding: 8px 12px; border-radius: 8px; width: 100%;
  border: 1px solid var(--danger); background: var(--danger-soft);
  color: var(--danger); font-size: 12px; line-height: 1.5;
}
</style>
