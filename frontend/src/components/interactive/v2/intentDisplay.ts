/**
 * Phase 3-A：虚拟意图层的**纯逻辑**（显示位置 / 显式 ids / 确认结果 / 能力迁移）。
 *
 * 契约：docs/interactive-page-v2-contract.md §6.1 / §6.2 / §3（Phase 1-D 审计结论）。
 *
 * 边界：
 * - 不发请求、不读 store、不碰 DOM；只做「真实字段 → 可判定的结果」映射；
 * - 状态只映射服务端真实状态；**绝不说成「已完成」**，除非服务端真的返回 completed；
 * - needs_update / 未知状态 / 已开始的意图**不能确认**（不冒充可确认）；
 * - 确认结果逐项给出显式 intent_id；服务端没返回的项按「未返回结果」如实标注，不猜成功；
 * - 只有响应里真的带 turn_id / turnId 才显示执行轮次；没有就说「没有返回执行轮次」，不假装已开始。
 */
import {
  BATCH_CONFIRM_MIN,
  canRecover,
  dependencyText,
  isConfirmable,
  operationsSummaryText,
  scopeSummary,
  statusLabel,
  statusTone,
  type ConfirmItemResult,
  type ConfirmResponse,
  type IntentView,
  type RecoveryPreviewView,
  type RecoveryResultView,
  type StatusTone,
} from "../../../interactive/intentWorkbench";

// --- 显示位置（契约 §6.1：display 按 intent_id 绑定，与目标/授权版本分开） -----

export interface IntentDisplayPoint {
  x: number;
  y: number;
}

/** IntentView 的 Phase 3 追加字段（后端 D 并行；前端先做兼容读取，缺省好过猜）。 */
export type IntentViewWithDisplay = IntentView & {
  /** 稳定 intent_id 绑定的显示位置；旧数据缺省 null */
  display?: { x?: unknown; y?: unknown } | null;
  /** 契约 §6.2：确认/恢复响应给真实执行轮次（snake_case 与 camelCase 都兼容读取） */
  turn_id?: string | null;
  turnId?: string | null;
};

/** 虚拟卡默认尺寸（板面坐标；只用于命中与拖动边界，不是材料卡尺寸）。 */
export const DEFAULT_INTENT_CARD_SIZE = { w: 260, h: 150 } as const;
/** 没有持久化 display 时的确定性落位（同一列表顺序永远同一位置，不随机）。 */
export const DEFAULT_INTENT_DISPLAY_ORIGIN = { x: 80, y: 80 } as const;
export const DEFAULT_INTENT_DISPLAY_STEP = { x: 300, y: 200 } as const;
export const DEFAULT_INTENT_DISPLAY_COLUMNS = 3;

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 读取持久化的 display：宽高都必须有限，旧数据 / 坏数据一律 null（不猜、不补 0）。 */
export function readIntentDisplay(view: IntentViewWithDisplay | null | undefined): IntentDisplayPoint | null {
  const display = view?.display;
  if (!display || typeof display !== "object") return null;
  const x = finiteNumber(display.x);
  const y = finiteNumber(display.y);
  if (x === null || y === null) return null;
  return { x, y };
}

/** 后端还没持久化时的确定性落位：按列表顺序排成网格（同一输入永远同一位置）。 */
export function fallbackIntentDisplay(
  index: number,
  origin: IntentDisplayPoint = DEFAULT_INTENT_DISPLAY_ORIGIN,
  step: IntentDisplayPoint = DEFAULT_INTENT_DISPLAY_STEP,
  columns = DEFAULT_INTENT_DISPLAY_COLUMNS,
): IntentDisplayPoint {
  const safeIndex = Number.isFinite(index) && index >= 0 ? Math.floor(index) : 0;
  const safeColumns = Math.max(1, Math.floor(columns) || 1);
  return {
    x: origin.x + (safeIndex % safeColumns) * step.x,
    y: origin.y + Math.floor(safeIndex / safeColumns) * step.y,
  };
}

/** 一张意图这一帧画在哪：优先持久化 display（或外部覆盖），否则确定性落位。 */
export function resolveIntentDisplay(
  view: IntentViewWithDisplay,
  index: number,
  overrides?: Record<string, IntentDisplayPoint> | null,
): { point: IntentDisplayPoint; persisted: boolean } {
  const override = overrides?.[view.intent_id];
  if (override && Number.isFinite(override.x) && Number.isFinite(override.y)) {
    return { point: { x: override.x, y: override.y }, persisted: true };
  }
  const stored = readIntentDisplay(view);
  if (stored) return { point: stored, persisted: true };
  return { point: fallbackIntentDisplay(index), persisted: false };
}

/** 全量显示位置映射（layer 渲染用；同一 intent_id 只出现一次）。 */
export function intentDisplayMap(
  views: readonly IntentViewWithDisplay[],
  overrides?: Record<string, IntentDisplayPoint> | null,
): Record<string, IntentDisplayPoint> {
  const map: Record<string, IntentDisplayPoint> = {};
  const seen = new Set<string>();
  for (const [index, view] of (views ?? []).entries()) {
    const id = view?.intent_id;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    map[id] = resolveIntentDisplay(view, index, overrides).point;
  }
  return map;
}

/** 拖动后钳进板面范围：不把虚拟卡拖到看不见的地方，也不产生 NaN。 */
export function clampIntentDisplay(
  point: IntentDisplayPoint,
  bounds: { w: number; h: number },
  cardSize: { w: number; h: number } = DEFAULT_INTENT_CARD_SIZE,
): IntentDisplayPoint {
  const width = Math.max(0, finiteNumber(bounds?.w) ?? 0);
  const height = Math.max(0, finiteNumber(bounds?.h) ?? 0);
  const cardW = Math.max(1, finiteNumber(cardSize?.w) ?? DEFAULT_INTENT_CARD_SIZE.w);
  const cardH = Math.max(1, finiteNumber(cardSize?.h) ?? DEFAULT_INTENT_CARD_SIZE.h);
  const x = finiteNumber(point?.x) ?? 0;
  const y = finiteNumber(point?.y) ?? 0;
  const maxX = Math.max(0, width - cardW);
  const maxY = Math.max(0, height - cardH);
  return {
    x: Math.round(Math.max(0, Math.min(maxX, x))),
    y: Math.round(Math.max(0, Math.min(maxY, y))),
  };
}

/** 拖动中的显示位置：起点 + 屏幕位移 / 画布缩放，再钳进板面范围。 */
export function dragIntentDisplay(
  start: IntentDisplayPoint,
  screenDelta: { dx: number; dy: number },
  options: {
    bounds: { w: number; h: number };
    scale?: number;
    cardSize?: { w: number; h: number };
  },
): IntentDisplayPoint {
  const scaleRaw = finiteNumber(options?.scale) ?? 1;
  const scale = scaleRaw > 0 ? scaleRaw : 1;
  const dx = (finiteNumber(screenDelta?.dx) ?? 0) / scale;
  const dy = (finiteNumber(screenDelta?.dy) ?? 0) / scale;
  return clampIntentDisplay(
    { x: (finiteNumber(start?.x) ?? 0) + dx, y: (finiteNumber(start?.y) ?? 0) + dy },
    options?.bounds ?? { w: 0, h: 0 },
    options?.cardSize,
  );
}

export function displayEquals(a: IntentDisplayPoint | null, b: IntentDisplayPoint | null, epsilon = 0.5): boolean {
  if (!a || !b) return a === b;
  return Math.abs(a.x - b.x) <= epsilon && Math.abs(a.y - b.y) <= epsilon;
}

// --- 可确认性（needs_update / 未知状态 / 已开始都不能确认） ---------------------

export type ConfirmBlockReason =
  | "needs_update"
  | "not_proposed"
  | "unknown_status"
  | "already_started"
  | "missing_id"
  | null;

/** needs_update 是旧链「需要更新预览」的状态；新链视图也可能经 raw_legacy_status 带出来。 */
export function isNeedsUpdate(view: IntentView | null | undefined): boolean {
  if (!view) return false;
  const status = String(view.status ?? "");
  const legacy = String((view as { raw_legacy_status?: string }).raw_legacy_status ?? "");
  return status === "needs_update" || legacy === "needs_update";
}

/**
 * 为什么这一条不能确认（返回 null = 可以确认）。
 * 只有 proposed / awaiting_confirmation 可以确认；needs_update 必须先更新预览。
 */
export function confirmBlockReason(view: IntentView | null | undefined): ConfirmBlockReason {
  if (!view || typeof view.status !== "string") return "missing_id";
  if (!view.intent_id) return "missing_id";
  if (isNeedsUpdate(view)) return "needs_update";
  if (isConfirmable(view)) return null;
  if (view.status === "approved" || view.status === "running" || view.status === "paused") {
    return "already_started";
  }
  if (["completed", "partially_completed", "failed", "cancelled", "rejected"].includes(view.status)) {
    return "not_proposed";
  }
  return "unknown_status";
}

export function confirmBlockText(view: IntentView | null | undefined): string {
  switch (confirmBlockReason(view)) {
    case "needs_update":
      return "需要更新：这条意图的预览已过期，必须先重新理解 / 更新预览才能确认（不能当作可确认）。";
    case "already_started":
      return "已经开始过：不能重复确认（同一意图同一时刻只有一个有效执行）。";
    case "not_proposed":
      return "这条意图已经结束或已拒绝：不能再次确认。";
    case "unknown_status":
      return "状态未知：不能确认（不做猜测）。";
    case "missing_id":
      return "缺少意图身份：不能确认。";
    default:
      return "";
  }
}

/** 可选择的意图 ids：只含真正可确认的（explicit ids，顺序稳定）。 */
export function selectableIntentIds(views: readonly IntentView[]): string[] {
  return (views ?? []).filter((view) => view && view.intent_id && confirmBlockReason(view) === null).map((view) => view.intent_id);
}

/** 已选 ids 里真正能提交的（显式 ids ∩ 可确认；去重、保序、忽略未知 id）。 */
export function selectedConfirmIds(
  views: readonly IntentView[],
  selectedIds: readonly string[],
): string[] {
  const eligible = new Set(selectableIntentIds(views));
  const out: string[] = [];
  for (const id of selectedIds ?? []) {
    if (typeof id !== "string" || !id || !eligible.has(id) || out.includes(id)) continue;
    out.push(id);
  }
  return out;
}

export interface BatchConfirmState {
  ids: string[];
  count: number;
  enabled: boolean;
  hint: string;
}

/** 同批确认：至少两条（BATCH_CONFIRM_MIN，与后端同口径）；单条必须走单项确认。 */
export function batchConfirmState(
  views: readonly IntentView[],
  selectedIds: readonly string[],
): BatchConfirmState {
  const ids = selectedConfirmIds(views, selectedIds);
  const count = ids.length;
  const enabled = count >= BATCH_CONFIRM_MIN;
  let hint: string;
  if (count <= 0) {
    hint = "同批确认至少选择 " + BATCH_CONFIRM_MIN + " 条；只处理一条时请用单项确认。";
  } else if (!enabled) {
    hint =
      "已选 " + count + " 条：同批批量确认至少 " + BATCH_CONFIRM_MIN +
      " 条；单条请用该项自己的「确认」，这里不会提交。";
  } else {
    hint = "已选 " + count + " 条：可同批确认（依赖 / 冲突 / 授权仍由服务端逐项校验）。";
  }
  return { ids, count, enabled, hint };
}

// --- 确认结果：显式 ids / 部分失败分别可见 / 真实 turnId ----------------------

export type ConfirmItemWithTurn = ConfirmItemResult & {
  turn_id?: string | null;
  turnId?: string | null;
};
export type ConfirmResponseWithTurn = Omit<ConfirmResponse, "results"> & {
  results?: ConfirmItemWithTurn[];
  turn_id?: string | null;
  turnId?: string | null;
  resume?: boolean;
};

/** 读取真实执行轮次：逐项优先，其次响应级；没有就 null（绝不编造）。 */
export function readTurnId(source: ConfirmItemWithTurn | ConfirmResponseWithTurn | null | undefined): string | null {
  if (!source || typeof source !== "object") return null;
  const record = source as Record<string, unknown>;
  for (const key of ["turn_id", "turnId"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

export interface ConfirmRowView {
  intentId: string;
  /** 服务端是否给了这一项（没给 = 未返回结果，不是成功） */
  returned: boolean;
  ok: boolean;
  status: string;
  statusText: string;
  statusTone: StatusTone;
  reason: string;
  /** 真实执行轮次（只有响应里真的有值） */
  turnId: string | null;
  /** 只有服务端状态 completed 才算完成 */
  completed: boolean;
  /** ok 且没有 turnId、也没有 completed：不能声称「已开始」 */
  startedUnknown: boolean;
}

export interface ConfirmSummary {
  batch: boolean;
  rows: ConfirmRowView[];
  succeededIds: string[];
  failedIds: string[];
  completedIds: string[];
  /** ok 但没有执行轮次的 id：界面必须说明「不知道有没有开始」 */
  startedUnknownIds: string[];
  turnIds: Record<string, string>;
  /** 服务端响应级 turn_id（有些实现放在顶层） */
  responseTurnId: string | null;
  resume: boolean;
  /** 可直接展示的一句人话（绝不说成已完成） */
  notice: string;
}

/**
 * 把确认响应摊成逐项结果：**显式 ids** 是唯一的行来源。
 * 服务端没返回的 id 记为「未返回结果」，不按成功处理；部分失败逐项可见。
 */
export function summarizeConfirm(
  response: ConfirmResponseWithTurn | null | undefined,
  requestedIds: readonly string[],
  options: { batch?: boolean } = {},
): ConfirmSummary {
  const requested = [...new Set((requestedIds ?? []).filter((id): id is string => typeof id === "string" && id.length > 0))];
  const items = Array.isArray(response?.results) ? response!.results! : [];
  const byId = new Map<string, ConfirmItemWithTurn>();
  for (const item of items) {
    if (!item || typeof item.intent_id !== "string" || !item.intent_id) continue;
    if (!byId.has(item.intent_id)) byId.set(item.intent_id, item);
  }
  const responseTurnId = readTurnId(response);
  const batch = options.batch ?? Boolean(response?.batch);

  const rows: ConfirmRowView[] = requested.map((intentId) => {
    const item = byId.get(intentId);
    if (!item) {
      return {
        intentId,
        returned: false,
        ok: false,
        status: "",
        statusText: "状态未知",
        statusTone: "open",
        reason: "服务端没有返回这一项的结果：状态以意图列表为准，不按成功处理。",
        turnId: null,
        completed: false,
        startedUnknown: false,
      };
    }
    const status = String(item.status ?? "");
    const completed = status === "completed";
    const ok = item.ok === true;
    const turnId = readTurnId(item) ?? responseTurnId;
    return {
      intentId,
      returned: true,
      ok,
      status,
      statusText: statusLabel(status),
      statusTone: statusTone(status),
      reason: String(item.reason ?? "").trim(),
      turnId,
      completed,
      startedUnknown: ok && !turnId && !completed,
    };
  });

  const succeededIds = rows.filter((row) => row.returned && row.ok).map((row) => row.intentId);
  const failedIds = rows.filter((row) => !row.returned || !row.ok).map((row) => row.intentId);
  const completedIds = rows.filter((row) => row.completed).map((row) => row.intentId);
  const startedUnknownIds = rows.filter((row) => row.startedUnknown).map((row) => row.intentId);
  const turnIds: Record<string, string> = {};
  for (const row of rows) if (row.turnId) turnIds[row.intentId] = row.turnId;

  const parts: string[] = [];
  parts.push(batch ? "同批确认结果：" : "确认结果：");
  if (!items.length) parts.push("服务端没有返回逐项结果，状态以意图列表为准。");
  else {
    parts.push(
      rows
        .map((row) => {
          const status = row.returned ? row.statusText : "未返回结果";
          const reason = row.reason ? "（" + row.reason + "）" : "";
          const turn = row.turnId ? " · 执行轮次 " + row.turnId : "";
          return row.intentId + "：" + status + reason + turn;
        })
        .join("；") + "。",
    );
  }
  if (failedIds.length) {
    parts.push("部分失败：" + failedIds.join("、") + " 没有确认成功，可逐项重试或查看原因。");
  }
  if (startedUnknownIds.length) {
    parts.push(
      "已确认但没有执行轮次：" + startedUnknownIds.join("、") +
        " —— 不能认为已经开始执行（服务端没有返回 turn_id）。",
    );
  }
  if (completedIds.length) {
    parts.push("服务端已完成：" + completedIds.join("、") + "。");
  } else if (succeededIds.length) {
    parts.push("已确认不等于已完成：执行结果以服务端状态、账本与结果卡为准。");
  }

  return {
    batch,
    rows,
    succeededIds,
    failedIds,
    completedIds,
    startedUnknownIds,
    turnIds,
    responseTurnId,
    resume: response?.resume === true,
    notice: parts.join(""),
  };
}

// --- 能力迁移（Phase 1-D 审计：旧工作台 / BatchTray / StatusPopover 的有效能力） ----

export interface MigratedCapability {
  from: "IntentWorkbench" | "IntentBatchTray" | "IntentStatusPopover";
  capability: string;
  /** 迁移到本 Phase 3-A 的哪里（组件 / 纯函数 / 依赖服务端字段） */
  movedTo: string;
  /** 已迁移 / 依赖后端字段 / 不在本轮（如实标注） */
  status: "migrated" | "needs-backend" | "kept-legacy";
}

/**
 * 旧能力的去向表（测试守住「不能直接删、有效能力要有去处」）。
 * 注意：IntentBatchTray / IntentStatusPopover 的暂停 / 继续 / 取消与旧账本是**旧链接口**，
 * 新链需要后端同步提供；本轮如实标注为 needs-backend，不用前端 setTimeout 或假状态顶上。
 */
export const MIGRATED_CAPABILITIES: readonly MigratedCapability[] = [
  { from: "IntentWorkbench", capability: "目标 / 预期 / 依据 / 范围 / 版本", movedTo: "v2/IntentLayer 详情面板", status: "migrated" },
  { from: "IntentWorkbench", capability: "依赖 / 冲突", movedTo: "v2/IntentLayer 详情面板（dependencyText）", status: "migrated" },
  { from: "IntentWorkbench", capability: "操作账本摘要", movedTo: "v2/IntentLayer 详情面板（operationsSummaryText）", status: "migrated" },
  { from: "IntentWorkbench", capability: "单条确认", movedTo: "v2/IntentLayer 显式单 id 确认", status: "migrated" },
  { from: "IntentWorkbench", capability: "同批确认（≥2，显式 ids）", movedTo: "v2/IntentLayer 批量栏 + batchConfirmIntents", status: "migrated" },
  { from: "IntentWorkbench", capability: "等待事项 / 结果卡 / 恢复预览", movedTo: "IntentWorkbench 保留（新链服务不变）+ v2/IntentLayer 恢复入口", status: "kept-legacy" },
  { from: "IntentBatchTray", capability: "批次列表 / 同批批准 / 拒绝 / 执行摘要", movedTo: "批量确认走 v2/IntentLayer；拒绝与旧批次台账留在旧链（不冒充新链）", status: "kept-legacy" },
  { from: "IntentBatchTray", capability: "依赖 / 冲突提示", movedTo: "v2/IntentLayer 详情（dependencyText + conflicts）", status: "migrated" },
  { from: "IntentStatusPopover", capability: "真实执行状态 / 定位预览", movedTo: "v2/IntentLayer 状态标签 + 定位（Lead 接线 move 事件）；旧浮层保留", status: "migrated" },
  { from: "IntentStatusPopover", capability: "暂停 / 继续 / 取消执行", movedTo: "新链需后端执行控制接口（本轮不伪造）", status: "needs-backend" },
  { from: "IntentStatusPopover", capability: "失败重试读取", movedTo: "v2/IntentLayer 恢复预览 + 诚实失败原因（自动重试由后端调度）", status: "migrated" },
];

/** 详情面板里的能力行：依赖 / 冲突 / 账本 / 范围 / 授权 / 恢复。 */
export interface CapabilityRow {
  key: "scope" | "basis" | "dependencies" | "operations" | "authorization" | "recovery" | "outcome";
  label: string;
  text: string;
  tone: StatusTone;
}

export function capabilityRows(view: IntentViewWithDisplay): CapabilityRow[] {
  const rows: CapabilityRow[] = [{ key: "scope", label: "范围", text: scopeSummary(view.scope), tone: "open" }];
  if (view.basis?.length) {
    rows.push({
      key: "basis",
      label: "依据",
      text: view.basis.map((item) => item.card_id + " · v" + item.version).join("、"),
      tone: "open",
    });
  }
  const dependency = dependencyText(view);
  if (dependency) rows.push({ key: "dependencies", label: "依赖 / 冲突", text: dependency, tone: "waiting" });
  const operations = operationsSummaryText(view);
  if (operations) rows.push({ key: "operations", label: "操作账本", text: operations, tone: "active" });
  if (view.authorization_valid === false) {
    rows.push({ key: "authorization", label: "授权", text: "旧授权已失效：需要重新确认当前目标与影响范围。", tone: "problem" });
  }
  if (canRecover(view)) {
    rows.push({ key: "recovery", label: "恢复", text: "可进入恢复（只把意图放回可继续状态，不自动执行操作）。", tone: "waiting" });
  }
  if (view.summary) rows.push({ key: "outcome", label: "说明", text: view.summary, tone: statusTone(view.status) });
  return rows;
}

// --- 恢复（能力迁移：旧「恢复预览」入口保持同一套真实条件） ----------------------

export interface RecoveryFacts {
  available: boolean;
  canResume: boolean;
  reasons: string[];
  operations: string[];
  dependencies: string[];
  text: string;
}

/** 恢复预览 → 可读事实；不可用时如实说明，不臆造可恢复结论。 */
export function recoveryFacts(
  preview: RecoveryPreviewView | null | undefined,
  available = true,
): RecoveryFacts {
  if (!preview) {
    return {
      available: false,
      canResume: false,
      reasons: [],
      operations: [],
      dependencies: [],
      text: available ? "服务端没有返回恢复预览，不臆造可恢复结论。" : "恢复预览不可用。",
    };
  }
  const mapItems = (items: unknown[] | undefined): string[] =>
    (items ?? []).map((item) => {
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        const key = record.operation_key ?? record.operation_id ?? record.action ?? record.intent_id ?? record.id;
        const status = record.status ? "（" + String(record.status) + "）" : "";
        return (key ? String(key) : "未标注项") + status;
      }
      return String(item);
    });
  const reasons = (preview.reasons ?? []).filter((reason) => typeof reason === "string" && reason.trim());
  const operations = mapItems(preview.unfinished_operations);
  const dependencies = mapItems(preview.unfinished_dependencies);
  const canResume = preview.can_resume === true;
  const parts: string[] = [];
  parts.push(canResume ? "可恢复" : "不可恢复（不会写入）");
  if (reasons.length) parts.push("原因：" + reasons.join("；"));
  if (operations.length) parts.push("未完成操作：" + operations.join("、"));
  if (dependencies.length) parts.push("未完成依赖：" + dependencies.join("、"));
  return { available: true, canResume, reasons, operations, dependencies, text: parts.join("；") };
}

/** 恢复结果的人话：只说「恢复已受理 / 没有恢复」，不说成「已执行完」。 */
export function recoveryResultText(result: RecoveryResultView | null | undefined): string {
  if (!result) return "恢复没有返回结果。";
  const status = statusLabel(result.status);
  if (!result.ok) {
    return "恢复没有完成：" + ((result.reason ?? "").trim() || "服务端未说明原因") + "（状态 " + status + "）。";
  }
  return "恢复已受理：当前状态「" + status + "」；不会自动执行操作，后续由服务端续作（不代表已完成）。";
}
