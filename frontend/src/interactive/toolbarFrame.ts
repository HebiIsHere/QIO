/**
 * 底部工具栏高度的运行时桥（用户决策 / 任务 C）。
 *
 * 关系编辑器（BoardCanvas 的 .link-editor）必须停在**工具栏上沿之上**，
 * 但工具栏高度随窗口宽度 / 内容换行变化，不能写死。口径：
 * - InteractiveView 用 ResizeObserver 量「舞台底边 → 工具栏顶边」的真实距离
 *   （已含 --im-toolbar-bottom 的留白），通过 {@link publishToolbarClearance} 发布；
 * - 关系编辑器用 {@link readToolbarClearance} 读（响应式），算 bottom = clearance + 间距；
 * - 没量到时用 {@link DEFAULT_TOOLBAR_CLEARANCE} 兜底，绝不让编辑器压住工具栏。
 *
 * 纯函数（linkEditorLayout）单独可测；这里不读 DOM、不发明第二套几何。
 */
import { ref } from "vue";

/** 还没量到工具栏时的兜底净空（工具栏本体约 56 + 底部留白约 16）。 */
export const DEFAULT_TOOLBAR_CLEARANCE = 72;

/** 编辑器与工具栏顶部之间保持的间距。 */
export const LINK_EDITOR_GAP = 10;

/** 窄窗口（<= 800px）时编辑器贴底全宽：仍要让出工具栏带。 */
export const LINK_EDITOR_NARROW_WIDTH = 800;

const toolbarClearance = ref<number | null>(null);

/**
 * 发布「舞台底边 → 工具栏顶边」的净空（px）。
 * 传 null / 非法值 = 回到兜底值（例如工具栏卸载）。
 */
export function publishToolbarClearance(value: number | null | undefined): void {
  if (value === null || value === undefined) {
    toolbarClearance.value = null;
    return;
  }
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number) || number < 0) {
    toolbarClearance.value = null;
    return;
  }
  toolbarClearance.value = Math.round(number);
}

/** 当前净空（响应式：组件 computed 里读它会在工具栏尺寸变化时重算）。 */
export function readToolbarClearance(): number {
  return toolbarClearance.value ?? DEFAULT_TOOLBAR_CLEARANCE;
}

/** 测试 / 组件重建时清空（不把上一块板面的测量值带过去）。 */
export function resetToolbarClearance(): void {
  toolbarClearance.value = null;
}

export interface LinkEditorLayout {
  /** 距容器底边的距离（px）：工具栏净空 + 间距 */
  bottom: number;
  /** 是否窄窗口布局（贴底全宽，自动让出工具栏带） */
  narrow: boolean;
  left: number;
  right: number | null;
  width: string;
}

/**
 * 关系编辑器布局（纯函数）：
 * - bottom 永远 = 工具栏净空 + 间距（打开时不盖工具栏）；
 * - 宽窗口：左侧留 16px、宽 420px（随视口收窄到 200..420）；
 * - 窄窗口（<= 800px）：左右各 8px、贴底全宽，但底部仍让出工具栏带。
 */
export function linkEditorLayout(input: {
  clearance?: number | null;
  viewportWidth?: number;
  gap?: number;
}): LinkEditorLayout {
  const clearance =
    typeof input.clearance === "number" && Number.isFinite(input.clearance) && input.clearance >= 0
      ? input.clearance
      : DEFAULT_TOOLBAR_CLEARANCE;
  const gap = typeof input.gap === "number" && Number.isFinite(input.gap) && input.gap >= 0 ? input.gap : LINK_EDITOR_GAP;
  const viewportWidth =
    typeof input.viewportWidth === "number" && Number.isFinite(input.viewportWidth) && input.viewportWidth > 0
      ? input.viewportWidth
      : 1280;
  const narrow = viewportWidth <= LINK_EDITOR_NARROW_WIDTH;
  return {
    bottom: Math.round(clearance + gap),
    narrow,
    left: narrow ? 8 : 16,
    right: narrow ? 8 : null,
    width: narrow ? "auto" : Math.min(420, Math.max(200, viewportWidth - 64)) + "px",
  };
}
