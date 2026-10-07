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
import { batchesWithList, groupIntentsByBatch, recordIntentBatch } from "../interactive/approval";
import { isStaleReceipt } from "../interactive/drafts";
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
/** 卡片草稿的保存状态（契约 §9.5）：失败必须能被看见并可重试，不许静默 */
export type DraftSaveState = "idle" | "saving" | "saved" | "error";

/** 草稿防抖：停下输入多久后写回服务端 */
const DRAFT_SAVE_DEBOUNCE_MS = 600;
/** 一次 flush 最多连存几轮（连续输入时避免把它变成停不下来的循环） */
const DRAFT_FLUSH_MAX_ROUNDS = 8;
export type SubmitStatus = "idle" | "submitting" | "succeeded" | "failed" | "empty" | "duplicate";

export const useInteractiveStore = defineStore("interactive", () => {
  const boardId = ref(DEFAULT_BOARD_ID);
  const board = ref<BoardState | null>(null);
  const loading = ref(false);
  const loadError = ref<string | null>(null);
  const drafts = ref<Record<string, string>>({});
  /**
   * 每个草稿键的保存状态（契约 §9.5）。按 key 分别记录：
   * 提示要贴近**当前编辑的那张卡**，不能用一个全局状态糊过去。
   */
  const draftStates = ref<Record<string, { status: DraftSaveState; error: string | null }>>({});
  /** 最近编辑过的草稿键（提示组件没拿到 cardId 时的兜底） */
  const lastDraftKey = ref("");

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

  /**
   * 浮层开合状态（本轮前端改版）。
   *
   * `auxOpen` 是上一版常驻右侧栏的开关，改版后不再有右侧栏，保留字段只为兼容旧引用，
   * 新代码不要再用它。
   */
  const auxOpen = ref(true);
  const demoMode = ref(false);
  /** 右下悬浮聊天是否展开（收起不清消息、不删草稿、不打断对话） */
  const chatOpen = ref(false);
  /** 右上批量列表是否展开（默认收起，不因数量变化抢占用户的决定） */
  const batchOpen = ref(false);
  /** 顶部「任务」浮层是否展开 */
  const tasksOpen = ref(false);
  /**
   * 板面当前的指针模式：工具栏是控制面、画布是执行面，两边共用同一份状态。
   * 这是**查看状态**：切换模式不形成表达、不调用 QIO、不触发保存。
   */
  const boardMode = ref<"select" | "rect" | "link">("select");

  /** 保存前的影响确认：这次改动会影响这些执行中的任务，等用户决定 */
  const pendingImpact = ref<{
    affected: { intentId: string; title: string; materials: string[]; consequence: string }[];
  } | null>(null);
  /** 保存后服务端回报「因为这些改动被暂停的任务」 */
  const materialPaused = ref<Intent[]>([]);
  let impactConfirmed = false;

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
  /** 需要处理的任务（执行中 / 已暂停），顶部「任务」入口显示它们的数量 */
  const runningIntents = computed(() => activeIntents.value);
  /** 已结束的任务（已完成 / 已拒绝 / 失败 / 已取消） */
  const finishedIntents = computed(() => settledIntents.value);
  /** 按批分组后的全部批次（契约 §8.5：不同批次不累加） */
  const batches = computed(() => groupIntentsByBatch(intents.value));
  /** 只有「同一批等待审批 ≥4」才需要批量列表 */
  const listBatches = computed(() => batchesWithList(intents.value));
  /** 顶部「任务」入口的数量：等待审批之外的、用户还需要关注的任务 */
  const taskCount = computed(() => activeIntents.value.length + settledIntents.value.length);

  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let draftTimer: ReturnType<typeof setTimeout> | null = null;
  let saveInFlight: Promise<void> | null = null;

  /** 草稿的写入代次（单调递增）：每个键记最后一次修改的序号，旧请求的返回据此丢弃 */
  let draftSeq = 0;
  const draftKeySeq = new Map<string, number>();
  const draftSavedKeySeq = new Map<string, number>();
  /** 同一时刻只允许一个草稿保存请求在飞（并发 PUT 会让旧内容盖掉新内容） */
  let draftInFlight: Promise<void> | null = null;

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
    // 保存前先问服务端一句：这次改动会不会碰到正在执行任务依赖的材料？
    // 会 → 不保存，把影响说明交给用户决定（继续=保存并暂停相关任务；取消=不改动，任务继续）。
    if (!impactConfirmed && activeIntents.value.length > 0) {
      try {
        const check = await api.previewMaterialImpact(boardId.value, snapshot);
        // 只有**执行中**的任务才需要「先说明影响再让用户决定」。
        // 已经暂停的任务不该拦住保存：它的依据已经失效是历史事实，用户每次编辑都被拦
        // 会让板面根本存不下去（复核实测过这个后果）。暂停的影响只作为提示显示。
        const running = new Set(
          intents.value.filter((item) => item.status === "running").map((item) => item.id),
        );
        const blocking = (check.affected ?? []).filter((item) => running.has(item.intentId));
        if (blocking.length) {
          pendingImpact.value = { affected: blocking };
          saveStatus.value = "idle";
          return;
        }
      } catch {
        // 预判失败不阻塞保存：它只是「多说一句话」，不是保存的前置条件
      }
    }
    saveInFlight = (async () => {
      try {
        const result = await api.saveBoardState(boardId.value, snapshot, lastOpLabel.value || "op");
        board.value = result.state;
        lastSavedAt.value = result.savedAt;
        saveStatus.value = "saved";
        saveError.value = null;
        dirty.value = false;
        impactConfirmed = false;
        const impact = (result as { materialImpact?: { paused?: Intent[] } }).materialImpact;
        if (impact?.paused?.length) {
          materialPaused.value = impact.paused;
          void loadIntents();
        }
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
    // 服务端是已保存内容的事实来源，但**本地还没保存成功的编辑内容**优先：
    // 失败/在飞的草稿不能被服务端旧值覆盖（§9.5「失败时保留编辑内容」）。
    const serverDrafts = payload.drafts?.drafts ?? {};
    const mergedDrafts: Record<string, string> = { ...serverDrafts };
    for (const key of unsavedDraftKeys()) mergedDrafts[key] = drafts.value[key] ?? "";
    drafts.value = mergedDrafts;
    // 已经保存成功、且服务端也有的键：状态回到 idle；未保存/失败的键保留自己的状态
    const keptStates: Record<string, { status: DraftSaveState; error: string | null }> = {};
    for (const key of Object.keys(mergedDrafts)) {
      if ((draftKeySeq.get(key) ?? 0) > (draftSavedKeySeq.get(key) ?? 0)) {
        keptStates[key] = draftStateFor(key);
      }
    }
    draftStates.value = keptStates;
    undoStack.value = [];
    redoStack.value = [];
    dirty.value = false;
    // 重新打开时板面上已经有保存过的东西：状态要说「已保存」，不能说「尚未保存过」。
    if (payload.seq > 0) {
      saveStatus.value = "saved";
      lastSavedAt.value = payload.state?.updatedAt ?? null;
    } else {
      saveStatus.value = "idle";
    }
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

  /**
   * 记录「这一次创建动作产生的意图」，供批次判定使用（契约 §8.5 的来源①）。
   *
   * 放在 store 而不是组件里：意图只有两个来源 —— 演示入口与提交后的 QIO 回复，
   * 两条路径都经过这里，记录一次就够，不需要任何组件知道这件事。
   */
  function recordNewIntents(before: Set<string>, batchKey: string) {
    const fresh = intents.value.filter((item) => !before.has(item.id)).map((item) => item.id);
    if (fresh.length) recordIntentBatch(batchKey, fresh);
  }

  const intentIdSet = () => new Set(intents.value.map((item) => item.id));

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

  /** 某个草稿键当前的保存状态（没有记录就是 idle）。 */
  function draftStateFor(key: string): { status: DraftSaveState; error: string | null } {
    return draftStates.value[key] ?? { status: "idle", error: null };
  }

  function setDraftState(key: string, status: DraftSaveState, error: string | null = null): void {
    draftStates.value = { ...draftStates.value, [key]: { status, error } };
  }

  /**
   * 有内容还没保存成功的草稿键。
   * 失败也算「没保存成功」：内容留在内存里，用户点重试时还要再存一次。
   */
  function unsavedDraftKeys(): string[] {
    return Object.keys(drafts.value).filter(
      (key) => (draftKeySeq.get(key) ?? 0) > (draftSavedKeySeq.get(key) ?? 0),
    );
  }

  function hasUnsavedDrafts(): boolean {
    return unsavedDraftKeys().length > 0;
  }

  /** 文字草稿：输入过程中保存，**不调用 QIO**，也不等于提交内容。 */
  function setDraft(key: string, text: string) {
    drafts.value = { ...drafts.value, [key]: text };
    lastDraftKey.value = key;
    draftKeySeq.set(key, ++draftSeq);
    // 一有输入就进「保存中」：失败时才会被改成 error（绝不停在「已保存」）
    setDraftState(key, "saving");
    scheduleDraftSave();
  }

  function draftFor(key: string): string {
    return drafts.value[key] ?? "";
  }

  function scheduleDraftSave(): void {
    if (draftTimer) clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      draftTimer = null;
      void flushDrafts();
    }, DRAFT_SAVE_DEBOUNCE_MS);
  }

  /**
   * 把未保存的草稿写回服务端（**只保存草稿**：不建卡、不提交板面、不调用 QIO）。
   *
   * 同一时刻只允许一个请求在飞：两个并发 PUT 会按返回顺序落库，先发出、后返回的
   * 旧内容会把新内容盖掉。一个请求结束后如果又有了新输入，就再存一轮（有上限）。
   * 失败**不自动重试**：内存内容与错误原因都留着，等用户点重试或下一次输入。
   */
  async function flushDrafts(): Promise<void> {
    if (draftTimer) {
      clearTimeout(draftTimer);
      draftTimer = null;
    }
    if (draftInFlight) {
      await draftInFlight;
      return;
    }
    if (!hasUnsavedDrafts()) return;
    draftInFlight = (async () => {
      try {
        for (let round = 0; round < DRAFT_FLUSH_MAX_ROUNDS; round += 1) {
          if (!hasUnsavedDrafts()) return;
          const payload = { ...drafts.value };
          const atSeq = draftSeq;
          const keys = Object.keys(payload);
          for (const key of keys) {
            if ((draftKeySeq.get(key) ?? 0) <= atSeq) setDraftState(key, "saving");
          }
          try {
            await api.saveDrafts(boardId.value, payload);
          } catch (err) {
            const reason = (err as Error).message || "原因未知";
            for (const key of keys) {
              const keySeq = draftKeySeq.get(key) ?? 0;
              // 期间又改了内容：它属于下一轮，状态保持「保存中」，不要标成这次的失败
              if (isStaleReceipt(atSeq, keySeq)) continue;
              setDraftState(key, "error", reason);
            }
            return;
          }
          for (const key of keys) {
            const keySeq = draftKeySeq.get(key) ?? 0;
            // 请求在飞时用户又改了：这次返回不算数，留给下一轮
            if (isStaleReceipt(atSeq, keySeq)) continue;
            draftSavedKeySeq.set(key, keySeq);
            setDraftState(key, "saved");
          }
        }
      } finally {
        draftInFlight = null;
      }
      // 到轮次上限还有新内容：交给下一次防抖，别在这里空转
      if (hasUnsavedDrafts()) scheduleDraftSave();
    })();
    await draftInFlight;
  }

  /**
   * 用户点「重试保存」：只重写草稿，**不建卡、不提交板面、不调用 QIO**。
   * 不传 key 就重试所有还没保存成功的草稿。
   */
  async function retryDraftSave(key?: string): Promise<void> {
    const keys = key ? [key] : unsavedDraftKeys();
    for (const item of keys) {
      if (draftStateFor(item).status === "error") setDraftState(item, "saving");
    }
    await flushDrafts();
  }

  /** 草稿保存的整体状态（错误 > 保存中 > 已保存 > 空闲），给整体性提示用 */
  const draftSaveStatus = computed<DraftSaveState>(() => {
    const states = Object.values(draftStates.value).map((item) => item.status);
    if (states.includes("error")) return "error";
    if (states.includes("saving")) return "saving";
    if (states.includes("saved")) return "saved";
    return "idle";
  });
  /** 最近一次草稿保存失败的原因（没有失败时为 null） */
  const draftSaveError = computed<string | null>(() => {
    const failed = Object.values(draftStates.value).find((item) => item.status === "error");
    return failed?.error ?? null;
  });

  if (typeof window !== "undefined") {
    // 离开编辑器/页面时防抖可能还没到点：这里再推一次（尽力而为 —— 浏览器可能来不及完成
    // 这次请求；那部分内容仍留在内存里，下一次编辑会再存，状态不会假装「已保存」）。
    window.addEventListener("pagehide", () => void flushDrafts());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") void flushDrafts();
    });
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
    const intentsBefore = intentIdSet();
    try {
      const result = await api.submitBoard(boardId.value);
      lastSubmission.value = result;
      submitStatus.value = result.status;
      // 只有成功提交才会让服务端清掉勾选并推进基准，所以成功后重新拉一遍状态。
      await refreshBoardFromServer();
      await refreshVisibleRange();
      await loadIntents();
      // 这次提交之后新出现的意图属于同一批（QIO 对同一次提交给出的多个工作项）
      recordNewIntents(intentsBefore, "session:submission:" + (result.submission?.id ?? Date.now()));
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
    outcome: "done" | "failed" | "paused" | "cancelled" | "revert_rest",
  ) {
    const result = await api.advanceIntent(intentId, outcome);
    await settleAfterIntentChange();
    return result;
  }

  async function createDemoIntents() {
    const before = intentIdSet();
    const result = await api.createDemoIntents(boardId.value);
    await loadIntents();
    /*
     * 演示入口一次产生的（可能不止四项）：记成同一批。
     *
     * 注意不能只看「新出现的 id」：后端对**尚未结束的同名演示项会复用**（不重复创建），
     * 复用时新 id 是 0 个，只按新 id 记就会漏掉整批 —— 实测表现为「点了生成 4 项，批量入口却不出现」。
     * 这里以**接口返回的这一批**为准（这正是「一次产生过程」的可证明来源，契约 §9.2）：
     * 有返回值就用返回值，没有才退回「新出现的 id」。
     */
    const produced = (result?.created ?? [])
      .map((item) => item?.id)
      .filter((id): id is string => typeof id === "string" && id.length > 0);
    if (produced.length) recordIntentBatch("session:demo:" + Date.now(), produced);
    else recordNewIntents(before, "session:demo:" + Date.now());
    return result;
  }

  /** 用户确认：改动生效，受影响的任务会暂停并保留进度。 */
  async function confirmImpact(): Promise<void> {
    impactConfirmed = true;
    pendingImpact.value = null;
    await saveNow();
  }

  /** 用户取消：不改动板面（也不保存），执行中的任务继续。 */
  function cancelImpact(): void {
    impactConfirmed = false;
    pendingImpact.value = null;
    void refreshBoardFromServer();
  }

  function dismissMaterialPaused(): void {
    materialPaused.value = [];
  }

  /** 保存前的只读预判（给组件用；不改任何状态）。 */
  async function checkMaterialImpact(state: BoardState) {
    return api.previewMaterialImpact(boardId.value, state);
  }

  /** 切换板面指针模式（工具栏与画布共用）。 */
  function setBoardMode(mode: "select" | "rect" | "link"): void {
    boardMode.value = mode;
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
    chatOpen,
    batchOpen,
    tasksOpen,
    boardMode,
    batches,
    listBatches,
    runningIntents,
    finishedIntents,
    taskCount,
    pendingImpact,
    materialPaused,
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
    draftStates,
    lastDraftKey,
    draftSaveStatus,
    draftSaveError,
    draftStateFor,
    flushDrafts,
    retryDraftSave,
    submit,
    approve,
    reject,
    decideBatch,
    updatePreview,
    advanceDemo,
    createDemoIntents,
    confirmImpact,
    cancelImpact,
    dismissMaterialPaused,
    checkMaterialImpact,
    setBoardMode,
    intentById,
    statusLabel,
  };
});
