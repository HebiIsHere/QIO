/**
 * 首次引导的六步元数据（spec 2026-08-18-onboarding-design §2）。
 * 顺序即向导顺序：欢迎 → 连接模型 → 认识你 → 偏好 → 目标 → 完成。
 */
export type StepKey =
  | "welcome"
  | "credential"
  | "profile"
  | "preference"
  | "goal"
  | "followup"
  | "review";

export const ONBOARDING_STEPS: { key: StepKey; title: string }[] = [
  { key: "welcome", title: "欢迎" },
  { key: "credential", title: "连接模型" },
  { key: "profile", title: "认识你" },
  { key: "preference", title: "偏好" },
  { key: "goal", title: "目标" },
  { key: "followup", title: "追问" },
  { key: "review", title: "核对并完成" },
];

/** 偏好四个维度：每一项都可以单独指定"只在某个话题里生效"。 */
export const PREFERENCE_DIMENSIONS = [
  { key: "verbosity", label: "详略", options: ["简洁", "详细"] },
  { key: "tone", label: "语气", options: ["平实", "轻松", "俏皮"] },
  { key: "explanation", label: "解释方式", options: ["先讲逻辑再给结论", "先给结论再讲逻辑", "直接给做法"] },
  { key: "collaboration", label: "协作方式", options: ["先问我再动手", "先给方案让我选", "直接动手"] },
];

/** 目标改成自由填写，不再给固定选项。 */
export const GOAL_EXAMPLES = ["把 QIO 的记忆问题做完"];
