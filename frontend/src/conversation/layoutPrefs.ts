/**
 * 对话页布局纯函数：栏宽夹取、窄窗口规则。
 *
 * 为什么单独一份纯函数：左右两栏、全窗口预览、窗口缩放三条路径都要用同一套
 * 阈值；写进组件里就会出现「左边认为 1024 该收起、右边认为不该」这种自相矛盾。
 * 任何函数都不读 DOM / window，方便直接单测。
 */
import type { ConversationLayoutPrefs } from "./contracts";
import type { PreviewTabState } from "./previewTypes";

export const LEFT_MIN = 232;
export const LEFT_MAX = 420;
export const LEFT_DEFAULT = 292;

export const RIGHT_MIN = 340;
export const RIGHT_MAX = 760;
export const RIGHT_DEFAULT = 420;

/** 中央对话区可读的最小宽度：低于它就先收左栏，再让预览占满窗口。 */
export const CENTER_MIN = 460;

/** 窗口窄于它：临时收起左栏（不写回用户偏好）。 */
export const LEFT_AUTO_COLLAPSE_BELOW = 1180;

/** 收起左栏后仍放不下「中央 + 右栏」时，预览改为全窗口。 */
export const PREVIEW_FULL_BELOW = 980;

export const SPLITTER_WIDTH = 10;
/** 收起后的左侧任务入口宽度。 */
export const LEFT_RAIL_WIDTH = 48;

export function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.round(value)));
}

export function clampLeftWidth(width: number): number {
  return clampNumber(width, LEFT_MIN, LEFT_MAX);
}

/** 右栏宽度同时受窗口宽度约束：至少给中央区留下 CENTER_MIN。 */
export function clampRightWidth(width: number, viewportWidth: number, leftWidth: number): number {
  const maxByViewport = Math.max(
    RIGHT_MIN,
    Math.floor(viewportWidth - leftWidth - SPLITTER_WIDTH * 2 - CENTER_MIN),
  );
  return clampNumber(width, RIGHT_MIN, Math.min(RIGHT_MAX, maxByViewport));
}

/** 窄窗口：先临时收起左栏。窗口变宽后自动恢复，用户手动收起偏好不变。 */
export function shouldAutoCollapseLeft(viewportWidth: number): boolean {
  return viewportWidth < LEFT_AUTO_COLLAPSE_BELOW;
}

/**
 * 三栏放不下时，右侧预览改为全窗口模式（带返回对话入口）。
 * 与用户手动 `previewFull` 分开：临时全窗口不覆盖保存的偏好。
 */
export function shouldUseFullPreview(
  viewportWidth: number,
  leftCollapsed: boolean,
  rightWidth: number,
): boolean {
  const left = leftCollapsed ? LEFT_RAIL_WIDTH : LEFT_MIN;
  const available = viewportWidth - left - SPLITTER_WIDTH * 2;
  // 放得下「中央最小可读宽度 + 右栏最小宽度」才并排；否则右栏改为全窗口。
  return available < CENTER_MIN + Math.max(RIGHT_MIN, rightWidth);
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
  if (right !== null) out.rightWidth = clampRightWidth(right, 1440, out.leftWidth);
  if (typeof value.rightOpen === "boolean") out.rightOpen = value.rightOpen;
  if (value.taskTab === "history" || value.taskTab === "current") out.taskTab = value.taskTab;
  if (typeof value.previewFull === "boolean") out.previewFull = value.previewFull;
  const active = stringOrNull(value.activePreviewTabId);
  if (Array.isArray(value.previewTabs)) {
    const tabs: PreviewTabState[] = [];
    for (const raw of value.previewTabs.slice(0, 12)) {
      if (!raw || typeof raw !== "object") continue;
      const tab = raw as Record<string, unknown>;
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
