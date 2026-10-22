/**
 * 对话页外壳状态：三栏宽度 / 收起 / 预览全窗口 / 标签持久化。
 *
 * 所有偏好都走服务端 `/api/settings/ui` 的 `conversation_layout`：
 * - 换窗口大小、刷新、重启后都能恢复；
 * - 页面切换不丢（消息、草稿、附件的业务状态各自有现有持久化，不靠这里）；
 * - 窗口变窄的临时收起/临时全窗口**不写回**用户偏好，变宽自动恢复。
 *
 * 写入是防抖的；保存失败保留当前内存状态并暴露 saveError，由界面给可重试入口。
 */
import { defineStore } from "pinia";
import { api } from "../services/api";
import type { ConversationLayoutPrefs, TaskView } from "../conversation/contracts";
import type { PreviewTabState } from "../conversation/previewTypes";
import {
  CENTER_MIN,
  LEFT_RAIL_WIDTH,
  RIGHT_MIN,
  SPLITTER_WIDTH,
  clampLeftWidth,
  clampRightWidth,
  defaultLayoutPrefs,
  sanitizeLayoutPrefs,
  shouldAutoCollapseLeft,
  shouldUseFullPreview,
} from "../conversation/layoutPrefs";

const SAVE_DEBOUNCE_MS = 40 * 10;

function nowIso(): string {
  return new Date().toISOString();
}

export const useConversationShellStore = defineStore("conversationShell", {
  state: () => ({
    prefs: defaultLayoutPrefs() as ConversationLayoutPrefs,
    viewportWidth: 1440,
    loaded: false,
    saving: false,
    /** 最近一次保存失败的原因（中文、可直接展示）；空串表示没有失败。 */
    saveError: "",
    /** 页面切换后保留的当前话题阅读位置由 MessageStream 自己负责，这里只存外壳。 */
    _timer: 0 as ReturnType<typeof setTimeout> | 0,
    _disposed: false,
  }),
  getters: {
    /** 窗口过窄导致的临时收起（与用户手动收起分开）。 */
    autoCollapsedLeft(state): boolean {
      return !state.prefs.leftCollapsed && shouldAutoCollapseLeft(state.viewportWidth);
    },
    effectiveLeftCollapsed(state): boolean {
      return state.prefs.leftCollapsed || shouldAutoCollapseLeft(state.viewportWidth);
    },
    leftVisible(): boolean {
      return !this.effectiveLeftCollapsed;
    },
    leftWidth(): number {
      return this.effectiveLeftCollapsed ? LEFT_RAIL_WIDTH : clampLeftWidth(this.prefs.leftWidth);
    },
    rightWidth(state): number {
      return clampRightWidth(state.prefs.rightWidth, state.viewportWidth, this.leftWidth);
    },
    /** 右栏是否可见：用户打开 + 有标签。 */
    rightVisible(state): boolean {
      return state.prefs.rightOpen && state.prefs.previewTabs.length > 0;
    },
    /** 临时全窗口：三栏放不下时推导出来，不改写 prefs.previewFull。 */
    effectivePreviewFull(state): boolean {
      if (!this.rightVisible) return false;
      if (state.prefs.previewFull) return true;
      return shouldUseFullPreview(state.viewportWidth, this.effectiveLeftCollapsed, this.rightWidth);
    },
    /** 中央区可读宽度下限：布局实现用它做最后一道保护。 */
    centerMinWidth(): number {
      return CENTER_MIN;
    },
    splitterWidth(): number {
      return SPLITTER_WIDTH;
    },
    rightMinWidth(): number {
      return RIGHT_MIN;
    },
  },
  actions: {
    /** 载入服务端偏好；失败时保留默认值并给出可见原因（不是静默用默认值）。 */
    async load() {
      try {
        const settings = await api.getUISettings();
        this.prefs = sanitizeLayoutPrefs(settings.conversation_layout);
        this.saveError = "";
      } catch (err) {
        this.saveError = `布局偏好读取失败：${(err as Error)?.message || "未知原因"}（当前使用默认布局）`;
      } finally {
        this.loaded = true;
      }
    },
    /** 绑定窗口宽度跟踪（返回清理函数；组件卸载时调用）。 */
    installViewportTracking(): () => void {
      if (typeof window === "undefined") return () => undefined;
      const sync = () => {
        this.viewportWidth = window.innerWidth || 1440;
      };
      sync();
      window.addEventListener("resize", sync, { passive: true });
      return () => window.removeEventListener("resize", sync);
    },
    scheduleSave() {
      if (this._disposed) return;
      if (this._timer) clearTimeout(this._timer);
      this._timer = setTimeout(() => {
        this._timer = 0;
        void this.persist();
      }, SAVE_DEBOUNCE_MS);
    },
    /** 立即写入（页面切走 / 关闭前可调用）。 */
    async flush() {
      if (this._timer) {
        clearTimeout(this._timer);
        this._timer = 0;
      }
      await this.persist();
    },
    async persist() {
      if (this.saving) {
        // 正在写：稍后再写一次，保证最后一次修改一定落盘。
        this.scheduleSave();
        return;
      }
      this.saving = true;
      try {
        const payload: ConversationLayoutPrefs = { ...this.prefs, updatedAt: nowIso() };
        const res = await api.saveConversationLayout(payload);
        // 服务端可能夹取/规范化过：用它返回的版本，避免本地与服务端长期不一致。
        if (res?.conversation_layout) this.prefs = sanitizeLayoutPrefs(res.conversation_layout);
        this.saveError = "";
      } catch (err) {
        this.saveError = `布局偏好保存失败：${(err as Error)?.message || "未知原因"}（当前布局仍会保留，可重试）`;
      } finally {
        this.saving = false;
      }
    },
    setLeftWidth(width: number) {
      this.prefs.leftWidth = clampLeftWidth(width);
      this.prefs.leftCollapsed = false;
      this.scheduleSave();
    },
    toggleLeft() {
      this.prefs.leftCollapsed = !this.prefs.leftCollapsed;
      this.scheduleSave();
    },
    setRightWidth(width: number) {
      this.prefs.rightWidth = clampRightWidth(width, this.viewportWidth, this.leftWidth);
      this.scheduleSave();
    },
    setRightOpen(open: boolean) {
      this.prefs.rightOpen = open;
      if (!open) this.prefs.previewFull = false;
      this.scheduleSave();
    },
    setPreviewFull(full: boolean) {
      this.prefs.previewFull = full;
      this.scheduleSave();
    },
    setTaskTab(tab: TaskView) {
      this.prefs.taskTab = tab;
      this.scheduleSave();
    },
    setPreviewTabs(tabs: PreviewTabState[], activeId: string | null) {
      const rest = this.prefs.previewTabs.filter((tab) => !tabs.some((next) => next.id === tab.id));
      this.prefs.previewTabs = [...rest, ...tabs].slice(-12);
      const sentinel = this.prefs.previewTabs.length
        ? this.prefs.previewTabs[this.prefs.previewTabs.length - 1]
        : null;
      this.prefs.activePreviewTabId =
        activeId && this.prefs.previewTabs.some((tab) => tab.id === activeId) ? activeId : (sentinel?.id ?? null);
      this.scheduleSave();
    },
    setActivePreviewTab(id: string | null) {
      this.prefs.activePreviewTabId = id;
      this.scheduleSave();
    },
    dispose() {
      this._disposed = true;
      if (this._timer) clearTimeout(this._timer);
      this._timer = 0;
    },
  },
});
