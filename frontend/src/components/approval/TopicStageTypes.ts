/**
 * 话题阶段 / 恢复 / 冲突的展示类型（P1R-D 组件共用）。
 *
 * 只描述组件消费的结构；权威定义在后端 `topic_operation.TopicPhase`，
 * 前端由 E 的 store 从 TOPIC_PHASE / 申请 payload 派生后传入，组件不做推断。
 */
export type TopicStageValue =
  | "decision_open"
  | "awaiting_confirmation"
  | "executing"
  | "context_preparing"
  | "answerable"
  | "recoverable_failure";

/** 回答准备失败等可恢复事实（E 的 topicRecoveryFor 派生）。 */
export interface TopicRecoveryView {
  recordId: string;
  message: string;
  retryable: boolean;
}

/**
 * 查重冲突事实：同一个 operation 的二次确认，不是第二份申请。
 * status 保留后端原值（不认识的值不猜语义，按「待确认」说明）。
 */
export interface TopicConflictView {
  status: string;
  existingTopicId?: string;
  existingTopicName?: string;
}
