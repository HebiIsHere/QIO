/**
 * 执行记录的可读展示映射（#69）：**纯逻辑**，不发请求、不读全局状态。
 *
 * 用户可见处只给「人话」：操作 / 状态 / 授权范围 / 确认材料 / 结果类型；
 * 未知或未登记的值给可读回退，并在诊断里保留原始枚举与结构数据。
 * 原始 ID、内部枚举、authorization 等结构数据只进「诊断」折叠区。
 *
 * 硬约束：任何对象值都不能被直接插值成 [object Object]；
 * 这里所有 `unknown` 入参都要先经过 `readableValue` / `diagnosticJson`。
 *
 * 操作名称与后端 `agent/tools/display.py`、账本 action 枚举
 * （`create_card` / `update_card` / `add_text_card` …）保持同源口径；
 * 新增动作时在这里补一条，未知动作保留原文回退。
 */
import type { IntentBasisRefView, IntentScopeView } from "./intentWorkbench";

/** 一行「可读字段 → 可读值」；模板只消费它，不直接插值原始结构。 */
export interface ReadableLine {
  label: string;
  value: string;
}

/** 操作（工具 / 账本 action 枚举）→ 中文名。 */
export const ACTION_LABELS: Readonly<Record<string, string>> = {
  // 板面工具（与 backend/src/agent/tools/display.py 同源）
  board_inspect: "查看互动板面",
  board_read_cards: "读取板面卡片",
  board_propose_intents: "提出板面意图",
  board_update_intent: "更新板面意图",
  board_wait_for_input: "等待补充信息",
  board_create_card: "创建成果卡片",
  board_update_card: "修改卡片",
  board_move_card: "移动卡片",
  board_group_cards: "分组卡片",
  board_order_group: "调整组内顺序",
  board_add_link: "添加卡片关系",
  board_finish_intent: "结束板面意图",
  // 权威账本 / 执行操作枚举
  create_card: "创建卡片",
  update_card: "修改卡片",
  move_card: "移动卡片",
  group_cards: "卡片分组",
  order_group: "调整组内顺序",
  add_link: "建立卡片关系",
  add_reply_card: "生成结果卡片",
  add_text_card: "生成文字卡片",
};

/** 账本操作状态（intent_service.OP_*）→ 中文名。 */
export const OPERATION_STATUS_LABELS: Readonly<Record<string, string>> = {
  applied: "已生效",
  running: "执行中",
  pending: "未开始",
  failed: "失败",
  conflict: "冲突",
};

/** 重试系列状态（board_intent_retries.status）→ 中文名。 */
export const RETRY_STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: "等待自动重试",
  running: "重试进行中",
  stopped: "重试已停止",
  exhausted: "重试次数已用尽",
  completed: "重试已完成",
};

/** 确认材料的用途（IntentBasisRef.role）→ 中文名。 */
export const BASIS_ROLE_LABELS: Readonly<Record<string, string>> = {
  source: "来源",
  target: "目标",
  context: "上下文",
};

/** 结果卡类型（CardKind）→ 中文名。 */
export const RESULT_KIND_LABELS: Readonly<Record<string, string>> = {
  reply: "回复卡片",
  text: "文字卡片",
  file: "文件卡片",
  code: "代码卡片",
  url: "网址卡片",
};

/** 授权确认人 → 可读名称；未知值原样回退。 */
const CONFIRMED_BY_LABELS: Readonly<Record<string, string>> = {
  user: "用户",
  system: "系统",
  qio: "QIO",
};

/** 授权来源 → 可读名称；未知值原样回退。 */
const AUTHORIZATION_SOURCE_LABELS: Readonly<Record<string, string>> = {
  explicit_user_confirmation: "用户显式确认",
};

const SUMMARY_KEYS = ["title", "name", "label", "summary", "text", "content"] as const;
const BODY_KEYS = ["text", "content", "summary", "body", "description"] as const;

function rawText(value: unknown): string {
  if (typeof value === "string") return value.trim();
  return readableValue(value).trim();
}

/**
 * 任意值 → 可读文本（绝不返回 [object Object]）。
 *
 * - 字符串 / 数字 / 布尔原样；
 * - 数组 → 「共 N 项」；
 * - 对象 → 优先常见的标题/正文键，否则给截断的 JSON 摘要；
 * - null / undefined / 函数 → 空串（调用方自行给「未记录」回退）。
 */
export function readableValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return String(value);
  }
  if (typeof value === "symbol") return value.toString();
  if (typeof value === "function") return "";
  if (Array.isArray(value)) return value.length ? "共 " + value.length + " 项" : "";
  const record = value as Record<string, unknown>;
  for (const key of SUMMARY_KEYS) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  try {
    const json = JSON.stringify(value);
    if (typeof json === "string" && json && json !== "{}") {
      return json.length > 200 ? json.slice(0, 200) + "…" : json;
    }
  } catch {
    // 循环引用等：下面给可读回退，绝不把对象直接插值。
  }
  return "结构化数据";
}

/** 操作名：内部枚举 → 中文；未登记保留原文；缺失说「操作未记录」。 */
export function actionLabel(action: unknown): string {
  const raw = rawText(action);
  if (!raw) return "操作未记录";
  return ACTION_LABELS[raw] ?? "未登记操作（" + raw + "）";
}

/** 账本操作状态：内部枚举 → 中文；未登记保留原文；缺失不冒充状态。 */
export function operationStatusLabel(status: unknown): string {
  const raw = rawText(status);
  if (!raw) return "状态未记录";
  return OPERATION_STATUS_LABELS[raw] ?? "未登记状态（" + raw + "）";
}

/** 重试系列状态 → 中文；未登记保留原文。 */
export function retryStatusLabel(status: unknown): string {
  const raw = rawText(status);
  if (!raw) return "重试状态未记录";
  return RETRY_STATUS_LABELS[raw] ?? "未登记重试状态（" + raw + "）";
}

/** 确认材料用途 → 中文；未登记保留原文。 */
export function basisRoleLabel(role: unknown): string {
  const raw = rawText(role);
  if (!raw) return "用途未记录";
  return BASIS_ROLE_LABELS[raw] ?? "未登记用途（" + raw + "）";
}

/** 结果卡类型 → 中文；未登记保留原文。 */
export function resultKindLabel(kind: unknown): string {
  const raw = rawText(kind);
  if (!raw) return "结果";
  return RESULT_KIND_LABELS[raw] ?? "未登记类型（" + raw + "）";
}

/**
 * 确认材料的可读汇总（数量 + 用途）：原始 card_id 不在这里出现，只进诊断。
 * 没有材料时如实说「没有记录确认材料」，不编造。
 */
export function basisSummaryLine(basis: IntentBasisRefView[] | null | undefined): string {
  const items = Array.isArray(basis) ? basis : [];
  if (!items.length) return "没有记录确认材料。";
  const counts = new Map<string, number>();
  for (const item of items) {
    const label = basisRoleLabel(item?.role);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  // 已知用途按固定顺序在前，未登记用途原样随后，读起来稳定。
  const order = [BASIS_ROLE_LABELS.source, BASIS_ROLE_LABELS.target, BASIS_ROLE_LABELS.context];
  const parts: string[] = [];
  for (const label of order) {
    const count = counts.get(label) ?? 0;
    if (count) {
      parts.push(label + " " + count);
      counts.delete(label);
    }
  }
  for (const [label, count] of counts) parts.push(label + " " + count);
  return "共 " + items.length + " 份材料：" + parts.join("、");
}

/** 授权范围的可读字段（数量 + 名称）；原始 ID / 枚举只进诊断。 */
export function scopeLines(scope: IntentScopeView | null | undefined): ReadableLine[] {
  if (!scope || typeof scope !== "object") return [{ label: "授权范围", value: "未记录" }];
  const lines: ReadableLine[] = [];
  const idLine = (label: string, ids: string[] | undefined, unit: string): void => {
    lines.push({
      label,
      value: Array.isArray(ids) ? ids.length + " " + unit : "未记录",
    });
  };
  idLine("可读卡片", scope.read_card_ids, "张");
  idLine("可写卡片", scope.write_card_ids, "张");
  idLine("分组", scope.group_ids, "个");
  const actions = Array.isArray(scope.allowed_actions) ? scope.allowed_actions : undefined;
  if (!actions) {
    lines.push({ label: "允许操作", value: "未记录" });
  } else if (!actions.length) {
    lines.push({ label: "允许操作", value: "0 类" });
  } else {
    lines.push({
      label: "允许操作",
      value: actions.length + " 类：" + actions.map((action) => actionLabel(action)).join("、"),
    });
  }
  const created: string[] = [];
  if (scope.max_new_cards) created.push("卡片 " + scope.max_new_cards + " 张");
  if (scope.max_new_groups) created.push("分组 " + scope.max_new_groups + " 个");
  if (scope.max_new_links) created.push("关系 " + scope.max_new_links + " 条");
  lines.push({ label: "新建上限", value: created.length ? created.join(" · ") : "未记录" });
  return lines;
}

/** 授权范围的一句话摘要（页面抬头用）；缺失说「范围未记录」。 */
export function scopeSummary(scope: IntentScopeView | null | undefined): string {
  if (!scope || typeof scope !== "object") return "范围未记录";
  const parts: string[] = [];
  if (scope.read_card_ids?.length) parts.push("读取 " + scope.read_card_ids.length + " 张卡");
  if (scope.write_card_ids?.length) parts.push("修改 " + scope.write_card_ids.length + " 张卡");
  if (scope.group_ids?.length) parts.push("组 " + scope.group_ids.length + " 个");
  if (scope.allowed_actions?.length) parts.push("允许 " + scope.allowed_actions.length + " 类操作");
  const created =
    (scope.max_new_cards ?? 0) + (scope.max_new_groups ?? 0) + (scope.max_new_links ?? 0);
  if (created) parts.push("最多新建 " + created + " 个对象");
  return parts.length ? parts.join(" · ") : "范围未记录";
}

/**
 * 授权快照的可读字段。这里只放授权本身的元信息（版本 / 确认方式 / 时间 / 来源 /
 * 依赖 / 互斥）；完整的 authorization 结构数据只进诊断 JSON。
 */
export function authorizationLines(authorization: unknown): ReadableLine[] {
  if (
    !authorization ||
    typeof authorization !== "object" ||
    Array.isArray(authorization) ||
    !Object.keys(authorization as Record<string, unknown>).length
  ) {
    return [{ label: "授权快照", value: "未记录" }];
  }
  const record = authorization as Record<string, unknown>;
  const lines: ReadableLine[] = [];
  if ("version" in record) {
    lines.push({ label: "授权版本", value: readableValue(record.version) || "未记录" });
  }
  if ("confirmed_by" in record) {
    const raw = rawText(record.confirmed_by);
    lines.push({
      label: "确认方式",
      value: raw ? CONFIRMED_BY_LABELS[raw] ?? raw : "未记录",
    });
  }
  if ("confirmed_at" in record) {
    lines.push({ label: "确认时间", value: readableValue(record.confirmed_at) || "未记录" });
  }
  if ("source" in record) {
    const raw = rawText(record.source);
    lines.push({
      label: "授权来源",
      value: raw ? AUTHORIZATION_SOURCE_LABELS[raw] ?? raw : "未记录",
    });
  }
  if (Array.isArray(record.depends_on)) {
    lines.push({ label: "依赖", value: record.depends_on.length + " 项" });
  }
  if (Array.isArray(record.conflicts_with)) {
    lines.push({ label: "互斥", value: record.conflicts_with.length + " 项" });
  }
  return lines.length ? lines : [{ label: "授权快照", value: "未记录" }];
}

/** 原始结构 → 诊断区展示的 JSON；不可序列化时给诚实回退。 */
export function diagnosticJson(value: unknown): string {
  if (value === undefined) return "（未记录）";
  try {
    const json = JSON.stringify(value, null, 2);
    return typeof json === "string" ? json : "（不可序列化）";
  } catch {
    const fallback = readableValue(value);
    return fallback || "（不可序列化）";
  }
}

/**
 * 结果条目正文 → 可读文本 + 诊断 JSON。
 *
 * content 按契约是字符串，但旧数据 / 异常数据可能是对象：对象不直接插值，
 * 优先取「标题：正文」摘要；未知结构给「结构化内容（N 个字段）」，原文进诊断。
 */
export function resultContentDisplay(content: unknown): { text: string; json: string } {
  // 空正文在界面上是「未记录」，不是一份 JSON 的 null 字面量。
  if (content === null || content === undefined) return { text: "", json: "（未记录）" };
  const json = diagnosticJson(content);
  if (typeof content === "string") return { text: content, json };
  if (Array.isArray(content)) {
    return { text: content.length ? "共 " + content.length + " 项结构化内容" : "", json };
  }
  if (typeof content === "object") {
    const record = content as Record<string, unknown>;
    const title = typeof record.title === "string" ? record.title.trim() : "";
    let body = "";
    for (const key of BODY_KEYS) {
      const candidate = record[key];
      if (typeof candidate === "string" && candidate.trim()) {
        body = candidate.trim();
        break;
      }
    }
    let text = "";
    if (title && body) text = title + "：" + body;
    else if (body) text = body;
    else if (title) text = title;
    else text = "结构化内容（" + Object.keys(record).length + " 个字段）";
    return { text, json };
  }
  return { text: readableValue(content), json };
}
