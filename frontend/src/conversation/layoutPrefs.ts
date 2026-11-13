/**
 * 对话页布局纯函数：按**中央实际可用宽度**解析三栏布局。
 *
 * 为什么单独一份纯函数：左右两栏、全窗口预览、窗口缩放三条路径都要用同一套
 * 阈值；写进组件里就会出现「左边认为该收起、右边认为不该」这种自相矛盾。
 * 任何函数都不读 DOM / window，方便直接单测；实际视口宽度由调用方传入
 * （ConversationShell 用 utils/windowFrame.contentViewport().width），
 * 不再用硬编码 1440 做夹取或断点判断。
 *
 * 0.3.2 审计 #83/#47：旧实现有两条硬断点（1180 提前收左栏、980 提前全窗口预览），
 * 断点两侧中央宽度会整块跳变；窗口稍窄时左右栏也不受中央下限约束（输入区可能被压到
 * 300px 以下）。现在规则只有一条：**先保中央最小可读/可输入宽度，再谈栏宽**。
 */
import type { ConversationLayoutPrefs } from "./contracts";
import type { PreviewTabState } from "./previewTypes";

export const LEFT_MIN = 232;
export const LEFT_MAX = 420;
export const LEFT_DEFAULT = 292;

export const RIGHT_MIN = 340;
export const RIGHT_MAX = 760;
export const RIGHT_DEFAULT = 420;

/** 中央对话区可读 / 输入可用的最小宽度：低于它就先缩栏、再收栏、最后改全窗口预览。 */
export const CENTER_MIN = 460;

export const SPLITTER_WIDTH = 12;
/** 收起后的左侧任务入口宽度（模板里另有 12px 槽位，等价于一个分隔条）。 */
export const LEFT_RAIL_WIDTH = 48;

export function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.round(value)));
}

export function clampLeftWidth(width: number): number {
  return clampNumber(width, LEFT_MIN, LEFT_MAX);
}

/**
 * 右栏宽度夹取。
 *
 * 只给 width：夹到用户可调范围（保存偏好时用，不按窗口改写用户值）。
 * 同时给 viewportWidth / leftWidth：再按实际视口压到「保住中央下限」的上限
 * （排布时用；这是唯一允许用视口算栏宽的地方）。
 */
export function clampRightWidth(width: number, viewportWidth?: number, leftWidth?: number): number {
  if (
    typeof viewportWidth !== "number" ||
    !Number.isFinite(viewportWidth) ||
    typeof leftWidth !== "number" ||
    !Number.isFinite(leftWidth)
  ) {
    return clampNumber(width, RIGHT_MIN, RIGHT_MAX);
  }
  const maxByViewport = Math.max(
    RIGHT_MIN,
    Math.floor(viewportWidth - leftWidth - SPLITTER_WIDTH * 2 - CENTER_MIN),
  );
  return clampNumber(width, RIGHT_MIN, Math.min(RIGHT_MAX, maxByViewport));
}

/** 一次解析结果：模板与 store getter 都只认这一份（栏宽、可见性、中央宽度不各算一套）。 */
export interface ConversationLayout {
  leftVisible: boolean;
  /** 实际渲染的左栏宽度（左栏可见时是夹取后的值，收起时是收起栏宽度）。 */
  leftWidth: number;
  /** 右侧预览是否以分栏形式占位（全窗口 / 未打开时为 false）。 */
  rightVisible: boolean;
  rightWidth: number;
  /** 右侧预览是否改为全窗口（临时全窗口或用户手动全窗口）。 */
  previewFull: boolean;
  /** 中央实际可用宽度（px）。分栏时保证 >= CENTER_MIN。 */
  centerWidth: number;
}

const RAIL_TOTAL = LEFT_RAIL_WIDTH + SPLITTER_WIDTH;

function rightRequested(prefs: ConversationLayoutPrefs): boolean {
  return Boolean(prefs.rightOpen) && prefs.previewTabs.length > 0;
}

/**
 * 解析布局。
 *
 * 优先级（从高到低）：
 * 1. 中央至少保留 CENTER_MIN（输入区不能被压没）；
 * 2. 用户手动收起 / 手动栏宽仍然有效；
 * 3. 窗口变窄时先收右栏到下限 → 再收左栏到下限 → 仍不行才临时收起左栏
 *    → 最后才把预览改为全窗口（不写回偏好）。
 *
 * 因此「断点」只发生在中央真的放不下的那一点上，且收起左栏的瞬间中央宽度不跳变
 * （空出来的宽度立刻由右栏补位，见 layoutPrefs.test.ts 的 1055/1056 用例）。
 */
export function resolveConversationLayout(
  viewportWidth: number,
  prefs: ConversationLayoutPrefs,
): ConversationLayout {
  const viewport = Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0;
  const wantsRight = rightRequested(prefs);

  if (!wantsRight) {
    const left = fitLeftOnly(viewport, prefs.leftCollapsed, prefs.leftWidth);
    return {
      leftVisible: !left.collapsed,
      leftWidth: left.width,
      rightVisible: false,
      rightWidth: 0,
      previewFull: false,
      centerWidth: left.centerWidth,
    };
  }

  if (prefs.leftCollapsed) {
    // 用户手动收起：先按收起栏排；右栏也放不下才全窗口。
    const right = fitRight(viewport, RAIL_TOTAL, prefs.rightWidth);
    return {
      leftVisible: false,
      leftWidth: LEFT_RAIL_WIDTH,
      rightVisible: right.split,
      rightWidth: right.width,
      previewFull: !right.split,
      centerWidth: right.centerWidth,
    };
  }

  const preferredLeft = clampLeftWidth(prefs.leftWidth);
  // 1) 先尝试三栏并排：右栏先按用户偏好/视口夹取（此时左栏还是偏好宽度）。
  const rightWidth = clampRightWidth(prefs.rightWidth, viewport, preferredLeft);
  const centerWithPreferredLeft = viewport - preferredLeft - SPLITTER_WIDTH * 2 - rightWidth;
  if (centerWithPreferredLeft >= CENTER_MIN) {
    return {
      leftVisible: true,
      leftWidth: preferredLeft,
      rightVisible: true,
      rightWidth,
      previewFull: false,
      centerWidth: centerWithPreferredLeft,
    };
  }

  // 2) 中央不够：先把左栏收到「刚好保住中央」的位置（右栏已经在下限或压不到更小）。
  const maxLeft = viewport - SPLITTER_WIDTH * 2 - rightWidth - CENTER_MIN;
  if (maxLeft >= LEFT_MIN) {
    const leftWidth = Math.min(preferredLeft, maxLeft);
    return {
      leftVisible: true,
      leftWidth,
      rightVisible: true,
      rightWidth,
      previewFull: false,
      centerWidth: viewport - leftWidth - SPLITTER_WIDTH * 2 - rightWidth,
    };
  }

  // 3) 左栏收到下限也放不下：临时收起左栏（不写回偏好），右栏按腾出的空间重新排。
  const right = fitRight(viewport, RAIL_TOTAL, prefs.rightWidth);
  return {
    leftVisible: false,
    leftWidth: LEFT_RAIL_WIDTH,
    rightVisible: right.split,
    rightWidth: right.width,
    previewFull: !right.split,
    centerWidth: right.centerWidth,
  };
}

interface LeftFit {
  collapsed: boolean;
  width: number;
  centerWidth: number;
}

/** 只有左栏时的排布：先把左栏收到「刚好保住中央」，连下限都放不下才临时收起。 */
function fitLeftOnly(viewport: number, leftCollapsed: boolean, preferredLeft: number): LeftFit {
  if (leftCollapsed) {
    return {
      collapsed: true,
      width: LEFT_RAIL_WIDTH,
      centerWidth: Math.max(0, viewport - RAIL_TOTAL),
    };
  }
  const maxLeft = viewport - SPLITTER_WIDTH - CENTER_MIN;
  if (maxLeft < LEFT_MIN) {
    return {
      collapsed: true,
      width: LEFT_RAIL_WIDTH,
      centerWidth: Math.max(0, viewport - RAIL_TOTAL),
    };
  }
  const width = Math.min(clampLeftWidth(preferredLeft), maxLeft);
  return { collapsed: false, width, centerWidth: viewport - width - SPLITTER_WIDTH };
}

interface RightFit {
  split: boolean;
  width: number;
  centerWidth: number;
}

/** 给定左栏已占宽度，判断右栏还能不能并排；能则返回按视口夹过的宽度，否则改为全窗口。 */
function fitRight(viewport: number, leftOccupied: number, preferredRight: number): RightFit {
  const maxRight = viewport - leftOccupied - SPLITTER_WIDTH - CENTER_MIN;
  if (maxRight >= RIGHT_MIN) {
    const width = clampNumber(Math.min(preferredRight, maxRight), RIGHT_MIN, RIGHT_MAX);
    return {
      split: true,
      width,
      centerWidth: viewport - leftOccupied - SPLITTER_WIDTH - width,
    };
  }
  return { split: false, width: 0, centerWidth: Math.max(0, viewport - leftOccupied) };
}

export function defaultLayoutPrefs(): ConversationLayoutPrefs {
  return {
    version: 1,
    leftWidth: LEFT_DEFAULT,
    leftCollapsed: false,
    rightWidth: RIGHT_DEFAULT,
    rightOpen: false,
    taskTab: "current",
    previewFull: false,
    previewTabs: [],
    activePreviewTabId: null,
  };
}

/** 深拷贝偏好（标签、目标与阅读位置各自复制），供「发出时的快照」和「只改字段的还原」使用。 */
export function cloneLayoutPrefs(prefs: ConversationLayoutPrefs): ConversationLayoutPrefs {
  return {
    ...prefs,
    previewTabs: prefs.previewTabs.map((tab) => ({
      id: tab.id,
      target: { ...tab.target },
      position: { ...tab.position },
    })),
  };
}

/**
 * #47 · 还原默认布局：**只重置用户可调的布局字段**。
 *
 * 会改：左右栏宽、左栏收起、右侧预览开合、手动全窗口（previewFull）。
 * 不动：预览标签及顺序、当前标签、各标签的搜索词与阅读位置、任务页签（当前/历史）、
 * 聊天内容/草稿/当前话题（它们根本不在这个结构里，由各自的 store 持久化）。
 *
 * 「窗口太窄导致的临时收起 / 临时全窗口」不是保存值，本来就不在这里：
 * 还原后仍由 resolveConversationLayout 按实际宽度推导，且不会写回偏好。
 */
export function restoreDefaultLayout(prefs: ConversationLayoutPrefs): ConversationLayoutPrefs {
  const defaults = defaultLayoutPrefs();
  const next = cloneLayoutPrefs(prefs);
  next.version = defaults.version;
  next.leftWidth = defaults.leftWidth;
  next.leftCollapsed = defaults.leftCollapsed;
  next.rightWidth = defaults.rightWidth;
  next.rightOpen = defaults.rightOpen;
  next.previewFull = defaults.previewFull;
  return next;
}

function finiteOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/**
 * 服务端/旧版本/损坏数据 → 合法偏好。
 *
 * 不信任输入：布局偏好只是显示偏好，读坏了不应该让整页白屏。
 * 越界值夹取、未知字段忽略、缺失字段用默认值；坏掉的标签直接丢（位置可以重读）。
 *
 * 注意：**只用用户可调范围夹取，不按视口夹取**（旧实现用硬编码 1440，导致 1920 屏
 * 保存的 700px 右栏读回被改成 664 —— 用户手动布局偏好被静默改写）。视口约束在
 * resolveConversationLayout 里按实际宽度做，保存值保持原样。
 */
export function sanitizeLayoutPrefs(raw: unknown): ConversationLayoutPrefs {
  const base = defaultLayoutPrefs();
  if (!raw || typeof raw !== "object") return base;
  const value = raw as Record<string, unknown>;
  const out: ConversationLayoutPrefs = { ...base };
  const left = finiteOrNull(value.leftWidth);
  if (left !== null) out.leftWidth = clampLeftWidth(left);
  if (typeof value.leftCollapsed === "boolean") out.leftCollapsed = value.leftCollapsed;
  const right = finiteOrNull(value.rightWidth);
  if (right !== null) out.rightWidth = clampRightWidth(right);
  if (typeof value.rightOpen === "boolean") out.rightOpen = value.rightOpen;
  if (value.taskTab === "history" || value.taskTab === "current") out.taskTab = value.taskTab;
  if (typeof value.previewFull === "boolean") out.previewFull = value.previewFull;
  const active = stringOrNull(value.activePreviewTabId);
  if (Array.isArray(value.previewTabs)) {
    const tabs: PreviewTabState[] = [];
    for (const rawTab of value.previewTabs.slice(0, 12)) {
      if (!rawTab || typeof rawTab !== "object") continue;
      const tab = rawTab as Record<string, unknown>;
      const target = (tab.target ?? {}) as Record<string, unknown>;
      const position = (tab.position ?? {}) as Record<string, unknown>;
      const id = stringOrNull(tab.id) ?? stringOrNull(target.id);
      if (!id) continue;
      const kind = stringOrNull(target.kind) ?? "unsupported";
      const source = stringOrNull(target.source) ?? "attachment";
      tabs.push({
        id,
        target: {
          id,
          kind: kind as PreviewTabState["target"]["kind"],
          title: stringOrNull(target.title) ?? "文件",
          source: source as PreviewTabState["target"]["source"],
          attachmentId: stringOrNull(target.attachmentId) ?? undefined,
          version: stringOrNull(target.version) ?? undefined,
          messageId: stringOrNull(target.messageId) ?? undefined,
          blockIndex: finiteOrNull(target.blockIndex) ?? undefined,
          taskId: stringOrNull(target.taskId) ?? undefined,
          topicId: stringOrNull(target.topicId) ?? undefined,
          filePath: stringOrNull(target.filePath) ?? undefined,
          mime: stringOrNull(target.mime) ?? undefined,
          size: finiteOrNull(target.size) ?? undefined,
        },
        position: {
          scrollTop: finiteOrNull(position.scrollTop) ?? undefined,
          page: finiteOrNull(position.page) ?? undefined,
          sheet: stringOrNull(position.sheet) ?? undefined,
          zoom: finiteOrNull(position.zoom) ?? undefined,
          search: stringOrNull(position.search) ?? undefined,
          line: finiteOrNull(position.line) ?? undefined,
        },
      });
    }
    out.previewTabs = tabs;
  }
  const lastTab = out.previewTabs.length ? out.previewTabs[out.previewTabs.length - 1] : null;
  out.activePreviewTabId =
    active && out.previewTabs.some((tab) => tab.id === active) ? active : (lastTab?.id ?? null);
  const updated = stringOrNull(value.updatedAt);
  if (updated) out.updatedAt = updated;
  return out;
}

/** 键顺序无关的规范 JSON：用于比较「两次快照是不是同一份内容」。 */
function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record)
      .filter((key) => record[key] !== undefined)
      .sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? String(value);
}

/**
 * 持久化内容指纹（排除只表示「写于何时」的顶层 updatedAt）。
 *
 * 用途（#47 竞态保护）：保存回执只有在「这次写出去的内容 === 当前界面状态」时才允许落回
 * 界面。拖动分隔线后立刻还原、或回到对话后又开了标签/滚了位置时，迟到旧回执的指纹与当前
 * 不一致，于是被忽略 —— 旧数据不能把还原结果、较新的标签或阅读位置覆盖回去。
 */
export function layoutPrefsFingerprint(prefs: ConversationLayoutPrefs): string {
  const rest: Record<string, unknown> = { ...prefs };
  delete rest.updatedAt;
  return canonicalJson(rest);
}
