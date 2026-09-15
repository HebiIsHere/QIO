/**
 * 星球「本次运行期」的浏览记忆。
 *
 * 只活在当前会话的内存里：不写 localStorage、不落盘、不进后端，刷新即回到默认。
 * 记录两件事：
 * - `browsedTopicId`：用户最后浏览的话题（星球选中态是唯一来源）；
 * - `anchorSignature`：上一次浏览时已生效的起点（话题 + 片段）。
 *
 * 重开星球的规则（任务05 E「返回聊天后再次打开时保留本次浏览状态」）：
 * - 起点没有变化 → 回到上次浏览的话题，不无条件跳回起点；
 * - 起点明确变化（换话题或换片段）→ 以新起点为准，浏览记忆不覆盖它。
 */
export const planetSession = {
  browsedTopicId: null as string | null,
  anchorSignature: null as string | null,
};

/** 起点的完整签名：同话题换片段也算「起点变化」 */
export function anchorSignatureOf(topicId: string | null | undefined, fragmentId: string | null | undefined): string | null {
  if (!topicId) return null;
  return `${topicId}|${fragmentId ?? ""}`;
}

export function resetPlanetSession() {
  planetSession.browsedTopicId = null;
  planetSession.anchorSignature = null;
}
