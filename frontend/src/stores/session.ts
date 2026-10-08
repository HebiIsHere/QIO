import { defineStore } from "pinia";
import {
  api,
  type DevAbandonResult,
  type DevAuthorizationRow,
  type DevTaskRow,
  type InterruptedTurn,
  type ToolRecordPreview,
} from "../services/api";
import { effectScope, watch } from "vue";
import {
  draftStorageAvailable,
  draftStorageKey,
  isStaleReceipt,
  readDraft,
  removeDraft,
  UNBOUND_DRAFT_ID,
  writeDraft,
} from "../interactive/drafts";
import { isBlankText, normalizeSendText } from "../interactive/chat";

export interface ToolPresentation {
  title?: string;
  status?: string;
  summary?: string;
  /** 原始工具名（后端附带）：只在悬停提示 / 开发者排查时使用 */
  tool?: string;
}

export interface QueueItem {
  turn_id: string;
  message: string;
}

export interface TurnQueueState {
  running: QueueItem | null;
  queued: QueueItem[];
  cancelled: QueueItem[];
}

/** 服务端发来的一份权威 Turn 队列快照（TURN_QUEUE 事件 / runtime state 里的同一形状）。 */
export interface TurnQueueSnapshot {
  instance_id?: string | null;
  revision?: number | null;
  running?: QueueItem | null;
  queued?: QueueItem[];
  cancelled?: QueueItem[];
}

/**
 * 工具卡的真实语义。
 *
 * `unknown` **不是**「没收到通知」的同义词：只有服务器自己也拿不出这次调用的
 * 结果（记录已被回收 / 后端重启过）时才允许用它。
 */
export type ToolStatus = "running" | "success" | "failed" | "cancelled" | "unknown";

/** 执行叙事（Execution Narrative）的四种形态，见 spec 2026-09-22。 */
export type NarrativeKind = "announce" | "progress" | "warning" | "result";

/** 历史里抽屉内容：**系统生成**的调用摘要（不是模型文案）。 */
export interface NarrativeCallRecord {
  callId: string;
  tool: string;
  title?: string;
  status: "success" | "failed" | "cancelled";
  error?: string | null;
  durationMs?: number | null;
}

/** 消息流里的一个渲染分组：叙事行 + 它收纳的调用卡。 */
export type TurnItemGroup =
  | { kind: "stage"; narrative: StreamMessage; calls: StreamMessage[] }
  | { kind: "loose"; items: StreamMessage[] };

/** 被叙事抽屉收纳的卡片类型（普通工具 / 独立任务 / 工具创建）。 */
const CALL_CARD_ROLES = new Set<StreamMessage["role"]>(["tool", "subagent", "tool_creation"]);

/**
 * 把一轮里的消息分成「叙事抽屉」与「散装消息」。
 *
 * 归属规则：一行叙事收纳**它之后、下一行叙事之前**的调用卡；轮次开头（还没有任何叙事）
 * 的调用卡保持散装 —— 那正是模型选择静默的那一批，不该被硬塞进某一行叙事里。
 */
export function groupTurnItems(items: StreamMessage[]): TurnItemGroup[] {
  const groups: TurnItemGroup[] = [];
  let stage: { kind: "stage"; narrative: StreamMessage; calls: StreamMessage[] } | null = null;
  let loose: StreamMessage[] = [];
  const flush = () => {
    if (loose.length) {
      groups.push({ kind: "loose", items: loose });
      loose = [];
    }
  };
  for (const item of items) {
    if (item.role === "narrative") {
      flush();
      stage = { kind: "stage", narrative: item, calls: [] };
      groups.push(stage);
      continue;
    }
    if (CALL_CARD_ROLES.has(item.role) && stage) {
      stage.calls.push(item);
      continue;
    }
    stage = null;
    loose.push(item);
  }
  flush();
  return groups;
}

function normalizeNarrativeKind(raw: unknown): NarrativeKind {
  return raw === "announce" || raw === "warning" || raw === "result" ? raw : "progress";
}

function narrativeCallRecords(raw: unknown): NarrativeCallRecord[] {
  if (!Array.isArray(raw)) return [];
  const out: NarrativeCallRecord[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const status = String(row.status ?? "");
    out.push({
      callId: String(row.call_id ?? ""),
      tool: String(row.tool ?? ""),
      title: row.title === undefined ? undefined : String(row.title),
      status: status === "failed" || status === "cancelled" ? status : "success",
      error: row.error === undefined || row.error === null ? null : String(row.error),
      durationMs: typeof row.duration_ms === "number" ? row.duration_ms : null,
    });
  }
  return out;
}

/** 历史行的 `raw`（JSON 字符串）→ 叙事元数据；解析失败一律当作"没有"。 */
function parseNarrativeRaw(raw?: string | null): {
  kind?: NarrativeKind;
  calls?: NarrativeCallRecord[];
} {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const meta = (parsed.narrative ?? {}) as Record<string, unknown>;
    const calls = narrativeCallRecords(parsed.calls);
    return {
      ...(meta.kind === undefined ? {} : { kind: normalizeNarrativeKind(meta.kind) }),
      ...(calls.length ? { calls } : {}),
    };
  } catch {
    return {};
  }
}

/**
 * 后端核对通过的完成结论（见 backend core/turn_facts.py）。
 *
 * 只有模型调 declare_completion 且后端逐条核对通过时才有：它证明「测试通过 /
 * 已注册 / 可以使用」是后端记录里的结论，而不是模型自己说的。没有它就不显示
 * 任何标记 —— 普通回答一个字都不加。
 */
export interface VerifiedFact {
  /** 核对的依据（版本摘要、测试摘要、注册情况），直接给用户看 */
  basis: string;
  /** 通过核对的那几条结论（test_passed / registered / usable） */
  claims: string[];
}

/** 把「核对结论」这个形状的值归一化；形状不对一律当作「没有」。 */
function normalizeVerification(value: unknown): VerifiedFact | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (raw.accepted !== true) return null;
  const basis = typeof raw.basis === "string" ? raw.basis : "";
  const claims = Array.isArray(raw.claims) ? raw.claims.map((c) => String(c)) : [];
  return { basis, claims };
}

/** 历史行的 `raw`（JSON 字符串）→ 核对结论；解析失败一律当作「没有」。 */
function parseVerifiedRaw(raw?: string | null): VerifiedFact | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return normalizeVerification(parsed?.verified);
  } catch {
    return null;
  }
}

/**
 * 一份工具执行事实（`/api/runtime/state.tools`：活工具 + 最近结束的工具）。
 *
 * 身份是 `tool_call_id`（同一个工具名可能在一轮里被调用多次），
 * `turn_id` 用于跨轮一致性校验 —— 别的 Turn 的调用不得改到本轮的卡片上。
 */
export interface ToolExecutionSnapshot {
  turn_id?: string | null;
  tool_call_id: string;
  tool_name?: string;
  status: ToolStatus;
  started_at?: string | null;
  ended_at?: string | null;
  error_summary?: string | null;
}

/**
 * 内部循环（子任务等）的 turn_id 前缀：它们的工具明细不属于主对话。
 * 产品上 Subagent 的最小显示单位是「独立任务」，本轮不扩大这个范围。
 */
function isInternalToolTurn(turnId: unknown): boolean {
  return typeof turnId === "string" && turnId.startsWith("subagent:");
}

/**
 * 工具参数 → 可读文本。
 *
 * 取全文接口给的是原样 JSON（已打码）；对象就缩进格式化，字符串直接显示，
 * 空值不编造内容。
 */
function formatToolArguments(args: unknown): string {
  if (args === null || args === undefined) return "";
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

/** 高影响知识候选（对话内确认卡）。 */
export interface KnowledgeCandidate {
  knowledgeId: string;
  category: string;
  content: string;
  reason?: string;
}

/** 候选卡自己的状态：保存 / 修改 / 忽略都要有进行中、成功、失败。 */
export type CandidateState = "idle" | "busy" | "ok" | "failed";

/**
 * 全局状态只表达「整体情况」，不暴露内部事件名（spec 第 36~38 条）：
 * 正在处理 / 正在使用工具 / 等待确认 / 正在处理独立任务 / 正在整理独立任务的结果。
 */
export type Activity =
  | "idle"
  | "waiting"
  | "generating"
  | "tool"
  | "approval"
  | "subagent"
  | "notify";

/**
 * 工具创建流程用到的工具名。
 *
 * 这些调用**不再各出一张普通工具卡**：它们的进度由 `TOOL_CREATE_STATUS` 汇总成
 * 同一张创建卡（spec 第 24 条：不要「创建卡 → 测试卡 → 审批卡 → 成功卡」一串卡）。
 * 失败时会把「创建没有完成：<原因>」写回创建卡，信息不会丢。
 */
const TOOL_CREATION_TOOLS = new Set([
  "create_tool",
  "dev_list_files",
  "dev_read_file",
  "dev_write_file",
  "dev_run_tests",
  "dev_submit_tool",
]);

/**
 * 历史读取状态。
 * 「没有历史」和「读不到历史」是完全不同的产品状态，不能都表现为空列表。
 */
export interface HistoryState {
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
}

/**
 * 首屏加载的历史条数。
 *
 * 进入 Topic 不再一次读全部历史：消息体积（尤其是长回答）会随使用时间增长，
 * 后端读取、JSON 大小、前端解析与内存都会线性恶化。最近一页足以支撑继续对话。
 */
export const HISTORY_PAGE_SIZE = 200;

export interface StreamMessage {
  id: string;
  /**
   * 消息角色。`tool` / `subagent` / `tool_creation` 都是「对话流里的卡片」，
   * 但它们是三种不同的东西：一次工具调用、一个独立任务、一条工具创建流程。
   * 用户必须能分辨（spec 第 61~65、20~31 条）。
   */
  role: "user" | "assistant" | "tool" | "subagent" | "tool_creation" | "system" | "narrative";
  content: string;
  contentType: string;
  createdAt: string;
  toolName?: string;
  toolOk?: boolean;
  toolError?: string | null;
  /** 工具卡呈现（present_call/present_result 合并结果，缺省回退默认模板） */
  presentation?: ToolPresentation | null;
  /** 工具调用标识：TOOL_START / TOOL_END 按它更新同一张卡，不再产生第二张 */
  callId?: string;
  /** 叙事形态（role === "narrative" 时使用） */
  narrativeKind?: NarrativeKind;
  /** 这一行叙事覆盖的调用（系统给的 call_ids，用于抽屉归属） */
  narrativeCallIds?: string[];
  /** 历史抽屉内容：系统生成的调用摘要（实时轮次里为空，用工具卡） */
  narrativeCalls?: NarrativeCallRecord[];
  /** 工具正在执行（卡片显示「运行中」，而不是假装已完成） */
  toolRunning?: boolean;
  /**
   * 工具的真实状态（内部数据模型必须区分五种语义；
   * 视觉上仍复用最接近的既有状态，见 spec 第 27 条）。
   */
  toolStatus?: ToolStatus;
  /** 工具耗时（毫秒）；没有意义时（太快/未知）不显示 */
  toolDurationMs?: number;
  /** 独立任务标识：同一 task_id 只有一张卡 */
  taskId?: string;
  /** 独立任务状态（用户可见语义：开始 / 进行中 / 已完成 / 失败） */
  taskStatus?: "queued" | "running" | "done" | "failed";
  /** 独立任务的目标（来自工具参数，拿不到就不显示，不编造） */
  taskGoal?: string;
  /** 工具创建流程标识：同一个开发工作区 = 同一张卡 */
  groupId?: string;
  /** 工具创建阶段（TOOL_CREATE_STATUS.phase） */
  createPhase?: string;
  /** 这条创建流程在造哪个工具（拿不到就先不显示） */
  createdToolName?: string;
  /**
   * 工具调用历史的记录 id（实时与历史两种卡片都有）：
   * 展开卡片时按它取参数与输出全文 —— 实时与历史走的是同一条路径。
   */
  toolRecordId?: string;
  /** 已取到全文（避免重复请求） */
  toolRecordLoaded?: boolean;
  toolRecordLoading?: boolean;
  /** 取全文失败的原因（可重试） */
  toolRecordError?: string | null;
  /** 参数（格式化后的 JSON 文本） */
  toolArgs?: string;
  /** 输出被截断（单条上限 4 万字） */
  toolTruncated?: boolean;
  /** 库里没有输出正文 */
  toolOutputMissing?: boolean;
  /** 为什么没有：'setting'（关闭了保存全文） / 'retention'（按保留期清掉） */
  toolMissingReason?: string;
  /** 中间助手消息（工具调用前的可见评论，区别于最终答复） */
  interim?: boolean;
  /** 后端核对通过的完成结论（有就显示「后端已核对」，没有就不显示） */
  verified?: VerifiedFact;
  /** 正在流式输出（打字机逐字）的消息；落定后为 undefined */
  streaming?: boolean;
  /** 最近一次增量到达的时间戳（毫秒）：用来按真实到达节奏驱动逐字显示 */
  deltaAt?: number;
  /** 观测到的相邻两次增量间隔（毫秒，40–400ms）：逐字显示按这个节奏走，而不是按固定字/秒估算 */
  paceMs?: number;
  /** 消息产生时所属话题名（快照，避免切换话题后显示串） */
  topicName?: string | null;
  /** 该消息所属 turn_id（SSE 事件归属） */
  turnId?: string | null;
    /** 本次会话中新产生（用于「最多一次很短的入场」；历史消息不带这个标记） */
    fresh?: boolean;
  /** 主 turn 运行中提交、等待执行的消息（TURN_START 时按 FIFO 清除） */
  queued?: boolean;
}

/**
 * 一次「没有发出去」的发送（契约 §10.1）。
 *
 * 原文只属于它当时的话题：切到别的话题时，这份记录不许改那边的输入、草稿或发送状态，
 * 也不许把原话题的错误显示成那边的失败。
 */
export interface FailedSend {
  /**
   * 这条失败事实自己的稳定 id（契约 §11.4）。
   *
   * 没有它就只能用一个内存槽记住「上一次失败」，连续两次失败时前一份原文会被
   * 静默覆盖。有它之后多条失败可以并存、逐条找回或放弃。
   *
   * 声明为可选只为了兼容两类既有输入：既有调用方直接写入的旧形状，以及本机里
   * 可能残留的旧记录；**会话层自己写入时一定会带上它**，读到缺失时会补一个。
   */
  id?: string;
  /**
   * 这次发送的归属身份（契约 §11.5）：随话题迁移保持不变。
   * 受理成功后按它定位「被发送的那一个版本」，不依赖旧存储位置。
   */
  draftId?: string;
  /** 点击发送那一刻的话题（null = 话题还没确定，用的是占位草稿） */
  topicId: string | null;
  /** 原始文字（用户当时输入的内容，不自动拼接、不改写） */
  text: string;
  /** 点击发送那一刻的草稿版本（历史事实；界面与既有用例仍在读它） */
  draftSeq?: number;
  /** 失败发生的时间（毫秒） */
  at: number;
}

/**
 * 点击「发送」那一刻定下的归属（契约 §10.1）。
 *
 * 请求本身必须用它：清空输入框、等待回执期间切换话题，都不许改变这条消息的去向。
 */
export interface SendAttribution {
  /**
   * 这次发送的**稳定身份**（契约 §11.5）。
   *
   * 点击那一刻生成，跟着这次发送走：草稿从「未绑定占位键」迁移到真实话题键时，
   * 身份不变、位置改指向新键，受理成功时才能准确清理被发送的那一个版本 ——
   * 既不依赖已经失效的旧存储位置，也不靠「文字相同」这种不牢靠的判断。
   */
  draftId: string;
  /** 点击那一刻的话题 */
  topicId: string | null;
  /** 点击那一刻输入框里的原始文字 */
  text: string;
  /** 点击那一刻的草稿版本 */
  draftSeq: number;
  /** 点击那一刻的草稿存储键（清理旧草稿只按它 + 版本） */
  key: string;
  /** 记录时间（毫秒） */
  at: number;
  /** 是否确实在点击那一刻记录过（false = 兜底捕获，不做存档保护） */
  recorded: boolean;
}

const sessionStoreDefinition = defineStore("session", {
  state: () => ({
    currentTopicId: null as string | null,
    /**
     * 上一次进程结束时仍没人回答的审批（后端 `/api/runtime/state` 的
     * `interrupted_approvals`）。它们**不会**恢复等待 —— 那次工具调用随进程
     * 一起没了；界面只负责说清「那一次操作没有执行」。
     */
    interruptedOperations: [] as {
      approval_id: string;
      kind: string;
      what: string;
      createdAt: string;
    }[],
    /**
     * 上一次进程结束时「已经被接受、但没有执行完」的**用户消息**
     * （后端 `/api/runtime/state` 的 `interrupted_turns`）。
     *
     * 与 interrupted_approvals 是两件事：审批那次是「工具没执行」，这里是
     * 「你说的话没人接」。两条都**不会自动重放** —— 是否继续由用户明确决定。
     * 这里只持有后端给的形状，不本地缓存、不自己判断哪条算没做完。
     */
    interruptedTurns: [] as InterruptedTurn[],
    /**
     * 正在提交中的那条（turn_id）；「全部忽略」用 `ALL`。
     *
     * 放在 store 而不是组件里：同一条记录的「继续」只能有一个请求在飞，
     * 否则双击就会变成两次重发（后端虽然会用一次性 claim 挡住第二次，
     * 但界面上不该出现"点了两下、弹两条错"这种事）。
     */
    interruptedBusyId: "" as string,
    /** 上一次恢复操作的结果（成功或失败都说清楚，不静默） */
    interruptedNotice: "" as string,
    /**
     * 工具开发任务（后端 `GET /api/dev/tasks` 的权威列表）。
     *
     * 以前任务只活在工具调用里：模型不提，界面就再也找不到它，用户也不知道还有
     * 一件事没做完。这里只持有后端给的形状，不缓存到本地存储、不自己推断状态。
     */
    devTasks: [] as DevTaskRow[],
    /**
     * 执行授权的**范围**（后端 `GET /api/dev/authorizations`）。
     *
     * 用户需要能看清「我同意过什么、在哪儿跑、用哪个凭据」，并且在想收回时
     * 能真的收回 —— 所以这份列表是可查、可撤销的，而不是一次点击后永久生效。
     */
    devAuthorizations: [] as DevAuthorizationRow[],
    topicName: null as string | null,
    anchorFragmentId: null as string | null,
    anchorFragment: null as { id: string; title: string | null } | null,
    /**
     * 当前位置是否是「历史位置」（用户从这里继续 / Agent 显式 continue 选中的历史片段）。
     * 成功一轮后后端把位置推进到当前片段 → historic=false，UI 的「从…继续」提示随之消失。
     */
    anchorHistoric: false,
    /**
     * 已登记、尚未落实的接续选择（后端 continuation_intents 的 registered 态）。
     * 有它时输入区要说明「下一条消息将从这段历史继续」；发出去（或取消）之后消失。
     */
    pendingContinuation: null as { intentId: string; sourceTitle: string | null } | null,
    /**
     * 输入草稿。放在 store 而不是 Composer 局部状态：
     * 打开设置/星球会让对话页组件卸载重建，局部状态会随之丢失。
     */
    draft: "",
    /**
     * 聊天草稿的保存状态（契约 §9.4）：
     * saving = 有内容还没落盘；saved = 已经保存进本机存储；
     * error = 保存失败，必须显示真实原因并允许重试 —— 这时绝不能显示「已保存」。
     */
    draftSaveStatus: "idle" as "idle" | "saving" | "saved" | "error",
    /** 最近一次草稿保存失败的真实原因（保存成功或没有草稿时为空） */
    draftSaveError: null as string | null,
    /**
     * 最近一次发送失败的原文（契约 §10.1）。
     *
     * 会话层统一持有这份事实：普通对话页的输入区与悬浮聊天都读它，
     * 不各写一套「失败了要不要把字放回去」的判断。它**不自动写回输入框**，
     * 只提供明确的取回入口；不属于当前话题时界面什么都不做。
     */
    failedSend: null as FailedSend | null,
    /** 这次发送失败的真实原因（与 failedSend 同生共死，界面直接显示，不改写成别的意思） */
    failedSendError: null as string | null,
    /**
     * 所有还没处理完的失败原文（契约 §11.4），按失败时间从新到旧。
     *
     * 为什么是列表而不是单个槽：连续两次失败、排队发送期间又失败、A→B 切话题后
     * 又回到 A 再失败 —— 用单个槽记「上一次」都会让更早的那份原文消失。
     * 清理只依据受理成功或用户明确操作（找回/互换不算丢弃，记录仍在）。
     */
    failedSends: [] as FailedSend[],
    /** 每条失败记录自己的真实原因（键是记录 id）；界面按记录显示，不串用别人的原因 */
    failedSendErrors: {} as Record<string, string>,
    /**
     * 失败原文写进本机存储的结果（null = 最近一次写入成功）。
     * 存储不可用/写满时必须如实说，不能显示成「已经可以恢复」。
     */
    failedSendPersistError: null as string | null,
    /**
     * 本机发起的发送序号。只有本机发送才允许把消息流强制拉回底部；
     * 后台/排队任务开始时用户可能正在往上读，不能被拽走。
     */
    localSendSeq: 0,
    /** 本机发送被后端拒绝的次数：消息流据此把阅读位置放回发送前 */
    sendRejectedSeq: 0,
    /**
     * 消息流阅读位置（内存态，同一会话内保留）。
     * 打开设置/星球会让对话页组件卸载重建，不记住位置就会把用户甩回最新处。
     */
    streamScrollTop: 0,
    /** 离开时是否处于「跟随底部」状态；恢复时据此决定要不要贴底 */
    streamFollowing: true,
    messages: [] as StreamMessage[],
    turnRunning: false,
    lastError: null as string | null,
    /** 非致命警告（WARNING 事件）；不代表 turn 结束，TURN_START 清空 */
    warning: null as string | null,
    /** 正在「取消中」的 turn_id（停止按钮反馈，避免假装已停止） */
    cancelling: null as string | null,
    /**
     * 已经结束的 turn（有界，只用于本地竞态判断）。
     *
     * 受理 ≠ 开始执行：`POST /api/turns` 的回执可能**晚于** `TURN_END` 到达
     * （没有可用凭据时一轮十几毫秒就结束了）。那种情况下绝不能再把运行态点亮，
     * 否则界面会永远停在「运行中」：停止按钮一直在、发送一直被禁用。
     */
    endedTurnIds: [] as string[],
    /** 历史读取状态（失败时保留已有消息，只标记失败） */
    history: { status: "idle", error: null } as HistoryState,
    /** 是否还有更早的历史可以加载 */
    historyHasMore: false,
    /** 上一页的游标（后端给的复合游标） */
    historyCursor: null as string | null,
    /** 正在加载更早的历史（滚动会连续触发，需要防重入） */
    historyOlderLoading: false,
    /**
     * 历史加载的请求代次：每一次**完整加载**都领一个新号。
     *
     * 提交结果前必须同时校验「自己还是最新一次请求」与「发起时的话题没被换掉」——
     * 慢的旧响应（成功、错误、finally 三处）都要整段丢弃，不能把新话题改回去。
     */
    _historyRequestSeq: 0,
    /**
     * 向前分页的票据号。
     *
     * 只有**还持有当前票据**的那次请求才允许解锁 `historyOlderLoading`：
     * 旧分页的 finally 不能把新分页的锁打开（否则新分页还在飞时又会被放进来一次）。
     */
    _historyOlderToken: 0,
    /** 开发任务列表的刷新代次：连接后可能并发刷新，只认最后发起的那次。 */
    _devTasksSeq: 0,
    /** 执行授权列表的刷新代次（同上）。 */
    _devAuthSeq: 0,
    /** 权威运行状态读取的代次（RESYNC 与失败恢复可能并发）。 */
    _runtimeStateSeq: 0,
    /**
     * RESYNC 状态机：`normal` 正常实时；`resyncing` 正在拉权威快照
     * （期间实时事件先缓存，快照应用后再按顺序补放）；`failed` 同步失败，
     * 界面要如实说「可能不是最新的」，不能假装已经同步完成。
     */
    resyncState: "normal" as "normal" | "resyncing" | "failed",
    /** 本地排队中的用户消息 id（FIFO；TURN_START 到来时清除最早的一条） */
    queuedMessageIds: [] as string[],
    /**
     * 当前**真正在运行**的主 turn id。
     *
     * 只有真实的 `TURN_START`（或后端 `TURN_QUEUE` 快照里明确的 running）
     * 才能写这个字段。SEND 返回的 turn_id 只代表「后端已经受理」，
     * 受理 ≠ 正在运行 —— 它绝不能把排队的 turn 变成 active。
     */
    activeTurnId: null as string | null,
    /** 已被后端受理、但还没有开始执行的 turn（accepted / queued） */
    queuedTurnIds: [] as string[],
    /**
     * 最近一次生效的队列快照版本（服务端单调递增）。
     * 快照是权威的，但**旧的**权威快照不能覆盖更新的状态 —— 用它做判断。
     */
    queueRevision: 0,
    /**
     * 后端实例标识。进程重启后 revision 会从 0 重新计数，
     * 所以「版本比较」必须建立在同一个实例的前提上。
     */
    instanceId: null as string | null,
    /**
     * 最近一轮的结局。界面用它安静地表达「已停止」这类状态：
     * 成功由回答本身表达，失败进 lastError，无凭据进 warning。
     */
    lastTurnOutcome: null as { turnId: string; status: string } | null,
    /**
     * 待确认切换（spec 第 29~30 条）：内容看起来属于另一个话题时的建议。
     * 它只是「建议」—— Anchor 没有被改，用户点「转到这里」才真的切。
     */
    pendingSwitch: null as { topicId: string; topicName: string; reason?: string } | null,
    /** 待确认切换正在提交（按钮防重复） */
    pendingSwitchBusy: false,
  /**
   * 一轮的可观察阶段（不猜测后台在干什么）：
   * idle 未在跑 / waiting 已提交但还没有任何助手内容 / generating 已有增量内容到达
   */
  turnPhase: "idle" as "idle" | "waiting" | "generating",
    /**
     * 全局整体状态（只表达整体，不堆内部事件名）。
     * 局部状态（工具卡 / 独立任务卡 / 审批卡）各自表达自己的位置。
     */
    activity: "idle" as Activity,
    /**
     * 高影响知识候选（spec 第 14~19 条）。
     * 收到事件先缓冲，TURN_END 之后才显示 —— 顺序必须是「回答完成 → 候选出现」，
     * 绝不打断正在生成的回答。
     */
    knowledgeCandidates: [] as KnowledgeCandidate[],
    /** 缓冲：回答还没完成时先放这里 */
    _queuedCandidates: [] as KnowledgeCandidate[],
    /** 每个候选自己的操作状态（进行中 / 成功 / 失败） */
    candidateState: {} as Record<string, CandidateState>,
    candidateError: {} as Record<string, string>,
    /** 主 turn 队列快照（TURN_QUEUE 事件更新）：运行中 + 排队中 */
    turnQueue: { running: null, queued: [], cancelled: [] } as TurnQueueState,
    /**
     * 等待用户决定是否继续的那次暂停。
     *
     * 不只是预算耗尽：无进展暂停（连续几次调用拿到完全一样的结果）走的是同一条
     * 「继续/停止」通道。所以这里必须原样保留后端给的原因与说明 —— 界面自己猜原因
     * 会把无进展暂停说成「已达迭代上限」。
     */
    pendingContinue: null as {
      id: string;
      used: number;
      max: number;
      /** 后端给的机器可读原因：no_progress / budget（旧后端可能为空） */
      reason: string;
      /** 预算耗尽时进一步区分：iterations / tokens；其它情况为空 */
      budgetKind: string;
      /** 后端给的人话说明；为空时由界面兜底 */
      message: string;
    } | null,
    _msgSeq: 0,
    /** 被折叠进创建卡的调用：call_id → 开发工作区 id（失败时回写创建卡用） */
    _creationGroupByCall: {} as Record<string, string>,
    /** 已折叠的 call_id（这些调用不再单独出工具卡） */
    _suppressedToolCalls: [] as string[],
    /**
     * 正在播入场的消息 id。放在状态里（而不是给消息对象打标记）：
     * 直接改对象属性不会经过响应式代理，界面不会更新。
     */
    freshIds: [] as string[],
  }),
  getters: {
    /**
     * 有任务在跑就可以停。
     *
     * 不要求 `activeTurnId` 已就位：还没收到 TURN_START 的那一小段时间里，
     * 停止动作会退化为「取消后端当前的 active turn」，同样不会打到排队项。
     */
    canStopTurn: (state): boolean => state.turnRunning,
    /**
     * 没做完的开发任务 = 还没提交、也还没被放弃的。
     *
     * 「提交」是唯一能证明这件事做完了的机器可读事实：任务一旦提交成功，
     * 工具已经注册进后端，界面就不该再把它列成待办。
     * 「放弃」是另一个终态：结束了、不再执行，同样不属于「没做完」。
     * 列表接口会把已放弃的行也带回来（事实清单），所以这里必须两个条件一起判。
     */
    unfinishedDevTasks: (state): DevTaskRow[] =>
      state.devTasks.filter((task) => !task.submitted && !task.abandoned),
    /**
     * 某个话题下待恢复的失败原文（新的在前）。
     *
     * 只按话题取：失败原文始终属于原话题，切到别的话题不插入、不显示，
     * 回到原话题还能找到（契约 §11.4）。
     * 兼容直接写入 failedSend 的调用方（既有组件/用例会这样造失败事实）：
     * 它没进列表时也一并返回，界面不会因此看不到。
     */
    failedSendsForTopic:
      (state) =>
      (topicId: string | null): FailedSend[] => {
        const list = state.failedSends.filter((record) => record.topicId === topicId);
        const legacy = state.failedSend;
        const known = Boolean(
          legacy &&
            (list.includes(legacy) || (legacy.id && list.some((record) => record.id === legacy.id))),
        );
        if (legacy && legacy.topicId === topicId && !known) return [legacy, ...list];
        return list;
      },
  },
  actions: {
    _nextId() {
      this._msgSeq += 1;
      return `local_${Date.now()}_${this._msgSeq}`;
    },
    /**
     * 立即把内存里的聊天草稿写进本机存储（不等防抖）。
     * 用于离开编辑器 / 页面隐藏这类「可能来不及等 400ms」的时刻。
     */
    flushDraft() {
      chatDraftKeeperFor(this as object)?.flushNow();
    },
    /**
     * 草稿保存失败后的重试：只重写本机存储，
     * **不发送消息、不碰板面、不提交、不调用 QIO**。
     */
    retryDraftSave() {
      chatDraftKeeperFor(this as object)?.retry();
    },
    /**
     * 点击「发送」那一刻定下归属（契约 §10.1）：原话题、原始文字、当时的草稿版本。
     *
     * 组件必须在**清空输入框之前**调用它；send() 会用这次记录发起请求。
     * 所以「等待回执期间切了话题」不会把消息发到别的话题上，
     * 失败原文也不会被放进别的话题的输入框。
     */
    sendAttribution(): SendAttribution {
      const keeper = chatDraftKeeperFor(this as object);
      // 必须**记下来**（recordAttribution 会把归属存进 pendingAttribution），
      // 只 capture 不记录的话 send() 取不到它，就会退化成「按当时的话题」发 —— 那正是要修的竞态。
      if (keeper) return keeper.recordAttribution();
      return {
        draftId: mintSendIdentity(),
        topicId: this.currentTopicId,
        text: this.draft,
        draftSeq: 0,
        key: "",
        at: Date.now(),
        recorded: true,
      };
    },
    /**
     * 找到一条待恢复的失败原文。
     *
     * 不传 id 时取当前话题最新的一条（兼容既有调用方）；界面按记录逐条传 id，
     * 所以多条失败可以分别找回、分别放弃，不会互相顶掉。
     */
    _resolveFailedSend(id?: string): FailedSend | null {
      if (id) {
        const listed = this.failedSends.find((record) => record.id === id || record.draftId === id);
        if (listed) return listed;
        /**
         * 兼容记录（既有调用方直接写入 failedSend 的旧形状）：它可能完全没有身份。
         * 有身份但与本次不符时不算命中 —— 否则会取回到别的一次发送的原文。
         */
        const legacy = this.failedSend;
        if (legacy && (!legacy.draftId || legacy.draftId === id || legacy.id === id)) return legacy;
        return null;
      }
      return this.failedSendsForTopic(this.currentTopicId)[0] ?? null;
    },
    /**
     * 把刚放进输入框的原文立刻写进本机存储，并如实回报结果。
     *
     * 为什么要立刻写：找回原文不是发送，之后用户可能马上刷新/关闭；
     * 只等 400ms 防抖的话这一步会丢。写不进本机时必须说清（不许假装能恢复）。
     */
    _flushDraftForRecovery(): { ok: boolean; error?: string } {
      const keeper = chatDraftKeeperFor(this as object);
      if (!keeper) return { ok: false, error: "草稿保护没有装上，刷新后可能取不回这段文字" };
      keeper.flushNow();
      const key = keeper.currentKey;
      if (!key) return { ok: false, error: "本机存储不可用，刷新后可能取不回这段文字" };
      const stored = readDraft(key);
      if (stored && stored.text === this.draft) return { ok: true };
      return {
        ok: false,
        error: this.draftSaveError
          ? `文字没有保存在本机（${this.draftSaveError}）`
          : "文字没有保存在本机，刷新后可能取不回这段文字",
      };
    },
    /**
     * 取回某一条失败原文（契约 §10.1 / §11.4）。**不自动发送**。
     *
     * 三条边界：
     * - 只在原话题可用：当前在别的话题时一个字都不改，只回报原因；
     * - 输入框为空就放回，但记录**仍然保留**：它还没成功发出去，只能靠受理成功或
     *   用户明确点「不再保留」清理（合并成「放回即清除」会在刷新后丢掉恢复入口）；
     * - 输入框里已有更新文字时**不覆盖**：两份都保留，取回失败那份走 swapFailedSendText()。
     */
    retryFailedSend(id?: string): { ok: boolean; restored: boolean; reason?: string } {
      const failed = this._resolveFailedSend(id);
      if (!failed) return { ok: false, restored: false, reason: "没有需要取回的失败原文" };
      if (failed.topicId !== this.currentTopicId) {
        return { ok: false, restored: false, reason: "需要回到原话题才能取回这段文字" };
      }
      if (isBlankText(this.draft)) {
        this.draft = failed.text;
        const saved = this._flushDraftForRecovery();
        return saved.ok
          ? { ok: true, restored: true }
          : { ok: true, restored: true, reason: "原文已放回输入框，但" + (saved.error ?? "没有保存在本机") };
      }
      if (normalizeSendText(this.draft) === normalizeSendText(failed.text)) {
        // 原文已经在输入框里（例如失败后自动放回、或回来时草稿被恢复）：不必再放一次。
        // 失败事实仍保留 —— 它还没有成功发出去，不能假装已经解决。
        return { ok: true, restored: false, reason: "原文已经在输入框里" };
      }
      return {
        ok: false,
        restored: false,
        reason: "输入框里已有更新文字，没有覆盖（两份都保留，可以互换取回原文）",
      };
    },
    /**
     * 把失败原文与当前输入**互换**：两份文字都保留，谁都不被丢掉。
     *
     * 只在用户明确点「互换」时调用（不是失败后的自动行为），也不拼接、不改写、不发送。
     * 互换之后这条记录里放的是刚才输入框里的那份文字 —— 它同样还没有成功发出去，
     * 所以记录继续保留，直到真正发送成功或用户明确放弃。
     */
    swapFailedSendText(id?: string): { ok: boolean; swapped: boolean; reason?: string } {
      const failed = this._resolveFailedSend(id);
      if (!failed) return { ok: false, swapped: false, reason: "没有需要取回的失败原文" };
      if (failed.topicId !== this.currentTopicId) {
        return { ok: false, swapped: false, reason: "需要回到原话题才能取回这段文字" };
      }
      const current = this.draft;
      if (isBlankText(current)) {
        // 输入框本来就是空的：直接放回，不需要互换
        this.draft = failed.text;
        const saved = this._flushDraftForRecovery();
        return saved.ok
          ? { ok: true, swapped: false }
          : { ok: true, swapped: false, reason: "原文已放回输入框，但" + (saved.error ?? "没有保存在本机") };
      }
      const keeper = chatDraftKeeperFor(this as object);
      const next: FailedSend = {
        ...failed,
        text: current,
        draftSeq: keeper?.currentSeq ?? failed.draftSeq ?? 0,
      };
      if (failed.id && this.failedSends.some((record) => record.id === failed.id)) {
        this.failedSends = this.failedSends.map((record) => (record.id === failed.id ? next : record));
      } else {
        // 兼容直接写入 failedSend 的调用方：镜像跟着换过来的那份文字走
        this.failedSend = next;
      }
      this.draft = failed.text;
      this._flushDraftForRecovery();
      this._persistFailedSends();
      return { ok: true, swapped: true };
    },
    /**
     * 不再保留某一条失败原文（用户明确操作）：只清掉这条失败事实与原因，
     * **不删草稿、不清输入框、不发送、不碰板面**。
     */
    discardFailedSend(id?: string) {
      const target = this._resolveFailedSend(id);
      if (!target) return;
      const listed = Boolean(target.id && this.failedSends.some((record) => record.id === target.id));
      const wasMirror =
        this.failedSend === target || (Boolean(target.id) && this.failedSend?.id === target.id);
      if (listed) {
        this.failedSends = this.failedSends.filter((record) => record.id !== target.id);
      }
      // 被放弃的正是镜像时先清掉它：它已经不在列表里，同步逻辑不能继续把它当外部事实保留
      if (wasMirror) {
        this.failedSend = null;
        this.failedSendError = null;
      }
      this._persistFailedSends();
    },
    /**
     * 把失败原文写进本机存储（契约 §11.4：必须经得起刷新与关闭重开）。
     *
     * 写失败时保留内存记录并如实回报错误（界面会说「刷新后可能取不回」），
     * 绝不显示成已经保存好。
     */
    _persistFailedSends() {
      const ids = new Set<string>();
      for (const record of this.failedSends) if (record.id) ids.add(record.id);
      const errors: Record<string, string> = {};
      for (const [key, value] of Object.entries(this.failedSendErrors)) {
        if (ids.has(key)) errors[key] = value;
      }
      this.failedSendErrors = errors;
      const legacy = this.failedSend;
      /**
       * 兼容：直接写入 failedSend 的调用方（既有组件/用例）给的记录不在列表里 ——
       * 保留它，不用列表去覆盖别人给的事实。除此之外镜像就是列表里最新的一条；
       * 被移除/被替换的记录不能继续留在镜像里（否则界面会把已经放弃的原文又显示出来）。
       */
      const legacyUnknown = Boolean(
        legacy &&
          !this.failedSends.includes(legacy) &&
          !(legacy.id && this.failedSends.some((record) => record.id === legacy.id)),
      );
      const result = writeFailedSendStateToStorage(this.failedSends, errors);
      this.failedSendPersistError = result.error;
      if (legacyUnknown) return;
      const newest = this.failedSends[0] ?? null;
      this.failedSend = newest;
      this.failedSendError = newest?.id ? this.failedSendErrors[newest.id] ?? null : null;
    },
    /**
     * 这次发送属于哪个话题（契约 §11.5）。
     *
     * 点击时话题还没确定（null）但发送期间服务器把真实话题绑定了上来：
     * 被发送的那份原文已经跟着迁移到新话题的位置，失败事实也应该算在新话题上，
     * 否则回到这个话题时看不到恢复入口。
     */
    _failedSendTopicFor(attribution: SendAttribution): string | null {
      if (attribution.topicId !== null) return attribution.topicId;
      const keeper = chatDraftKeeperFor(this as object);
      const location = keeper?.locationOf(attribution.draftId);
      if (location) {
        const topic = keeper?.topicForKey(location.key);
        if (topic !== undefined) return topic;
      }
      return this.currentTopicId;
    },
    /** 记下一次失败事实（原文 + 原因 + 归属身份），并立刻写进本机存储 */
    _recordFailedSend(attribution: SendAttribution, reason: string): FailedSend {
      const record: FailedSend = {
        id: mintFailedSendId(),
        draftId: attribution.draftId,
        topicId: this._failedSendTopicFor(attribution),
        text: attribution.text,
        draftSeq: attribution.draftSeq,
        at: Date.now(),
      };
      this.failedSends = trimFailedSendsPerTopic([record, ...this.failedSends]);
      if (record.id) {
        this.failedSendErrors = { ...this.failedSendErrors, [record.id]: reason };
      }
      this.failedSend = record;
      this.failedSendError = reason;
      this._persistFailedSends();
      return record;
    },
    /**
     * 受理成功：只清理**这一次发送对应的**失败原文（契约 §11.4 / §11.5）。
     *
     * 两种匹配：同一归属身份 + 同一版本（精确），或同一话题里文字相同。
     * 后者覆盖用户把找回的原文原样再发一次、以及本机里旧记录没有身份的兼容情形；
     * 文字不同的其他待恢复原文一律保留，晚到的旧回执不会清掉它们。
     */
    _clearFailedSendsAccepted(attribution: SendAttribution, message: string): void {
      const normalized = normalizeSendText(message);
      const kept: FailedSend[] = [];
      let dropped = 0;
      for (const record of this.failedSends) {
        const sameAttempt = Boolean(
          record.draftId &&
            record.draftId === attribution.draftId &&
            (record.draftSeq ?? attribution.draftSeq) === attribution.draftSeq,
        );
        const sameText =
          record.topicId === attribution.topicId && normalizeSendText(record.text) === normalized;
        if (sameAttempt || sameText) {
          dropped += 1;
          continue;
        }
        kept.push(record);
      }
      const legacy = this.failedSend;
      const legacyCleared = Boolean(
        legacy &&
          !kept.includes(legacy) &&
          !(legacy.id && kept.some((record) => record.id === legacy.id)) &&
          legacy.topicId === attribution.topicId &&
          normalizeSendText(legacy.text) === normalized,
      );
      if (!dropped && !legacyCleared) return;
      this.failedSends = kept;
      if (legacyCleared) {
        this.failedSend = null;
        this.failedSendError = null;
      }
      this._persistFailedSends();
    },
    /**
     * 话题从「还没确定」变成真实话题：属于这次发送的失败原文跟着迁移（契约 §11.5）。
     * 与草稿迁移同一条规则 —— 用户在那次发送里说的话属于这个话题，
     * 不该因为记录写在占位位置就找不回来。
     */
    adoptUnboundFailedSends(topicId: string) {
      if (!topicId) return;
      if (!this.failedSends.some((record) => record.topicId === null)) return;
      this.failedSends = this.failedSends.map((record) =>
        record.topicId === null ? { ...record, topicId } : record,
      );
      this._persistFailedSends();
    },
    /**
     * 设置锚点话题。name/fragment 可选：传入则刷新话题名与锚点片段，
     * 未传则保留现有值（向后兼容）。
     */
    setAnchor(
      topicId: string,
      fragmentId?: string | null,
      name?: string | null,
      fragment?: { id: string; title: string | null } | null,
      historic?: boolean,
    ) {
      this.currentTopicId = topicId;
      this.topicName = name !== undefined ? name : this.topicName;
      this.anchorFragmentId = fragmentId ?? null;
      this.anchorFragment = fragment !== undefined ? fragment : this.anchorFragment;
      if (historic !== undefined) this.anchorHistoric = historic;
    },
    /**
     * 切换起点并同步可见消息。
     *
     * 「从这里继续」成功只更新锚点是不够的：消息列表还是上一个话题的对话，
     * 界面显示的内容与真实起点不一致（实测：切到空话题后后端 0 条消息，
     * 界面仍显示上一个话题的 16 条，刷新才对）。所以锚点真的变了就重新拉一次。
     */
    async setAnchorAndSync(
      topicId: string,
      fragmentId?: string | null,
      name?: string | null,
      fragment?: { id: string; title: string | null } | null,
      historic?: boolean,
    ) {
      const before = `${this.currentTopicId ?? ""}|${this.anchorFragmentId ?? ""}|${this.anchorHistoric}`;
      this.setAnchor(topicId, fragmentId, name, fragment, historic);
      const after = `${this.currentTopicId ?? ""}|${this.anchorFragmentId ?? ""}|${this.anchorHistoric}`;
      if (before !== after) await this.loadHistory();
    },
    /**
     * 登记「待确认切换」建议：只记录，不动 Anchor（spec 第 29 条）。
     * 新一轮用户消息开始时清掉旧建议（TURN_START 处理），避免跨轮堆积。
     */
    setPendingSwitch(payload: { topicId: string; topicName: string; reason?: string } | null) {
      this.pendingSwitch = payload;
    },

    /**
     * 「已登记、还没落实」的接续选择（阶段 1 的后端语义）。
     *
     * 与 anchorHistoric 的区别很重要：
     * - anchorHistoric 表示「当前位置就在一段历史上」；
     * - pendingContinuation 表示「你选了从这段历史继续，下一条消息才会落实」。
     * 界面必须说清是哪一种：选了但还没发送时，不能显示成「已经创建了新片段」。
     */
    setPendingContinuation(
      payload: { intentId: string; sourceTitle: string | null } | null,
    ) {
      this.pendingContinuation = payload;
    },
    /** 用户点「转到这里」：只有这一步会真的改变 Anchor。 */
    async confirmPendingSwitch() {
      const pending = this.pendingSwitch;
      if (!pending || this.pendingSwitchBusy) return;
      this.pendingSwitchBusy = true;
      try {
        const res = await api.confirmTopicSwitch();
        if (!res.ok || !res.topic_id) {
          this.lastError = "后端没有确认这次切换，未切换话题";
          return;
        }
        await this.setAnchorAndSync(
          res.topic_id,
          res.fragment_id ?? null,
          pending.topicName,
          res.fragment_id ? { id: res.fragment_id, title: res.fragment_title ?? null } : undefined,
          Boolean(res.historic),
        );
        this.pendingSwitch = null;
      } catch (e) {
        // 失败必须如实说「未切换」，不能留下一个看起来已经生效的状态
        this.lastError = `切换失败，未切换话题：${(e as Error).message}`;
      } finally {
        this.pendingSwitchBusy = false;
      }
    },
    /** 用户点「保留当前」：拒绝建议，Anchor 一动不动。 */
    async rejectPendingSwitch() {
      if (!this.pendingSwitch || this.pendingSwitchBusy) return;
      this.pendingSwitchBusy = true;
      this.pendingSwitch = null;
      try {
        await api.rejectTopicSwitch();
      } catch (e) {
        this.lastError = `未能通知后端保留当前话题：${(e as Error).message}`;
      } finally {
        this.pendingSwitchBusy = false;
      }
    },
    /**
     * 一轮开始。
     * `notify=true` 是系统驱动的轮（例如独立任务完成后的收尾），
     * 它**不是**用户发起的消息轮：不能清掉排队消息的「等待中」标记。
     */
    turnStarted(notify = false) {
      this.turnRunning = true;
      this.turnPhase = notify ? "generating" : "waiting";
      this.activity = notify ? "notify" : "waiting";
      this.lastError = null;
      this.warning = null;
      this.cancelling = null;
      // 新一轮开始：上一轮的切换建议不再相关，避免跨轮堆积
      this.pendingSwitch = null;
      if (notify) return;
      // 队列中的最早一条开始执行：清除「等待中」标记（后端 TURN_QUEUE 事件负责其余展示）
      const nextQueued = this.queuedMessageIds.shift();
      if (nextQueued) {
        const msg = this.messages.find((m) => m.id === nextQueued);
        if (msg) msg.queued = false;
      }
    },
    /** 真实 TURN_START：唯一允许把某个 turn 设为 active 的入口。 */
    activateTurn(turnId: string, revision?: number | null, instanceId?: string | null): boolean {
      if (!turnId) return false;
      this.adoptInstance(instanceId);
      // 陈旧事件：整条事件不产生**任何**状态变化（不能只跳过 revision 比较）
      if (!this.noteQueueRevision(revision)) return false;
      this.forgetQueuedTurn(turnId);
      this.activeTurnId = turnId;
      this.turnQueue = {
        ...this.turnQueue,
        running: { turn_id: turnId, message: this.turnQueue.running?.message ?? "" },
      };
      this.turnRunning = true;
      if (this.turnPhase === "idle") this.turnPhase = "waiting";
      return true;
    },
    /**
     * 后端实例变化（进程重启）→ revision 基准作废。
     *
     * 重启后新实例的 revision 会从 1 开始；如果不重置基准，
     * 新实例的一切都会被当成「比 135 旧」而丢弃，界面就永远不再更新。
     */
    adoptInstance(instanceId?: string | null): boolean {
      if (!instanceId) return false;
      if (this.instanceId === instanceId) return false;
      this.instanceId = instanceId;
      this.queueRevision = 0;
      return true;
    },
    /**
     * 记录快照版本。
     * 返回 false 表示这份快照比已知状态更旧，调用方应当丢弃它。
     * 没有版本号（旧事件 / 测试）时一律接受，保持向后兼容。
     */
    noteQueueRevision(revision?: number | null): boolean {
      if (typeof revision !== "number" || !Number.isFinite(revision)) return true;
      if (revision < this.queueRevision) return false;
      this.queueRevision = revision;
      return true;
    },
    /** SEND 只表示「后端受理了」：登记为排队，不改变 active。 */
    markTurnQueued(turnId: string) {
      if (!turnId) return;
      if (!this.queuedTurnIds.includes(turnId)) {
        this.queuedTurnIds = [...this.queuedTurnIds, turnId];
      }
    },
    forgetQueuedTurn(turnId: string) {
      if (!turnId) return;
      this.queuedTurnIds = this.queuedTurnIds.filter((id) => id !== turnId);
      if (this.turnQueue.queued.some((q) => q.turn_id === turnId)) {
        this.turnQueue = {
          ...this.turnQueue,
          queued: this.turnQueue.queued.filter((q) => q.turn_id !== turnId),
        };
      }
    },
    isQueuedTurn(turnId: string) {
      return Boolean(turnId) && this.queuedTurnIds.includes(turnId);
    },
    /**
     * 应用一份**权威队列快照**（TURN_QUEUE 事件，或 RESYNC 后重新拉取的快照）。
     *
     * 快照必须能同时做两件相反的事：
     * - 恢复：running=A → active=A（重连 / 丢帧后本地不知道谁在跑）；
     * - 清除：running=null → active=null（服务器早就跑完了，本地不能还停在「正在运行」）。
     *
     * 同时用 revision 挡住**旧快照覆盖新状态**（TURN_START 之后晚到的 running=null）。
     */
    applyTurnQueue(snapshot: TurnQueueSnapshot): boolean {
      this.adoptInstance(snapshot.instance_id);
      // 先校验、再落地：旧快照必须**整条**丢弃，
      // 否则会出现「active 用新数据、QueueChip 用旧数据」这种半应用状态。
      if (!this.noteQueueRevision(snapshot.revision)) return false;

      const running = snapshot.running ?? null;
      const queued = snapshot.queued ?? [];
      this.turnQueue = {
        running,
        queued,
        cancelled: snapshot.cancelled ?? this.turnQueue.cancelled,
      };
      this.queuedTurnIds = queued.map((q) => q.turn_id);
      this.activeTurnId = running?.turn_id ?? null;
      if (this.activeTurnId) this.forgetQueuedTurn(this.activeTurnId);
      // 「有活要干」= 正在跑，或还有排队在等。排队中时停止按钮仍可用
      // （后端只会取消真正在跑的那一轮，不会误伤排队项）。
      const busy = Boolean(this.activeTurnId) || this.queuedTurnIds.length > 0;
      this.turnRunning = busy;
      if (!busy) {
        this.turnPhase = "idle";
        this.cancelling = null;
      } else if (this.turnPhase === "idle") {
        this.turnPhase = "waiting";
      }
      return true;
    },
    /** 一轮结束：清掉 running 指针（同样只由这一处写，保持单一真相）。 */
    endTurn(turnId: string, revision?: number | null) {
      if (revision !== undefined && revision !== null) this.noteQueueRevision(revision);
      if (this.turnQueue.running && this.turnQueue.running.turn_id === turnId) {
        this.turnQueue = { ...this.turnQueue, running: null };
      }
      if (this.activeTurnId === turnId) this.activeTurnId = null;
      // 记下「这一轮已经结束」：发送回执迟到时不能把它重新点亮（见 endedTurnIds）
      if (turnId) {
        this.endedTurnIds.push(turnId);
        if (this.endedTurnIds.length > 50) this.endedTurnIds.shift();
      }
    },
    /**
     * 一轮结束时收敛工具卡。
     *
     * 服务器保证「TURN_END 之前所有工具都已经结束」，所以此刻还显示
     * 「运行中」的卡片一定是 TOOL_END 丢在失真区间里了 —— 收口，
     * 而不是让用户一直看一个转圈的卡片。
     *
     * 这不是最终结论：如果服务器其实知道结果，下一次快照（`reconcileTools`）
     * 会把这张卡改回真实的 success / failed / cancelled。
     */
    convergeRunningTools(turnId?: string | null) {
      for (const m of this.messages) {
        if (m.role !== "tool" || !m.toolRunning) continue;
        if (turnId && m.turnId && m.turnId !== turnId) continue;
        this._markToolUnknown(m);
      }
    },
    /**
     * 用快照里的**工具执行事实**核对工具卡。
     *
     * 优先级（spec 第 20 条）：
     *
     * * 服务器给出的终态 > 本地过期的「运行中」（服务器知道就不能降级成 unknown）；
     * * 快照之后到达的实时事件 > 快照 —— 由调用方的顺序保证：
     *   快照先应用，同步期间缓存的事件随后按到达顺序补放；
     * * 服务器说「还在跑」时只点亮还没结论的卡片，绝不把已有终态降级回运行中；
     * * 两边都没有结果时才是 `unknown`。
     */
    reconcileTools(records: ToolExecutionSnapshot[]) {
      const byCall = new Map<string, ToolExecutionSnapshot>();
      for (const record of records ?? []) {
        const callId = String(record?.tool_call_id ?? "");
        if (!callId) continue;
        // 子任务内部工具不属于主对话
        if (isInternalToolTurn(record.turn_id)) continue;
        byCall.set(callId, record);
      }
      for (const m of this.messages) {
        if (m.role !== "tool" || !m.callId) continue;
        const record = byCall.get(m.callId);
        // 跨 Turn 不得串状态：别的 Turn 的同名 call_id 不是这次调用的事实，
        // 与「服务器没有这条记录」等价 —— 同样只能收口成 unknown。
        const mismatch = Boolean(record?.turn_id && m.turnId && record.turn_id !== m.turnId);
        if (!record || mismatch) {
          // 服务器也没有这次调用的记录（已按 retention 回收 / 后端重启过）：
          // 这是服务器真的不知道，不是「通知没收到」。
          if (m.toolStatus === "running" || m.toolRunning) this._markToolUnknown(m);
          continue;
        }
        if (record.status === "running") {
          if (!m.toolStatus || m.toolStatus === "unknown") {
            m.toolStatus = "running";
            m.toolRunning = true;
            m.toolOk = undefined;
            m.toolError = null;
          }
          continue;
        }
        if (record.status === "unknown") {
          if (m.toolStatus === "running") this._markToolUnknown(m);
          continue;
        }
        this._applyToolTerminal(m, record.status, record.error_summary ?? null);
      }
    },
    /** 服务器确认不了结果：如实收口成 unknown，绝不伪造 success / failed。 */
    _markToolUnknown(m: StreamMessage) {
      m.toolStatus = "unknown";
      m.toolRunning = false;
      m.toolOk = false;
      m.toolError = m.toolError ?? "结果未收到";
    },
    /** 把服务器知道的终态写进卡片（同一次调用，原位更新）。 */
    _applyToolTerminal(m: StreamMessage, status: ToolStatus, errorSummary: string | null) {
      m.toolStatus = status;
      m.toolRunning = false;
      m.toolOk = status === "success";
      if (status === "success") {
        m.toolError = null;
        return;
      }
      const known = (errorSummary ?? "").trim() || (m.toolError ?? "").trim();
      m.toolError = known || (status === "cancelled" ? "已取消" : null);
    },
    /**
     * 用快照里的活动任务集合核对独立任务卡：不在其中却还显示
     * running / queued 的，说明它的结局事件丢了 —— 按「结果未收到」收口。
     */
    reconcileSubagents(activeTaskIds: string[]) {
      const active = new Set(activeTaskIds.filter(Boolean));
      for (const m of this.messages) {
        if (m.role !== "subagent" || !m.taskId) continue;
        if (m.taskStatus !== "running" && m.taskStatus !== "queued") continue;
        if (active.has(m.taskId)) continue;
        m.taskStatus = "failed";
        m.toolOk = false;
        m.toolError = m.toolError ?? "连接中断，未收到最终结果";
      }
    },
    /** 旧签名（只给 running/queued）：等价于带快照的权威应用，保持向后兼容。 */
    syncTurnQueue(
      runningTurnId: string | null,
      queuedTurnIds: string[],
      revision?: number | null,
      instanceId?: string | null,
    ) {
      this.applyTurnQueue({
        instance_id: instanceId,
        running: runningTurnId ? { turn_id: runningTurnId, message: "" } : null,
        queued: queuedTurnIds.map((turn_id) => ({ turn_id, message: "" })),
        revision,
      });
    },
    /**
     * 继续发送一条「上次没执行」的消息。
     *
     * 只做用户点的那一次：**不自动重发**是产品语义（进程退出可能正是用户的意思，
     * 自动重放会重复花钱、重复产生回答）。
     *
     * 结果必须说清楚，不静默：
     * * 成功 → 该条从入口消失（后端台账已把它标成已处理）；
     * * **409** → 它已经不在「未执行」状态（别处处理过 / 已完成）—— 这时**不猜**，
     *   重新向服务端要一份权威状态，并把这句话原样告诉用户；
     * * 其它失败 → 保留入口，给出可重试的说明。
     */
    async resumeInterruptedTurn(turnId: string): Promise<{ ok: boolean; message: string }> {
      if (this.interruptedBusyId) {
        return { ok: false, message: "上一次操作还在提交中，请稍候" };
      }
      this.interruptedBusyId = turnId;
      this.interruptedNotice = "";
      try {
        const res = await api.resendInterruptedTurn(turnId);
        this.interruptedTurns = this.interruptedTurns.filter((t) => t.turn_id !== turnId);
        // 不把内部 turn_id 抛给用户：他要的是"这条重新发出去了"，不是一串标识
        void res;
        this.interruptedNotice = "已经按原话题重新排队，这一轮马上开始";
        return { ok: true, message: this.interruptedNotice };
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 409) {
          await this.resyncTurnState();
          this.interruptedNotice =
            "这条已经被处理过了（可能已在别处继续、或已被忽略），入口已按后端最新状态刷新";
          return { ok: false, message: this.interruptedNotice };
        }
        this.interruptedNotice = `提交没有成功：${(e as Error).message}（可以重试）`;
        return { ok: false, message: this.interruptedNotice };
      } finally {
        this.interruptedBusyId = "";
      }
    },
    /**
     * 忽略一条：不再提示，但台账记录与消息原文都保留（后端不删用户数据）。
     * 同样只有真正成功才从入口移除；409 时向后端要真相。
     */
    async dismissInterruptedTurn(turnId: string): Promise<{ ok: boolean; message: string }> {
      if (this.interruptedBusyId) {
        return { ok: false, message: "上一次操作还在提交中，请稍候" };
      }
      this.interruptedBusyId = turnId;
      this.interruptedNotice = "";
      try {
        await api.dismissInterruptedTurn(turnId);
        this.interruptedTurns = this.interruptedTurns.filter((t) => t.turn_id !== turnId);
        this.interruptedNotice = "已忽略这一条（原文仍然保留在记录里）";
        return { ok: true, message: this.interruptedNotice };
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 409) {
          await this.resyncTurnState();
          this.interruptedNotice = "这条已经被处理过了，入口已按后端最新状态刷新";
          return { ok: false, message: this.interruptedNotice };
        }
        this.interruptedNotice = `忽略没有成功：${(e as Error).message}（可以重试）`;
        return { ok: false, message: this.interruptedNotice };
      } finally {
        this.interruptedBusyId = "";
      }
    },
    /**
     * 全部忽略：逐条提交，失败的保留在入口里并如实报数（不假装全成功）。
     */
    async dismissAllInterruptedTurns(): Promise<{ ok: boolean; message: string }> {
      if (this.interruptedBusyId) {
        return { ok: false, message: "上一次操作还在提交中，请稍候" };
      }
      const ids = this.interruptedTurns.map((t) => t.turn_id);
      if (!ids.length) return { ok: true, message: "没有需要忽略的记录" };
      this.interruptedBusyId = "ALL";
      this.interruptedNotice = "";
      let failed = 0;
      let already = 0;
      for (const id of ids) {
        try {
          await api.dismissInterruptedTurn(id);
          this.interruptedTurns = this.interruptedTurns.filter((t) => t.turn_id !== id);
        } catch (e) {
          if ((e as { status?: number }).status === 409) already += 1;
          else failed += 1;
        }
      }
      this.interruptedBusyId = "";
      if (failed) {
        this.interruptedNotice = `有 ${failed} 条没有忽略成功（可以重试）；其余已忽略`;
        return { ok: false, message: this.interruptedNotice };
      }
      if (already) {
        // 已经被别处处理过的那些：不猜，直接以后端为准重新对齐
        await this.resyncTurnState();
        this.interruptedNotice = `有 ${already} 条已经被处理过，入口已按后端最新状态刷新`;
        return { ok: true, message: this.interruptedNotice };
      }
      this.interruptedNotice = "已忽略全部未完成的消息（原文仍然保留在记录里）";
      return { ok: true, message: this.interruptedNotice };
    },
    /**
     * 事件流可能已经不完整（收到 RESYNC）：不再假装状态是最新的，
     * 直接向服务器要一份**完整**权威状态并对齐（turn 队列 + 待审批 + 独立任务）。
     */
    async resyncTurnState(): Promise<{
      turn_queue: TurnQueueSnapshot;
      approvals: Awaited<ReturnType<typeof api.getRuntimeState>>["approvals"];
      tasks: Awaited<ReturnType<typeof api.getRuntimeState>>["tasks"];
    } | null> {
      const seq = ++this._runtimeStateSeq;
      try {
        const state = await api.getRuntimeState();
        // 归属校验：期间又发起了一次权威读取 → 这一份是旧的，整段丢弃
        // （队列快照自身还有 revision / instance 校验，这里补的是请求代次）
        if (seq !== this._runtimeStateSeq) return null;
        this.adoptInstance(state.instance_id);
        this.applyTurnQueue(state.turn_queue);
        this.interruptedOperations = (state.interrupted_approvals ?? []).map((item) => ({
          approval_id: item.approval_id,
          kind: item.kind,
          what: item.what,
          createdAt: item.created_at,
        }));
        // 上一次退出时没执行完的用户消息：后端只给「还没被处理过」的那些，
        // 这里照单收下 —— 前端不做第二套「算不算没做完」的判断。
        this.interruptedTurns = state.interrupted_turns ?? [];
        // 开发任务是另一份权威状态（独立的接口）：连上就一起拉，别等用户想起来刷新
        await this.refreshDevTasks();
        return { turn_queue: state.turn_queue, approvals: state.approvals, tasks: state.tasks };
      } catch (e) {
        this.lastError = `状态同步失败，界面显示的状态可能不是最新的：${(e as Error).message}`;
        return null;
      }
    },
    /**
     * 拉一次开发任务列表（连接建立 / RESYNC / 一轮结束之后调用）。
     *
     * 失败时**保留上一次的结果**：这是补充信息，不值得为它弹错误；但也不能把
     * 「拉不到」擦成空列表 —— 那会让用户以为任务没了，或以为事情已经做完。
     */
    async refreshDevTasks(): Promise<void> {
      const seq = ++this._devTasksSeq;
      const instanceAtStart = this.instanceId;
      try {
        const res = await api.getDevTasks();
        // 归属校验：有更新的刷新已经发起 → 这份是旧列表，别覆盖新的
        if (seq !== this._devTasksSeq) return;
        // 归属校验：期间后端换了实例 → 旧实例的任务列表不是当前状态
        if (this.instanceId !== instanceAtStart) return;
        this.devTasks = res.tasks ?? [];
      } catch {
        // 安静地保留旧值（noticeable 的失败由主流程的 lastError 负责，这里不抢戏）
      }
      await this.refreshDevAuthorizations();
    },
    /** 拉一次执行授权范围（展开任务清单、每轮结束时用）。 */
    async refreshDevAuthorizations(): Promise<void> {
      const seq = ++this._devAuthSeq;
      const instanceAtStart = this.instanceId;
      try {
        const res = await api.listDevAuthorizations();
        if (seq !== this._devAuthSeq) return;
        if (this.instanceId !== instanceAtStart) return;
        this.devAuthorizations = res.authorizations ?? [];
      } catch {
        // 同上：这是补充信息，失败保留旧值
      }
    },
    /**
     * 收回一个任务的执行授权。返回是否真的收回 —— 失败时**不能**假装已经收回。
     */
    async revokeDevAuthorization(taskId: string): Promise<boolean> {
      try {
        const res = await api.revokeDevAuthorization(taskId);
        if (!res.revoked) return false;
        this.devAuthorizations = this.devAuthorizations.filter(
          (item) => item.task_id !== taskId,
        );
        const task = this.devTasks.find((item) => item.id === taskId);
        if (task) task.authorized = false;
        return true;
      } catch {
        return false;
      }
    },
    /**
     * 放弃一项没做完的开发任务。返回后端是否确认（`ok`）、状态词与一句人话。
     *
     * **不做乐观移除**：只有 `ok === true` 才把这一条从本地列表去掉。后端说
     * 「正在执行」「已经做完」时，列表一行都不动，原因原样交回界面显示。
     *
     * 网络异常也返回（`status: "network_error"`）而不是抛出：调用方要能如实说
     * 「这次没有放弃」并允许重试，不能把请求失败装成已放弃。
     */
    async abandonDevTask(
      taskId: string,
    ): Promise<{ ok: boolean; status: string; message: string }> {
      let res: DevAbandonResult;
      try {
        res = await api.abandonDevTask(taskId);
      } catch (e) {
        return {
          ok: false,
          status: "network_error",
          message:
            `没能放弃这项开发（请求没有得到后端确认）：${(e as Error).message}。` +
            "这项任务还在列表里，可以再试一次。",
        };
      }
      if (!res.ok) {
        // 后端明确拒绝（正在执行 / 已经做完）：什么都不改，原因原样交给界面
        return { ok: false, status: res.status, message: res.message };
      }
      // 后端已确认放弃：先让界面与「已放弃」对齐，再按权威列表拉一次
      this.devTasks = this.devTasks.filter((item) => item.id !== taskId);
      this.devAuthorizations = this.devAuthorizations.filter(
        (item) => item.task_id !== taskId,
      );
      await this.refreshDevTasks();
      return { ok: true, status: res.status, message: res.message };
    },
    /**
     * 停止「真正在运行的主 turn」。
     *
     * 已知 active → 精确取消它；还不知道 turn_id → 让后端取消 active，
     * 绝不因为用户又发了一条排队消息就取消错对象。
     */
    async stopActiveTurn(): Promise<boolean> {
      const target = this.activeTurnId;
      try {
        if (target) await api.cancelTurn(target);
        else await api.cancelActiveTurn();
        return true;
      } catch (e) {
        this.lastError = `停止失败：${(e as Error).message}`;
        return false;
      }
    },
    pushMessage(
      msg: Omit<StreamMessage, "id" | "createdAt"> & { id?: string; createdAt?: string },
    ) {
      const item: StreamMessage = {
        id: this._nextId(),
        createdAt: new Date().toISOString(),
        topicName: this.topicName,
        turnId: this.activeTurnId,
        // 流式消息一出现就记下到达时间：下一段增量就能算出真实间隔
        ...(msg.streaming ? { deltaAt: Date.now() } : {}),
        ...msg,
      };
      this.messages.push(item);
      // 只有「本次会话里新产生的」消息才有入场动画；历史消息 loadHistory 不走这里
      if (msg.fresh !== false) {
        this.freshIds.push(item.id);
        const id = item.id;
        setTimeout(() => {
          this.freshIds = this.freshIds.filter((x) => x !== id);
        }, 600);
      }
    },
    pushUser(text: string) {
      this.pushMessage({ role: "user", content: text, contentType: "text" });
    },
    pushAssistant(text: string, interim = false, streaming = false) {
      // 若本轮正在产出且最后一条是流式助手消息，则就地更新（避免"过程+最终"两条）
      const last = this.messages[this.messages.length - 1];
      if (streaming && last && last.role === "assistant" && last.streaming) {
        // 记录真实到达节奏：下一段文字按「上一次增量到这次增量的间隔」显示，
        // 这样逐字进度跟的是模型实际速度，而不是一个固定的字/秒估计值。
        const now = Date.now();
        if (last.deltaAt) {
          last.paceMs = Math.min(400, Math.max(40, now - last.deltaAt));
        }
        last.deltaAt = now;
        last.content = text;
        last.interim = true;
        return;
      }
      this.pushMessage({
        role: "assistant",
        content: text,
        contentType: "text",
        ...(interim ? { interim: true } : {}),
        ...(streaming ? { streaming: true } : {}),
      });
    },
    finalizeAssistant() {
      const last = this.messages[this.messages.length - 1];
      if (last && last.role === "assistant" && last.streaming) {
        // 落定（停止逐字），但保留 interim 标记：中间话不是最终答案
        delete last.streaming;
      }
    },
    /**
     * TURN_END.final_content 是最终回答的唯一权威来源。
     * 只有当最后一条助手消息**内容就是它**时才复用，否则单独追加一条 —— 
     * 绝不能因为「最后一条已经是 assistant」就把最终回答丢掉，
     * 也不能把工具前的中间话当成最终答案。
     */
    applyFinalAnswer(text: string, verification?: unknown) {
      const last = this.messages[this.messages.length - 1];
      if (
        last &&
        last.role === "assistant" &&
        !last.streaming &&
        last.content.trim() === text.trim()
      ) {
        last.interim = false;
        this._attachVerification(last, verification);
        return;
      }
      this.pushAssistant(text);
      const added = this.messages[this.messages.length - 1];
      if (added) this._attachVerification(added, verification);
    },
    /** 把后端核对结论挂到这一条回答上（形状不对就当没有，不留半截状态） */
    _attachVerification(message: StreamMessage, verification: unknown) {
      const fact = normalizeVerification(verification);
      if (fact) message.verified = fact;
    },
    /** 没有最终回答（失败/取消）时，别把中间话留在「已落定的最终回答」位置 */
    markLastAssistantInterim() {
      const last = this.messages[this.messages.length - 1];
      if (last && last.role === "assistant" && !last.streaming) last.interim = true;
    },
    pushTool(
      name: string,
      ok: boolean,
      error: string | null,
      preview: string,
      presentation?: ToolPresentation | null,
    ) {
      this.pushMessage({
        role: "tool",
        content: preview,
        contentType: "tool",
        toolName: name,
        toolOk: ok,
        toolStatus: ok ? "success" : "failed",
        toolError: error,
        presentation,
      });
    },
    // -- 执行叙事（Execution Narrative） --------------------------------

    /**
     * 一条叙事到达（实时事件）。
     *
     * 去重是必须的：断线重连会补发、页面恢复可能重放，同一条叙事只能出现一次。
     * 身份用后端落库的消息 id（`narrative_id`）；另外再做一层「同 turn 同 kind 同 text」
     * 的内容级兜底（历史里不同帧可能带来不同的临时 id）。
     */
    applyNarrative(payload: {
      narrative_id?: string;
      turn_id?: string | null;
      kind?: string;
      text?: string;
      call_ids?: string[];
      created_at?: string | null;
    }) {
      const id = String(payload.narrative_id ?? "");
      const text = String(payload.text ?? "").trim();
      const turnId = payload.turn_id ? String(payload.turn_id) : (this.activeTurnId ?? null);
      // 只有 explanation 的叙事不产生过程说明行（它只服务审批窗口）。
      if (!id || !text) return;
      if (this.messages.some((m) => m.id === id)) return;
      const kind = normalizeNarrativeKind(payload.kind);
      const duplicate = this.messages.some(
        (m) =>
          m.role === "narrative" &&
          m.narrativeKind === kind &&
          m.content === text &&
          (m.turnId ?? null) === turnId,
      );
      if (duplicate) return;
      const callIds = Array.isArray(payload.call_ids) ? payload.call_ids.map(String) : [];
      this.pushMessage({
        id,
        createdAt: payload.created_at ?? new Date().toISOString(),
        role: "narrative",
        content: text,
        contentType: "narrative",
        narrativeKind: kind,
        ...(callIds.length ? { narrativeCallIds: callIds } : {}),
        ...(turnId ? { turnId } : {}),
      });
    },

    /**
     * RESYNC 恢复：把服务器已经落库的叙事补进消息流。
     *
     * 按 `narrative_id` 去重，并按 `created_at` 插到时间正确的位置 ——
     * 叙事必须排在它说明的那次工具调用之前，不能挂在末尾。
     */
    mergeNarratives(
      list:
        | {
            narrative_id?: string;
            turn_id?: string | null;
            kind?: string;
            text?: string;
            calls?: { call_id?: string; tool?: string; title?: string; status?: string; error?: string | null; duration_ms?: number | null }[];
            created_at?: string | null;
          }[]
        | undefined,
    ) {
      for (const item of list ?? []) {
        const id = String(item?.narrative_id ?? "");
        const text = String(item?.text ?? "").trim();
        if (!id || !text) continue;
        if (this.messages.some((m) => m.id === id)) continue;
        const kind = normalizeNarrativeKind(item.kind);
        const calls = narrativeCallRecords(item.calls);
        const at = Date.parse(String(item.created_at ?? ""));
        const message: StreamMessage = {
          id,
          role: "narrative",
          content: text,
          contentType: "narrative",
          createdAt: String(item.created_at ?? new Date().toISOString()),
          narrativeKind: kind,
          ...(calls.length ? { narrativeCalls: calls } : {}),
          ...(item.turn_id ? { turnId: String(item.turn_id) } : {}),
          fresh: false,
        };
        const index = Number.isFinite(at)
          ? this.messages.findIndex((m) => {
              const other = Date.parse(m.createdAt);
              return Number.isFinite(other) && other > at;
            })
          : -1;
        if (index < 0) this.messages.push(message);
        else this.messages.splice(index, 0, message);
      }
    },

    /**
     * 工具开始执行（TOOL_START）。
     *
     * 第三阶段的缺陷：前端完全没消费 TOOL_START，用户要等工具跑完才看到一张卡。
     * 现在开始执行就出现「运行中」的卡片，并在 TOOL_END 时**原地**变成结果。
     */
    startTool(
      callId: string,
      toolName: string,
      presentation?: ToolPresentation | null,
      turnId?: string | null,
      arguments_?: Record<string, unknown> | null,
    ) {
      const key = callId || toolName;
      // 工具创建流程：折叠进创建卡（进度由 TOOL_CREATE_STATUS 提供）
      if (TOOL_CREATION_TOOLS.has(toolName)) {
        const workspace = String((arguments_ ?? {}).workspace ?? "").trim();
        if (key && workspace) this._creationGroupByCall = { ...this._creationGroupByCall, [key]: workspace };
        if (key) this._suppressedToolCalls = [...this._suppressedToolCalls, key];
        return;
      }
      const existing = key ? this.messages.find((m) => m.role === "tool" && m.callId === key) : undefined;
      if (existing) {
        existing.toolRunning = true;
        existing.toolStatus = "running";
        existing.toolOk = undefined;
        existing.toolError = null;
        existing.presentation = presentation ?? existing.presentation;
        return;
      }
      this.pushMessage({
        role: "tool",
        content: "",
        contentType: "tool",
        toolName,
        presentation: presentation ?? null,
        toolRunning: true,
        toolStatus: "running",
        ...(key ? { callId: key } : {}),
        ...(turnId ? { turnId } : {}),
      });
    },
    /**
     * 工具结束（TOOL_END）：按 `call_id` 更新**同一张**卡。
     * 找不到对应卡（重连重放 / 丢帧）时补一张 —— 结果不能丢。
     */
    finishTool(
      callId: string,
      toolName: string,
      ok: boolean,
      error: string | null,
      preview: string,
      presentation?: ToolPresentation | null,
      durationMs?: number,
      status?: ToolStatus | null,
      recordId?: string | null,
    ) {
      const key = callId || toolName;
      // 折叠掉的创建流程调用：失败要写回创建卡，不能让用户看不到原因
      if (TOOL_CREATION_TOOLS.has(toolName) || this._suppressedToolCalls.includes(key)) {
        const groupId = this._creationGroupByCall[key];
        if (!ok && groupId) {
          this.upsertToolCreation(groupId, {
            phase: "failed",
            label: "创建失败",
            detail: (error ?? "").trim() || "这一步没有完成",
            ok: false,
          });
        }
        return;
      }
      const existing = key ? this.messages.find((m) => m.role === "tool" && m.callId === key) : undefined;
      const target =
        existing ?? this.messages.find((m) => m.role === "tool" && !m.callId && m.toolName === toolName && m.toolRunning);
      // 后端事件里的 status 是权威语义；老格式（没有 status）才用 ok 兜底 ——
      // 但「取消」不能靠 ok 反推，所以缺 status 时只区分成功 / 失败。
      const terminal: ToolStatus = status ?? (ok ? "success" : "failed");
      if (target) {
        target.toolRunning = false;
        target.toolStatus = terminal;
        target.toolOk = terminal === "success";
        target.toolError = error;
        if (preview) target.content = preview;
        target.presentation = presentation ?? target.presentation;
        if (key && !target.callId) target.callId = key;
        if (typeof durationMs === "number" && Number.isFinite(durationMs) && durationMs >= 0) {
          target.toolDurationMs = durationMs;
        }
        // 工具调用历史的记录 id：卡片展开时靠它取参数与输出全文
        if (recordId) target.toolRecordId = recordId;
        return;
      }
      this.pushMessage({
        role: "tool",
        content: preview,
        contentType: "tool",
        toolName,
        toolOk: terminal === "success",
        toolStatus: terminal,
        toolError: error,
        presentation: presentation ?? null,
        toolRunning: false,
        ...(key ? { callId: key } : {}),
        ...(typeof durationMs === "number" ? { toolDurationMs: durationMs } : {}),
        ...(recordId ? { toolRecordId: recordId } : {}),
      });
    },
    /**
     * 独立任务状态（SUBAGENT_STATUS）：同一 `task_id` 只有一张卡。
     * 用户看到的是「独立任务」，不是普通工具，也不是内部智能体进程。
     */
    upsertSubagent(
      taskId: string,
      data: {
        status: "queued" | "running" | "done" | "failed";
        toolName?: string;
        goal?: string;
        ok?: boolean | null;
        preview?: string;
        error?: string | null;
      },
    ) {
      const existing = this.messages.find((m) => m.role === "subagent" && m.taskId === taskId);
      if (existing) {
        existing.taskStatus = data.status;
        if (data.goal) existing.taskGoal = data.goal;
        if (data.toolName) existing.toolName = data.toolName;
        if (typeof data.preview === "string" && data.preview) existing.content = data.preview;
        if (data.status === "done" || data.status === "failed") {
          existing.toolOk = data.status === "done";
          existing.toolError = data.error ?? null;
        }
        return;
      }
      this.pushMessage({
        role: "subagent",
        content: data.preview ?? "",
        contentType: "text",
        toolName: data.toolName ?? "独立任务",
        taskId,
        taskStatus: data.status,
        ...(data.goal ? { taskGoal: data.goal } : {}),
        ...(data.status === "done" || data.status === "failed"
          ? { toolOk: data.status === "done", toolError: data.error ?? null }
          : {}),
      });
    },
    /**
     * 工具创建进度（TOOL_CREATE_STATUS）：同一 `group_id`（开发工作区）一张卡，
     * 逐步变化，而不是「创建工具卡 → 测试卡 → 审批卡 → 成功卡」一串卡。
     */
    upsertToolCreation(
      groupId: string,
      data: {
        phase: string;
        label?: string;
        detail?: string;
        ok?: boolean | null;
        toolName?: string;
        turnId?: string | null;
      },
    ) {
      const existing = this.messages.find(
        (m) => m.role === "tool_creation" && m.groupId === groupId,
      );
      if (existing) {
        existing.createPhase = data.phase;
        if (data.label) existing.content = data.label;
        existing.presentation = {
          ...(existing.presentation ?? {}),
          ...(data.label ? { title: data.label } : {}),
          ...(data.detail ? { summary: data.detail } : {}),
        };
        if (typeof data.ok === "boolean") {
          existing.toolOk = data.ok;
          existing.toolError = data.ok ? null : (data.detail ?? "创建没有完成");
        }
        if (data.toolName) existing.createdToolName = data.toolName;
        return;
      }
      this.pushMessage({
        role: "tool_creation",
        content: data.label ?? "正在准备",
        contentType: "text",
        groupId,
        createPhase: data.phase,
        createdToolName: data.toolName,
        toolOk: data.ok ?? undefined,
        toolError: data.ok === false ? (data.detail ?? "创建没有完成") : null,
        presentation: {
          ...(data.label ? { title: data.label } : {}),
          ...(data.detail ? { summary: data.detail } : {}),
        },
        ...(data.turnId ? { turnId: data.turnId } : {}),
      });
    },
    // -- 高影响知识候选 -------------------------------------------------
    /** 收到候选：先缓冲（回答还没完成时不能弹出来打断） */
    queueKnowledgeCandidate(candidate: KnowledgeCandidate) {
      if (!candidate.knowledgeId) return;
      const known = (list: KnowledgeCandidate[]) =>
        list.some((c) => c.knowledgeId === candidate.knowledgeId);
      if (known(this._queuedCandidates) || known(this.knowledgeCandidates)) return;
      this._queuedCandidates.push(candidate);
    },
    /** 回答完成（TURN_END）：候选现在才出现在对话里 */
    flushKnowledgeCandidates() {
      if (!this._queuedCandidates.length) return;
      this.knowledgeCandidates = [...this.knowledgeCandidates, ...this._queuedCandidates];
      this._queuedCandidates = [];
    },
    _dropCandidate(knowledgeId: string) {
      this.knowledgeCandidates = this.knowledgeCandidates.filter(
        (c) => c.knowledgeId !== knowledgeId,
      );
      this._queuedCandidates = this._queuedCandidates.filter(
        (c) => c.knowledgeId !== knowledgeId,
      );
    },
    /** 保存：批准这条长期知识（让它真正生效） */
    async saveCandidate(knowledgeId: string) {
      const key = knowledgeId;
      if (this.candidateState[key] === "busy") return;
      this.candidateState = { ...this.candidateState, [key]: "busy" };
      this.candidateError = { ...this.candidateError, [key]: "" };
      try {
        await api.verifyKnowledge(knowledgeId);
        this._dropCandidate(knowledgeId);
      } catch (e) {
        this.candidateState = { ...this.candidateState, [key]: "failed" };
        this.candidateError = {
          ...this.candidateError,
          [key]: `没能保存这条长期信息：${(e as Error).message}（可以重试）`,
        };
      }
    },
    /** 修改：进入很轻的内联编辑，保存后生成新版本并生效 */
    async editCandidate(knowledgeId: string, content: string) {
      const text = content.trim();
      if (!text) return;
      const key = knowledgeId;
      if (this.candidateState[key] === "busy") return;
      this.candidateState = { ...this.candidateState, [key]: "busy" };
      this.candidateError = { ...this.candidateError, [key]: "" };
      try {
        await api.reviseKnowledge(knowledgeId, text);
        this._dropCandidate(knowledgeId);
      } catch (e) {
        this.candidateState = { ...this.candidateState, [key]: "failed" };
        this.candidateError = {
          ...this.candidateError,
          [key]: `修改没有保存：${(e as Error).message}（可以重试）`,
        };
      }
    },
    /** 忽略：用户不要这条长期知识 → 记录状态，之后不再重复问 */
    async ignoreCandidate(knowledgeId: string) {
      const key = knowledgeId;
      if (this.candidateState[key] === "busy") return;
      this.candidateState = { ...this.candidateState, [key]: "busy" };
      this.candidateError = { ...this.candidateError, [key]: "" };
      try {
        await api.ignoreKnowledge(knowledgeId);
        this._dropCandidate(knowledgeId);
      } catch (e) {
        this.candidateState = { ...this.candidateState, [key]: "failed" };
        this.candidateError = {
          ...this.candidateError,
          [key]: `没能记下「忽略」：${(e as Error).message}（可以重试）`,
        };
      }
    },
    turnEnded() {
      this.turnRunning = false;
      this.turnPhase = "idle";
      this.activity = "idle";
      this.cancelling = null;
    },
    async loadHistory() {
      const seq = ++this._historyRequestSeq;
      const topicAtStart = this.currentTopicId;
      // 这次加载开始时就存在的消息 id：用来识别「加载期间新到的」本地消息
      const idsAtStart = new Set(this.messages.map((m) => m.id));
      // 这次完整加载作废所有在飞的旧分页票据：它们的 finally 不许再动 loading 标记
      this._historyOlderToken += 1;
      this.history = { status: "loading", error: null };
      try {
        const ctx = await api.getSessionContext(HISTORY_PAGE_SIZE);
        // 归属校验（成功路径）：期间换了话题 / 又发起了新的加载 → 这一份是旧答案
        if (!this._historyResultBelongs(seq, topicAtStart)) return;
        this.currentTopicId = ctx.topic_id;
        this.topicName = ctx.topic_name ?? null;
        this.anchorFragment = ctx.anchor_fragment ?? null;
        this.anchorFragmentId = ctx.anchor_fragment?.id ?? null;
        this.anchorHistoric = ctx.anchor_fragment?.historic ?? false;
        // 重新进入（或换了 Topic）：分页状态必须一起重置，
        // 否则会拿上一个话题的游标去翻新话题的历史。
        this.historyHasMore = Boolean(ctx.has_more);
        this.historyCursor = ctx.next_before ?? null;
        this.historyOlderLoading = false;
        const snapshot = this._mergeHistory(ctx.messages, ctx.tool_records ?? []);
        /**
         * 快照是「请求发起那一刻」的历史。加载期间新到的本地消息（乐观消息、
         * 实时助手/工具/叙事条目）比它更新，不能被整段替换掉 ——
         * 用户刚发出去的那句话不能因为一次历史刷新凭空消失。
         * 只保留**加载期间新到的**：加载之前就有的本地消息按老规矩让快照接手，
         * 否则后端已经收录的那条会以两个 id 重复出现。
         */
        const arrivedDuring = this.messages.filter(
          (m) => m.id.startsWith("local_") && !idsAtStart.has(m.id),
        );
        this.messages = arrivedDuring.length
          ? [...snapshot, ...arrivedDuring].sort((a, b) =>
              a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0,
            )
          : snapshot;
        this.history = { status: "ready", error: null };
      } catch (e) {
        // 归属校验（错误路径）：旧请求的失败不能把**新**话题标成 error
        if (!this._historyResultBelongs(seq, topicAtStart)) return;
        // 读不到 ≠ 没有：保留已经加载过的消息，只把失败状态交给界面显示与重试
        this.history = { status: "error", error: (e as Error).message };
      }
    },
    /**
     * 这份历史结果还属于当前页面吗？
     *
     * 两个条件同时满足才算数：它是最新一次请求的结果，且发起时的话题仍是当前话题。
     * 成功、错误、finally 三处都要过这一关 —— 慢响应回来时页面可能已经换了话题、
     * 或者用户已经重新加载过一轮。
     */
    _historyResultBelongs(seq: number, topicAtStart: string | null): boolean {
      return seq === this._historyRequestSeq && this.currentTopicId === topicAtStart;
    },
    /**
     * 把这一页的消息与工具调用记录合成一条时间线。
     *
     * 工具记录按 `created_at` 插进消息之间，所以历史里的顺序与当时一致：
     * 你的话 → 过程说明行 → 工具卡 → 助手回答。跨页边界时同一条记录可能被
     * 两页各带一次，按记录 id 去重后不丢不重。
     */
    _mergeHistory(
      messages: {
        id: string;
        role: string;
        content: string;
        content_type: string;
        created_at: string;
        raw?: string;
      }[],
      records: ToolRecordPreview[],
    ): StreamMessage[] {
      const items: StreamMessage[] = messages.map((m) => this._historyMessage(m));
      const known = new Set(items.map((i) => i.toolRecordId).filter(Boolean) as string[]);
      for (const record of records) {
        if (known.has(record.id)) continue;
        known.add(record.id);
        items.push(this._historyToolRecord(record));
      }
      // 时间升序；同一时刻保持原有先后（V8 的 sort 是稳定的）
      items.sort((a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0));
      return items;
    },
    /** 一条工具调用记录 → 消息流里的工具卡（预览态；展开时才取全文） */
    _historyToolRecord(r: ToolRecordPreview): StreamMessage {
      return {
        id: `toolrec_${r.id}`,
        role: "tool",
        content: r.preview,
        contentType: "text",
        createdAt: r.created_at,
        topicName: this.topicName,
        fresh: false,
        toolName: r.tool_name,
        callId: r.call_id,
        toolRecordId: r.id,
        toolRecordLoaded: false,
        toolStatus: (r.status as ToolStatus) ?? "success",
        toolOk: r.status === "success",
        toolError: r.error || null,
        toolDurationMs: r.duration_ms ?? undefined,
        toolTruncated: r.truncated,
        toolOutputMissing: r.output_missing,
        toolMissingReason: r.missing_reason,
        presentation: { title: r.title, tool: r.tool_name },
      };
    },
    /**
     * 取一次工具调用的全文（参数 + 输出）。
     *
     * 实时与历史两种卡片共用这一条路径：库里只有预览时展开才调，
     * 写回同一条消息（原位更新，不新起一张卡）。输出被清理 / 没保存时
     * 不清空已有的预览内容 —— 预览也是真实内容。
     */
    async loadToolRecord(messageId: string) {
      const m = this.messages.find((x) => x.id === messageId);
      if (!m || !m.toolRecordId || m.toolRecordLoaded || m.toolRecordLoading) return;
      m.toolRecordLoading = true;
      m.toolRecordError = null;
      try {
        const record = await api.getToolRecord(m.toolRecordId);
        m.toolRecordLoaded = true;
        m.toolArgs = formatToolArguments(record.arguments);
        m.toolTruncated = Boolean(record.truncated);
        m.toolOutputMissing = Boolean(record.output_missing);
        m.toolMissingReason = record.missing_reason || "";
        if (!record.output_missing && record.output) {
          m.content = record.output;
        }
      } catch (e) {
        m.toolRecordError = (e as Error).message || "读取失败";
      } finally {
        m.toolRecordLoading = false;
      }
    },
    /** 历史消息 → 消息流条目（历史不走入场动画，也不带 queued 之类的临时标记） */
    _historyMessage(m: {
      id: string;
      role: string;
      content: string;
      content_type: string;
      created_at: string;
      raw?: string;
    }): StreamMessage {
      if (m.content_type === "narrative") {
        // 历史里的叙事行：模型文案 + 系统生成的调用摘要（工具卡本身不进历史）
        const meta = parseNarrativeRaw(m.raw);
        return {
          id: m.id,
          role: "narrative",
          content: m.content,
          contentType: m.content_type,
          createdAt: m.created_at,
          topicName: this.topicName,
          fresh: false,
          narrativeKind: meta.kind ?? "progress",
          ...(meta.calls ? { narrativeCalls: meta.calls } : {}),
        };
      }
      return {
        id: m.id,
        role: m.role as StreamMessage["role"],
        content: m.content,
        contentType: m.content_type,
        createdAt: m.created_at,
        topicName: this.topicName,
        fresh: false,
        ...(m.role === "tool"
          ? { toolName: "tool", toolOk: true, toolStatus: "success" as const, toolError: null }
          : {}),
        ...(m.role === "assistant" && parseVerifiedRaw(m.raw)
          ? { verified: parseVerifiedRaw(m.raw) as VerifiedFact }
          : {}),
      };
    },
    /**
     * 向上读时加载更早的一页。
     *
     * 按 id 去重：新消息持续产生时，分页边界处后端可能把本地已有的消息再返回一次，
     * 直接 concat 会出现重复条目（虚拟列表按 index 渲染，重复 id 会直接报错或串行）。
     */
    async loadOlderHistory(): Promise<boolean> {
      const cursor = this.historyCursor;
      if (!this.historyHasMore || !cursor || this.historyOlderLoading) return false;
      const token = ++this._historyOlderToken;
      const seq = this._historyRequestSeq;
      const topicAtStart = this.currentTopicId;
      this.historyOlderLoading = true;
      try {
        const page = await api.getSessionMessagesBefore(
          this.currentTopicId,
          cursor,
          HISTORY_PAGE_SIZE,
        );
        // 归属校验：换了话题 / 重新加载过 → 这一页属于旧页面，整段丢弃
        // （不串进新列表、不改新话题的游标与 has_more）
        if (!this._historyResultBelongs(seq, topicAtStart)) return false;
        const known = new Set(this.messages.map((m) => m.id));
        const knownRecords = new Set(
          this.messages.map((m) => m.toolRecordId).filter(Boolean) as string[],
        );
        const older = page.messages
          .filter((m) => !known.has(m.id))
          .map((m) => this._historyMessage(m));
        // 工具记录同样按 id 去重：跨页边界时同一轮可能被两页各带一次
        for (const record of page.tool_records ?? []) {
          if (knownRecords.has(record.id)) continue;
          knownRecords.add(record.id);
          older.push(this._historyToolRecord(record));
        }
        older.sort((a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0));
        this.messages = [...older, ...this.messages];
        this.historyHasMore = Boolean(page.has_more);
        this.historyCursor = page.next_before ?? null;
        return older.length > 0;
      } catch (e) {
        // 归属校验（错误路径）：旧分页的失败不该写进新话题的提示
        if (this._historyResultBelongs(seq, topicAtStart)) {
          this.lastError = `更早的历史没有加载出来：${(e as Error).message}`;
        }
        return false;
      } finally {
        // 只有还持有当前票据时才解锁：旧分页的 finally 不许解锁**新**分页
        if (token === this._historyOlderToken) this.historyOlderLoading = false;
      }
    },
    /** 顶部提示里的「重试」：同一份状态机再跑一次 */
    async retryHistory() {
      if (this.history.status === "loading") return;
      await this.loadHistory();
    },
    /**
     * 发送一轮消息。turnRunning 时后端会排队（TURN_QUEUE 事件回执），
     * 因此仍然允许提交：本地先以「等待中」状态呈现，不阻塞用户写下一条。
     */
    async send(text: string): Promise<boolean> {
      const message = text.trim();
      if (!message) return false;
      const draftKeeper = chatDraftKeeperFor(this as object);
      /**
       * 归属在**点击那一刻**就已经定下（契约 §10.1）：这里只消费那次记录，
       * 不再读「现在的话题」—— 清空输入框之后、等待回执期间切换话题，
       * 都不许改变这条消息的去向。
       */
      const attribution = draftKeeper
        ? draftKeeper.takeAttribution(message)
        : {
            draftId: mintSendIdentity(),
            topicId: this.currentTopicId,
            text: message,
            draftSeq: 0,
            key: "",
            at: Date.now(),
            recorded: false,
          };
      // 原文先受保护：清空输入框引起的「删草稿」写入在拿到受理结果之前被按住
      draftKeeper?.protectForSend(attribution);
      const queued = this.turnRunning;
      this.pushUser(message);
      const optimistic = this.messages[this.messages.length - 1];
      if (queued) {
        optimistic.queued = true;
        this.queuedMessageIds.push(optimistic.id);
      } else {
        this.turnStarted();
      }
      // 本机发送：允许把消息流拉回底部跟随（用户刚写完，想看结果）
      this.localSendSeq += 1;
      try {
        const res = await api.sendTurn(message, attribution.topicId);
        // 受理 ≠ 开始执行：SEND 只告诉我们「后端收下了这条消息」。
        // 绝不能在这里写 activeTurnId —— 排队中的 turn 被当成 active 会同时造成
        // 两个后果：Stop 打到错的目标，以及真正在跑那一轮的 TURN_END 被丢弃。
        if (res && res.turn_id) {
          optimistic.turnId = res.turn_id;
          if (queued) {
            this.markTurnQueued(res.turn_id);
          } else if (!this.endedTurnIds.includes(res.turn_id)) {
            // 只有「还没结束的」才点亮运行态：快轮次的 TURN_END 可能已经先到，
            // 那时再点亮就永远不会有人来熄灭它（真实缺陷：界面卡在运行中）。
            this.turnRunning = true;
            this.turnPhase = "waiting";
          }
        }
        // 受理成功：只清掉这一次发送对应的**旧版本**草稿（身份 + 版本，迁移后按新位置找），
        // 同话题后来写的新草稿、别的话题的草稿一律不动（契约 §10.2 / §11.5）
        draftKeeper?.settleSend(attribution, true);
        // 这条失败原文已经成功发出去了：按身份/文字精确解除，别的待恢复原文不受影响（§11.4）
        this._clearFailedSendsAccepted(attribution, message);
        return true;
      } catch (e) {
        const reason = (e as Error).message;
        this.lastError = reason;
        // 通知消息流：这次发送没有被受理，界面要回到发送前的样子
        this.sendRejectedSeq += 1;
        // 这条请求没有被后端接受：撤掉乐观消息，
        // 避免「界面上有一条没发出去的消息」这种误导状态。
        this.messages = this.messages.filter((m) => m.id !== optimistic.id);
        this.queuedMessageIds = this.queuedMessageIds.filter((id) => id !== optimistic.id);
        // 只有「不是排队」的失败才说明当前 active turn 没起来。
        // 排队请求失败不能把仍在运行的其他任务一起标记成已结束。
        if (!queued) this.turnRunning = false;
        draftKeeper?.settleSend(attribution, false);
        /**
         * 失败事实由会话层统一记录（契约 §10.1）：这里**不碰任何输入框**。
         * 恢复只发生在原话题、且只由用户点恢复入口触发（不自动重发）。
         */
        if (!isBlankText(attribution.text)) {
          /**
           * 失败事实进会话层、并立刻写进本机存储（契约 §11.4）：
           * 两个入口都只读这份事实，不各写一套「失败了要不要把字放回去」。
           *
           * 原文放回输入框由入口在拿到结果后调用 `retryFailedSend(本次归属)` 完成：
           * 只看这一条失败事实，不猜、不自动发送、不覆盖用户后来的输入。
           */
          this._recordFailedSend(attribution, reason || "原因未知");
        }
        return false;
      }
    },
    /** 取消排队中的消息（只影响该条，不动 active turn） */
    dequeue(messageId: string) {
      this.queuedMessageIds = this.queuedMessageIds.filter((id) => id !== messageId);
      const msg = this.messages.find((m) => m.id === messageId);
      if (msg) msg.queued = false;
    },
    /** 后端队列已空（如排队项被取消）：清除本地所有「等待中」标记，避免状态残留 */
    clearQueuedFlags() {
      this.queuedMessageIds = [];
      for (const m of this.messages) if (m.queued) m.queued = false;
    },
  },
});

/* ============================================================================
 * 聊天草稿的持久化（契约 §9.4）
 *
 * 为什么要包一层 useSessionStore：
 * - 草稿的读写入口是 `session.draft`：Composer.vue（对话页）与 ChatDock.vue（悬浮聊天）
 *   都用 v-model 直接读写它。**不能**把它改成 getter/方法（会打断这两个既有组件），
 *   也**不能**再建一份草稿状态（§9.4 要求单一写者），所以持久化只能挂在 store 上。
 * - 没有任何一个 action 会在「每次击键」时被调用，所以草稿与本机存储的连线由 watch 建立。
 *   这一层只加草稿持久化，**不新建事件订阅、不碰轮次管理、不发消息**。
 *
 * 单一写者：同一 pinia 实例的所有草稿读写都经过同一个保存器（WeakMap 按实例存放，
 * 测试里同时建多个 pinia 也不会互相串）。
 * ========================================================================== */

/** 聊天草稿防抖：停下输入多久后落盘（不阻塞输入，也不至于刷新丢一大段） */
const CHAT_DRAFT_DEBOUNCE_MS = 400;

/** 每次发送的稳定身份序号：同一标签页内单调递增，保证身份不重号 */
let sendIdentitySeq = 0;

/**
 * 生成一次发送的稳定身份（契约 §11.5）。
 *
 * 不依赖加密随机源（Tauri/离线环境也可能拿不到），只要求本机唯一：
 * 时间 + 自增序号 + 一小段随机后缀，足够区分先后到达的多个回执。
 */
function mintSendIdentity(): string {
  sendIdentitySeq += 1;
  return `send_${Date.now().toString(36)}_${sendIdentitySeq.toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

/** 一条失败事实自己的 id（多条失败互不覆盖） */
function mintFailedSendId(): string {
  sendIdentitySeq += 1;
  return `fail_${Date.now().toString(36)}_${sendIdentitySeq.toString(36)}`;
}

/** 每个话题最多保留多少条待恢复原文（有界；只丢最旧的，不静默丢最新的） */
const FAILED_SEND_TOPIC_LIMIT = 8;

/**
 * 按话题裁剪（新的在前）：每个话题只留最近 FAILED_SEND_TOPIC_LIMIT 条。
 * 上限是产品口径的一部分：本机存储有配额，恢复入口也不该无限堆积。
 */
function trimFailedSendsPerTopic(records: FailedSend[]): FailedSend[] {
  const seen = new Map<string, number>();
  const kept: FailedSend[] = [];
  for (const record of records) {
    const key = record.topicId ?? UNBOUND_DRAFT_ID;
    const count = (seen.get(key) ?? 0) + 1;
    seen.set(key, count);
    if (count <= FAILED_SEND_TOPIC_LIMIT) kept.push(record);
  }
  return kept;
}

/**
 * 失败原文的本机存储（契约 §11.4：必须经得起刷新与关闭重开）。
 *
 * 为什么不用聊天草稿的那套键：一条草稿键只能存一份正文，而失败原文与「用户后来
 * 输入的新文字」必须同时存在；用单个内存槽又会互相覆盖。这里按话题分组存**一组**
 * 失败事实（正文 + 原因 + 归属身份 + 时间），与草稿记录互不干扰。
 */
const FAILED_SEND_STORAGE_KEY = "qio.chat.failedSend.v1";

/** 存进本机的形状：在会话层形状上多带一条真实原因（界面刷新后仍要说清为什么没发出去） */
interface PersistedFailedSend extends FailedSend {
  error?: string | null;
}

interface FailedSendStorageState {
  version: 1;
  topics: Record<string, PersistedFailedSend[]>;
}

function resolveLocalStorage(): {
  storage: { getItem(key: string): string | null; setItem(key: string, value: string): void } | null;
  error?: string;
} {
  try {
    const backend = (globalThis as { localStorage?: { getItem(k: string): string | null; setItem(k: string, v: string): void } | null })
      .localStorage;
    if (!backend) return { storage: null, error: "当前环境没有本地存储，失败原文无法保存在本机" };
    return { storage: backend };
  } catch (err) {
    return { storage: null, error: "浏览器不允许使用本地存储：" + describeLocalError(err) };
  }
}

function describeLocalError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  const text = String(err ?? "").trim();
  return text || "原因未知";
}

/** 归一化一条从本机读回来的失败记录；形状不对时返回 null（当作没有这条） */
function normalizePersistedFailedSend(raw: unknown): PersistedFailedSend | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  if (typeof record.text !== "string" || !record.text) return null;
  const topicId = typeof record.topicId === "string" && record.topicId ? record.topicId : null;
  return {
    id: typeof record.id === "string" && record.id ? record.id : mintFailedSendId(),
    draftId: typeof record.draftId === "string" && record.draftId ? record.draftId : undefined,
    topicId,
    text: record.text,
    draftSeq: typeof record.draftSeq === "number" && Number.isFinite(record.draftSeq) ? record.draftSeq : 0,
    at: typeof record.at === "number" && Number.isFinite(record.at) ? record.at : 0,
    error: typeof record.error === "string" && record.error ? record.error : null,
  };
}

/**
 * 读回本机保存的全部失败原文。
 *
 * 读不出来（没有存储 / 内容损坏）时如实回报错误，**不假装恢复成功**；
 * 单条形状不对只丢那一条，不因为一条坏记录丢掉整份清单。
 */
function readFailedSendStateFromStorage(): { records: PersistedFailedSend[]; error: string | null } {
  const { storage, error } = resolveLocalStorage();
  if (!storage) return { records: [], error: error ?? null };
  let raw: string | null = null;
  try {
    raw = storage.getItem(FAILED_SEND_STORAGE_KEY);
  } catch (err) {
    return { records: [], error: "读不出本机保存的失败原文：" + describeLocalError(err) };
  }
  if (!raw) return { records: [], error: null };
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("不是对象");
    const topics = (parsed as { topics?: unknown }).topics;
    if (!topics || typeof topics !== "object" || Array.isArray(topics)) throw new Error("没有 topics");
    const records: PersistedFailedSend[] = [];
    for (const list of Object.values(topics as Record<string, unknown>)) {
      if (!Array.isArray(list)) continue;
      for (const item of list) {
        const record = normalizePersistedFailedSend(item);
        if (record) records.push(record);
      }
    }
    records.sort((a, b) => b.at - a.at);
    return { records, error: null };
  } catch (err) {
    return {
      records: [],
      error: "本机保存的失败原文读不出来，已按没有处理：" + describeLocalError(err),
    };
  }
}

/** 把失败原文写进本机；失败返回真实原因（调用方必须显示，不许静默） */
function writeFailedSendStateToStorage(
  records: FailedSend[],
  errors: Record<string, string>,
): { error: string | null } {
  const { storage, error } = resolveLocalStorage();
  if (!storage) return { error: error ?? "本地存储不可用，失败原文无法保存在本机" };
  const topics: Record<string, PersistedFailedSend[]> = {};
  for (const record of records) {
    const key = record.topicId ?? UNBOUND_DRAFT_ID;
    if (!topics[key]) topics[key] = [];
    // 原因跟着记录一起存：刷新后仍然要说清「为什么没发出去」，不能只剩一段文字
    topics[key].push({ ...record, error: record.id ? errors[record.id] ?? null : null });
  }
  // 没有待恢复原文时写入空清单：读回来等价于「从来没有过」，不留半条旧记录
  const payload: FailedSendStorageState = { version: 1, topics };
  try {
    storage.setItem(FAILED_SEND_STORAGE_KEY, JSON.stringify(payload));
    return { error: null };
  } catch (err) {
    return { error: "失败原文没有保存在本机：" + describeLocalError(err) };
  }
}

/** 保存器需要的最小 store 形状（只管草稿与本机存储状态，不碰会话的网络能力） */
interface SessionDraftHost {
  draft: string;
  currentTopicId: string | null;
  draftSaveStatus: "idle" | "saving" | "saved" | "error";
  draftSaveError: string | null;
}

/** 话题 → 存储键；话题还没确定（currentTopicId 为 null）时用占位键，等话题确定后迁移。 */
function chatDraftKeyFor(topicId: string | null): string {
  return draftStorageKey("chat", topicId || UNBOUND_DRAFT_ID);
}

function createChatDraftKeeper(host: SessionDraftHost) {
  const scope = effectScope();
  /** 防抖计时器（null = 没有待写内容） */
  let timer: ReturnType<typeof setTimeout> | null = null;
  /** 当前草稿绑定的存储键（始终有效：话题未知时是占位键） */
  let key = chatDraftKeyFor(host.currentTopicId);
  /** 本保存器上单调递增的写入序号：迟到的写入用它判断自己是否已经过期 */
  let seq = 0;
  /** 当前键上最后一次**成功写进存储**的正文（null = 存储里还没有记录） */
  let savedText: string | null = null;
  /** 正在把存储内容装回输入框：这次赋值不算用户输入，不该再写一次 */
  let loadingStored = false;
  /**
   * 已经发出、还在等受理结果的原文（契约 §10.1）。
   *
   * 每条记的是「身份 + 现在的键 + 版本 + 原文」：在拿到回执之前，清空输入框
   * （或切话题时的落盘）都不许把这条原文删掉。用列表而不是单个位置：
   * 排队发送时确实可能有两条同时在飞。
   * 话题迁移会改这里的 key（契约 §11.5），受理成功时按这里的**新位置**清理。
   */
  const inFlightSends: SendAttribution[] = [];
  /**
   * 存储键 → 话题（契约 §11.5）。
   *
   * 失败事实要记在「被发送的文字现在属于哪个话题」上：未绑定话题时点击发送、
   * 服务器随后绑定真实话题的情况下，只按点击那一刻的 null 记，就会让恢复入口
   * 在真正的话题里找不到。
   */
  const keyTopics = new Map<string, string | null>();
  keyTopics.set(key, host.currentTopicId);
  /** 点击发送那一刻记下的归属；send() 消费它，消费后即失效 */
  let pendingAttribution: SendAttribution | null = null;

  function setStatus(status: SessionDraftHost["draftSaveStatus"], error: string | null): void {
    host.draftSaveStatus = status;
    host.draftSaveError = error;
  }

  function cancelTimer(): void {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  /**
   * 真正写一次存储。atSeq 对应这次写入的内容：如果期间又有了更新的输入
   * （seq 已经往前走），这次写入整段丢弃 —— 旧内容不许覆盖新内容。
   */
  function commit(atSeq: number, atKey: string, text: string): void {
    // 已经有更新的写入在排队：这次整段丢弃，状态由那次写入决定
    if (isStaleReceipt(atSeq, seq)) return;
    if (atKey !== key) {
      // 防抖期间话题被换掉：这段内容属于旧键，不能写进新键。
      // 但也不能把状态永远停在「保存中」——按当前键重新安排一次保存。
      armTimer();
      return;
    }
    /**
     * 正在等受理结果的原文：清空输入框（或切话题落盘）不许把它删掉（契约 §10.1）。
     * 用户在发送期间写下了更新的内容时，保护自动结束 —— 那条原文仍在失败记录里，
     * 不会因为这里放手而丢。
     */
    const held = inFlightSends.find((item) => item.key === atKey);
    if (held) {
      if (isBlankText(text)) {
        // 原文还压在存储里：这一小段没有待保存的内容，状态别停在「保存中」
        if (isBlankText(host.draft)) setStatus("idle", null);
        return;
      }
      if (atSeq > held.draftSeq) {
        for (let i = inFlightSends.length - 1; i >= 0; i -= 1) {
          if (inFlightSends[i].key === atKey && inFlightSends[i].draftSeq < atSeq) {
            inFlightSends.splice(i, 1);
          }
        }
      }
    }
    if (isBlankText(text)) {
      // 空草稿就是「没有草稿」：删掉记录，不留一条看着像有草稿的空记录
      removeDraft(atKey);
      savedText = null;
      setStatus("idle", null);
      return;
    }
    const result = writeDraft(atKey, text, atSeq);
    if (result.ok) {
      savedText = text;
      setStatus("saved", null);
      return;
    }
    // 保存失败：内存里的文字一个字都不动，状态如实说失败（绝不说「已保存」）
    setStatus("error", result.error ?? "草稿没有保存成功（原因未知）");
  }

  /** 记下「输入变了」，等防抖到点再写 */
  function armTimer(): void {
    seq += 1;
    const atSeq = seq;
    const atKey = key;
    cancelTimer();
    setStatus("saving", null);
    timer = setTimeout(() => {
      timer = null;
      commit(atSeq, atKey, host.draft);
    }, CHAT_DRAFT_DEBOUNCE_MS);
  }

  /** 把存储里的草稿装回输入框（这次赋值不是用户输入，不该触发保存） */
  function loadInto(text: string): void {
    loadingStored = true;
    try {
      host.draft = text;
    } finally {
      loadingStored = false;
    }
  }

  /**
   * 首次绑定：把当前键上**已经存好**的草稿装回输入框（刷新 / 关闭重开后的恢复）。
   *
   * 为什么单独做这一步：话题没有变化时 bind() 不会被调用（例如整个会话里
   * currentTopicId 一直是 null，草稿存在占位键上），只等 bind 就永远恢复不出来。
   */
  function restore(): void {
    if (!isBlankText(host.draft)) return; // 用户已经在输入了：绝不覆盖
    const stored = readDraft(key);
    if (!stored || !stored.text) return;
    savedText = stored.text;
    seq = stored.seq;
    loadInto(stored.text);
    setStatus("saved", null);
  }

  /** 输入变化（由 watch 调用） */
  function onDraftChanged(): void {
    if (loadingStored) return;
    armTimer();
  }

  /** 立即落盘（不等防抖）：离开编辑器 / 页面隐藏 / 切换话题之前用 */
  function flushNow(): void {
    cancelTimer();
    if (host.draft === savedText) return;
    seq += 1;
    commit(seq, key, host.draft);
  }

  /**
   * 话题变了：先把旧键落盘，再装载新键的草稿。
   *
   * 话题还没确定时（占位键）写的字会跟着用户进入这个话题 —— 既不丢，
   * 也不会被搬到用户后来切换到的**别的**话题。
   */
  function bind(topicId: string | null): void {
    const nextKey = chatDraftKeyFor(topicId);
    if (nextKey === key) return;
    const previousKey = key;
    const previousUnbound = previousKey === chatDraftKeyFor(null);
    const buffer = host.draft;
    flushNow();
    const stored = readDraft(nextKey);
    const carry = previousUnbound
      ? (isBlankText(buffer) ? (readDraft(previousKey)?.text ?? "") : buffer)
      : "";
    /**
     * 这次迁移搬的是不是「已经点过发送、还在等回执」的那一份原文（契约 §11.5）。
     *
     * 是同一份的话**版本号保持不变**，并把在飞的归属改指向新键：受理成功时按身份
     * 找到新位置清理它。若这里给它换一个新版本号，回执就会以为存储里是「用户后来
     * 写的新内容」而不敢清理，已经发出去的文字会重新回到输入框 —— 那正是要修的缺陷。
     */
    const movedSends = !isBlankText(carry)
      ? inFlightSends.filter(
          (item) =>
            item.key === previousKey && normalizeSendText(item.text) === normalizeSendText(carry),
        )
      : [];
    const movedVersion = movedSends.reduce((max, item) => Math.max(max, item.draftSeq), 0);
    key = nextKey;
    keyTopics.set(nextKey, topicId);
    savedText = stored ? stored.text : null;
    // 序号只增不减：迟到的写入回执必须能被判定为「已过期」
    seq = Math.max(seq, stored?.seq ?? 0, movedVersion);
    // 用户刚打的字比存储里的旧草稿新：以用户输入为准，不拿旧稿盖掉它
    const text = !isBlankText(carry) ? carry : (stored ? stored.text : "");
    loadInto(text);
    // 从存储里读回来的草稿：它本来就是保存好的，状态如实说「已保存」
    if (stored && stored.text) setStatus("saved", null);
    if (!isBlankText(carry) && carry !== savedText) {
      if (movedSends.length > 0) {
        // 同一个版本换了话题位置：按原版本写入，并把在飞的归属指向新键
        const result = writeDraft(nextKey, carry, movedVersion);
        if (result.ok) {
          savedText = carry;
          for (const item of movedSends) item.key = nextKey;
        } else {
          setStatus("error", result.error ?? "草稿没有保存成功（原因未知）");
        }
      } else {
        seq += 1;
        commit(seq, nextKey, carry);
      }
    }
    if (previousUnbound) removeDraft(previousKey);
    // 存储不可用要立刻说清，而不是等用户打了字再发现
    const available = draftStorageAvailable();
    if (!available.ok) setStatus("error", available.error ?? "本地存储不可用，草稿无法保存");
  }

  /** 记下「点击发送」那一刻的归属（身份、话题、原文、草稿版本、存储键） */
  function captureAttribution(): SendAttribution {
    return {
      draftId: mintSendIdentity(),
      topicId: host.currentTopicId,
      text: host.draft,
      draftSeq: seq,
      key,
      at: Date.now(),
      recorded: true,
    };
  }

  /** 点击发送时调用：把归属记住，等 send() 来取（请求必须用它） */
  function recordAttribution(): SendAttribution {
    const at = captureAttribution();
    pendingAttribution = at;
    return at;
  }

  /**
   * send() 取用点击时记下的归属。
   *
   * 只有文字确实对得上才认这份记录（对不上说明它不是这次发送的）。
   * 没有记录时用当前话题兜底捕获，**照样保护原文**（见下面的说明）。
   */
  function takeAttribution(message: string): SendAttribution {
    const pending = pendingAttribution;
    pendingAttribution = null;
    if (pending && normalizeSendText(pending.text) === message) return pending;
    /**
     * 兜底归属：调用方没有在点击那一刻记录过（例如会话层被直接调用、或组件版本较旧）。
     *
     * 这时**仍然要保护原文**（契约 §10.1「发送前文字还没到自动保存时间也必须受保护」）：
     * 用当前话题与当前草稿键建一份可用归属，protectForSend 会把原文落到**这个话题自己的键**上，
     * 受理成功后再按版本清理。key 为空（存储不可用）时才退化成「只保住内存」。
     */
    return {
      draftId: mintSendIdentity(),
      topicId: host.currentTopicId,
      text: message,
      draftSeq: seq,
      key,
      at: Date.now(),
      recorded: Boolean(key),
    };
  }

  /**
   * 发送请求在飞：按住「清空输入框 → 删草稿」的防抖写入，并把原文先落盘。
   *
   * 受理成功才清理（settleSend），失败时原文必须原样还在 ——
   * 而且它属于**点击那一刻**的话题，和之后用户切到哪儿无关（契约 §10.1）。
   */
  function protectForSend(at: SendAttribution): void {
    cancelTimer();
    pendingAttribution = null;
    if (!at.recorded || !at.key) {
      // 兜底归属（没有在点击那一刻记录过）：不动存储，只保住状态显示。
      // 仍然登记这次发送：失败时要能说清它现在属于哪个话题（契约 §11.5）。
      inFlightSends.push({ ...at });
      if (at.key === key && isBlankText(host.draft)) setStatus("idle", null);
      return;
    }
    // 输入框已经清空（消息正在飞）：先收起「保存中」，原文本身下面单独落盘
    if (at.key === key && isBlankText(host.draft)) setStatus("idle", null);
    if (isBlankText(at.text)) return;
    const stored = readDraft(at.key);
    if (!stored || stored.seq <= at.draftSeq) {
      const result = writeDraft(at.key, at.text, at.draftSeq);
      if (result.ok) {
        if (at.key === key) savedText = at.text;
      } else {
        // 存不进本机也要如实说：内存里的原文不会丢，但刷新后可能取不回
        setStatus("error", result.error ?? "草稿没有保存成功（原因未知）");
      }
    }
    inFlightSends.push({ ...at });
  }

  /**
   * 这次发送有了结果：受理成功 / 失败。
   *
   * 受理成功只清掉**这一次发送对应的旧版本**（键相同、且存储里的版本没有被更新的草稿取代）；
   * 失败什么都不删 —— 原文留在它自己的键上，回到原话题就能取。
   */
  function settleSend(at: SendAttribution, accepted: boolean): void {
    /**
     * 按**身份**找到这次发送（契约 §11.5），拿它**现在**的位置（迁移会改键）：
     * 不能用点击那一刻的旧键 —— 未绑定话题发送后服务器绑定真实话题时，草稿已经
     * 搬到新键，按旧键找只会找不到，于是已经发出去的文字又留在输入框里。
     * 身份对不上时退回「键 + 版本 + 原文」的兼容匹配。
     */
    const index = inFlightSends.findIndex(
      (item) =>
        (at.draftId && item.draftId === at.draftId) ||
        (item.key === at.key && item.draftSeq === at.draftSeq && item.text === at.text),
    );
    const held = index >= 0 ? inFlightSends[index] : null;
    if (index >= 0) inFlightSends.splice(index, 1);
    if (!accepted) return;
    const targetKey = held?.key ?? at.key;
    const targetSeq = held?.draftSeq ?? at.draftSeq;
    const targetText = held?.text ?? at.text;
    if (!targetKey) return;
    const stored = readDraft(targetKey);
    if (!stored) return;
    /**
     * 存储里是后来写下的新草稿（版本更高）：一个字都不动（契约 §10.2）。
     * 「输入框是不是空的」不能独立证明没有新草稿，所以只看版本，不看输入框。
     */
    if (stored.seq > targetSeq) return;
    removeDraft(targetKey);
    if (targetKey !== key) return; // 发送期间换了话题：当前话题的草稿与状态不受影响
    // 序号往前推一格：还在等防抖、对应这次发送的旧写入不许把草稿又写回来
    if (seq <= targetSeq) seq = targetSeq + 1;
    savedText = "";
    if (timer === null && host.draft === targetText) {
      /**
       * 用户切走又切回来时，输入框会被这条草稿填回来；这次发送已经受理，
       * 原文再留在输入框会被当成「还没发出去」。只在**没有待保存写入**、
       * 且输入框里就是这次发出去的原文时才清掉显示（不当成删除，也不重发）。
       */
      loadInto("");
    }
    if (isBlankText(host.draft)) setStatus("idle", null);
  }

  /** 保存失败后的重试：立刻重写一次，不等防抖 */
  function retry(): void {
    cancelTimer();
    seq += 1;
    commit(seq, key, host.draft);
  }

  return {
    scope,
    get currentKey(): string {
      return key;
    },
    /** 当前键上的草稿版本（点击那一刻记归属、互换取回时都要用它） */
    get currentSeq(): number {
      return seq;
    },
    /**
     * 这次发送**现在**在哪个存储键上（契约 §11.5）：话题迁移会改这里的位置。
     * 找不到（已经被回执处理掉、或从来没登记过）时返回 null。
     */
    locationOf(draftId: string): { key: string; seq: number; text: string } | null {
      const found = inFlightSends.find((item) => item.draftId === draftId);
      return found ? { key: found.key, seq: found.draftSeq, text: found.text } : null;
    },
    /** 某个存储键对应哪个话题（不认识这个键时返回 undefined，调用方自己兜底） */
    topicForKey(target: string): string | null | undefined {
      return keyTopics.get(target);
    },
    bind,
    restore,
    onDraftChanged,
    flushNow,
    recordAttribution,
    takeAttribution,
    protectForSend,
    settleSend,
    retry,
  };
}

type ChatDraftKeeper = ReturnType<typeof createChatDraftKeeper>;

/**
 * 保存器挂在 store 实例上的符号键。
 *
 * 为什么不用 WeakMap 按 store 对象取：Pinia 的 store 是 reactive 代理，而 action 里的
 * `this` 在真实浏览器里并不保证与 `useSessionStore()` 返回的那个代理是**同一个对象**
 * （实测：动作里读实例属性正常，WeakMap.get(this) 却是 undefined）。符号属性走的是
 * 对象本身，代理与原始对象都能取到同一个保存器 —— 单一写者不会因此变成两个。
 */
const CHAT_DRAFT_KEEPER: unique symbol = Symbol("qio.chatDraftKeeper");

function chatDraftKeeperFor(store: object): ChatDraftKeeper | undefined {
  return (store as { [CHAT_DRAFT_KEEPER]?: ChatDraftKeeper })[CHAT_DRAFT_KEEPER];
}

/**
 * 会话 store 的取用入口：对外仍是同一个名字、同一份状态、同一套行为，
 * 只在**第一次取用时**给这个实例装上聊天草稿的持久化（watch 草稿与话题、防抖落盘、离开时落盘）。
 */
export function useSessionStore(): ReturnType<typeof sessionStoreDefinition> {
  const store = sessionStoreDefinition();
  if (!chatDraftKeeperFor(store)) {
    const keeper = createChatDraftKeeper(store as unknown as SessionDraftHost);
    Object.defineProperty(store, CHAT_DRAFT_KEEPER, {
      value: keeper,
      enumerable: false,
      configurable: true,
      writable: true,
    });

    keeper.scope.run(() => {
      watch(() => (store as unknown as SessionDraftHost).draft, () => keeper.onDraftChanged(), {
        flush: "sync",
      });
      watch(
        () => (store as unknown as SessionDraftHost).currentTopicId,
        (topicId: string | null, previous: string | null) => {
          keeper.bind(topicId);
          // 话题从「还没确定」变为真实话题：这次发送的失败原文跟着迁移（契约 §11.5）
          if (previous === null && topicId) store.adoptUnboundFailedSends(topicId);
        },
        { flush: "sync" },
      );
    });
    // 刷新 / 关闭重开：先把本机存好的草稿装回输入框（不发送、不动板面）
    keeper.restore();
    /**
     * 刷新 / 关闭重开：把本机保存的失败原文装回会话状态（契约 §11.4）。
     * 只读存储、只恢复事实：不发送、不重试、不动板面。
     */
    const persistedFails = readFailedSendStateFromStorage();
    if (persistedFails.records.length) {
      store.failedSends = trimFailedSendsPerTopic(persistedFails.records);
      const errors: Record<string, string> = {};
      for (const record of persistedFails.records) {
        if (record.id && record.error) errors[record.id] = record.error;
      }
      store.failedSendErrors = errors;
      const newest = store.failedSends[0] ?? null;
      store.failedSend = newest;
      store.failedSendError = newest?.id ? errors[newest.id] ?? null : null;
    }
    store.failedSendPersistError = persistedFails.error;
    // 打开应用时就检查一次存储可用性：不可用要能说清，而不是等用户打完字才发现
    const available = draftStorageAvailable();
    if (!available.ok) {
      store.draftSaveStatus = "error";
      store.draftSaveError = available.error ?? "本地存储不可用，草稿无法保存";
    }
    if (typeof window !== "undefined") {
      // 正常离开 / 关闭页面：本机存储是同步写，来得及把还没到防抖时间的内容落盘
      window.addEventListener("pagehide", () => keeper.flushNow());
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") keeper.flushNow();
      });
    }
  }
  return store;
}

