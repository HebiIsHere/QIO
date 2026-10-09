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
import {
  cardDraftKey,
  cardIdFromDraftKey,
  isDraftRecord,
  isStaleReceipt,
  listLocalCardDraftIds,
  readCardLocalDraft,
  removeCardLocalDraft,
  removeCardLocalDraftIfUnchanged,
  writeCardLocalDraft,
  ensureCardLocalClear,
  hasCardLocalClear,
  writeCardLocalClear,
  type DraftRecord,
} from "../interactive/drafts";
import {
  cloneState,
  emptyBoardState,
  type BoardState,
  type BoardStateResponse,
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
/**
 * 等旧请求结束之后最多重新检查几次（契约 §10.3）。
 * 串行保存必须保留，但「等旧请求」不能拿到结果就返回、把后来的版本遗忘；
 * 重新检查是有界的：失败会明确停在 error 等用户重试，不做无界自动重试。
 */
const DRAFT_FLUSH_MAX_PASSES = 4;
/*
 * 卡片草稿的键一律走 `interactive/drafts.ts` 的构造函数（§11.1：两种记录身份都要能被识别与枚举）：
 * - `cardDraftKey(cardId)`      与服务器同步的草稿（内存与服务端草稿接口里的键）
 * - `cardLocalDraftKey(cardId)` 本机恢复副本的身份；实际存储键由 drafts.ts 内部统一拼
 * 这里不再出现手写的字符串前缀，也不再需要「清除哨兵序号」——
 * 「已被用户清除、还没同步」现在是一条真正的本机记录（kind: "cleared"，§11.2）。
 */
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
  /**
   * 本机草稿记录（存在记录 / 恢复副本）的写入结果（§10.5 / §11.3）。
   * 本机存储写不进去时必须能提示并重试，不能只在内存里假装存过了。
   *
   * 与 `draftStates`（**服务器**保存结果）分开：服务器已经成功时，
   * 绝不能因为本机这一路失败就把它显示成服务器保存失败（§11.3）。
   */
  const draftLocalStates = ref<Record<string, { ok: boolean; error: string | null }>>({});
  /**
   * 每个草稿键上「待确认的清除」状态（§11.2）：
   * 删除是一项待同步的真实变化，服务器确认前**不能**算已同步；失败要有准确状态与重试。
   */
  const draftRemovalStates = ref<Record<string, { status: "idle" | "pending" | "error"; error: string | null }>>({});
  /**
   * 按记录自己的版本无法判定新旧时留下的冲突（§11.3）：两份内容都保留，等用户明确选择。
   * key 是草稿键，值是「本机那份」与「服务器那份」的正文。
   */
  const draftConflicts = ref<Record<string, { local: string; server: string }>>({});
  /**
   * 每个未决冲突登记时的**本机记录版本**（不进 draftConflicts 的公开形状）。
   * 「清理只作用于已经确认处理的对应版本」（§12.1/§12.4）：用户做出选择时，
   * 只有仍是这一版的记录才允许被清掉；已被更晚写入替换的记录不动。
   */
  const conflictVersions = new Map<string, number>();

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


  /** 保存前的影响确认：这次改动会影响这些执行中的任务，等用户决定 */
  const pendingImpact = ref<{
    affected: { intentId: string; title: string; materials: string[]; consequence: string }[];
    /** 这次预判针对的候选版本（M4/08 路径2）：确认只对这一版有效 */
    previewRev: number;
    /** 这次预判针对的已保存板面版本 */
    stateVersion: number;
    /** 服务端预判句柄（C 的 M4 协议）：有就带上，服务端据此拒绝过期确认 */
    checkId?: string;
  } | null>(null);
  /** 保存后服务端回报「因为这些改动被暂停的任务」 */
  const materialPaused = ref<Intent[]>([]);
  let impactConfirmed = false;
  /**
   * 影响预判失败的真实原因（08）：预判失败时保存暂停，候选保留，等用户重试。
   * 这里不与 saveError 混用：一个是「保存」，一个是「保存前的预判」。
   */
  const impactCheckError = ref<string | null>(null);
  /**
   * 板面候选的版本记账（M1，收尾轮契约）：
   * - `boardLocalRev`：每产生一个新候选（commit/undo/redo/load）就 +1；
   * - `boardCleanRev`：当前板面与服务器一致的那一版的 localRev（刚读过 / 保存刚被确认）。
   * `boardLocalRev > boardCleanRev` ⟺ 有未保存的本地候选（与 dirty 同步维护）。
   * 任何旧回执、旧读取落地前都要用这对计数器判断「它读的版本是不是还是当前事实」。
   */
  let boardLocalRev = 0;
  let boardCleanRev = 0;
  /**
   * 用户已确认的影响检查句柄（M4）：
   * - `pendingConfirm` 在用户点「确认」后设置，随这一次保存的 PUT 一起提交；
   * - `confirmedCheck` 记录「哪一次确认授权了哪个已保存版本」，只有板面自那以后没有新改动时
   *   才允许在提交时把它作为 confirmedCheckId 带上（避免拿过期授权去提交）。
   */
  let pendingConfirm: { checkId: string; stateVersion?: number } | null = null;
  let confirmedCheck: { checkId: string; savedSeq: number } | null = null;

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
  /**
   * 用户已清除、但服务端还没确认的草稿键（§11.2）。
   *
   * 键是**草稿键**（`card:<id>`），值是这次清除针对的本机版本号：
   * - 服务端草稿是整份替换保存的，所以「提交剩余集合」本身就完成了删除；
   * - 只要这份依据还在，`flushDrafts` 就必须真的发一次请求（清空最后一份也要发）；
   * - 版本号用来保证「旧版本的清除不许删掉后来新建的版本」。
   */
  const pendingRemovals = new Map<string, { cardId: string; version: number }>();
  /**
   * 本机副本删除**真的失败**、还没处理完的键（13）：值是当时要删的记录版本（null = 按对象删）。
   * 只有 storage-failure 才登记；version-guard 是「另一份更新的记录还在」的有意保留，不是失败。
   * 重试入口（retryDraftSave）会连同它一起重试，界面不会停在「看起来删掉了、其实还在」。
   */
  const pendingLocalRemovals = new Map<string, number | null>();
  /**
   * 07：组件登记「这次板面变更成功后要清哪个键的草稿」。
   *
   * 组件只**登记**，不在这里删候选、不写 cleared 依据、不排草稿请求 ——
   * 正式变更（板面保存）真实成功之前，取消/失败/等待确认都必须保留候选与恢复来源。
   * version = 登记时 draftKeySeq 的那一版；登记后用户又输入更新版本 → 该次登记作废。
   */
  const pendingDraftClears = new Map<string, number>();
  /**
   * 登记表的**响应式镜像**（给界面/测试观察用）。
   *
   * 不能直接 computed 一个普通 Map：Map 的变化不会触发 computed 重算，
   * 首次读到的数组会被永久缓存，于是「已经消化完的登记」看起来还在（假失败）。
   * 每次改动登记表都调用 syncPendingDraftClearKeys() 同步这一份。
   */
  const pendingDraftClearKeys = ref<string[]>([]);
  function syncPendingDraftClearKeys(): void {
    pendingDraftClearKeys.value = [...pendingDraftClears.keys()];
  }

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
    // 新的改动让既有影响确认授权失效：它只授权它当时那一版（08 路径2）
    confirmedCheck = null;
    pendingConfirm = null;
    boardLocalRev += 1;
    dirty.value = true;
    saveStatus.value = "saving";
    scheduleSave();
  }

  /**
   * 影响检查的统一入口（M4）：优先用服务端定稿的 impact-check；测试替身只提供旧预判时
   * 回退到旧接口，保证既有测试的 mock 仍能用（生产路径走新接口）。
   */
  async function runImpactCheck(
    stateVersion: number,
    state: BoardState,
  ): Promise<import("../services/interactive").ImpactCheckResult> {
    const mod = api as unknown as {
      checkMaterialImpact?: (
        boardId: string,
        version: number,
        candidate: BoardState,
      ) => Promise<import("../services/interactive").ImpactCheckResult>;
    };
    // 注意：测试替身（vi.mock 工厂）访问未声明的导出会抛错，这里必须就地兜住，
    // 不能让「替身没实现新接口」表现成「预判失败」。
    let checker: typeof mod.checkMaterialImpact;
    try {
      checker = mod.checkMaterialImpact;
    } catch {
      checker = undefined;
    }
    if (typeof checker === "function") {
      return checker(boardId.value, stateVersion, state);
    }
    const legacy = await api.previewMaterialImpact(boardId.value, state);
    return {
      ok: true,
      affected: legacy.affected,
      impactConfirmationRequired: (legacy.affected ?? []).length > 0,
    };
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
    // 本次 PUT 对应的候选版本与「上次与服务器一致」版本（M1）：回执落地时用来判断
    // 「回执期间有没有出现更新的候选 / 更新的读取」。旧回执不许覆盖新候选（06 反例 A）。
    const putRev = boardLocalRev;
    const putCleanRev = boardCleanRev;
    // 保存前先问服务端一句：这次改动会不会碰到正在执行任务依赖的材料？
    // 会 → 不保存，把影响说明交给用户决定（继续=保存并暂停相关任务；取消=不改动，任务继续）。
    if (!impactConfirmed && activeIntents.value.length > 0) {
      try {
        const check = await runImpactCheck(snapshot.seq, snapshot);
        // 只有**执行中**的任务才需要「先说明影响再让用户决定」。
        // 已经暂停的任务不该拦住保存：它的依据已经失效是历史事实，用户每次编辑都被拦
        // 会让板面根本存不下去（复核实测过这个后果）。暂停的影响只作为提示显示。
        const running = new Set(
          intents.value.filter((item) => item.status === "running").map((item) => item.id),
        );
        const affected = (check.affected ?? check.affectedTasks ?? []) as {
          intentId: string;
          title: string;
          materials: string[];
          consequence: string;
        }[];
        if (check.ok === false) {
          // 服务端如实说明「为什么这次预判没做成」（版本过期 / 无法预判）：不改任何状态
          impactCheckError.value = `这次保存前的影响预判没有完成，保存已暂停（${check.reason || "原因未知"}）`;
          saveStatus.value = "idle";
          return;
        }
        const blocking = affected.filter((item) => running.has(item.intentId));
        const needsConfirm =
          check.impactConfirmationRequired === true ? affected.length > 0 : blocking.length > 0;
        if (needsConfirm) {
          // 确认必须绑定这次预判的候选版本与范围（M4/08 路径2）：
          // 等待期间又改了别处，确认时要重新核实，不能放行未说明的改动。
          pendingImpact.value = {
            affected: blocking.length ? blocking : affected,
            previewRev: putRev,
            stateVersion: check.stateVersion ?? snapshot.seq,
            checkId: check.checkId,
          };
          saveStatus.value = "idle";
          return;
        }
      } catch (err) {
        // 影响预判失败（08 路径1）：保存暂停、候选保留、真实原因可见，不阻塞在无解释的等待里。
        impactCheckError.value = `这次保存前的影响预判没有完成，保存已暂停（${(err as Error).message || "原因未知"}）`;
        saveStatus.value = "idle";
        return;
      }
    }
    impactCheckError.value = null;
    saveInFlight = (async () => {
      try {
        const confirmPayload =
          impactConfirmed && pendingConfirm ? pendingConfirm : undefined;
        const result = await api.saveBoardState(
          boardId.value,
          snapshot,
          lastOpLabel.value || "op",
          confirmPayload,
        );
        const newerCandidate = boardLocalRev !== putRev;
        const boardReadAdvanced = boardCleanRev !== putCleanRev;
        if (newerCandidate || boardReadAdvanced) {
          // 回执在飞期间出现了更新的候选（用户继续编辑）/ 或一次更新的读取已经落地：
          // 服务器已接受的是旧版本，板面（新候选）不被这次旧回执覆盖。
          // 新候选保持未保存并安排下一轮保存；读取推进的情形下板面仍是本次读取的事实。
          dirty.value = newerCandidate;
          if (newerCandidate) {
            confirmedCheck = null;
            pendingConfirm = null;
          }
          saveStatus.value = newerCandidate ? "saving" : "saved";
          lastSavedAt.value = result.savedAt;
          impactConfirmed = false;
          if (newerCandidate) scheduleSave();
          return;
        }
        board.value = result.state;
        boardCleanRev = putRev;
        lastSavedAt.value = result.savedAt;
        saveStatus.value = "saved";
        saveError.value = null;
        dirty.value = false;
        impactConfirmed = false;
        if (confirmPayload) {
          // 这次保存由用户确认授权：记住它授权的已保存版本，供提交时校验
          confirmedCheck = { checkId: confirmPayload.checkId, savedSeq: result.seq ?? putRev };
        }
        pendingConfirm = null;
        /*
         * 07：正式变更真的落地了，才执行登记的草稿清除。
         * 版本守卫：登记之后用户又输入了更新版本（draftKeySeq 前进）→ 该次登记作废，
         * 绝不为了清旧稿误删后来的输入。
         */
        for (const [key, version] of pendingDraftClears) {
          if (Object.prototype.hasOwnProperty.call(drafts.value, key) && (draftKeySeq.get(key) ?? 0) !== version) {
            pendingDraftClears.delete(key);
            continue;
          }
          pendingDraftClears.delete(key);
          clearDraft(key);
        }
        syncPendingDraftClearKeys();
        const impact = (result as { materialImpact?: { paused?: Intent[] } }).materialImpact;
        if (impact?.paused?.length) {
          materialPaused.value = impact.paused;
          void loadIntents();
        }
        // 保存只落到本机板面；顺手刷新「本次允许查看的范围」让提交前预览是新的。
        // 这一步同样不调用 QIO。
        void refreshVisibleRange();
      } catch (err) {
        const status = (err as { status?: number }).status;
        const failure = (err as { payload?: { error?: string; reason?: string; affectedTasks?: { intentId: string; title: string; materials: string[]; consequence: string }[] } }).payload;
        if (status === 409 && failure?.error === "impact_confirmation_required") {
          // 服务端门（M4）：这次保存会改动运行任务依赖的材料，必须先做影响确认。
          // 候选与勾选保留，不落库；把服务端列出的真实受影响任务交给用户决定。
          pendingImpact.value = {
            affected: failure.affectedTasks ?? [],
            previewRev: putRev,
            stateVersion: snapshot.seq,
          };
          saveStatus.value = "idle";
          saveError.value = null;
          return;
        }
        if (status === 409 && failure?.error === "stale_check") {
          // 确认句柄过期：重新核实，不落库
          impactCheckError.value = `这次改动的影响确认已经过期，请重新确认（${failure.reason ?? "板面版本已变化"}）`;
          saveStatus.value = "idle";
          saveError.value = null;
          return;
        }
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
    boardLocalRev += 1;
    dirty.value = true;
    lastOpLabel.value = "撤销";
    scheduleSave();
  }

  function redo() {
    if (!canRedo.value || !board.value) return;
    const next = redoStack.value.pop() as string;
    undoStack.value.push(JSON.stringify(board.value));
    board.value = JSON.parse(next) as BoardState;
    boardLocalRev += 1;
    dirty.value = true;
    lastOpLabel.value = "重做";
    scheduleSave();
  }

  /** 这一版内存内容是不是比 `before` 时更新（含请求在飞期间的新输入，§11.1） */
  function memoryIsNewer(key: string, before: Map<string, number>): boolean {
    const seq = draftKeySeq.get(key) ?? 0;
    const saved = draftSavedKeySeq.get(key) ?? 0;
    return seq > (before.get(key) ?? 0) || seq > saved;
  }

  /** 这个板面上还有这张卡（已删除的对象不恢复草稿，§11.1） */
  function boardHasCard(cardId: string): boolean {
    return (board.value?.cards ?? []).some((card) => card.id === cardId && !card.deleted);
  }

  /**
   * 恢复本机记录（§11.1）：**枚举本机记录**，不看服务器有没有这份草稿。
   *
   * 这是「第一次编辑、防抖还没到就刷新」能恢复的关键：那时服务器上没有这份草稿，
   * 只按服务器/内存里已有的键去遍历永远发现不了它。
   *
   * 恢复的边界（逐条都按记录自己的信息判断，不用整个草稿集合的更新时间，§11.3）：
   * - 记录属于别的板面 → 不恢复（也不删，那是别的板面的恢复数据）；
   * - 记录属于已删除/不存在的卡片 → 不恢复；
   * - 记录是「待确认的清除」→ 不是草稿：不许复活，登记一条待同步的清除；
   * - 请求期间用户又输入了新文字 → 以新输入为准，绝不用服务器返回值覆盖；
   * - 恢复结果只回到**编辑草稿**（内存里的 drafts），不写进正式卡片内容。
   */
  function restoreLocalCardDrafts(merged: Record<string, string>): void {
    for (const cardId of listLocalCardDraftIds()) {
      const key = cardDraftKey(cardId);
      const local = readCardLocalDraft(cardId);
      if (!local) continue;

      // —— 恢复顺序（§12.2）第一步：先确认**归属** ——
      // 记录自己写了归属、和当前板面对不上就跳过：别的板面的编辑副本**与清除依据**
      // 都不许作用于本板（§12.2：别板面的 cleared 不作用于本板）。
      if (local.boardId && local.boardId !== boardId.value) continue;
      // 已删除或不存在的对象不恢复草稿
      if (!boardHasCard(cardId)) {
        // 这份本机记录已经没有可归属的对象了；留着只会在别的对象上误恢复（§10.4）
        applyLocalRemovalResult(key, removeCardLocalDraft(cardId), null);
        continue;
      }

      // —— 第二步：看它是否已被**更晚的编辑**取代 ——
      // 内存里已经持有这个键（本次会话写下的内容，无论已同步还是未同步）时，内存内容更晚：
      // 旧的 cleared 依据不许作用于它之后的新输入（本机那次写入失败时，磁盘上留下的
      // 还是旧依据，内存里的新文字才是当前事实）；本机编辑副本也不许盖掉它。这一步
      // 必须在「cleared 生效」**之前**（§12.2）。
      if (Object.prototype.hasOwnProperty.call(drafts.value, key)) continue;

      // —— 第三步：记录仍有效，才轮到 cleared / 恢复生效 ——
      if (local.kind === "cleared") {
        /**
         * 删除是一项**待确认的变化**（§11.2）：刷新后仍然知道这份旧草稿要清掉。
         * 服务器上那份旧记录不许再出现在编辑器里（否则就是「清除后又复活」）。
         */
        pendingRemovals.set(key, { cardId, version: local.version ?? 0 });
        setDraftRemovalState(key, "pending", null);
        if (Object.prototype.hasOwnProperty.call(merged, key)) delete merged[key];
        continue;
      }

      const serverText = Object.prototype.hasOwnProperty.call(merged, key) ? merged[key] : null;
      if (serverText !== null && serverText === local.text) {
        // 服务器上已经有同样一份：这条本机记录已经被确认，按对象清理掉
        applyLocalRemovalResult(key, removeCardLocalDraft(cardId), null);
        continue;
      }
      if (serverText !== null) {
        /**
         * 服务器上另有一份：只有一个本地版本号（正数也一样）不足以证明比服务器新 ——
         * 服务器的草稿不带本机版本号，新旧**无法判定**（§12.1：旧格式与当前格式都按此检查）。
         * 两份都保留：内存草稿先显示本机候选（编辑框是它），冲突记录里留服务器事实，
         * flushDrafts 按服务器事实回写该键 —— 交给用户明确选择，不静默丢弃也不静默覆盖。
         */
        conflictVersions.set(key, typeof local.version === "number" && local.version > 0 ? local.version : 0);
        merged[key] = local.text;
        setDraftConflict(key, { local: local.text, server: serverText });
        continue;
      }

      // 本机独有的记录：服务器没有这份草稿也要恢复（这正是「第一次输入」的形态）
      merged[key] = local.text;
      draftKeySeq.set(key, ++draftSeq);
      setDraftLocalState(key, { ok: true, error: null });
      setDraftState(key, "saving");
      scheduleDraftSave();
    }
  }

  async function refreshBoardFromServer() {
    // 请求发出前的内存版本：迟到返回时用它判断「用户是不是已经又输入了」（§11.1）
    const before = new Map(draftKeySeq);
    // 各键「已保存版本」的读取基线（M1/01）：GET 在飞期间一次 PUT 先成功到达，
    // saved 越过读取基线——这次 GET 返回的正文对那个已确认保存的新版本是迟到的，
    // 不予应用（新输入由 memoryIsNewer 覆盖；两者合起来才完整）。
    const readSaved = new Map(draftSavedKeySeq);
    const payload = await api.fetchBoardState(boardId.value);
    boardId.value = payload.board.id;
    submissions.value = payload.submissions ?? [];
    /*
     * 板面候选的落地决定（M1/06）必须排在草稿合并**之前**：
     * `restoreLocalCardDrafts` 要按「这个板面上还有没有这张卡」判断本机记录的归属，
     * 所以它必须看到本次读取之后的板面；把整块落地放到合并后面会让首屏加载
     * 把有效本机记录当成无主记录清掉（回归过）。
     *
     * - 没有未保存候选：服务器状态就是当前事实，整块落地（撤销栈、dirty、状态一起收敛）；
     * - 有未保存候选（审批收尾保存失败后回读、旧回执在飞期间的新编辑）：
     *   整块板面**不覆盖**候选，候选保持未保存，服务器新结果等下一次保存成功后再采纳。
     */
    if (boardLocalRev > boardCleanRev) {
      // 草稿仍然要合并（按候选板面判断归属），只是板面本身不被这次读取覆盖
      mergeDraftsFromPayload(payload, before, readSaved);
      if (pendingRemovals.size > 0) scheduleDraftSave();
      return;
    }
    board.value = payload.state;
    boardCleanRev = boardLocalRev;
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
    mergeDraftsFromPayload(payload, before, readSaved);
    // 刷新后发现的「待同步清除」要真的发出去：清空最后一份也要发（§11.2）
    if (pendingRemovals.size > 0) scheduleDraftSave();
  }

  /**
   * 草稿合并（M1/01）：服务器正文、本机未保存的新输入、以及「GET 在飞期间保存成功的新版本」
   * 三者按版本关系落地，任何一条都不被迟到的读取回退。
   */
  function mergeDraftsFromPayload(
    payload: BoardStateResponse,
    before: Map<string, number>,
    readSaved: Map<string, number>,
  ): void {
    // 服务端是已保存内容的事实来源，但**本地还没保存成功的编辑内容**优先：
    // 失败/在飞的草稿不能被服务端旧值覆盖（§9.5「失败时保留编辑内容」），
    // 请求期间用户新输入的文字同样优先（§11.1），
    // GET 在飞期间已保存成功的新版本同样不被迟到的旧正文回退（M1/01）。
    const serverDrafts = payload.drafts?.drafts ?? {};
    const mergedDrafts: Record<string, string> = { ...serverDrafts };
    for (const key of Object.keys(drafts.value)) {
      const savedNow = draftSavedKeySeq.get(key) ?? 0;
      const keepLocal =
        memoryIsNewer(key, before) ||
        savedNow > (readSaved.get(key) ?? 0) ||
        !Object.prototype.hasOwnProperty.call(serverDrafts, key);
      if (keepLocal) mergedDrafts[key] = drafts.value[key] ?? "";
    }
    restoreLocalCardDrafts(mergedDrafts);
    drafts.value = mergedDrafts;
    // 未保存/失败的键保留自己的状态；GET 在飞期间保存成功（越过读取基线）的键
    // 仍然是「已保存」——迟到的读取不许把它说成没有状态（M1/01）。
    const keptStates: Record<string, { status: DraftSaveState; error: string | null }> = {};
    for (const key of Object.keys(mergedDrafts)) {
      const keySeq = draftKeySeq.get(key) ?? 0;
      const savedNow = draftSavedKeySeq.get(key) ?? 0;
      const state = draftStateFor(key);
      if (keySeq > savedNow || state.status === "error") {
        keptStates[key] = state;
      } else if (savedNow > (readSaved.get(key) ?? 0)) {
        keptStates[key] = { status: "saved", error: null };
      }
    }
    draftStates.value = keptStates;
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
   * 本机恢复记录（不是服务器草稿）的写入结果。
   * 默认 ok：没有要保护的内容时不算失败，界面不该平白冒出提示。
   */
  function draftLocalStateFor(key: string): { ok: boolean; error: string | null } {
    return draftLocalStates.value[key] ?? { ok: true, error: null };
  }

  function setDraftLocalState(key: string, result: { ok: boolean; error?: string | null }): void {
    draftLocalStates.value = { ...draftLocalStates.value, [key]: { ok: result.ok, error: result.error ?? null } };
  }

  /** 某个草稿键「待同步的清除」状态（§11.2）：没登记过就是 idle */
  function draftRemovalStateFor(key: string): { status: "idle" | "pending" | "error"; error: string | null } {
    return draftRemovalStates.value[key] ?? { status: "idle", error: null };
  }

  function setDraftRemovalState(key: string, status: "idle" | "pending" | "error", error: string | null = null): void {
    draftRemovalStates.value = { ...draftRemovalStates.value, [key]: { status, error } };
  }

  function draftConflictFor(cardIdOrKey: string): { local: string; server: string } | null {
    const key = cardIdFromDraftKey(cardIdOrKey) ? cardIdOrKey : cardDraftKey(cardIdOrKey);
    return draftConflicts.value[key] ?? null;
  }

  function setDraftConflict(key: string, conflict: { local: string; server: string } | null): void {
    const next = { ...draftConflicts.value };
    if (conflict) next[key] = conflict;
    else {
      delete next[key];
      conflictVersions.delete(key);
    }
    draftConflicts.value = next;
  }

  /**
   * 服务器草稿保存结果与本机恢复记录保存结果的**合并口径**（§11.3）。
   *
   * 一份提示只能有一个「主要原因」，但两个结果必须分别可见：
   * - 服务器失败 → 原因是服务器的；
   * - 服务器成功、本机失败 → 绝不显示成服务器保存失败（原因取本机那一路）；
   * - 清除没同步 → 说清除，不说改写成功。
   */
  function draftProtectionStatus(cardId: string): {
    local: "ok" | "failed";
    server: DraftSaveState;
    error: string | null;
  } {
    const key = cardDraftKey(cardId);
    const local = draftLocalStateFor(key);
    const server = draftStateFor(key);
    const removal = draftRemovalStateFor(key);
    const error =
      removal.status === "error"
        ? removal.error
        : server.status === "error"
          ? server.error
          : !local.ok
            ? local.error
            : null;
    return { local: local.ok ? "ok" : "failed", server: server.status, error };
  }

  /**
   * 本机副本「真的没删掉」（storage-failure）时的真实原因；没有待处理失败时 null（13）。
   *
   * version-guard（另一份更新的记录仍在，有意的保留）与 missing-record（本来就没有）
   * 都不算失败，返回 null —— 界面不许把它们显示成删除失败。
   */
  function draftLocalRemovalErrorFor(cardIdOrKey: string): string | null {
    const key = cardIdFromDraftKey(cardIdOrKey) ? cardIdOrKey : cardDraftKey(cardIdOrKey);
    if (!pendingLocalRemovals.has(key)) return null;
    const state = draftLocalStateFor(key);
    return state.ok ? null : (state.error ?? "这份本机副本没能删掉，重开后可能又出现");
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

  /**
   * 有没有「在 `atSeq` 之后又改过、且还没保存成功」的更新版本。
   *
   * 只给串行保存用：一次请求失败后，**只有更新版本**才值得自动再试一次（契约 §10.3）；
   * 同一个版本失败就停下来，明确显示原因与重试入口 —— 不做无界的自动重试。
   */
  function hasNewerUnsaved(atSeq: number): boolean {
    return Object.keys(drafts.value).some((key) => {
      const seq = draftKeySeq.get(key) ?? 0;
      return seq > atSeq && seq > (draftSavedKeySeq.get(key) ?? 0);
    });
  }

  /**
   * 空草稿也算「有草稿」：区分「没有草稿」与「存在但正文为空」（契约 §10.4）。
   *
   * 两个来源都要看，而且**都要按记录是否存在**判断（§11.1，不看字符串是否非空）：
   * - 内存里的 `drafts`（本次会话刚编辑过：哪怕正文是空串，它也是一条**存在**的草稿）；
   * - 本机记录（刷新/重开之后的恢复来源；「只存在于本机」的记录在这里被发现）。
   * 只看其中一个都会把「空草稿」判成「没有草稿」，于是旧正文又冒出来盖掉它。
   *
   * 待同步的清除依据（kind: "cleared"）**不是草稿**：用户已经清掉了它，
   * 不能在编辑器里把它当草稿显示出来（§11.2）。
   */
  /**
   * 本机记录里属于**当前板面**的那份编辑草稿（没有/不是草稿/属于别的板面都返回 null）。
   * 记录自己写了归属就按归属判断，绝不给别的板面恢复内容（§11.1）。
   */
  function localDraftFor(cardId: string): DraftRecord | null {
    const local = readCardLocalDraft(cardId);
    // 待同步的清除依据不是草稿：它只说明「这份旧草稿要清掉」，不能恢复成文字（§11.2）
    if (!isDraftRecord(local)) return null;
    if (local && local.boardId && local.boardId !== boardId.value) return null;
    return local;
  }

  function hasCardDraft(cardId: string): boolean {
    const key = cardDraftKey(cardId);
    if (Object.prototype.hasOwnProperty.call(drafts.value, key)) return true;
    return localDraftFor(cardId) !== null;
  }

  /** 卡片草稿正文：存在则为草稿内容（可以是空串）；不存在时返回空串。 */
  function cardDraftText(cardId: string): string {
    const key = cardDraftKey(cardId);
    if (Object.prototype.hasOwnProperty.call(drafts.value, key)) return drafts.value[key];
    return localDraftFor(cardId)?.text ?? "";
  }

  /**
   * 清掉一条草稿（用户确认编辑 / 删除卡片时用）。
   *
   * **不能只是写一个空串**：那会留下一条「存在且正文为空」的草稿，
   * 下次打开编辑器会把用户刚确认的正式内容盖成空（契约 §10.4）。
   *
   * 删除本身是一项**待确认的真实变化**（§11.2）：
   * - 本机留下「待同步清除」的依据（kind: "cleared"），刷新后仍然知道这份旧草稿要清掉；
   * - 登记待同步删除，`flushDrafts` 必须真的发一次请求（清空最后一份也要发）；
   * - 服务器确认之前状态**不是**已同步：清除失败要能看到原因并重试；
   * - 版本往前推，让还在飞的旧保存回执不再算数（不许把已清除的草稿复活）。
   */
  function clearDraft(key: string): void {
    if (Object.prototype.hasOwnProperty.call(drafts.value, key)) {
      const next = { ...drafts.value };
      delete next[key];
      drafts.value = next;
    }
    draftKeySeq.set(key, ++draftSeq);
    draftSavedKeySeq.delete(key);
    setDraftState(key, "idle");
    setDraftConflict(key, null);
    const cardId = cardIdFromDraftKey(key);
    if (!cardId) return;
    const cleared = writeCardLocalClear(cardId, { boardId: boardId.value, seq: draftKeySeq.get(key) ?? 0 });
    setDraftLocalState(key, cleared);
    /**
     * 12：只登记**真实写进存储**的版本（committedVersion）。
     * 写失败时磁盘上仍是旧记录，拿「计划版本」去登记会让后面的清理版本守卫对不上：
     * 服务器清除成功了，却删不掉磁盘上那份旧稿，重开就复活。
     */
    const committed = cleared.ok ? cleared.committedVersion ?? cleared.version : undefined;
    if (committed !== undefined) {
      pendingRemovals.set(key, { cardId, version: committed });
      pendingLocalRemovals.delete(key);
    } else {
      /**
       * 本机那份 cleared 依据**没写成功**（12 的第二个反例）：
       * 磁盘上留下的还是旧的 kind=draft 副本，它仍然有「恢复并重新上传」的权限。
       * 两件事都要登记：
       * - `pendingLocalRemovals`：本地清除保护待补写（重试时先补写，而不是只删记录）；
       * - `pendingRemovals`：网络那一路的清除仍要真的发出去，版本取**磁盘上那份旧副本的真实版本**
       *   （它才是要被清掉的对象；拿计划版本会与版本守卫对不上）。
       */
      const onDisk = readCardLocalDraft(cardId);
      const onDiskVersion =
        onDisk && typeof onDisk.version === "number" ? onDisk.version : (draftKeySeq.get(key) ?? 0);
      /**
       * 预期版本 = 磁盘上那份要被清除的旧副本的版本（版本守卫的依据）：
       * 补写 cleared 时若磁盘上已经是**更晚**的一版（同一浏览器另一个页面写的），
       * 必须保留它、不许覆盖，也不许随后把它删掉。
       */
      pendingLocalRemovals.set(key, onDiskVersion);
      pendingRemovals.set(key, { cardId, version: onDiskVersion });
    }
    setDraftRemovalState(
      key,
      cleared.ok ? "pending" : "error",
      cleared.ok ? null : cleared.error ?? "本机没能记下这次清除，刷新后这份旧草稿可能重新出现",
    );
    if (cleared.ok) scheduleDraftSave();
  }

  /**
   * 07：登记「这次板面变更成功后要清这个键的草稿」。
   *
   * 触发方（完成编辑 / 删除卡片 / 删除所选）在这里只登记：
   * 不删内存候选、不写 cleared 依据、不排草稿请求 —— 板面变更还在影响确认里、
   * 保存还没成功的时候，草稿候选与恢复来源必须原样留着。
   * 真正的清除由 `saveNow` 在「回执对应当前候选且没有任何更新候选」时按登记版本执行。
   */
  function requestDraftClear(key: string): void {
    pendingDraftClears.set(key, draftKeySeq.get(key) ?? 0);
    syncPendingDraftClearKeys();
  }

  /** 文字草稿：输入过程中保存，**不调用 QIO**，也不等于提交内容。 */
  function setDraft(key: string, text: string) {
    const conflict = draftConflicts.value[key];
    if (conflict && text === conflict.local) {
      // 打开编辑器/原样写入本机候选**不等于**做出选择（§12.1）：冲突保持可见，
      // 不清冲突、不排保存 —— 否则用户还没选，本机候选就被写上服务器了。
      lastDraftKey.value = key;
      return;
    }
    drafts.value = { ...drafts.value, [key]: text };
    lastDraftKey.value = key;
    draftKeySeq.set(key, ++draftSeq);
    /**
     * 这一版取代了这个键上任何还没确认的清除（§11.2）：
     * 旧版本的清除不许删掉后来新建的版本，所以先撤掉待同步删除，再写下新版记录。
     */
    if (pendingRemovals.delete(key)) setDraftRemovalState(key, "idle");
    // 这一版取代了「还没删成功的本机副本」：目标变了，不再重试那次删除
    pendingLocalRemovals.delete(key);
    /**
     * 未决冲突（收尾轮 05）：改一个字**不**清冲突 —— 服务器那份候选继续保留，
     * 这份新输入只是「本机候选」的新版本；用户仍然必须明确选择才落地。
     * （只有本来就没有冲突时才走清空，保持既有语义。）
     */
    if (!conflict) setDraftConflict(key, null);
    /**
     * 本机恢复副本：**同步**写（不等防抖、不等网络）。
     * 正常刷新/关闭时来不及等防抖也能把最后输入恢复出来（契约 §10.5）；
     * 只用于编辑恢复 —— 不提交、不发送、不扩大 QIO 可见范围。
     * 写入结果**必须留下来**（§11.3）：本机写失败时不能只在内存里假装存过了。
     */
    const cardId = cardIdFromDraftKey(key);
    if (cardId) {
      const written = writeCardLocalDraft(cardId, text, { boardId: boardId.value, seq: draftKeySeq.get(key) ?? 0 });
      setDraftLocalState(key, written);
      if (conflict) {
        // 冲突里的「本机候选」已经换成这份新输入：两份来源都还在，选择入口继续可操作
        setDraftConflict(key, { local: text, server: conflict.server });
      }
      if (conflict && written.ok) {
        // 清理守卫跟着记录的新版本走
        conflictVersions.set(key, written.version);
      }
    }
    // 一有输入就进「保存中」：失败时才会被改成 error（绝不停在「已保存」）
    setDraftState(key, "saving");
    scheduleDraftSave();
  }

  /**
   * 用户对「无法判定新旧」的冲突做出选择（§11.3）：两份都保留过，选了才继续。
   * - 用本机的：这份内容继续作为草稿，重新排一次保存；
   * - 用服务器上的：把它作为当前草稿内容，本机那份冲突记录清掉（服务器上已经有它）。
   * 两种选择都不提交板面、不调用 QIO。
   */
  function resolveDraftConflict(cardIdOrKey: string, choice: "local" | "server"): void {
    const cardId = cardIdFromDraftKey(cardIdOrKey) ?? cardIdOrKey;
    const key = cardDraftKey(cardId);
    const conflict = draftConflicts.value[key];
    if (!conflict) return;
    // 冲突登记时记下的本机记录版本：清理只作用于「已确认的对应版本」（§12.1/§12.4）
    const confirmedVersion = conflictVersions.get(key);
    setDraftConflict(key, null);
    if (choice === "local") {
      // 明确选本机：本机候选就是当前草稿，按用户的选择重新排一次保存
      drafts.value = { ...drafts.value, [key]: conflict.local };
      draftKeySeq.set(key, ++draftSeq);
      draftSavedKeySeq.delete(key);
      setDraftState(key, "saving");
      scheduleDraftSave();
      return;
    }
    // 明确选服务器：编辑内容跟随服务器那份；本机那份候选按已确认的版本清理
    drafts.value = { ...drafts.value, [key]: conflict.server };
    draftKeySeq.set(key, ++draftSeq);
    draftSavedKeySeq.set(key, draftKeySeq.get(key) ?? 0);
    setDraftState(key, "saved");
    if (typeof confirmedVersion === "number" && confirmedVersion > 0) {
      // 版本守卫：记录已被更晚的写入替换（如同浏览器的另一个页面）时不能删
      applyLocalRemovalResult(key, removeCardLocalDraft(cardId, confirmedVersion), confirmedVersion);
    } else {
      // 旧格式记录没有版本可校验：用户的明确选择就是确认，按对象清理
      applyLocalRemovalResult(key, removeCardLocalDraft(cardId), null);
    }
  }

  /**
   * 消费「本机副本删除」的真实结果（13）：
   * - 删掉/本来就没有（ok）：清掉待处理决定；
   * - version-guard（另一份更新的记录仍在）：有意的保留，静默、不报错、不重试；
   * - storage-failure：真失败 —— 保留待处理决定 + 真实原因，等重试入口。
   */
  function applyLocalRemovalResult(
    key: string,
    result: ReturnType<typeof removeCardLocalDraft>,
    expectVersion: number | null,
  ): void {
    if (result.ok || result.reason === "version-guard") {
      pendingLocalRemovals.delete(key);
      setDraftLocalState(key, { ok: true, error: null });
      return;
    }
    pendingLocalRemovals.set(key, expectVersion);
    setDraftLocalState(key, {
      ok: false,
      error: result.error ?? "这份本机副本没能删掉，暂时还留在本机",
    });
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

  /** 还有「没做完的草稿工作」：未保存的内容，或还没被服务器确认的清除（§11.2） */
  function hasDraftWork(): boolean {
    return hasUnsavedDrafts() || pendingRemovals.size > 0;
  }

  /**
   * 把未保存的草稿写回服务端（**只保存草稿**：不建卡、不提交板面、不调用 QIO）。
   *
   * 同一时刻只允许一个请求在飞：两个并发 PUT 会按返回顺序落库，先发出、后返回的
   * 旧内容会把新内容盖掉。一个请求结束后如果又有了新输入，就再存一轮（有上限）。
   * 失败**不自动重试**：内存内容与错误原因都留着，等用户点重试或下一次输入。
   *
   * 清除草稿同样是这里发出的（§11.2）：服务端草稿是**整份替换**保存的，
   * 所以「提交剩余集合」本身就完成了删除 —— 清空最后一份也要发出请求（此时 payload 是 `{}`）。
   * 服务器确认之前这份删除都留在 `pendingRemovals` 里，绝不算已同步。
   */
  async function flushDrafts(): Promise<void> {
    if (draftTimer) {
      clearTimeout(draftTimer);
      draftTimer = null;
    }
    /**
     * 已经有请求在飞：**等它结束后再检查一次**未保存的新版本（契约 §10.3）。
     *
     * 不能直接 return —— 那正是「第二版被遗忘、界面永远停在保存中」的根因：
     * 第二版的防抖到点时旧请求还在飞，等待后直接返回，旧请求失败就再也没人管它了。
     * 这里等完后同样要重新检查「待确认的清除」：旧保存成功之后服务器上又有了那份草稿，
     * 清除请求必须真的再发一次，否则刷新就复活（§11.2）。
     */
    while (draftInFlight) await draftInFlight;
    if (!hasDraftWork()) return;
    let lastError: string | null = null;
    draftInFlight = (async () => {
      try {
        for (let round = 0; round < DRAFT_FLUSH_MAX_ROUNDS; round += 1) {
          if (!hasDraftWork()) return;
          const payload = { ...drafts.value };
          /**
           * 未决冲突键（§12.1）：本机候选**不上去**，也**不许从请求里删掉** ——
           * 服务端保存是整份替换，删键就是删掉服务器那份。未决时按**服务器事实**回写该键，
           * 本机候选留在本机记录与冲突记录里等用户选择。
           */
          const conflictKeys = new Set<string>();
          for (const [conflictKey, value] of Object.entries(draftConflicts.value)) {
            if (!value) continue;
            conflictKeys.add(conflictKey);
            payload[conflictKey] = value.server;
          }
          const atSeq = draftSeq;
          // 这次请求会一并清掉的删除依据（payload 就是剩余集合：不带某个键 = 删掉它）
          const removalsAtRequest = new Map(pendingRemovals);
          // 未决冲突键不属于「这次要保存成功的工作」：它们等用户选择，不发状态、不做成功清理
          const keys = Object.keys(payload).filter((key) => !conflictKeys.has(key));
          // 各键此刻本机记录的版本：在飞回执清理本地副本必须按记录版本校验（§12.2）。
          // 记账口径与 removeCardLocalDraftIfUnchanged 一致：有记录取 version（旧格式记 0），没有记录为 null。
          const localVersionsAtRequest = new Map<string, number | null>();
          for (const key of keys) {
            if ((draftKeySeq.get(key) ?? 0) <= atSeq) setDraftState(key, "saving");
            const cardId = cardIdFromDraftKey(key);
            if (cardId) {
              const record = readCardLocalDraft(cardId);
              const version = record ? (typeof record.version === "number" ? record.version : 0) : null;
              localVersionsAtRequest.set(key, version);
            }
          }
          for (const key of removalsAtRequest.keys()) setDraftRemovalState(key, "pending");
          try {
            await api.saveDrafts(boardId.value, payload);
            lastError = null;
          } catch (err) {
            lastError = (err as Error).message || "原因未知";
            for (const key of keys) {
              const keySeq = draftKeySeq.get(key) ?? 0;
              // 期间又改了内容：它属于下一轮，不要标成这次的失败（状态留给下一轮）
              if (isStaleReceipt(atSeq, keySeq)) continue;
              setDraftState(key, "error", lastError);
            }
            for (const [key, entry] of removalsAtRequest) {
              // 已经被新版本取代的清除不属于这次请求
              if (pendingRemovals.get(key)?.version !== entry.version) continue;
              setDraftRemovalState(key, "error", lastError);
            }
            /**
             * 只有「这次请求期间又改过」的更新版本才自动再试一次（有界）；
             * 同一个版本失败就**停下来**：明确显示原因与重试入口，等用户点重试或下一次输入。
             * 这样既不会把新版本一起吞掉，也不会无界重试、更不会假装成功。
             */
            if (!hasNewerUnsaved(atSeq)) break;
            continue;
          }
          for (const key of keys) {
            const keySeq = draftKeySeq.get(key) ?? 0;
            // 请求在飞时用户又改了：这次返回不算数，留给下一轮
            if (isStaleReceipt(atSeq, keySeq)) continue;
            draftSavedKeySeq.set(key, keySeq);
            setDraftState(key, "saved");
            // 服务端已经拿到这一版：本机恢复副本按「当时那一版」清理（契约 §10.5 / §12.2）。
            // 版本校验（§12.2）：请求在飞期间被同一浏览器的**另一个页面**换成新版本的记录不许被这次回执删掉。
            const cardId = cardIdFromDraftKey(key);
            if (cardId) {
              const expectVersion = localVersionsAtRequest.get(key) ?? null;
              applyLocalRemovalResult(
                key,
                removeCardLocalDraftIfUnchanged(cardId, expectVersion),
                expectVersion,
              );
            }
          }
          /**
           * 未决冲突键：服务器事实已成功回写。这里只把「保存工作」的记账清零
           * （避免每 600ms 反复重发同一回写），**冲突本身不动**，仍等用户明确选择（§12.1）。
           */
          for (const key of conflictKeys) {
            const keySeq = draftKeySeq.get(key) ?? 0;
            // 请求在飞时用户又输入了新候选：属于下一轮回写，不算这次
            if (isStaleReceipt(atSeq, keySeq)) continue;
            draftSavedKeySeq.set(key, keySeq);
            setDraftState(key, "idle");
          }
          /**
           * 服务器确认了这次一并清掉的删除依据：
           * 只有**同一版本**才算确认 —— 用户在清除之后又编辑的新版本不能被旧清除删掉（§11.2）。
           */
          for (const [key, entry] of removalsAtRequest) {
            // 版本守卫：带计划版本时，服务器确认的版本可能与 pendingRemovals 里登记的不同，
            // 这时按「这次请求实际要清的版本」核对，避免把后来重建的记录误删。
            const registered = pendingRemovals.get(key);
            if (registered && registered.version !== entry.version) continue;
            pendingRemovals.delete(key);
            if (pendingLocalRemovals.has(key)) {
              /**
               * 12：本机清除保护还没写成 —— 先把 cleared 依据补写成功（幂等），
               * 而不是直接删记录：删除再失败一次，磁盘上仍是 kind=draft，重开就复活。
               */
              const protection = ensureCardLocalClear(entry.cardId, {
                boardId: boardId.value,
                seq: draftKeySeq.get(key) ?? 0,
                expectVersion: pendingLocalRemovals.get(key) ?? null,
              });
              if (protection.reason === "version-guard") {
                // 12b：更晚的一版草稿（别的页面写的）优先，这次清除不再作用于它
                pendingLocalRemovals.delete(key);
                setDraftLocalState(key, { ok: true, error: null });
              } else if (protection.ok) {
                pendingLocalRemovals.delete(key);
                setDraftLocalState(key, { ok: true, error: null });
              } else {
                setDraftLocalState(key, {
                  ok: false,
                  error: protection.error ?? "本机没能记下这次清除，重开后这份旧稿可能重新出现",
                });
              }
            } else {
              applyLocalRemovalResult(key, removeCardLocalDraft(entry.cardId, entry.version), entry.version);
            }
            setDraftRemovalState(key, "idle");
            setDraftState(key, "idle");
          }
        }
      } finally {
        draftInFlight = null;
      }
    })();
    await draftInFlight;
    /**
     * 请求结束后再看一眼：还有没保存的内容就必须**说清楚**（契约 §10.3）——
     * 要么继续安排下一次保存（真的会发出请求），要么明确显示未保存原因与重试入口。
     * 绝不留下「没有请求、没有计时、没有后续工作，却一直显示保存中」。
     */
    if (hasDraftWork()) {
      if (lastError) {
        for (const key of unsavedDraftKeys()) setDraftState(key, "error", lastError);
        for (const [key] of pendingRemovals) {
          if (draftRemovalStateFor(key).status === "pending") setDraftRemovalState(key, "error", lastError);
        }
      } else {
        // 轮次上限用尽但内容还在更新：交给下一次防抖，不在这里空转
        scheduleDraftSave();
      }
    }
  }

  /**
   * 用户点「重试」：只重写草稿，**不建卡、不提交板面、不调用 QIO**。
   * 不传 key 就重试所有还没保存成功的草稿与还没确认的清除。
   *
   * 重试要覆盖三件可能失败过的事（§11.2 / §11.3）：
   * 1. 服务器草稿保存；
   * 2. **本机恢复记录**的写入（之前只重发网络请求，本机那一路永远没被补上）；
   * 3. 待确认的清除（包括「内容已经清空、只剩清除」的情况）。
   */
  async function retryDraftSave(key?: string): Promise<void> {
    const keys = key ? [key] : unsavedDraftKeys();
    if (!key) {
      for (const item of pendingRemovals.keys()) if (!keys.includes(item)) keys.push(item);
    }
    /**
     * 本机副本删除失败过：这次先真的把它删掉（13）。
     * 删成功的键在本轮**不再**被下面的「本机写入」补写回去 —— 用户已经明确放弃这份副本，
     * 重建它等于把刚处理完的决定又撤销了。
     */
    const localRemovalRetries = key ? [key] : [...pendingLocalRemovals.keys()];
    const removedLocalNow = new Set<string>();
    for (const item of localRemovalRetries) {
      if (!pendingLocalRemovals.has(item)) continue;
      const retryCardId = cardIdFromDraftKey(item);
      if (!retryCardId) continue;
      /**
       * 12（独立复核发现的反例）：本机清除保护写失败、而服务器清除已经确认时，
       * 重试**必须先把本机那份 cleared 依据补写成功**（幂等），而不是只尝试删记录 ——
       * 删除再失败一次，磁盘上留着的仍是 kind=draft 的旧稿，重开后它会重新取得
       * 恢复与上传权限（旧稿复活）。补写成功后保留这份依据（它就是「已确认清除」的事实），
       * 由后续正常的服务器清除流程按版本清理。
       */
      const expectVersion = pendingLocalRemovals.get(item) ?? null;
      const protection = ensureCardLocalClear(retryCardId, {
        boardId: boardId.value,
        seq: draftKeySeq.get(item) ?? 0,
        expectVersion,
      });
      if (protection.reason === "version-guard") {
        /**
         * 12b（独立复核）：磁盘上已经是另一个页面写下的**更新**一版草稿 ——
         * 这次清除的目的（清掉自己那一版旧稿）已经不存在，也不该动更新的那一份。
         * 静默解除登记，不写、不删，界面不报失败。
         */
        pendingLocalRemovals.delete(item);
        pendingRemovals.delete(item);
        setDraftLocalState(item, { ok: true, error: null });
        setDraftRemovalState(item, "idle");
        continue;
      }
      if (!protection.ok) {
        setDraftLocalState(item, {
          ok: false,
          error: protection.error ?? "本机没能记下这次清除，重开后这份旧稿可能重新出现",
        });
        continue;
      }
      const committed = protection.committedVersion ?? protection.version;
      pendingLocalRemovals.delete(item);
      removedLocalNow.add(item);
      setDraftLocalState(item, { ok: true, error: null });
      if (pendingRemovals.has(item)) {
        // 网络那一路还没确认：把待同步清除的版本对齐到真实落盘的这一版
        pendingRemovals.set(item, { cardId: retryCardId, version: committed });
      }
    }
    for (const item of keys) {
      if (draftStateFor(item).status === "error") setDraftState(item, "saving");
      const cardId = cardIdFromDraftKey(item);
      if (!cardId) continue;
      // 本机那一路之前失败过：这次连本机一起重写
      if (
        !removedLocalNow.has(item) &&
        !draftLocalStateFor(item).ok &&
        Object.prototype.hasOwnProperty.call(drafts.value, item)
      ) {
        const written = writeCardLocalDraft(cardId, drafts.value[item] ?? "", {
          boardId: boardId.value,
          seq: draftKeySeq.get(item) ?? 0,
        });
        setDraftLocalState(item, written);
      }
      /**
       * 清除失败过（12）：重试必须**先重建本机清除保护**，再发网络清除。
       * 只重发网络的话，本机那条 cleared 依据始终没写进去 —— 网络请求在飞期间重开，
       * 磁盘上的旧稿会再次取得恢复与上传权限。
       * ensureCardLocalClear 幂等：磁盘上已经是 cleared 时不新写、不推进版本
       * （否则登记的确认版本会被换掉，清理反而删不掉）。
       */
      if (pendingRemovals.has(item) && draftRemovalStateFor(item).status === "error") {
        const retryCardId = cardIdFromDraftKey(item);
        if (retryCardId && !hasCardLocalClear(retryCardId)) {
          const protection = ensureCardLocalClear(retryCardId, {
            boardId: boardId.value,
            seq: draftKeySeq.get(item) ?? 0,
          });
          if (protection.ok) {
            const committed = protection.committedVersion ?? protection.version;
            pendingRemovals.set(item, { cardId: retryCardId, version: committed });
            setDraftLocalState(item, { ok: true, error: null });
            setDraftRemovalState(item, "pending");
          } else {
            setDraftLocalState(item, { ok: false, error: protection.error ?? "本机没能记下这次清除" });
            setDraftRemovalState(item, "error", protection.error ?? "本机没能记下这次清除，刷新后这份旧草稿可能重新出现");
          }
        } else if (retryCardId) {
          setDraftRemovalState(item, "pending");
        }
      }
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
    const blocked =
      pendingImpact.value
        ? "这次板面还有待确认的影响说明，请先选择确认或取消，本次未提交"
        : impactCheckError.value
          ? `保存前的影响预判没有完成，本次未提交（${impactCheckError.value}）`
          : saveStatus.value === "error" || dirty.value || !board.value
            ? `板面没有保存成功，本次未提交（${saveError.value ?? "原因未知"}）`
            : null;
    if (blocked) {
      // 08 路径3：等待影响确认、预判未完成或候选尚未保存成功时，不得发出板面提交；
      // 也不能把 idle 当成「已经保存」拿服务器旧板面当本次提交。
      submitStatus.value = "failed";
      submitError.value = blocked;
      return null;
    }
    const intentsBefore = intentIdSet();
    try {
      const confirmedCheckId =
        confirmedCheck && confirmedCheck.savedSeq === board.value.seq
          ? confirmedCheck.checkId
          : undefined;
      const result = await api.submitBoard(
        boardId.value,
        undefined,
        "",
        board.value.seq,
        confirmedCheckId,
      );
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
      const status = (err as { status?: number }).status;
      const failure = (err as { payload?: { error?: string; reason?: string } }).payload;
      submitStatus.value = "failed";
      submitError.value =
        status === 409 && failure?.error === "stale_state"
          ? `板面版本已经变化，本次未提交（${failure.reason ?? "服务端已有更新的已保存版本"}）。请确认当前板面后再提交`
          : (err as Error).message;
      // 提交失败保留改动与本次注释选择：这里不刷新、不清勾选、不动基准
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
    const pending = pendingImpact.value;
    pendingImpact.value = null;
    if (pending && pending.previewRev !== boardLocalRev) {
      /**
       * 等待确认期间板面又出现了新的候选（08 路径2）：这次说明只覆盖当时那一版，
       * 确认不能放行未说明的改动 —— 重新核实影响，再把新的说明交给用户。
       */
      impactConfirmed = false;
      impactCheckError.value = "等待确认期间板面又有改动，已重新核实这次改动的影响";
      await saveNow();
      return;
    }
    impactConfirmed = true;
    impactCheckError.value = null;
    if (pending?.checkId) {
      pendingConfirm = { checkId: pending.checkId, stateVersion: pending.stateVersion };
    }
    await saveNow();
  }

  /** 用户取消：这次未确认的候选放弃（07），回到服务器已保存状态；执行中的任务继续。 */
  async function cancelImpact(): Promise<void> {
    impactConfirmed = false;
    pendingImpact.value = null;
    impactCheckError.value = null;
    // 07：取消 = 这次板面变更不生效，那么它带来的草稿清除也不该落地
    pendingDraftClears.clear();
    syncPendingDraftClearKeys();
    /**
     * 卡片编辑草稿与恢复来源**不在这里清理**：「正式变更与草稿清理」的最终确认关系
     * 由 07 保证 —— 未确认、取消期间都保留候选，正式变更成功后才清对应版本。
     * 明确取消是用户决定：候选丢弃，直接回读服务器状态；回读失败则候选恢复为未保存并显示原因。
     */
    dirty.value = false;
    saveStatus.value = "idle";
    try {
      await refreshBoardFromServer();
    } catch (err) {
      saveStatus.value = "error";
      saveError.value = (err as Error).message;
      dirty.value = true;
    }
  }

  function dismissMaterialPaused(): void {
    materialPaused.value = [];
  }

  /** 保存前的只读预判（给组件用；不改任何状态）。走 M4 定稿的 impact-check。 */
  async function checkMaterialImpact(state: BoardState) {
    return runImpactCheck(state.seq, state);
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

    batches,
    listBatches,
    runningIntents,
    finishedIntents,
    taskCount,
    pendingImpact,
    impactCheckError,
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
    hasCardDraft,
    cardDraftText,
    clearDraft,
    draftStates,
    draftLocalStates,
    draftRemovalStates,
    draftConflicts,
    lastDraftKey,
    draftSaveStatus,
    draftSaveError,
    draftStateFor,
    draftLocalStateFor,
    draftRemovalStateFor,
    draftProtectionStatus,
    draftLocalRemovalErrorFor,
    requestDraftClear,
    pendingDraftClearKeys,
    draftConflictFor,
    resolveDraftConflict,
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
    intentById,
    statusLabel,
  };
});
