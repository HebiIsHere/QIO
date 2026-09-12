import { defineStore } from "pinia";
import { api } from "../services/api";

export interface ToolPresentation {
  title?: string;
  status?: string;
  summary?: string;
  /** 原始工具名（后端附带）：只在悬停提示 / 开发者排查时使用 */
  tool?: string;
}

export interface QueueItem {
  turn_id: string;
  message: string;
}

export interface TurnQueueState {
  running: QueueItem | null;
  queued: QueueItem[];
  cancelled: QueueItem[];
}

export interface StreamMessage {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  contentType: string;
  createdAt: string;
  toolName?: string;
  toolOk?: boolean;
  toolError?: string | null;
  /** 工具卡呈现（present_call/present_result 合并结果，缺省回退默认模板） */
  presentation?: ToolPresentation | null;
  /** 记忆注入摘要（MEMORY_INJECT 事件附带，助手消息展示玫红虚线胶囊） */
  memoryInject?: { label: string } | null;
  /** 中间助手消息（工具调用前的可见评论，区别于最终答复） */
  interim?: boolean;
  /** 正在流式输出（打字机逐字）的消息；落定后为 undefined */
  streaming?: boolean;
  /** 消息产生时所属话题名（快照，避免切换话题后显示串） */
  topicName?: string | null;
  /** 该消息所属 turn_id（SSE 事件归属） */
  turnId?: string | null;
  /** 主 turn 运行中提交、等待执行的消息（TURN_START 时按 FIFO 清除） */
  queued?: boolean;
}

export const useSessionStore = defineStore("session", {
  state: () => ({
    currentTopicId: null as string | null,
    topicName: null as string | null,
    anchorFragmentId: null as string | null,
    anchorFragment: null as { id: string; title: string | null } | null,
    /**
     * 当前位置是否是「历史位置」（用户从这里继续 / Agent 显式 continue 选中的历史片段）。
     * 成功一轮后后端把位置推进到当前片段 → historic=false，UI 的「从…继续」提示随之消失。
     */
    anchorHistoric: false,
    messages: [] as StreamMessage[],
    turnRunning: false,
    lastError: null as string | null,
    /** 非致命警告（WARNING 事件）；不代表 turn 结束，TURN_START 清空 */
    warning: null as string | null,
    /** 正在「取消中」的 turn_id（停止按钮反馈，避免假装已停止） */
    cancelling: null as string | null,
    /** 本地排队中的用户消息 id（FIFO；TURN_START 到来时清除最早的一条） */
    queuedMessageIds: [] as string[],
    /** 当前 active turn 的 id（TURN_START 记录，TURN_END 清除） */
    activeTurnId: null as string | null,
    /** 主 turn 队列快照（TURN_QUEUE 事件更新）：运行中 + 排队中 */
    turnQueue: { running: null, queued: [], cancelled: [] } as TurnQueueState,
    /** 迭代/输出预算耗尽，等待用户决定是否继续 */
    pendingContinue: null as { id: string; used: number; max: number } | null,
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
      historic?: boolean,
    ) {
      this.currentTopicId = topicId;
      this.topicName = name !== undefined ? name : this.topicName;
      this.anchorFragmentId = fragmentId ?? null;
      this.anchorFragment = fragment !== undefined ? fragment : this.anchorFragment;
      if (historic !== undefined) this.anchorHistoric = historic;
    },
    turnStarted() {
      this.turnRunning = true;
      this.lastError = null;
      this.warning = null;
      this.cancelling = null;
      // 队列中的最早一条开始执行：清除「等待中」标记（后端 TURN_QUEUE 事件负责其余展示）
      const nextQueued = this.queuedMessageIds.shift();
      if (nextQueued) {
        const msg = this.messages.find((m) => m.id === nextQueued);
        if (msg) msg.queued = false;
      }
    },
    pushMessage(msg: Omit<StreamMessage, "id" | "createdAt">) {
      this.messages.push({
        id: this._nextId(),
        createdAt: new Date().toISOString(),
        topicName: this.topicName,
        turnId: this.activeTurnId,
        ...msg,
      });
    },
    pushUser(text: string) {
      this.pushMessage({ role: "user", content: text, contentType: "text" });
    },
    pushAssistant(
      text: string,
      memoryInject?: StreamMessage["memoryInject"],
      interim = false,
      streaming = false,
    ) {
      // 若本轮正在产出且最后一条是流式助手消息，则就地更新（避免"过程+最终"两条）
      const last = this.messages[this.messages.length - 1];
      if (streaming && last && last.role === "assistant" && last.streaming) {
        last.content = text;
        last.memoryInject = memoryInject ?? last.memoryInject;
        last.interim = true;
        return;
      }
      this.pushMessage({
        role: "assistant",
        content: text,
        contentType: "text",
        memoryInject,
        ...(interim ? { interim: true } : {}),
        ...(streaming ? { streaming: true } : {}),
      });
    },
    finalizeAssistant() {
      const last = this.messages[this.messages.length - 1];
      if (last && last.role === "assistant" && last.streaming) {
        delete last.streaming;
        last.interim = false;
      }
    },
    pushTool(
      name: string,
      ok: boolean,
      error: string | null,
      preview: string,
      presentation?: ToolPresentation | null,
    ) {
      this.pushMessage({
        role: "tool",
        content: preview,
        contentType: "tool",
        toolName: name,
        toolOk: ok,
        toolError: error,
        presentation,
      });
    },
    turnEnded() {
      this.turnRunning = false;
      this.cancelling = null;
    },
    async loadHistory() {
      try {
        const ctx = await api.getSessionContext();
        this.currentTopicId = ctx.topic_id;
        this.topicName = ctx.topic_name ?? null;
        this.anchorFragment = ctx.anchor_fragment ?? null;
        this.anchorFragmentId = ctx.anchor_fragment?.id ?? null;
        this.anchorHistoric = ctx.anchor_fragment?.historic ?? false;
        this.messages = ctx.messages.map((m) => ({
          id: m.id,
          role: m.role as StreamMessage["role"],
          content: m.content,
          contentType: m.content_type,
          createdAt: m.created_at,
          topicName: this.topicName,
          ...(m.role === "tool"
            ? { toolName: "tool", toolOk: true, toolError: null }
            : {}),
        }));
      } catch {
        // 后端未启动时保持空状态
      }
    },
    /**
     * 发送一轮消息。turnRunning 时后端会排队（TURN_QUEUE 事件回执），
     * 因此仍然允许提交：本地先以「等待中」状态呈现，不阻塞用户写下一条。
     */
    async send(text: string) {
      const message = text.trim();
      if (!message) return;
      const queued = this.turnRunning;
      this.pushUser(message);
      if (queued) {
        const last = this.messages[this.messages.length - 1];
        last.queued = true;
        this.queuedMessageIds.push(last.id);
      }
      if (!queued) this.turnStarted();
      try {
        await api.sendTurn(message, this.currentTopicId);
      } catch (e) {
        this.lastError = (e as Error).message;
        this.turnRunning = false;
      }
    },
    /** 取消排队中的消息（只影响该条，不动 active turn） */
    dequeue(messageId: string) {
      this.queuedMessageIds = this.queuedMessageIds.filter((id) => id !== messageId);
      const msg = this.messages.find((m) => m.id === messageId);
      if (msg) msg.queued = false;
    },
    /** 后端队列已空（如排队项被取消）：清除本地所有「等待中」标记，避免状态残留 */
    clearQueuedFlags() {
      this.queuedMessageIds = [];
      for (const m of this.messages) if (m.queued) m.queued = false;
    },
  },
});
