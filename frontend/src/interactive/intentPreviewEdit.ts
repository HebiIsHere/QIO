/**
 * 预演编辑（Stage 2）：成组 / 连线 / 正文编辑的 operations 变换（纯函数）。
 *
 * 契约：docs/superpowers/specs/2026-10-21-intent-preview-apply-design.md §8（Stage 2 第 4 项）
 * 与 Lead 2026-10-21 冻结契约第 4 条：
 * - 成组：多选 >=2 张可编辑幽灵卡 → 追加 { op: "group_cards", ref: "group_<唯一>", cardIds }；
 * - 连线：恰好 2 张 → 追加 { op: "add_link", src, dst, meaning: "", direction: false }；
 * - 正文编辑：改对应 add 操作的 content（Markdown 原文）；空白正文**本地拦截**（服务端会拒）；
 * - 全部只改 operations，不动 preview / 板面；失败如实返回原因，绝不修改入参。
 *
 * 幽灵卡 ↔ add 操作沿用 intentPreviewApply 的同一映射口径
 * （服务端 execution.preview_from_operations：ref → cardId → p_<下标>）。
 */
import type { IntentOperation } from "./types";
import { previewCardOperationIndex } from "./intentPreviewApply";

/** 单条意图的操作条数上限（与服务端 models 校验同口径）。 */
export const MAX_INTENT_OPERATIONS = 20;

export type PreviewPlanEdit =
  | { ok: true; operations: IntentOperation[] }
  | { ok: false; reason: string };

/**
 * 幽灵卡在 operations 里的**稳定引用**：add 操作的 ref（服务端预览与落地都用它），
 * 其次 cardId。没有稳定符号的 add 操作（服务端预览 id 是 p_<下标>）不能参与成组 / 连线：
 * 落地时服务端按 ref → 已分配对象 id 解析成员，p_<下标> 不在映射里 —— 与其提交一个
 * 注定落地失败的方案，不如当场如实说不能成组 / 连线（拖动 / 删除 / 改正文仍然可用）。
 */
export function previewCardRef(
  operations: IntentOperation[] | null | undefined,
  previewCardId: string,
): string | null {
  const list = operations ?? [];
  const index = previewCardOperationIndex(list, previewCardId);
  if (index < 0) return null;
  const operation = list[index];
  const ref = String(operation?.ref ?? operation?.cardId ?? "").trim();
  return ref || null;
}

/** 多选切换：加入 / 移除（保持点击顺序，界面按它决定成组 / 连线按钮）。 */
export function togglePreviewSelection(selected: string[], cardId: string): string[] {
  const current = Array.isArray(selected) ? selected : [];
  if (current.includes(cardId)) return current.filter((item) => item !== cardId);
  return [...current, cardId];
}

/** 生成一个在 operations 里没有被占用过的符号（默认 group_1、group_2 …）。 */
export function uniquePlanRef(
  operations: IntentOperation[] | null | undefined,
  prefix: string,
): string {
  const used = new Set<string>();
  for (const operation of operations ?? []) {
    const record = operation as unknown as Record<string, unknown>;
    for (const key of ["ref", "groupId", "linkId", "cardId", "id"]) {
      const value = String(record[key] ?? "").trim();
      if (value) used.add(value);
    }
  }
  let index = 1;
  while (used.has(prefix + index)) index += 1;
  return prefix + index;
}

function operationLimitReason(operations: IntentOperation[], adding: number): string {
  if (operations.length + adding <= MAX_INTENT_OPERATIONS) return "";
  return (
    "操作计划最多 " + MAX_INTENT_OPERATIONS + " 条：这次修改会让计划超限，没有提交。"
  );
}

function resolveCardRefs(
  operations: IntentOperation[],
  cardIds: string[],
): { ok: true; refs: string[] } | { ok: false; reason: string } {
  const refs: string[] = [];
  for (const cardId of cardIds) {
    const ref = previewCardRef(operations, cardId);
    if (!ref) {
      return {
        ok: false,
        reason:
          "选中的预演卡片里有一张没有稳定符号（ref）：不能参与成组 / 连线（落地时无法解析成员）。",
      };
    }
    refs.push(ref);
  }
  return { ok: true, refs };
}

function sameMembers(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const left = [...a].sort();
  const right = [...b].sort();
  return left.every((item, index) => item === right[index]);
}

/**
 * 选中的幽灵卡成组：追加一条 group_cards 操作（组名交给服务端默认名）。
 * 同一组卡片已经成过组时不重复追加（避免一条计划里出现两个完全相同的组）。
 */
export function groupPreviewCards(
  operations: IntentOperation[] | null | undefined,
  cardIds: string[],
): PreviewPlanEdit {
  const list = operations ?? [];
  const selected = (cardIds ?? []).filter((id) => String(id ?? "").trim());
  if (selected.length < 2) {
    return { ok: false, reason: "成组至少需要选中 2 张预演卡片。" };
  }
  const limit = operationLimitReason(list, 1);
  if (limit) return { ok: false, reason: limit };
  const resolved = resolveCardRefs(list, selected);
  if (!resolved.ok) return resolved;
  const duplicate = list.some(
    (operation) =>
      operation?.op === "group_cards" &&
      sameMembers((operation.cardIds ?? []).map(String), resolved.refs),
  );
  if (duplicate) {
    return { ok: false, reason: "这些卡片已经是同一个组了。" };
  }
  return {
    ok: true,
    operations: [
      ...list,
      {
        op: "group_cards",
        ref: uniquePlanRef(list, "group_"),
        cardIds: resolved.refs,
      },
    ],
  };
}

/**
 * 选中的 2 张幽灵卡连线：追加一条无方向的 add_link（含义留空，由用户之后写；
 * 方向只表示用户写明的方向，前端不替用户解释成因果 / 顺序）。
 * 同一对卡片之间已经有关系时不重复追加。
 */
export function linkPreviewCards(
  operations: IntentOperation[] | null | undefined,
  cardIds: string[],
): PreviewPlanEdit {
  const list = operations ?? [];
  const selected = (cardIds ?? []).filter((id) => String(id ?? "").trim());
  if (selected.length !== 2) {
    return { ok: false, reason: "连线需要恰好选中 2 张预演卡片。" };
  }
  const limit = operationLimitReason(list, 1);
  if (limit) return { ok: false, reason: limit };
  const resolved = resolveCardRefs(list, selected);
  if (!resolved.ok) return resolved;
  const src = resolved.refs[0];
  const dst = resolved.refs[1];
  const duplicate = list.some(
    (operation) =>
      operation?.op === "add_link" &&
      sameMembers(
        [String(operation.src ?? ""), String(operation.dst ?? "")],
        [src, dst],
      ),
  );
  if (duplicate) {
    return { ok: false, reason: "这两张预演卡片之间已经有关系了。" };
  }
  return {
    ok: true,
    operations: [
      ...list,
      { op: "add_link", src, dst, meaning: "", direction: false },
    ],
  };
}

/** 读取幽灵卡当前计划的正文（Markdown 原文）；找不到对应 add 操作返回 null。 */
export function readPreviewCardContent(
  operations: IntentOperation[] | null | undefined,
  previewCardId: string,
): string | null {
  const list = operations ?? [];
  const index = previewCardOperationIndex(list, previewCardId);
  if (index < 0) return null;
  return String(list[index]?.content ?? "");
}

/**
 * 编辑幽灵卡正文：只改对应 add 操作的 content。
 * 空白正文本地拦截（服务端校验会拒绝空正文：本阶段不把注定失败的请求发出去）。
 */
export function editPreviewCardContent(
  operations: IntentOperation[] | null | undefined,
  previewCardId: string,
  content: string,
): PreviewPlanEdit {
  const list = operations ?? [];
  const index = previewCardOperationIndex(list, previewCardId);
  if (index < 0) {
    return { ok: false, reason: "这张预演卡片没有对应的新增操作，不能编辑正文。" };
  }
  const text = String(content ?? "");
  if (!text.trim()) {
    return { ok: false, reason: "正文不能为空：服务端会拒绝空白正文，这次修改没有提交。" };
  }
  const next = list.slice();
  next[index] = { ...list[index], content: text };
  return { ok: true, operations: next };
}
