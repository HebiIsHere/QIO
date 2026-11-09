/**
 * 对话消息视口状态（跨路由卸载重建保留）。
 *
 * 为什么需要单独一个 store：
 * - 打开设置 / 星球会让对话页组件卸载重建，阅读位置、是否跟随、未读数量都必须活过
 *   这一次重建（见修复提示词 §二）；
 * - 「回到最新」入口已经并入输入区辅助入口行（§三），它和消息流是两个组件，
 *   需要同一份真实状态，不能各拿一份本地 ref 互相猜。
 *
 * 这里只存**阅读状态**，不复制消息、不改业务状态：
 * - positions 按话题保存稳定的阅读位置（轮次身份 + 轮内锚点 + 视口相对偏移 +
 *   文本片段 + 旧像素回退）；
 * - following / unseen 是当前话题的跟随与未读事实；
 * - backRequest 只是「用户点击回到最新」的请求序号，由消息流消费。
 */
import { defineStore } from "pinia";

export interface SavedReadingPosition {
  topicId: string | null;
  /** 轮次稳定身份：turn_id（新消息）或该轮首条消息 id（旧历史） */
  turnKey: string;
  /** 轮内锚定元素身份（msg:<id> / test:<name> / class:<name>）；没有可定位元素时 null */
  anchorKey: string | null;
  /** 锚定元素顶部相对滚动视口顶部的偏移（px，负数 = 锚点在视口上方） */
  anchorOffset: number;
  /** 轮次顶部相对滚动视口顶部的偏移（px；锚定元素缺失时的回退） */
  turnOffset: number;
  /** 长回答内部的文本锚点：视口顶部附近可见的文字片段 */
  textSnippet: string | null;
  /** 文本锚点所在元素顶部相对视口顶部的偏移（px） */
  textOffset: number | null;
  /** 旧像素值：只在没有稳定身份 / 环境量不到 DOM 时回退，不能作为唯一依据 */
  scrollTop: number;
  /** 离开时是否在跟随底部 */
  following: boolean;
  /** 保存时的消息总数与最后一条消息 id：计算离开期间新增消息数 */
  messageCount: number;
  lastMessageId: string | null;
  savedAt: number;
  /** 保存时的窗口宽度：恢复时若宽度变化很大，后续校正放宽但仍有界 */
  viewportWidth: number;
}

/** 最多保留多少个话题的阅读位置（有界，避免长会话无限增长）。 */
export const MAX_READING_POSITIONS = 20;

function topicKey(topicId: string | null): string {
  return topicId || "__default__";
}

export const useChatViewportStore = defineStore("chatViewport", {
  state: () => ({
    /** 当前话题（用于隔离恢复代次） */
    topicId: null as string | null,
    /** 当前是否跟随底部 */
    following: true,
    /** 上翻阅读期间新到的消息数（回到最新入口上的角标） */
    unseen: 0,
    /** 「回到最新」请求序号：入口点击一次 +1，消息流消费 */
    backRequest: 0,
    /** 按话题保存的阅读位置 */
    positions: {} as Record<string, SavedReadingPosition>,
  }),
  actions: {
    key(topicId: string | null): string {
      return topicKey(topicId);
    },
    setTopic(topicId: string | null): void {
      this.topicId = topicId;
    },
    setFollowing(following: boolean): void {
      this.following = following;
      if (following) this.unseen = 0;
    },
    setUnseen(count: number): void {
      this.unseen = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
    },
    savePosition(position: SavedReadingPosition): void {
      const key = topicKey(position.topicId);
      const next = { ...this.positions, [key]: position };
      const keys = Object.keys(next);
      if (keys.length > MAX_READING_POSITIONS) {
        keys
          .sort((a, b) => (next[a]?.savedAt ?? 0) - (next[b]?.savedAt ?? 0))
          .slice(0, keys.length - MAX_READING_POSITIONS)
          .forEach((stale) => {
            delete next[stale];
          });
      }
      this.positions = next;
    },
    getPosition(topicId: string | null): SavedReadingPosition | null {
      return this.positions[topicKey(topicId)] ?? null;
    },
    clearPosition(topicId: string | null): void {
      const key = topicKey(topicId);
      if (!(key in this.positions)) return;
      const next = { ...this.positions };
      delete next[key];
      this.positions = next;
    },
    /** 用户明确点击「回到最新」：只表达阅读意图，不取消接续选择、不改消息状态。 */
    requestBackToLatest(): void {
      this.backRequest += 1;
    },
  },
});
