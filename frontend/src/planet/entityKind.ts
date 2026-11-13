/**
 * 实体卡「类型」（kind）的人话名称。
 *
 * 后端 kind 是模型 / 系统写入的自由文本：既有中文（如「家人」），也有 onboarding
 * 写下的英文内部 slug（如 "person"）。管理页直接打印 slug 会让用户看到实现细节，
 * 所以在这里做**只读展示映射**：
 *
 * - 只映射本仓库代码里真实出现过的内部 slug（不臆造词表）；
 * - 未知值原样返回 —— 宁可显示用户/模型给的原词，也不猜一个含义不同的中文名；
 * - 不改写、不回写后端数据（编辑态始终编辑原值）。
 */
const ENTITY_KIND_LABELS: Record<string, string> = {
  // onboarding 的自我介绍卡固定写 kind="person"（services/onboarding.py）。
  person: "人物",
  // 实体提炼与测试数据里出现过的内部类型（extract / test_entity_cards）。
  animal: "动物",
  project: "项目",
};

/** 内部 kind slug → 可读中文名；未知/空值保留原值（空值返回空串）。 */
export function entityKindLabel(kind?: string | null): string {
  const raw = String(kind ?? "").trim();
  if (!raw) return "";
  return ENTITY_KIND_LABELS[raw.toLowerCase()] ?? raw;
}
