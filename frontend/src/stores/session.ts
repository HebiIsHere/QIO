import { defineStore } from "pinia";
import {
  api,
  type DevAbandonResult,
  type DevAuthorizationRow,
  type DevTaskRow,
  type InterruptedTurn,
  type ToolRecordPreview,
} from "../services/api";
import { toAttachmentRef, type AttachmentRef } from "../services/attachments";

/**
 * 历史消息里的附件（契约 §1.6）：后端 payload 已带 id / name / size_bytes / kind / state / error，
 * 用 C 的 `toAttachmentRef` 收敛成冻结形状 —— 历史与输入区不再各写一套解析。
 * 形状不对的整条跳过：宁可没有这一条，也不编造文件名或状态。
 */
function historyAttachmentRefs(raw: unknown): MessageAttachment[] {
  if (!Array.isArray(raw)) return [];
  const out: MessageAttachment[] = [];
  for (const row of raw) {
    if (!row || typeof row !== "object") continue;
    const payload = row as Record<string, unknown>;
    if (!String(payload.id ?? "").trim()) continue;
    out.push(toAttachmentRef(payload));
  }
  return out;
}

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

/**
 * 消息上的附件展示形状（契约 §4.2）：**逐字复用** C 的 `AttachmentRef`。
 *
 * 不再自己声明一份：两份形状一旦漂移，历史附件与输入区附件就会各说一套。
 */
export type MessageAttachment = AttachmentRef;

/** 阶段自身状态（契约 §1.3）：只表达这个阶段，不代表整轮。 */
export type StageStatus = "running" | "done";

/**
 * 阶段内的一次说明。
 *
 * 两个来源共用同一个列表（契约 §1.3 最终版）：
 * * `STAGE.text`：narrativeId 是后端落库的消息 id；
 * * 工具轮的**中间话**（`ASSISTANT{interim:true, stage_id}`）：narrativeId 是这条本地消息 id。
 *   中间话是**该阶段的历次说明之一**，不再是一个并列的过程气泡。
 */
export interface StageNote {
  narrativeId: string;
  text: string;
  kind: NarrativeKind;
  at: string;
}

/** 一轮里的一个过程阶段：顺序、名称、状态、历次说明、关联工具（契约 §1.1/§1.3）。 */
export interface TurnStage {
  stageId: string;
  /** 从 1 开始、单调（后端给；缺失时按到达顺序补） */
  index: number;
  name: string;
  status: StageStatus;
  notes: StageNote[];
  /** 归属只看 stage_id 的 call_id（工具归属不靠消息相邻位置） */
  callIds: string[];
}

/**
 * 一轮结束后**确实可用**的操作（契约 §1.2）。
 *
 * 白名单由后端给的动作名冻结在这里：未知动作一律丢弃 —— 界面上不能出现
 * 一个点了没有反应的按钮。
 */
export type TurnAction = "retry" | "resend" | "continue";
export const TURN_ACTIONS: readonly TurnAction[] = ["retry", "resend", "continue"];

/** 后端 actions 数组 → 去重、过白名单的动作列表（顺序保留）。 */
function normalizeTurnActions(raw: unknown): TurnAction[] {
  if (!Array.isArray(raw)) return [];
  const out: TurnAction[] = [];
  for (const item of raw) {
    if (typeof item !== "string") continue;
    const action = item.trim() as TurnAction;
    if (!TURN_ACTIONS.includes(action)) continue;
    if (!out.includes(action)) out.push(action);
  }
  return out;
}

/**
 * TURN_END 的权威事实（契约 §3 / §1.2）：总耗时与结束原因都只在轮次结束后才存在。
 *
 * 结束原因/可用操作全部来自后端（已脱敏）：
 * * 旧记录没有这些字段 → reason/reasonCode 为 null、actions 为空，
 *   界面**不伪造**原因，也不给一个点不动的按钮；
 * * 单次可恢复的工具错误不等于整轮失败（status 语义不变）。
 */
export interface TurnFacts {
  turnId: string;
  status: string;
  durationMs: number | null;
  queueMs: number | null;
  startedAt: string | null;
  endedAt: string | null;
  /** 一句话人话原因（后端 redact 过）；没有就是 null */
  reason: string | null;
  /** 机器可读原因码（provider_error / tool_failed / budget / no_progress / guard_halt / user_stopped / interrupted / none） */
  reasonCode: string | null;
  /** 谁停的：user / system；没有就是 null */
  stoppedBy: "user" | "system" | null;
  /** 后端明确「当前确实可用」的操作（只渲染这些） */
  actions: TurnAction[];
  /** 失败/停止的完整错误文本：只进默认折叠的「详情」 */
  errorText: string | null;
}

/**
 * 按 turn 记录的过程数据（阶段 / 事实）保留上限。
 *
 * 长会话 + 历史分页会带来很多 turn：不能因为「过程区要能回看」就让这两张表无界增长。
 * 保留最近的一批（正在跑的那一轮永远在最后，不会被裁掉）。
 */
const TURN_PROCESS_LIMIT = 200;

/**
 * 每轮结束事实的**本机留痕**（刷新兜底）。
 *
 * 为什么需要：TURN_END 的 reason / actions 原本只活在内存里 —— 用户看到一轮失败、
 * 刷新页面之后「重试」入口就没了，只能重写整段需求（D 实机 S6 复现）。
 * 这里把**后端真实给过的**事实按 turn_id 留一份（有界、超长文本截断），
 * 只用于本机恢复兜底；后端历史 / RESYNC 一旦给了同一条，以它为准。
 *
 * 三条底线：不猜、不造（没收到过 TURN_END 事实的轮次这里什么都没有）、
 * 读不到/写不进都不影响会话本身。
 */
const TURN_FACTS_STORAGE_KEY = "qio.turnFacts";
const TURN_FACTS_CACHE_LIMIT = 100;
const TURN_FACTS_TEXT_LIMIT = 500;

function loadTurnFactsCache(): Record<string, TurnFacts> {
  try {
    if (typeof localStorage === "undefined") return {};
    const raw = localStorage.getItem(TURN_FACTS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, TurnFacts> = {};
    for (const [key, value] of Object.entries(parsed)) {
      const row = value as TurnFacts | null;
      if (row && typeof row === "object" && typeof row.turnId === "string") out[key] = row;
    }
    return out;
  } catch {
    return {}; // 存储不可用 / 内容坏了：当作没有留痕
  }
}

function persistTurnFactsCache(map: Record<string, TurnFacts>): void {
  try {
    if (typeof localStorage === "undefined") return;
    const keep = Object.keys(map).slice(-TURN_FACTS_CACHE_LIMIT);
    const out: Record<string, TurnFacts> = {};
    for (const key of keep) {
      const facts = map[key];
      if (!facts) continue;
      out[key] = {
        ...facts,
        reason: facts.reason ? facts.reason.slice(0, TURN_FACTS_TEXT_LIMIT) : facts.reason,
        errorText: facts.errorText ? facts.errorText.slice(0, TURN_FACTS_TEXT_LIMIT) : facts.errorText,
      };
    }
    localStorage.setItem(TURN_FACTS_STORAGE_KEY, JSON.stringify(out));
  } catch {
    // 写不进去（隐私模式 / 配额）：留痕只是兜底，不是功能本身
  }
}
function trimTurnMap<T>(map: Record<string, T>): Record<string, T> {
  const keys = Object.keys(map);
  if (keys.length <= TURN_PROCESS_LIMIT) return map;
  const out: Record<string, T> = {};
  for (const key of keys.slice(keys.length - TURN_PROCESS_LIMIT)) out[key] = map[key] as T;
  return out;
}

/** 当前阶段：还在跑的最后一个是「当前」；都结束了就是最后一个。 */
export function currentStageOf(stages: TurnStage[]): TurnStage | null {
  if (!stages.length) return null;
  for (let i = stages.length - 1; i >= 0; i -= 1) {
    if (stages[i].status === "running") return stages[i];
  }
  return stages[stages.length - 1] ?? null;
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

/** 一轮里的条目分别进哪一块（过程区 / 正文区）—— 组件与测试共用这一份规则。 */
export interface TurnItemsView {
  /** 用户消息（轮首） */
  user: StreamMessage[];
  /** 过程区：中间话 + 工具卡 + legacy 叙事行（按到达顺序） */
  process: StreamMessage[];
  /** 正文区：正式回答（interim 不为 true 的助手消息） */
  answers: StreamMessage[];
  /** 其余卡片（独立任务 / 工具创建 / system）：它们有自己的生命周期，不进过程区 */
  other: StreamMessage[];
}

/**
 * 把一轮的消息分成「过程」与「正文」。
 *
 * * 中间话（interim）**不再单独成气泡**，统一进过程区（契约 §1.5）；
 * * 工具卡进过程区（同一内容只出现一次，不再散落在一轮里）；
 * * 独立任务 / 工具创建卡留在过程区外：它们的生命周期比一轮的过程说明长，
 *   被过程区收起会看不见（它们各自已有明确的卡与状态）。
 */
export function splitTurnItems(items: StreamMessage[]): TurnItemsView {
  const view: TurnItemsView = { user: [], process: [], answers: [], other: [] };
  for (const m of items) {
    if (m.role === "user") view.user.push(m);
    else if (m.role === "assistant") (m.interim ? view.process : view.answers).push(m);
    else if (m.role === "tool" || m.role === "narrative") view.process.push(m);
    else view.other.push(m);
  }
  return view;
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

/** 历史行 raw 里的阶段元数据（契约 §1.4：阶段沿用 messages，raw.stage 记录顺序与状态）。 */
interface RawStageMeta {
  stageId: string;
  index: number | null;
  name: string;
  status: string;
}

/** 历史行的 `raw`（JSON 字符串）→ 叙事元数据；解析失败一律当作"没有"。 */
function parseNarrativeRaw(raw?: string | null): {
  kind?: NarrativeKind;
  calls?: NarrativeCallRecord[];
  stage?: RawStageMeta;
} {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const meta = (parsed.narrative ?? {}) as Record<string, unknown>;
    const calls = narrativeCallRecords(parsed.calls);
    const rawStage = (parsed.stage ?? null) as Record<string, unknown> | null;
    const stageId = rawStage ? String(rawStage.stage_id ?? "").trim() : "";
    const index = rawStage?.index;
    return {
      ...(meta.kind === undefined ? {} : { kind: normalizeNarrativeKind(meta.kind) }),
      ...(calls.length ? { calls } : {}),
      ...(stageId
        ? {
            stage: {
              stageId,
              index: typeof index === "number" && Number.isFinite(index) ? Math.trunc(index) : null,
              name: String(rawStage?.name ?? ""),
              status: String(rawStage?.status ?? ""),
            },
          }
        : {}),
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

/**
 * 从发送失败里取出「附件没附上」的结构化信息（契约 §1.2）。
 *
 * 后端在 rejected 非空时返回结构化失败（detail 对象里带 rejected 数组）。
 * 拿不到这个形状就返回 null —— 普通失败绝不伪造成附件原因。
 */
function attachmentRejectionFrom(err: unknown): {
  message: string;
  rejected: { id: string; reason: string }[];
} | null {
  const body = (err as { body?: unknown } | null)?.body;
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  const source =
    detail && typeof detail === "object"
      ? (detail as Record<string, unknown>)
      : (body as Record<string, unknown>);
  const raw = source.rejected;
  if (!Array.isArray(raw) || !raw.length) return null;
  const rejected: { id: string; reason: string }[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const id = String(row.id ?? "").trim();
    if (!id) continue;
    rejected.push({ id, reason: String(row.reason ?? "没有附上") });
  }
  if (!rejected.length) return null;
  const text =
    typeof source.message === "string" && source.message.trim()
      ? source.message.trim()
      : `有 ${rejected.length} 个附件没有附上`;
  return { message: text, rejected };
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
  /** 工具归属的阶段（契约 §1.1：只看 stage_id；缺省 = 归入「整轮」） */
  stageId?: string | null;
  /** 这条流式正文属于哪一次模型调用（delta_id）：同一轮的多个 delta 绝不互相覆盖 */
  assistantDeltaId?: string;
  /** 已收到的最大 seq：(delta_id, seq) 单调，用它丢弃重复 / 迟到的事件 */
  assistantSeq?: number;
  /**
   * 这条消息是否**已经被后续增量更新过**（同一 delta_id 又来了一条累计快照）。
   *
   * 用途：打字机只用于「一次整段到达」的文本；真正的增量流式里节奏由增量本身给出，
   * DOM 必须立刻跟上 —— 否则「边生成边显示」会退化成「整段到达后还要再等一拍」。
   */
  assistantGrew?: boolean;
  /** 用户消息携带的附件 id（发送时登记的事实） */
  attachmentIds?: string[];
  /** 附件展示元数据（与 C 的 AttachmentRef 同形；拿不到就不编造名称与状态） */
  attachments?: MessageAttachment[];
  /**
   * 受理回执与界面不一致时的**事实**说明（例如「有 1 个附件没有附上：…」）。
   * 只在后端明确说「没绑上」时写：绝不出现「界面有附件、模型实际没有」。
   */
  attachmentNotice?: string;
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

export const useSessionStore = defineStore("session", {
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
     * 本机发起的发送序号。只有本机发送才允许把消息流强制拉回底部；
     * 后台/排队任务开始时用户可能正在往上读，不能被拽走。
     */
    localSendSeq: 0,
    /**
     * 最近一次发送因附件被拒（结构化失败）：保留文本与可恢复信息，
     * 由输入区显示逐条原因并提供「移除这些附件后发送」。
     * 下一次发送开始时清空（不把上一次的原因挂在新的尝试上）。
     */
    lastSendRejection: null as {
      message: string;
      rejected: { id: string; reason: string }[];
    } | null,
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
    /** 按 turn_id 组织的阶段（契约 §1.3：系统生成，前端只消费，不能自己造） */
    stagesByTurn: {} as Record<string, TurnStage[]>,
    /**
     * 每轮 TURN_END 的权威事实（总耗时 / 状态 / 结束原因）：折叠态不展开也要显示。
     * 初值来自本机留痕（刷新后失败轮的「重试」入口不能消失）。
     */
    turnFacts: loadTurnFactsCache(),
    /** 问题 7 的可用操作失败反馈：按 turn_id 留在那一轮上（不静默、可重试） */
    turnActionFeedback: {} as Record<string, string>,
    /** 正在提交的操作（`turnId:action`）：防重复提交 */
    turnActionBusy: null as string | null,
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
    /** 某一轮的阶段（按 index 顺序；没有就是空数组 = legacy 平铺） */
    stagesFor: (state) => (turnId?: string | null): TurnStage[] =>
      turnId ? (state.stagesByTurn[turnId] ?? []) : [],
    /** 某一轮 TURN_END 的权威事实（没有 = 还在跑 / 旧记录） */
    factsFor: (state) => (turnId?: string | null): TurnFacts | null =>
      turnId ? (state.turnFacts[turnId] ?? null) : null,
    /**
     * 某一轮的**用户消息**：问题 7 的「重试」要重发的就是它。
     * 旧记录没有 turn_id / 已经不在消息流里 → null（界面据此不显示重试按钮）。
     */
    userMessageFor: (state) => (turnId?: string | null): StreamMessage | null =>
      turnId
        ? (state.messages.find((m) => m.role === "user" && m.turnId === turnId) ?? null)
        : null,
  },
  actions: {
    _nextId() {
      this._msgSeq += 1;
      return `local_${Date.now()}_${this._msgSeq}`;
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
    pushUser(text: string, attachmentIds: string[] = [], attachments: MessageAttachment[] = []) {
      const ids = attachmentIds.map((x) => String(x)).filter(Boolean);
      const refs = attachments.filter((a) => a && a.id && (!ids.length || ids.includes(a.id)));
      this.pushMessage({
        role: "user",
        content: text,
        contentType: "text",
        ...(ids.length ? { attachmentIds: ids } : {}),
        ...(refs.length ? { attachments: refs } : {}),
      });
    },
    /**
     * 一条助手正文到达（ASSISTANT 事件）。
     *
     * 契约 §1.1 / §2.1（Lead 追加契约 task-6）：
     * * `content` 是**累计全文**（同一个 delta_id 每次替换，不做增量拼接）；
     * * `delta_id` 标识一次模型调用，`seq` 单调，(delta_id, seq) 用来丢弃重复 / 迟到事件；
     * * 同一轮里**不同 delta 绝不互相覆盖**：换 delta 之前先把上一条落定，
     *   否则第二次 interim 会把第一段过程说明吃掉；
     * * 唯一允许的角色改判是 **interim → 正式回答**（提升）：该次调用结束且没有工具调用时，
     *   后端对同一个 delta_id 再发一条 `{interim:false, content:累计全文}`，这段文字
     *   原样升到正文区（同一条消息、不重打、不重复）；**正式回答 → 过程区永远不允许**；
     * * 显式 `stage_id=null` 表示「未归属」，不按「到达时的当前阶段」猜；缺字段才是旧后端。
     */
    pushAssistant(
      text: string,
      interim = false,
      streaming = false,
      meta: {
        deltaId?: string | null;
        seq?: number | null;
        /** 契约最终版：interim 时后端给同批 STAGE 的 stage_id；正式回答为 null */
        stageId?: string | null;
        /** 这一批的工具调用 id（系统事实：用于把工具补到该阶段） */
        callIds?: string[];
      } = {},
    ) {
      const deltaId = String(meta.deltaId ?? "");
      const seq =
        typeof meta.seq === "number" && Number.isFinite(meta.seq) ? Math.trunc(meta.seq) : null;
      /**
       * 中间话归属：显式给的 stage_id 优先；**只有缺字段**（旧后端）才回落到
       * 「到达时的当前阶段」。归属只看 stage_id，不靠消息相邻位置。
       *
       * 显式 `null` 是「这段文字暂时未归属」：后端随后会用同一 delta_id 补发带
       * stage_id 的累计快照，那时就地归位（见下面 existing 分支）。若在这里按
       * 「当前阶段」猜归属，同一段字会在两个阶段各留一条说明（Lead 追加契约 task-6）。
       */
      const explicitStage = meta.stageId === null ? null : String(meta.stageId ?? "").trim();
      const stageId = interim
        ? explicitStage === null
          ? null
          : explicitStage || this._currentStageIdFor(this.activeTurnId)
        : null;
      /**
       * 这一批的工具调用 id：系统事实，登记到阶段上（TOOL_START 没给 stage_id 时补归属）。
       * 先 ensureStage：ASSISTANT 的 stage_id 可能早于 STAGE 到达，占位要在这里就建好，
       * 否则 call_ids 会因为「阶段还不存在」而丢掉（STAGE 到达后就地补齐，不新建阶段）。
       */
      if (stageId) {
        const turnId = this.activeTurnId ?? "";
        if (interim) this.ensureStage(stageId, turnId);
        for (const callId of meta.callIds ?? []) {
          this.attachCallToStage(turnId, stageId, String(callId));
        }
      }

      // 1) 同一个 delta_id：累计快照**就地更新**，绝不新建第二条
      const existing = deltaId
        ? this.messages.find((m) => m.role === "assistant" && m.assistantDeltaId === deltaId)
        : undefined;
      if (existing) {
        /**
         * 唯一允许的角色改判：**interim → 正式回答**（提升）。
         *
         * 该次调用结束且没有工具调用时，后端对同一个 delta_id 再发一条
         * `{interim:false, streaming:false, content:累计全文}`：这段文字原样升到正文区，
         * 同一条消息、不重打、不重复。反向（正式回答 → 过程区）**永远不允许**。
         */
        const promoting = interim === false && existing.interim === true;
        // 去重：seq 不大于已收最大值的一律丢弃（重连重放 / 重复事件不回退）。
        // 提升是角色变化而不是重复：后端补发的累计快照可能带同一个 seq，必须放行。
        if (
          !promoting &&
          seq !== null &&
          existing.assistantSeq !== undefined &&
          seq <= existing.assistantSeq
        ) {
          return;
        }
        this._touchAssistantDelta(existing, streaming);
        existing.content = text;
        if (seq !== null) {
          existing.assistantSeq = Math.max(seq, existing.assistantSeq ?? seq);
        }
        if (promoting) {
          existing.interim = false;
          // 正式回答永远不挂阶段，而且这段文字不能再作为阶段说明出现（否则出现两次）
          const before = String(existing.stageId ?? "");
          delete existing.stageId;
          if (before) this._detachInterimNote(existing, before);
          return;
        }
        // 提升之后不再接受任何往过程区的改判（interim === false 是明确的正式回答）
        if (interim && existing.interim !== false) {
          existing.interim = true;
          const before = String(existing.stageId ?? "");
          if (stageId) {
            if (before && before !== stageId) this._detachInterimNote(existing, before);
            existing.stageId = stageId;
            // 说明挂到该阶段（累计快照：同一条说明原地更新，不新增）
            this._attachInterimNote(existing, stageId);
          } else if (explicitStage === null && before) {
            // 显式未归属：先摘掉旧阶段的说明，等带 stage_id 的快照归位
            delete existing.stageId;
            this._detachInterimNote(existing, before);
          }
        }
        return;
      }

      // 2) 没有 delta_id（旧后端整段推送）：沿用「最后一条流式消息就地更新」的既有行为
      const last = this.messages[this.messages.length - 1];
      if (!deltaId && streaming && last && last.role === "assistant" && last.streaming) {
        this._touchAssistantDelta(last, true);
        last.content = text;
        if (interim) {
          last.interim = true;
          if (stageId) {
            last.stageId = stageId;
            this._attachInterimNote(last, stageId);
          }
        }
        return;
      }

      // 3) 新的一次模型调用：先落定上一条，再另起一条 —— 两段过程说明各自保留
      if (deltaId) this.finalizeAssistant();
      this.pushMessage({
        role: "assistant",
        content: text,
        contentType: "text",
        // interim 显式写成布尔：缺省的 undefined 无法区分「正式回答」与「还没定角色」
        interim: interim === true,
        ...(streaming ? { streaming: true } : {}),
        ...(deltaId ? { assistantDeltaId: deltaId } : {}),
        ...(seq !== null ? { assistantSeq: seq } : {}),
        ...(stageId ? { stageId } : {}),
      });
      if (interim && stageId) {
        const added = this.messages[this.messages.length - 1];
        if (added) this._attachInterimNote(added, stageId);
      }
    },
    /**
     * 把一条中间话挂到它的阶段上（作为该阶段的历次说明之一）。
     *
     * * 阶段还没建立（ASSISTANT 先于 STAGE 到达）→ 先建**占位阶段**，
     *   STAGE 到达后由 upsertStage 就地把名字 / 顺序补齐（同一 stage_id，不新建）；
     * * 同一个 delta 的累计快照**原地更新同一条说明**（按消息 id 认），
     *   绝不每来一个增量就多出一条说明；
     * * 空文本不产生说明（不占位置）。
     */
    _attachInterimNote(message: StreamMessage, stageId: string) {
      const turnId = message.turnId ?? this.activeTurnId ?? "";
      if (!turnId) return;
      this.ensureStage(stageId, turnId);
      const list = this.stagesByTurn[turnId];
      const stage = list?.find((s) => s.stageId === stageId);
      if (!stage) return;
      const text = String(message.content ?? "").trim();
      if (!text) return;
      const existing = stage.notes.find((n) => n.narrativeId === message.id);
      if (existing) {
        existing.text = text;
        return;
      }
      stage.notes.push({
        narrativeId: message.id,
        text,
        kind: "progress",
        at: message.createdAt,
      });
      // 重新赋值一次：让嵌套改动也走一遍响应式更新（与 upsertStage 保持一致）
      this.stagesByTurn = { ...this.stagesByTurn, [turnId]: [...(list ?? [])] };
    },
    /**
     * 把一条中间话从它的阶段说明里摘掉（按消息 id 认）。
     *
     * 用到它的两种情形都属于**归属变化**，不是「丢掉内容」：
     * * interim → 正式回答提升：这段文字改由正文区承担，过程区不能再留一份；
     * * 同一 delta 的累计快照换了 stage_id（或变成显式未归属）：旧阶段不该留旧文字。
     */
    _detachInterimNote(message: StreamMessage, stageId: string) {
      const turnId = message.turnId ?? this.activeTurnId ?? "";
      const list = this.stagesByTurn[turnId];
      const stage = list?.find((s) => s.stageId === stageId);
      if (!stage) return;
      const before = stage.notes.length;
      stage.notes = stage.notes.filter((n) => n.narrativeId !== message.id);
      if (stage.notes.length === before) return;
      this.stagesByTurn = { ...this.stagesByTurn, [turnId]: [...(list ?? [])] };
    },
    /**
     * 确保某个 stage_id 的阶段存在（占位）。
     *
     * ASSISTANT 的 stage_id 可能先于 STAGE 到达：先建一个没有名字的占位，
     * STAGE 到达后 upsertStage 找到同一 stage_id 就地补齐 —— 不新建第二个阶段。
     */
    ensureStage(stageId: string, turnId: string) {
      const id = String(stageId ?? "").trim();
      const turn = String(turnId ?? "");
      if (!id || !turn) return;
      const list = this.stagesByTurn[turn] ?? [];
      if (list.some((s) => s.stageId === id)) return;
      const next = [
        ...list,
        {
          stageId: id,
          index: list.length + 1,
          name: "",
          status: "running" as const,
          notes: [],
          callIds: [],
        },
      ];
      this.stagesByTurn = trimTurnMap({ ...this.stagesByTurn, [turn]: next });
    },
    /**
     * 记一次增量到达的真实节奏（相邻两次增量的间隔），并同步 streaming 标记。
     * 明确说「这条路径不支持实时生成」（streaming=false）时不假装在逐字输出。
     */
    _touchAssistantDelta(message: StreamMessage, streaming: boolean) {
      const now = Date.now();
      if (message.deltaAt) {
        message.paceMs = Math.min(400, Math.max(40, now - message.deltaAt));
      }
      message.deltaAt = now;
      // 这条文字已经**被增量更新过**：DOM 不再对它做逐字点亮（见 MessageItem 的 reveal）
      message.assistantGrew = true;
      if (streaming) message.streaming = true;
      else delete message.streaming;
    },
    /**
     * 落定**最近一条还在流式输出**的助手消息（停止逐字；interim 标记保留 ——
     * 中间话不是最终答案）。
     *
     * 为什么不能只看最后一条：工具卡会插在正文之后（正文 → 工具 → 下一段正文），
     * 只看最后一条就会漏掉那条还在 streaming 的正文 —— 它会一直显示成「正在生成」，
     * 而且换 delta 时也不会被落定（task-2 的覆盖缺陷就是从这里开始的）。
     */
    finalizeAssistant() {
      for (let i = this.messages.length - 1; i >= 0; i -= 1) {
        const message = this.messages[i];
        if (!message || message.role !== "assistant") continue;
        if (message.streaming) delete message.streaming;
        return;
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
    /**
     * 没有最终回答（失败/取消）时，别把**中间话**留在「已落定的最终回答」位置。
     *
     * 但已经以正式回答身份发布出去的文字**永不移动**（契约 §1.1）：回答调用是
     * `interim:false` 的流式正文，断流时用户已经看到了它 —— 把它标回过程区，
     * 等于把用户看到的回答撤走并换个位置重放。
     */
    markLastAssistantInterim() {
      const last = this.messages[this.messages.length - 1];
      if (!last || last.role !== "assistant" || last.streaming) return;
      if (last.interim === false) return; // 已发布的正式回答：不动
      last.interim = true;
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

    // -- 过程阶段（STAGE 事件，契约 §1.3） --------------------------------

    /**
     * 应用一个 STAGE 事件：阶段集合、顺序、阶段内说明、阶段状态。
     *
     * 规则：
     * * 阶段由**系统**生成 —— 前端只消费，绝不根据模型文案自己造阶段；
     * * 没有 stage_id 的事件不产生阶段（旧 NARRATIVE 走 legacy 平铺渲染，不伪造阶段）；
     * * 同一 stage_id 原地推进；`done` 是终态（后续 update 不会把它改回 running）；
     * * 说明按 narrative_id 去重（断线重连会重放同一事件）。
     */
    upsertStage(payload: {
      stage_id?: string;
      turn_id?: string | null;
      index?: number | null;
      status?: string;
      name?: string;
      text?: string;
      kind?: string;
      op?: string;
      narrative_id?: string | null;
      call_ids?: string[];
      created_at?: string | null;
    }) {
      const stageId = String(payload.stage_id ?? "").trim();
      if (!stageId) return;
      const turnId = payload.turn_id ? String(payload.turn_id) : (this.activeTurnId ?? "");
      const list = [...(this.stagesByTurn[turnId] ?? [])];
      let stage = list.find((s) => s.stageId === stageId);
      if (!stage) {
        stage = {
          stageId,
          index:
            typeof payload.index === "number" && Number.isFinite(payload.index) && payload.index > 0
              ? Math.trunc(payload.index)
              : list.length + 1,
          name: "",
          status: "running",
          notes: [],
          callIds: [],
        };
        list.push(stage);
      }
      // 顺序以事件为准（占位阶段是先建的，index 要按 STAGE 补齐）
      if (
        typeof payload.index === "number" &&
        Number.isFinite(payload.index) &&
        payload.index > 0
      ) {
        stage.index = Math.trunc(payload.index);
      }
      const name = String(payload.name ?? "").trim();
      if (name) stage.name = name;
      // 状态单调：done 之后不再回到 running（阶段文案不能重开一整轮）
      if (payload.status === "done") stage.status = "done";
      else if (payload.status === "running" && stage.status !== "done") stage.status = "running";

      const text = String(payload.text ?? "").trim();
      if (text) {
        const noteId = String(payload.narrative_id ?? "");
        const duplicate = stage.notes.some((n) =>
          noteId ? n.narrativeId === noteId : n.text === text,
        );
        if (!duplicate) {
          stage.notes.push({
            narrativeId: noteId,
            text,
            kind: normalizeNarrativeKind(payload.kind),
            at: String(payload.created_at ?? new Date().toISOString()),
          });
        }
      }
      const callIds = Array.isArray(payload.call_ids) ? payload.call_ids.map(String) : [];
      for (const callId of callIds) {
        if (callId && !stage.callIds.includes(callId)) stage.callIds.push(callId);
      }
      list.sort((a, b) => a.index - b.index || (a.stageId < b.stageId ? -1 : 1));
      this.stagesByTurn = trimTurnMap({ ...this.stagesByTurn, [turnId]: list });
    },

    /** 当前阶段 id（没有阶段时 null）：中间话归属用它。 */
    _currentStageIdFor(turnId?: string | null): string | null {
      const id = turnId ? String(turnId) : (this.activeTurnId ?? "");
      return currentStageOf(this.stagesByTurn[id] ?? [])?.stageId ?? null;
    },

    /** 工具归属（契约 §1.1：只看 stage_id，不靠消息相邻位置）。 */
    attachCallToStage(turnId: string | null | undefined, stageId: string | null | undefined, callId: string) {
      if (!stageId || !callId) return;
      const id = turnId ? String(turnId) : (this.activeTurnId ?? "");
      const list = this.stagesByTurn[id];
      const stage = list?.find((s) => s.stageId === stageId);
      if (stage && !stage.callIds.includes(callId)) stage.callIds.push(callId);
      /**
       * 批内工具归属：ASSISTANT 的 call_ids 是**系统事实**（这一批调用了哪些工具），
       * 用它把还没归属的工具卡补到该阶段 —— 这不是「靠消息相邻位置猜」，
       * TOOL_START 自己带了 stage_id 时不会被改动。
       */
      const tool = this.messages.find((m) => m.role === "tool" && m.callId === callId && !m.stageId);
      if (tool) tool.stageId = stageId;
    },

    /**
     * 一轮结束：把还挂着的阶段收口。
     *
     * 阶段状态不代表整轮（阶段文案不能结束整轮），但整轮已经结束了，
     * 就不该再有阶段显示「运行中」——那是过期的界面状态。
     */
    endRunningStages(turnId?: string | null) {
      const id = turnId ? String(turnId) : (this.activeTurnId ?? "");
      const list = this.stagesByTurn[id];
      if (!list?.some((s) => s.status === "running")) return;
      this.stagesByTurn = {
        ...this.stagesByTurn,
        [id]: list.map((s) => (s.status === "running" ? { ...s, status: "done" as const } : s)),
      };
    },

    /**
     * TURN_END 的权威事实（契约 §3 / §1.2）：耗时与结束原因只在这里落地，
     * 缺失就是 null（不是 0），**旧记录不伪造原因**。
     */
    recordTurnFacts(turnId: string, d: Record<string, unknown>) {
      if (!turnId) return;
      const num = (value: unknown): number | null =>
        typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
      const text = (value: unknown): string | null => {
        const s = typeof value === "string" ? value.trim() : "";
        return s ? s : null;
      };
      const nextFacts: TurnFacts = {
          turnId,
          status: String(d.status ?? "completed"),
          durationMs: num(d.duration_ms),
          queueMs: num(d.queue_ms),
          startedAt: typeof d.started_at === "string" ? d.started_at : null,
          endedAt: typeof d.ended_at === "string" ? d.ended_at : null,
          // 结束原因：只写后端真实给过的字段；旧记录没有 → null，界面不编造
          reason: text(d.reason),
          reasonCode: text(d.reason_code),
          stoppedBy: d.stopped_by === "user" || d.stopped_by === "system" ? d.stopped_by : null,
        // 只列后端说「当前确实可用」的操作（未知 / 重复 / 非字符串丢弃）
        actions: normalizeTurnActions(d.actions),
        errorText: text(d.error ?? d.message),
      };
      this.turnFacts = trimTurnMap({ ...this.turnFacts, [turnId]: nextFacts });
      // 本机留痕：刷新之后失败轮的「重试」入口不能消失（后端历史给了就以它为准）
      persistTurnFactsCache(this.turnFacts);
    },
    /**
     * 历史分页 / RESYNC 快照里的每轮结束事实（契约 §1.2）。
     *
     * 形状不对的整条跳过：宁可没有原因，也不伪造一个原因。
     */
    applyTurnFactsSnapshot(rows: unknown) {
      if (!Array.isArray(rows)) return;
      for (const row of rows) {
        if (!row || typeof row !== "object") continue;
        const r = row as Record<string, unknown>;
        const turnId = typeof r.turn_id === "string" ? r.turn_id.trim() : "";
        if (!turnId) continue;
        this.recordTurnFacts(turnId, r);
      }
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
      stageId?: string | null,
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
        if (stageId) existing.stageId = stageId;
        this.attachCallToStage(turnId ?? this.activeTurnId, stageId ?? existing.stageId, key);
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
        ...(stageId ? { stageId } : {}),
      });
      this.attachCallToStage(turnId ?? this.activeTurnId, stageId, key);
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
      stageId?: string | null,
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
        if (stageId) target.stageId = stageId;
        this.attachCallToStage(null, stageId ?? target.stageId, key);
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
        ...(stageId ? { stageId } : {}),
      });
      this.attachCallToStage(null, stageId, key);
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
      // 整轮结束 = 阶段不可能还在跑（阶段状态本身不代表整轮，这里只是收口）
      this.endRunningStages(this.activeTurnId);
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
        /**
         * 每轮结束事实（契约 §1.2）：历史分页同样要带回 reason / actions ——
         * 刷新后失败的那一轮仍然说得出为什么。旧记录没有这个字段 → 什么也不写。
         */
        this.applyTurnFactsSnapshot((ctx as unknown as { turn_facts?: unknown }).turn_facts);
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
        turn_id?: string | null;
        raw?: string;
      }[],
      records: ToolRecordPreview[],
    ): StreamMessage[] {
      // 先重放阶段（契约 §1.4）：工具归属要按 stage_id 找得到阶段，再建工具卡
      this._replayHistoryStages(messages);
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
    /**
     * 历史重放：按时间顺序读 `raw.stage`，恢复阶段顺序 / 历次说明 / 关联工具（契约 §1.4）。
     *
     * 没有 `raw.stage` 的旧数据**不伪造阶段**（继续走 legacy 平铺）；
     * 没有 turn_id 的行无法归属到某一轮，同样跳过（不把不同轮混进一个阶段）。
     */
    _replayHistoryStages(rows: { id: string; content: string; content_type: string; created_at: string; turn_id?: string | null; raw?: string }[]) {
      const ordered = [...rows].sort((a, b) =>
        a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0,
      );
      for (const row of ordered) {
        if (row.content_type !== "narrative" || !row.raw) continue;
        const turnId = row.turn_id ? String(row.turn_id) : "";
        if (!turnId) continue;
        const meta = parseNarrativeRaw(row.raw);
        const stage = meta.stage;
        if (!stage) continue;
        this.upsertStage({
          stage_id: stage.stageId,
          turn_id: turnId,
          index: stage.index,
          status: stage.status,
          name: stage.name,
          text: row.content,
          kind: meta.kind,
          narrative_id: row.id,
          call_ids: (meta.calls ?? []).map((c) => c.callId).filter(Boolean),
          created_at: row.created_at,
        });
      }
    },

    /** 某个 call_id 属于哪一阶段（历史工具卡按它归属；找不到就归「整轮」）。 */
    _stageIdForCall(turnId: string | null | undefined, callId: string | null | undefined): string | null {
      if (!turnId || !callId) return null;
      const stage = (this.stagesByTurn[String(turnId)] ?? []).find((s) => s.callIds.includes(String(callId)));
      return stage?.stageId ?? null;
    },

    /** 一条工具调用记录 → 消息流里的工具卡（预览态；展开时才取全文） */
    _historyToolRecord(r: ToolRecordPreview): StreamMessage {
      const stageId = this._stageIdForCall(r.turn_id, r.call_id);
      return {
        id: `toolrec_${r.id}`,
        role: "tool",
        content: r.preview,
        contentType: "text",
        createdAt: r.created_at,
        topicName: this.topicName,
        fresh: false,
        ...(r.turn_id ? { turnId: r.turn_id } : {}),
        ...(stageId ? { stageId } : {}),
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
      turn_id?: string | null;
      raw?: string;
      /** 附件（契约 §1.6）：历史消息同样要能打开副本 / 重新定位 */
      attachments?: unknown;
    }): StreamMessage {
      const attachments = historyAttachmentRefs(m.attachments);
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
          ...(m.turn_id ? { turnId: String(m.turn_id) } : {}),
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
        ...(m.turn_id ? { turnId: String(m.turn_id) } : {}),
        ...(m.role === "tool"
          ? { toolName: "tool", toolOk: true, toolStatus: "success" as const, toolError: null }
          : {}),
        ...(m.role === "assistant" && parseVerifiedRaw(m.raw)
          ? { verified: parseVerifiedRaw(m.raw) as VerifiedFact }
          : {}),
        // 附件行：名称/大小/保存方式/可用性（打开与重新定位入口在 MessageItem）
        ...(attachments.length ? { attachments } : {}),
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
        // 更早的一页同样带回每轮结束事实（翻到的失败轮也要能「重试」）
        this.applyTurnFactsSnapshot((page as unknown as { turn_facts?: unknown }).turn_facts);
        const known = new Set(this.messages.map((m) => m.id));
        const knownRecords = new Set(
          this.messages.map((m) => m.toolRecordId).filter(Boolean) as string[],
        );
        // 更早的一页同样按 raw.stage 重放阶段（顺序 / 说明 / 关联工具）
        this._replayHistoryStages(page.messages);
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
    /** 清掉某一轮的操作反馈（新的尝试开始时旧错误不再显示） */
    clearTurnActionFeedback(turnId: string) {
      if (!turnId || !this.turnActionFeedback[turnId]) return;
      const next = { ...this.turnActionFeedback };
      delete next[turnId];
      this.turnActionFeedback = next;
    },
    /** 记一次可用操作的失败：留在那一轮上，用户能看见，也能再点一次 */
    setTurnActionFeedback(turnId: string, message: string) {
      if (!turnId) return;
      this.turnActionFeedback = { ...this.turnActionFeedback, [turnId]: message };
    },
    /**
     * 问题 7 的「重试」：用现有发送接口重发**这一轮的用户消息**（新开一轮）。
     *
     * 找不到这一轮的用户消息（旧记录没有 turn_id、或它已经不在消息流里）时
     * **不发请求**，如实反馈「找不到」—— 过程区也不会显示这个按钮。
     */
    async retryTurn(turnId: string): Promise<boolean> {
      if (!turnId || this.turnActionBusy === `${turnId}:retry`) return false;
      const source = this.userMessageFor(turnId);
      const text = (source?.content ?? "").trim();
      if (!text) {
        this.setTurnActionFeedback(turnId, "找不到这一轮的用户消息，无法重试");
        return false;
      }
      this.turnActionBusy = `${turnId}:retry`;
      this.clearTurnActionFeedback(turnId);
      try {
        /**
         * 关键：把「这是哪一轮的重试」告诉后端。
         * 原轮的附件已经绑在那一轮上，没有这个参数后端只能拒绝（旧实现正是漏了它，
         * 于是重试轮 0 附件、界面却还显示着标签）。
         */
        const ok = await this.send(text, source?.attachmentIds, source?.attachments, {
          retryOfTurnId: turnId,
        });
        if (!ok) {
          const rejection = this.lastSendRejection;
          this.setTurnActionFeedback(
            turnId,
            rejection
              ? `重试没有发出去：${rejection.message}（可以移除这些附件后再试）`
              : `重试没有发出去：${this.lastError ?? "请求未被受理"}（可以再试一次）`,
          );
        }
        return ok;
      } finally {
        this.turnActionBusy = null;
      }
    },
    /**
     * 问题 7 的「重新发送」：走既有的 `POST /api/turns/{id}/resend`（后端一次性 claim）。
     *
     * 409（已经有结局 / 已被抢占）如实说明；失败保留可重试，绝不静默。
     */
    async resendTurn(turnId: string): Promise<boolean> {
      if (!turnId || this.turnActionBusy === `${turnId}:resend`) return false;
      this.turnActionBusy = `${turnId}:resend`;
      this.clearTurnActionFeedback(turnId);
      try {
        await api.resendInterruptedTurn(turnId);
        return true;
      } catch (e) {
        this.setTurnActionFeedback(turnId, `重新发送没有成功：${(e as Error).message}（可以重试）`);
        return false;
      } finally {
        this.turnActionBusy = null;
      }
    },
    /**
     * 发送一轮消息。turnRunning 时后端会排队（TURN_QUEUE 事件回执），
     * 因此仍然允许提交：本地先以「等待中」状态呈现，不阻塞用户写下一条。
     */
    async send(
      text: string,
      attachmentIds?: string[],
      attachments?: MessageAttachment[],
      options: {
        /** 只在重试/重发某一轮时给：后端据此允许克隆复用那一轮的附件 */
        retryOfTurnId?: string | null;
        /** 准备期间中止这次请求（输入区的「正在准备附件…」用它）：真 abort 才会让后端 abandon */
        signal?: AbortSignal;
      } = {},
    ): Promise<boolean> {
      const message = text.trim();
      if (!message) return false;
      const ids = (attachmentIds ?? []).map((x) => String(x)).filter(Boolean);
      // 新的尝试开始：上一次的附件拒绝原因不再挂到这一次上
      this.lastSendRejection = null;
      const queued = this.turnRunning;
      this.pushUser(message, ids, attachments ?? []);
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
        /**
         * 附件显式绑定（契约 §1.4）：**无条件**带第三个参数 —— 空数组也是
         * 「这条消息没有附件」的显式语义。不传参数 = 缺字段 = 旧客户端，
         * 后端会走兜底把该话题下遗留的未绑定附件绑上（审计问题 3）。
         */
        // 非重试路径保持既有三参形状；只有重试/重发才带第四个参数（retry_of_turn_id）
        // 调用形状按需最小化：没有 signal 时保持既有参数个数（3 参 / 重试 4 参），
        // 免得把「准备期中止」的管道塞进所有调用点的既有契约里。
        let res;
        if (options.retryOfTurnId) {
          res = options.signal
            ? await api.sendTurn(
                message,
                this.currentTopicId,
                ids,
                options.retryOfTurnId,
                options.signal,
              )
            : await api.sendTurn(message, this.currentTopicId, ids, options.retryOfTurnId);
        } else {
          res = options.signal
            ? await api.sendTurn(message, this.currentTopicId, ids, undefined, options.signal)
            : await api.sendTurn(message, this.currentTopicId, ids);
        }
        // 受理 ≠ 开始执行：SEND 只告诉我们「后端收下了这条消息」。
        // 绝不能在这里写 activeTurnId —— 排队中的 turn 被当成 active 会同时造成
        // 两个后果：Stop 打到错的目标，以及真正在跑那一轮的 TURN_END 被丢弃。
        if (res && res.turn_id) {
          optimistic.turnId = res.turn_id;
          if (queued) {
            this.markTurnQueued(res.turn_id);
          } else {
            this.turnRunning = true;
            this.turnPhase = "waiting";
          }
        }
        // 以受理回执为准：没绑上的附件不得再显示为已带上（契约 §1.2）
        this._applySendReceipt(optimistic, ids, res);
        return true;
      } catch (e) {
        this.lastError = (e as Error).message;
        // 附件被拒（结构化失败）：留下可恢复信息，输入区据此给出逐条原因与出口
        this.lastSendRejection = attachmentRejectionFrom(e);
        // 通知消息流：这次发送没有被受理，界面要回到发送前的样子
        this.sendRejectedSeq += 1;
        // 这条请求没有被后端接受：撤掉乐观消息，交给 Composer 恢复草稿，
        // 避免「界面上有一条没发出去的消息」这种误导状态。
        this.messages = this.messages.filter((m) => m.id !== optimistic.id);
        this.queuedMessageIds = this.queuedMessageIds.filter((id) => id !== optimistic.id);
        // 只有「不是排队」的失败才说明当前 active turn 没起来。
        // 排队请求失败不能把仍在运行的其他任务一起标记成已结束。
        if (!queued) this.turnRunning = false;
        return false;
      }
    },
    /**
     * 以**受理回执**为准核对「界面上带的附件」与「后端真的绑上的附件」（契约 §1.2）。
     *
     * * 回执给了 bound_attachment_ids → 只保留真的绑上的；没绑上的从这条消息上摘掉，
     *   并留下事实说明（绝不出现「界面有附件、模型实际没有」）；
     * * 回执缺失（旧后端）→ 保持原样：不凭空判定，也不伪造一个结果。
     */
    _applySendReceipt(
      message: StreamMessage | undefined,
      requestedIds: string[],
      res:
        | {
            bound_attachment_ids?: string[];
            /** 旧形状的回执：这一轮真实绑定的附件 payload（id 就是事实） */
            attachments?: { id?: string }[];
            rejected?: { id: string; reason: string }[];
          }
        | null
        | undefined,
    ) {
      if (!message) return;
      // 回执优先用 bound_attachment_ids；只有旧形状（attachments 列表）时用它的 id ——
      // 两者都缺才是「拿不到回执」，那时保持原样（不凭空判定）。
      const boundRaw = Array.isArray(res?.bound_attachment_ids)
        ? res?.bound_attachment_ids
        : Array.isArray(res?.attachments)
          ? res.attachments.map((row) => String(row?.id ?? "")).filter(Boolean)
          : null;
      if (!boundRaw) return;
      const bound = new Set(boundRaw.map((x) => String(x)));
      const dropped = requestedIds.filter((id) => !bound.has(id));
      message.attachmentIds = requestedIds.filter((id) => bound.has(id));
      if (message.attachments) {
        message.attachments = message.attachments.filter((a) => bound.has(a.id));
      }
      if (!dropped.length) return;
      const reasons = new Map(
        (res?.rejected ?? []).map((row) => [String(row.id), String(row.reason)]),
      );
      const first = reasons.get(dropped[0]);
      message.attachmentNotice =
        `有 ${dropped.length} 个附件没有附上` + (first ? `：${first}` : "");
      // 全局也留一句：这条消息发出去时附件没带上，用户必须看得见
      this.lastError = message.attachmentNotice;
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
