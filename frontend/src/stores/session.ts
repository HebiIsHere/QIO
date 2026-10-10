import { defineStore } from "pinia";
import {
  api,
  type DevAbandonResult,
  type DevAuthorizationRow,
  type DevTaskRow,
  type InterruptedTurn,
  type ToolRecordPreview,
} from "../services/api";
import {
  confirmStopped as confirmStoppedCall,
  continueRecovery as continueRecoveryCall,
  fetchRecoveryRecords,
  ignoreRecovery as ignoreRecoveryCall,
  isRecoveryConflict,
  repairOrphan as repairOrphanCall,
  requeueDerived as requeueDerivedCall,
  type RecoveryActionView,
  type RecoveryRecordView,
} from "../services/recoveryApi";
import { restoreRuntimeState } from "./restore";
import { newClientRequestId, setNextSendRequestId } from "../services/sendIdentity";
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
 * 终态动作归一（契约 K3.1 / K3.4）：`cancelled` / `stopped` 且**不是真正的中断**时，
 * `resend` 是一个点不通的死按钮 —— 后端 `/api/turns/{id}/resend` 只接受台账里
 * `interrupted` 的行，对已取消的轮必然 409。于是把它归一成 `retry`
 * （前端用既有发送接口新建一轮）；真正 interrupted（reasonCode === "interrupted"）
 * 保留 resend。实时事件、历史 turn_facts、本机留痕与渲染层共用这一条规则。
 */
export function normalizeTurnActionsForStatus(
  status: unknown,
  reasonCode: unknown,
  actions: TurnAction[],
): TurnAction[] {
  const st = String(status ?? "");
  const code = String(reasonCode ?? "");
  if ((st !== "cancelled" && st !== "stopped") || code === "interrupted") return actions;
  if (!actions.includes("resend")) return actions;
  const out: TurnAction[] = [];
  for (const action of actions) {
    const next: TurnAction = action === "resend" ? "retry" : action;
    if (!out.includes(next)) out.push(next);
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
  /**
   * 后端给的终态词（契约 §七 C2）：completed / failed / cancelled / unavailable /
   * **incomplete**（流在结束标记前 EOF —— 已经确认的正文保留，但**不是完成**）。
   * 保持 string 是为了兼容旧记录里可能的未知词：界面不认识的词不当成完成。
   */
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
      if (!row || typeof row !== "object" || typeof row.turnId !== "string") continue;
      // 读取路径归一（K3.4）：旧留痕里的 cancelled + resend 也不能变成死按钮
      out[key] = Array.isArray(row.actions)
        ? {
            ...row,
            actions: normalizeTurnActionsForStatus(
              row.status,
              row.reasonCode,
              normalizeTurnActions(row.actions),
            ),
          }
        : row;
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

/** 消息流里的一轮（契约 §1.5：一轮 = 一个过程区）。 */
export interface StreamTurn {
  id: string;
  index: number;
  startedAt: string;
  items: StreamMessage[];
  parts: TurnItemsView;
  /** 这一轮的 turn_id（旧历史没有 turn_id 时为空串） */
  turnId: string;
  firstAssistantId: string | null;
  running: boolean;
  queued: boolean;
  stages: TurnStage[];
  facts: TurnFacts | null;
  showProcess: boolean;
}

export interface GroupTurnsOptions {
  turnRunning: boolean;
  activeTurnId: string | null;
  stagesFor: (turnId?: string | null) => TurnStage[];
  factsFor: (turnId?: string | null) => TurnFacts | null;
}

/**
 * 按 **turn 身份**组织消息流（F05 修复的核心规则）。
 *
 * 为什么不能用「遇 user 消息才开新一轮」的数组位置分组：排队消息会插进数组中间，
 * 正在运行那一轮随后的说明 / 工具 / 回答就落在排队轮之后，被错误吸收到排队轮里，
 * 同时「最后一轮才 running」会让正在运行的过程区误显示成已停止。
 *
 * 规则：
 * 1. 有 turn_id 的消息按 turn_id 归属；属于同一 turn 的内容即使晚于排队消息到达，
 *    也回到它自己的那一轮；
 * 2. 用户消息**永远**开新一轮（保留用户消息顺序）；turn_id 已被别的轮占用时
 *    （乐观发送到受理回执之间）先不登记，等回执纠正后再归位；
 * 3. 无 turn_id 的旧历史整体按位置分组（user/system 开新轮，其余挂在当前轮）；
 *    无 turn_id 的实时事件优先挂到 active turn，**绝不挂到数组最后一轮**
 *    （最后一轮可能是排队轮）；
 * 4. 只有 active turn（有 turn_id 时）或最后一轮（无 turn_id 的旧后端）可能是 running，
 *    排队轮永远不是 running。
 */
export function groupTurns(messages: StreamMessage[], opts: GroupTurnsOptions): StreamTurn[] {
  const out: StreamTurn[] = [];
  const byTurnId = new Map<string, StreamTurn>();
  let cur: StreamTurn | null = null;
  let n = 0;

  const startTurn = (startedAt: string): StreamTurn => {
    n += 1;
    const turn: StreamTurn = {
      id: `turn_${n}`,
      index: n,
      startedAt,
      items: [],
      parts: { user: [], process: [], answers: [], other: [] },
      turnId: "",
      firstAssistantId: null,
      running: false,
      queued: false,
      stages: [],
      facts: null,
      showProcess: false,
    };
    out.push(turn);
    return turn;
  };

  for (const m of messages) {
    const tid = String(m.turnId ?? "").trim();
    let target: StreamTurn;
    if (m.role === "user" || m.role === "system") {
      target = startTurn(m.createdAt);
      const owner = tid ? byTurnId.get(tid) : undefined;
      if (tid && !owner) {
        byTurnId.set(tid, target);
        target.turnId = tid;
      } else if (tid && owner === target) {
        target.turnId = tid;
      }
      // owner 存在且不是自己 = 乐观消息的临时归属：不登记，回执纠正后自然归位
      target.startedAt = m.createdAt;
      cur = target;
    } else if (tid && byTurnId.has(tid)) {
      target = byTurnId.get(tid) as StreamTurn;
    } else if (tid) {
      // 没有对应 user 消息的 turn（历史 / 系统轮 / 起点丢失）：按 turn_id 自成一节
      target = startTurn(m.createdAt);
      target.turnId = tid;
      byTurnId.set(tid, target);
      cur = target;
    } else if (opts.activeTurnId && byTurnId.has(opts.activeTurnId)) {
      // 旧后端无 turn_id 的实时事件：优先回到 active turn，绝不挂到排队轮
      target = byTurnId.get(opts.activeTurnId) as StreamTurn;
    } else if (cur) {
      target = cur;
    } else {
      target = startTurn(m.createdAt);
      cur = target;
    }
    target.items.push(m);
    if (target.firstAssistantId === null && m.role === "assistant" && !m.interim) {
      target.firstAssistantId = m.id;
    }
  }

  const last = out[out.length - 1];
  for (const turn of out) {
    turn.parts = splitTurnItems(turn.items);
    turn.queued = turn.items.some((m) => m.role === "user" && m.queued === true);
    turn.running =
      opts.turnRunning &&
      !turn.queued &&
      (turn.turnId ? turn.turnId === opts.activeTurnId : turn === last);
    turn.stages = opts.stagesFor(turn.turnId);
    turn.facts = opts.factsFor(turn.turnId);
    turn.showProcess =
      turn.parts.process.length > 0 ||
      turn.running ||
      turn.queued ||
      Boolean(turn.turnId && turn.facts);
  }
  return out;
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

/**
 * 历史 assistant 行的 `raw`（JSON 字符串）→ 核对结论 + 系统核对注记。
 *
 * 契约 K2.3：raw.annotation 是注记的**权威字段**（后端 turn_orchestrator.verification_raw
 * 与 verified 一起落库）；legacy 旧记录可能把注记内联在正文末尾。解析失败 / 字段缺失
 * 一律当作「没有」—— 正常恢复正文，绝不制造失败或「未验证」提醒。
 */
function parseAssistantRaw(raw?: string | null): {
  annotation: string | null;
  verified: VerifiedFact | null;
} {
  if (!raw) return { annotation: null, verified: null };
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown> | null;
    if (!parsed || typeof parsed !== "object") return { annotation: null, verified: null };
    const note = parsed.annotation;
    const annotation = typeof note === "string" && note.trim() ? note.trim() : null;
    return { annotation, verified: normalizeVerification(parsed.verified) };
  } catch {
    return { annotation: null, verified: null };
  }
}

/**
 * 后端「系统核对」事实注记的起始标记（backend core/turn_facts.py::ANNOTATION_HEADER）。
 *
 * 旧形态里这段注记被拼进 `TURN_END.final_content` 的末尾；新形态会走独立字段。
 * 两种形态前端都认：独立字段优先，内嵌的按这个标记切出来，**只保留一份**。
 */
export const SYSTEM_ANNOTATION_HEADER = "—— 系统核对（后端事实，不是模型的说法）：";

/**
 * 把 final_content 切成「模型正文」与「系统核对注记」；没有注记时原样返回。
 *
 * 渲染层（MessageItem）也用它：注记要在**独立「系统事实」区域**显示，
 * 不能混进正文的 Markdown（契约 §七 C1：正文只出现一次、不重启打字动画）。
 */
export function splitSystemAnnotation(text: string): { body: string; annotation: string | null } {
  const raw = String(text ?? "");
  const idx = raw.indexOf(SYSTEM_ANNOTATION_HEADER);
  if (idx < 0) return { body: raw, annotation: null };
  const body = raw.slice(0, idx).replace(/\s+$/, "");
  const annotation = raw.slice(idx).trim();
  return { body, annotation: annotation || null };
}

/** 注记统一带上标记（独立字段没带时补上），保证它在界面上明确是「系统事实」。 */
function labelSystemAnnotation(note: string): string {
  const trimmed = String(note ?? "").trim();
  if (!trimmed) return "";
  return trimmed.startsWith(SYSTEM_ANNOTATION_HEADER)
    ? trimmed
    : `${SYSTEM_ANNOTATION_HEADER}\n${trimmed}`;
}

/** 把注记并入已有回答（同一条消息，正文不复制）；已经包含同一段注记时不重复追加。 */
function appendSystemAnnotation(content: string, note: string): string {
  const labeled = labelSystemAnnotation(note);
  if (!labeled) return content;
  const current = String(content ?? "");
  if (current.includes(labeled)) return current;
  const base = current.replace(/\s+$/, "");
  return base ? `${base}\n\n${labeled}` : labeled;
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

// -- 可恢复记录收件箱（A01 + A03）-----------------------------------------

/** 一次收件箱动作的结果：界面据此就地说明「成功了」还是「为什么没成功」。 */
export interface RecoveryOutcome {
  ok: boolean;
  message: string;
}

/** 后端 `RecoveryRecord.status` 的中文说法；认不出的照原样显示，不编。 */
const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  interrupted: "上次没有执行完",
  pending: "等待执行",
  claimed: "已被接手",
  done: "已完成",
  failed: "已失败",
  cancelled: "已取消",
};

/**
 * 服务端没给 `actions` 时的兜底（例如接的是旧后端）。
 *
 * 兜底**只允许**对唯一一种精确状态生成可点动作：`ready`（已确认可恢复）。
 * 其余状态一律给出禁用按钮 + 一句「为什么现在动不了」—— 界面上绝不能出现
 * 一个点了没效果的按钮，也不能凭 `owner_state === 'unknown'` 就当成「已死」。
 */
function fallbackActions(stateClass: string): RecoveryActionView[] {
  if (stateClass === "ready") {
    return [
      { id: "continue", label: "继续发送这条", enabled: true, reason: "" },
      { id: "ignore", label: "忽略", enabled: true, reason: "" },
    ];
  }
  const reason = stateClass === "orphaned_claim"
    ? "这条记录的重发关系没有写成，先要修好才能继续"
    : stateClass === "derived_stale" || stateClass === "derived_legacy"
      ? "后台任务的归属已经不可用，需要重新排队"
      : stateClass === "legacy_unowned"
        ? "这是升级前留下的记录，没有任何归属信息"
        : "无法确认上次的写入者是否已停止，暂时不能直接动它";
  return [{ id: "continue", label: "暂时不能继续", enabled: false, reason }];
}

/** 记录自己带来的原因原文；为空时不编造，只说状态本身。 */
function statusLabelOf(record: RecoveryRecordView): string {
  const status = (record.status || "").trim();
  if (!status) return "状态未知";
  return STATUS_LABELS[status] ?? status;
}

/**
 * 「为什么现在动不了」的一句话。
 *
 * 优先用服务端给的 `owner_note`（它知道真实归属），其次用记录里的 reason 原文，
 * 再退回状态本身 —— 三层都不含猜测：`unknown` 永远不说成「已停止」。
 */
export function recoveryReasonOf(record: RecoveryRecordView): string {
  const note = (record.owner_note || "").trim();
  if (note) return note;
  const reason = (record.reason || "").trim();
  if (reason) return reason;
  return `这条记录停留在「${statusLabelOf(record)}」`;
}

/** 服务端给的动作按钮文案；缺 label 时用一句能看懂的中文兜底。 */
export function recoveryActionLabel(actionId: string, label?: string): string {
  const text = (label || "").trim();
  if (text) return text;
  if (actionId === "continue") return "继续发送这条";
  if (actionId === "repair") return "修好这条记录";
  if (actionId === "ignore") return "忽略";
  if (actionId === "requeue") return "重新排队";
  if (actionId === "confirm_stopped") return "确认旧执行者已停止";
  return "处理";
}

/**
 * 「修好」之后本地就地得到的形状（不靠刷新）。
 *
 * 修复的语义是「回到可继续 / 可忽略」：状态原文回到 `interrupted`，
 * 归属变成「本实例」，动作换成继续 / 忽略。它仍然需要用户明确点一下 ——
 * 修复本身绝不自动重发。
 */
function repairedRecord(record: RecoveryRecordView): RecoveryRecordView {
  return {
    ...record,
    kind: record.kind === "derived_task" ? "derived_task" : "user_turn",
    state_class: "ready",
    status: record.kind === "derived_task" ? "pending" : "interrupted",
    owner_state: "alive",
    owner_note: "",
    actions: [
      { id: "continue", label: "继续发送这条", enabled: true, reason: "" },
      { id: "ignore", label: "忽略", enabled: true, reason: "" },
    ],
  };
}

/**
 * 首屏那行汇总：只说数量，**不替用户点**任何东西。
 *
 * 「继续」是用户明确决定的动作（进程退出可能正是他的意思），所以这里
 * 只报告「有几条需要你决定」，绝不自动重发。
 */
function recoveryInboxSummary(records: RecoveryRecordView[]): string {
  if (!records.length) return "";
  return records.length === 1
    ? "上次有一条记录需要你决定：继续还是忽略"
    : `上次有 ${records.length} 条记录需要你决定：继续还是忽略`;
}

/**
 * `/api/runtime/state` 里的 `orphaned_turns`（与 `interrupted_turns` 同形状）
 * → 收件箱里的一条记录。
 *
 * 这类记录的精确状态是「抢占过、但没有写成任何后继」：唯一有意义的动作是
 * **修好它**（修好之后才谈得上继续 / 忽略）。这里不生成可点的「继续」，
 * 否则用户会点到一个注定 409 的按钮。
 */
function orphanedTurnToRecord(turn: InterruptedTurn): RecoveryRecordView {
  const updatedAt = String(turn.ended_at || turn.updated_at || "");
  return {
    record_id: String(turn.turn_id ?? ""),
    kind: "user_turn",
    state_class: "orphaned_claim",
    status: String(turn.status ?? "interrupted"),
    message: turn.message ?? null,
    topic_id: turn.topic_id ?? null,
    reason: turn.reason ?? null,
    created_at: String(turn.created_at ?? ""),
    updated_at: updatedAt || null,
    owner_instance_id: null,
    owner_state: "unknown",
    owner_note: (turn.reason_text || "").trim() || "这条记录的重发关系没有写成，需要先修好",
    claim_generation: null,
    attempts: null,
    last_error: null,
    actions: [
      {
        id: "repair",
        label: "修好这条记录",
        enabled: true,
        reason: "",
      },
      {
        id: "continue",
        label: "继续发送这条",
        enabled: false,
        reason: "需要先修好这条记录的重发关系",
      },
    ],
  };
}


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
  /**
   * 提交这条消息时**捕获下来的起点身份**（M01）。
   *
   * 起点身份必须在「受理那一刻」定死：之后用户再去星球里浏览、改选片段、
   * 明确进入别的话题，都不能追溯改向这条已经提交（可能正在排队）的消息。
   * 它同时是「受理结果与提交时是否一致」的核对依据。
   */
  startIdentity?: StartIdentity;
}

/** 一次发送在受理时捕获的起点身份（稳定身份，不随后续导航变化）。 */
export interface StartIdentity {
  /** 提交时的话题（后端 `/api/turns` 收到的 topic_id） */
  topicId: string | null;
  /** 提交时锚定的片段（null = 话题的最新位置） */
  fragmentId: string | null;
  /** 提交时待落实的接续选择（null = 没有登记「从某段历史继续」） */
  intentId: string | null;
  /** 提交时的话题名（只用于界面文案，不参与判定） */
  topicName: string | null;
  /** 捕获时刻（毫秒），用于诊断与「先后关系」判断 */
  capturedAt: number;
}

/**
 * 这个失败是否意味着「没有拿到发送回执」？
 *
 * * ApiTimeoutError（name === "ApiTimeoutError"）：等不到响应，结果未知；
 * * TypeError：fetch 的网络层失败，结果未知；
 * * 带 status 的错误（ApiError）是后端**明确**的拒绝（后端已给出结论），不算；
 * * 其它错误按明确失败处理（保持既有行为）。
 *
 * 按名字/形状判断而不 import 具体错误类：api 模块在多处既有测试里被整体 mock
 * （工厂只提供 api 对象），import 具体类会让那些 mock 变成 undefined 而炸掉。
 */
function isSendReceiptUnknown(e: unknown): boolean {
  // Lead 裁决（契约 5）：没有响应就没有结论 —— 判定口径是 sendErrorStatus，
  // 而不是错误类型：ApiTimeoutError / TypeError / 任何没有 status 的失败
  // 都属于「没拿到发送回执，受理情况未知」（契约：只有明确 4xx 才撤回为未
  // 发送草稿）。带 status 的错误（ApiError）是后端明确给出的结论，不算。
  const status = sendErrorStatus(e);
  return !(status !== null && status >= 400 && status < 500);
}

/** 错误携带的 HTTP 状态码（ApiError）；不携带（普通 Error / TypeError）→ null。 */
function sendErrorStatus(e: unknown): number | null {
  const status = (e as { status?: unknown } | null)?.status;
  return typeof status === "number" ? status : null;
}

/**
 * 一次发送动作的生命周期状态（契约 5）。
 *
 * 四个尝试态：
 * * `pending` —— 已发出、还没拿到任何回执；
 * * `accepted` —— 后端明确受理（HTTP 200 回执或查证命中；幂等命中也算）；
 * * `confirmed-rejected` —— 后端**明确**拒绝（有响应、无副作用）；
 * * `needs-confirm` —— 没拿到回执（超时 / 网络失败）：结果未知，正在确认。
 *
 * 「accepted」是终点：成功回执只能 pending → accepted **单向前进**，
 * 绝不因一张迟到的回执把已经推进的运行态拉回去。
 */
export type SendAttemptState = "pending" | "accepted" | "confirmed-rejected" | "needs-confirm";

/** 一次发送动作（乐观消息 + 请求身份 + 状态）。重试复用同一个 clientRequestId。 */
export interface SendAttempt {
  /** 乐观消息 id：撤回与采纳都定位到它 */
  messageId: string;
  /** 请求身份（幂等键）：同一发送动作（含所有重试）永远复用同一个 */
  clientRequestId: string;
  state: SendAttemptState;
  /** 受理回执 / 查证命中拿到的 turn_id */
  turnId?: string;
  /** 原文：重试按原样重发，不取当前草稿 */
  message: string;
  /** 发送时的话题：重试回到原话题，不跟当前选中走 */
  topicId: string | null;
  /** 发送时是否已在排队（turnRunning 已为 true） */
  queued: boolean;
  /** 确认查询进行中（防重入） */
  confirmBusy?: boolean;
  /** 重发进行中（防重入） */
  retryBusy?: boolean;
}

/**
 * 「正在确认」的界面状态。
 *
 * 文案必须区分两件事（契约 5 第 6 条）：
 * * 正在确认是否已发送（本状态）；
 * * 明确失败：被拒绝（进 lastError）。
 */
export interface SendConfirmState {
  messageId: string;
  clientRequestId: string;
  /** 当前文案（界面原样显示，不自己推断） */
  notice: string;
  /** true = 查到 404：后端无记录 → 提供【重试 / 放弃】 */
  unknown: boolean;
  /** true = 查到命中：已受理 → 不提供放弃，只提供取消 */
  accepted: boolean;
  /** 查询进行中 */
  busy: boolean;
}

export const SEND_CONFIRM_QUERYING_NOTICE = "正在确认这条消息是否已经发出…";
export const SEND_CONFIRM_RETRY_NOTICE =
  "正在确认这条消息是否已经发出…（刚才的确认查询没有成功，可以再查一次）";
/** 404 的呈现：进程重启也会变成 404，所以是「未确认」，绝不是「未发送」。 */
export const SEND_CONFIRM_UNKNOWN_NOTICE = "发送未确认：后端没有该请求记录（可能未送达）";
export const SEND_REJECTED_NOTICE = "发送失败：被拒绝";
/** 超时 / 网络失败后等多久才去查证（数百毫秒：给回执一点「在路上」的时间）。 */
export const SEND_CONFIRM_QUERY_DELAY_MS = 600;

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
     * 权威快照给的那份「未执行消息」**原始集合**（未过滤）。
     *
     * `interruptedTurns` 现在是从它派生出来的：收件箱（recoveryRecords）已经覆盖的记录
     * 不再由旧入口重复提供操作；本地已确认处理的、以及被收件箱覆盖的都在派生时过滤掉。
     * 快照失败时保留旧值 —— 「拉不到」不能说成「没有」。
     */
    _rawInterruptedTurns: [] as InterruptedTurn[],
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
    /**
     * 「取消未落实的接续选择」是否正在提交（M01）。
     *
     * 明确改变起点时（进入某话题最新位置 / 选中的就是当前开放片段 / 确认话题切换）
     * 必须先把这个选择取消掉，否则下一条消息会被旧登记接续带偏。单飞避免连点两次。
     */
    _cancelContinuationBusy: false,
    /**
     * 本地已经确认结束的「未执行消息」（M12）。
     *
     * 恢复快照可能是这些事项结束**之前**取的：稍旧的快照不能让它们复活成
     * 一个点不动的假待办。这里记下确认结束的 turn_id（有界），应用快照时过滤。
     */
    _resolvedInterruptedTurnIds: [] as string[],
    /**
     * 可恢复记录收件箱（A01 + A03）。
     *
     * 一台规则同时覆盖：确认中断的用户消息、孤立重发、升级前无归属的历史行、
     * 归属判不出来的行、以及归属已死的派生任务。界面**只**在这里呈现它们 ——
     * 「有的记录永远看不见」正是这两个缺陷的共同根因。
     */
    recoveryRecords: [] as RecoveryRecordView[],
    /**
     * 服务端说的匹配总数（不受 limit 影响）。
     * 它比已显示的条数多时，界面必须说出「还有 N 条未显示」——
     * 剩下的记录不能因为没被返回就看起来不存在。
     */
    recoveryTotal: 0,
    /** 正在拉收件箱（单飞；**同一实例**同一时间只有一个请求） */
    recoveryLoading: false,
    /**
     * 收件箱读取的请求代次 + 这次在飞请求所属的后端实例。
     *
     * 复用 `_devTasksSeq` 的既有模式：提交结果前同时校验「仍是最新一次请求」与
     * 「发起时的实例没被换掉」。后端换实例时旧实例的响应必须整段丢弃 ——
     * 它会把旧实例的记录写进新实例的清单；而新实例的读取也不能被旧请求的单飞锁
     * 挡住、更不能被旧请求的 finally 提前解锁。
     */
    _recoverySeq: 0,
    _recoveryLoadingInstance: null as string | null,
    /**
     * 收件箱的最近一次结果说明。
     *
     * 失败时**保留清单**（可重试），成功时也写一句 —— 「点了没反应」和
     * 「悄悄成功」都是任务书禁止的静默状态。
     */
    recoveryError: "",
    /** 正在提交的那条（record_id）：同一条只能有一个请求在飞，重复点击不重发 */
    recoveryBusyId: "" as string,
    /**
     * 已经处理掉的记录 id（有界）。
     *
     * `/api/runtime/state` 的快照可能是处理**之前**取的，而它是另一个请求
     * （专用接口失败时也照样要显示）。没有这份集合，旧快照就会把用户刚处理完的
     * 记录复活成一个点不动的假待办。
     */
    _recoveredResolvedIds: [] as string[],
    /** 最近一次 `/api/runtime/state` 里的孤儿记录：并入同一份清单用 */
    _pendingOrphanedTurns: [] as InterruptedTurn[],
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
    /**
     * 本机发送动作的生命周期台账（契约 5）。
     * 每一次 send 领一个请求身份；受理回执只能把条目 pending → accepted 单向推进，
     * 回执丢失时条目停在 needs-confirm，由查证（by-request 端点）收尾。
     * 有界：只保留最近 50 条。
     */
    sendAttempts: [] as SendAttempt[],
    /**
     * 当前「正在确认是否已发送」的界面状态（文案区分「正在确认」与
     * 「发送未确认：后端没有该请求记录」；明确失败走 lastError）。
     */
    sendConfirm: null as SendConfirmState | null,
    /** 各 attempt 的查证定时器（messageId → timer id；非业务状态） */
    _confirmTimers: {} as Record<string, ReturnType<typeof setTimeout>>,
    /**
     * TURN_START 已关联的 request_id（有界）。
     * 「这一轮已经开始」是事件带来的事实：受理回执/查证不得再改运行态，
     * 「后端无记录」的收敛也不得误伤一条真实开始过的轮次。
     */
    startedRequestIds: [] as string[],
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
    /** 按乐观消息 id 找发送动作（界面把「重试/放弃/取消」挂回这条消息用）。 */
    sendAttemptForMessage: (state) => (messageId: string): SendAttempt | undefined =>
      state.sendAttempts.find((a) => a.messageId === messageId),
    /** 当前是否处于「正在确认是否已发送」（界面据此区分两种失败文案）。 */
    pendingSendConfirm: (state): SendConfirmState | null => state.sendConfirm,
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

    /**
     * M01：取消「下一条消息从这段历史继续」的登记（复用既有 cancelContinuation 接口）。
     *
     * 用在**明确改变起点**的动作之前：进入某话题的最新位置、选中当前开放片段、
     * 确认话题切换。语义：
     * * 没有登记时是 no-op（不产生任何请求）；
     * * 成功 → 本地立刻清掉 pendingContinuation，保证紧接着的发送不会带上旧意图
     *   （后台也会广播新的 ANCHOR，两边最终一致）；
     * * 失败 → **不改本地状态**（提示保留），返回 false 并给出可读原因，调用方
     *   必须放弃这次起点改变 —— 宁可不动，也不把消息送到错误的起点；
     * * 只是浏览星球 / 查看历史**不会**调到这里，所以浏览不取消选择。
     */
    async cancelPendingContinuation(): Promise<boolean> {
      if (!this.pendingContinuation) return true;
      if (this._cancelContinuationBusy) return false;
      this._cancelContinuationBusy = true;
      try {
        const res = await api.cancelContinuation();
        if (res && res.ok === false) {
          this.lastError = "后端没有确认取消「从所选历史继续」，起点未改变（可以重试）";
          return false;
        }
        // 服务端已确认取消 → 本地同步撤下提示，避免「本地还留着旧选择」。
        // （服务端随后广播的 ANCHOR 也会是 pending=null，两边一致。）
        this.pendingContinuation = null;
        return true;
      } catch (e) {
        this.lastError = `没能取消「从所选历史继续」：${(e as Error).message}（起点未改变）`;
        return false;
      } finally {
        this._cancelContinuationBusy = false;
      }
    },

    /**
     * M01：捕获「这一次发送的起点身份」。
     *
     * 三个字段都在提交那一刻从本地状态取值，之后不再重新计算 —— 已受理
     * （可能还在排队）的消息因此不会被后续导航追溯改向。
     */
    captureStartIdentity(): StartIdentity {
      return {
        topicId: this.currentTopicId,
        fragmentId: this.anchorFragmentId,
        intentId: this.pendingContinuation?.intentId ?? null,
        topicName: this.topicName,
        capturedAt: Date.now(),
      };
    },
    /** 用户点「转到这里」：只有这一步会真的改变 Anchor。 */
    async confirmPendingSwitch() {
      const pending = this.pendingSwitch;
      if (!pending || this.pendingSwitchBusy) return;
      this.pendingSwitchBusy = true;
      try {
        /**
         * M01：确认「转到这里」= 明确改变起点 → 旧的未落实接续选择必须一起取消。
         * 否则切换之后的第一条消息仍会被旧登记带偏（起点与界面显示不一致）。
         * 取消失败就不切换：宁可不动，也不把消息送到错误的起点。
         */
        if (!(await this.cancelPendingContinuation())) {
          return;
        }
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
     * TURN_START 携带 request_id 时的精确关联（契约 5）。
     *
     * 事件层在分发 TURN_START 时调用：把「这次发送对应的轮次已经开始」记下来。
     * 之后发生的一切都据此判断：
     * * 受理回执/查证命中不得改写该轮运行态（它们本来就只单向落定 attempt）；
     * * 查证 404（后端重启 = 记录丢失）时**不**收敛运行态 —— 轮次可能真的在跑；
     * * 该 attempt 的 turn_id 在这里就能关联上，不必等回执。
     */
    noteTurnStartedForRequest(requestId: string, turnId?: string) {
      if (!requestId) return;
      if (!this.startedRequestIds.includes(requestId)) {
        this.startedRequestIds = [...this.startedRequestIds.slice(-199), requestId];
      }
      if (turnId) {
        const attempt = this.sendAttempts.find((a) => a.clientRequestId === requestId);
        if (attempt && !attempt.turnId) attempt.turnId = turnId;
      }
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
      // 实例切换：在飞的收件箱请求属于旧实例，整个作废（代次 +1）。
      // 旧请求的 finally 因此不会再解锁 loading；新实例的读取可以立刻发起。
      this._recoverySeq += 1;
      this._recoveryLoadingInstance = null;
      this.recoveryLoading = false;
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
     * 这一轮的用户消息是否还标着「等待中」（已受理、还没开始）。
     *
     * 与 isQueuedTurn 的区别：队列快照可能已经把它摘出 queued 列表，但消息上的
     * 「等待中」标记（以及它「从未开始」这个事实）还在 —— 迟到 / 竞争到达的
     * 结束事件要靠它判断「这确实是一个排队轮的结局」（§七 C1/C5）。
     */
    hasQueuedMessage(turnId: string): boolean {
      const id = String(turnId ?? "");
      if (!id) return false;
      return this.messages.some((m) => m.role === "user" && m.turnId === id && m.queued === true);
    },
    /**
     * 排队中的 turn 已经有结局（取消 / 准备失败）。
     *
     * 结束事实由 recordTurnFacts **先**落地，这里只负责清理排队标记：
     * * 清掉这条用户消息的「等待中」；
     * * 把它登记为「已取消」（QueueChip 可查看）；
     * * 绝不触碰 active / turnRunning —— 正在跑的可能是另一轮。
     */
    concludeQueuedTurn(turnId: string) {
      const id = String(turnId ?? "");
      if (!id) return;
      const message = this.messages.find((m) => m.role === "user" && m.turnId === id);
      for (const m of this.messages) {
        if (m.role === "user" && m.turnId === id && m.queued) m.queued = false;
      }
      if (message) {
        this.queuedMessageIds = this.queuedMessageIds.filter((mid) => mid !== message.id);
      }
      if (!this.turnQueue.cancelled.some((c) => c.turn_id === id)) {
        this.turnQueue = {
          ...this.turnQueue,
          cancelled: [
            ...this.turnQueue.cancelled,
            { turn_id: id, message: message?.content ?? "" },
          ],
        };
      }
      this.forgetQueuedTurn(id);
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
        // 两个入口共用同一份「已处理」真相：收件箱里同一条也不再残留/复活
        this._syncRecoveryHandled(turnId);
        // R6：成功重发之后这一轮的本机留痕与「未完成」入口都不再给死按钮
        this.markTurnResendConsumed(turnId);
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
        // 两个入口共用同一份「已处理」真相：收件箱里同一条也不再残留/复活
        this._syncRecoveryHandled(turnId);
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
          this._syncRecoveryHandled(id);
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
     * 事件流可能已经不完整（收到 RESYNC / 重连 / 实例变化）：
     * 不再假装状态是最新的，交给**唯一恢复入口** `stores/restore.ts`。
     *
     * 保留这个方法名是因为它已经被既有调用点与用例使用；实现只有一份。
     */
    async resyncTurnState(): Promise<void> {
      await restoreRuntimeState("resync");
    },

    // -- 可恢复记录收件箱（A01 + A03）------------------------------------

    /**
     * 收下 `/api/runtime/state` 里的孤儿记录（快照里的那一份）。
     *
     * 为什么必须单独收：`orphaned_turns` 与 `interrupted_turns` 是两个出口，
     * 专用收件箱接口失败时前者仍然看得到 —— 孤儿记录一旦不可见，那条消息就
     * 永久消失（点都点不到）。它们先与**本次快照时刻**的已处理集合过滤一次，
     * 再与收件箱合并；下一次快照到达时会重新合并，所以旧快照不会复活已处理的记录。
     */
    adoptOrphanedTurns(turns: InterruptedTurn[] | undefined) {
      this._pendingOrphanedTurns = (turns ?? []).map((t) => ({ ...t }));
      this.mergeRecoveryInbox();
    },
    /**
     * 把「运行时快照里的孤儿」与「收件箱接口的记录」并成同一份清单。
     *
     * 去重规则：同一条 id 只出现一次；两边都有时**以收件箱记录为准**（它带
     * `actions` 与归属说明，能真的操作）。
     */
    mergeRecoveryInbox() {
      const resolved = new Set(this._recoveredResolvedIds);
      const merged: RecoveryRecordView[] = [];
      const seen = new Set<string>();
      for (const record of this.recoveryRecords) {
        if (!record?.record_id || resolved.has(record.record_id) || seen.has(record.record_id)) {
          continue;
        }
        seen.add(record.record_id);
        merged.push(record);
      }
      for (const turn of this._pendingOrphanedTurns) {
        const id = String((turn as { turn_id?: string }).turn_id ?? "");
        if (!id || resolved.has(id) || seen.has(id)) continue;
        seen.add(id);
        merged.push(orphanedTurnToRecord(turn));
      }
      // 服务端说「还有没返回的」时，总数不能被本地合并改小 —— 截断提示必须真实
      this.recoveryTotal = Math.max(this.recoveryTotal, merged.length);
      this.recoveryRecords = merged;
      // 收件箱清单变了：旧入口（InterruptedTurnEntry）渲染的那份派生清单也要跟着收敛
      this._syncInterruptedTurns();
    },
    /**
     * 从 `_rawInterruptedTurns` 派生出旧入口渲染的 `interruptedTurns`。
     *
     * 两个入口（旧的 InterruptedTurnEntry 与新的 RecoveryInbox）覆盖的是**同一批**事项，
     * 同一条记录绝不能同时出现在两边（那会让用户对同一条重复操作，且状态各说各话）。
     * 收件箱已经覆盖的 id 由新入口独家提供操作；收件箱拉不到时旧入口仍然兜底显示
     * （覆盖不会因此丢失）。已确认处理的 id 两边都过滤：稍旧的快照不许复活它们。
     */
    _syncInterruptedTurns() {
      const resolved = new Set(this._resolvedInterruptedTurnIds);
      const covered = new Set(this.recoveryRecords.map((r) => r.record_id));
      this.interruptedTurns = this._rawInterruptedTurns.filter(
        (t) => !resolved.has(t.turn_id) && !covered.has(t.turn_id),
      );
    },
    /**
     * 一条记录已经被处理掉（继续 / 忽略）：两个入口共用这一份「已处理」真相。
     *
     * * 从收件箱清单移除，并记入两个「已处理」集合（新旧快照都不许复活）；
     * * 总数**有依据地**减一：这条本来就属于服务端匹配集合（列表里拿到的，或
     *   合并进总数的那份孤儿）—— 不重读清单（既有契约要求就地更新），
     *   也不把未知数量猜成 0；
     * * 重新派生旧入口的清单，让另一处入口不再残留。
     */
    _syncRecoveryHandled(recordId: string) {
      if (!recordId) return;
      const had = this.recoveryRecords.some((r) => r.record_id === recordId);
      this.recoveryRecords = this.recoveryRecords.filter((r) => r.record_id !== recordId);
      this.noteRecoveryResolved(recordId);
      this.noteInterruptedTurnResolved(recordId);
      if (had) {
        this.recoveryTotal = Math.max(this.recoveryRecords.length, this.recoveryTotal - 1);
      }
      this._syncInterruptedTurns();
    },
    /** 本地确认一条可恢复记录已经处理掉：旧快照不许把它复活。 */
    noteRecoveryResolved(recordId: string) {
      if (!recordId || this._recoveredResolvedIds.includes(recordId)) return;
      this._recoveredResolvedIds.push(recordId);
      if (this._recoveredResolvedIds.length > 200) this._recoveredResolvedIds.shift();
    },
    /**
     * 拉一次收件箱（**单飞**）。
     *
     * 单飞的理由与发布闸门一样：恢复入口（连接建立 / 重连 / RESYNC / 实例变化）
     * 可能几乎同时触发好几次，没必要发同样四五个请求。失败时**保留上一次的清单**
     * 并写下可重试说明 —— 绝不把「拉不到」擦成「没有未完成的事」。
     */
    async loadRecoveryInbox(): Promise<void> {
      const instanceAtStart = this.instanceId;
      // 单飞只约束**同一实例**：已经在飞的请求若是旧实例的，新实例必须能继续读，
      // 不能被旧请求的锁挡在门外。
      if (this.recoveryLoading && this._recoveryLoadingInstance === instanceAtStart) return;
      const seq = ++this._recoverySeq;
      this._recoveryLoadingInstance = instanceAtStart;
      this.recoveryLoading = true;
      this.recoveryError = "";
      try {
        const listing = await fetchRecoveryRecords();
        // 迟到响应（实例已切换 / 已有更新的请求）：整段丢弃，一个字段都不许写
        if (!this._recoveryResultBelongs(seq, instanceAtStart)) return;
        this.recoveryRecords = listing.records ?? [];
        this.recoveryTotal =
          typeof listing.total === "number" && Number.isFinite(listing.total)
            ? listing.total
            : this.recoveryRecords.length;
      } catch (e) {
        // 失败也必须属于当前实例：旧实例的错误不能写到新实例的清单上
        if (!this._recoveryResultBelongs(seq, instanceAtStart)) return;
        // 拉不到 ≠ 没有：保留既有清单（含快照里的孤儿），说明这一份可能不完整
        this.recoveryError =
          `没能读取「未完成事项」清单：${(e as Error).message}` +
          "（已显示的记录仍然可以处理，可以重试）";
      } finally {
        // 只有仍属于当前实例、且自己还是最新一次请求的那次加载才允许合并与解锁 ——
        // 旧请求不得把新请求的 loading 提前关掉。
        if (this._recoveryResultBelongs(seq, instanceAtStart)) {
          // 合并放在 finally：无论成功失败，快照里的孤儿都必须在这一份清单里看得见
          this.mergeRecoveryInbox();
          this._recoveryLoadingInstance = null;
          this.recoveryLoading = false;
        }
      }
    },
    /** 这次收件箱读取是否仍属于「当前实例 + 最新一次请求」。 */
    _recoveryResultBelongs(seq: number, instanceAtStart: string | null): boolean {
      return seq === this._recoverySeq && this.instanceId === instanceAtStart;
    },
    /** 找到一条记录；找不到时给出可读原因（不抛半截状态）。 */
    _recoveryRecord(recordId: string): RecoveryRecordView | null {
      return this.recoveryRecords.find((r) => r.record_id === recordId) ?? null;
    },
    /** 单飞闸门：同一条记录只允许一个请求在飞。 */
    _beginRecoveryAction(recordId: string): RecoveryOutcome | null {
      if (this.recoveryBusyId) {
        return { ok: false, message: "上一次操作还在提交中，请稍候" };
      }
      if (!recordId) {
        return { ok: false, message: "这条记录没有标识，无法处理（请重试）" };
      }
      this.recoveryBusyId = recordId;
      this.recoveryError = "";
      return null;
    },
    /**
     * 失败：保留这条记录（可重试），把可读原因就地写出来。
     *
     * **409 不等于「已经被处理过」**：后端在好几种情况下都回 409 —— 记录状态已经变化、
     * 原写入者还在运行不能接管、孤立记录要先修复再继续。这些情况下这条记录**仍然在
     * 权威清单里**，所以绝不能凭本地猜测把它标成「已解决」再遮掉（那正是「以不确定为
     * 由永久隐藏记录」）。只做一件事：重新拉一次权威清单，由后端决定它还在不在；
     * 原因用后端给的那句话，不自己编。
     */
    _failRecovery(_recordId: string, prefix: string, e: unknown): RecoveryOutcome {
      const conflict = isRecoveryConflict(e);
      const serverReason = String((e as Error)?.message ?? "").trim();
      const message = conflict
        ? `${prefix}没有完成：${serverReason || "这条记录的状态已经变化"}（清单已按后端最新状态刷新，可重试）`
        : `${prefix}没有成功：${serverReason || "未知原因"}（可以重试）`;
      this.recoveryError = message;
      if (conflict) {
        // 不猜本地状态：只重新要一份真相（不再本地标记 resolved，避免遮掉仍然可操作的记录）
        void this.loadRecoveryInbox();
      }
      return { ok: false, message };
    },
    /**
     * 「继续这一条」。
     *
     * 只有后端确认成功才从入口移除（**不做乐观移除**）；失败就地保留 + 原因 + 重试。
     * 不在这里自动重发：进程退出可能正是用户的意思。
     */
    async continueRecovery(recordId: string): Promise<RecoveryOutcome> {
      const gate = this._beginRecoveryAction(recordId);
      if (gate) return gate;
      const record = this._recoveryRecord(recordId);
      try {
        const res = await continueRecoveryCall(recordId, {
          expected_class: String(record?.state_class ?? ""),
          expected_status: String(record?.status ?? ""),
        });
        if (!res?.ok) {
          throw new Error("后端没有确认这条记录可以继续");
        }
        // 两个入口共用同一份「已处理」真相（列表 / total / 旧入口一起收敛）
        this._syncRecoveryHandled(recordId);
        this.recoveryError = "已经按原话题重新排队，这一轮马上开始";
        return { ok: true, message: this.recoveryError };
      } catch (e) {
        return this._failRecovery(recordId, "继续这条记录", e);
      } finally {
        this.recoveryBusyId = "";
      }
    },
    /**
     * 「修好这一条」（孤立重发）。
     *
     * 修复本身不改消息原文、不新建 turn：成功之后这条回到「可继续 / 可忽略」，
     * 仍然要用户明确点一下才会重发。
     */
    async repairOrphan(recordId: string): Promise<RecoveryOutcome> {
      const gate = this._beginRecoveryAction(recordId);
      if (gate) return gate;
      const record = this._recoveryRecord(recordId);
      try {
        const res = await repairOrphanCall(recordId, String(record?.state_class ?? "orphaned_claim"));
        if (res && res.ok === false && res.repaired !== true) {
          // 后端明确说「这条不在孤儿状态 / 已经真正重发过」：不假装修好了
          const why = (res.reason || "").trim();
          this.recoveryError = why
            ? `这条记录没有需要修复的地方：${why}`
            : "这条记录已经不在可修复状态（可能已经真正重发过）";
          return { ok: false, message: this.recoveryError };
        }
        if (record) {
          const fixed = repairedRecord(record);
          this.recoveryRecords = this.recoveryRecords.map((r) =>
            r.record_id === recordId ? fixed : r,
          );
          // 让快照里的孤儿也被替换成修好后的形状
          this._pendingOrphanedTurns = this._pendingOrphanedTurns.filter(
            (t) => String((t as { turn_id?: string }).turn_id ?? "") !== recordId,
          );
        }
        this.recoveryError = "已经修好这条记录：现在可以继续发送，或忽略它";
        return { ok: true, message: this.recoveryError };
      } catch (e) {
        return this._failRecovery(recordId, "修复这条记录", e);
      } finally {
        this.recoveryBusyId = "";
      }
    },
    /**
     * F01：「确认写下这条无归属记录的旧执行者已经停止」。
     *
     * 旧版本不写实例 / 心跳 / 归属 —— 库里没有证据说明它停了，所以 continue /
     * ignore / repair / requeue 默认全被挡住。这是**唯一**的解锁入口：由用户显式
     * 承担「旧进程已经退出」这个判断，服务端把它持久化下来。
     *
     * 确认本身不执行任何东西，也不改消息原文；确认之后重读清单，让按钮的真实可用性
     * 仍由服务端判定（前端不自己猜哪一条变成可点了）。
     */
    async confirmStopped(recordId: string): Promise<RecoveryOutcome> {
      const gate = this._beginRecoveryAction(recordId);
      if (gate) return gate;
      const record = this._recoveryRecord(recordId);
      try {
        const res = await confirmStoppedCall(recordId, String(record?.state_class ?? ""));
        if (!res || res.ok !== true || res.confirmed !== true) {
          throw new Error("后端没有确认这条记录");
        }
        this.recoveryRecords = this.recoveryRecords.map((r) =>
          r.record_id === recordId ? { ...r, confirmed_stopped: true } : r,
        );
        // 真实可用性由服务端重新判定（动作列表是服务端给的，前端不重算）
        await this.loadRecoveryInbox();
        this.recoveryError = "已确认旧执行者已停止：现在可以继续或忽略了";
        return { ok: true, message: this.recoveryError };
      } catch (e) {
        return this._failRecovery(recordId, "确认旧执行者已停止", e);
      } finally {
        this.recoveryBusyId = "";
      }
    },
    /**
     * 「忽略这一条」：不再提示，但原文与记录都由后端保留（前端不删数据）。
     * 只有真正成功才从入口移除；409 时向后端要真相。
     */
    async ignoreRecovery(recordId: string): Promise<RecoveryOutcome> {
      const gate = this._beginRecoveryAction(recordId);
      if (gate) return gate;
      const record = this._recoveryRecord(recordId);
      try {
        await ignoreRecoveryCall(recordId, String(record?.state_class ?? ""));
        // 两个入口共用同一份「已处理」真相（列表 / total / 旧入口一起收敛）
        this._syncRecoveryHandled(recordId);
        this.recoveryError = "已忽略这一条（原文仍然保留在记录里）";
        return { ok: true, message: this.recoveryError };
      } catch (e) {
        return this._failRecovery(recordId, "忽略这条记录", e);
      } finally {
        this.recoveryBusyId = "";
      }
    },
    /**
     * 「重新排队」（归属已死的派生任务）。
     *
     * `attempts` / `last_error` 由后端原样保留；这里只就地把它标成等待执行，
     * 不重置任何计数（重置会让用户以为失败从没发生过）。
     */
    async requeueDerived(recordId: string): Promise<RecoveryOutcome> {
      const gate = this._beginRecoveryAction(recordId);
      if (gate) return gate;
      const record = this._recoveryRecord(recordId);
      try {
        const res = await requeueDerivedCall(recordId, {
          expected_state: String(record?.status ?? ""),
          expected_generation: record?.claim_generation ?? null,
        });
        if (res && res.ok === false) {
          throw new Error("后端没有确认这条任务可以重新排队");
        }
        this.recoveryRecords = this.recoveryRecords.map((r) =>
          r.record_id === recordId
            ? {
                ...r,
                status: String(res?.state ?? "pending"),
                state_class: "ready",
                owner_note: "",
              }
            : r,
        );
        this.recoveryError = "已经重新排队，这一项马上会被再次执行";
        return { ok: true, message: this.recoveryError };
      } catch (e) {
        return this._failRecovery(recordId, "重新排队", e);
      } finally {
        this.recoveryBusyId = "";
      }
    },
    /** 首屏那一行汇总（不入全局提示、不自动重发）。 */
    recoveryInboxSummary(): string {
      return recoveryInboxSummary(this.recoveryRecords);
    },
    /**
     * M12：把权威快照里的「未完成事项」一次性收下。
     *
     * * `interrupted_approvals`：上一次进程结束时没执行的工具操作（只说明事实）；
     * * `interrupted_turns`：已经被接受、但没执行完的用户消息。
     *
     * 快照是权威，但它可能是在某个事项**刚刚结束之前**取的 —— 本地已经确认
     * 结束（用户点了继续 / 忽略，或别处处理过）的那些不能被它复活成一个
     * 点不动的假待办，所以这里按「本地已确认结束」的 id 过滤。
     */
    applyInterruptedState(
      approvals: {
        approval_id: string;
        kind: string;
        what: string;
        created_at: string;
      }[],
      turns: InterruptedTurn[],
      orphanedTurns?: InterruptedTurn[],
    ) {
      this.interruptedOperations = (approvals ?? []).map((item) => ({
        approval_id: item.approval_id,
        kind: item.kind,
        what: item.what,
        createdAt: item.created_at,
      }));
      this._rawInterruptedTurns = (turns ?? []).map((t) => ({ ...t }));
      /**
       * 孤儿记录（抢占过、没有后继）走**同一份收件箱**：它们不在
       * `interrupted_turns` 里，所以必须有这个出口，否则那条消息永久消失。
       * 收进来时按「本地已处理 id」过滤 —— 旧快照不许复活已处理记录。
       * 这一步会顺带把旧入口的派生清单收敛（收件箱覆盖的记录不重复出现在两边）。
       */
      this.adoptOrphanedTurns(orphanedTurns);
      this._syncInterruptedTurns();
    },
    /** 本地确认一条「未执行消息」已经结束（继续 / 忽略成功）：不允许快照把它复活。 */
    noteInterruptedTurnResolved(turnId: string) {
      if (!turnId || this._resolvedInterruptedTurnIds.includes(turnId)) return;
      this._resolvedInterruptedTurnIds.push(turnId);
      if (this._resolvedInterruptedTurnIds.length > 200) this._resolvedInterruptedTurnIds.shift();
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
     * 中止「正在准备附件」的那一次发送：**以服务端确认为准**（契约 §1.1）。
     *
     * 客户端 abort 不是证据 —— 只有后端的 cancelled / already_started 才算事实。
     * 返回 null 表示**拿不到确认**（请求失败）：调用方要保留原因与可用操作，
     * 不得提前宣称「已中止」。
     */
    async cancelPreparing(
      prepareId: string,
    ): Promise<{ state: "cancelled" | "already_started" | "unknown"; turnId: string | null } | null> {
      try {
        const res = await api.cancelPreparing(prepareId);
        if (res?.already_started) {
          return { state: "already_started", turnId: res.turn_id ?? null };
        }
        if (res?.cancelled) {
          return { state: "cancelled", turnId: res.turn_id ?? null };
        }
        return { state: "unknown", turnId: null };
      } catch (e) {
        this.lastError = `中止失败：${(e as Error).message}`;
        return null;
      }
    },
    /**
     * 按**取消确认返回的 turn_id** 停止（契约 §1.2）。
     *
     * 与 stopActiveTurn 的区别：目标来自后端确认，而不是「当前 active 轮」——
     * 另一轮在跑时用 activeTurnId 会**停错对象**。
     * 返回后端事实：cancelled=false 表示这一轮已经没有可取消的目标（例如已经结束）。
     * 返回 null 表示请求失败（拿不到事实）。
     */
    async stopTurnById(turnId: string): Promise<{ ok: boolean; cancelled: boolean } | null> {
      try {
        const res = await api.cancelTurn(turnId);
        return { ok: !!res?.ok, cancelled: !!res?.cancelled };
      } catch (e) {
        this.lastError = `停止失败：${(e as Error).message}`;
        return null;
      }
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
     * 按**回答身份**取校准目标（契约 K2.2）：身份就是流式 delta_id。
     *
     * 有 answer_id 时只认这一条；找不到就是「缺失」——调用方据此什么都不做
     * （不猜、不新建）。turnId 为空（旧后端）时不按轮过滤。
     */
    _answerByDeltaId(turnId: string, deltaId: string): StreamMessage | null {
      const id = String(deltaId ?? "").trim();
      if (!id) return null;
      for (let i = this.messages.length - 1; i >= 0; i -= 1) {
        const m = this.messages[i];
        if (!m || m.role !== "assistant") continue;
        if (m.assistantDeltaId !== id) continue;
        if (turnId && m.turnId !== turnId) continue;
        return m;
      }
      return null;
    },
    /**
     * 该轮**最后一条「已作为正式回答发布」**的消息（interim === false）。
     *
     * 这是旧事件（没有 answer_id）的校准目标；**不再无条件回落到中间话** ——
     * 中间话不是回答（旧实现把最后一条中间话提升成正式回答，等于把过程说明当结论）。
     * 唯一例外：旧协议（没有 delta_id）里本轮只有一条助手消息时可就地提升，
     * 见 _legacySoleAssistant。
     */
    _lastFormalAnswer(turnId: string): StreamMessage | null {
      for (let i = this.messages.length - 1; i >= 0; i -= 1) {
        const m = this.messages[i];
        if (!m || m.role !== "assistant") continue;
        if (turnId && m.turnId !== turnId) continue;
        if (m.interim !== true) return m;
      }
      return null;
    },
    /**
     * 旧协议（没有 delta_id）里「这一轮只有一条助手消息」的那一条：
     * 它是这次回答的唯一候选，TURN_END 可就地把它提升为正式回答（§2.1 的既有语义）。
     * 有任何 delta_id / 有多条助手消息时返回 null —— 不在无法确定身份时猜。
     */
    _legacySoleAssistant(turnId: string): StreamMessage | null {
      let found: StreamMessage | null = null;
      let count = 0;
      for (const m of this.messages) {
        if (!m || m.role !== "assistant") continue;
        if (turnId && m.turnId !== turnId) continue;
        count += 1;
        if (count > 1) return null;
        found = m;
      }
      if (!found || found.assistantDeltaId) return null;
      return found.interim === true ? found : null;
    },
    /** 就地校准一条回答：正文（null = 不动）、注记、核对结论；绝不重启动画 / 改身份 */
    _applyAnswerCalibration(
      message: StreamMessage,
      body: string | null,
      note: string,
      verification: unknown,
    ) {
      if (body !== null) message.content = body;
      message.interim = false;
      this._attachVerification(message, verification);
      if (note) message.content = appendSystemAnnotation(message.content, note);
    },
    /**
     * TURN_END 的最终正文校准（契约 K2.2，**禁止文字相似度 / 前缀判定**）。
     *
     * 目标身份：
     * * 有 `answer_id`（= 目标回答的 delta_id）→ 只更新该 turn 内这一条；
     *   找不到该身份 = 按缺失处理（不猜、不新建、不动任何正文）；
     * * 没有 `answer_id`（旧事件）→ 该 turn 内**最后一条**正式回答（interim === false）；
     *   该 turn 没有正式回答时：旧协议单条消息就地提升（_legacySoleAssistant），
     *   否则在 final_content 非空时新建一条。
     *
     * 正文语义：
     * * `undefined` / `null`（字段缺省）= **不校准**正文（注记 / 核对结论仍可挂到目标上）；
     * * 显式空串 = 清空目标正文（消息与注记保留）；
     * * 其它一律**覆盖**为目标正文（长 -> 短不再被忽略，「12 -> 13」不会多出一条）。
     *
     * 校准只改目标那一条：不新建第二条正式回答、不重启动画、不改变身份、
     * 不把正式回答移进过程区；重复 TURN_END / 重复 delta 幂等。
     */
    applyFinalAnswer(
      text?: string | null,
      verification?: unknown,
      opts: {
        turnId?: string | null;
        annotation?: string | null;
        /** 目标回答的 delta_id（TURN_END.answer_id；旧生产端没有这个字段） */
        answerId?: string | null;
      } = {},
    ) {
      const turnId = String(opts.turnId ?? this.activeTurnId ?? "");
      const hasFinal = text !== undefined && text !== null;
      const raw = hasFinal ? String(text) : "";
      const split = splitSystemAnnotation(raw);
      const separateNote = typeof opts.annotation === "string" ? opts.annotation.trim() : "";
      const note = separateNote || split.annotation || "";
      const body = split.annotation ? split.body : raw;
      const wantedId = String(opts.answerId ?? "").trim();

      // 1) 有身份：只认这一条；缺失 = 什么都不做（不猜）
      if (wantedId) {
        const target = this._answerByDeltaId(turnId, wantedId);
        if (!target) return;
        this._applyAnswerCalibration(target, hasFinal ? body : null, note, verification);
        return;
      }

      // 2) 旧事件：该 turn 最后一条正式回答
      const lastFormal = this._lastFormalAnswer(turnId);
      if (lastFormal) {
        this._applyAnswerCalibration(lastFormal, hasFinal ? body : null, note, verification);
        return;
      }

      /**
       * 3) 该 turn 没有正式回答。
       *
       * 旧协议（没有 delta_id 的整段推送）里「这一轮的整个回答只有一条助手消息」时，
       * TURN_END 的最终正文**就地更新那一条**（interim -> 正式回答的提升，
       * 契约 §2.1）；否则按 K2 新建 —— 多条 / 有身份时无法确定身份，绝不猜。
       */
      const legacySole = hasFinal ? this._legacySoleAssistant(turnId) : null;
      if (legacySole) {
        this._applyAnswerCalibration(legacySole, body, note, verification);
        return;
      }
      // 4) 新建：只有确实给了非空 final_content 才新建（显式空串没有可清空的目标）
      if (!hasFinal || !body.trim()) return;
      this.pushMessage({
        role: "assistant",
        content: body,
        contentType: "text",
        interim: false,
        ...(turnId ? { turnId } : {}),
      });
      const added = this.messages[this.messages.length - 1];
      if (added) this._applyAnswerCalibration(added, null, note, verification);
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
      const factsStatus = String(d.status ?? "completed");
      const factsReasonCode = text(d.reason_code);
      const nextFacts: TurnFacts = {
          turnId,
          status: factsStatus,
          durationMs: num(d.duration_ms),
          queueMs: num(d.queue_ms),
          startedAt: typeof d.started_at === "string" ? d.started_at : null,
          endedAt: typeof d.ended_at === "string" ? d.ended_at : null,
          // 结束原因：只写后端真实给过的字段；旧记录没有 → null，界面不编造
          reason: text(d.reason),
          reasonCode: factsReasonCode,
          stoppedBy: d.stopped_by === "user" || d.stopped_by === "system" ? d.stopped_by : null,
          // 只列后端说「当前确实可用」的操作（未知 / 重复 / 非字符串丢弃），
          // 再按终态做防御性归一（K3.4：cancelled + resend -> retry）
          actions: normalizeTurnActionsForStatus(
            factsStatus,
            factsReasonCode,
            normalizeTurnActions(d.actions),
          ),
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
        this.applyTurnFactsSnapshot(ctx.turn_facts);
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
      /**
       * 系统核对注记（R5 / K2.3）：**服务端字段优先** ——
       * raw.annotation 存在时用它；只有字段缺失才回落到正文里的旧内联表头。
       * 两者并存时不再渲染第二份（字段优先，正文侧的内联表头按表头切掉）。
       * 统一成与实时一致的内部表示：content = 正文 + 带表头的注记，渲染层再拆成
       * 独立的「系统事实」区域（正文 DOM 一次、注记 DOM 一次）。
       */
      const parsedRaw = m.role === "assistant" ? parseAssistantRaw(m.raw) : null;
      let content = m.content;
      if (parsedRaw?.annotation) {
        const inline = splitSystemAnnotation(m.content);
        content = appendSystemAnnotation(
          inline.annotation ? inline.body : m.content,
          parsedRaw.annotation,
        );
      }
      return {
        id: m.id,
        role: m.role as StreamMessage["role"],
        content,
        contentType: m.content_type,
        createdAt: m.created_at,
        topicName: this.topicName,
        fresh: false,
        ...(m.turn_id ? { turnId: String(m.turn_id) } : {}),
        ...(m.role === "tool"
          ? { toolName: "tool", toolOk: true, toolStatus: "success" as const, toolError: null }
          : {}),
        ...(parsedRaw?.verified ? { verified: parsedRaw.verified } : {}),
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
        this.applyTurnFactsSnapshot(page.turn_facts);
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
     * R6：这一轮已经**成功重发** → 本机留痕不得再暴露 `resend`，并把它从「未完成」入口清掉。
     *
     * 为什么必须做：成功后后端台账已经把这一轮领走（再点 /resend 必然 409），
     * 但过程区渲染读的是**本机留痕**（qio.turnFacts）—— 不清就会留下一个点不通的死按钮。
     * 后端读时投影由 W4 修；这里只收口本机这一份（刷新后一律以后端事实为准）。
     * 只有真正成功才收口：失败（409 / 网络）保留留痕与入口，可重试。
     */
    markTurnResendConsumed(turnId: string): void {
      if (!turnId) return;
      const facts = this.turnFacts[turnId];
      if (facts && facts.actions.includes("resend")) {
        this.turnFacts = {
          ...this.turnFacts,
          [turnId]: { ...facts, actions: facts.actions.filter((action) => action !== "resend") },
        };
        persistTurnFactsCache(this.turnFacts);
      }
      this.interruptedTurns = this.interruptedTurns.filter((t) => t.turn_id !== turnId);
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
        // R6：成功才收口本机留痕与「未完成」入口（失败路径不动，保留可重试）
        this.markTurnResendConsumed(turnId);
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
     *
     * 生命周期（契约 5）：每次发送领一个请求身份 client_request_id（重试复用）。
     * * 200 回执 → attempt 单向落定 accepted；**绝不**在这里写运行态 ——
     *   回执可能比 SSE 晚到，这轮可能已经开始甚至结束（反例 A）。
     * * 明确 4xx → confirmed-rejected：撤回乐观消息、恢复阅读位置，
     *   文案是「发送失败：被拒绝」。
     * * 超时 / 网络失败 → needs-confirm「正在确认」：不撤消息、不动运行态、
     *   不自动重发；延迟数百毫秒后用同一个 id 查证（by-request 端点）。
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
        /** 准备标识（契约 §1.1）：中止时用它调取消端点，**以后端确认为准** */
        prepareId?: string;
      } = {},
    ): Promise<boolean> {
      const message = text.trim();
      if (!message) return false;
      const ids = (attachmentIds ?? []).map((x) => String(x)).filter(Boolean);
      // 新的尝试开始：上一次的附件拒绝原因不再挂到这一次上
      this.lastSendRejection = null;
      const queued = this.turnRunning;
      /**
       * M01：起点身份在**提交这一刻**定死，跟着这条消息走。
       * 受理之后再发生的导航（浏览星球、改选片段、进入别的话题）都不会
       * 重新计算它 —— 排队中的消息更不会被追溯改向。
       */
      const startIdentity = this.captureStartIdentity();
      this.pushUser(message, ids, attachments ?? []);
      const optimistic = this.messages[this.messages.length - 1];
      optimistic.startIdentity = startIdentity;
      if (queued) {
        optimistic.queued = true;
        this.queuedMessageIds.push(optimistic.id);
      } else {
        this.turnStarted();
      }
      // 本机发送：允许把消息流拉回底部跟随（用户刚写完，想看结果）
      this.localSendSeq += 1;
      // 登记发送动作：同一动作（含之后的所有重试）只用这一个请求身份
      this.sendAttempts = [
        ...this.sendAttempts.slice(-49),
        {
          messageId: optimistic.id,
          clientRequestId: newClientRequestId(),
          state: "pending",
          message,
          topicId: this.currentTopicId,
          queued,
        },
      ];
      const attempt = this._attemptOf(optimistic.id);
      if (!attempt) return false;
      // 一轮新的发送开始：上一条「正在确认」的提示让位（旧 attempt 状态保留，
      // 它自己的查证定时器不受影响）
      this.sendConfirm = null;
      try {
          setNextSendRequestId(attempt.clientRequestId);
        /**
         * 附件显式绑定（契约 §1.4）：**无条件**带第三个参数 —— 空数组也是
         * 「这条消息没有附件」的显式语义。不传参数 = 缺字段 = 旧客户端，
         * 后端会走兜底把该话题下遗留的未绑定附件绑上（审计问题 3）。
         */
          // 起点身份（M01）在提交那一刻定死：一律用 startIdentity.topicId，而不是
          // 「此刻的 currentTopicId」—— 排队中的消息不随后续导航改向。
        // 非重试路径保持既有三参形状；只有重试/重发才带第四个参数（retry_of_turn_id）
        // 调用形状按需最小化：没有 signal 时保持既有参数个数（3 参 / 重试 4 参），
        // 免得把「准备期中止」的管道塞进所有调用点的既有契约里。
        let res;
        if (options.prepareId) {
          // 带准备标识：多一个参数（契约 §1.1 的取消端点要靠它定位这次准备）
          res = await api.sendTurn(
            message,
            startIdentity.topicId,
            ids,
            options.retryOfTurnId,
            options.signal,
            options.prepareId,
          );
        } else if (options.retryOfTurnId) {
          res = options.signal
            ? await api.sendTurn(
                message,
                startIdentity.topicId,
                ids,
                options.retryOfTurnId,
                options.signal,
              )
            : await api.sendTurn(message, startIdentity.topicId, ids, options.retryOfTurnId);
        } else {
          res = options.signal
            ? await api.sendTurn(message, startIdentity.topicId, ids, undefined, options.signal)
            : await api.sendTurn(message, startIdentity.topicId, ids);
        }
        if (res && res.cancelled) {
          /**
           * 服务端确认：这一轮**没有被受理执行**（用户中止 / 连接断开，契约 §1.1）。
           * 撤掉乐观消息、回到发送前的样子 —— 绝不能显示成「已发送」。
           */
          this.messages = this.messages.filter((m) => m.id !== optimistic.id);
          this.queuedMessageIds = this.queuedMessageIds.filter((id) => id !== optimistic.id);
          if (!queued) this.turnRunning = false;
          return false;
        }
        // 受理 ≠ 开始执行：SEND 只告诉我们「后端收下了这条消息」。
        // 回执只把发送动作单向推进到 accepted（_acceptSendReceipt），
        // 绝不在这里写 turnRunning / turnPhase / activeTurnId —— 那会把已经结束的轮次
        // 拉回「运行中」，也会把真正在跑那一轮的 TURN_END 丢掉。
        this._acceptSendReceipt(attempt.messageId, res);
        /**
         * 核对受理结果与提交时捕获的起点是否一致（M01）。
         * 不一致时**不追改** captured 身份、也不重发：只如实提示 —— 起点错位
         * 属于必须让用户看见的事实，不能被静默吞掉。后端没给 topic_id 时跳过核对。
         */
        const acceptedTopic = res?.topic_id ?? null;
        if (acceptedTopic && startIdentity.topicId && acceptedTopic !== startIdentity.topicId) {
          this.warning =
            `这条消息提交时属于「${startIdentity.topicName || startIdentity.topicId}」，` +
            "后端受理到的起点与提交时不一致；请确认当前起点后再发送下一条。";
        }
        // 以受理回执为准：没绑上的附件不得再显示为已带上（契约 §1.2）
        this._applySendReceipt(optimistic, ids, res);
        return true;
      } catch (e) {
        // 附件被拒（结构化失败）：留下可恢复信息，输入区据此给出逐条原因与出口
        this.lastSendRejection = attachmentRejectionFrom(e);
        /**
         * 用户自己中止了这次带附件的发送（准备期 abort）：后端要么已确认取消，要么
         * 因客户端断开而 abandon 预留 —— 两种事实都是「这一轮没有被受理执行」。
         * 绝不能滑进「正在确认（可能已经发送）」：那会留下一条看起来发出去的消息，
         * 而用户明确按了中止（输入区已经如实显示「已中止」）。
         */
        if (options.signal?.aborted) {
          this._rejectAttempt(attempt.messageId, e);
          return false;
        }
        return this._onSendFailure(attempt.messageId, e);
      }
    },
    /**
     * 待确认下重试：复用**同一个** client_request_id 重新 POST（幂等）。
     * 绝不换 id 重发 —— 换了 id，后端就无法识别这是同一次发送动作。
     */
    async retrySendAttempt(messageId: string): Promise<boolean> {
      const attempt = this._attemptOf(messageId);
      if (!attempt || attempt.state !== "needs-confirm" || attempt.retryBusy) return false;
      attempt.retryBusy = true;
      try {
        setNextSendRequestId(attempt.clientRequestId);
        const res = await api.sendTurn(attempt.message, attempt.topicId);
        this._acceptSendReceipt(attempt.messageId, res);
        return (attempt.state as SendAttemptState) === "accepted";
      } catch (e) {
        if (isSendReceiptUnknown(e)) {
          // 还是没拿到回执：留在「正在确认」，稍后再查证一次
          attempt.state = "needs-confirm";
          this.sendConfirm = this._confirmingState(attempt.messageId);
          this._scheduleConfirmQuery(attempt.messageId);
          return false;
        }
        this._rejectAttempt(attempt.messageId, e);
        return false;
      } finally {
        attempt.retryBusy = false;
      }
    },
    /** 「正在确认」下再查一次（不重发；查询失败也可以反复查）。 */
    async recheckSendAttempt(messageId?: string): Promise<void> {
      const target = messageId
        ? this._attemptOf(messageId)
        : this.sendConfirm
          ? this._attemptOf(this.sendConfirm.messageId)
          : undefined;
      if (!target || target.state !== "needs-confirm") return;
      this._clearConfirmTimer(target.messageId);
      await this._runConfirmQuery(target.messageId);
    },
    /**
     * 「放弃」这条发送：只允许在后端确实没有它的记录时（sendConfirm.unknown）。
     * 查证曾命中已受理的（accepted）拒绝放弃 —— 那条消息已受理，只有「取消」可用。
     */
    abandonSendAttempt(messageId?: string): boolean {
      const target = messageId
        ? this._attemptOf(messageId)
        : this.sendConfirm
          ? this._attemptOf(this.sendConfirm.messageId)
          : undefined;
      if (!target || target.state !== "needs-confirm") return false;
      if (this.sendConfirm?.messageId === target.messageId && this.sendConfirm.accepted) {
        return false;
      }
      this._settleConfirm(target.messageId);
      target.state = "confirmed-rejected";
      // 撤回乐观消息 + 阅读位置放回发送前（与被拒绝同一通道）
      this.sendRejectedSeq += 1;
      this.messages = this.messages.filter((m) => m.id !== target.messageId);
      this.queuedMessageIds = this.queuedMessageIds.filter((id) => id !== target.messageId);
      // 恢复草稿（输入框为空才放回，不覆盖正在输入的内容）
      if (!this.draft.trim()) this.draft = target.message;
      this.lastError = SEND_CONFIRM_UNKNOWN_NOTICE;
      // 后端无这条记录、这次发送也从未被 TURN_START 认领 →
      // 乐观的「运行中」是站不住的（同 _runConfirmQuery 的收敛条件）
      if (
        !target.queued &&
        this.activeTurnId === null &&
        !this.startedRequestIds.includes(target.clientRequestId)
      ) {
        this.turnRunning = false;
        this.turnPhase = "idle";
      }
      return true;
    },
    /**
     * 「取消」一条已受理的发送（查证命中后不再提供「放弃」）。
     * 与停止按钮同一条通道：有 turn_id 精确取消，否则退化为取消后端 active。
     */
    async cancelConfirmedSend(messageId?: string): Promise<boolean> {
      const target = messageId
        ? this._attemptOf(messageId)
        : this.sendConfirm
          ? this._attemptOf(this.sendConfirm.messageId)
          : undefined;
      if (!target) return false;
      const confirmed =
        target.state === "accepted" ||
        (target.state === "needs-confirm" && this.sendConfirm?.accepted === true);
      if (!confirmed) return false;
      try {
        if (target.turnId) await api.cancelTurn(target.turnId);
        else await api.cancelActiveTurn();
        return true;
      } catch (e) {
        this.lastError = `取消失败：${(e as Error).message}`;
        return false;
      }
    },
    // -- 发送生命周期的内部收口 -----------------------------------------
    _attemptOf(messageId: string): SendAttempt | undefined {
      return this.sendAttempts.find((a) => a.messageId === messageId);
    },
    _confirmingState(messageId: string): SendConfirmState {
      const attempt = this._attemptOf(messageId);
      return {
        messageId,
        clientRequestId: attempt?.clientRequestId ?? "",
        notice: SEND_CONFIRM_QUERYING_NOTICE,
        unknown: false,
        accepted: false,
        busy: true,
      };
    },
    /**
     * 发送回执（HTTP 200，含幂等命中）：pending → accepted 的**单向**落定。
     *
     * 关键边界（反例 A）：受理 ≠ 开始执行 —— 这里**绝不**写 turnRunning /
     * turnPhase / activeTurnId。「开始」只认 TURN_START，「结束」只认 TURN_END；
     * 回执晚到时不得把已经结束的轮次拉回「运行中」，也不得把 generating
     * 拉回 waiting。
     */
    _acceptSendReceipt(messageId: string, res: Record<string, unknown> | null | undefined) {
      const attempt = this._attemptOf(messageId);
      if (!attempt) return;
      // 单向：accepted 是终点，confirmed-rejected 是另一个终点
      if (attempt.state === "accepted" || attempt.state === "confirmed-rejected") return;
      const turnId = res && typeof res.turn_id === "string" ? res.turn_id : "";
      if (turnId) {
        attempt.turnId = turnId;
        const msg = this.messages.find((m) => m.id === attempt.messageId);
        if (msg && !msg.turnId) msg.turnId = turnId;
      }
      attempt.state = "accepted";
      // 排队登记保持原语义：SEND 只把 turn 记为「已受理排队」，不写 active
      if (turnId && attempt.queued) this.markTurnQueued(turnId);
      // 幂等命中（deduplicated）也一样：这只是同一次发送的回执，
      // 不重复执行、不重复展示、不重复排队
      this._settleConfirm(attempt.messageId);
    },
    /** 发送失败的分流：没拿到回执 → 「正在确认」；明确拒绝 → 撤回 + 明确失败。 */
    _onSendFailure(messageId: string, e: unknown): boolean {
      const attempt = this._attemptOf(messageId);
      if (!attempt) return false;
      if (isSendReceiptUnknown(e)) {
        attempt.state = "needs-confirm";
        this.sendConfirm = this._confirmingState(attempt.messageId);
        this._scheduleConfirmQuery(attempt.messageId);
        return false;
      }
      this._rejectAttempt(attempt.messageId, e);
      return false;
    },
    /**
     * 明确拒绝（后端给了响应：4xx/5xx；或其它明确失败）：撤回乐观消息、
     * 恢复阅读位置。文案区分「明确失败：被拒绝」与「正在确认是否已发送」。
     */
    _rejectAttempt(messageId: string, e: unknown) {
      const attempt = this._attemptOf(messageId);
      this._settleConfirm(messageId);
      const status = sendErrorStatus(e);
      this.lastError =
        status !== null && status >= 400 && status < 500
          ? SEND_REJECTED_NOTICE
          : (e as Error).message;
      this.sendRejectedSeq += 1;
      this.messages = this.messages.filter((m) => m.id !== messageId);
      this.queuedMessageIds = this.queuedMessageIds.filter((id) => id !== messageId);
      if (attempt) {
        attempt.state = "confirmed-rejected";
        // 这次发送没有被受理：只有「不是排队」的失败才说明当前 active turn 没起来。
        // 排队请求失败不能把仍在运行的其他任务一起标记成已结束。
        if (!attempt.queued) this.turnRunning = false;
      }
    },
    _scheduleConfirmQuery(messageId: string) {
      this._clearConfirmTimer(messageId);
      // 裸 setTimeout（= window.setTimeout）：假定时器测试按 globalThis 补丁走
      const timer = setTimeout(() => {
        void this._runConfirmQuery(messageId);
      }, SEND_CONFIRM_QUERY_DELAY_MS);
      this._confirmTimers = { ...this._confirmTimers, [messageId]: timer };
    },
    _clearConfirmTimer(messageId: string) {
      const timer = this._confirmTimers[messageId];
      if (!timer) return;
      clearTimeout(timer);
      const next = { ...this._confirmTimers };
      delete next[messageId];
      this._confirmTimers = next;
    },
    _settleConfirm(messageId: string) {
      this._clearConfirmTimer(messageId);
      if (this.sendConfirm?.messageId === messageId) this.sendConfirm = null;
    },
    /**
     * 查证一次发送动作（GET /api/turns/by-request/{id}）：
     * 命中 → 采纳 turn 状态；404 → 「发送未确认」（绝不等于「未发送」）；
     * 查询失败 → 保持「正在确认」，可以再查一次。
     */
    async _runConfirmQuery(messageId: string): Promise<void> {
      const attempt = this._attemptOf(messageId);
      if (!attempt || attempt.state !== "needs-confirm" || attempt.confirmBusy) return;
      attempt.confirmBusy = true;
      this.sendConfirm = this._confirmingState(attempt.messageId);
      try {
        const res = await api.lookupTurnByRequest(attempt.clientRequestId);
        if (attempt.state !== "needs-confirm") return; // 期间已被回执/重试落定
        this._adoptConfirmedTurn(attempt.messageId, res);
      } catch (e) {
        if (attempt.state !== "needs-confirm") return;
        if (sendErrorStatus(e) === 404) {
          // 本进程没有这次请求的记录：如实呈现「未确认」；
          // 进程重启也会走到这里，所以它绝不等于「未发送」。
          this.sendConfirm = {
            messageId: attempt.messageId,
            clientRequestId: attempt.clientRequestId,
            notice: SEND_CONFIRM_UNKNOWN_NOTICE,
            unknown: true,
            accepted: false,
            busy: false,
          };
          // 后端明确没有这条记录、这次发送也从未被 TURN_START 认领 →
          // 乐观的「运行中」是站不住的：收敛，但不撤消息（用户还可以重试/放弃）。
          // 注意：后端重启会让记录丢失（404），但 TURN_START 可能已在重启前到过 ——
          // 用「request_id 是否已关联」判断，绝不误伤真实开始过的轮次。
          if (
            !attempt.queued &&
            this.activeTurnId === null &&
            !this.startedRequestIds.includes(attempt.clientRequestId)
          ) {
            this.turnRunning = false;
            this.turnPhase = "idle";
          }
        } else {
          // 查询失败：保持「正在确认」，可以再查一次（不自动重发）
          this.sendConfirm = {
            messageId: attempt.messageId,
            clientRequestId: attempt.clientRequestId,
            notice: SEND_CONFIRM_RETRY_NOTICE,
            unknown: false,
            accepted: false,
            busy: false,
          };
        }
      } finally {
        attempt.confirmBusy = false;
      }
    },
    /** 查证命中：采纳 turn 事实（幂等命中也一样），不重放执行、不写运行态。 */
    _adoptConfirmedTurn(messageId: string, res: Record<string, unknown> | null | undefined) {
      const attempt = this._attemptOf(messageId);
      if (!attempt || attempt.state !== "needs-confirm") return;
      const turnId = res && typeof res.turn_id === "string" ? res.turn_id : "";
      if (!turnId) {
        // 形状不对：当作这次查询失败处理，保持「正在确认」
        this.sendConfirm = {
          messageId: attempt.messageId,
          clientRequestId: attempt.clientRequestId,
          notice: SEND_CONFIRM_RETRY_NOTICE,
          unknown: false,
          accepted: false,
          busy: false,
        };
        return;
      }
      attempt.turnId = turnId;
      attempt.state = "accepted";
      const msg = this.messages.find((m) => m.id === attempt.messageId);
      if (msg && !msg.turnId) msg.turnId = turnId;
      if (String(res?.status ?? "") === "queued") this.markTurnQueued(turnId);
      this._settleConfirm(attempt.messageId);
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
