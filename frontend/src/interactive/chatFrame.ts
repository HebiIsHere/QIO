/**
 * ChatFrame —— 互动对话窗口的显示框类型（屏幕像素，左上角为视口原点）。
 *
 * 2026-10-26（合并后清理 · C2）：旧 ChatDock 的「按板面身份持久化对话框位置 / 尺寸」
 * （loadChatFrame / saveChatFrame / clearChatFrame / defaultChatFrame / convergeChatFrame /
 * chatFrameLimits / CHAT_FRAME_* / 本机 localStorage 键）已随旧组件删除 ——
 * v2 互动页的对话区不再是可拖动、可保存的浮窗，新版 InteractiveConversation 只把
 * **当前可见框**发布到 chatFrameState（BoardCanvas 用它算卡片可用区域）。
 * 这里只保留共用类型，供 chatFrameState 与消费方使用；不再有持久化入口。
 */
export interface ChatFrame {
  x: number;
  y: number;
  w: number;
  h: number;
}
