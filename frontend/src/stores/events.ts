import { defineStore } from "pinia";
import { connectEvents, publishTestEvent, type AgentEvent, type EventType } from "../services/events";
import { useSessionStore, type ToolPresentation } from "./session";
import { useApprovalsStore } from "./approvals";

export type ModelMode = "native" | "text" | "unsupported";

export interface TurnUsage {
  /** 该 turn 的模型输出 token 累计（后端 USAGE/TURN_END 事件，按 turn_id 归属） */
  tokens: number;
  iterations?: number;
  toolCalls?: number;
}

export const useEventStore = defineStore("events", {
  state: () => ({
    connected: false,
    events: [] as AgentEvent[],
    error: null as string | null,
    /** 模型三态适配（CAPABILITY 事件更新，默认 native） */
    modelMode: "native" as ModelMode,
    /** 按 turn_id 归属的用量（不再把累计值显示成单条消息用量） */
    usageByTurn: {} as Record<string, TurnUsage>,
    /** 最近结束/开始的 turn_id：USAGE 事件紧随 TURN_END，用它归属 */
    lastTurnId: null as string | null,
    _pendingMemoryInject: null as { label: string } | null,
    _source: null as EventSource | null,
  }),
  getters: {
    turnUsageFor: (state) => (turnId?: string | null): TurnUsage | undefined =>
      turnId ? state.usageByTurn[turnId] : undefined,
  },
  actions: {
    connect() {
      this.disconnect();
      const source = connectEvents((event) => {
        this.events.push(event);
        if (this.events.length > 300) this.events.shift();
        this.error = null;
        this.route(event);
      });
      source.onopen = () => {
        this.connected = true;
      };
      source.onerror = () => {
        this.connected = false;
        this.error = "SSE 连接断开，请确认后端已启动";
      };
      this._source = source;
    },
    disconnect() {
      this._source?.close();
      this._source = null;
      this.connected = false;
    },
    route(event: AgentEvent) {
      const session = useSessionStore();
      switch (event.type) {
        case "TURN_START":
          {
            const d = event.data as Record<string, unknown>;
            const tid = String(d.turn_id ?? "");
            session.activeTurnId = tid || null;
            if (tid) this.lastTurnId = tid;
          }
          session.turnStarted();
          break;
        case "TURN_END": {
          const d = event.data as Record<string, unknown>;
          const tid = String(d.turn_id ?? session.activeTurnId ?? this.lastTurnId ?? "");
          this.recordUsage(tid, d);
          session.turnEnded();
          const final = d.final_content;
          // 落定正在流式输出的助手消息（打字机结束，变为静态）
          session.finalizeAssistant();
          // 无论 final 是否为空都清空 pending，避免残留注入挂到下一轮
          const inject = this._pendingMemoryInject;
          this._pendingMemoryInject = null;
          if (typeof final === "string" && final.trim()) {
            // 若最后一条已是流式消息，则 finalize 已落定；final 仅用于补充
            const last = session.messages[session.messages.length - 1];
            if (!(last && last.role === "assistant" && !last.streaming)) {
              session.pushAssistant(final, inject ?? undefined);
            }
          }
          session.activeTurnId = null;
          break;
        }
        case "TURN_QUEUE": {
          const d = event.data as Record<string, unknown>;
          const queuedList = (d.queued as { turn_id: string; message: string }[] | undefined) ?? [];
          session.turnQueue = {
            running: (d.running as { turn_id: string; message: string } | null) ?? null,
            queued: queuedList,
            cancelled: (d.cancelled as { turn_id: string; message: string }[] | undefined) ?? [],
          };
          // 后端队列已空：本地「等待中」标记同步清除（排队项被取消时不会残留）
          if (!queuedList.length) session.clearQueuedFlags();
          break;
        }
        case "CAPABILITY": {
          // 后端协议用 data.adapter 传三态模式（见 backend/tests/test_events.py），mode 作兜底
          const d = event.data as Record<string, unknown>;
          const mode = String(d.adapter ?? d.mode ?? "");
          if (mode === "native" || mode === "text" || mode === "unsupported") {
            this.modelMode = mode;
          }
          break;
        }
        case "USAGE": {
          const d = event.data as Record<string, unknown>;
          // USAGE 紧跟在 TURN_END 之后，归属当前（或最近结束的）turn
          this.recordUsage(session.activeTurnId ?? this.lastTurnId ?? "", d);
          break;
        }
        case "ASSISTANT": {
          const d = event.data as Record<string, unknown>;
          const content = String(d.content ?? "");
          if (content.trim()) {
            session.pushAssistant(content, undefined, true, true);
          }
          break;
        }
        case "MEMORY_INJECT": {
          const d = event.data as Record<string, unknown>;
          const count = Number(d.count ?? 0);
          const kind = String(d.kind ?? d.category ?? "记忆注入");
          const safeCount = Number.isFinite(count) ? Math.max(0, count) : 0;
          this._pendingMemoryInject = { label: `${kind} · ${safeCount} 条` };
          break;
        }
        case "TOOL_END": {
          const d = event.data as Record<string, unknown>;
          session.pushTool(
            String(d.tool ?? "?"),
            Boolean(d.ok),
            (d.error as string | null) ?? null,
            String(d.content_preview ?? ""),
            (d.presentation as ToolPresentation | null) ?? null,
          );
          break;
        }
        case "ANCHOR": {
          // agent 切换/创建话题后实时更新锚点（输入框/消息流话题行随之切换）
          const d = event.data as Record<string, unknown>;
          const topicId = String(d.topic_id ?? "");
          if (!topicId) break;
          const fragmentId = (d.fragment_id as string | null) ?? null;
          const fragmentTitle = (d.fragment_title as string | null) ?? null;
          session.setAnchor(
            topicId,
            fragmentId,
            (d.topic_name as string | null) ?? null,
            fragmentId ? { id: fragmentId, title: fragmentTitle } : undefined,
            // historic=false（位置推进到当前片段）→ UI 收起「从…继续」提示
            Boolean(d.historic),
          );
          break;
        }
        case "SUBAGENT_STATUS": {
          const d = event.data as Record<string, unknown>;
          const status = String(d.status ?? "?");
          if (status === "started") break; // 启动不重复入列
          session.pushTool(
            `子任务 · ${String(d.tool ?? "?")}`,
            Boolean(d.ok),
            (d.error as string | null) ?? null,
            String(d.content_preview ?? ""),
          );
          break;
        }
        case "ERROR": {
          session.turnEnded();
          this._pendingMemoryInject = null;
          const d = event.data as Record<string, unknown>;
          session.lastError = String(d.message ?? "agent error");
          break;
        }
        case "WARNING": {
          // 非致命警告：只记提示，不代表 turn 结束（terminal 事件是 TURN_END/ERROR/取消）
          const d = event.data as Record<string, unknown>;
          const msg = String(d.message ?? "agent warning");
          if (msg.trim()) {
            session.warning = msg;
          }
          break;
        }
        case "APPROVAL_REQUIRED": {
          const d = event.data as Record<string, unknown>;
          const approval = (d.approval ?? d) as Record<string, unknown>;
          const id = String(approval.approval_id ?? "");
          const kind = String(approval.kind ?? "");
          if (kind === "continue") {
            // 迭代/输出预算耗尽：进入「继续/停止」操作条，不进入审批队列
            const payload = (approval.payload ?? {}) as Record<string, unknown>;
            session.pendingContinue = {
              id,
              used: Number(payload.used_iterations ?? 0),
              max: Number(payload.max_iterations ?? 0),
            };
            break;
          }
          if (id) {
            useApprovalsStore().enqueue(
              id,
              kind || "unknown",
              (approval.payload ?? {}) as Record<string, unknown>,
            );
          }
          break;
        }
      }
    },
    /** 把用量记到指定 turn（tokens 缺失时保留已有值，避免被空 USAGE 清零） */
    recordUsage(turnId: string, d: Record<string, unknown>) {
      if (!turnId) return;
      const tokens = Number(d.tokens ?? NaN);
      const prev = this.usageByTurn[turnId];
      if (!Number.isFinite(tokens) && !prev) return;
      const iterations = Number(d.iterations ?? NaN);
      const toolCalls = Number(d.tool_calls ?? NaN);
      this.usageByTurn = {
        ...this.usageByTurn,
        [turnId]: {
          tokens: Number.isFinite(tokens) && tokens >= 0 ? tokens : (prev?.tokens ?? 0),
          ...(Number.isFinite(iterations)
            ? { iterations }
            : prev?.iterations !== undefined
              ? { iterations: prev.iterations }
              : {}),
          ...(Number.isFinite(toolCalls)
            ? { toolCalls }
            : prev?.toolCalls !== undefined
              ? { toolCalls: prev.toolCalls }
              : {}),
        },
      };
      this.lastTurnId = turnId;
    },
    async sendTest(type: EventType) {
      await publishTestEvent(type, { smoke: Date.now() });
    },
    clear() {
      this.events = [];
    },
  },
});
