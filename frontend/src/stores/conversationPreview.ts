/**
 * 对话页右侧只读预览的运行时 store（C 拥有）。
 *
 * 分工（唯一事实源）：
 *   * **标签与阅读位置**写在 conversationShell 的 previewTabs / activePreviewTabId（服务端
 *     conversation_layout 持久化，刷新/收起重开/全窗口切换都能恢复）；
 *   * 本 store 只放**运行时**内容：当前这一版预览结果、失败原因、更新提示、回复代码/任务
 *     成果的正文（只在内存里，绝不写进布局偏好）、以及每个标签的请求代次。
 *
 * 诚实规则：
 *   * 打开文件才打开预览栏；新附件/新成果**不自动展开**；
 *   * 文件更新只更新内容与提示，**不切换标签、不重新打开已收起的栏**；
 *   * 更新失败时保留上次内容并明确说「当前显示的是上次读取的内容」；
 *   * 迟到的旧请求按代次丢弃，旧请求不得覆盖新版本。
 */
import { defineStore } from "pinia";
import {
  AttachmentRequestError,
  PreviewAbortedError,
  downloadAttachment,
  fetchAttachmentPreview,
  isDesktopShell,
  openAttachment,
} from "../services/attachments";
import type { PreviewEnvelope } from "../services/attachments";
import { useConversationShellStore } from "./conversationShell";
import type { PreviewPosition, PreviewTabState, PreviewTarget } from "../conversation/previewTypes";
import { attachmentPreviewId } from "../conversation/previewTypes";
import {
  FILE_UPDATE_MAX_FAILURES,
  activePreviewTab,
  attachmentPreviewTarget,
  attachmentRefForTarget,
  classifyPreviewProblem,
  closePreviewTab,
  createPreviewVersionGuard,
  isFileVersionChanged,
  mediaPreviewProblem,
  mergePreviewPosition,
  openPreviewTab,
  positionForKind,
  previewKindForName,
  taskOutputPreviewTarget,
  replyCodePreviewTarget,
} from "../conversation/preview";
import type {
  AttachmentPreviewInput,
  PreviewProblem,
  PreviewVersionGuard,
  ReplyCodePreviewInput,
  TaskOutputPreviewInput,
} from "../conversation/preview";
import type { PreviewKind } from "../conversation/previewTypes";

// ---------------------------------------------------------------------------
// 运行时形状
// ---------------------------------------------------------------------------

export type PreviewRuntimePhase = "loading" | "ready" | "problem";

export interface PreviewRuntime {
  phase: PreviewRuntimePhase;
  /** 结构化预览结果（回复代码/任务输出正文没有 envelope）。 */
  envelope: PreviewEnvelope | null;
  problem: PreviewProblem | null;
  /** 当前内容对应的真实文件版本（后端 version；没有就是空串，不编造）。 */
  version: string;
  /** 「文件已更新」这类短暂提示。 */
  notice: string;
  /** 更新检查失败时的说明（此时页面显示的仍是上次读取的内容）。 */
  updateError: string;
  /** 连续失败次数（超过上限暂停自动检查）。 */
  updateFailures: number;
  loadedAt: number;
}

/** 回复代码 / 任务输出正文：只在内存里，不写布局偏好（契约 §4.2 只存轻量位置）。 */
export interface PreviewDocumentInput {
  text?: string;
  language?: string;
  /** 已知「正文已被保留策略清理 / 不在当前页面」时的真实说明。 */
  unavailableReason?: string;
}

export type PreviewOpenOutcome = "opened" | "skipped";

export interface PreviewActionResult {
  ok: boolean;
  note: string;
}

/** 模块级：请求代次 / 在途控制器 / 提示计时器（不进 Pinia state，避免响应式代理 controller）。 */
const guards = new Map<string, PreviewVersionGuard>();
const controllers = new Map<string, AbortController>();
const noticeTimers = new Map<string, ReturnType<typeof setTimeout>>();
let disposed = false;

function guardFor(tabId: string): PreviewVersionGuard {
  let guard = guards.get(tabId);
  if (!guard) {
    guard = createPreviewVersionGuard();
    guards.set(tabId, guard);
  }
  return guard;
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

function statusOf(error: unknown): number | null {
  return error instanceof AttachmentRequestError ? error.status : null;
}

function isAbort(error: unknown): boolean {
  if (error instanceof PreviewAbortedError) return true;
  const name = error instanceof Error ? error.name : "";
  return name === "AbortError" || name === "PdfAbortedError";
}

function emptyRuntime(): PreviewRuntime {
  return {
    phase: "loading",
    envelope: null,
    problem: null,
    version: "",
    notice: "",
    updateError: "",
    updateFailures: 0,
    loadedAt: 0,
  };
}

/**
 * 后端 envelope.kind → 界面预览类别。
 * 旧版 Office / 不支持保持「不支持」；code 再按扩展名区分文本与代码（markdown 单独一类）。
 */
export function previewKindFromEnvelope(
  envelopeKind: string,
  declared: PreviewKind,
  title: string,
): PreviewKind {
  const kind = String(envelopeKind || "").trim().toLowerCase();
  if (kind === "legacy_office") return "unsupported";
  if (kind === "code") {
    if (declared === "markdown" || declared === "text" || declared === "code") return declared;
    return previewKindForName(title) === "markdown" ? "markdown" : "code";
  }
  if (kind === "image" || kind === "pdf" || kind === "docx" || kind === "xlsx" || kind === "pptx") {
    return kind;
  }
  if (kind === "unsupported" || !kind) return declared === "audio" || declared === "video" ? declared : "unsupported";
  return declared;
}

export const useConversationPreviewStore = defineStore("conversationPreview", {
  state: () => ({
    /** 按标签 id 的运行时内容。 */
    runtime: {} as Record<string, PreviewRuntime>,
    /** 回复代码 / 任务输出正文（内存，不持久化）。 */
    documents: {} as Record<string, PreviewDocumentInput>,
    /** 供 UI 展示最近一次系统操作的结果（打开/下载）——不持久化。 */
    lastActionNote: "",
    _disposed: false,
  }),
  getters: {
    tabs(): PreviewTabState[] {
      return useConversationShellStore().prefs.previewTabs;
    },
    activeTab(): PreviewTabState | null {
      const shell = useConversationShellStore();
      return activePreviewTab(shell.prefs.previewTabs, shell.prefs.activePreviewTabId);
    },
    /** 右栏是否展开（用户打开 + 有标签；全窗口也算展开）。 */
    isOpen(): boolean {
      return useConversationShellStore().rightVisible;
    },
    /** 集成别名：B/A 的入口按钮只读这个值决定「是否已打开」。 */
    visible(): boolean {
      return useConversationShellStore().rightVisible;
    },
    isFull(): boolean {
      return useConversationShellStore().effectivePreviewFull;
    },
    runtimeOf(): (tabId: string) => PreviewRuntime | null {
      return (tabId: string) => this.runtime[tabId] ?? null;
    },
    documentOf(): (tabId: string) => PreviewDocumentInput | null {
      return (tabId: string) => this.documents[tabId] ?? null;
    },
    /** 供阅读器恢复滚动位置：当前标签的轻量位置。 */
    positionOf(): (tabId: string) => PreviewPosition {
      return (tabId: string) => {
        const tab = this.tabs.find((item) => item.id === tabId);
        return tab ? { ...tab.position } : {};
      };
    },
  },
  actions: {
    // -- 打开 / 关闭 -------------------------------------------------------

    /** 打开一个附件预览（B：消息附件；A：任务成果文件）。只有用户动作才会调用。 */
    openAttachment(input: AttachmentPreviewInput): PreviewOpenOutcome {
      return this.openTarget(attachmentPreviewTarget(input));
    },

    /** 打开回复里的一个代码块（B）：不同消息/不同代码块各自一个稳定身份。 */
    openReplyCode(input: ReplyCodePreviewInput & PreviewDocumentInput): PreviewOpenOutcome {
      const target = replyCodePreviewTarget(input);
      return this.openTarget(target, {
        text: input.text,
        language: input.language,
        unavailableReason: input.unavailableReason,
      });
    },

    /** 打开任务成果文件或输出正文（A）：有附件走附件预览，只有文本就按只读代码/文本。 */
    openTaskOutput(input: TaskOutputPreviewInput & PreviewDocumentInput): PreviewOpenOutcome {
      const target = taskOutputPreviewTarget(input);
      return this.openTarget(target, {
        text: input.text,
        language: input.language,
        unavailableReason: input.unavailableReason,
      });
    },

    /** 统一的打开入口：同身份复用标签，新身份追加；写 shell 的标签与激活项。 */
    openTarget(target: PreviewTarget, document?: PreviewDocumentInput): PreviewOpenOutcome {
      if (!target.id) return "skipped";
      const shell = useConversationShellStore();
      const result = openPreviewTab(shell.prefs.previewTabs, target);
      if (result.replacedIndex === null) {
        // 新标签：交给 shell 的既有入口（它会合并、限制数量并持久化）。
        shell.setPreviewTabs([result.tab], result.activeId);
      } else if (result.previousId && result.previousId !== result.tab.id) {
        // 同一附件的旧版本标签：复用并更新身份（位置保留、顺序不变）。
        this.applyTabIdentity(result.previousId, result.tab.target);
        shell.setActivePreviewTab(result.tab.id);
      } else {
        const live = this.tabs.find((tab) => tab.id === result.tab.id);
        if (live) live.target = { ...live.target, ...result.tab.target };
        shell.setActivePreviewTab(result.tab.id);
      }
      if (document !== undefined) this.rememberDocument(result.activeId, document);
      // 打开 = 用户意图：展开预览栏（不改变全窗口偏好）。
      shell.setRightOpen(true);
      const liveTab = this.tabs.find((tab) => tab.id === result.activeId);
      if (liveTab) this.ensureLoaded(liveTab.id);
      return "opened";
    },

    /**
     * 集成入口（Lead 约定命名）：打开一个预览目标。
     * B/A 的按钮可以调 openPreview(attachmentPreviewTarget({...})) 或下面三个语义化方法。
     */
    openPreview(target: PreviewTarget): PreviewOpenOutcome {
      return this.openTarget(target);
    },

    /** 集成别名：保存阅读位置（组件内部用 savePosition，同一实现）。 */
    setPosition(tabId: string, position: Partial<PreviewPosition>): void {
      this.savePosition(tabId, position);
    },

    /** 集成别名：进入全窗口预览。 */
    openFull(): void {
      this.setFull(true);
    },

    /** 集成别名：从全窗口返回对话布局。 */
    closeFull(): void {
      this.setFull(false);
    },

    /** 切到一个已有标签（不改变内容与位置）。 */
    activateTab(tabId: string): void {
      const shell = useConversationShellStore();
      if (!shell.prefs.previewTabs.some((tab) => tab.id === tabId)) return;
      shell.setActivePreviewTab(tabId);
      shell.setRightOpen(true);
      this.ensureLoaded(tabId);
    },

    /** 关闭一个标签：只关这一份预览；最后一个关掉时收起预览栏（不删文件）。 */
    closeTab(tabId: string): void {
      const shell = useConversationShellStore();
      const result = closePreviewTab(shell.prefs.previewTabs, shell.prefs.activePreviewTabId, tabId);
      if (!result.closed) return;
      shell.prefs.previewTabs = result.tabs;
      shell.prefs.activePreviewTabId = result.activeId;
      shell.scheduleSave();
      this.releaseTab(tabId);
      if (!result.tabs.length) {
        shell.setRightOpen(false);
        return;
      }
      if (result.activeId) this.ensureLoaded(result.activeId);
    },

    /** 收起整个预览栏：标签与位置都保留（再次打开继续读）。 */
    collapsePanel(): void {
      useConversationShellStore().setRightOpen(false);
    },

    /** 重新打开预览栏（不新建标签）。 */
    reopenPanel(): void {
      const shell = useConversationShellStore();
      if (!shell.prefs.previewTabs.length) return;
      shell.setRightOpen(true);
      const active = this.activeTab;
      if (active) this.ensureLoaded(active.id);
    },

    /** 全窗口 / 返回对话；标签与位置不动。 */
    setFull(full: boolean): void {
      useConversationShellStore().setPreviewFull(full);
      const active = this.activeTab;
      if (active) this.ensureLoaded(active.id);
    },

    toggleFull(): void {
      this.setFull(!this.isFull);
    },

    // -- 阅读位置 ---------------------------------------------------------

    /** 保存某一个标签的轻量阅读位置（每个标签各存各的；不存正文）。 */
    savePosition(tabId: string, patch: Partial<PreviewPosition>): void {
      const shell = useConversationShellStore();
      const tab = shell.prefs.previewTabs.find((item) => item.id === tabId);
      if (!tab) return;
      // 只保留这一类文件有意义的位置键：换过文件类型时不会把上一类的页码/工作表带过去。
      tab.position = positionForKind(tab.target.kind, mergePreviewPosition(tab.position, patch));
      shell.scheduleSave();
    },

    // -- 内容加载与更新 ---------------------------------------------------

    /** 确保这个标签的运行时内容已开始加载（幂等；已有内容/失败态不重复拉取）。 */
    ensureLoaded(tabId: string): void {
      if (this._disposed || disposed) return;
      const tab = this.tabs.find((item) => item.id === tabId);
      if (!tab) return;
      if (this.runtime[tabId]) return;
      if (tab.target.attachmentId) {
        if (tab.target.kind === "audio" || tab.target.kind === "video") {
          this.runtime[tabId] = {
            ...emptyRuntime(),
            phase: "problem",
            problem: mediaPreviewProblem(tab.target.kind),
            version: tab.target.version ?? "",
          };
          return;
        }
        void this.loadAttachment(tabId, "initial");
        return;
      }
      this.prepareContentRuntime(tab);
    },

    /** 用户点「重试」：重新读取（失败原因可能只是暂时的）。 */
    retryTab(tabId: string): void {
      const tab = this.tabs.find((item) => item.id === tabId);
      if (!tab) return;
      if (tab.target.attachmentId) {
        void this.loadAttachment(tabId, "retry");
        return;
      }
      delete this.runtime[tabId];
      this.prepareContentRuntime(tab);
    },

    /**
     * 检查文件是否更新（打开期间用轻量轮询 / 一轮结束后的 debounce 调用）。
     * 只比较**真实文件版本**；版本没变不做任何事；版本变了原地更新内容与提示。
     */
    async checkForUpdate(tabId: string): Promise<"unchanged" | "updated" | "failed" | "skipped"> {
      if (this._disposed || disposed) return "skipped";
      const tab = this.tabs.find((item) => item.id === tabId);
      const runtime = this.runtime[tabId];
      if (!tab || !tab.target.attachmentId) return "skipped";
      if (!runtime || runtime.phase !== "ready") return "skipped";
      if (runtime.updateFailures >= FILE_UPDATE_MAX_FAILURES) return "skipped";
      const result = await this.loadAttachment(tabId, "check");
      if (result === "updated") return "updated";
      if (result === "failed") return "failed";
      if (result === "unchanged") return "unchanged";
      return "skipped";
    },

    /** 读取一次附件预览；mode 决定失败时是展示错误还是保留旧内容并提示。 */
    async loadAttachment(
      tabId: string,
      mode: "initial" | "retry" | "check",
    ): Promise<"ready" | "problem" | "unchanged" | "updated" | "failed" | "stale" | "aborted" | "skipped"> {
      if (this._disposed || disposed) return "skipped";
      const tab = this.tabs.find((item) => item.id === tabId);
      if (!tab || !tab.target.attachmentId) return "skipped";
      const guard = guardFor(tabId);
      const sequence = guard.begin();
      controllers.get(tabId)?.abort();
      const controller = new AbortController();
      controllers.set(tabId, controller);
      const previous = this.runtime[tabId];
      if (mode !== "check" || !previous) {
        this.runtime[tabId] = {
          ...(previous ?? emptyRuntime()),
          phase: "loading",
          problem: null,
          notice: previous?.notice ?? "",
          updateError: previous?.updateError ?? "",
          loadedAt: previous?.loadedAt ?? 0,
        };
      }
      try {
        const envelope = await fetchAttachmentPreview(tab.target.attachmentId, {
          signal: controller.signal,
        });
        if (!guard.isCurrent(sequence)) return "stale";
        if (this._disposed || disposed) return "stale";
        return this.applyEnvelope(tabId, envelope, mode);
      } catch (error) {
        if (!guard.isCurrent(sequence)) return "stale";
        if (isAbort(error)) {
          // 主动中止（收起/换标签/卸载）不是失败：只有我们自己这一代才需要复原状态。
          return "aborted";
        }
        return this.applyLoadError(tabId, error, mode);
      } finally {
        if (guard.isCurrent(sequence)) controllers.delete(tabId);
      }
    },

    /** 把后端 envelope 应用到运行时；版本变化时原地更新标签身份（位置保留）。 */
    applyEnvelope(
      tabId: string,
      envelope: PreviewEnvelope,
      mode: "initial" | "retry" | "check",
    ): "ready" | "problem" | "unchanged" | "updated" | "failed" | "stale" {
      const tab = this.tabs.find((item) => item.id === tabId);
      if (!tab) return "stale";
      const previous = this.runtime[tabId];
      const knownVersion = previous?.version || tab.target.version || "";
      if (mode === "check") {
        if (!isFileVersionChanged(knownVersion, envelope.version)) {
          if (previous && (previous.updateError || previous.updateFailures)) {
            this.runtime[tabId] = { ...previous, updateError: "", updateFailures: 0 };
          }
          return "unchanged";
        }
        if (envelope.status !== "ready" && previous?.phase === "ready") {
          // 文件变了但新版本暂时读不出来：保留上次读取的内容，并如实说明看到的是旧内容。
          this.runtime[tabId] = {
            ...previous,
            updateFailures: previous.updateFailures + 1,
            updateError:
              "文件已更新，但新版本暂时读不出来：当前显示的是上次读取的内容（" +
              (envelope.reason || "原因未说明") +
              "）",
          };
          return "failed";
        }
      }
      const resolvedKind = previewKindFromEnvelope(
        String(envelope.kind || ""),
        tab.target.kind,
        tab.target.title,
      );
      const version = String(envelope.version || tab.target.version || "");
      let finalId = tab.id;
      if (version && version !== (tab.target.version ?? "")) {
        finalId = attachmentPreviewId(tab.target.attachmentId ?? "", version);
        if (finalId === tab.id || !tab.target.attachmentId) {
          finalId = tab.id;
          tab.target = { ...tab.target, version, kind: resolvedKind };
        } else {
          this.applyTabIdentity(tab.id, {
            ...tab.target,
            id: finalId,
            version,
            kind: resolvedKind,
          });
        }
      } else if (resolvedKind !== tab.target.kind) {
        tab.target = { ...tab.target, kind: resolvedKind };
      }
      const hasOpenTarget = Boolean(tab.target.attachmentId || tab.target.filePath);
      if (envelope.status === "ready") {
        this.runtime[finalId] = {
          phase: "ready",
          envelope,
          problem: null,
          version,
          notice: previous?.notice ?? "",
          updateError: "",
          updateFailures: 0,
          loadedAt: Date.now(),
        };
        if (mode === "check" && previous?.phase === "ready" && isFileVersionChanged(knownVersion, version)) {
          this.noteUpdate(finalId, "文件已更新，阅读位置已保留");
          return "updated";
        }
        return "ready";
      }
      const problem = classifyPreviewProblem({
        status: envelope.status,
        message: envelope.reason || "后端没有给出原因",
        hasOpenTarget,
        hasCopyText: Boolean(this.documents[finalId]?.text),
      });
      this.runtime[finalId] = {
        phase: "problem",
        envelope,
        problem,
        version,
        notice: previous?.notice ?? "",
        updateError: "",
        updateFailures: 0,
        loadedAt: Date.now(),
      };
      return "problem";
    },

    /** 加载失败：初次展示诚实状态；更新检查失败则保留上次内容并说明。 */
    applyLoadError(
      tabId: string,
      error: unknown,
      mode: "initial" | "retry" | "check",
    ): "problem" | "failed" | "stale" {
      const tab = this.tabs.find((item) => item.id === tabId);
      if (!tab) return "stale";
      const previous = this.runtime[tabId];
      const message = messageOf(error);
      if (mode === "check" && previous?.phase === "ready") {
        this.runtime[tabId] = {
          ...previous,
          updateFailures: previous.updateFailures + 1,
          updateError: "更新检查失败：当前显示的是上次读取的内容（" + message + "）",
        };
        return "failed";
      }
      const problem = classifyPreviewProblem({
        httpStatus: statusOf(error),
        message,
        hasOpenTarget: Boolean(tab.target.attachmentId || tab.target.filePath),
        hasCopyText: Boolean(this.documents[tabId]?.text),
      });
      this.runtime[tabId] = {
        phase: "problem",
        envelope: previous?.envelope ?? null,
        problem,
        version: previous?.version || tab.target.version || "",
        notice: previous?.notice ?? "",
        updateError: "",
        updateFailures: previous?.updateFailures ?? 0,
        loadedAt: Date.now(),
      };
      return "problem";
    },

    // -- 系统打开 / 下载 --------------------------------------------------

    /** 打开目标：桌面走系统默认程序（可执行类只定位文件夹），浏览器走既有 blob 链路。 */
    async openInSystem(tabId: string): Promise<PreviewActionResult> {
      const tab = this.tabs.find((item) => item.id === tabId);
      if (!tab) return { ok: false, note: "这个预览已经关闭" };
      const ref = attachmentRefForTarget(tab.target);
      if (!ref) {
        return { ok: false, note: "这个预览没有可打开的文件（正文来自回复或任务输出，可以直接复制）" };
      }
      try {
        const result = await openAttachment(ref, { desktop: isDesktopShell() });
        return { ok: true, note: result.note };
      } catch (error) {
        return { ok: false, note: "打不开：" + messageOf(error) };
      }
    },

    /** 下载副本（只在有真实附件时可用）。 */
    async downloadCopy(tabId: string): Promise<PreviewActionResult> {
      const tab = this.tabs.find((item) => item.id === tabId);
      if (!tab) return { ok: false, note: "这个预览已经关闭" };
      const ref = attachmentRefForTarget(tab.target);
      if (!ref) return { ok: false, note: "这个预览没有可下载的文件" };
      try {
        const result = await downloadAttachment(ref);
        return { ok: true, note: result.note };
      } catch (error) {
        return { ok: false, note: "下载失败：" + messageOf(error) };
      }
    },

    // -- 内部 -------------------------------------------------------------

    /** 回复代码 / 任务输出：正文在内存里；缺失或已被清理时给真实说明。 */
    prepareContentRuntime(tab: PreviewTabState): void {
      const document = this.documents[tab.id];
      if (document?.unavailableReason) {
        this.runtime[tab.id] = {
          ...emptyRuntime(),
          phase: "problem",
          problem: classifyPreviewProblem({
            unavailableReason: document.unavailableReason,
            message: document.unavailableReason,
            hasCopyText: false,
            hasOpenTarget: Boolean(tab.target.filePath),
          }),
        };
        return;
      }
      if (typeof document?.text === "string" && document.text.length >= 0) {
        this.runtime[tab.id] = {
          phase: "ready",
          envelope: null,
          problem: null,
          version: "",
          notice: "",
          updateError: "",
          updateFailures: 0,
          loadedAt: Date.now(),
        };
        return;
      }
      if (tab.target.kind === "audio" || tab.target.kind === "video") {
        this.runtime[tab.id] = {
          ...emptyRuntime(),
          phase: "problem",
          problem: mediaPreviewProblem(tab.target.kind),
        };
        return;
      }
      if (tab.target.filePath) {
        this.runtime[tab.id] = {
          ...emptyRuntime(),
          phase: "problem",
          problem: {
            kind: "archived",
            title: "这个成果只有本地路径，没有可读取的正文",
            detail: "用「打开」在桌面端定位/打开原文件；右侧预览不会去读任意路径。",
            retryable: false,
            openable: true,
            downloadable: false,
            copyable: false,
          },
        };
        return;
      }
      this.runtime[tab.id] = {
        ...emptyRuntime(),
        phase: "problem",
        problem: {
          kind: "archived",
          title: "这条内容不在当前页面里",
          detail:
            tab.target.source === "reply_code"
              ? "回复里的代码不会存进布局偏好：请从原消息重新点「预览」。"
              : "任务输出正文没有保存在预览状态里：请从任务详情重新点「预览」。",
          retryable: false,
          openable: false,
          downloadable: false,
          copyable: false,
        },
      };
    },

    /** 记录回复代码 / 任务输出正文；已存在的旧内容按新内容替换。 */
    rememberDocument(tabId: string, document: PreviewDocumentInput): void {
      const next: PreviewDocumentInput = { ...(this.documents[tabId] ?? {}) };
      if (document.text !== undefined) next.text = document.text;
      if (document.language !== undefined) next.language = document.language;
      if (document.unavailableReason !== undefined) next.unavailableReason = document.unavailableReason;
      this.documents[tabId] = next;
      const runtime = this.runtime[tabId];
      if (runtime && runtime.phase === "problem") delete this.runtime[tabId];
    },

    /** 原地更新标签身份（附件版本变化）：顺序与阅读位置不变，运行时/正文一起迁移。 */
    applyTabIdentity(oldId: string, target: PreviewTarget): void {
      const shell = useConversationShellStore();
      const index = shell.prefs.previewTabs.findIndex((tab) => tab.id === oldId);
      if (index < 0) return;
      const newId = target.id;
      const conflict = newId !== oldId && shell.prefs.previewTabs.some((tab, at) => at !== index && tab.id === newId);
      if (conflict) {
        // 极少见：另一个标签已经是这个身份。合并掉旧标签，保留新身份的标签。
        shell.prefs.previewTabs.splice(index, 1);
      } else {
        const tab = shell.prefs.previewTabs[index];
        tab.id = newId;
        tab.target = target;
      }
      if (shell.prefs.activePreviewTabId === oldId) shell.prefs.activePreviewTabId = newId;
      const previousRuntime = this.runtime[oldId];
      if (previousRuntime) {
        /**
         * 版本已经不同：旧运行时内容不能挂在新版本标签下（那会把「旧内容」说成新版本）。
         * 丢掉旧运行时，交给 ensureLoaded 按新版本重新读取；位置仍在标签里。
         */
        if (target.version && previousRuntime.version && previousRuntime.version !== target.version) {
          delete this.runtime[oldId];
        } else {
          this.runtime[newId] = previousRuntime;
          delete this.runtime[oldId];
        }
      }
      if (this.documents[oldId]) {
        this.documents[newId] = this.documents[oldId];
        delete this.documents[oldId];
      }
      const notice = noticeTimers.get(oldId);
      if (notice) {
        noticeTimers.delete(oldId);
        noticeTimers.set(newId, notice);
      }
      const guard = guards.get(oldId);
      if (guard) {
        guards.delete(oldId);
        guards.set(newId, guard);
      }
      shell.scheduleSave();
    },

    /** 短暂提示（文件已更新）：几秒后自动消失。 */
    noteUpdate(tabId: string, text: string): void {
      const runtime = this.runtime[tabId];
      if (!runtime) return;
      this.runtime[tabId] = { ...runtime, notice: text, updateError: "", updateFailures: 0 };
      const existing = noticeTimers.get(tabId);
      if (existing) clearTimeout(existing);
      noticeTimers.set(
        tabId,
        setTimeout(() => {
          noticeTimers.delete(tabId);
          const current = this.runtime[tabId];
          if (current?.notice === text) this.runtime[tabId] = { ...current, notice: "" };
        }, 4_000),
      );
    },

    /** 释放一个标签的运行时资源（关闭标签 / 卸载）。 */
    releaseTab(tabId: string): void {
      controllers.get(tabId)?.abort();
      controllers.delete(tabId);
      delete this.runtime[tabId];
      delete this.documents[tabId];
      guards.delete(tabId);
      const timer = noticeTimers.get(tabId);
      if (timer) clearTimeout(timer);
      noticeTimers.delete(tabId);
    },

    /** 页面离开 / 组件卸载：中止全部在途请求与计时器（位置已经在 shell 里）。 */
    dispose(): void {
      this._disposed = true;
      disposed = true;
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      for (const timer of noticeTimers.values()) clearTimeout(timer);
      noticeTimers.clear();
      guards.clear();
    },

    /** 仅供测试：把模块级状态全部清掉（生产不调用）。 */
    resetForTests(): void {
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      for (const timer of noticeTimers.values()) clearTimeout(timer);
      noticeTimers.clear();
      guards.clear();
      disposed = false;
      this._disposed = false;
      this.runtime = {};
      this.documents = {};
      this.lastActionNote = "";
    },
  },
});
