/**
 * 互动模式的 Pinia store（Lead 维护，A/B/C 只消费）。
 *
 * 契约：docs/interactive-mode-contract.md §4.3。
 *
 * 三条不许绕过的规则在这里落地：
 * 1. `commit` / `saveNow` 只保存，**不调用 QIO**；
 * 2. 只有 `submit()` 会让 QIO 拿到未提交的有效表达；
 * 3. 提交失败保留改动与本次注释选择，不提前更新「上次成功提交」基准。
 */
import { defineStore } from "pinia";
import { computed, ref } from "vue";
import * as api from "../services/interactive";
import {
  cloneState,
  emptyBoardState,
  type BoardState,
  type DecideResult,
  type Intent,
  type IntentPreview,
  type IntentStatus,
  type SubmissionRecord,
  type SubmissionResult,
  type VisibleRange,
} from "../interactive/types";

const DEFAULT_BOARD_ID = "board_default";
const UNDO_LIMIT = 100;

export type SaveStatus = "idle" | "saving" | "saved" | "error";
export type SubmitStatus = "idle" | "submitting" | "succeeded" | "failed" | "empty" | "duplicate";

export const useInteractiveStore = defineStore("interactive", () => {
  const boardId = ref(DEFAULT_BOARD_ID);
  const board = ref<BoardState | null>(null);
  const loading = ref(false);
  const loadError = ref<string | null>(null);
  const drafts = ref<Record<string, string>>({});

  const saveStatus = ref<SaveStatus>("idle");
  const lastSavedAt = ref<string | null>(null);
  const saveError = ref<string | null>(null);
  const dirty = ref(false);
  const lastOpLabel = ref("");

  const submissions = ref<SubmissionRecord[]>([]);
  const lastSubmission = ref<SubmissionResult | null>(null);

  const submitStatus = ref<SubmitStatus>("idle");
  const submitError = ref<string | null>(null);

  const visibleRange = ref<VisibleRange | null>(null);

  const intents = ref<Intent[]>([]);
  const conflicts = ref<string[][]>([]);
  const batchAvailable = ref(false);
  const recoverNotice = ref<string[]>([]);

  const auxOpen = ref(true);
  const demoMode = ref(false);

  const undoStack = ref<string[]>([]);
  const redoStack = ref<string[]>([]);
  const canUndo = computed(() => undoStack.value.length > 0);
  const canRedo = computed(() => redoStack.value.length > 0);

  const pendingIntents = computed(() =>
    intents.value.filter((item) =>
      ["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status),
    ),
  );
  const activeIntents = computed(() =>
    intents.value.filter((item) => ["running", "paused"].includes(item.status)),
  );
  const settledIntents = computed(() =>
    intents.value.filter((item) => ["done", "rejected", "failed", "cancelled"].includes(item.status)),
  );

  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let draftTimer: ReturnType<typeof setTimeout> | null = null;
  let saveInFlight: Promise<void> | null = null;

  function pushUndo(previous: BoardState) {
    undoStack.value.push(JSON.stringify(previous));
    if (undoStack.value.length > UNDO_LIMIT) undoStack.value.shift();
    redoStack.value = [];
  }

  /**
   * 提交一次板面操作：算好的新状态交到这里，由 store 负责撤销栈与自动保存。
   * **这里不调用 QIO**：保存与「交给 QIO」是两件事。
   */
  function commit(next: BoardState, label: string) {
    const current = board.value;
    if (current) pushUndo(current);
    board.value = { ...next, boardId: boardId.value };
    lastOpLabel.value = label;
    dirty.value = true;
    saveStatus.value = "saving";
    scheduleSave();
  }

  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      void saveNow();
    }, 450);
  }

  async function saveNow(): Promise<void> {
    if (!board.value) return;
    if (saveInFlight) {
      await saveInFlight;
      return;
    }
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    const snapshot = cloneState(board.value);
    saveInFlight = (async () => {
      try {
        const result = await api.saveBoardState(boardId.value, snapshot, lastOpLabel.value || "op");
        board.value = result.state;
        lastSavedAt.value = result.savedAt;
        saveStatus.value = "saved";
        saveError.value = null;
        dirty.value = false;
        // 保存只落到本机板面；顺手刷新「本次允许查看的范围」让提交前预览是新的。
        // 这一步同样不调用 QIO。
        void refreshVisibleRange();
      } catch (err) {
        saveStatus.value = "error";
        saveError.value = (err as Error).message;
        dirty.value = true;
      } finally {
        saveInFlight = null;
      }
    })();
    await saveInFlight;
  }

  function undo() {
    if (!canUndo.value || !board.value) return;
    const previous = undoStack.value.pop() as string;
    redoStack.value.push(JSON.stringify(board.value));
    board.value = JSON.parse(previous) as BoardState;
    dirty.value = true;
    lastOpLabel.value = "撤销";
    scheduleSave();
  }

  function redo() {
    if (!canRedo.value || !board.value) return;
    const next = redoStack.value.pop() as string;
    undoStack.value.push(JSON.stringify(board.value));
    board.value = JSON.parse(next) as BoardState;
    dirty.value = true;
    lastOpLabel.value = "重做";
    scheduleSave();
  }

  async function refreshBoardFromServer() {
    const payload = await api.fetchBoardState(boardId.value);
    board.value = payload.state;
    boardId.value = payload.board.id;
    submissions.value = payload.submissions ?? [];
    drafts.value = payload.drafts?.drafts ?? {};
    undoStack.value = [];
    redoStack.value = [];
    dirty.value = false;
    saveStatus.value = "idle";
  }

  async function refreshVisibleRange() {
    try {
      const payload = await api.fetchVisibleRange(boardId.value);
      visibleRange.value = payload.visibleRange;
    } catch (err) {
      visibleRange.value = null;
      saveError.value = (err as Error).message;
    }
  }

  async function loadIntents() {
    try {
      const payload = await api.fetchIntents(boardId.value);
      intents.value = payload.intents ?? [];
      conflicts.value = payload.conflicts ?? [];
      batchAvailable.value = Boolean(payload.batchAvailable);
      recoverNotice.value = payload.recovery?.paused ?? [];
    } catch (err) {
      intents.value = [];
      recoverNotice.value.push((err as Error).message);
    }
  }

  async function load() {
    loading.value = true;
    loadError.value = null;
    try {
      await refreshBoardFromServer();
      await refreshVisibleRange();
      await loadIntents();
    } catch (err) {
      board.value = board.value ?? emptyBoardState(boardId.value);
      loadError.value = (err as Error).message;
    } finally {
      loading.value = false;
    }
  }

  /** 文字草稿：输入过程中保存，**不调用 QIO**，也不等于提交内容。 */
  function setDraft(key: string, text: string) {
    drafts.value = { ...drafts.value, [key]: text };
    if (draftTimer) clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      void api.saveDrafts(boardId.value, drafts.value).catch(() => undefined);
    }, 600);
  }

  function draftFor(key: string): string {
    return drafts.value[key] ?? "";
  }

  /**
   * 提交：QIO 取得未提交有效表达的唯一入口。
   * 先保存（保证服务端看到的是最新板面），再提交；失败时保留改动与勾选。
   */
  async function submit(): Promise<SubmissionResult | null> {
    if (!board.value || submitStatus.value === "submitting") return null;
    submitError.value = null;
    submitStatus.value = "submitting";
    await saveNow();
    if (saveStatus.value === "error") {
      // 保存没成功就不提交：宁可让用户再点一次，也不能拿旧板面当「本次提交」。
      submitStatus.value = "failed";
      submitError.value = `板面没有保存成功，本次未提交（${saveError.value ?? "原因未知"}）`;
      return null;
    }
    try {
      const result = await api.submitBoard(boardId.value);
      lastSubmission.value = result;
      submitStatus.value = result.status;
      // 只有成功提交才会让服务端清掉勾选并推进基准，所以成功后重新拉一遍状态。
      await refreshBoardFromServer();
      await refreshVisibleRange();
      await loadIntents();
      return result;
    } catch (err) {
      submitStatus.value = "failed";
      submitError.value = (err as Error).message;
      return null;
    }
  }

  async function settleAfterIntentChange() {
    if (dirty.value) await saveNow();
    await refreshBoardFromServer();
    await refreshVisibleRange();
    await loadIntents();
  }

  async function approve(intentId: string, confirmDependency = false): Promise<DecideResult> {
    const result = await api.approveIntent(intentId, confirmDependency);
    await settleAfterIntentChange();
    return result;
  }

  async function reject(intentId: string): Promise<DecideResult> {
    const result = await api.rejectIntent(intentId);
    await settleAfterIntentChange();
    return result;
  }

  async function decideBatch(approveIds: string[], rejectIds: string[]) {
    const result = await api.batchDecide(approveIds, rejectIds);
    await settleAfterIntentChange();
    return result;
  }

  async function updatePreview(intentId: string, preview: IntentPreview) {
    const result = await api.updateIntentPreview(intentId, preview);
    await loadIntents();
    return result;
  }

  async function advanceDemo(
    intentId: string,
    outcome: "done" | "failed" | "paused" | "cancelled",
  ) {
    const result = await api.advanceIntent(intentId, outcome);
    await settleAfterIntentChange();
    return result;
  }

  async function createDemoIntents() {
    const result = await api.createDemoIntents(boardId.value);
    await loadIntents();
    return result;
  }

  function intentById(intentId: string): Intent | undefined {
    return intents.value.find((item) => item.id === intentId);
  }

  function statusLabel(status: IntentStatus): string {
    const labels: Record<IntentStatus, string> = {
      pending: "等待审批",
      needs_update: "需要更新",
      rejected: "已拒绝",
      waiting_dependency: "等待前项",
      waiting_confirm: "等待再次确认",
      running: "执行中",
      paused: "已暂停",
      done: "已完成",
      failed: "失败",
      cancelled: "已取消",
    };
    return labels[status] ?? status;
  }

  return {
    boardId,
    board,
    loading,
    loadError,
    drafts,
    saveStatus,
    lastSavedAt,
    saveError,
    dirty,
    lastOpLabel,
    submissions,
    lastSubmission,
    submitStatus,
    submitError,
    visibleRange,
    intents,
    conflicts,
    batchAvailable,
    recoverNotice,
    auxOpen,
    demoMode,
    undoStack,
    redoStack,
    canUndo,
    canRedo,
    pendingIntents,
    activeIntents,
    settledIntents,
    commit,
    undo,
    redo,
    saveNow,
    refreshVisibleRange,
    loadIntents,
    load,
    refreshBoardFromServer,
    setDraft,
    draftFor,
    submit,
    approve,
    reject,
    decideBatch,
    updatePreview,
    advanceDemo,
    createDemoIntents,
    intentById,
    statusLabel,
  };
});
