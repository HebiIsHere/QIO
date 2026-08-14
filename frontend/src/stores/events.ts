import { defineStore } from "pinia";
import { connectEvents, publishTestEvent, type AgentEvent, type EventType } from "../services/events";
import { useSessionStore, type ToolPresentation } from "./session";
import { useApprovalsStore } from "./approvals";

export type ModelMode = "native" | "text" | "unsupported";

export const useEventStore = defineStore("events", {
  state: () => ({
    connected: false,
    events: [] as AgentEvent[],
    error: null as string | null,
    /** 模型三态适配（CAPABILITY 事件更新，默认 native） */
    modelMode: "native" as ModelMode,
    /** 累计 token 用量（USAGE 事件更新） */
    usageTokens: 0,
    _pendingMemoryInject: null as { label: string } | null,
    _source: null as EventSource | null,
  }),
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
          session.turnStarted();
          break;
        case "TURN_END": {
          session.turnEnded();
          const final = (event.data as Record<string, unknown>).final_content;
          // 无论 final 是否为空都清空 pending，避免残留注入挂到下一轮
          const inject = this._pendingMemoryInject;
          this._pendingMemoryInject = null;
          if (typeof final === "string" && final.trim()) {
            session.pushAssistant(final, inject ?? undefined);
          }
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
          const tokens = Number(d.tokens ?? 0);
          if (Number.isFinite(tokens) && tokens >= 0) {
            this.usageTokens = tokens;
          }
          break;
        }
        case "ASSISTANT": {
          const d = event.data as Record<string, unknown>;
          const content = String(d.content ?? "");
          if (content.trim()) {
            session.pushAssistant(content, undefined, true);
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
        case "APPROVAL_REQUIRED": {
          const d = event.data as Record<string, unknown>;
          const approval = (d.approval ?? d) as Record<string, unknown>;
          const id = String(approval.approval_id ?? "");
          if (id) {
            useApprovalsStore().enqueue(
              id,
              String(approval.kind ?? "unknown"),
              (approval.payload ?? {}) as Record<string, unknown>,
            );
          }
          break;
        }
      }
    },
    async sendTest(type: EventType) {
      await publishTestEvent(type, { smoke: Date.now() });
    },
    clear() {
      this.events = [];
    },
  },
});
