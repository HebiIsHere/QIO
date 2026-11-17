/**
 * 底部功能栏的「按实际可用宽度分档」逻辑（纯函数，可单测）。
 *
 * 为什么需要它（2026-10-15 布局重构）：
 *   旧实现用视口媒体查询（@media max-width: 1080px / 900px / 620px）决定功能栏的行列关系。
 *   于是「窗口很宽、但聊天面板 / 侧栏 / 缩放后的容器把功能栏挤窄」时，断点根本不触发：
 *   功能栏仍按宽档排一行，提交区的长说明被压成极窄竖列，整条栏被撑高。
 *   这里改成读**功能栏自己的可用宽度**（由 BoardToolbar 用探测器实测），再决定分档。
 *
 * 三档的行列关系（由 BoardToolbar 的 grid-template-areas 定义，避免随内容长度随机排列）：
 *   wide   ：第一行 [编辑操作 | 上次提交 + 查看过程]，第二行 [本次统计 | 详情 + 提交]，notes 整行
 *   mid    ：第一行 [编辑操作]，第二行 [本次统计 | 详情 + 提交]，第三行 [上次提交 + 查看过程]，notes 整行
 *   narrow ：编辑操作 / 本次统计 / 详情 + 提交（右对齐）/ 上次提交 + 查看过程 / notes，逐行铺开
 *
 * 阈值的来历：用真实渲染实测两档参考宽度（功能栏约 764px 与约 1178px）——
 * 764px 下两行主栏放不下「编辑操作 + 上次提交状态与查看过程」这一行（实测会被压出竖列），
 * 1178px 下两行主栏完整成立。所以 wide 的下界取在两者之间，且留出余量给更长的真实内容
 * （例如「已提交过：有新的改动尚未提交」这类更长的状态文字）。
 * 数值集中在这里，改了必须重跑真实渲染验收（scripts 见 _bt-evidence）。
 */
export type ToolbarBand = "wide" | "mid" | "narrow";

/** 进入 wide 档所需的最小可用宽度（px）。 */
export const TOOLBAR_WIDE_MIN = 900;
/** 进入 mid 档所需的最小可用宽度（px）；再窄就是 narrow。 */
export const TOOLBAR_MID_MIN = 560;
/**
 * 回宽档的滞回余量（px）：缩小时立即降档、放大时要多出这么多才升档。
 * 没有它，在阈值附近连续拖动窗口会来回跳档（要求「窗口连续改变尺寸时不出现反复跳动」）。
 */
export const TOOLBAR_BAND_HYSTERESIS = 40;

export interface ToolbarBandInput {
  /** 功能栏实际可用宽度（px）。量不到时给 0，表示「保持上一档」。 */
  available: number;
  /** 上一档；首次计算传 undefined。 */
  previous?: ToolbarBand;
}

/**
 * 由可用宽度决定分档。缩小时立即降档（宁可早一点排成多行，也不要把文字压成竖列）；
 * 放大时带滞回，避免在阈值附近抖动。
 */
export function toolbarBand({ available, previous }: ToolbarBandInput): ToolbarBand {
  const width = Number.isFinite(available) ? Math.max(0, available) : 0;
  // 量不到就保持上一档（jsdom / 首帧）：绝不用 0 当作「最窄」，否则测试与首帧都会被判成 narrow
  if (width <= 0) return previous ?? "wide";
  // 从宽档离开是「降档」：立即生效，宁可早排成多行，也不要把文字压成竖列
  if (previous === "wide") {
    if (width >= TOOLBAR_WIDE_MIN) return "wide";
    return width >= TOOLBAR_MID_MIN ? "mid" : "narrow";
  }
  // 回到宽档是「升档」：要多出滞回余量才回得去，避免阈值附近反复跳档
  if (width >= TOOLBAR_WIDE_MIN + TOOLBAR_BAND_HYSTERESIS) return "wide";
  if (previous === "mid") return width >= TOOLBAR_MID_MIN ? "mid" : "narrow";
  // previous === "narrow"
  return width >= TOOLBAR_MID_MIN + TOOLBAR_BAND_HYSTERESIS ? "mid" : "narrow";
}

function lengthOf(style: CSSStyleDeclaration, name: string, fallback: number): number {
  const value = Number.parseFloat(style.getPropertyValue(name));
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/**
 * 量「功能栏实际可用宽度」。
 *
 * 它等于功能栏 max-width 的实际取值（变量定义在 styles/layers.css）：
 *   min(--im-toolbar-max-width, 100% - --im-toolbar-chat-reserve - --sp-8 - --sp-4)
 * 其中 100% 是舞台（[data-im="interactive-stage"]）的内容宽度。--im-toolbar-chat-reserve 是 min()/vw 组合，
 * `getPropertyValue` 拿到的是原始 token 串而不是像素，所以用一个宽度就等于该令牌的
 * 隐形探测器（reserveProbe）来量 —— 它不含相对自身容器的百分比，不会自引用。
 *
 * 量不到（jsdom / 还没布局）时返回 0，调用方据此保持上一档。
 */
export function toolbarAvailableWidth(el: HTMLElement, reserveProbe?: HTMLElement | null): number {
  if (typeof getComputedStyle !== "function") return 0;
  const owner = el.offsetParent as HTMLElement | null;
  const stage = owner ?? (el.parentElement as HTMLElement | null);
  const stageWidth = stage?.clientWidth ?? 0;
  if (!(stageWidth > 0)) return 0;

  const style = getComputedStyle(el);
  const probed = reserveProbe?.getBoundingClientRect?.().width ?? 0;
  const reserve = probed > 0 ? probed : lengthOf(style, "--im-toolbar-chat-reserve", 132);
  const edgeGap = lengthOf(style, "--sp-8", 32) + lengthOf(style, "--sp-4", 16);
  const rawMax = style.getPropertyValue("--im-toolbar-max-width").trim();
  const cap = /px$/i.test(rawMax) ? Number.parseFloat(rawMax) : stageWidth;
  const width = Math.min(Number.isFinite(cap) && cap > 0 ? cap : stageWidth, stageWidth - reserve - edgeGap);
  return Number.isFinite(width) && width > 0 ? width : 0;
}

/* --------------------------------------------------------------------------
 * rail（互动页底部「操作行」）形态的分档（2026-11-17 互动底部操作区改版）
 *
 * rail=true 时工具栏不再是「两行栅格 + 胶囊底」，而是一条透明单行：先省略能由图标说明的
 * 文字（full → icons），再把低频工具收进右侧「更多」（icons → more）。
 * 与 toolbarBand 同一个教训：档位必须由**工具栏自己的实测宽度**决定（BoardToolbar 用
 * ResizeObserver + getBoundingClientRect().width 量），不能用视口媒体查询 ——
 * 操作行嵌在聊天面板里，窗口很宽但面板/侧栏把它挤窄时视口断点根本不触发。
 *
 * 阈值来历（2026-11-17 用系统 Edge headless 1000×600 + 临时 lab 页挂真实组件、
 * 真实 tokens/CSS/字体实测 getBoundingClientRect；校准点就是下面三个常量，改之前先重跑）：
 * - icons 下界 300px：icons 档行上常驻 6 个控件 —— 添加（图标 32）+ 撤销 32 + 重做 32 +
 *   书签 32 + 详情（图标 32）+ 提交（实测「提交给 QIO」89.25，最长态「用同一身份重试」111.5），
 *   加 5 个 4px 等距间隙：常规内容 269.25px，最长态 291.5px —— 300 留 8.5px 余量。
 * - full 下界 344px：full 档额外露出「添加」文字（55.5）；「详情」在悬浮/rail 下仍是图标（compact），
 *   这里只会计入图标本身的 32px）。按钮横向内边距
 *   统一 8px 后常规内容 303.75px，最长提交态 326px —— 344 留 18px 余量。
 *   为什么不是更大：当前 InteractiveConversation 的右侧槽宽 = max(96, 面板宽/2 - 62)，
 *   面板最宽 860 → 槽宽最大只有 368px；下界若取 430 则 full 永远进不去（死档）。
 *   取 344 = 368 - 24，保证 1000×600 能进 full，且缩窄后从 icons 回到最大宽度 368 时
 *   刚好满足滞回、能恢复 full。
 * - 24px 滞回：缩小时立即降档、放大时多出 24px 才升档，阈值附近连续拖动不会来回跳档
 *   （与 TOOLBAR_BAND_HYSTERESIS 同一理由）。若父组件槽宽上限变了，重跑 lab 重新校准。
 * ------------------------------------------------------------------------ */
export type RailToolbarMode = "full" | "icons" | "more";

/** 进入 full 档（添加 / 详情带文字）所需的最小实测宽度（px）：= 最大槽宽 368 - 滞回 24。 */
export const RAIL_TOOLBAR_FULL_MIN = 344;
/** 进入 icons 档所需的最小实测宽度（px）；再窄就是 more。 */
export const RAIL_TOOLBAR_ICONS_MIN = 300;
/** 升档滞回余量（px）：缩小时立即降档，放大时多出这么多才升档。 */
export const RAIL_TOOLBAR_HYSTERESIS = 24;

export interface RailToolbarModeInput {
  /** 操作行工具栏自己的实测宽度（px）；量不到时给 0，表示「保持上一档」。 */
  available: number;
  /** 上一档；首次计算传 undefined（初始 full，绝不用 0 当作最窄）。 */
  previous?: RailToolbarMode;
}

/**
 * 由操作行自己的实测宽度决定三档。缩小时立即降档（宁可早点省略文字 / 收进更多，
 * 也不要把一行按钮挤到换行或撑高）；放大时带滞回，避免阈值附近抖动。
 */
export function railToolbarMode({ available, previous }: RailToolbarModeInput): RailToolbarMode {
  const width = Number.isFinite(available) ? Math.max(0, available) : 0;
  // 量不到（jsdom / 首帧 / 容器还没布局）就保持上一档；初始 full —— 绝不因为量不到就降成 more
  if (width <= 0) return previous ?? "full";
  if (previous === "full") {
    // 缩窄：立即降档。先省文字（icons），再收工具（more）
    if (width >= RAIL_TOOLBAR_FULL_MIN) return "full";
    return width >= RAIL_TOOLBAR_ICONS_MIN ? "icons" : "more";
  }
  if (previous === "icons") {
    // 从 icons 回 full 要多出滞回余量；继续缩窄时立即降 more
    if (width >= RAIL_TOOLBAR_FULL_MIN + RAIL_TOOLBAR_HYSTERESIS) return "full";
    return width >= RAIL_TOOLBAR_ICONS_MIN ? "icons" : "more";
  }
  // previous === "more"：回上一档同样要带滞回
  if (width >= RAIL_TOOLBAR_FULL_MIN + RAIL_TOOLBAR_HYSTERESIS) return "full";
  if (width >= RAIL_TOOLBAR_ICONS_MIN + RAIL_TOOLBAR_HYSTERESIS) return "icons";
  return "more";
}