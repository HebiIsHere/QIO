/**
 * 互动页 v2 组件与布局纯函数的统一出口（Phase 1-B / Phase 3-B）。
 *
 * Lead 集成时二选一：
 * - 直接按路径 import（与项目现有习惯一致）；
 * - 或从本文件统一取：InteractiveWorkspace / InteractiveChromeTools / InteractiveConversation 等。
 */
export { default as InteractiveWorkspace } from "./InteractiveWorkspace.vue";
export { default as InteractiveChromeTools } from "./InteractiveChromeTools.vue";
export { default as InteractiveConversation } from "./InteractiveConversation.vue";
export { useChromeExtraTarget } from "./chromeTarget";
export * from "./layout";
export * from "./chromePlacement";
export * from "./saveStatus";
export * from "./conversationLayout";
