/**
 * 发送请求身份（client_request_id）。
 *
 * 后端契约（Lead 冻结，2026-10-09）：POST /api/turns 支持可选 client_request_id；
 * **同一发送动作的重试必须复用同一个 id**，后端据它保证幂等 —— 幂等命中也回 200，
 * 绝不产生第二次执行。
 *
 * 为什么用「一次性挂载」而不是给 sendTurn 加第三个参数：
 * 既有调用点（session.send）的调用形态 `api.sendTurn(message, topicId)` 被多处
 * 既有测试逐参数精确断言（toHaveBeenCalledWith），保持两参调用可以把本轮改动
 * 限定在 stores/session.ts + services/api.ts + 本模块的边界内。
 *
 * 用法：发送方在调用 api.sendTurn **之前** setNextSendRequestId(id)；sendTurn
 * 构造请求体时取走它（取走即清空，绝不会泄漏到之后的无关请求）。
 */

let pendingRequestId: string | null = null;

/** 为**下一次** sendTurn 调用附加请求身份（幂等键）。 */
export function setNextSendRequestId(id: string): void {
  pendingRequestId = id;
}

/** sendTurn 内部取走挂载的请求身份（一次性：取走即清空）。 */
export function takeNextSendRequestId(): string | null {
  const id = pendingRequestId;
  pendingRequestId = null;
  return id;
}

/** 生成一次发送动作的请求身份。优先 crypto.randomUUID（契约指定的形态）。 */
export function newClientRequestId(): string {
  const c = globalThis.crypto as { randomUUID?: () => string } | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  // 兜底（极老环境）：保证唯一性即可，形态不参与任何语义
  return `creq-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}
