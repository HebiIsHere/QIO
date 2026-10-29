/**
 * chatFrameState —— 对话窗口「当前显示框」的共享只读桥（互动模式契约 2026-10-13 · IRL §2）
 *
 * 为什么需要它：互动对话窗被展开 / 收起 / 调整高度之后，**别的浮层与板面可用区域要跟着避让**
 * （展开卡片与「添加」菜单的可用区域计算）。这些图层由不同组件渲染，又不该各自去问 DOM。
 * 于是由 v2/InteractiveConversation 在每次真正变化时**发布一次**自己可见内容的外接矩形，
 * 需要的人读这一份。
 *
 * 边界：
 * - 这是**显示态**，不是板面状态：不产生表达、不排队保存、不触发模型；
 * - 真实来源是对话窗里「消息面板 + 输入区」的实时 DOM 外接矩形（每次重新测量，不缓存、不持久化）；
 * - 收起 / 组件卸载时发布 `null`，让下游回落到「按默认几何计算」，不留幽灵矩形。
 */
import { ref, type Ref } from "vue";
import type { ChatFrame } from "./chatFrame";

const frame = ref<ChatFrame | null>(null);
const collapsed = ref(false);

/** 当前对话窗口显示框（屏幕像素，左上角为视口原点）；未挂载时为 null。 */
export const liveChatFrame: Ref<ChatFrame | null> = frame;

/** 对话窗口是否收起（收起时浮层不按它避让）。 */
export const liveChatCollapsed: Ref<boolean> = collapsed;

export function publishChatFrame(next: ChatFrame | null, isCollapsed = false): void {
  frame.value = next ? { ...next } : null;
  collapsed.value = Boolean(isCollapsed);
}

export function readChatFrame(): ChatFrame | null {
  return frame.value ? { ...frame.value } : null;
}

export function setChatCollapsed(isCollapsed: boolean): void {
  collapsed.value = Boolean(isCollapsed);
}

/**
 * 收起状态下浮层**不应**按它避让：返回 null 表示「这块空间是可用的」。
 *
 * 与 `readChatFrame` 一样返回**副本**：下游拿去做几何换算时常常会就地改几个字段，
 * 返回活引用会把这些改动写回发布源（E 复核 F4）。
 */
export function chatObstacleFrame(): ChatFrame | null {
  if (collapsed.value) return null;
  return frame.value ? { ...frame.value } : null;
}

export function resetChatFrameState(): void {
  frame.value = null;
  collapsed.value = false;
}
