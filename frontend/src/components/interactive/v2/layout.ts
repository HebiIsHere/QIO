/**
 * 互动页 v2 布局决策（Phase 1-B，纯函数，可在 Node / jsdom 下直接单测）。
 *
 * 设计口径（契约 §2.3）：
 * 1. 板面占主区域：工具栏、输入区、书签栏、对话区都是**参与布局的区域**，
 *    不是压在板面上的浮层 —— 任意两个区域的矩形互不相交（不覆盖主区）。
 * 2. 底部工具栏与输入区明确分层：输入区贴底，工具栏在它上方，两者之间留 GAP。
 * 3. 对话区从输入区一侧向上展开：它的下边缘挨着底部区域、上边缘朝板面方向生长；
 *    高度有上限（可用高度的 46%），且永远给板面保留 MAIN_MIN_HEIGHT。
 * 4. 书签栏：宽档（≥1024）为右侧窄栏；中/窄档为受限高度的下侧区域。
 *    右侧栏放不下时（会把板面压到 MAIN_MIN_WIDTH 以下）自动改为下侧。
 * 5. 所有尺寸由「工作区自身宽度/高度」推导，不读视口 —— 窗口很宽但被其它栏挤窄时也不会误判。
 *
 * 这里只产出几何数字；颜色/字体/圆角等视觉一律由组件用 var(--*) 令牌消费。
 */

export type WorkspaceBand = "wide" | "compact" | "narrow";
export type BookmarkDock = "side" | "bottom";
export type LayoutIssue =
  | "board_too_narrow"
  | "board_too_short"
  | "bookmarks_capped"
  | "chat_capped";

/** 档位阈值（工作区实测宽度，px）。与 styles/layers.css 的 1024 / 800 工具栏档位断点同一量级，但量的是容器不是视口。 */
export const BAND_WIDE_MIN = 1024;
export const BAND_COMPACT_MIN = 760;

/** 区域间距：与对话页卡片间距同源（12px），保证两页视觉节奏一致。 */
export const GAP = 12;

/** 板面最小可用尺寸：任何侧栏 / 展开都不许把主区压到这条线以下。 */
export const MAIN_MIN_WIDTH = 420;
export const MAIN_MIN_HEIGHT = 220;

/** 桌面右侧书签栏：按宽度比例取，钳在 168–260px（窄栏而不是第二列内容区）。 */
export const BOOKMARK_SIDE_MIN = 168;
export const BOOKMARK_SIDE_MAX = 260;
export const BOOKMARK_SIDE_RATIO = 0.16;

/** 窄窗口下侧书签栏：受限高度，钳在 72–148px。 */
export const BOOKMARK_BOTTOM_MIN = 72;
export const BOOKMARK_BOTTOM_MAX = 148;
export const BOOKMARK_BOTTOM_RATIO = 0.18;

/** 对话区向上展开：最多占可用高度的 46%；低于 CHAT_MIN_HEIGHT 时如实上报被压缩。 */
export const CHAT_MAX_RATIO = 0.46;
export const CHAT_MIN_HEIGHT = 140;

/** 首次量到高度之前使用的兜底高度（组件实测到真实高度后立即替换）。 */
export const TOOLBAR_FALLBACK_HEIGHT = 48;
export const COMPOSER_FALLBACK_HEIGHT = 64;

export interface WorkspaceRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface WorkspaceLayoutInput {
  /** 工作区内容盒宽度（不含页面外边距）。 */
  width: number;
  /** 工作区内容盒高度。 */
  height: number;
  /** 书签栏是否有内容且处于展开状态。 */
  bookmarksOpen?: boolean;
  /** 对话区是否展开。 */
  chatOpen?: boolean;
  /** 实测工具栏高度（0/缺省 = 没有这个区域）。 */
  toolbarHeight?: number;
  /** 实测输入区高度（0/缺省 = 没有这个区域）。 */
  composerHeight?: number;
}

export interface WorkspaceLayout {
  band: WorkspaceBand;
  bookmarkDock: BookmarkDock;
  /** 实际渲染的书签栏尺寸：右侧栏的宽度，或下侧栏的高度。 */
  bookmarkSize: number;
  /** 实际是否出现对话区（空间不足时可能不出现）。 */
  chatOpen: boolean;
  /** 对话区高度上限（0 = 不展开）。 */
  chatMaxHeight: number;
  board: WorkspaceRect;
  bookmarks: WorkspaceRect | null;
  chat: WorkspaceRect | null;
  toolbar: WorkspaceRect | null;
  composer: WorkspaceRect | null;
  /** 兜底发生时的机器可读说明（正常布局为空数组）。 */
  issues: LayoutIssue[];
}

function positive(value: number | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return 0;
  return value;
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

/**
 * 响应式档位：只看工作区自己的宽度。
 * 还没量到宽度（0 / 非法 / 负数）时按宽档处理：首帧不许把桌面布局闪成窄档。
 */
export function workspaceBand(width: number): WorkspaceBand {
  const measured = positive(width);
  if (measured <= 0) return "wide";
  if (measured >= BAND_WIDE_MIN) return "wide";
  if (measured >= BAND_COMPACT_MIN) return "compact";
  return "narrow";
}

/** 桌面右侧书签栏宽度（px）。 */
export function sideBookmarkWidth(width: number): number {
  const measured = positive(width);
  if (!measured) return BOOKMARK_SIDE_MIN;
  return Math.round(clamp(measured * BOOKMARK_SIDE_RATIO, BOOKMARK_SIDE_MIN, BOOKMARK_SIDE_MAX));
}

/** 窄窗口下侧书签栏高度（px）。 */
export function bottomBookmarkHeight(height: number): number {
  const measured = positive(height);
  if (!measured) return BOOKMARK_BOTTOM_MIN;
  return Math.round(
    clamp(measured * BOOKMARK_BOTTOM_RATIO, BOOKMARK_BOTTOM_MIN, BOOKMARK_BOTTOM_MAX),
  );
}

/**
 * 书签栏停靠方式：宽档优先右侧窄栏；若右侧栏会把板面压到 MAIN_MIN_WIDTH 以下，
 * 或者不是宽档，就退到下侧（受限高度），保证主区永远优先。
 */
export function bookmarkDock(input: { band: WorkspaceBand; width: number }): BookmarkDock {
  if (input.band !== "wide") return "bottom";
  const width = positive(input.width);
  if (!width) return "side";
  const side = sideBookmarkWidth(width);
  return width - side - GAP >= MAIN_MIN_WIDTH ? "side" : "bottom";
}

/**
 * 计算整个工作区的区域几何。
 *
 * 从底部往上堆叠（输入区 → 工具栏 → 下侧书签栏 → 对话区），每一层都按「放得下才放」处理：
 * 空间不足时先压书签栏/对话区，再如实记录 issues，绝不产生负尺寸或相交矩形。
 */
export function resolveWorkspaceLayout(input: WorkspaceLayoutInput): WorkspaceLayout {
  const width = positive(input.width);
  const height = positive(input.height);
  const band = workspaceBand(width);
  const bookmarksOpen = Boolean(input.bookmarksOpen) && width > 0;
  const chatOpen = Boolean(input.chatOpen) && height > 0;
  const toolbarHeight = positive(input.toolbarHeight);
  const composerHeight = positive(input.composerHeight);
  const dock = bookmarkDock({ band, width });
  const issues: LayoutIssue[] = [];

  let side = 0;
  if (bookmarksOpen && dock === "side") side = sideBookmarkWidth(width);

  let stackTop = height;
  let stackCount = 0;

  /** 从下往上压入一个区域：第一层贴底，之后的层各留一个 GAP。放不下时按剩余空间裁剪。 */
  function push(itemHeight: number): WorkspaceRect | null {
    if (!(itemHeight > 0)) return null;
    const gap = stackCount > 0 ? GAP : 0;
    const available = Math.max(0, stackTop - gap);
    const size = Math.min(itemHeight, available);
    if (!(size > 0)) return null;
    const rect: WorkspaceRect = { x: 0, y: Math.max(0, stackTop - gap - size), w: width, h: size };
    stackTop = rect.y;
    stackCount += 1;
    return rect;
  }

  const composer = push(composerHeight);
  const toolbar = push(toolbarHeight);

  let bookmarks: WorkspaceRect | null = null;
  let bookmarkSize = side;
  if (bookmarksOpen && dock === "bottom") {
    const desired = bottomBookmarkHeight(height);
    const gap = stackCount > 0 ? GAP : 0;
    const available = Math.max(0, stackTop - gap);
    // 下侧书签栏与板面之间还有一个 GAP：它最多只能占到「板面保住最小高度」为止
    const maxSize = Math.max(0, available - GAP - MAIN_MIN_HEIGHT);
    const size = Math.min(desired, maxSize);
    const rect = size > 0 ? push(size) : null;
    if (rect) {
      bookmarks = rect;
      bookmarkSize = rect.h;
      if (rect.h < desired) issues.push("bookmarks_capped");
    } else {
      issues.push("bookmarks_capped");
    }
  }

  let chat: WorkspaceRect | null = null;
  let chatMaxHeight = 0;
  if (chatOpen) {
    const gap = stackCount > 0 ? GAP : 0;
    const available = Math.max(0, stackTop - gap);
    const forChatAndBoard = Math.max(0, available - GAP);
    const roomKeepingBoard = Math.max(0, forChatAndBoard - MAIN_MIN_HEIGHT);
    const desired = Math.round(forChatAndBoard * CHAT_MAX_RATIO);
    const size = Math.min(desired, roomKeepingBoard);
    const rect = size > 0 ? push(size) : null;
    if (rect) {
      chat = rect;
      chatMaxHeight = rect.h;
      if (rect.h < CHAT_MIN_HEIGHT) issues.push("chat_capped");
    } else {
      issues.push("chat_capped");
    }
  }

  const bodyHeight = stackCount > 0 ? Math.max(0, stackTop - GAP) : height;
  const boardWidth = side > 0 ? Math.max(0, width - side - GAP) : width;
  const board: WorkspaceRect = { x: 0, y: 0, w: boardWidth, h: bodyHeight };

  if (side > 0) {
    bookmarks = { x: Math.max(0, width - side), y: 0, w: side, h: bodyHeight };
    bookmarkSize = side;
  }

  if (width > 0 && boardWidth < MAIN_MIN_WIDTH) issues.push("board_too_narrow");
  if (height > 0 && bodyHeight < MAIN_MIN_HEIGHT) issues.push("board_too_short");

  return {
    band,
    bookmarkDock: dock,
    bookmarkSize,
    chatOpen: chat !== null,
    chatMaxHeight,
    board,
    bookmarks,
    chat,
    toolbar,
    composer,
    issues,
  };
}
