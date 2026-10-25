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
import { computed, ref, watch } from "vue";
import * as api from "../services/interactive";
// 阅读位置的读写属于**预览服务**（不是板面状态）：位置按「附件 + 文件版本」归属，
// 写它不产生表达、不改板面版本（契约 §3.3）。
import { putBoardPreviewPosition } from "../services/attachments";
import type { PreviewPosition } from "../services/attachments";
import {
  batchesWithList,
  executionIsLive,
  groupIntentsByBatch,
  recordIntentBatch,
  understandingSettled,
  understandingStatusLabel,
} from "../interactive/approval";
import {
  CHECKABLE_KINDS,
  applyParticipation,
  clampBoardBounds,
  participationOf,
  participationVisibleRange,
  syncLegacyParticipation,
} from "../interactive/board";
import { cardAttachmentIsImage } from "../interactive/materials";
import {
  cardDraftKey,
  cardIdFromDraftKey,
  cardLocalDraftFingerprint,
  isDraftRecord,
  localRecordFingerprint,
  isStaleReceipt,
  listLocalCardDraftIds,
  readCardLocalDraft,
  removeCardLocalDraft,
  removeCardLocalDraftIfUnchanged,
  writeCardLocalDraft,
  ensureCardLocalClear,
  hasCardLocalClear,
  writeCardLocalClear,
  writeCardDraftInput,
  type CardDraftInput,
  type DraftRecord,
} from "../interactive/drafts";
import {
  cloneState,
  emptyBoardState,
  type AcceptanceLookup,
  type BoardState,
  type BoardStateResponse,
  type DecideResult,
  type Execution,
  type ExecutionControlResult,
  type ExecutionStatus,
  type ImpactConfirmOutcome,
  type ImpactConfirmResult,
  type Intent,
  type IntentPreview,
  type IntentStatus,
  type Participation,
  type SubmissionRecord,
  type SubmissionResult,
  type Understanding,
  type UnderstandingResponse,
  type UnderstandingStatus,
  type VisibleRange,
} from "../interactive/types";
import {
  mergeServerInto,
  type BoardContentConflict,
  type RemovalConflict,
} from "../interactive/serverFacts";
import {
  acceptanceLookupText,
  acceptanceMatchesIdentity,
  canLookupAcceptance,
  identityConflictText,
  isRequestIdentityConflict,
  parseStoredSubmissionIdentity,
  serializeSubmissionIdentity,
  submissionIdentityFor,
  submissionIdentityStorageKey,
  frozenViewPayload,
  readBoardScale,
  type SubmissionIdentity,
} from "../interactive/submission";
import { readBoardArea } from "../interactive/viewport";
import { canRecover, type IntentView } from "../interactive/intentWorkbench";
// 纯逻辑放在 v2/（A 组自有）：任务行合并 / 恢复事项摊平都不读全局、不发请求，
// 由 store 传入真实服务端事实，界面也复用同一份，避免第二套判定。
import {
  buildTaskRows,
  taskCounts,
  type TaskRow,
} from "../components/interactive/v2/taskCenter";
import { buildRecoveryItems, type RecoveryItem } from "../components/interactive/v2/recoverySummary";

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
export type SubmitStatus =
  | "idle"
  | "submitting"
  | "succeeded"
  | "failed"
  | "empty"
  | "duplicate"
  /** F10：POST 没有拿到回执（超时/断线）—— 提交结果**未知**，不能假定成功或失败 */
  | "unknown";

/**
 * 界面上必须分开的四种状态（契约 §14.1）：
 * 提交已可靠受理 → 模型开始理解 → 理解完成（三态）→ 任务执行结束。
 *
 * `submitStatus` 只说这次**提交记录本身**的结果，它不再兼代表「模型理解完成」；
 * 理解过程看 understanding / understandingStatus，任务是否完成看 executions。
 */
export type SubmissionStage =
  | "idle"
  | "submitting"
  /** F10 + 契约 §1.3：没有拿到回执、且按请求身份也还没查证到受理事实 */
  | "unknown"
  | "accepted"
  | "understanding"
  | "understood"
  | "needs_clarification"
  | "no_action"
  | "understanding_failed"
  | "submit_failed"
  | "empty"
  | "duplicate";

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
  /**
   * F2：冲突登记那一刻本机记录的**内容身份依据**（无版本 / version 0 记录的唯一证明）。
   * 用户点「用服务器上的」时用它守卫删除：只有当前记录仍是当时那一条才允许删。
   */
  const conflictFingerprints = new Map<string, string | null>();

  const saveStatus = ref<SaveStatus>("idle");
  const lastSavedAt = ref<string | null>(null);
  const saveError = ref<string | null>(null);
  const dirty = ref(false);
  const lastOpLabel = ref("");

  const submissions = ref<SubmissionRecord[]>([]);
  const lastSubmission = ref<SubmissionResult | null>(null);
  /**
   * 阶段三（契约 §13.3）：聊天发送的互动绑定。
   *
   * 互动模式的每条聊天消息都带 mode=interactive + boardId；有当前提交（等待回答 /
   * 续作）时带 submissionId，服务端据此把这一轮绑定为**原提交的续作**，并从 waiting
   * 恢复原话题与已等待问题；普通聊天不经过这个 store，因此不带板面身份。
   */
  const chatBinding = computed(() => ({
    mode: "interactive" as const,
    boardId: boardId.value,
    submissionId: lastSubmission.value?.submission?.id ?? undefined,
  }));

  const submitStatus = ref<SubmitStatus>("idle");
  const submitError = ref<string | null>(null);
  /**
   * F10：提交请求的结果与**提交之后的读取**结果必须分开记录。
   *
   * - `submitResultUnknown`：POST 没有拿到回执（超时 / 断线 / 无响应）——
   *   这是「提交结果未知」，既不能说成功也不能说失败；
   * - `submitRefreshState/Error`：POST 已经明确成功、随后刷新板面 / 可见范围 / 任务清单失败。
   *   提交成功是既成事实，界面必须说「提交成功，页面刷新失败」，并只提供**重试刷新**
   *   （重试只触发读取，绝不自动重复提交）。
   */
  const submitResultUnknown = ref(false);
  const submitRefreshState = ref<"idle" | "pending" | "error">("idle");
  const submitRefreshError = ref<string | null>(null);
  /**
   * 契约 §1.3：这一次**未决**提交的稳定请求身份。
   *
   * 它在发出提交**之前**生成，并先落到本机台账（localStorage，按板面一条）：
   * 同一次未决提交重启后仍是**同一个身份**，服务端据此返回原事实（不重复创建
   * 提交与理解请求），同身份但内容不同则按冲突拒绝。
   */
  const pendingSubmission = ref<SubmissionIdentity | null>(null);
  /** 这次「已受理」是**按请求身份精确查证**确认的：本机没有完整回执，界面不得编造改动条数 */
  const submitAcceptedByLookup = ref(false);
  /** 查证结论的如实说明（没查到 / 查询失败时也写清「结果仍然未知」） */
  const acceptanceLookupNote = ref<string | null>(null);
  /** 正在按请求身份查证（按钮据此禁用，避免连点） */
  const acceptanceLookupPending = ref(false);
  /** 最近一次**精确查证**命中的受理事实（服务端事实；查不到时为 null） */
  const lastAcceptance = ref<AcceptanceLookup | null>(null);

  const visibleRange = ref<VisibleRange | null>(null);
  /**
   * 本地「本次作为互动材料的范围」（Phase 2 §5.1 唯一规则；R9 口径：不是文件访问权限）：与 participation 同源，
   * 供提交前预览 / 范围说明使用；权威结果仍以服务端 visible_range 为准。
   */
  const participationRange = computed<VisibleRange>(() => participationVisibleRange(board.value));

  const intents = ref<Intent[]>([]);
  const conflicts = ref<string[][]>([]);
  const batchAvailable = ref(false);
  const recoverNotice = ref<string[]>([]);
  /**
   * F11：任务清单的读取状态。读取失败**不是**「任务消失」：
   * 最后一次成功读到的任务、冲突与批次依据原样保留，界面明确说「刷新失败」并给重试入口。
   * `intentsLoadedOnce` 用来区分「成功返回空集合」与「从未成功读到过」。
   */
  const intentsLoadState = ref<"idle" | "loading" | "ok" | "error">("idle");
  const intentsLoadError = ref<string | null>(null);
  const intentsLoadedOnce = ref(false);

  /* ------------------------------------------------------------------
   * F02：新链意图视图（服务端事实）在本 store 里**唯一维护**
   *
   * 基线里这份数据由页面本地 ref 持有，只在 lastSubmission.submission.id 变化时刷新：
   * 理解延迟返回时新意图永远不会出现（必须重进页面）；刷新失败直接清空列表；
   * 两次读取乱序时旧结果会盖掉新结果。现在：
   * - 页面 / 虚拟意图层 / 任务面板 / 恢复事项都只读这一份（没有第二份列表驱动资格）；
   * - 读取带板面身份与代次：迟到的旧响应不落地；失败保留最后一次成功内容（F06）；
   * - 刷新复用既有 understanding / execution 定时器与事件，不新增第四套轮询。
   * ------------------------------------------------------------------ */
  /** 新链意图视图（display / turn 是 Phase 3 追加字段；缺省即没有，不猜）。 */
  type IntentViewFact = IntentView & {
    display?: { x?: unknown; y?: unknown } | null;
    turn_id?: string | null;
    turnId?: string | null;
  };
  const intentViews = ref<IntentViewFact[]>([]);
  const intentViewsLoadState = ref<"idle" | "loading" | "ok" | "error">("idle");
  const intentViewsLoadError = ref<string | null>(null);
  /** 是否**成功**读到过：用来区分「成功返回空集合」与「从来没读到过」 */
  const intentViewsLoadedOnce = ref(false);
  /** 服务端来源（service / legacy）：界面据此如实标注，不冒充新链能力 */
  const intentViewsSource = ref("");
  let intentViewsSeq = 0;
  let intentViewsAppliedSeq = 0;
  let intentViewsInFlight: Promise<boolean> | null = null;
  /** 在飞读取属于哪个板面：板面切走后旧读取不能挡住新板面的读取 */
  let intentViewsInFlightBoardId = "";
  /** 最近一次真正发起读取的时间（节流用：轮询路径不会把请求打成风暴） */
  let intentViewsLastStartedAt = 0;
  /** 任务面板入口（真实界面承接；不再只有内部开关） */
  const taskCenterFocus = ref<"tasks" | "pending">("tasks");

  /* ------------------------------------------------------------------
   * 真实理解与真实执行（契约 §14.1 / §14.6）
   *
   * 受理与理解分开：`submitStatus` 只说这次提交记录本身的结果；
   * 理解过程由 understanding / understandingStatus 表达，任务是否完成看 executions。
   * ------------------------------------------------------------------ */

  /** 最近一次理解过程（服务端事实；只有受理回执时可能还是 null） */
  const understanding = ref<Understanding | null>(null);
  /** 本板面的理解过程历史（含最近一次，最近排在最前） */
  const understandings = ref<Understanding[]>([]);
  /** 最近一次理解的状态：accepted / understanding / 三态结果 / failed */
  const understandingStatus = ref<UnderstandingStatus | null>(null);
  const understandingError = ref<string | null>(null);
  /** 正在查询理解状态（间隔 1s；页面不可见时停止） */
  const understandingPolling = ref(false);
  /** 自动查询停下但结果还没回来时的如实说明（不是错误） */
  const understandingNote = ref<string | null>(null);
  /** 受理回执给出的理解请求身份：还没拿到理解记录时也能据此查询 / 重试 */
  const acceptedUnderstandingId = ref<string | null>(null);
  /** 提交回执丢失但已查询确认受理时的如实说明 */
  const submitNote = ref<string | null>(null);

  /**
   * 上面两条说明是否仍属于**当前**阶段（任务书 §5.2 / §5.3）。
   *
   * 为什么要有这两个判据：说明是**写下它的那个阶段**的事实。受理说明说的是「正在理解」，
   * 理解一旦有结论（理解完成 / 本次无需任务 / 需要补充 / 理解失败），它就只是历史 ——
   * 继续与当前状态并列显示，就会出现「本次无需开展任务」下面还压着一句「正在理解」。
   * 判据只取服务端事实（本次理解是否结束），不引入第二套时钟；两个 ref 本身的值与既有
   * 语义保持不变，只是给消费者一条「这条说明是当前事实还是历史」的明确归属。
   */
  const submitNoteOwnsCurrentStage = computed(() => !understandingSettled(understandingStatus.value));
  const understandingNoteOwnsCurrentStage = computed(
    () => !understandingSettled(understandingStatus.value),
  );

  /** 本板面的真实执行记录（服务端事实，前端不自己拼进度） */
  const executions = ref<Execution[]>([]);
  const executionError = ref<string | null>(null);
  const executionPolling = ref(false);

  /** 除了最近一次之外的历史理解过程（可展开查看） */
  const understandingHistory = computed(() =>
    understandings.value.filter((item) => item.id !== understanding.value?.id),
  );

  /** 这次理解产出的意图（服务端事实；前端不自己建任务） */
  const understandingIntents = computed(() => {
    const id = understanding.value?.id ?? acceptedUnderstandingId.value;
    if (!id) return [];
    return intents.value.filter((item) => item.understandingId === id);
  });

  /** 每个意图的执行记录（按 attempt 排序，最后一次在末尾） */
  const executionsByIntent = computed<Record<string, Execution[]>>(() => {
    const map: Record<string, Execution[]> = {};
    for (const item of executions.value) {
      if (!item?.intentId) continue;
      if (!map[item.intentId]) map[item.intentId] = [];
      map[item.intentId].push(item);
    }
    for (const list of Object.values(map)) {
      list.sort(
        (a, b) =>
          (a.attempt ?? 0) - (b.attempt ?? 0) ||
          String(a.createdAt ?? "").localeCompare(String(b.createdAt ?? "")),
      );
    }
    return map;
  });

  /** 每个意图的最近一次执行状态（queued|running|paused|done|failed|cancelled） */
  const executionStatus = computed<Record<string, ExecutionStatus>>(() => {
    const map: Record<string, ExecutionStatus> = {};
    for (const [intentId, list] of Object.entries(executionsByIntent.value)) {
      const latest = list[list.length - 1];
      if (latest) map[intentId] = latest.status;
    }
    // 意图列表里带的最近执行记录也是服务端事实（列表刷新后仍然有效）
    for (const intent of intents.value) {
      if (!map[intent.id] && intent.execution) map[intent.id] = intent.execution.status;
    }
    return map;
  });

  /** 还在推进的执行（排队 / 运行）：决定要不要继续查询执行记录 */
  const liveExecutions = computed(() => executions.value.filter((item) => executionIsLive(item)));

  /** 四种状态分开的合成视图：受理 / 理解中 / 理解完成三态 / 提交本身失败 */
  const submissionStage = computed<SubmissionStage>(() => {
    if (submitStatus.value === "submitting") return "submitting";
    const state = understandingStatus.value;
    if (state === "accepted") return "accepted";
    if (state === "understanding") return "understanding";
    if (state === "understood") return "understood";
    if (state === "needs_clarification") return "needs_clarification";
    if (state === "no_action") return "no_action";
    if (state === "failed") return "understanding_failed";
    if (submitStatus.value === "succeeded") return "accepted";
    // F10：没有回执 = 结果未知，必须与 submitStatus 的取值一一对应（不许落回 idle）
    if (submitStatus.value === "unknown") return "unknown";
    if (submitStatus.value === "failed") return "submit_failed";
    if (submitStatus.value === "empty") return "empty";
    if (submitStatus.value === "duplicate") return "duplicate";
    return "idle";
  });

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
  /** 顶部「任务」浮层是否展开 */
  const tasksOpen = ref(false);
  /**
   * 展开预览的卡片 id（契约 2026-10-12 §3.5 / §3.6）：**只在前端内存**的查看状态。
   *
   * 展开、让位、收起都只是「这张卡这一帧画多大」：
   * 不写板面状态（board.cards 的 x/y/w/h 不动）、不产生表达（连 layout_only 都没有）、
   * 不触发模型理解 / 任务暂停、不排保存、不发请求。让位几何由
   * `interactive/cardLayout.ts` 的纯函数算，这里只保存「哪几张卡在展开」。
   */
  const expandedCards = ref<Set<string>>(new Set());

  /**
   * 这张卡是不是「附件是图片的文件卡」——判定只有一份口径：
   * materials.cardAttachmentIsImage（内部复用 isPreviewableImageName），不按 kind 判
   * （类型里已经没有 kind="image"）。
   *
   * 卡片不在板面上时返回 false：既有行为不变（不认识的 id 仍由调用方自己决定）。
   */
  function cardIsImageMaterial(cardId: string): boolean {
    const card = board.value?.cards.find((item) => item.id === cardId);
    if (!card) return false;
    return cardAttachmentIsImage(card.meta, card.kind);
  }

  /** 展开列表里真正可阅读的 id（图片一律不算，BoardCanvas 的临时布局也读这个出口）。 */
  function readableExpandedIds(): string[] {
    return [...expandedCards.value].filter((id) => !cardIsImageMaterial(id));
  }

  /**
   * 展开 / 收起这张卡的预览主体；返回切换后的状态（true = 现在展开着）。
   *
   * 图片卡没有卡片内阅读态：**不加入**展开列表，也如实返回 false（不假成功）。
   */
  function toggleCardPreview(cardId: string): boolean {
    if (!cardId) return false;
    if (cardIsImageMaterial(cardId)) {
      if (expandedCards.value.has(cardId)) {
        const cleaned = new Set(expandedCards.value);
        cleaned.delete(cardId);
        expandedCards.value = cleaned;
      }
      return false;
    }
    const next = new Set(expandedCards.value);
    const opened = !next.has(cardId);
    if (opened) next.add(cardId);
    else next.delete(cardId);
    expandedCards.value = next;
    return opened;
  }

  /** 收起所有展开的预览；返回这一次真的收起了几张（图片不算数）。 */
  function collapseAllPreviews(): number {
    const readable = readableExpandedIds().length;
    if (expandedCards.value.size) expandedCards.value = new Set();
    return readable;
  }

  /** 这张卡现在是否展开着预览（模板里读它才会跟着 Set 变化重渲染）；图片永远为 false。 */
  function isCardPreviewExpanded(cardId: string): boolean {
    if (!expandedCards.value.has(cardId)) return false;
    return !cardIsImageMaterial(cardId);
  }

  /**
   * 保存一张卡片的**阅读位置**（页码 / 幻灯片 / 工作表 / 行 / 缩放）。
   *
   * 三条边界：
   * 1. 位置按「附件 + 文件版本」归属，服务端在 attachmentId 与卡片不一致时回 409；
   * 2. 写它**不是**板面改动：不产生表达、不改板面版本、不排队保存、不触发模型（契约 §3.5 / §3.6）；
   * 3. 失败不打断阅读：只把原因写进 previewPositionError，界面据此如实显示（没有假成功）。
   */
  const previewPositionError = ref<string | null>(null);

  async function savePreviewPosition(
    cardId: string,
    item: { attachmentId: string; version: string; position: PreviewPosition },
  ): Promise<boolean> {
    if (!cardId || !item?.attachmentId || !item.version) return false;
    try {
      await putBoardPreviewPosition(boardId.value, cardId, item);
      previewPositionError.value = null;
      return true;
    } catch (err) {
      previewPositionError.value = err instanceof Error ? err.message : String(err);
      return false;
    }
  }

  /** 展开顺序 = 让位处理顺序：直接喂给 planExpandedLayout 的 expanded（图片不算）。 */
  const expandedCardIds = computed<string[]>(() => readableExpandedIds());
  /** 展开中的预览数量（界面据此显示「收起全部预览」；图片不算）。 */
  const previewExpandedCount = computed<number>(() => readableExpandedIds().length);

  /**
   * 板面变化后重新判定类型：**只移除**「已经变成图片卡」的 id，
   * 不关闭其它文件正在进行的阅读（PDF 换成图片必须退出原有展开态；
   * 图片换成 PDF 之后可以重新展开并恢复阅读）。
   *
   * flush: "sync" —— 与板面写入同步完成，界面不会有一帧把图片画成阅读态。
   */
  watch(
    board,
    () => {
      if (!expandedCards.value.size) return;
      const next = new Set<string>();
      let changed = false;
      for (const id of expandedCards.value) {
        if (cardIsImageMaterial(id)) {
          changed = true;
          continue;
        }
        next.add(id);
      }
      if (changed) expandedCards.value = next;
    },
    { flush: "sync" },
  );
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
    /**
     * R5：兜底/重新说明时给用户看的一句补充（例如「重新核对后范围与刚才不同」）。
     * 正常的保存前预判不带它，界面按原样展示受影响任务。
     */
    note?: string;
  } | null>(null);
  /** 保存后服务端回报「因为这些改动被暂停的任务」 */
  const materialPaused = ref<Intent[]>([]);
  /** 最近一次**真的落库**的保存所暂停的任务（N2：只有它能支撑「任务已暂停」的表述） */
  const lastSavePaused = ref<Intent[]>([]);
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
   * R2：正式板面读取的代次记账。
   * - `boardReadSeq`：每发起一次 GET 就 +1（请求发出时刻的读取代次）；
   * - `boardReadAppliedSeq`：已经真正落地过的最大读取代次。
   * 两次 GET 乱序返回时，只有代次更新（且期间板面没有被推进）的那次算数。
   */
  let boardReadSeq = 0;
  let boardReadAppliedSeq = 0;
  /**
   * 用户已确认的影响检查句柄（M4）：
   * - `pendingConfirm` 在用户点「确认」后设置，随这一次保存的 PUT 一起提交；
   * - `confirmedCheck` 记录「哪一次确认授权了哪个已保存版本」，只有板面自那以后没有新改动时
   *   才允许在提交时把它作为 confirmedCheckId 带上（避免拿过期授权去提交）。
   */
  let pendingConfirm: { checkId: string; stateVersion?: number } | null = null;
  let confirmedCheck: { checkId: string; savedSeq: number } | null = null;

  /**
   * 本轮（F1/F3/N5）的**版本事实基线**：最近一次「与服务器一致」的正式板面快照。
   *
   * 为什么需要它：取消撤回、跨页面合并、以及「用户到底改过哪些字段」的判定，
   * 都要有一个「用户改动之前是什么样」的参照。没有它就只能整块覆盖或整块拒绝，
   * 也就是本轮的 F1（取消后新操作仍带着被取消的正文）与 F3（候选永远带旧 seq）。
   */
  let cleanState: BoardState | null = null;

  /**
   * 保存生命周期的代次（N1）：一次保存 = 影响检查 → 写入 → 回执。
   * 任何 await 之后只要发现已经有更新的保存开始（或候选已经不是当初那一版），
   * 这一次就**不许再发出旧候选**。
   */
  let saveToken = 0;

  /**
   * 已知的服务器已保存版本（只增不减）：判据是「这次读到的比已知的新吗」。
   * 迟到的读取（比已知版本旧）绝不许用来回退板面事实（F3/R2 共用）。
   */
  let knownServerSeq = 0;

  /**
   * F01/C1：草稿通道的服务端存簿版本（rev）与逐键版本。
   *
   * 客户端据此在保存时证明「这次更新 / 删除基于哪一版」；
   * 服务端回执里的 rev 是**只增不减**的存簿事实，迟到的回执不许把它退回去。
   */
  let serverDraftRev: number | null = null;
  const serverDraftKeyVersions = new Map<string, number>();

  /**
   * N1/F3：同一次候选的「旧版本写入被拒 → 读最新事实 → 重试」次数上限。
   * 服务端版本一直在被别人推进时，绝不做无界重试：停在明确失败上，让用户看见原因。
   */
  let saveRecoveryRev = -1;
  let saveRecoveryAttempts = 0;

  /**
   * 「勾选」是按对象记录的修改代次（N5）：提交成功回报 checkedCleared 时，
   * 只有**用户没有在等待期间自己重新勾选**的那一条才能被这次清理取消。
   */
  let checkedMutationSeq = 0;
  const cardCheckedSeq = new Map<string, number>();
  /**
   * N5：用户自己做出的、**还没被服务器事实确认**的勾选选择。
   * 服务器上的旧回执/迟到的读取不许取消它；只有当服务器事实与它一致
   * （保存成功、或提交回执与读取都能证明服务器已经这么记）时才清掉这条记录。
   */
  const localCheckedChoices = new Map<string, boolean>();
  /**
   * Phase 2 §5.1：参与状态是唯一事实源。用户做过的参与选择（还没被服务器事实确认）在这里留一份：
   * 旧后端的「提交成功清勾选」只回 checked=false，不能因此把参与状态也清掉
   * —— 界面显示的范围与服务端后续保存必须仍是用户选的那一份。
   */
  const localParticipationChoices = new Map<string, Participation>();
  /**
   * 本页**自己**最后成功写下的本机记录版本（每个草稿键）。
   * 用于在重试补写本机副本时判断「磁盘上的是不是另一个页面写下的更新记录」——
   * 能证明是别人的更新版本就保留它，绝不覆盖（F2 的同类保护，针对写入而不是删除）。
   */
  const draftLocalWrittenVersion = new Map<string, number>();

  /**
   * 正式的**内容冲突**（F3）：本页候选与服务器上同一张卡片的正文都被改过。
   * 必须由用户明确选择保留哪一份，绝不静默覆盖（界面入口见 resolveBoardContentConflict）。
   */
  const boardContentConflicts = ref<Record<string, { local: string; server: string }>>({});

  /**
   * F07：本地删除（解散组 / 删除关联）与服务器对**同一对象**的实质修改相撞。
   *
   * 键是对象 id；值里保留服务器上那一份的完整依据。未解决之前不许保存
   * （保存会把删除写上去 = 丢掉对方的修改；直接复活 = 静默撤销用户的删除），
   * 由用户明确选择「保持删除」或「恢复服务器版」。
   */
  const boardRemovalConflicts = ref<
    Record<string, { kind: "group" | "link"; id: string; server: Record<string, unknown> }>
  >({});

  /**
   * 取消撤回的恢复状态（F1）：active=true 表示「服务器事实还没落地确认这次撤回」。
   * 界面据此**不许**宣称「已撤回」；reason 是回读失败的真实原因，可重试。
   */
  const cancelRecovery = ref<{ boardId: string; active: boolean; reason: string | null } | null>(null);

  /**
   * 被用户关闭（取消/再次查看之前的暂缓）的「剩余撤回决定」提示（N3）。
   * 键是 intentId：同一会话内不因任务列表更新反复打断；页面刷新后 store 重建，
   * 服务端若仍有 pendingDecision 会重新提醒（那时它仍然是真实未处理的决定）。
   */
  const revertDecisionDismissed = ref<Record<string, true>>({});
  const revertDecisionStates = ref<Record<string, { status: "idle" | "pending" | "error"; error: string | null }>>(
    {},
  );

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

  /* ------------------------------------------------------------------
   * F03：任务行 / 批量对象 = 唯一一份（由新链视图 + 旧链清单 + 执行记录合成）
   *
   * 界面显示、数量、暂停 / 继续 / 取消 / 批准 / 拒绝的资格都读这一份，
   * 不会出现「两份互不更新的列表分别驱动界面和操作资格」。
   * ------------------------------------------------------------------ */
  const taskRows = computed<TaskRow[]>(() =>
    buildTaskRows({
      views: intentViews.value.map((view) => ({
        intentId: String(view.intent_id ?? ""),
        title: String(view.title ?? ""),
        status: String(view.status ?? ""),
        version: Number(view.version ?? 0),
        legacyStatus:
          typeof view.raw_legacy_status === "string" && view.raw_legacy_status
            ? view.raw_legacy_status
            : null,
        authorizationValid:
          typeof view.authorization_valid === "boolean" ? view.authorization_valid : null,
      })),
      legacy: intents.value.map((intent) => ({
        intentId: String(intent.id ?? ""),
        title: String(intent.title ?? ""),
        status: String(intent.status ?? ""),
        version: Number((intent as { version?: number }).version ?? 0),
      })),
      executionStatus: executionStatus.value,
    }),
  );
  const taskCountsValue = computed(() => taskCounts(taskRows.value));
  const runningTaskCount = computed(() => taskCountsValue.value.live);
  const pendingTaskCount = computed(() => taskCountsValue.value.waiting);
  const recoverableIntentCount = computed(
    () => taskRows.value.filter((row) => row.recoverable).length,
  );
  /** 还有需要跟踪的任务（执行中 / 已暂停）：页面据此提示「任务仍在推进」。 */
  const hasActiveTasks = computed(() =>
    taskRows.value.some((row) => row.bucket === "live" || row.bucket === "paused"),
  );
  const exhaustedRetryIntentCount = computed(
    () =>
      intentViews.value.filter((view) => {
        const retry = (view as { retry?: { status?: string } | null }).retry;
        return retry?.status === "exhausted";
      }).length,
  );

  /**
   * F05：恢复事项的**唯一摘要**（页面入口与面板内容同源）。
   *
   * 覆盖：影响确认等待、板面保存失败、草稿保存失败、提交后刷新失败、提交结果未知、
   * 正文 / 删除冲突、草稿冲突、可恢复意图、自动重试用尽。
   * 同一事项按稳定身份去重；读取失败 / 尚未读取由 recoveryEntry 单独表达，
   * 绝不用空数组冒充「没有事项」（那是「没读到」）。
   */
  const recoveryItems = computed<RecoveryItem[]>(() =>
    buildRecoveryItems({
      impactPending: Boolean(pendingImpact.value),
      saveFailed: saveStatus.value === "error",
      saveError: saveError.value,
      draftSaveFailed: draftSaveStatus.value === "error",
      draftError: draftSaveError.value,
      submitRefreshFailed: submitRefreshState.value === "error",
      submitRefreshError: submitRefreshError.value,
      submitUnknown: Boolean(submitResultUnknown.value),
      submitNote: submitNote.value,
      contentConflictCardIds: Object.keys(boardContentConflicts.value),
      removalConflictIds: Object.keys(boardRemovalConflicts.value),
      draftConflictKeys: Object.keys(draftConflicts.value),
      recoverableIntents: intentViews.value
        .filter((view) => canRecover(view as IntentView))
        .map((view) => ({ intentId: String(view.intent_id ?? ""), title: String(view.title ?? "") })),
      exhaustedRetryIntentIds: intentViews.value
        .filter((view) => (view as { retry?: { status?: string } | null }).retry?.status === "exhausted")
        .map((view) => String(view.intent_id ?? "")),
    }),
  );
  /**
   * 恢复入口状态：数量 + 读取状态。
   *
   * - error：意图读取失败 —— 入口仍要出现并说明「可能还有未列出的事项」，不能显示成「没有事项」；
   * - pending：还没成功读到过 —— 同样不能说没有；
   * - count：真实事项数（处理后立刻减少，不保留历史最大数量）。
   */
  const recoveryEntry = computed(() => {
    const readFailed = intentViewsLoadState.value === "error";
    return {
      count: recoveryItems.value.length,
      readFailed,
      readError: readFailed ? intentViewsLoadError.value ?? "原因未知" : null,
      pending: !intentViewsLoadedOnce.value && intentViewsLoadState.value !== "error",
      visible:
        recoveryItems.value.length > 0 ||
        readFailed ||
        (!intentViewsLoadedOnce.value && intentViewsLoadState.value === "loading"),
    };
  });
  /** 可恢复意图数量（入口 / 面板共用；读取失败时如实为「未知」而不是 0） */
  const recoveryReadState = computed<"ok" | "loading" | "error" | "idle">(() => {
    if (intentViewsLoadState.value === "error") return "error";
    if (intentViewsLoadState.value === "loading") return "loading";
    if (intentViewsLoadState.value === "ok") return "ok";
    return "idle";
  });

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
  /**
   * R4（本轮修复）：待处理的本机记录操作必须保留**用户决定的真实目的**，不能只留版本。
   * - `remove-local-copy`：本机冗余副本删除（用户选了「用服务器上的」，或服务器已保存成功后的本机清理）；
   *   重试只删这份记录，**绝不写 cleared 依据**；
   * - `clear-draft`：整份草稿清除（用户要清掉这份草稿）；重试先幂等补写 cleared 依据（12 的既有保护）。
   *
   * 基线把两者混为一谈：选「用服务器上的」删除失败后重试写下 cleared，重开后用户选择保留的
   * 服务器稿被当成本次要清除的旧稿删掉 —— 这正是 R4。
   */
  type LocalRemovalPurpose = "remove-local-copy" | "clear-draft";
  /**
   * 重试时的版本守卫口径（A 的 drafts.ts 同口径）：
   * - `version`：只删那个确切版本；
   * - `absent`：登记时本来就没有记录 → 现在仍然没有才算无事可做（flushDrafts 成功清理那一条）；
   * - `object`：旧格式记录没有版本可校验，按对象删。
   * 没有它，null 期望版本会被当成「按对象删」，可能误删请求期间别的页面新建的记录。
   */
  type LocalRemovalGuard = "version" | "absent" | "fingerprint" | "object";
  const pendingLocalRemovals = new Map<
    string,
    {
      purpose: LocalRemovalPurpose;
      expectVersion: number | null;
      guard: LocalRemovalGuard;
      /**
       * F2：无版本（旧格式 / version 0）记录的**可验证身份依据**。
       * 重试时只有当前记录仍与它一致才允许删除；不一致 → 保留新稿（version-guard）。
       */
      expectFingerprint?: string | null;
    }
  >();
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
  /** N5：记录「哪些卡片的勾选被用户自己动过」——提交成功的清理只作用于没被重新选择的对象。 */
  function trackCheckedChanges(previous: BoardState | null, next: BoardState): void {
    if (!previous) return;
    const before = new Map(previous.cards.map((card) => [card.id, card.checked]));
    for (const card of next.cards) {
      if (!before.has(card.id)) continue;
      if (before.get(card.id) !== card.checked) {
        cardCheckedSeq.set(card.id, ++checkedMutationSeq);
        localCheckedChoices.set(card.id, card.checked);
      }
    }
  }

  /** 服务器事实已经包含某个勾选选择时，就不再算「本地未被确认的选择」。 */
  function reconcileCheckedChoices(server: BoardState): void {
    for (const [cardId, choice] of [...localCheckedChoices]) {
      const card = (server.cards ?? []).find((item) => item.id === cardId);
      if (card && card.checked === choice) localCheckedChoices.delete(cardId);
    }
  }

  /**
   * Phase 2 §5.1：参与选择的三件套（与勾选同一套「用户选择优先、服务器确认后释放」口径）。
   * - track：用户这一版改了参与 → 记下来；
   * - force：把还没被服务器确认的选择重新贴回候选（迟到的旧事实不许把它改回去）；
   * - reconcile：服务器事实与选择一致 → 这条本地选择已经落地，可以释放。
   */
  function trackParticipationChanges(previous: BoardState | null, next: BoardState): void {
    if (!previous) return;
    const before = new Map(previous.cards.map((card) => [card.id, participationOf(card)]));
    for (const card of next.cards) {
      const was = before.get(card.id);
      if (was === undefined) continue;
      const now = participationOf(card);
      if (was !== now) localParticipationChoices.set(card.id, now);
    }
  }

  function forceParticipationChoices(state: BoardState): BoardState {
    if (!localParticipationChoices.size) return state;
    let changed = false;
    const cards = state.cards.map((card) => {
      const choice = localParticipationChoices.get(card.id);
      if (!choice || participationOf(card) === choice) return card;
      /*
        hidden / 遗留不可交付卡在参与判定里优先级更高（与后端一致）：
        本地选择不能把明确隐藏的内容拉回范围。
      */
      if (card.hidden) return card;
      const kind = card.kind;
      if (kind === "code" || kind === "url") return card;
      if (kind === "text" && card.meta && (typeof card.meta.language === "string" || (card.meta.url && typeof card.meta.url === "object"))) {
        return card;
      }
      changed = true;
      const next: typeof card = { ...card, participation: choice };
      if (choice === "included") next.hidden = false;
      syncLegacyParticipation(next);
      return next;
    });
    return changed ? { ...state, cards } : state;
  }

  function reconcileParticipationChoices(server: BoardState): void {
    for (const [cardId, choice] of [...localParticipationChoices]) {
      const card = (server.cards ?? []).find((item) => item.id === cardId);
      if (card && participationOf(card) === choice) localParticipationChoices.delete(cardId);
    }
  }

  /**
   * Phase 2 §5.4：服务器接受的这一版带着权威 version（后端权威自增）。
   * 被更新候选接手的保存**仍要吸收**这些版本号 —— 否则候选下次保存会拿 1 去覆盖
   * 服务端已经推进的版本（CAS 退化），这正是本轮要修的问题之一。
   */
  function absorbAuthoritativeVersions(candidate: BoardState, server: BoardState): BoardState {
    const mergeList = <T extends { id: string; version?: number }>(
      local: T[] | undefined,
      remote: T[] | undefined,
    ): T[] => {
      const byId = new Map((remote ?? []).map((item) => [item.id, item]));
      return (local ?? []).map((item) => {
        const source = byId.get(item.id);
        if (!source || typeof source.version !== "number" || !Number.isFinite(source.version)) {
          return item;
        }
        if (item.version === source.version) return item;
        return { ...item, version: source.version };
      });
    };
    const cards = mergeList(candidate.cards, server.cards);
    const groups = mergeList(candidate.groups, server.groups);
    const links = mergeList(candidate.links, server.links);
    const serverParticipationVersion = Number(server.participationVersion);
    const participationVersion =
      Number.isFinite(serverParticipationVersion) && serverParticipationVersion > 0
        ? Math.max(serverParticipationVersion, Number(candidate.participationVersion) || 0)
        : candidate.participationVersion;
    return { ...candidate, cards, groups, links, participationVersion };
  }

  /**
   * N6：这条本机记录里是否带着**只在本机存在的**附加字段快照。
   *
   * 附加字段（网址/标题/文件名/图片名/代码语言）不进服务器草稿通道（那份只放正文），
   * 所以「服务器已经有这份正文」**不能**证明这条记录是冗余副本 ——
   * 删掉它，刷新/关闭重开后这些字段就没有任何来源，只能悄悄回退成正式卡片的旧值。
   * 带附加字段的记录一律保留，等用户「完成编辑」（清除登记）或明确选「用服务器上的」时再清理。
   */
  function recordCarriesLocalMeta(record: DraftRecord | null): boolean {
    return Boolean(record && record.meta && Object.keys(record.meta).length > 0);
  }

  /**
   * 丢弃「与这份事实不一致」的本地勾选选择记录：
   * 候选已经被整体撤回/被服务器整块替换时，那些选择不再是当前板面的一部分，
   * 留着只会在下一次合并时把早已作废的勾选又「强制」回来。
   */
  function dropCheckedChoicesNotIn(state: BoardState): void {
    for (const [cardId, choice] of [...localCheckedChoices]) {
      const card = (state.cards ?? []).find((item) => item.id === cardId);
      if (!card || card.checked !== choice) localCheckedChoices.delete(cardId);
    }
  }

  /** 用户重新编辑了正文：这张卡片的内容冲突由这次编辑解决（不再拦住保存）。 */
  function settleContentConflicts(previous: BoardState | null, next: BoardState): void {
    if (!previous || !Object.keys(boardContentConflicts.value).length) return;
    const before = new Map(previous.cards.map((card) => [card.id, card.content]));
    const edited = (next.cards ?? []).filter((card) => before.has(card.id) && before.get(card.id) !== card.content);
    if (!edited.length) return;
    const remaining = { ...boardContentConflicts.value };
    for (const card of edited) delete remaining[card.id];
    boardContentConflicts.value = remaining;
  }

  function commit(next: BoardState, label: string) {
    const current = board.value;
    // Phase 2 §5.1：参与的旧兼容字段（checked / hidden）与 participation 同口径；
    // 对齐不补时间戳（否则等于凭空造出一次改动）。
    const aligned = applyParticipation({ ...next, boardId: boardId.value });
    const alignedCurrent = current ? applyParticipation(current) : null;
    trackCheckedChanges(alignedCurrent, aligned);
    trackParticipationChanges(alignedCurrent, aligned);
    settleContentConflicts(alignedCurrent, aligned);
    if (current) pushUndo(current);
    board.value = aligned;
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

  /** 受影响任务集合的身份（R5/R6）：用来判断「补取预判后的范围与刚才的说明是否不同」。 */
  function affectedSignature(
    items: { intentId?: string }[] | undefined,
  ): string {
    return (items ?? [])
      .map((item) => String(item?.intentId ?? ""))
      .filter((id) => id.length > 0)
      .sort()
      .join("|");
  }

  /**
   * 把一次影响预判落到「等用户决定」的候选说明上（R5 兜底分支与确认过期后的重新说明共用）。
   *
   * 只有拿到服务端 checkId 才算一次**有效**的说明：没有 checkId 的说明，用户确认后必然
   * 被服务端再拒一次（基线的真实缺陷：兜底分支拿不到 checkId，确认后重复弹同一说明）。
   * 返回 false 表示这次没拿到有效说明，调用方要如实说明原因并保留候选。
   *
   * 传了 previousAffected 时，如果新范围与刚才展示的不同，必须**先展示变化**、
   * 由用户按新范围重新确认，不能在用户只确认旧说明时就自动批准新增后果（R5/R6）。
   */
  function openImpactConfirm(
    check: import("../services/interactive").ImpactCheckResult,
    putRev: number,
    fallbackStateVersion: number,
    previousAffected?: { intentId: string; title: string; materials: string[]; consequence: string }[],
    /** 明确要写给用户看的说明（例如服务端说「真实范围变了」） */
    forceNote?: string,
  ): boolean {
    if (check.ok === false || !check.checkId) return false;
    const affected = (check.affected ?? check.affectedTasks ?? []).map((item) => ({
      intentId: String(item.intentId),
      title: String(item.title ?? ""),
      materials: (item.materials ?? []).map((name) => String(name)),
      consequence: String(item.consequence ?? ""),
    }));
    /**
     * 服务端门与补取的影响预判都是服务端自己算出来的；两者若不一致，
     * 显示它们的并集（至少有一份服务端判断说有影响，就不能少报给用户看）。
     */
    for (const item of previousAffected ?? []) {
      if (!affected.some((entry) => entry.intentId === item.intentId)) affected.push(item);
    }
    const scopeChanged = previousAffected
      ? affectedSignature(previousAffected) !== affectedSignature(affected)
      : false;
    pendingImpact.value = {
      affected,
      previewRev: putRev,
      stateVersion: check.stateVersion ?? fallbackStateVersion,
      checkId: check.checkId,
      note:
        forceNote ??
        (previousAffected
          ? scopeChanged
            ? "重新核对后发现受影响的任务与刚才的说明不同：下面是当前候选的真实影响范围，请按它重新确认。"
            : "影响范围已经重新核对：下面是当前候选的真实影响，确认后才会保存生效。"
          : undefined),
    };
    // N2：一次**有效**的说明已经拿到，上一次「预判没做成」的原因不再是当前事实
    impactCheckError.value = null;
    return true;
  }

  /**
   * F3 的可恢复流程第一步：读取最新服务器事实并安全合并进候选。
   *
   * 返回 null 表示成功（版本事实已接收）；否则返回一句给用户看的真实原因。
   * 注意这里**保留用户自己的候选**（合并口径见 serverFacts.ts），只协调服务器上的独立变化。
   */
  async function recoverCandidateFromServer(reason: string): Promise<string | null> {
    try {
      const applied = await refreshBoardFromServer();
      if (!applied) return `${reason}；但没有取到当前板面的最新事实（板面已经切换）`;
      return null;
    } catch (err) {
      return `${reason}；读取最新板面事实也失败了（${(err as Error).message || "原因未知"}）`;
    }
  }

  /**
   * F3 的可恢复流程第二步：用**当前候选的最新版本**重新做一次影响预判。
   *
   * 返回：
   * - "confirm"：已经拿到有效说明并打开确认框，等用户决定；
   * - "ok"：这次改动不需要影响确认，可以继续保存；
   * - "blocked"：预判没做成（原因已写入 impactCheckError），候选保留。
   */
  async function recheckImpactForCurrentCandidate(
    previousAffected?: { intentId: string; title: string; materials: string[]; consequence: string }[],
    forceNote?: string,
  ): Promise<"confirm" | "ok" | "blocked"> {
    if (!board.value) return "blocked";
    if (Object.keys(boardContentConflicts.value).length > 0) {
      const ids = Object.keys(boardContentConflicts.value).join("、");
      impactCheckError.value = null;
      saveStatus.value = "error";
      saveError.value = `本页候选与服务器上同一张卡片的正文都有改动（${ids}），请选择保留哪一份后再保存`;
      return "blocked";
    }
    // F07：本地删除与服务器修改相撞时先等用户明确选择（与 saveNow 的门同一口径）
    if (Object.keys(boardRemovalConflicts.value).length > 0) {
      const ids = Object.keys(boardRemovalConflicts.value).join("、");
      impactCheckError.value = null;
      saveStatus.value = "error";
      saveError.value = `本地删除了这些对象，但服务器上它们被改过（${ids}）：请选择保持删除还是恢复服务器版后再保存`;
      return "blocked";
    }
    const current = cloneState(board.value);
    const rev = boardLocalRev;
    try {
      const check = await runImpactCheck(current.seq, current);
      if (openImpactConfirm(check, rev, current.seq, previousAffected, forceNote)) return "confirm";
      if (check.ok === false) {
        impactCheckError.value = `这次保存前的影响预判没有完成，保存已暂停（${check.reason || "服务端没有给出可用的影响确认记录"}）`;
        return "blocked";
      }
      return "ok";
    } catch (err) {
      impactCheckError.value = `这次保存前的影响预判没有完成，保存已暂停（${(err as Error).message || "原因未知"}）`;
      return "blocked";
    }
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
      // 等旧请求结束后要看一眼：这期间用户又改了（dirty 仍为真）就必须**再存一次**，
      // 不能拿到结果就返回，把更新的候选永远忘在内存里（相邻于 N1 的真实遗漏）。
      await saveInFlight;
      if (dirty.value && !pendingImpact.value) scheduleSave();
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
    /**
     * N1：这次保存的生命周期代次。影响检查、写入、回执**整段**都要用它判断
     * 「是不是已经有更新的保存接手了」—— 只要发现被取代，就绝不把旧候选发出去。
     */
    const token = ++saveToken;
    const superseded = () => token !== saveToken || boardLocalRev !== putRev;
    /** 回执之后是否还要再存一次（服务端要求用最新版本重来）。 */
    let resumeSave = false;
    /**
     * F3：正文内容冲突必须先由用户决定（本页候选与服务器同一张卡片都被改过），
     * 不许静默覆盖服务器上的那一份，也不许把本页候选悄悄丢掉。
     */
    if (Object.keys(boardContentConflicts.value).length > 0) {
      const ids = Object.keys(boardContentConflicts.value).join("、");
      saveStatus.value = "error";
      saveError.value = `本页候选与服务器上同一张卡片的正文都有改动（${ids}），请选择保留哪一份后再保存`;
      dirty.value = true;
      return;
    }
    /**
     * F07：本地删除的对象在服务器上被改过 —— 必须先由用户明确选择
     * （保持删除 / 恢复服务器版），否则保存会把对方的修改丢掉、或者本地删除被静默复活。
     */
    if (Object.keys(boardRemovalConflicts.value).length > 0) {
      const ids = Object.keys(boardRemovalConflicts.value).join("、");
      saveStatus.value = "error";
      saveError.value = `本地删除了这些对象，但服务器上它们被改过（${ids}）：请选择保持删除还是恢复服务器版后再保存`;
      dirty.value = true;
      return;
    }
    // 保存前先问服务端一句：这次改动会不会碰到正在执行任务依赖的材料？
    // 会 → 不保存，把影响说明交给用户决定（继续=保存并暂停相关任务；取消=不改动，任务继续）。
    if (!impactConfirmed && activeIntents.value.length > 0) {
      try {
        const check = await runImpactCheck(snapshot.seq, snapshot);
        // N1：预判在飞期间用户完成了更新的一版（更晚的保存已经接手）——
        // 这一次的旧候选**不许再发出去**，否则旧写入会把已保存的新版覆盖掉。
        if (superseded()) {
          if (dirty.value && !pendingImpact.value) scheduleSave();
          return;
        }
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
          //
          // R6：确认框展示的是**服务端给出的完整受影响范围**，而不是「客户端自己知道在运行的那几个」。
          // 客户端的任务清单可能滞后（刚被批准并运行的任务它还看不到），按交集展示会让用户在
          // 不知情的情况下确认一份更小的范围。是否需要确认仍以服务端 impactConfirmationRequired 为准
          // （已暂停任务只作为说明展示，不额外拦住保存）。
          pendingImpact.value = {
            affected: affected.length ? affected : blocking,
            previewRev: putRev,
            stateVersion: check.stateVersion ?? snapshot.seq,
            checkId: check.checkId,
          };
          // N2：新的说明已经取到，上一次「预判没做成」的原因不再是当前事实
          impactCheckError.value = null;
          saveStatus.value = "idle";
          return;
        }
      } catch (err) {
        // N1：这次预判已经被更新的保存取代：它的失败不属于当前候选，不许据此报错/停住
        if (superseded()) {
          if (dirty.value && !pendingImpact.value) scheduleSave();
          return;
        }
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
        /**
         * F08：服务器**已经确认**的这一版是独立的版本事实，与「用户正在编辑哪个候选」无关，
         * 必须一直单调推进。基线只在「没有更新候选」时才记它：被更新候选接手的那次保存
         * 服务端其实已经接受了（seq 前进），已知服务器版本却停在旧值上，于是更旧的 GET
         * 会被当成「更新的读取」，把板面退回旧版本并造出针对旧正文的假冲突。
         */
        const acceptedServerSeq = Number(result.seq);
        if (Number.isFinite(acceptedServerSeq)) {
          knownServerSeq = Math.max(knownServerSeq, acceptedServerSeq);
        }
        if (newerCandidate || boardReadAdvanced) {
          // 回执在飞期间出现了更新的候选（用户继续编辑）/ 或一次更新的读取已经落地：
          // 服务器已接受的是旧版本，板面（新候选）不被这次旧回执覆盖。
          /**
           * R3（本轮修复）：必须把两件事**分开接收** ——
           * 「服务器已经接受了这一版」是一个已经发生的版本事实；「用户正在编辑的是更新的候选」
           * 是另一件事。基线把两者绑在一起：既不把服务器接受的新 seq 记到候选上，也不推进
           * 「与服务器一致」的版本，于是候选仍带旧 seq，下一轮保存被服务端预判判为过期版本、
           * 一般回读又被候选保护阻断 —— 第二版再也存不下去。
           * 这里只吸收版本事实（seq / updatedAt），绝不整块替换用户正在写的候选正文。
           */
          const cleanUnchanged = boardCleanRev === putCleanRev;
          if (newerCandidate && cleanUnchanged && board.value) {
            boardCleanRev = putRev;
            if (Number.isFinite(acceptedServerSeq)) {
              /*
                Phase 2 §5.4：服务器接受的这一版带着权威 version。候选被更新的编辑接手时，
                旧回执**不覆盖正文**，但必须吸收这些版本号 —— 否则下一次保存会拿过期 version
                去写，CAS 退化（后端权威自增的版本被前端旧值覆盖）。
              */
              const aligned = applyParticipation({
                ...board.value,
                seq: acceptedServerSeq,
                updatedAt: result.savedAt ?? board.value.updatedAt,
              });
              board.value = result.state
                ? absorbAuthoritativeVersions(aligned, applyParticipation(result.state))
                : aligned;
            } else if (result.state) {
              board.value = absorbAuthoritativeVersions(
                board.value,
                applyParticipation(result.state),
              );
            }
            // 被接受的这一版就是新的「与服务器一致」事实（F1 的撤回基线随之前进）
            if (result.state) cleanState = cloneState(applyParticipation(result.state));
          }
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
        // 服务器接受了这次写入：这是**已保存事实**（F1 的撤回也由它确认）
        settleCancelRecovery(true, null);
        saveRecoveryRev = putRev;
        saveRecoveryAttempts = 0;
        /*
          Phase 2：保存回执同样是服务器事实 —— 对齐参与口径，并把还没被服务器确认的
          用户参与选择保留下来（旧后端的回执里没有 participation 时不能把它抹掉）。
        */
        const savedState = applyParticipation(clampBoardBounds(result.state));
        reconcileParticipationChoices(savedState);
        board.value = forceParticipationChoices(savedState);
        cleanState = cloneState(board.value);
        reconcileCheckedChoices(board.value);
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
        // N2：这一次保存真实撤回/暂停了哪些任务，以服务端回执为准（客户端不猜）
        lastSavePaused.value = impact?.paused ?? [];
        if (impact?.paused?.length) {
          materialPaused.value = impact.paused;
          void loadIntents();
        }
        // 保存只落到本机板面；顺手刷新「本次互动材料的范围」让提交前预览是新的。
        // 这一步同样不调用 QIO。
        void refreshVisibleRange();
      } catch (err) {
        const status = (err as { status?: number }).status;
        const failure = (err as { payload?: { error?: string; reason?: string; scopeChanged?: boolean; affectedTasks?: { intentId: string; title: string; materials: string[]; consequence: string }[] } }).payload;
        // N1：已经有更新的保存接手 —— 这次的结果不属于当前候选，不许据此改写状态或报错
        if (superseded()) return;
        /**
         * N1 服务端保护：这是一次**旧版本**的整板写入，服务端拒绝落库。
         * 不能只把 seq 改大了重发（那会覆盖另一页面的成果），也不能假装保存成功：
         * 先读最新事实合并进候选（保留本页候选与独立变化），再用最新版本重新走一遍保存
         * （含影响预判与影响确认 —— 于是自动接回 F3 的可恢复流程）。
         */
        if (status === 409 && failure?.error === "stale_state") {
          impactConfirmed = false;
          pendingConfirm = null;
          saveStatus.value = "idle";
          saveError.value = null;
          // 有界重试：同一次候选最多重来 3 次，之后如实停在失败上（不做无界自动重试）
          if (saveRecoveryRev !== putRev) {
            saveRecoveryRev = putRev;
            saveRecoveryAttempts = 0;
          }
          saveRecoveryAttempts += 1;
          if (saveRecoveryAttempts > 3) {
            saveStatus.value = "error";
            saveError.value =
              "服务端上这个板面的版本一直在变化，这次保存没有完成（已自动重来 3 次）。" +
              "请确认是否有其他窗口或页面正在同时编辑，然后点重试";
            dirty.value = true;
            return;
          }
          const readError = await recoverCandidateFromServer(
            "服务端已有更新的已保存版本（" + (failure.reason ?? "板面版本已经变化") + "）",
          );
          if (readError) {
            saveStatus.value = "error";
            saveError.value = readError;
            dirty.value = true;
            return;
          }
          resumeSave = true;
          return;
        }
        if (status === 409 && failure?.error === "impact_confirmation_required") {
          /**
           * 服务端门（M4）：这次保存会改动运行任务依赖的材料，必须先做影响确认。
           * 候选与勾选保留，不落库。
           *
           * R5（本轮修复）：兜底分支**必须补取**与当前候选、服务器版本一致的影响预判（含 checkId）。
           * 基线在这里只把服务端列的 affectedTasks 放进对话框、没有 checkId，
           * 用户点确认后仍发不带确认句柄的 PUT，被服务端再拒一次 —— 反复弹同一说明、存不进去。
           * 补取后如果范围与刚才的服务端说明不同，先展示变化、由用户按新范围确认（绝不自动放行）。
           */
          impactConfirmed = false;
          pendingConfirm = null;
          const gatedTasks = failure.affectedTasks?.length ? failure.affectedTasks : undefined;
          // F3：先接收最新服务器事实（保留本页候选），再用**最新版本**重新预判 ——
          // 只有这样才能拿到有效的 checkId，而不是拿旧 seq 反复被拒（基线的真实缺陷）。
          const readError = await recoverCandidateFromServer("这次保存需要先做影响确认，但板面版本已经变化");
          // 读取失败**不能**让用户连确认说明都看不到：仍然按当前候选重新预判，
          // 把「最新事实没取到」如实写在说明里；万一版本真的变了，保存时会被服务端再拦一次。
          const outcome = await recheckImpactForCurrentCandidate(
            gatedTasks,
            readError ? "板面最新事实没有取到：" + readError + "。下面是按当前候选重新预判的范围；若版本已经变化，保存时会再要求你确认。" : undefined,
          );
          if (outcome === "blocked") {
            if (readError && impactCheckError.value) {
              impactCheckError.value = readError + "；重新预判也没有完成（" + impactCheckError.value + "）";
            }
            return;
          }
          if (outcome === "ok") resumeSave = true;
          saveStatus.value = "idle";
          saveError.value = null;
          return;
        }
        if (status === 409 && failure?.error === "stale_check") {
          /**
           * 确认句柄过期（板面版本变了，或服务端重新核对后发现范围变了）：重新核实，不落库。
           * R5/R6：保留候选，把**重新预判后的完整范围**交回用户重新确认；
           * 不自动批准、不无条件重发、不绕过服务端校验。
           */
          impactConfirmed = false;
          pendingConfirm = null;
          const gatedTasks = failure.affectedTasks?.length ? failure.affectedTasks : undefined;
          // R6：服务端明确说「拒绝是因为真实受影响范围变了」——先把变化讲清楚，再让用户确认
          const scopeNote =
            failure.scopeChanged === true
              ? "重新核对后发现受影响的运行中任务与刚才的说明不同：下面是当前的完整范围，" +
                "这次改动还没有保存、任务也都还没被暂停，请按它重新确认。"
              : undefined;
          /**
           * F3：确认过期多半是因为另一页面已经保存了更新的版本。
           * 先接收最新事实（seq 前进、独立变化被协调进候选），再用最新版本重新预判，
           * 然后重新展示完整范围交给用户重新确认 —— 不自动确认、不无条件重发、不改大 seq 蒙过去。
           */
          const readError = await recoverCandidateFromServer(
            "这次改动的影响确认已经过期（" + (failure.reason ?? "板面版本已变化") + "）",
          );
          // 读取失败同样不能把用户堵在「无解释」上：仍按当前候选重新预判并重新展示范围
          const outcome = await recheckImpactForCurrentCandidate(
            gatedTasks,
            readError
              ? "板面最新事实没有取到：" + readError + "。下面是按当前候选重新预判的范围；若版本已经变化，保存时会再要求你确认。"
              : scopeNote,
          );
          if (outcome === "blocked") {
            if (readError && impactCheckError.value) {
              impactCheckError.value = readError + "；重新预判也没有完成（" + impactCheckError.value + "）";
            }
            return;
          }
          if (outcome === "ok") resumeSave = true;
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
    // N1/F3：服务端要求「用最新版本重来」时，这里必须真的再发一次；
    // 收敛由代次 + 恢复状态保证（不会无界重试：要么保存成功，要么停在明确失败上）。
    if (resumeSave && dirty.value && !pendingImpact.value && saveStatus.value !== "error") {
      scheduleSave();
    }
  }

  function undo() {
    if (!canUndo.value || !board.value) return;
    const previous = undoStack.value.pop() as string;
    redoStack.value.push(JSON.stringify(board.value));
    const reverted = JSON.parse(previous) as BoardState;
    // 撤销也是一次板面变化：勾选/正文冲突的记账要跟着走（N5 保护不能拿旧值覆盖）
    trackCheckedChanges(board.value, reverted);
    settleContentConflicts(board.value, reverted);
    board.value = reverted;
    boardLocalRev += 1;
    dirty.value = true;
    lastOpLabel.value = "撤销";
    scheduleSave();
  }

  function redo() {
    if (!canRedo.value || !board.value) return;
    const next = redoStack.value.pop() as string;
    undoStack.value.push(JSON.stringify(board.value));
    const reapplied = JSON.parse(next) as BoardState;
    trackCheckedChanges(board.value, reapplied);
    settleContentConflicts(board.value, reapplied);
    board.value = reapplied;
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
        // F2：即便如此也按「登记时那一条」的身份清理，不用裸对象删除
        applyIdentityRemoval(key, cardId, local);
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
      if (serverText !== null && serverText === local.text && !recordCarriesLocalMeta(local)) {
        // 服务器上已经有同样一份（且这条记录不带只在本机存在的附加字段）：按它的身份清理掉
        applyIdentityRemoval(key, cardId, local);
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
        conflictFingerprints.set(key, localRecordFingerprint(local));
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

  /**
   * F1：取消撤回的恢复状态收敛。
   * `ok=true` 只表示**服务器事实已经落地**（回读成功采纳 / 后续保存被接受），
   * 不是「我们相信撤回成功了」；失败时保留真实原因与可重试状态。
   */
  function settleCancelRecovery(ok: boolean, reason: string | null): void {
    const state = cancelRecovery.value;
    if (!state || !state.active) return;
    cancelRecovery.value = ok
      ? { ...state, active: false, reason: null }
      : { ...state, active: true, reason: reason ?? "撤回还没被服务器事实确认" };
  }

  /**
   * F3/N5 的唯一版本事实入口：把最新服务器事实**安全合并**进当前候选。
   *
   * - 吸收版本事实（seq / updatedAt / 已知服务器版本）；
   * - 服务器上的独立变化（另一页面的移动、勾选、提交清理）协调进候选；
   * - 本页用户自己的编辑一律保留；同一张卡片正文两边都改 → 登记内容冲突，交用户决定；
   * - **绝不整块替换候选**，也绝不因为「有候选」就拒绝接收服务器事实。
   *
   * 返回合并结果（含冲突清单），调用方据此提示用户或继续保存。
   */
  function adoptServerFacts(
    server: BoardState,
  ): { conflicts: BoardContentConflict[]; removalConflicts: RemovalConflict[] } {
    /*
      Phase 2：服务器事实与候选都先对齐**参与口径**再进入三路合并。
      这里用 applyParticipation（只补 participation / 旧镜像 / 迁移标记），
      **不用 normalizeState** —— 后者会给缺 updatedAt 的卡片补当前时间，
      等于凭空造出一次「无关改动」，让 F07 的删除合并误判成冲突（全量测试抓到过）。
    */
    const serverState = applyParticipation(server);
    const candidate = board.value ? applyParticipation(board.value) : null;
    // 基线同样对齐参与口径：三路比较必须在同一套字段上做，否则会把「迁移补齐的字段」当成改动。
    const base = cleanState ? applyParticipation(cleanState) : null;
    const serverSeq = Number(serverState?.seq ?? server?.seq ?? 0);
    if (Number.isFinite(serverSeq)) knownServerSeq = Math.max(knownServerSeq, serverSeq);
    if (!candidate) {
      cleanState = cloneState(serverState);
      board.value = forceParticipationChoices(serverState);
      reconcileParticipationChoices(serverState);
      boardLocalRev += 1;
      boardCleanRev = boardLocalRev;
      dirty.value = false;
      saveStatus.value = serverSeq > 0 ? "saved" : "idle";
      return { conflicts: [], removalConflicts: [] };
    }
    const hadCandidate = boardLocalRev > boardCleanRev;
    const outcome = mergeServerInto(candidate, serverState, base);
    reconcileCheckedChoices(serverState);
    reconcileParticipationChoices(serverState);
    /*
      §5.1：用户自己重新选过的参与状态以用户为准 —— 服务器上的迟到事实
      （旧后端的勾选清理 / 旧回执）不许把它改回去，直到服务器事实与它一致。
    */
    outcome.state = forceParticipationChoices(outcome.state);
    /**
     * N5：用户在基线之后自己动过的勾选，以**用户当前这一版**为准 ——
     * 服务器上的迟到事实（例如提交清理、旧回执）不许把用户重新做的选择取消掉。
     */
    if (localCheckedChoices.size > 0) {
      const localById = new Map(candidate.cards.map((card) => [card.id, card]));
      let forced = false;
      const cards = outcome.state.cards.map((card) => {
        if (!localCheckedChoices.has(card.id)) return card;
        const choice = localCheckedChoices.get(card.id) as boolean;
        const localCard = localById.get(card.id);
        if (!localCard || localCard.checked === card.checked) return card;
        forced = true;
        return { ...card, checked: choice };
      });
      if (forced) outcome.state = { ...outcome.state, cards };
    }
    cleanState = cloneState(serverState);
    board.value = outcome.state;
    boardLocalRev += 1;
    if (hadCandidate) {
      // 候选仍在（用户自己的改动没被丢掉），但它现在基于服务器的最新版本
      dirty.value = true;
      if (pendingImpact.value) {
        // 还在等用户决定影响确认：不要自作主张再排一次保存，等用户确认
        saveStatus.value = "idle";
      } else {
        saveStatus.value = "saving";
        scheduleSave();
      }
    } else {
      boardCleanRev = boardLocalRev;
      dirty.value = false;
    }
    if (outcome.conflicts.length) {
      const next = { ...boardContentConflicts.value };
      for (const item of outcome.conflicts) {
        next[item.cardId] = { local: item.local, server: item.server };
      }
      boardContentConflicts.value = next;
    }
    /**
     * F07：本地删除与服务器对同一对象的修改相撞 —— 把服务器那一份作为**依据**留下，
     * 在用户明确选择之前保存会被挡住（见 saveNow 的门），既不静默复活也不丢对方修改。
     */
    if (outcome.removalConflicts.length) {
      const next = { ...boardRemovalConflicts.value };
      for (const item of outcome.removalConflicts) {
        next[item.id] = { kind: item.kind, id: item.id, server: item.server };
      }
      boardRemovalConflicts.value = next;
    }
    return outcome;
  }

  /** 还没被用户决定的内容冲突（F3）：有它就不许保存，避免静默覆盖服务器正文。 */
  function unresolvedContentConflicts(): { cardId: string; local: string; server: string }[] {
    return Object.entries(boardContentConflicts.value).map(([cardId, value]) => ({ cardId, ...value }));
  }

  async function refreshBoardFromServer() {
    // 请求发出前的内存版本：迟到返回时用它判断「用户是不是已经又输入了」（§11.1）
    const before = new Map(draftKeySeq);
    // 各键「已保存版本」的读取基线（M1/01）：GET 在飞期间一次 PUT 先成功到达，
    // saved 越过读取基线——这次 GET 返回的正文对那个已确认保存的新版本是迟到的，
    // 不予应用（新输入由 memoryIsNewer 覆盖；两者合起来才完整）。
    const readSaved = new Map(draftSavedKeySeq);
    /**
     * R2（本轮修复）：正式板面的读取同样要按**请求发出时刻的基线**落地，不能只看 dirty。
     * - `requestedBoardId`：这次读取属于哪个板面（迟到的别板面回执不许落到当前板面）；
     * - `readLocalRev` / `readCleanRev`：读取开始时「本地候选版本」与「与服务器一致的版本」。
     *   期间任何一次保存被确认（boardCleanRev 前进）都说明这次 GET 读到的正文已经迟到 ——
     *   用户刚保存成功的新版本不许被旧读取回退；
     * - `readSeq`：读取代次，两次 GET 乱序时只有更新的那次算数。
     */
    const requestedBoardId = boardId.value;
    const readLocalRev = boardLocalRev;
    const readCleanRev = boardCleanRev;
    const readSeq = ++boardReadSeq;
    /**
     * 板面身份一变，上一个板面的提交 / 理解事实就不属于这里（任务书 §5.7）：
     * 不整块归零的话，切过来还会看到上一个板面的「已提交 / 待审批」，
     * 而迟到的理解回执也可能落到新板面上。
     */
    if (requestedBoardId !== statusBoardId) resetSubmissionStatusForBoard(requestedBoardId);
    const payload = await api.fetchBoardState(requestedBoardId);
    // 板面已经切走：这次回执不属于当前板面，按板面身份保护丢弃（不能串用别板面的版本）
    if (boardId.value !== requestedBoardId) return false;
    boardId.value = payload.board.id;
    // 服务端可能给出规范化的板面 id：以它为准，下一次读取（同一个板面）才不会误判成切换
    statusBoardId = payload.board.id;
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
    /**
     * F3：这次读取是不是比我们已知的服务器版本**更新**？只有更新的读取才允许改动板面事实。
     * - 有未保存候选 / 读取期间板面被推进：不能整块覆盖，但**必须接收服务器事实** ——
     *   合并进候选并吸收新 seq（基线正是在这里连事实都不接收，候选永远带旧 seq，保存一直被拒）。
     * - 已有更新的读取落地（readSeq 更小）或这次读到的比已知版本旧：只合并草稿，不动板面事实。
     */
    const payloadSeq = Number(payload.state?.seq ?? payload.seq ?? 0);
    const payloadIsNewer = payloadSeq >= knownServerSeq;
    if (payloadIsNewer) knownServerSeq = payloadSeq;
    const hasCandidate = boardLocalRev > boardCleanRev;
    const boardMovedDuringRead =
      boardLocalRev !== readLocalRev || boardCleanRev !== readCleanRev;
    const readIsStale = readSeq < boardReadAppliedSeq;
    if (hasCandidate || boardMovedDuringRead || readIsStale) {
      if (payloadIsNewer && !readIsStale) {
        // 接收服务器事实：吸收新版本，服务器上的独立变化被协调进候选，用户自己的编辑保留
        adoptServerFacts(payload.state);
        boardReadAppliedSeq = Math.max(boardReadAppliedSeq, readSeq);
        settleCancelRecovery(true, null);
      } else {
        // 迟到的读取：只合并草稿，不用它回退任何板面事实
        mergeDraftsFromPayload(payload, before, readSaved);
      }
      if (pendingRemovals.size > 0) scheduleDraftSave();
      settleCancelRecovery(true, null);
      return true;
    }
    /**
     * F08：即使当前没有未保存候选，也**不能**用一个比已确认事实更旧的读取回退板面版本
     * 与清洁基线（那会直接把保存已确认的新版本退回去，还会让后续合并把旧正文当成
     * 「服务器改动」而造出假冲突）。旧读取只允许合并草稿，不动任何板面事实。
     */
    if (!payloadIsNewer) {
      mergeDraftsFromPayload(payload, before, readSaved);
      if (pendingRemovals.size > 0) scheduleDraftSave();
      settleCancelRecovery(true, null);
      return true;
    }
    /*
      服务器事实入口统一归一化（2026-10-14 第七个问题）：画布范围是**状态不变量** ——
      已存成负坐标的卡片（实测 y = −63.84）顶部永远看不见，加载时就夹回画布内。
    */
    /*
      Phase 2 §5.1：整块采用服务器事实之前先对齐参与口径（补 participation / 同步旧镜像 /
      补迁移标记），再把用户还没被服务器确认的参与选择贴回去 —— 旧后端的勾选清理
      不能把参与状态一起清掉。
    */
    const serverState = applyParticipation(payload.state);
    reconcileParticipationChoices(serverState);
    /*
      整块采用**刷新读到的服务器事实**：不贴回本地参与选择 —— 这是一次权威读取，
      服务端明确给出的 participation / checked 就是当前事实（既有「以服务端事实为准」不变量）。
      本地参与选择只在「有未保存候选、合并服务器事实」时生效（adoptServerFacts）。
    */
    board.value = applyParticipation(clampBoardBounds(serverState));
    cleanState = cloneState(board.value);
    reconcileCheckedChoices(board.value);
    // 整块采用服务器事实：与它不一致的本地勾选选择记录一并作废（不能留到下次合并又冒出来）
    dropCheckedChoicesNotIn(board.value);
    boardCleanRev = boardLocalRev;
    boardReadAppliedSeq = Math.max(boardReadAppliedSeq, readSeq);
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
    settleCancelRecovery(true, null);
    return true;
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
    /**
     * C1：服务端存簿版本（rev / 逐键版本）只增不减的事实入口。
     * 迟到的读取不许把已经确认的更高版本退回去，否则客户端会拿旧版本去覆盖别人的新内容。
     */
    const payloadRev = Number(payload.drafts?.rev);
    if (Number.isFinite(payloadRev) && (serverDraftRev === null || payloadRev > serverDraftRev)) {
      serverDraftRev = payloadRev;
    }
    for (const [key, version] of Object.entries(payload.drafts?.keyVersions ?? {})) {
      if (typeof version !== "number" || !Number.isFinite(version)) continue;
      serverDraftKeyVersions.set(key, Math.max(serverDraftKeyVersions.get(key) ?? 0, version));
    }
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

  /**
   * F11：读取任务清单。
   *
   * - 成功：整体替换清单 / 冲突 / 批次依据，并清掉上一次的读取错误（不累积无意义提示）；
   * - 失败：**保留**最后一次成功读到的任务、冲突与批次依据，只标明「刷新失败」与真实原因。
   *   读取失败绝不是「任务消失」；只有成功返回的空集合才是真的空。
   */
  async function loadIntents(): Promise<boolean> {
    intentsLoadState.value = "loading";
    try {
      const payload = await api.fetchIntents(boardId.value);
      intents.value = payload.intents ?? [];
      conflicts.value = payload.conflicts ?? [];
      batchAvailable.value = Boolean(payload.batchAvailable);
      recoverNotice.value = payload.recovery?.paused ?? [];
      intentsLoadedOnce.value = true;
      intentsLoadState.value = "ok";
      intentsLoadError.value = null;
      // 意图列表里带的执行记录也是服务端事实（与 executions 接口去重合并）
      if (payload.executions?.length) mergeExecutions(payload.executions);
      return true;
    } catch (err) {
      // 只记这次读取失败：旧清单、旧冲突、旧批次依据原样保留（不清空、不假装任务消失）
      intentsLoadState.value = "error";
      intentsLoadError.value = (err as Error).message || "原因未知";
      return false;
    }
  }

  /** 意图视图读取失败时给用户看的原因（不暴露传输层外壳之外的内部代码）。 */
  function intentViewsErrorMessage(err: unknown): string {
    const message = (err as Error)?.message ?? String(err ?? "");
    return message || "原因未知";
  }

  /**
   * F02：读取新链意图视图（**唯一入口**）。
   *
   * 关键性质：
   * - 同一时刻只允许一份在中国（in-flight 复用）：连点 / 多条路径同时刷新不会打出重复请求；
   * - 按**板面身份 + 读取代次**落地：切走板面或已经有更新的读取落地时，旧响应直接丢弃（F02.8）；
   * - 失败**保留**最后一次成功内容，只记错误（F06）；只有成功返回的空集合才是真的空；
   * - 可传 throttleMs：轮询路径用节流，避免把 1s 轮询变成请求风暴（F02.7）。
   *
   * 返回 true = 这次真的拿到了服务端事实（含空集合）；false = 没拿到（复用/失败/被取代）。
   */
  async function loadIntentViews(options: { throttleMs?: number } = {}): Promise<boolean> {
    if (!apiSupports("fetchIntentViews")) return false;
    // 同一时刻只允许一份在飞读取（连点 / 多路径刷新不重复请求）；
    // 但板面已经切换时，旧板面的在飞读取不许挡住新板面的读取。
    if (intentViewsInFlight && intentViewsInFlightBoardId === boardId.value) {
      return intentViewsInFlight;
    }
    const throttle = Number(options.throttleMs ?? 0);
    if (throttle > 0 && Date.now() - intentViewsLastStartedAt < throttle) return false;
    const requestedBoardId = boardId.value;
    const seq = ++intentViewsSeq;
    intentViewsLastStartedAt = Date.now();
    intentViewsLoadState.value = "loading";
    let run: Promise<boolean> | null = null;
    run = (async () => {
      try {
        const payload = await api.fetchIntentViews(requestedBoardId);
        if (boardId.value !== requestedBoardId) {
          settleDiscardedIntentRead(seq);
          return false;
        }
        if (seq < intentViewsAppliedSeq) {
          settleDiscardedIntentRead(seq);
          return false;
        }
        intentViewsAppliedSeq = seq;
        if (payload?.available === false) {
          // 服务端明确说「这份能力现在不可用」：不是空集合，也不是读取成功
          intentViewsLoadState.value = "error";
          intentViewsLoadError.value =
            (payload as { reason?: string }).reason || "服务端的意图视图能力当前不可用（不代表没有意图）";
          return false;
        }
        const list = Array.isArray(payload?.intents) ? payload.intents : [];
        intentViews.value = list as IntentViewFact[];
        intentViewsSource.value = String((payload as { source?: string }).source ?? "");
        intentViewsLoadedOnce.value = true;
        intentViewsLoadState.value = "ok";
        intentViewsLoadError.value = null;
        return true;
      } catch (err) {
        if (boardId.value !== requestedBoardId || seq < intentViewsAppliedSeq) {
          settleDiscardedIntentRead(seq);
          return false;
        }
        // 读取失败不是「意图消失」：旧清单原样保留，只标明更新失败与真实原因
        intentViewsLoadState.value = "error";
        intentViewsLoadError.value = intentViewsErrorMessage(err);
        return false;
      } finally {
        if (run !== null && intentViewsInFlight === run) {
          intentViewsInFlight = null;
          intentViewsInFlightBoardId = "";
        }
      }
    })();
    intentViewsInFlight = run;
    intentViewsInFlightBoardId = requestedBoardId;
    return run;
  }

  /**
   * 被丢弃的读取（板面切走 / 已经有更新的读取落地）：
   * 只有它确实是最后一次发起、且状态还停在本次读取时，才把「正在读取」收回去，
   * 免得界面永远停在加载中；更新的读取正在飞时什么都不做。
   */
  function settleDiscardedIntentRead(seq: number): void {
    if (seq !== intentViewsSeq) return;
    if (intentViewsLoadState.value !== "loading") return;
    intentViewsLoadState.value = intentViewsLoadedOnce.value ? "ok" : "idle";
  }

  /**
   * F11 + F06：依赖最新意图事实的动作（确认 / 恢复 / 任务控制之外的读取）先核实。
   *
   * 上一次读取失败意味着本页看到的是旧快照 —— 不能拿它当「当前权威」直接放行。
   * 先按服务端事实重新读一次；仍然读不到就返回可读原因，由调用方**暂停**这个动作。
   */
  async function ensureFreshIntentViews(): Promise<string | null> {
    if (intentViewsLoadState.value !== "error") return null;
    const ok = await loadIntentViews();
    if (ok) return null;
    return (
      "意图清单读取失败，无法确认这次操作依据的是最新事实，已暂停这次操作（" +
      (intentViewsLoadError.value ?? "原因未知") +
      "）。请先重试读取意图清单"
    );
  }

  /** 意图事实的完整刷新（清单 + 视图 + 执行记录）：同一处实现，所有入口都走它。 */
  async function refreshIntentFacts(): Promise<{ ok: boolean; intentError?: string }> {
    const [, viewsOk] = await Promise.all([loadIntents(), loadIntentViews()]);
    return viewsOk
      ? { ok: true }
      : { ok: false, intentError: intentViewsLoadError.value ?? "原因未知" };
  }

  /**
   * 确认 / 恢复 / 任务控制之后：意图状态、正式板面、可见范围、执行记录一起刷新。
   *
   * 只读：不重复创建意图、不重复执行任务；板面刷新走既有合并规则
   * （有未保存候选时保留用户候选，只接收服务器事实）。
   */
  async function refreshAfterIntentAction(): Promise<void> {
    await refreshIntentFacts();
    try {
      await refreshBoardFromServer();
      await refreshVisibleRange();
    } catch {
      // 板面刷新失败只影响「页面事实是否最新」，执行与意图读取照常进行
    }
    await loadExecutions();
  }

  /** 打开任务面板（f焦点区分「任务」与「批量待确认」两条入口，界面是同一个）。 */
  function openTaskCenter(focus: "tasks" | "pending" = "tasks"): void {
    taskCenterFocus.value = focus;
    tasksOpen.value = true;
  }

  function closeTaskCenter(): void {
    tasksOpen.value = false;
    taskCenterFocus.value = "tasks";
  }

  /**
   * F02.6：回到前台 / 断线恢复时**重新核实**一次事实。
   *
   * 只读：绝不重复创建意图、不重复提交、不自动执行任务；
   * 失败只记在对应的读取状态里（界面照旧显示上次事实并说明更新失败）。
   */
  let lastReverifyAt = 0;
  async function reverifyAfterResume(): Promise<void> {
    const now = Date.now();
    // 1s 内的重复触发（可见性 + online 双事件）合并成一次
    if (now - lastReverifyAt < 1000) return;
    lastReverifyAt = now;
    if (submitStatus.value === "submitting") return;
    await refreshIntentFacts();
    await loadExecutions();
    if (understandingStatus.value && !understandingSettled(understandingStatus.value)) {
      void refreshUnderstanding();
    }
  }

  /**
   * 页面卸载 / 离开互动页：停掉本页的轮询与可见性监听。
   *
   * 只清计时器与监听，不清任何服务端事实 —— 重新进入时 load() 会重新核实。
   */
  function disposePageRuntime(): void {
    stopUnderstandingPolling();
    stopExecutionPolling();
    if (understandingVisibilityHooked) {
      understandingVisibilityHooked = false;
    }
    if (executionVisibilityHooked) {
      executionVisibilityHooked = false;
    }
    intentViewsInFlight = null;
  }

  /**
   * F11：依赖最新事实的动作（审批 / 拒绝 / 批量决定）在事实可疑时先做一次服务端核验。
   *
   * 上一次读取失败意味着本页看到的是旧快照 —— 不能拿它当「当前权威」直接放行。
   * 先按服务端事实重新读一次；仍然读不到就**暂停**这个动作并给出可重试原因
   * （绝不用旧快照批准，也不假装它仍然有效）。
   */
  async function ensureFreshIntents(): Promise<string | null> {
    if (intentsLoadState.value !== "error") return null;
    const ok = await loadIntents();
    if (ok) return null;
    return `任务清单读取失败，无法确认这条任务的最新状态，已暂停这次操作（${intentsLoadError.value ?? "原因未知"}）。请先重试刷新任务清单`;
  }

  async function load() {
    loading.value = true;
    loadError.value = null;
    try {
      await refreshBoardFromServer();
      await refreshVisibleRange();
      await loadIntents();
      // F02/F06：意图视图与任务清单同一处维护；读取失败保留上次内容并标「更新失败」
      await loadIntentViews();
      // 刷新后如实恢复真实的理解与执行状态（页面刷新不重复创建、不重复执行）
      await refreshUnderstanding();
      await loadExecutions();
      /**
       * 契约 §1.3：重启 / 刷新后仍未确认的同一次提交沿用**同一个**请求身份，
       * 并立刻按这个身份做一次精确查证：查到就恢复受理事实，查不到就继续「结果未知」。
       */
      await restorePendingSubmission();
      if (understandingStatus.value && !understandingSettled(understandingStatus.value)) {
        void startUnderstandingPolling();
      }
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
      conflictFingerprints.delete(key);
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

  /**
   * P1：本次提交**真的把哪些未确认草稿提升成了正文**（键：卡片 id；值：提升后的正文字样）。
   *
   * 为什么需要它：提升是「用户没点完成编辑，但字确实随提交生效了」这件事的唯一可核对依据。
   * 提交成功之后界面要能如实说明「N 条未点完成编辑的文字已随提交生效」，
   * 否则用户会以为自己的输入凭空消失了（契约 4）。
   *
   * 只有**回执证明这份正文真的进了本次快照**（服务端回读的板面 / 提交载荷里的 before）
   * 才写进来：绝不在还没提交成功时就宣布「已生效」。
   */
  const promotedDrafts = ref<{ cardId: string; text: string }[]>([]);
  const promotedDraftsCount = computed(() => promotedDrafts.value.length);
  /**
   * P1：这次提交**没有**提升的卡片草稿及原因（未决冲突）。
   * 界面据此给出可读说明；草稿与正文都原样留着，等用户明确选择（契约 2）。
   */
  const draftsNotPromoted = ref<{ cardId: string; reason: string }[]>([]);

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
      pendingLocalRemovals.set(key, { purpose: "clear-draft", expectVersion: onDiskVersion, guard: "version" });
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
      if (written.ok && typeof written.version === "number") draftLocalWrittenVersion.set(key, written.version);
      if (conflict) {
        // 冲突里的「本机候选」已经换成这份新输入：两份来源都还在，选择入口继续可操作
        setDraftConflict(key, { local: text, server: conflict.server });
      }
      if (conflict && written.ok) {
        // 清理守卫跟着记录的新版本走（版本号与内容身份依据一起更新）
        conflictVersions.set(key, written.version);
        conflictFingerprints.set(key, cardLocalDraftFingerprint(cardId));
      }
    }
    // 一有输入就进「保存中」：失败时才会被改成 error（绝不停在「已保存」）
    setDraftState(key, "saving");
    scheduleDraftSave();
  }

  /**
   * N6：卡片编辑的**完整未完成输入**（正文 + 适用附加字段，含空值）一次写入本机记录。
   *
   * 与 `setDraft` 走**同一套规则**（冲突保持、清除登记回收、代次推进、排一次保存），
   * 唯一差别是本机记录一次写下 text + meta —— 不新增第二写者。
   * 服务器草稿通道仍然只放正文（Record<string,string>），附加字段只落在本机恢复来源里。
   */
  function setCardDraftInput(cardId: string, input: CardDraftInput): void {
    const key = cardDraftKey(cardId);
    const text = input.text ?? "";
    const conflict = draftConflicts.value[key];
    if (conflict && text === conflict.local) {
      lastDraftKey.value = key;
      return;
    }
    drafts.value = { ...drafts.value, [key]: text };
    lastDraftKey.value = key;
    draftKeySeq.set(key, ++draftSeq);
    if (pendingRemovals.delete(key)) setDraftRemovalState(key, "idle");
    pendingLocalRemovals.delete(key);
    if (!conflict) setDraftConflict(key, null);
    const written = writeCardDraftInput(cardId, { text, ...(input.meta ? { meta: input.meta } : {}) }, {
      boardId: boardId.value,
      seq: draftKeySeq.get(key) ?? 0,
    });
    setDraftLocalState(key, written);
    if (written.ok && typeof written.version === "number") draftLocalWrittenVersion.set(key, written.version);
    if (conflict) {
      setDraftConflict(key, { local: text, server: conflict.server });
      if (written.ok) {
        conflictVersions.set(key, written.version);
        conflictFingerprints.set(key, cardLocalDraftFingerprint(cardId));
      }
    }
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
    /**
     * F2：用户的明确选择只证明「**登记冲突那一刻**那条记录」该删，
     * 不能证明现在磁盘上还是同一条 —— 所以清理必须带上当时的身份依据。
     * 正版本号用版本守卫；旧格式（无版本 / version 0）只能用内容指纹守卫，
     * 对不上（另一页面已经写了新稿）就保留新稿，绝不退化成按对象 id 裸删。
     */
    const identity = conflictFingerprints.has(key) ? conflictFingerprints.get(key) ?? null : cardLocalDraftFingerprint(cardId);
    if (typeof confirmedVersion === "number" && confirmedVersion > 0) {
      // 版本守卫：记录已被更晚的写入替换（如同浏览器的另一个页面）时不能删
      applyLocalRemovalResult(
        key,
        removeCardLocalDraftIfUnchanged(cardId, confirmedVersion, identity),
        confirmedVersion,
        "remove-local-copy",
        "version",
        identity,
      );
    } else {
      applyLocalRemovalResult(
        key,
        removeCardLocalDraftIfUnchanged(cardId, null, identity),
        null,
        "remove-local-copy",
        "fingerprint",
        identity,
      );
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
    purpose: LocalRemovalPurpose = "remove-local-copy",
    guard?: LocalRemovalGuard,
    /**
     * F2：无版本（旧格式 / version 0）记录的**可验证身份依据**（登记删除决定时算出的那一条）。
     * 重试时只有当前记录仍与它一致才允许删除；对不上就保留新稿（version-guard，不算失败）。
     */
    expectFingerprint: string | null = null,
  ): void {
    const resolvedGuard: LocalRemovalGuard =
      guard ?? (typeof expectVersion === "number" && expectVersion > 0 ? "version" : expectFingerprint ? "fingerprint" : "object");
    if (result.ok || result.reason === "version-guard") {
      pendingLocalRemovals.delete(key);
      setDraftLocalState(key, { ok: true, error: null });
      return;
    }
    pendingLocalRemovals.set(key, { purpose, expectVersion, guard: resolvedGuard, expectFingerprint });
    setDraftLocalState(key, {
      ok: false,
      error: result.error ?? "这份本机副本没能删掉，暂时还留在本机",
    });
  }

  /**
   * 按登记的真实目的与守卫口径重试一次本机记录处理（R4）：
   * - remove-local-copy + absent：登记时本来就没有记录 → 现在仍然没有才算无事可做
   *   （与 removeCardLocalDraftIfUnchanged 同口径，绝不误删请求期间别的页面新建的记录）；
   * - remove-local-copy + version：只删那个确切版本；
   * - remove-local-copy + object：按对象删（旧格式记录没有版本可校验）；
   * - clear-draft 不走这里：整份草稿清除必须先幂等补写 cleared 依据（12 的既有保护）。
   */
  function retryLocalRemoval(
    cardId: string,
    entry: { expectVersion: number | null; guard: LocalRemovalGuard; expectFingerprint?: string | null },
  ): ReturnType<typeof removeCardLocalDraft> {
    if (entry.guard === "absent") return removeCardLocalDraftIfUnchanged(cardId, entry.expectVersion);
    /**
     * F2：**没有正版本号**的记录（旧格式 / version 0）一律走指纹守卫 ——
     * 只有当前记录仍与登记决定时那条一致才允许删除；证明不了就保留新稿。
     * 绝不允许退化成「按对象 id 裸删」（那正是重试误删另一页面新稿的根因）。
     */
    if (entry.guard === "fingerprint" || (entry.guard === "object" && entry.expectFingerprint)) {
      return removeCardLocalDraftIfUnchanged(cardId, entry.expectVersion, entry.expectFingerprint ?? null);
    }
    if (entry.guard === "object") {
      // 没有指纹、也没有正版本：登记时本来就没有可校验的依据 → 只允许「仍然没有记录」这一种情况
      return removeCardLocalDraftIfUnchanged(cardId, entry.expectVersion, null);
    }
    if (entry.guard === "version" && entry.expectVersion !== null) {
      return removeCardLocalDraftIfUnchanged(cardId, entry.expectVersion, entry.expectFingerprint ?? undefined);
    }
    return removeCardLocalDraftIfUnchanged(cardId, entry.expectVersion, null);
  }

  /**
   * F2：按「当前记录仍是登记时那一条」清理本机记录（正版本号 + 内容身份依据双守卫）。
   * 无版本 / version 0 的旧格式记录只靠指纹证明身份；证明不了就保留（不误删新稿）。
   */
  function applyIdentityRemoval(
    key: string,
    cardId: string,
    record: DraftRecord | null,
    purpose: LocalRemovalPurpose = "remove-local-copy",
  ): void {
    const fingerprint = localRecordFingerprint(record);
    const version = record && typeof record.version === "number" && record.version > 0 ? record.version : null;
    applyLocalRemovalResult(
      key,
      removeCardLocalDraftIfUnchanged(cardId, version, fingerprint),
      version,
      purpose,
      "fingerprint",
      fingerprint,
    );
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
    /**
     * C1：未决冲突键在等用户明确选择，不算「还有没做完的保存工作」——
     * 否则每 600ms 都会重发一次同样的冲突，界面永远停在「保存中」。
     */
    const unsavedWaitingForChoice = unsavedDraftKeys().every((key) => Boolean(draftConflicts.value[key]));
    return (hasUnsavedDrafts() && !unsavedWaitingForChoice) || pendingRemovals.size > 0;
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
    /**
     * C1：这一轮有没有停在「必须由用户决定」的状态上（同键冲突 / 删除没被确认）。
     * 有就不再做无意义的重发 —— 那只会反复产生同样的冲突，不会让事情往前一步。
     */
    let conflictStopped = false;
    draftInFlight = (async () => {
      try {
        for (let round = 0; round < DRAFT_FLUSH_MAX_ROUNDS; round += 1) {
          if (!hasDraftWork()) return;
          /**
           * C1（F01 的根治点）：只上传**本次确实有未保存改动**的键 —— 逐键更新。
           *
           * 基线把内存里的整份草稿集合发上去当整集合替换：另一个页面的旧快照会把本页
           * 刚保存的新草稿删掉，也会把对方更新过的同一个键按「最后到达」静默覆盖。
           * 现在未出现在请求里的键 = **未修改**，服务端绝不因此删除它们。
           */
          const conflictKeys = new Set<string>();
          for (const [conflictKey, value] of Object.entries(draftConflicts.value)) {
            if (!value) continue;
            // 未决冲突键等用户选择：不上传（本机候选不上去），也不删除（服务器那份原样保留）
            conflictKeys.add(conflictKey);
          }
          const keys = unsavedDraftKeys().filter((key) => !conflictKeys.has(key));
          const patchDrafts: Record<string, string> = {};
          for (const key of keys) patchDrafts[key] = drafts.value[key] ?? "";
          const atSeq = draftSeq;
          // 这次请求**明确删除**的键（每条带自己登记的身份版本）
          const removalsAtRequest = new Map(pendingRemovals);
          const removedKeys = [...removalsAtRequest.keys()];
          if (keys.length === 0 && removedKeys.length === 0) break;
          /**
           * 逐键版本（C1）：没有安全基线（baseRev 与服务端当前 rev 一致）时，
           * 服务端只凭它判断「这次覆盖 / 删除是不是基于当前那一版」。
           */
          const keyVersionsAtRequest: Record<string, number> = {};
          for (const key of [...keys, ...removedKeys]) {
            const version = serverDraftKeyVersions.get(key);
            if (typeof version === "number" && version > 0) keyVersionsAtRequest[key] = version;
          }
          /** 这次请求的服务端回执（只有它才能证明「哪些键真的保存 / 删除成功」） */
          let receiptsAtRequest: Awaited<ReturnType<typeof api.saveDrafts>> | null = null;
          // 各键此刻本机记录的版本：在飞回执清理本地副本必须按记录版本校验（§12.2）。
          // 记账口径与 removeCardLocalDraftIfUnchanged 一致：有记录取 version（旧格式记 0），没有记录为 null。
          const localVersionsAtRequest = new Map<string, number | null>();
          /**
           * 请求发出时本机记录的**完整快照**（版本 + 内容身份指纹 + 正文）。
           *
           * F2（独立复核发现的真实缺口）：只记版本不够 —— 如果本机记录里的内容
           * **不是**这次上传给服务器的内容（冲突里选了服务器版、或记录已被同一浏览器
           * 另一个页面换成新稿），服务器并没有收到这条记录，按「服务器已经有这一版」删它
           * 就会把另一页面还没上传的新稿删掉。正文 + 指纹一起才是「这条记录真的上去了」的证明。
           */
          const localSnapshotsAtRequest = new Map<
            string,
            { version: number | null; fingerprint: string | null; text: string; hasMeta: boolean } | null
          >();
          for (const key of keys) {
            if ((draftKeySeq.get(key) ?? 0) <= atSeq) setDraftState(key, "saving");
            const cardId = cardIdFromDraftKey(key);
            if (cardId) {
              const record = readCardLocalDraft(cardId);
              const version = record ? (typeof record.version === "number" ? record.version : 0) : null;
              localVersionsAtRequest.set(key, version);
              localSnapshotsAtRequest.set(
                key,
                record
                  ? {
                      version,
                      fingerprint: localRecordFingerprint(record),
                      text: record.text ?? "",
                      // N6：带附加字段快照的记录不是冗余副本，成功回执不许删它（见 recordCarriesLocalMeta）
                      hasMeta: recordCarriesLocalMeta(record),
                    }
                  : null,
              );
            }
          }
          for (const key of removalsAtRequest.keys()) setDraftRemovalState(key, "pending");
          try {
            receiptsAtRequest = await api.saveDrafts(boardId.value, patchDrafts, {
              removed: removedKeys,
              baseRev: serverDraftRev,
              keyVersions: keyVersionsAtRequest,
            });
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
          /**
           * C1：回执是**版本与确认**的唯一来源。
           * - rev / keyVersions 只增不减地吸收，供下一轮证明「这次写入基于哪一版」；
           * - conflicts 里的键服务器**没有**接受这次更新（它保留了自己那份）：两份文字都留下；
           * - cleared 只列出服务器**真的**清掉的键（本机副本能不能清理只依据它）。
           */
          const receiptRev = Number(receiptsAtRequest?.rev);
          if (Number.isFinite(receiptRev) && (serverDraftRev === null || receiptRev > serverDraftRev)) {
            serverDraftRev = receiptRev;
          }
          for (const [key, version] of Object.entries(receiptsAtRequest?.keyVersions ?? {})) {
            if (typeof version !== "number" || !Number.isFinite(version)) continue;
            serverDraftKeyVersions.set(key, Math.max(serverDraftKeyVersions.get(key) ?? 0, version));
          }
          const conflictByKey = new Map<string, { key: string; local: string; server: string }>();
          for (const item of receiptsAtRequest?.conflicts ?? []) {
            if (item && typeof item.key === "string") {
              conflictByKey.set(item.key, { key: item.key, local: item.local ?? "", server: item.server ?? "" });
            }
          }
          const blockedRemovals = new Set(receiptsAtRequest?.blockedRemovals ?? []);
          const clearedSet = Array.isArray(receiptsAtRequest?.cleared)
            ? new Set(receiptsAtRequest?.cleared ?? [])
            : null;
          for (const key of keys) {
            const keySeq = draftKeySeq.get(key) ?? 0;
            // 请求在飞时用户又改了：这次返回不算数，留给下一轮
            if (isStaleReceipt(atSeq, keySeq)) continue;
            const conflict = conflictByKey.get(key);
            if (conflict) {
              /**
               * C1 同键冲突：服务端**没有**覆盖它那份，本机这份也原样保留 ——
               * 两份都留下等用户明确选择（绝不静默挑一个，也绝不假装这次保存成功）。
               */
              setDraftConflict(key, {
                local: conflict.local || (drafts.value[key] ?? ""),
                server: conflict.server,
              });
              setDraftState(key, "error", "服务器上这个键已经有另一份文字：两份都已保留，请选择保留哪一份");
              conflictStopped = true;
              continue;
            }
            draftSavedKeySeq.set(key, keySeq);
            setDraftState(key, "saved");
            // 服务端已经拿到这一版：本机恢复副本按「当时那一版」清理（契约 §10.5 / §12.2）。
            // 版本校验（§12.2）：请求在飞期间被同一浏览器的**另一个页面**换成新版本的记录不许被这次回执删掉。
            const cardId = cardIdFromDraftKey(key);
            if (cardId) {
              const expectVersion = localVersionsAtRequest.get(key) ?? null;
              const snapshot = localSnapshotsAtRequest.get(key) ?? null;
              const uploaded = patchDrafts[key] ?? "";
              if (!snapshot || snapshot.hasMeta || snapshot.text !== uploaded) {
                /**
                 * F2：本机记录的内容**不是**这次上传的内容 —— 服务器并没有收到这条记录，
                 * 绝不能按「服务器已经有这一版」把它删掉（那会删掉另一页面刚写下的新稿）。
                 * 之前登记的删除决定也已经不作用于当前这条记录：保留记录、静默解除登记。
                 */
                if (pendingLocalRemovals.has(key)) {
                  pendingLocalRemovals.delete(key);
                  setDraftLocalState(key, { ok: true, error: null });
                }
              } else {
                applyLocalRemovalResult(
                  key,
                  removeCardLocalDraftIfUnchanged(cardId, snapshot.version, snapshot.fingerprint),
                  snapshot.version,
                  "remove-local-copy",
                  // 有正版本号按版本守卫；旧格式（0/null）只认内容指纹；都不可能退化成按对象裸删
                  snapshot.version !== null && snapshot.version > 0 ? "version" : "fingerprint",
                  snapshot.fingerprint,
                );
              }
            }
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
            /**
             * C1：只有服务端**确认**清掉的键才允许往下走（清理本机副本、解除登记）。
             * 明确回执以 cleared 为准；没有 cleared 字段时退回「回执里的剩余集合已不含这个键」
             * 这一可核对证据 —— 绝不靠「请求发出去了」推断删除成功。
             */
            const blocked = blockedRemovals.has(key);
            const stillStored = Object.prototype.hasOwnProperty.call(receiptsAtRequest?.drafts ?? {}, key);
            const confirmed = clearedSet ? clearedSet.has(key) : !stillStored;
            if (blocked || !confirmed) {
              const serverText = (receiptsAtRequest?.drafts ?? {})[key] ?? "";
              if (blocked || serverText) setDraftConflict(key, { local: "", server: serverText });
              setDraftRemovalState(
                key,
                "error",
                "服务器没有确认这次删除（它上面这份草稿可能已被另一处改过，或无法证明版本）：已保留服务器上的文字，请处理后再试",
              );
              setDraftState(key, "error", "这份草稿的删除还没有被服务器确认");
              conflictStopped = true;
              continue;
            }
            pendingRemovals.delete(key);
            const pendingLocal = pendingLocalRemovals.get(key);
            if (pendingLocal && pendingLocal.purpose === "remove-local-copy") {
              /**
               * R4：这条待处理决定的目的是**删本机冗余副本**，不是整份草稿清除 ——
               * 绝不能在这里补写 cleared 依据，否则就等于替用户决定清掉整份草稿。
               * 只按版本删这份记录；version-guard（更新的那一版仍在）照样是「有意保留」。
               */
              applyLocalRemovalResult(
                key,
                retryLocalRemoval(entry.cardId, pendingLocal),
                pendingLocal.expectVersion,
                "remove-local-copy",
                pendingLocal.guard,
                pendingLocal.expectFingerprint ?? null,
              );
            } else if (pendingLocal) {
              /**
               * 12：本机清除保护还没写成 —— 先把 cleared 依据补写成功（幂等），
               * 而不是直接删记录：删除再失败一次，磁盘上仍是 kind=draft，重开就复活。
               */
              const protection = ensureCardLocalClear(entry.cardId, {
                boardId: boardId.value,
                seq: draftKeySeq.get(key) ?? 0,
                expectVersion: pendingLocal.expectVersion,
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
          // C1：同键冲突 / 删除没被确认时停在明确状态上等用户决定，不再重发同样的请求
          if (conflictStopped) break;
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
    if (hasDraftWork() && !conflictStopped) {
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
      const pendingLocal = pendingLocalRemovals.get(item);
      if (!pendingLocal) continue;
      const retryCardId = cardIdFromDraftKey(item);
      if (!retryCardId) continue;
      /**
       * R4：按**用户决定的真实目的**分派重试，绝不把「删本机冗余副本」做成「整份草稿清除」。
       * - remove-local-copy：只按版本删掉这份本机记录（成功或 version-guard 都算处理完），
       *   不写 cleared 依据 —— 写下去就等于替用户决定清掉整份草稿，重开后服务器稿会被删掉；
       * - clear-draft：整份草稿清除，重试必须先幂等补写 cleared 依据（12 的既有保护）。
       */
      if (pendingLocal.purpose === "remove-local-copy") {
        // F2：重试必须按登记时的**身份依据**（版本 + 内容指纹）复核，不能裸按对象 id 删
        const removal = retryLocalRemoval(retryCardId, pendingLocal);
        applyLocalRemovalResult(
          item,
          removal,
          pendingLocal.expectVersion,
          "remove-local-copy",
          pendingLocal.guard,
          pendingLocal.expectFingerprint ?? null,
        );
        // 删成功、或被版本守卫有意保留：本轮都不再把本机副本写回去
        if (removal.ok || removal.reason === "version-guard") removedLocalNow.add(item);
        continue;
      }
      /**
       * 12（独立复核发现的反例）：本机清除保护写失败、而服务器清除已经确认时，
       * 重试**必须先把本机那份 cleared 依据补写成功**（幂等），而不是只尝试删记录 ——
       * 删除再失败一次，磁盘上留着的仍是 kind=draft 的旧稿，重开后它会重新取得
       * 恢复与上传权限（旧稿复活）。补写成功后保留这份依据（它就是「已确认清除」的事实），
       * 由后续正常的服务器清除流程按版本清理。
       */
      const expectVersion = pendingLocal.expectVersion;
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
        /**
         * F2 同类保护（写入方向）：磁盘上已经是**另一个页面写下的更新记录**时，
         * 这次补写会把它覆盖掉 —— 只在我们能证明「它比本页最后写下的版本更新」时保留它。
         */
        const currentRecord = readCardLocalDraft(cardId);
        const currentVersion =
          currentRecord && typeof currentRecord.version === "number" ? currentRecord.version : null;
        const ourVersion = draftLocalWrittenVersion.get(item);
        const foreignNewer =
          currentVersion !== null && ourVersion !== undefined && currentVersion > ourVersion;
        if (foreignNewer) {
          setDraftLocalState(item, { ok: true, error: null });
          continue;
        }
        const written = writeCardLocalDraft(cardId, drafts.value[item] ?? "", {
          boardId: boardId.value,
          seq: draftKeySeq.get(item) ?? 0,
        });
        setDraftLocalState(item, written);
        if (written.ok && typeof written.version === "number") draftLocalWrittenVersion.set(item, written.version);
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
   * F10：这次提交失败是不是「没有拿到回执」？
   *
   * 与发送通道同一口径（Lead 裁决）：带 status 的 4xx 是服务端**明确**的结论（明确失败）；
   * 没有响应、超时、5xx 等一律属于「提交结果未知」—— 服务端可能已经产生副作用，
   * 既不能假定成功，也不能假定失败。
   */
  function submitReceiptUnknown(err: unknown): boolean {
    const status = (err as { status?: unknown } | null)?.status;
    if (typeof status !== "number") return true;
    return !(status >= 400 && status < 500);
  }

  /**
   * 提交：QIO 取得未提交有效表达的唯一入口。
   * 先保存（保证服务端看到的是最新板面），再提交；失败时保留改动与勾选。
   *
   * F10：**提交请求**与**提交之后的读取**必须分开记录：
   * - POST 明确成功后刷新失败 → 提交成功是既成事实，只说「提交成功，页面刷新失败」，
   *   保留成功事实与基线依据，并给只触发读取的重试入口（绝不自动重复提交）；
   * - POST 没有拿到回执 → 「提交结果未知」，不假定成功也不假定失败。
   */
  async function submit(): Promise<SubmissionResult | null> {
    if (!board.value || submitStatus.value === "submitting") return null;
    submitError.value = null;
    submitResultUnknown.value = false;
    submitRefreshState.value = "idle";
    submitRefreshError.value = null;
    submitNote.value = null;
    acceptanceLookupNote.value = null;
    submitAcceptedByLookup.value = false;
    /**
     * P1：本次提交的「未点完成编辑」草稿统计属于**这一次**提交 —— 先清掉上一次的说明，
     * 免得把上次的提升说成本次的事实。
     */
    promotedDrafts.value = [];
    draftsNotPromoted.value = [];
    submitStatus.value = "submitting";
    /**
     * P1：把未确认草稿提升为正文，**必须在 saveNow() 之前**。
     *
     * 提升只改本页候选（board.value 的 cards[].content）并登记一次既有语义的草稿清除，
     * 之后照旧走 saveNow（版本门 / 正文冲突门 / 影响确认 / 保存回执），
     * 所以后端协议、提交载荷形状、可见范围判定都没有第二套实现。
     */
    const promotion = promoteDraftsBeforeSubmit();
    /**
     * 契约 2：有卡片的草稿处于未决冲突（草稿冲突 / 正文内容冲突）时**不提也不提交** ——
     * 那两种情况的正确做法是让用户明确选择保留哪一份，而不是替他猜。
     */
    if (promotion.blocked.length) {
      draftsNotPromoted.value = promotion.blocked;
      promotedDrafts.value = [];
      submitStatus.value = "failed";
      submitError.value =
        "这次没有提交：以下卡片还有未决定的冲突（草稿或正文哪一份更新无法判定，两份都保留着、不会静默丢掉任何一份）——" +
        promotion.blocked.map((item) => `${item.cardId}：${item.reason}`).join("；") +
        "。请先在卡片上选择保留哪一份，然后重新提交";
      return null;
    }
    /**
     * P1：这次**真的被写进候选**的卡片（提交成功后还要用回执核对它们确实进了快照）。
     * 只取提升函数自己的回执：不再另算一遍，免得两边口径漂移。
     */
    const promotedCandidates = promotion.promoted;
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
    /** N5：提交这一刻的「勾选修改代次」——等待期间用户自己重新勾选的，不许被这次清理取消。 */
    const checkedAtSubmit = new Map(cardCheckedSeq);
    /**
     * 契约 §1.3：请求身份在**发出提交之前**生成，并先落到本机台账。
     *
     * 内容没变（同板面 / 同基准版本 / 同说明）就沿用同一个未决身份：服务端据此返回
     * **原事实**（不重复创建提交与理解请求）；同一次未决提交重启后也接着用这个身份。
     * 内容变了就是另一次提交，用新身份（拿旧身份去发新内容会被服务端按冲突拒绝）。
     */
    const identity = submissionIdentityFor({
      boardId: boardId.value,
      baseStateVersion: board.value.seq,
      note: "",
      existing: pendingSubmission.value,
    });
    writePendingIdentity(identity);
    /**
     * 理解状态归属于**本次**提交：换一次提交就换一代查询请求，
     * 上一次提交的迟到查询不再落地（也就不会覆盖本次的失败 / 未知）。
     */
    beginUnderstandingForNewSubmission();
    /**
     * 第一段：提交请求本身。**只有这一段能决定「已提交 / 未提交 / 未知」的事实**。
     *
     * 提交之后的读取（板面 / 可见范围 / 任务清单）全部交给 refreshAfterSubmit：
     * 它失败只说明「页面上的事实不新」，绝不改变这一段已经定下的事实（F10）。
     */
    let result: SubmissionResult;
    try {
      const confirmedCheckId =
        confirmedCheck && confirmedCheck.savedSeq === board.value.seq
          ? confirmedCheck.checkId
          : undefined;
      result = await api.submitBoard(
        boardId.value,
        undefined,
        "",
        board.value.seq,
        confirmedCheckId,
        identity.clientRequestId,
        // F6：板面坐标下的当前可见区域 + 当前缩放；任一量不到就是 null（不编造）。
        frozenViewPayload(readBoardArea(), readBoardScale()),
      );
    } catch (err) {
      const status = (err as { status?: number }).status;
      const failure = (err as { payload?: { error?: string; reason?: string } }).payload;
      if (isRequestIdentityConflict(err)) {
        /**
         * 契约 §1.1.3：同身份但内容不同 —— 服务端按冲突拒绝（409 request_identity_conflict）。
         *
         * 这是**明确失败**（不是未知），而且不许拿这个身份去认领任何受理事实。
         * 旧身份已经被服务端拒绝，不能再继续用：下一次提交换一个新身份。
         */
        writePendingIdentity(null);
        submitStatus.value = "failed";
        submitError.value = identityConflictText();
        return null;
      }
      if (submitReceiptUnknown(err)) {
        /**
         * F10 + 契约 §1.3：没有拿到回执 —— 提交结果**未知**。
         *
         * 先用本次请求身份做**精确查证**（唯一查证路径）：
         * - 查到这条身份的可靠受理事实 → 恢复 submissionId / understandingId 并开始理解查询；
         * - 查不到 / 内容不符 / 查询失败 → 继续「结果未知」：保留原内容与勾选，
         *   不盲目重发、不改判失败（§1.1.5）。
         */
        submitStatus.value = "unknown";
        submitResultUnknown.value = true;
        submitError.value = `提交结果未知：没有拿到服务端回执（${(err as Error).message || "原因未知"}）。这次提交可能已经受理，请先确认结果，不要直接重复提交`;
        /**
         * P1：结果未知时不把提升掉的那几条说成「随提交生效」——
         * 该卡对应的草稿已经随刚才的保存被登记清除，这里把草稿候选放回去，与既有的
         * 「保留原内容与勾选」同一口径；下次提交换新身份重来（草稿与正文一致，不再重复提升）。
         */
        await confirmAcceptanceAfterUnclear();
        /**
         * 查证命中时受理事实已经恢复（lastAcceptance / submitNote / 理解状态与查询）；
         * 两种情况都返回 null：本次没有拿到回执对象，状态由 submitStatus 表达。
         * 无论命中与否都把原内容放回草稿候选：查证只回答「受理没受理」，
         * 不影响「用户没点完成编辑的文字必须还能被找到」。
         */
        restorePromotedDraftsAfterUnknown(promotedCandidates);
        return null;
      }
      /**
       * 非「没有回执」的错误（含 409 stale_state）是**明确失败**：服务端已经给出结论，
       * 这次提交没有受理 —— 收掉未决身份，下次提交是一次新提交（新身份）。
       */
      writePendingIdentity(null);
      submitStatus.value = "failed";
      submitError.value =
        status === 409 && failure?.error === "stale_state"
          ? `板面版本已经变化，本次未提交（${failure.reason ?? "服务端已有更新的已保存版本"}）。请确认当前板面后再提交`
          : (err as Error).message;
      // 提交失败保留改动与本次注释选择：这里不刷新、不清勾选、不动基准
      return null;
    }
    // POST 已经明确返回：提交请求的事实到此确定，后面无论读取成败都不再改变它
    lastSubmission.value = result;
    const delivery = result.delivery;
    /**
     * 受理是服务端回执说的真实事实（§14.1 / §14.4）：只有它说 `status: "accepted"` 时，
     * 界面才立刻显示「已受理，正在理解」。旧的 `succeeded` 语义（没有 status 字段）继续兼容，
     * 但它不再兼代表「模型已经理解完成」。
     */
    const acceptedByReceipt = result.status === "succeeded" && delivery?.status === "accepted";
    if (acceptedByReceipt && delivery?.delivered === false) {
      // 服务端明确说这次没有被可靠受理：保留改动与勾选，不推进基准，也不冒充成功
      writePendingIdentity(null);
      submitStatus.value = "failed";
      submitError.value =
        delivery.detail || delivery.reason || "这次提交没有被可靠受理；改动与勾选都保留，可以重试。";
      return null;
    }
    submitStatus.value = result.status;
    // 服务端已经给出明确结论（成功 / 空 / 重复）：这次未决身份到此结束
    writePendingIdentity(null);
    /**
     * P1（契约 4）：这一次有哪几条未点完成编辑的文字真的随提交生效了。
     *
     * 判据是**回执里的本次快照**（before：服务端按已保存板面算出的可见范围）与提升后的
     * 正文逐字一致 —— 只要板面在提交期间被别的版本取代过，这里就不会记它，绝不谎报。
     */
    recordPromotedDrafts(promotedCandidates, result.before as { cards?: { id: string; content: string }[] } | null);
    /**
     * N5：提交成功会让服务端清掉本次提交范围内的勾选。这件事**必须作用于本页候选**，
     * 不能只依赖回读 —— 等待返回期间用户只移动了卡片（未保存候选）时，回读会被候选保护挡住，
     * 于是本页仍带着旧的 checked=true，后续保存又把用户没有重新选择过的注释勾回去。
     * 按对象与选择版本应用：用户等待期间自己重新勾选过的那一条以用户为准。
     */
    applyCheckedCleared(result.checkedCleared, checkedAtSubmit);
    if (result.status === "succeeded") {
      // 受理回执给出的理解请求身份：还没拿到理解记录时也能据此查询 / 重试
      acceptedUnderstandingId.value = delivery?.understandingId ?? null;
      // 本次理解状态绑定到**本次**提交（旧查询的归属判定靠它）
      understandingBoundSubmissionId = result.submission?.id ?? null;
      if (acceptedByReceipt) {
        understandingStatus.value = "accepted";
        // 旧的理解记录（上一次提交留下的）绝不允许冒充本次理解：
        // 本次理解状态从「已受理、还没查到记录」开始，由本次的查询结果补上。
        understanding.value = null;
        submitNote.value =
          "已受理：QIO 已经可靠收下这次提交，正在理解。受理不等于理解完成，也不等于批准执行。";
      }
      understandingNote.value = null;
      understandingError.value = null;
      // 受理成功后立刻查询理解状态：先出现统一状态，说明随查询结果补上
      void startUnderstandingPolling();
    } else {
      submitNote.value = null;
    }
    /**
     * 第二段：提交之后的读取（板面 / 可见范围 / 任务清单）—— **唯一**的提交后读取路径。
     * 它在 refreshAfterSubmit 内部失败时只记「页面刷新失败」，绝不影响上面已定下的提交事实。
     */
    await refreshAfterSubmit(intentsBefore, "session:submission:" + (result.submission?.id ?? Date.now()));
    return result;
  }

  /**
   * 契约 §1.3：未决提交身份的本机台账（按板面一条）。
   *
   * 写台账**先于**发出请求：进程被杀 / 页面重启之后仍然知道「上一次提交用的是哪个身份」，
   * 于是同一次未决提交能沿用同一身份重试（服务端返回原事实，不重复创建）。
   * 本机存储写不进去也不能阻止提交 —— 身份仍在内存里，只是重启后无法复用。
   */
  function writePendingIdentity(identity: SubmissionIdentity | null): void {
    pendingSubmission.value = identity;
    try {
      const key = submissionIdentityStorageKey(boardId.value);
      if (identity) globalThis.localStorage?.setItem(key, serializeSubmissionIdentity(identity));
      else globalThis.localStorage?.removeItem(key);
    } catch {
      // 存储不可用：只影响「重启后复用同一身份」，不影响这次提交的事实表达
    }
  }

  function readPendingIdentity(forBoardId: string): SubmissionIdentity | null {
    try {
      const remembered = parseStoredSubmissionIdentity(
        globalThis.localStorage?.getItem(submissionIdentityStorageKey(forBoardId)),
      );
      return remembered && remembered.boardId === forBoardId ? remembered : null;
    } catch {
      return null;
    }
  }

  /**
   * 重启 / 刷新后恢复未决提交：沿用同一个身份，并立刻按它做一次**精确查证**。
   * 查不到就继续「结果未知」（保留台账，等用户查证或同身份重试），绝不改判失败。
   */
  async function restorePendingSubmission(): Promise<void> {
    const remembered = readPendingIdentity(boardId.value);
    pendingSubmission.value = remembered;
    if (!remembered) return;
    submitStatus.value = "unknown";
    submitResultUnknown.value = true;
    submitNote.value = null;
    acceptanceLookupNote.value = null;
    submitError.value =
      "上一次提交没有拿到回执：本机保留了同一个请求身份，结果仍然未知。请先查证结果，不要直接重复提交。";
    await confirmAcceptanceAfterUnclear();
  }

  /**
   * F10：提交成功之后的读取（板面 / 可见范围 / 任务清单）。
   *
   * 失败**不改变**提交结果：`submitStatus` 保持成功、`lastSubmission` 与基准依据都不动，
   * 只记录 `submitRefreshError`，让界面说「提交成功，页面刷新失败」。
   */
  async function refreshAfterSubmit(intentsBefore: Set<string>, batchKey: string): Promise<boolean> {
    submitRefreshState.value = "pending";
    submitRefreshError.value = null;
    try {
      await refreshBoardFromServer();
      await refreshVisibleRange();
      await refreshIntentFacts();
      // 这次提交之后新出现的意图属于同一批（QIO 对同一次提交给出的多个工作项）
      recordNewIntents(intentsBefore, batchKey);
      submitRefreshState.value = "idle";
      return true;
    } catch (err) {
      submitRefreshState.value = "error";
      submitRefreshError.value = (err as Error).message || "原因未知";
      return false;
    }
  }

  /**
   * F10：只重试**读取**（板面 / 可见范围 / 任务清单），绝不重新提交，
   * 也绝不动「已提交」这个已经成立的事实与基线依据。
   */
  async function retrySubmitRefresh(): Promise<{ ok: boolean; error?: string }> {
    submitRefreshState.value = "pending";
    submitRefreshError.value = null;
    try {
      await refreshBoardFromServer();
      await refreshVisibleRange();
      await refreshIntentFacts();
      submitRefreshState.value = "idle";
      return { ok: true };
    } catch (err) {
      const message = (err as Error).message || "读取服务器状态失败";
      submitRefreshState.value = "error";
      submitRefreshError.value = message;
      return { ok: false, error: message };
    }
  }

  /**
   * N5：按对象与选择版本应用「提交成功清掉的勾选」。
   *
   * - 用户在这次提交等待期间**自己重新勾选**了同一条（代次前进）→ 以用户为准，不动；
   * - 其余被清掉的条目：本页候选与已保存基线一起更新，避免下一次保存把旧勾选写回；
   * - 只清勾选：不提交、不调用 QIO、不自动再提交。
   */
  function applyCheckedCleared(cleared: string[] | undefined, atSeq: Map<string, number>): void {
    const ids = (cleared ?? []).filter((id) => typeof id === "string" && id.length > 0);
    if (!ids.length || !board.value) return;
    let changed = false;
    const cards = board.value.cards.map((card) => {
      if (!ids.includes(card.id)) return card;
      if ((cardCheckedSeq.get(card.id) ?? 0) !== (atSeq.get(card.id) ?? 0)) return card;
      /*
        Phase 2 §5.1：**提交后不清除参与**。旧服务端回执里的 checkedCleared 只是旧兼容字段，
        这里保留参与与 checked 镜像，并记成本地参与选择 —— 后续迟到的读取不能把它改回 excluded。
      */
      if (participationOf(card) === "included") {
        if (card.checked) return card;
        changed = true;
        const next: typeof card = { ...card, checked: true };
        syncLegacyParticipation(next);
        return next;
      }
      if (!card.checked && card.participation === "excluded") return card;
      changed = true;
      cardCheckedSeq.set(card.id, ++checkedMutationSeq);
      localCheckedChoices.delete(card.id);
      const excluded: typeof card = { ...card, checked: false, participation: "excluded" };
      return excluded;
    });
    if (!changed) return;
    const wasClean = boardLocalRev === boardCleanRev;
    board.value = { ...board.value, cards };
    if (wasClean) {
      // 本地与服务器本来就一致：这是服务器已经生效的事实，把干净基线一起前移
      cleanState = cloneState(board.value);
    } else {
      // 仍有未保存候选（例如等待期间只移动了卡片）：候选内容变了，交给既有保存流程送上去
      boardLocalRev += 1;
      confirmedCheck = null;
      pendingConfirm = null;
      if (!pendingImpact.value) scheduleSave();
    }
  }
  async function settleAfterIntentChange() {
    if (dirty.value) await saveNow();
    await refreshBoardFromServer();
    await refreshVisibleRange();
    await refreshIntentFacts();
    await loadExecutions();
  }

  /** 同一意图同一时间只允许一次批准 / 拒绝：重复点击不重复创建、不重复执行。 */
  const decideInFlight = new Set<string>();
  let batchDecideInFlight = false;

  async function approve(intentId: string, confirmDependency = false): Promise<DecideResult> {
    if (decideInFlight.has(intentId)) {
      return { ok: false, reason: "这项批准正在处理中，请等它返回。" };
    }
    /**
     * 防重复操作在**核验之前**取得：失败的核验也要在 finally 里把锁放掉，
     * 否则一次失败会把这一项永久钉死。核验没做成就不放行（不用旧快照批准）。
     */
    decideInFlight.add(intentId);
    try {
      const stale = await ensureFreshIntents();
      if (stale) return { ok: false, reason: stale, detail: stale };
      const result = await api.approveIntent(intentId, confirmDependency);
      // 批准后服务端同步返回真实执行：立刻显示 running，而不是等下一次查询
      adoptExecution(result?.execution ?? null);
      await settleAfterIntentChange();
      return result;
    } finally {
      decideInFlight.delete(intentId);
    }
  }

  async function reject(intentId: string): Promise<DecideResult> {
    if (decideInFlight.has(intentId)) {
      return { ok: false, reason: "这项决定正在处理中，请等它返回。" };
    }
    /** 与 approve 同一口径：锁先取、核验不过就不放行、finally 里释放。 */
    decideInFlight.add(intentId);
    try {
      const stale = await ensureFreshIntents();
      if (stale) return { ok: false, reason: stale, detail: stale };
      const result = await api.rejectIntent(intentId);
      adoptExecution(result?.execution ?? null);
      await settleAfterIntentChange();
      return result;
    } finally {
      decideInFlight.delete(intentId);
    }
  }

  async function decideBatch(approveIds: string[], rejectIds: string[]) {
    if (batchDecideInFlight) {
      throw new Error("批量审批正在处理中，请等它返回再提交下一次。");
    }
    /** 批量同样：锁在核验之前取得，核验失败也不放行，finally 里释放。 */
    batchDecideInFlight = true;
    try {
      const stale = await ensureFreshIntents();
      if (stale) {
        // 旧快照不许放行批量决定：服务端核验没做成 → 暂停，并把真实原因交回界面
        return { results: [], conflicts: [], approved: [], rejected: [], error: stale };
      }
      const result = await api.batchDecide(approveIds, rejectIds);
      for (const item of result?.results ?? []) adoptExecution(item?.execution ?? null);
      await settleAfterIntentChange();
      return result;
    } finally {
      batchDecideInFlight = false;
    }
  }

  async function updatePreview(intentId: string, preview: IntentPreview) {
    const result = await api.updateIntentPreview(intentId, preview);
    await refreshIntentFacts();
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

  /* ------------------------------------------------------------------
   * N3/N4：剩余撤回决定（intent.revert.pendingDecision）的真正执行与关闭
   * ------------------------------------------------------------------ */

  function revertDecisionStateFor(intentId: string): { status: "idle" | "pending" | "error"; error: string | null } {
    return revertDecisionStates.value[intentId] ?? { status: "idle", error: null };
  }

  function setRevertDecisionState(
    intentId: string,
    status: "idle" | "pending" | "error",
    error: string | null = null,
  ): void {
    revertDecisionStates.value = { ...revertDecisionStates.value, [intentId]: { status, error } };
  }

  /** 用户关闭这次提示：保留板面、结束本次打断，**不**执行任何撤回。 */
  function dismissRevertDecision(intentId: string): void {
    if (!intentId) return;
    revertDecisionDismissed.value = { ...revertDecisionDismissed.value, [intentId]: true };
  }

  /** 用户主动「再次查看」：把提示重新放出来（服务端仍是真实未处理的决定）。 */
  function reopenRevertDecision(intentId: string): void {
    if (!intentId) return;
    if (!revertDecisionDismissed.value[intentId]) return;
    const next = { ...revertDecisionDismissed.value };
    delete next[intentId];
    revertDecisionDismissed.value = next;
  }

  /**
   * N3：继续处理明确选择的剩余撤回项。
   *
   * - 只传这次界面上**明确展示**的 decisionIds（N4 的服务端保护会按当前内容重核）；
   * - 只有服务端真的执行了（ok===true）才算成功；失败原样返回真实原因，界面给重试入口；
   * - 仍有剩余待决定项时保留提示；处理完则收起这次提示。
   * - 不重复执行：上一次请求还在飞时直接返回，不叠加请求。
   */
  async function continueRevertDecision(
    intentId: string,
    decisionIds?: string[],
  ): Promise<{ ok: boolean; reason?: string; detail?: string; intent?: Intent }> {
    if (!intentId) return { ok: false, reason: "没有指定要处理的任务" };
    if (revertDecisionStateFor(intentId).status === "pending") {
      return { ok: false, reason: "这次撤回决定正在执行，请等它返回" };
    }
    setRevertDecisionState(intentId, "pending");
    try {
      const result = await api.advanceIntent(intentId, "revert_rest", decisionIds);
      await loadIntents();
      await refreshBoardFromServer();
      await refreshVisibleRange();
      const intent = result.intent ?? intents.value.find((item) => item.id === intentId);
      const remaining = intent?.revert?.pendingDecision?.length ?? 0;
      if (result.ok === false) {
        const reason = result.detail || result.reason || "这次撤回没有执行";
        setRevertDecisionState(intentId, "error", reason);
        return { ok: false, reason, detail: result.detail, intent };
      }
      setRevertDecisionState(intentId, "idle");
      if (remaining > 0) reopenRevertDecision(intentId);
      else dismissRevertDecision(intentId);
      return { ok: true, intent };
    } catch (err) {
      const failure = (err as { payload?: { error?: string; reason?: string; detail?: string } }).payload;
      const reason =
        failure?.detail || failure?.reason || (err as Error).message || "这次撤回没有执行";
      setRevertDecisionState(intentId, "error", reason);
      return { ok: false, reason, detail: failure?.detail };
    }
  }

  /* ------------------------------------------------------------------
   * F3：正式正文的内容冲突（本页候选与服务器同一张卡片都被改过）
   * ------------------------------------------------------------------ */

  /* ------------------------------------------------------------------
   * P1：提交时把「未点完成编辑」的草稿提升为正式正文
   *
   * 根因：草稿只存在草稿通道（board_drafts / 内存 drafts），后端只读 card.content
   * （selectable_cards 与 materials._inline_entry），所以「勾了注释、写了字、没点完成编辑」
   * 的用户在提交快照里是一条**空正文**的卡片，模型什么都看不到。
   *
   * 这里只做一件事：在 **saveNow() 之前**把该卡的 content 改成它当前未确认的草稿正文，
   * 然后照旧走「既有保存 → 版本门 → 影响确认 → 提交」链路；不新增协议、不改载荷形状。
   * ------------------------------------------------------------------ */

  /**
   * 一张卡片当前未确认的草稿正文是不是**新的、值得提升的**内容。
   *
   * 两个条件都要看（与既有草稿口径一致 §10.4 / §11.1）：
   * - 这个键上确实存在一条草稿（内存里有记录，或本机有恢复副本）：空串也是有效草稿；
   * - 它的正文与当前正式正文不同 —— 相同时提升是空操作，不该被算成「随提交生效」。
   */
  function draftDiffersFromContent(cardId: string, content: string): boolean {
    if (!hasCardDraft(cardId)) return false;
    const key = cardDraftKey(cardId);
    const memory = Object.prototype.hasOwnProperty.call(drafts.value, key);
    // 内存里已有这一版（含本机写失败的情况）时以内存为准；否则退回本机恢复副本
    return (memory ? drafts.value[key] : cardDraftText(cardId)) !== content;
  }

  /**
   * 这张卡片这次**会不会**连同它的正文进提交快照。
   *
   * 与后端的可见范围同一口径（agent/interactive/models.visible_except_deleted）：
   * 已删除 / 明确隐藏都进不去；可勾选的**文字注释与结果卡**（CHECKABLE_KINDS）未勾选 = QIO 看不到；
   * 材料（file）默认在范围内。
   *
   * F4：结果卡与文字卡同一套许可语义（默认未勾选，勾选后持续有效）；
   * 为什么不给未勾选的注释提升：那会越过「勾选才作为互动材料」的边界，
   * 而且用户根本没打算把这段草稿交出去 —— 草稿原样留着，等他勾选或自己点完成编辑。
   */
  function cardSubmittedFor(card: { kind: string; checked: boolean; hidden: boolean; deleted: boolean }): boolean {
    if (card.deleted || card.hidden) return false;
    if (CHECKABLE_KINDS.includes(card.kind as (typeof CHECKABLE_KINDS)[number])) return card.checked;
    return true;
  }

  /**
   * 提交前的提升：把每张将进快照的卡片上「未点完成编辑」的草稿正文写进该卡 content，
   * 形成**候选板面**，并登记一次既有语义的草稿清除。
   *
   * 逐条契约：
   * 1) 返回 false 表示「这次不许提交」（有卡片的草稿处于未决冲突），调用方据此停在可读原因上；
   * 2) 未决冲突（draftConflicts：两份来源无法判定新旧 / boardContentConflictFor：正文两边都改过）
   *    的卡片**不提**，草稿与正文都原样保留，留给用户明确选择；
   * 3) 提升成功才 requestDraftClear（与「完成编辑」同一个入口）：真正的清除仍由 saveNow
   *    在「回执对应当前候选且没有更新候选」时按登记版本执行，不新造第二套清理；
   * 4) 有提升才产生新候选（boardLocalRev 前进、dirty、状态：saving、清掉旧的影响确认授权），
   *    于是保存、版本门与影响确认全部沿用既有逻辑。
   */
  function promoteDraftsBeforeSubmit(): {
    blocked: { cardId: string; reason: string }[];
    promoted: { cardId: string; text: string }[];
  } {
    const blocked: { cardId: string; reason: string }[] = [];
    const promoted: { cardId: string; text: string }[] = [];
    if (!board.value) return { blocked, promoted };
    const currentBoard = board.value;
    const nextCards = currentBoard.cards.map((card) => {
      if (!cardSubmittedFor(card)) return card;
      if (!hasCardDraft(card.id)) return card;
      const conflict = draftConflicts.value[cardDraftKey(card.id)];
      if (conflict) {
        blocked.push({
          cardId: card.id,
          reason: "这份草稿与服务器上的草稿哪个更新无法判定（冲突未决），还没有选择保留哪一份",
        });
        return card;
      }
      if (boardContentConflictFor(card.id)) {
        blocked.push({
          cardId: card.id,
          reason: "这张卡的正文在本页与服务器上都被改过（正文冲突未决），还没有选择保留哪一份",
        });
        return card;
      }
      if (!draftDiffersFromContent(card.id, card.content)) return card;
      promoted.push({ cardId: card.id, text: cardDraftText(card.id) });
      return { ...card, content: cardDraftText(card.id) };
    });
    if (blocked.length) return { blocked, promoted: [] };
    if (!promoted.length) return { blocked, promoted: [] };
    board.value = { ...currentBoard, cards: nextCards };
    // 与 commit 同一口径：这是**新的候选**（版本代次前进、待保存、旧的影响确认授权作废）
    boardLocalRev += 1;
    dirty.value = true;
    confirmedCheck = null;
    pendingConfirm = null;
    saveStatus.value = "saving";
    /**
     * 登记清除（既有入口）：saveNow 保存成功后按登记的版本执行 —— 提升后的旧草稿不再复活。
     * 登记版本由 requestDraftClear 自己取当前 draftKeySeq；保存期间用户又输入了更新版本时，
     * saveNow 的版本守卫会作废这次登记（绝不为了清旧稿误删后来的输入）。
     */
    for (const item of promoted) requestDraftClear(cardDraftKey(item.cardId));
    return { blocked, promoted };
  }

  /**
   * P1：提交结果**未知**（没有拿到回执）时，把提升后的草稿放回草稿候选。
   *
   * 为什么：这次提升已经把正文写进候选并保存（保存成功才有清除登记），所以内容没有丢；
   * 但「提交到底受没受理」还不知道，这时如果草稿也没了，用户会看到一次说不清的消失。
   * 恢复走既有 setDraft（版本推进、清除登记回收、本机记录重写都由它按规则处理），
   * 不新造第二套恢复逻辑。下一次提交时这份草稿与正文一致 → 不再重复提升，语义稳定。
   */
  function restorePromotedDraftsAfterUnknown(candidates: { cardId: string; text: string }[]): void {
    promotedDrafts.value = [];
    for (const item of candidates) {
      if (draftFor(cardDraftKey(item.cardId)) === item.text) continue;
      setDraft(cardDraftKey(item.cardId), item.text);
    }
  }

  /**
   * P1：把「回执证明真的进了本次快照」的卡片记成 promotedDrafts。
   *
   * 只有提升过的卡片才可能有条目；正文（含空串）必须与提升时写进候选的那一份**逐字一致**，
   * 所以提交期间板面被别的写入取代时这里会如实不记（不谎报「已生效」）。
   */
  function recordPromotedDrafts(
    candidates: { cardId: string; text: string }[],
    snapshot: { cards?: { id: string; content: string }[] } | null | undefined,
  ): void {
    const cards = snapshot?.cards;
    if (!candidates.length || !Array.isArray(cards)) {
      promotedDrafts.value = [];
      return;
    }
    promotedDrafts.value = candidates
      .filter((item) => cards.some((card) => card.id === item.cardId && card.content === item.text))
      .map((item) => ({ cardId: item.cardId, text: item.text }));
  }

  /** 这张卡片当前的正文冲突（没有则为 null）。界面据此给出「用本页 / 用服务器版」的选择。 */
  function boardContentConflictFor(cardId: string): { local: string; server: string } | null {
    return boardContentConflicts.value[cardId] ?? null;
  }

  /** 用户对内容冲突做出选择：只有明确选择之后才允许继续保存（绝不静默覆盖）。 */
  function resolveBoardContentConflict(cardId: string, choice: "local" | "server"): void {
    const conflict = boardContentConflicts.value[cardId];
    if (!conflict || !board.value) return;
    const remaining = { ...boardContentConflicts.value };
    delete remaining[cardId];
    boardContentConflicts.value = remaining;
    const content = choice === "local" ? conflict.local : conflict.server;
    board.value = {
      ...board.value,
      cards: board.value.cards.map((card) => (card.id === cardId ? { ...card, content } : card)),
    };
    if (choice === "server" && JSON.stringify(board.value) === JSON.stringify(cleanState)) {
      // 选服务器版之后候选与服务器事实一致：没有要保存的候选了
      boardLocalRev += 1;
      boardCleanRev = boardLocalRev;
      dirty.value = false;
      saveStatus.value = "saved";
      saveError.value = null;
      return;
    }
    boardLocalRev += 1;
    dirty.value = true;
    confirmedCheck = null;
    pendingConfirm = null;
    saveStatus.value = "saving";
    saveError.value = null;
    scheduleSave();
  }

  /* ------------------------------------------------------------------
   * F07：本地删除（解散组 / 删除关联）与服务器修改相撞
   * ------------------------------------------------------------------ */

  /** 这个对象（组 / 关联）当前的删除冲突依据；没有则为 null。 */
  function boardRemovalConflictFor(id: string): { kind: "group" | "link"; server: Record<string, unknown> } | null {
    const item = boardRemovalConflicts.value[id];
    return item ? { kind: item.kind, server: item.server } : null;
  }

  /**
   * 用户对「删除 vs 对方修改」做出明确选择：
   * - `keep-deleted`：保持删除（用户明确放弃对方这次修改）；
   * - `restore-server`：恢复服务器版（把那一条按服务器事实放回候选）。
   * 两种都只改本页候选并排一次保存，不调用 QIO；未选之前保存一直被挡住。
   */
  function resolveBoardRemovalConflict(id: string, choice: "keep-deleted" | "restore-server"): void {
    const conflict = boardRemovalConflicts.value[id];
    if (!conflict || !board.value) return;
    const remaining = { ...boardRemovalConflicts.value };
    delete remaining[id];
    boardRemovalConflicts.value = remaining;
    if (choice === "restore-server") {
      const restored = cloneState(conflict.server) as unknown as { id: string };
      if (conflict.kind === "group") {
        const groups = board.value.groups ?? [];
        if (!groups.some((item) => item.id === id)) {
          board.value = { ...board.value, groups: [...groups, restored as never] };
        }
      } else {
        const links = board.value.links ?? [];
        if (!links.some((item) => item.id === id)) {
          board.value = { ...board.value, links: [...links, restored as never] };
        }
      }
    }
    boardLocalRev += 1;
    dirty.value = true;
    confirmedCheck = null;
    pendingConfirm = null;
    saveStatus.value = "saving";
    saveError.value = null;
    if (!pendingImpact.value) scheduleSave();
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

/**
   * 读取当前的影响确认。
   *
   * 放进函数里是必要的：`confirmImpact` 里先执行了 `pendingImpact.value = null`，
   * 直接再读会命中 TypeScript 的控制流收窄（判成 null/never），
   * 而这里要的正是「await 之后真实的最新值」。
   */
  function peekPendingImpact() {
    return pendingImpact.value;
  }

  /**
   * N2：把这次影响确认的**真实结果**如实说出来（成功 / 仍待确认 / 检查失败 / 保存失败 / 未落库）。
   * 绝不因为 Promise 正常返回就宣称「已保存、任务已暂停」。
   */
  function summarizeImpactConfirm(extraReason?: string): ImpactConfirmResult {
    if (pendingImpact.value) {
      return {
        outcome: "needs_confirm",
        reason: pendingImpact.value.note ?? "这次改动还没有保存，正在等你确认影响范围",
      };
    }
    if (impactCheckError.value) return { outcome: "check_failed", reason: impactCheckError.value };
    if (saveStatus.value === "error") {
      return { outcome: "save_failed", reason: saveError.value ?? "保存没有完成，原因未知" };
    }
    if (saveStatus.value === "saved" && !dirty.value) {
      return { outcome: "saved", paused: [...lastSavePaused.value] };
    }
    if (dirty.value) {
      return {
        outcome: "superseded",
        reason: extraReason ?? "板面又有更新的改动接手了这次保存，已按最新版本重新核实",
      };
    }
    return { outcome: "superseded", reason: extraReason ?? "这次确认没有对应的落库结果，已重新核实" };
  }

  /** 用户确认：改动生效，受影响的任务会暂停并保留进度。 */
  async function confirmImpact(): Promise<ImpactConfirmResult> {
    const pending = pendingImpact.value;
    pendingImpact.value = null;
    if (pending && pending.previewRev !== boardLocalRev) {
      /**
       * 等待确认期间板面又出现了新的候选（08 路径2）：这次说明只覆盖当时那一版，
       * 确认不能放行未说明的改动 —— 重新核实影响，再把新的说明交给用户。
       *
       * N2：这条「已重新核实」的事实写在**新的确认说明**里（note），不再挂在
       * impactCheckError 上 —— 否则界面会一边说「未保存未暂停」、一边还留着上一次的预判失败。
       */
      impactConfirmed = false;
      await saveNow();
      const recheckNote = "等待确认期间板面又有改动，已按最新版重新核实这次改动的影响，请重新确认";
      const rechecked = peekPendingImpact();
      if (rechecked && !rechecked.note) {
        pendingImpact.value = { ...rechecked, note: recheckNote };
      }
      return summarizeImpactConfirm(recheckNote);
    }
    impactConfirmed = true;
    impactCheckError.value = null;
    if (pending?.checkId) {
      pendingConfirm = { checkId: pending.checkId, stateVersion: pending.stateVersion };
    }
    await saveNow();
    return summarizeImpactConfirm();
  }

  /** 用户取消：这次未确认的候选放弃（07），回到服务器已保存状态；执行中的任务继续。 */
  async function cancelImpact(): Promise<void> {
    impactConfirmed = false;
    pendingImpact.value = null;
    impactCheckError.value = null;
    // 07：取消 = 这次板面变更不生效，那么它带来的草稿清除也不该落地
    pendingDraftClears.clear();
    syncPendingDraftClearKeys();
    // F07：候选整块退回已保存事实，本地删除也随之作废 —— 删除冲突不再成立
    boardRemovalConflicts.value = {};
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    confirmedCheck = null;
    pendingConfirm = null;
    /**
     * F1：取消必须**同步**把板面候选退回「上一次与服务器一致」的那一版，不能等回读。
     *
     * 基线的真实缺陷：取消只收回了版本记账，板面里还留着被取消的正文；回读在飞期间用户
     * 移动 / 勾选 / 新增，那些独立改动都是从**尚未恢复的板面**复制的 —— 取消的正文于是又进了
     * 新的候选（甚至可能被真的保存回服务器）。回读只是把撤回落到服务器事实上确认，不是撤回本身。
     *
     * 退回用的是 cleanState（上一次保存/读取确认过的事实），不是猜测：
     * - 被取消的正文 / 删除表达立刻消失；
     * - 等待期间的独立改动基于**已恢复**的板面，保留下来；
     * - 回读失败时如实说明「撤回还没被服务器确认」，给出重试入口，不假装撤回成功；
     * - 卡片编辑草稿与恢复来源仍不在这里清理（07：正式变更成功后才清对应版本）。
     */
    if (cleanState) board.value = cloneState(cleanState);
    // 取消丢弃的是整个候选：这期间用户做的勾选选择也一并作废（以撤回后的板面为准）
    if (board.value) dropCheckedChoicesNotIn(board.value);
    boardLocalRev = boardCleanRev;
    dirty.value = false;
    saveStatus.value = "idle";
    saveError.value = null;
    cancelRecovery.value = { boardId: boardId.value, active: true, reason: null };
    try {
      const applied = await refreshBoardFromServer();
      if (!applied) settleCancelRecovery(false, "撤回还没有被服务器事实确认（板面已经切换）");
    } catch (err) {
      settleCancelRecovery(
        false,
        "撤回还没有被服务器事实确认：" + ((err as Error).message || "读取服务器状态失败"),
      );
    }
  }

  /** F1：撤回还没被服务器事实确认时的重试入口（界面据此提供「重新核对」）。 */
  async function retryCancelRecovery(): Promise<{ ok: boolean; error?: string }> {
    try {
      const applied = await refreshBoardFromServer();
      if (!applied) {
        settleCancelRecovery(false, "撤回还没有被服务器事实确认（板面已经切换）");
        return { ok: false, error: "没有取到当前板面的最新事实" };
      }
      return { ok: true };
    } catch (err) {
      const message = (err as Error).message || "读取服务器状态失败";
      settleCancelRecovery(false, "撤回还没有被服务器事实确认：" + message);
      return { ok: false, error: message };
    }
  }

  function dismissMaterialPaused(): void {
    materialPaused.value = [];
  }

  /** 保存前的只读预判（给组件用；不改任何状态）。走 M4 定稿的 impact-check。 */
  async function checkMaterialImpact(state: BoardState) {
    return runImpactCheck(state.seq, state);
  }

  /* ------------------------------------------------------------------
   * 真实理解（契约 §14.1 / §14.6）
   * ------------------------------------------------------------------ */

  /**
   * 能力探测：服务替身可能只实现了一部分接口。缺少新接口时如实降级为「没有这份数据」，
   * 不抛错、不假装有数据，也不影响保存与提交这些既有路径。
   */
  function apiSupports(name: string): boolean {
    // 服务替身（测试里只实现一部分接口的 mock）对**未声明的导出**会直接抛错，
    // 而不是返回 undefined —— 所以能力探测必须包一层：缺少新接口时如实降级为
    // 「没有这份数据」，绝不能让保存 / 提交这些既有路径炸掉（契约 §14.6）。
    try {
      return typeof (api as unknown as Record<string, unknown>)[name] === "function";
    } catch {
      return false;
    }
  }

  function sortUnderstandings(list: Understanding[]): Understanding[] {
    return [...list].sort((a, b) => {
      const at = Date.parse(String(a?.createdAt ?? "")) || 0;
      const bt = Date.parse(String(b?.createdAt ?? "")) || 0;
      if (at !== bt) return bt - at;
      return String(b?.id ?? "").localeCompare(String(a?.id ?? ""));
    });
  }

  /**
   * 理解状态必须**归属本次提交**（契约 §3 / 任务书 3）：
   *
   * - `understandingEpoch`：每开始一次新提交就 +1。上一代（上一次提交）的查询返回时
   *   代次已经不同 → **不落地**，所以「上一次理解完成」绝不会覆盖本次的失败 / 未知；
   * - `understandingBoundSubmissionId`：本次理解状态绑定的是哪一次提交。查询结果属于
   *   别的提交时不许冒充本次（受理与理解都必须对得上身份）。
   */
  let understandingEpoch = 0;
  let understandingBoundSubmissionId: string | null = null;

  /** 本次理解状态归属的提交 id：本次受理回执 / 精确查证恢复的身份优先，其次最近一次回执。 */
  function understandingSubmissionId(): string | undefined {
    return understandingBoundSubmissionId ?? lastSubmission.value?.submission?.id ?? undefined;
  }

  /**
   * 开始一次新的提交：换一代查询请求，并把上一次提交的理解状态从「本次」上摘下来
   * （understandingStatus / understandingError / acceptedUnderstandingId 归位到本次请求）。
   *
   * 注意：只在**确认要发出这次提交**之后调用 —— 被保存 / 影响确认挡住的提交什么都没发生，
   * 不能把上一次提交的理解状态清掉。
   */
  function beginUnderstandingForNewSubmission(): void {
    understandingEpoch += 1;
    understandingBoundSubmissionId = null;
    stopUnderstandingPolling();
    understanding.value = null;
    understandingStatus.value = null;
    understandingError.value = null;
    understandingNote.value = null;
    acceptedUnderstandingId.value = null;
  }

  /** 当前这份「提交 / 理解事实」属于哪个板面：板面一换，这些事实就不属于当前板面。 */
  let statusBoardId = boardId.value;

  /**
   * 板面切换：把上一个板面的提交 / 理解事实整块归零（任务书 §5.7）。
   *
   * 只清「显示与查询」的状态：理解查询代次前进，因此迟到的理解回执再也落地不了；
   * 板面内容、草稿、勾选、执行记录都不动 —— 它们各自由对应板面的读取负责。
   */
  function resetSubmissionStatusForBoard(nextBoardId: string): void {
    statusBoardId = nextBoardId;
    understandingEpoch += 1;
    understandingBoundSubmissionId = null;
    stopUnderstandingPolling();
    understanding.value = null;
    understandings.value = [];
    understandingStatus.value = null;
    understandingError.value = null;
    understandingNote.value = null;
    acceptedUnderstandingId.value = null;
    submitNote.value = null;
    submitStatus.value = "idle";
    submitError.value = null;
    submitResultUnknown.value = false;
    submitRefreshState.value = "idle";
    submitRefreshError.value = null;
    submitAcceptedByLookup.value = false;
    acceptanceLookupNote.value = null;
    lastAcceptance.value = null;
    lastSubmission.value = null;
    /**
     * 板面身份切换：上一个板面的意图视图不属于这里（F02.8）。
     * 代次前进让还在飞的旧读取落地不了；新板面由随后的读取重新建立事实。
     */
    intentViewsSeq += 1;
    intentViews.value = [];
    intentViewsLoadedOnce.value = false;
    intentViewsLoadState.value = "idle";
    intentViewsLoadError.value = null;
    intentViewsSource.value = "";
  }

  /** 把一次查询结果落成界面事实：最近一次 + 历史，不做任何推断。 */
  function adoptUnderstandings(
    payload: UnderstandingResponse | null | undefined,
    submissionId?: string,
  ): Understanding | null {
    const list = sortUnderstandings(payload?.understandings ?? []);
    if (submissionId) {
      /**
       * 归属校验：调用方给了 submissionId，就**只**采纳 submissionId 精确匹配的那条。
       *
       * 旧实现在匹配不到时会退回 `list[0]`（按 createdAt 最新的那条）——那正好让
       * **上一次提交**的理解记录冒充本次理解（Lead 核对点 1）。现在匹配不到就是
       * `understanding = null`（历史仍列出），本次状态保持「已受理、还没查到本次记录」，
       * 绝不改成上一次的终态。
       */
      const direct =
        payload?.understanding && payload.understanding.submissionId === submissionId
          ? payload.understanding
          : null;
      const owned = list.find((item) => item.submissionId === submissionId) ?? null;
      const recent = direct ?? owned;
      understandings.value = list.length ? list : recent ? [recent] : [];
      understanding.value = recent;
      if (recent) {
        understandingStatus.value = recent.status ?? null;
        acceptedUnderstandingId.value = recent.id;
      }
      return recent;
    }
    const recent = payload?.understanding ?? list[0] ?? null;
    understandings.value = list.length ? list : recent ? [recent] : [];
    understanding.value = recent;
    understandingStatus.value = recent?.status ?? null;
    if (recent) acceptedUnderstandingId.value = recent.id;
    return recent;
  }

  /**
   * 读取本板面的理解过程（可按提交过滤）；失败时如实记录原因。
   *
   * `epoch` 是发起这次查询时的请求代次：返回时代次已经前进（说明期间开始了新的提交），
   * 这次结果就是**迟到**的旧查询 —— 不落地，绝不覆盖新请求的理解状态（任务书 3）。
   */
  async function loadUnderstanding(
    submissionId?: string,
    epoch: number = understandingEpoch,
  ): Promise<Understanding | null> {
    if (!apiSupports("fetchUnderstanding")) return null;
    const payload = await api.fetchUnderstanding(boardId.value, submissionId);
    if (epoch !== understandingEpoch) return null;
    const found = adoptUnderstandings(payload, submissionId);
    understandingError.value = null;
    return found;
  }

  /**
   * 用最近一次提交的身份查询理解状态。受理情况不明时**先查询确认**，
   * 不新建请求身份、不盲目重发提交。
   */
  async function refreshUnderstanding(): Promise<Understanding | null> {
    const submissionId = understandingSubmissionId();
    let found: Understanding | null = null;
    try {
      found = await loadUnderstanding(submissionId);
    } catch (err) {
      understandingError.value = (err as Error).message;
      return null;
    }
    /**
     * F02.5：刷新理解只更新**理解事实**本身（不重复创建意图，也不在这里额外读取意图清单）。
     * 意图清单的刷新由调用方决定：页面「刷新理解状态」会随后调用 refreshAfterIntentAction()，
     * 重试理解路径在 retryUnderstanding 里显式刷新 —— 同一处维护，不重复请求。
     */
    return found;
  }

  /**
   * 契约 §1.3：受理情况不明时，**只认这一次提交身份在服务端的精确记录**。
   *
   * 唯一查证路径：有本次 `clientRequestId` → `fetchAcceptance`（精确身份）→ 命中才认受理。
   * 这条路径**没有**任何时间推断：按 `createdAt` 接近 + 排除上一次 submissionId 的
   * 「认领」逻辑已经删除（§1.1.4：时间接近 / 最近一条理解记录都不能单独作为受理证据）。
   *
   * - 命中（身份相同、内容依据一致）：恢复 submissionId / delivery / understandingId，
   *   按**这一次**的理解请求开始查询；
   * - 404 / 内容不一致 / 没有可用身份 / 查询失败：返回 null，由调用方继续「结果未知」——
   *   保留原内容与勾选，不盲目重发，也不改判失败（§1.1.5）。
   */
  async function confirmAcceptanceAfterUnclear(): Promise<AcceptanceLookup | null> {
    const identity = pendingSubmission.value;
    acceptanceLookupNote.value = null;
    if (!canLookupAcceptance(identity)) {
      // 没有可用的请求身份就没有精确证据：不许拿时间接近 / 最近一条记录去推断受理
      return null;
    }
    if (!apiSupports("fetchAcceptance")) {
      acceptanceLookupNote.value = acceptanceLookupText({
        confirmed: false,
        reason: "当前服务端没有可用的精确查证接口",
      });
      return null;
    }
    acceptanceLookupPending.value = true;
    /**
     * 这次查证属于**发起时那一次提交**（任务书 §5.7）。返回时只要已经换过一代提交
     * （用户重新提交 / 换过板面），或正在提交新的一次，这次回执就是迟到的：
     * 不落地 —— 迟到的受理事实绝不许覆盖更新的提交状态与说明。
     */
    const lookupEpoch = understandingEpoch;
    const lookupBoardId = boardId.value;
    let payload: { acceptance: AcceptanceLookup | null; reason?: string };
    try {
      payload = await api.fetchAcceptance(boardId.value, identity!.clientRequestId);
    } catch (err) {
      acceptanceLookupPending.value = false;
      // 查询失败不是失败结论：继续「结果未知」，保留原内容与勾选
      acceptanceLookupNote.value = acceptanceLookupText({
        confirmed: false,
        reason: `查证没有完成（${(err as Error).message || "原因未知"}）`,
      });
      return null;
    }
    acceptanceLookupPending.value = false;
    if (
      lookupEpoch !== understandingEpoch ||
      boardId.value !== lookupBoardId ||
      submitStatus.value === "submitting"
    ) {
      return null;
    }
    const acceptance = payload?.acceptance ?? null;
    if (!acceptanceMatchesIdentity(identity, acceptance)) {
      // 查不到，或者查到的记录与这次请求的内容不一致 —— 都不是这次受理的证据
      acceptanceLookupNote.value = acceptanceLookupText({
        confirmed: false,
        reason: acceptance ? "服务端有这条请求身份的记录，但内容与这次请求不一致" : payload?.reason,
      });
      return null;
    }
    lastAcceptance.value = acceptance;
    /**
     * 记录确实存在，但服务端明确说这次**没有被可靠受理**（`delivered: false`）：
     * 与回执路径同一口径 —— 如实算失败、不冒充成功、不推进基准；身份已经有结论，
     * 收掉本机台账（下一次提交是一次新提交）。理由原样带到界面。
     */
    if (acceptance.delivery?.delivered === false) {
      writePendingIdentity(null);
      submitStatus.value = "failed";
      submitResultUnknown.value = false;
      submitError.value =
        acceptance.delivery.detail ||
        acceptance.delivery.reason ||
        "服务端按这次请求身份给出的结论是：没有被可靠受理；改动与勾选都保留，可以重试。";
      return null;
    }
    /**
     * 精确命中：恢复这次提交的事实（submissionId / delivery / understandingId），
     * 并把理解状态绑定到**这一次**提交，然后按它开始查询。
     */
    submitAcceptedByLookup.value = true;
    submitStatus.value = "succeeded";
    submitResultUnknown.value = false;
    submitError.value = null;
    acceptedUnderstandingId.value = acceptance.understandingId ?? acceptance.delivery?.understandingId ?? null;
    understandingBoundSubmissionId = acceptance.submissionId;
    understanding.value = null;
    understandingStatus.value = acceptance.understandingStatus ?? acceptance.delivery?.status ?? "accepted";
    understandingError.value = null;
    understandingNote.value = null;
    submitNote.value = acceptanceLookupText({ confirmed: true, replayed: acceptance.replayed });
    // 这次未决身份已经有明确结论：收掉本机台账（重放由服务端的原记录负责）
    writePendingIdentity(null);
    // 恢复板面 / 可见范围 / 清单事实：这是只读路径，失败只记「刷新失败」，不影响已受理事实
    await refreshAfterSubmit(intentIdSet(), "session:submission:" + acceptance.submissionId);
    void startUnderstandingPolling();
    return acceptance;
  }

  const UNDERSTANDING_POLL_MS = 1000; // 800~1500ms：够及时，也不在窄窗口里空转
  const UNDERSTANDING_POLL_MAX = 90;
  let understandingTimer: ReturnType<typeof setTimeout> | null = null;
  let understandingPollToken = 0;
  let understandingPollAttempts = 0;
  let understandingVisibilityHooked = false;

  function pageVisible(): boolean {
    if (typeof document === "undefined") return true;
    return document.visibilityState !== "hidden";
  }

  /** 停止自动查询；`note` 是给用户看的如实说明（不是错误）。 */
  function stopUnderstandingPolling(note: string | null = null): void {
    understandingPollToken += 1;
    if (understandingTimer !== null) {
      clearTimeout(understandingTimer);
      understandingTimer = null;
    }
    understandingPolling.value = false;
    if (note) understandingNote.value = note;
  }

  function scheduleUnderstandingPoll(): void {
    if (typeof window === "undefined") return;
    // 与既有保存计时器同一套类型（ReturnType<typeof setTimeout>）：环境里可能是 number
    understandingTimer = setTimeout(() => {
      understandingTimer = null;
      void pollUnderstandingOnce();
    }, UNDERSTANDING_POLL_MS);
  }

  /** 页面不可见时停轮询；重新可见后从当前事实继续（不补跑、不造假进度）。 */
  function resumeUnderstandingWhenVisible(token: number): void {
    if (typeof document === "undefined" || understandingVisibilityHooked) return;
    understandingVisibilityHooked = true;
    const onVisible = () => {
      if (!pageVisible()) return;
      document.removeEventListener("visibilitychange", onVisible);
      understandingVisibilityHooked = false;
      if (token !== understandingPollToken) return;
      if (understandingSettled(understandingStatus.value)) return;
      understandingNote.value = null;
      understandingPolling.value = true;
      scheduleUnderstandingPoll();
    };
    document.addEventListener("visibilitychange", onVisible);
  }

  async function pollUnderstandingOnce(): Promise<void> {
    const token = understandingPollToken;
    const epoch = understandingEpoch;
    if (token !== understandingPollToken) return;
    if (!pageVisible()) {
      understandingPolling.value = false;
      understandingNote.value = "页面不在前台：已暂停自动查询，回到页面会继续。";
      resumeUnderstandingWhenVisible(token);
      return;
    }
    if (understandingPollAttempts >= UNDERSTANDING_POLL_MAX) {
      stopUnderstandingPolling("自动查询已停止：结果还没有回来，可以点「刷新理解状态」继续查询。");
      return;
    }
    understandingPollAttempts += 1;
    try {
      // 本次提交的理解查询按**本次**受理回执的身份查（不是「最近一次 lastSubmission」）
      await loadUnderstanding(understandingSubmissionId(), epoch);
    } catch (err) {
      understandingError.value = (err as Error).message;
    }
    if (token !== understandingPollToken) return;
    if (understandingSettled(understandingStatus.value)) {
      stopUnderstandingPolling();
      if (understandingStatus.value === "understood") await syncIntentsFromUnderstanding();
      return;
    }
    scheduleUnderstandingPoll();
  }

  /** 开始查询理解状态（受理成功后立刻调用；已在查询或已结束时什么都不做）。 */
  async function startUnderstandingPolling(): Promise<void> {
    if (understandingSettled(understandingStatus.value)) return;
    if (understandingPolling.value) return;
    if (!apiSupports("fetchUnderstanding")) return;
    understandingNote.value = null;
    understandingError.value = null;
    understandingPolling.value = true;
    understandingPollAttempts = 0;
    understandingPollToken += 1;
    scheduleUnderstandingPoll();
  }

  /** 同一理解请求只拉一次意图；服务端按 intent_key 也不会复制任务。 */
  const syncedUnderstandings = new Set<string>();
  let intentsSyncInFlight = false;

  async function syncIntentsFromUnderstanding(): Promise<number> {
    const id = understanding.value?.id;
    if (!id || understandingStatus.value !== "understood") return 0;
    if (syncedUnderstandings.has(id) || intentsSyncInFlight) return 0;
    if (!apiSupports("createIntents")) return 0;
    const submissionId = understanding.value?.submissionId ?? lastSubmission.value?.submission?.id;
    intentsSyncInFlight = true;
    const before = intentIdSet();
    try {
      await api.createIntents(boardId.value, submissionId ?? undefined);
      syncedUnderstandings.add(id);
      await loadIntents();
      // F02.2：理解迟到返回时，新意图必须立刻出现在界面上（不要求重新进入页面）
      await loadIntentViews();
      recordNewIntents(before, "session:submission:" + (submissionId ?? id));
      return understandingIntents.value.length;
    } catch (err) {
      understandingError.value = (err as Error).message;
      return 0;
    } finally {
      intentsSyncInFlight = false;
    }
  }

  /**
   * 重试失败的理解：只从该请求保存的快照恢复（服务端负责），
   * 前端先查询确认，不新建请求身份、不盲目重发提交。
   */
  async function retryUnderstanding(
    understandingId?: string,
  ): Promise<{ ok: boolean; reason?: string }> {
    // 一律先查询确认（服务端事实优先）：本地记的是失败，也可能已经被别处重试过
    const found = await refreshUnderstanding();
    let id = found?.id ?? understandingId ?? understanding.value?.id ?? acceptedUnderstandingId.value ?? null;
    if (!found && !id) {
      return { ok: false, reason: "还没有查到这次提交的理解请求：先确认受理情况，再决定是否重试。" };
    }
    if (found && found.status !== "failed") {
      return {
        ok: false,
        reason: "这次理解不在失败状态，不需要重试（当前：" + understandingStatusLabel(found.status) + "）。",
      };
    }
    if (!id) return { ok: false, reason: "还没有可重试的理解请求。" };
    if (!apiSupports("retryUnderstanding")) {
      return { ok: false, reason: "当前没有可用的重试入口，请稍后再试。" };
    }
    const result = await api.retryUnderstanding(id);
    await refreshUnderstanding();
    if (understandingStatus.value && !understandingSettled(understandingStatus.value)) {
      void startUnderstandingPolling();
    }
    // 重试之后意图事实也可能变化：同一处刷新（读取失败保留旧内容）
    await refreshIntentFacts();
    return { ok: result?.ok !== false, reason: result?.reason ?? result?.detail };
  }

  /* ------------------------------------------------------------------
   * 真实执行（契约 §14.1 / §14.6）：状态与操作都取服务端执行记录
   * ------------------------------------------------------------------ */

  const EXECUTION_POLL_MS = 1200;
  /**
   * F02.4：执行轮询顺带刷新意图视图（每 N 次一次）。
   * 复用同一个计时器，不新增第四套轮询；任务全部结束后会停下。
   */
  const INTENT_VIEWS_POLL_EVERY = 3;
  const EXECUTION_POLL_MAX = 150;
  let executionTimer: ReturnType<typeof setTimeout> | null = null;
  let executionPollToken = 0;
  let executionPollAttempts = 0;
  let executionVisibilityHooked = false;

  function stopExecutionPolling(): void {
    executionPollToken += 1;
    if (executionTimer !== null) {
      clearTimeout(executionTimer);
      executionTimer = null;
    }
    executionPolling.value = false;
  }

  function scheduleExecutionPoll(): void {
    if (typeof window === "undefined") return;
    executionTimer = setTimeout(() => {
      executionTimer = null;
      void pollExecutionsOnce();
    }, EXECUTION_POLL_MS);
  }

  function resumeExecutionsWhenVisible(token: number): void {
    if (typeof document === "undefined" || executionVisibilityHooked) return;
    executionVisibilityHooked = true;
    const onVisible = () => {
      if (!pageVisible()) return;
      document.removeEventListener("visibilitychange", onVisible);
      executionVisibilityHooked = false;
      if (token !== executionPollToken) return;
      if (!liveExecutions.value.length) return;
      executionPolling.value = true;
      scheduleExecutionPoll();
    };
    document.addEventListener("visibilitychange", onVisible);
  }

  async function pollExecutionsOnce(): Promise<void> {
    const token = executionPollToken;
    if (token !== executionPollToken) return;
    if (!apiSupports("fetchExecutions")) return;
    if (!pageVisible()) {
      executionPolling.value = false;
      resumeExecutionsWhenVisible(token);
      return;
    }
    if (executionPollAttempts >= EXECUTION_POLL_MAX) {
      stopExecutionPolling();
      executionError.value = "自动查询执行记录已停止：可以重新打开任务浮层查看最新状态。";
      return;
    }
    executionPollAttempts += 1;
    try {
      const payload = await api.fetchExecutions(boardId.value);
      mergeExecutions(payload?.executions ?? []);
      executionError.value = null;
    } catch (err) {
      executionError.value = (err as Error).message;
    }
    if (token !== executionPollToken) return;
    // F02.4：执行中/结束后及时更新意图状态与任务记录（与执行记录同一个轮询节奏）
    void loadIntentViews({ throttleMs: 800 });
    // 成功 / 失败 / 取消都算结束：没有还在推进的记录就停下，不出现「已批准一直运行中」
    if (!liveExecutions.value.length) {
      stopExecutionPolling();
      await refreshIntentFacts();
      return;
    }
    scheduleExecutionPoll();
  }

  function startExecutionPolling(): void {
    if (executionPolling.value) return;
    if (!liveExecutions.value.length) return;
    if (!apiSupports("fetchExecutions")) return;
    executionPolling.value = true;
    executionPollAttempts = 0;
    executionPollToken += 1;
    scheduleExecutionPoll();
  }

  /** 合并意图列表自带的执行记录（按 id 取更新的那份，不覆盖更新的本地事实）。 */
  function mergeExecutions(incoming: Execution[]): void {
    const byId = new Map(executions.value.map((item) => [item.id, item]));
    for (const item of incoming) {
      if (!item?.id) continue;
      const current = byId.get(item.id);
      if (!current) {
        byId.set(item.id, item);
        continue;
      }
      const currentAt = Date.parse(String(current.updatedAt ?? "")) || 0;
      const incomingAt = Date.parse(String(item.updatedAt ?? "")) || 0;
      if (incomingAt >= currentAt) byId.set(item.id, item);
    }
    executions.value = [...byId.values()];
  }

  /** 读取本板面的执行记录；读完之后只在确有排队 / 运行的任务时才继续查询。 */
  async function loadExecutions(): Promise<void> {
    if (!apiSupports("fetchExecutions")) return;
    try {
      const payload = await api.fetchExecutions(boardId.value);
      // 合并而不是整体替换：刚同步拿到的执行记录不该被一次旧/空的列表抹掉（记录只追加）
      mergeExecutions(payload?.executions ?? []);
      executionError.value = null;
      startExecutionPolling();
    } catch (err) {
      executionError.value = (err as Error).message;
    }
  }

  /**
   * 采纳服务端**同步返回**的一次执行记录（批准 / 暂停 / 继续 / 取消都会给）。
   * 这样界面立刻显示真实状态（例如批准后就是 running），不必等下一次轮询；
   * execution 为 null / 缺失时什么都不做 —— 绝不自己造一条执行记录。
   */
  function adoptExecution(execution?: Execution | null): void {
    if (!execution?.id) return;
    mergeExecutions([execution]);
    startExecutionPolling();
  }

  /** 这个意图最近一次真实执行（先看执行记录，再看意图列表里带的那份）。 */
  function latestExecution(intentId: string): Execution | null {
    const list = executionsByIntent.value[intentId] ?? [];
    if (list.length) return list[list.length - 1] ?? null;
    return intents.value.find((item) => item.id === intentId)?.execution ?? null;
  }

  const executionControlInFlight = new Set<string>();

  /**
   * 暂停 / 继续 / 取消的真实入口：同一意图同一时间只发一次（重复点击不重复执行），
   * 结果以服务端返回的执行记录为准。
   */
  async function controlExecution(
    intentId: string,
    action: "pause" | "resume" | "cancel",
  ): Promise<ExecutionControlResult> {
    if (executionControlInFlight.has(intentId)) {
      return { ok: false, reason: "这项任务的控制操作正在处理中，请等它返回。" };
    }
    const call =
      action === "pause" ? api.pauseIntent : action === "resume" ? api.resumeIntent : api.cancelIntent;
    if (typeof call !== "function") {
      return { ok: false, reason: "当前没有可用的执行控制入口，请稍后再试。" };
    }
    executionControlInFlight.add(intentId);
    try {
      const result = await call(intentId);
      // 暂停 / 继续 / 取消都会同步返回执行记录：立即采纳，界面不等下一次轮询
      adoptExecution(result?.execution ?? null);
      await settleAfterIntentChange();
      return result;
    } catch (err) {
      executionError.value = (err as Error).message;
      return { ok: false, reason: (err as Error).message };
    } finally {
      executionControlInFlight.delete(intentId);
    }
  }

  const pauseExecution = (intentId: string) => controlExecution(intentId, "pause");
  const resumeExecution = (intentId: string) => controlExecution(intentId, "resume");
  const cancelExecution = (intentId: string) => controlExecution(intentId, "cancel");

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
    /** 阶段三：聊天发送要携带的互动绑定（mode/boardId/submissionId） */
    chatBinding,
    submitStatus,
    submitError,
    /** 四种状态分开的合成视图（受理 / 理解中 / 理解完成三态 / 提交失败） */
    submissionStage,
    /** 回执丢失但查询确认受理时的如实说明 */
    submitNote,
    /**
     * 说明的阶段归属（任务书 §5.2 / §5.3）：false = 理解已经有结论，
     * 这条说明只是历史，界面不得再把它与当前状态并列显示。
     */
    submitNoteOwnsCurrentStage,
    understandingNoteOwnsCurrentStage,
    visibleRange,
    /** §5.1：本地按 participation 推导的可见范围（提交前预览用；权威以服务端为准） */
    participationRange,
    intents,
    conflicts,
    batchAvailable,
    recoverNotice,
    intentsLoadState,
    intentsLoadError,
    intentsLoadedOnce,
    ensureFreshIntents,
    /** F02：新链意图视图（页面 / 意图层 / 任务面板 / 恢复入口的唯一数据源） */
    intentViews,
    intentViewsLoadState,
    intentViewsLoadError,
    intentViewsLoadedOnce,
    intentViewsSource,
    loadIntentViews,
    refreshIntentFacts,
    refreshAfterIntentAction,
    ensureFreshIntentViews,
    /** F03：任务行（显示 + 操作资格同源）与任务面板入口 */
    taskRows,
    taskCounts: taskCountsValue,
    runningTaskCount,
    pendingTaskCount,
    hasActiveTasks,
    recoverableIntentCount,
    exhaustedRetryIntentCount,
    taskCenterFocus,
    openTaskCenter,
    closeTaskCenter,
    /** F05：恢复事项摘要（不依赖面板挂载） */
    recoveryItems,
    recoveryEntry,
    recoveryReadState,
    /** F02.6：回前台 / 断线恢复时重新核实；F02.7：离开页面时清理计时器与监听 */
    reverifyAfterResume,
    disposePageRuntime,

    // 真实理解（契约 §14.1 / §14.6）
    understanding,
    understandings,
    understandingStatus,
    understandingError,
    understandingPolling,
    understandingNote,
    understandingHistory,
    understandingIntents,
    acceptedUnderstandingId,
    loadUnderstanding,
    refreshUnderstanding,
    retryUnderstanding,
    syncIntentsFromUnderstanding,
    startUnderstandingPolling,
    stopUnderstandingPolling,

    // 真实执行（契约 §14.1 / §14.6）
    executions,
    executionsByIntent,
    executionStatus,
    executionError,
    executionPolling,
    liveExecutions,
    loadExecutions,
    latestExecution,
    pauseExecution,
    resumeExecution,
    cancelExecution,

    auxOpen,
    demoMode,
    chatOpen,
    tasksOpen,
    // 卡片内预览的展开状态（只在前端内存；契约 2026-10-12 §3.5 / §3.6）
    expandedCards,
    expandedCardIds,
    previewExpandedCount,
    toggleCardPreview,
    collapseAllPreviews,
    isCardPreviewExpanded,
    savePreviewPosition,
    previewPositionError,

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
    /** P1：本次提交真的把哪些未确认草稿提升成了正文（含条数与未提升原因） */
    promotedDrafts,
    promotedDraftsCount,
    draftsNotPromoted,
    submitResultUnknown,
    submitRefreshState,
    submitRefreshError,
    retrySubmitRefresh,
    // 契约 §1.3：稳定提交身份与按身份的精确查证
    pendingSubmission,
    submitAcceptedByLookup,
    acceptanceLookupNote,
    acceptanceLookupPending,
    lastAcceptance,
    confirmAcceptanceAfterUnclear,
    approve,
    reject,
    decideBatch,
    updatePreview,
    advanceDemo,
    createDemoIntents,
    confirmImpact,
    cancelImpact,
    cancelRecovery,
    retryCancelRecovery,
    boardContentConflictFor,
    resolveBoardContentConflict,
    boardRemovalConflictFor,
    resolveBoardRemovalConflict,
    boardRemovalConflicts,
    continueRevertDecision,
    dismissRevertDecision,
    reopenRevertDecision,
    revertDecisionDismissed,
    revertDecisionStateFor,
    lastSavePaused,
    setCardDraftInput,
    dismissMaterialPaused,
    checkMaterialImpact,
    intentById,
    statusLabel,
  };
});
