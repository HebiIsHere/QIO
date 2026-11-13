/**
 * 对话页外壳状态：三栏宽度 / 收起 / 预览全窗口 / 标签持久化。
 *
 * 所有偏好都走服务端 `/api/settings/ui` 的 `conversation_layout`：
 * - 换窗口大小、刷新、重启后都能恢复；
 * - 页面切换不丢（消息、草稿、附件的业务状态各自有现有持久化，不靠这里）；
 * - 窗口变窄的临时收起/临时全窗口**不写回**用户偏好，变宽自动恢复。
 *
 * 写入是防抖的，并且**串行**：同一时刻只有一个写请求在途，后发的排在它之后。
 * 这样「拖动分隔线 → 马上进设置还原」时，还原的写入一定落在旧写入之后；
 * 服务端回显也只在「写出去的内容仍是当前界面状态」时才落回界面（#47）——
 * 迟到旧回执不能覆盖还原结果，也不能覆盖回到对话后新开的标签或新的阅读位置。
 *
 * #47 · 还原默认布局：`resetLayout()` 只重置左右栏宽 / 左栏收起 / 预览开合 / 手动全窗口；
 * 预览标签及顺序、当前标签、各标签的搜索词与阅读位置、任务页签都保留。
 * 保存失败保留当前内存状态并暴露 saveError，由界面给可重试入口。
 */
import { defineStore } from "pinia";
import { api } from "../services/api";
import { contentViewport } from "../utils/windowFrame";
import type { ConversationLayoutPrefs, TaskView } from "../conversation/contracts";
import type { PreviewTabState } from "../conversation/previewTypes";
import {
  CENTER_MIN,
  LEFT_RAIL_WIDTH,
  RIGHT_MIN,
  SPLITTER_WIDTH,
  clampLeftWidth,
  clampRightWidth,
  cloneLayoutPrefs,
  defaultLayoutPrefs,
  layoutPrefsFingerprint,
  resolveConversationLayout,
  restoreDefaultLayout,
  sanitizeLayoutPrefs,
  type ConversationLayout,
} from "../conversation/layoutPrefs";

const SAVE_DEBOUNCE_MS = 40 * 10;

function nowIso(): string {
  return new Date().toISOString();
}

export const useConversationShellStore = defineStore("conversationShell", {
  state: () => ({
    prefs: defaultLayoutPrefs() as ConversationLayoutPrefs,
    /** 可见内容区实际宽度（contentViewport().width）；挂载后由 installViewportTracking 同步。 */
    viewportWidth: typeof window === "undefined" ? 0 : contentViewport().width,
    loaded: false,
    /** 是否真的从服务端读到过布局（还原默认布局前要拿到真实标签，不能拿空默认值清掉）。 */
    layoutLoaded: false,
    saving: false,
    /** 最近一次保存失败的原因（中文、可直接展示）；空串表示没有失败。 */
    saveError: "",
    /** 页面切换后保留的当前话题阅读位置由 MessageStream 自己负责，这里只存外壳。 */
    _timer: 0 as ReturnType<typeof setTimeout> | 0,
    /** 进行中的载入（同一次载入只发一个请求；迟到的旧读取也不能把更新状态盖回去）。 */
    _loading: null as Promise<boolean> | null,
    /** 保存串行链：新的写入排在它后面，保证服务端最后一次写入一定是最后的状态。 */
    _saveChain: null as Promise<boolean> | null,
    /** 排在队列里（含在途）的写请求数，用于 saving 展示。 */
    _saveQueueDepth: 0,
    _disposed: false,
  }),
  getters: {
    /**
     * 一次解析出的真实布局（#83：按中央实际可用宽度算，不用固定断点）。
     * 所有栏宽 / 可见性 / 中央宽度都从这里取，模板与测试不各算一套。
     */
    layout(state): ConversationLayout {
      return resolveConversationLayout(state.viewportWidth, state.prefs);
    },
    /** 窗口过窄导致的临时收起（与用户手动收起分开）。 */
    autoCollapsedLeft(): boolean {
      return !this.prefs.leftCollapsed && !this.layout.leftVisible;
    },
    effectiveLeftCollapsed(): boolean {
      return !this.layout.leftVisible;
    },
    leftVisible(): boolean {
      return this.layout.leftVisible;
    },
    leftWidth(): number {
      return this.layout.leftWidth;
    },
    /** 分栏时右栏实际宽度；全窗口 / 未打开时为 0。 */
    rightWidth(): number {
      return this.layout.rightWidth;
    },
    /** 用户是否打开了右侧预览（与「本次是否以分栏形式呈现」是两件事）。 */
    rightVisible(state): boolean {
      return state.prefs.rightOpen && state.prefs.previewTabs.length > 0;
    },
    /** 分栏形式占位的右栏（全窗口时为 false，避免同一份预览同时渲染两套）。 */
    splitRightVisible(): boolean {
      return this.layout.rightVisible && !this.effectivePreviewFull;
    },
    /** 临时全窗口：三栏放不下时推导出来，不改写 prefs.previewFull。 */
    effectivePreviewFull(state): boolean {
      if (!this.rightVisible) return false;
      if (state.prefs.previewFull) return true;
      return this.layout.previewFull;
    },
    /** 中央实际可用宽度；分栏时保证 >= centerMinWidth（模板可据此留最小宽度）。 */
    centerWidth(): number {
      return this.layout.centerWidth;
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
    /**
     * 载入服务端偏好；失败时保留默认值并给出可见原因（不是静默用默认值）。
     * 返回是否成功：还原默认布局在还没读到真实标签时会先调用它（失败就不写，
     * 否则会用空标签把服务端已保存的标签/阅读位置清掉）。
     */
    async load(): Promise<boolean> {
      if (this._loading) return this._loading;
      const promise = (async (): Promise<boolean> => {
        try {
          const settings = await api.getUISettings();
          this.prefs = sanitizeLayoutPrefs(settings.conversation_layout);
          this.layoutLoaded = true;
          this.saveError = "";
          return true;
        } catch (err) {
          this.saveError = `布局偏好读取失败：${(err as Error)?.message || "未知原因"}（当前使用默认布局）`;
          return false;
        } finally {
          this.loaded = true;
        }
      })();
      this._loading = promise;
      try {
        return await promise;
      } finally {
        if (this._loading === promise) this._loading = null;
      }
    },
    /** 绑定窗口宽度跟踪（返回清理函数；组件卸载时调用）。 */
    installViewportTracking(): () => void {
      if (typeof window === "undefined") return () => undefined;
      const sync = () => {
        // 只认可见内容区的实际宽度（透明留白不算）；读不到时保持上一次的值，
        // 不用硬编码 1440 冒充窗口宽度。
        const width = contentViewport().width;
        if (width > 0) this.viewportWidth = width;
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
    /** 立即写入并等它真正结束（页面切走 / 关闭前 / 还原默认布局后可据此给反馈）。 */
    async flush(): Promise<boolean> {
      if (this._timer) {
        clearTimeout(this._timer);
        this._timer = 0;
      }
      return this.persist();
    },
    /**
     * 排队保存（串行执行）。
     *
     * 为什么必须串行：写请求携带整份快照，服务端按到达顺序覆盖。拖动分隔线的旧请求若在
     * 还原的新请求之后到达，服务端就会停在旧布局。排队保证「最后发出的状态最后写入」。
     */
    persist(): Promise<boolean> {
      if (this._disposed) return Promise.resolve(false);
      this._saveQueueDepth += 1;
      this.saving = true;
      const previous = this._saveChain ?? Promise.resolve(true);
      const run = previous.then(
        () => this._persistOnce(),
        () => this._persistOnce(),
      );
      const settled = run.finally(() => {
        this._saveQueueDepth = Math.max(0, this._saveQueueDepth - 1);
        this.saving = this._saveQueueDepth > 0;
      });
      this._saveChain = settled.then(
        () => true,
        () => false,
      );
      return settled;
    },
    /** 真正的一次写入；回执只有与当前界面状态一致时才回填（#47 迟到旧回执保护）。 */
    async _persistOnce(): Promise<boolean> {
      // 发出时的快照：读取期间页面里继续改标签/阅读位置也不会改变这次写出的内容。
      const sentFingerprint = layoutPrefsFingerprint(this.prefs);
      const payload: ConversationLayoutPrefs = {
        ...cloneLayoutPrefs(this.prefs),
        updatedAt: nowIso(),
      };
      try {
        const res = await api.saveConversationLayout(payload);
        // 服务端可能夹取/规范化过：用它返回的版本，避免本地与服务端长期不一致。
        if (res && res.ok === false) {
          // 明确被拒（不是网络异常）时同样不能当成保存成功。
          throw new Error("服务端没有接受这次布局偏好");
        }
        // 关键竞态保护：回执是「这次写出去的那份状态」。若等待期间用户又还原了布局、
        // 开了标签或滚了位置，当前指纹已变化 —— 旧回执直接丢弃，绝不把新状态盖回去。
        if (res?.conversation_layout && layoutPrefsFingerprint(this.prefs) === sentFingerprint) {
          this.prefs = sanitizeLayoutPrefs(res.conversation_layout);
        }
        this.saveError = "";
        return true;
      } catch (err) {
        this.saveError = `布局偏好保存失败：${(err as Error)?.message || "未知原因"}（当前布局仍会保留，可重试）`;
        return false;
      }
    },
    /**
     * #47 · 还原默认布局。
     *
     * 只重置左右栏宽 / 左栏收起 / 右侧预览开合 / 手动全窗口；预览标签及顺序、当前标签、
     * 各标签的搜索词与阅读位置、任务页签原样保留。窗口太窄时的临时收起/临时全窗口由
     * resolveConversationLayout 继续推导，不会被写成手动偏好。
     *
     * 返回是否**保存成功**：内存里的还原结果先生效（失败也保留，界面给重试）；
     * 还没读过服务端布局时先读一次，避免用空默认值把未知的标签/阅读位置清掉。
     */
    async resetLayout(): Promise<boolean> {
      if (!this.layoutLoaded) {
        const ok = await this.load();
        if (!ok) return false;
      }
      this.prefs = restoreDefaultLayout(this.prefs);
      return this.flush();
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
      // 只存用户实际想要的宽度；窄窗口显示时再按实际视口收缩（不改写保存值，#47）。
      this.prefs.rightWidth = clampRightWidth(width);
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
