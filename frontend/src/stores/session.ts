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
}

export const useSessionStore = defineStore("session", {
  state: () => ({
    currentTopicId: null as string | null,
    anchorFragmentId: null as string | null,
    memoryStrength: 0.5,
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
    setAnchor(topicId: string, fragmentId?: string | null) {
      this.currentTopicId = topicId;
      this.anchorFragmentId = fragmentId ?? null;
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
    pushAssistant(text: string) {
      this.pushMessage({ role: "assistant", content: text, contentType: "text" });
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