import { defineStore } from "pinia";
import { api } from "../services/api";

export interface StreamMessage {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  contentType: string;
  createdAt: string;
  toolName?: string;
  toolOk?: boolean;
  toolError?: string | null;
  /** 记忆注入摘要（MEMORY_INJECT 事件附带，助手消息展示玫红虚线胶囊） */
  memoryInject?: { label: string } | null;
  /** 中间助手消息（工具调用前的可见评论，区别于最终答复） */
  interim?: boolean;
}

export const useSessionStore = defineStore("session", {
  state: () => ({
    currentTopicId: null as string | null,
    topicName: null as string | null,
    anchorFragmentId: null as string | null,
    anchorFragment: null as { id: string; title: string | null } | null,
    messages: [] as StreamMessage[],
    turnRunning: false,
    lastError: null as string | null,
    _msgSeq: 0,
  }),
  actions: {
    _nextId() {
      this._msgSeq += 1;
      return `local_${Date.now()}_${this._msgSeq}`;
    },
    /**
     * 设置锚点话题。name/fragment 可选：传入则刷新话题名与锚点片段，
     * 未传则保留现有值（向后兼容）。
     */
    setAnchor(
      topicId: string,
      fragmentId?: string | null,
      name?: string | null,
      fragment?: { id: string; title: string | null } | null,
    ) {
      this.currentTopicId = topicId;
      this.topicName = name !== undefined ? name : this.topicName;
      this.anchorFragmentId = fragmentId ?? null;
      this.anchorFragment = fragment !== undefined ? fragment : this.anchorFragment;
    },
    turnStarted() {
      this.turnRunning = true;
      this.lastError = null;
    },
    pushMessage(msg: Omit<StreamMessage, "id" | "createdAt">) {
      this.messages.push({ id: this._nextId(), createdAt: new Date().toISOString(), ...msg });
    },
    pushUser(text: string) {
      this.pushMessage({ role: "user", content: text, contentType: "text" });
    },
    pushAssistant(text: string, memoryInject?: StreamMessage["memoryInject"], interim = false) {
      this.pushMessage({
        role: "assistant",
        content: text,
        contentType: "text",
        memoryInject,
        ...(interim ? { interim: true } : {}),
      });
    },
    pushTool(name: string, ok: boolean, error: string | null, preview: string) {
      this.pushMessage({
        role: "tool",
        content: preview,
        contentType: "tool",
        toolName: name,
        toolOk: ok,
        toolError: error,
      });
    },
    turnEnded() {
      this.turnRunning = false;
    },
    async loadHistory() {
      try {
        const ctx = await api.getSessionContext();
        this.currentTopicId = ctx.topic_id;
        this.topicName = ctx.topic_name ?? null;
        this.anchorFragment = ctx.anchor_fragment ?? null;
        this.anchorFragmentId = ctx.anchor_fragment?.id ?? null;
        this.messages = ctx.messages.map((m) => ({
          id: m.id,
          role: m.role as StreamMessage["role"],
          content: m.content,
          contentType: m.content_type,
          createdAt: m.created_at,
          ...(m.role === "tool"
            ? { toolName: "tool", toolOk: true, toolError: null }
            : {}),
        }));
      } catch {
        // 后端未启动时保持空状态
      }
    },
    async send(text: string) {
      const message = text.trim();
      if (!message || this.turnRunning) return;
      this.pushUser(message);
      this.turnStarted();
      try {
        await api.sendTurn(message, this.currentTopicId);
      } catch (e) {
        this.lastError = (e as Error).message;
        this.turnRunning = false;
      }
    },
  },
});