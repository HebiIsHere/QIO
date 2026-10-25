/**
 * 互动页 v2 组件与纯函数的统一出口（Phase 1-B / Phase 3-B / Phase 3-C）。
 *
 * Lead 集成时二选一：
 * - 直接按路径 import（与项目现有习惯一致）；
 * - 或从本文件统一取：InteractiveWorkspace / InteractiveChromeTools / InteractiveConversation 等。
 */
export { default as InteractiveWorkspace } from "./InteractiveWorkspace.vue";
export { default as InteractiveChromeTools } from "./InteractiveChromeTools.vue";
export { default as InteractiveConversation } from "./InteractiveConversation.vue";
export { default as RetryPanel } from "./RetryPanel.vue";
export { default as RecoveryPanel } from "./RecoveryPanel.vue";
export { default as SaveFailureBanner } from "./SaveFailureBanner.vue";
export { default as ImpactConfirmLayer } from "./ImpactConfirmLayer.vue";
export { default as TaskCenterPanel } from "./TaskCenterPanel.vue";
export { useChromeExtraTarget } from "./chromeTarget";
export * from "./layout";
export * from "./chromePlacement";
export * from "./saveStatus";
export * from "./conversationLayout";
export * from "./recoverySummary";
export * from "./retryApi";
export * from "./intentDisplayState";
export { buildTaskRows, taskCounts, taskControlActions, taskEmptyText, type TaskRow } from "./taskCenter";
