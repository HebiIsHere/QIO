/**
 * 首次引导的六步元数据（spec 2026-08-18-onboarding-design §2）。
 * 顺序即向导顺序：欢迎 → 连接模型 → 认识你 → 偏好 → 目标 → 完成。
 */
export type StepKey = "welcome" | "credential" | "profile" | "preference" | "goal" | "done";

export const ONBOARDING_STEPS: { key: StepKey; title: string }[] = [
  { key: "welcome", title: "欢迎" },
  { key: "credential", title: "连接模型" },
  { key: "profile", title: "认识你" },
  { key: "preference", title: "偏好" },
  { key: "goal", title: "目标" },
  { key: "done", title: "完成" },
];

/** 「目标」步骤的可选项：勾选后生成种子话题（后端幂等建）。 */
export const GOAL_OPTIONS = ["学习", "写作", "编程", "生活", "陪聊"];

/** 回答风格：写进「我」实体卡的属性。 */
export const STYLE_OPTIONS = [
  { value: "简洁", label: "简洁" },
  { value: "详细", label: "详细" },
  { value: "俏皮", label: "俏皮" },
];

/** 标签候选维度（身份/职业/兴趣/习惯），用户填值后作为实体卡属性落库。 */
export const TAG_SUGGESTIONS = ["身份", "职业", "兴趣", "习惯"];
