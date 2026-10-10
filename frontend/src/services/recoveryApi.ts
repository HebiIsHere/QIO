/**
 * 「可恢复记录」接口（A01 + A03 的前端出口）。
 *
 * 为什么单独一个文件：后端把「上一次没执行完的用户消息」与「孤立重发、历史无归属、
 * 归属已死的派生任务」收敛成**一套**规则（见 `backend/services/recovery.py`，
 * 端点在 `api/recovery_routes.py`）。前端也只在**一个**清单里呈现这些事 ——
 * 分散到几个组件里就会出现「有的记录永远看不见」。
 *
 * 纪律：
 * * 失败**必须抛可读错误**（`ApiError` 已带状态码与路径），不静默吞；
 * * 只在服务端确认成功之后才改本地状态（调用方负责，见 stores/session.ts）；
 * * 不在这里猜「这条能不能继续」——`enabled` 是服务端给的判定，
 *   前端只如实呈现它和它给出的 `reason`。
 */
import { ApiError, api, type InterruptedTurn } from "./api";

/** 与后端 `RecoveryRecord.actions[].id` 一一对应。 */
export type RecoveryActionId =
  | "continue"
  | "repair"
  | "ignore"
  | "requeue"
  /** F01：确认「无归属记录的旧执行者已停止」——唯一的解锁入口。 */
  | "confirm_stopped";

/** 后端 `RecoveryRecord.kind`。 */
export type RecoveryKind = "user_turn" | "derived_task";

/**
 * 后端 `RecoveryRecord.state_class`。
 *
 * 前端**不**据此自己发明动作：它只用来给界面兜底文案（服务端没给 `actions` 的旧版
 * 情况下也要能说清「为什么现在动不了」）。
 */
export type RecoveryStateClass =
  | "ready"
  | "orphaned_claim"
  | "legacy_unowned"
  | "owner_unknown"
  | "derived_stale"
  | "derived_legacy"
  /** F03：后继已落库、派发没做成 —— 可原地重试的明确状态。 */
  | "dispatch_failed"
  | (string & {});

/** 后台执行单元的归属状态。`unknown` **不是**死（不许当死处理）。 */
export type RecoveryOwnerState = "alive" | "dead" | "unknown" | "none" | (string & {});

/** 服务端给的一个动作：能不能点、为什么不能点，都由它说了算。 */
export interface RecoveryActionView {
  id: RecoveryActionId | (string & {});
  label: string;
  enabled: boolean;
  reason: string;
}

/** 一条可恢复记录（服务端形状，见契约 §2.1）。 */
export interface RecoveryRecordView {
  record_id: string;
  kind: RecoveryKind | (string & {});
  state_class: RecoveryStateClass;
  /** 持久状态原文：queued / running / interrupted / pending / … */
  status: string;
  message: string | null;
  topic_id: string | null;
  reason: string | null;
  created_at: string;
  updated_at: string | null;
  owner_instance_id: string | null;
  owner_state: RecoveryOwnerState;
  /** 服务端给的一句话（例如「无法确认上次的写入者是否已停止」） */
  owner_note: string;
  claim_generation: number | null;
  attempts: number | null;
  last_error: string | null;
  /** F01：这条记录的「旧执行者已停止」是否已被用户显式确认过（旧后端没有这个字段）。 */
  confirmed_stopped?: boolean;
  actions: RecoveryActionView[];
}

/** 一次列举的结果；`total > shown` 时界面必须说出「还有 N 条未显示」。 */
export interface RecoveryListingView {
  records: RecoveryRecordView[];
  total: number;
  shown: number;
  truncated: boolean;
}

/** `/api/runtime/state` 里的 `orphaned_turns`：与 `interrupted_turns` 同一形状。 */
export type OrphanedTurn = InterruptedTurn;

export interface RecoveryListParams {
  limit?: number;
  kinds?: string[];
  classes?: string[];
}

/** 继续一条记录时要带的「我看到的是这个版本」——服务端据此挡并发与陈旧点击。 */
export interface ExpectedRecord {
  expected_class: string;
  expected_status?: string;
}

export interface ContinueRecoveryResult {
  ok: boolean;
  record_id: string;
  turn_id: string;
  status: string;
}

export interface RepairRecoveryResult {
  ok: boolean;
  repaired: boolean;
  record_id: string;
  reason?: string;
}

export interface IgnoreRecoveryResult {
  ok: boolean;
  ignored: boolean;
  record_id?: string;
}

export interface RequeueDerivedResult {
  ok: boolean;
  record_id?: string;
  state: string;
}

/** 服务端 409：这条已经被处理过 / 版本已经不是我看到的那一份。 */
export function isRecoveryConflict(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409;
}

function jsonBody(payload: Record<string, unknown>): RequestInit {
  return { method: "POST", body: JSON.stringify(payload) };
}

function requireRecordId(recordId: string): string {
  const id = String(recordId ?? "").trim();
  if (!id) throw new Error("这条记录没有标识，无法继续处理（请刷新后重试）");
  return id;
}

/** 一条记录在列举结果里被无条件要求的字段（形状不对就是坏响应，不猜）。 */
function normalizeRecord(raw: unknown): RecoveryRecordView {
  const row = (raw ?? {}) as Record<string, unknown>;
  const recordId = String(row.record_id ?? row.turn_id ?? row.id ?? "");
  if (!recordId) throw new Error("服务端返回了一条没有标识的可恢复记录");
  const actions = Array.isArray(row.actions) ? row.actions : [];
  return {
    record_id: recordId,
    kind: String(row.kind ?? "user_turn"),
    state_class: String(row.state_class ?? "ready"),
    status: String(row.status ?? ""),
    message: row.message === undefined || row.message === null ? null : String(row.message),
    topic_id: row.topic_id === undefined || row.topic_id === null ? null : String(row.topic_id),
    reason: row.reason === undefined || row.reason === null ? null : String(row.reason),
    created_at: String(row.created_at ?? ""),
    updated_at:
      row.updated_at === undefined || row.updated_at === null ? null : String(row.updated_at),
    owner_instance_id:
      row.owner_instance_id === undefined || row.owner_instance_id === null
        ? null
        : String(row.owner_instance_id),
    owner_state: String(row.owner_state ?? "none"),
    owner_note: String(row.owner_note ?? ""),
    claim_generation:
      typeof row.claim_generation === "number" ? row.claim_generation : null,
    attempts: typeof row.attempts === "number" ? row.attempts : null,
    last_error:
      row.last_error === undefined || row.last_error === null ? null : String(row.last_error),
    // 旧后端没有这个字段时按「未确认」处理：拿不到证据就不放行。
    confirmed_stopped: row.confirmed_stopped === true,
    actions: actions.map((item) => {
      const a = (item ?? {}) as Record<string, unknown>;
      return {
        id: String(a.id ?? ""),
        label: String(a.label ?? ""),
        enabled: a.enabled !== false,
        reason: String(a.reason ?? ""),
      };
    }),
  };
}

/**
 * `GET /api/recovery/records`：收件箱清单。
 *
 * 读数失败**抛错**：担心的是「拉不到」被界面说成「没有未完成的事」。
 */
export async function fetchRecoveryRecords(
  params: RecoveryListParams = {},
): Promise<RecoveryListingView> {
  const query = new URLSearchParams();
  if (typeof params.limit === "number" && Number.isFinite(params.limit)) {
    query.set("limit", String(Math.max(1, Math.trunc(params.limit))));
  }
  if (params.kinds?.length) query.set("kinds", params.kinds.join(","));
  if (params.classes?.length) query.set("classes", params.classes.join(","));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const raw = await api.getRecoveryRecords(suffix);
  const records = Array.isArray(raw?.records) ? raw.records.map(normalizeRecord) : [];
  const shown = typeof raw?.shown === "number" ? raw.shown : records.length;
  const total = typeof raw?.total === "number" ? raw.total : records.length;
  return {
    records,
    shown,
    total,
    // 服务端说截断就截断；它没说但条数对不上时也如实说「还有未显示的」
    truncated: raw?.truncated === true || total > shown,
  };
}

/** `POST /api/recovery/records/{id}/continue`：接管 + 沿用既有可靠重发规则。 */
export function continueRecovery(
  recordId: string,
  expected: ExpectedRecord,
): Promise<ContinueRecoveryResult> {
  return api.continueRecovery(requireRecordId(recordId), {
    expected_class: expected.expected_class,
    expected_status: expected.expected_status,
  });
}

/** `POST /api/recovery/records/{id}/repair`：把孤立抢占修回「可继续」。 */
export function repairOrphan(recordId: string, expectedClass = "orphaned_claim"): Promise<RepairRecoveryResult> {
  return api.repairOrphanRecord(requireRecordId(recordId), { expected_class: expectedClass });
}

export interface ConfirmStoppedResult {
  ok: boolean;
  confirmed: boolean;
  already_confirmed: boolean;
  confirmed_at: string;
  record_id: string;
  kind: string;
}

/**
 * F01：`POST /api/recovery/records/{id}/confirm-stopped`。
 *
 * 只对**当前确实无归属**的记录生效（有归属的由服务端拒绝）——前端不自己判断。
 */
export function confirmStopped(
  recordId: string,
  expectedClass = "",
): Promise<ConfirmStoppedResult> {
  return api.confirmStoppedRecord(
    requireRecordId(recordId),
    expectedClass ? { expected_class: expectedClass } : {},
  );
}

/** `POST /api/recovery/records/{id}/ignore`：用户已知晓，原文与记录都保留。 */
export function ignoreRecovery(
  recordId: string,
  expectedClass: string,
): Promise<IgnoreRecoveryResult> {
  return api.ignoreRecoveryRecord(requireRecordId(recordId), { expected_class: expectedClass });
}

/** `POST /api/recovery/records/{id}/requeue`：把归属已死的派生任务放回待执行。 */
export function requeueDerived(
  recordId: string,
  expected: { expected_state: string; expected_generation: number | null },
): Promise<RequeueDerivedResult> {
  return api.requeueDerivedRecord(requireRecordId(recordId), {
    expected_state: expected.expected_state,
    expected_generation: expected.expected_generation,
  });
}
