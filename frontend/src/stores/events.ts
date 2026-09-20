import { defineStore } from "pinia";
import {
  connectEvents,
  publishTestEvent,
  type AgentEvent,
  type EventStreamHandle,
  type EventType,
} from "../services/events";
import { useSessionStore, type ToolPresentation } from "./session";
import { useApprovalsStore } from "./approvals";

/**
 * 用户此刻是否正在输入（输入框 / 文本域 / 可编辑区域）。
 * 后台任务请求确认时用它决定「立即弹出」还是「亮出入口、等用户主动打开」。
 */
function isUserEditing(): boolean {
  if (typeof document === "undefined") return false;
  const ae = document.activeElement as HTMLElement | null;
  if (!ae) return false;
  return ae.tagName === "INPUT" || ae.tagName === "TEXTAREA" || ae.isContentEditable === true;
}

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
    /**
     * 能力降级提示（FALLBACK）：低干扰、一次性、可自动淡出。
     * 正常模式下永远是 null —— 不制造噪声（spec 第 40~42 条）。
     */
    fallbackNotice: null as string | null,
    /** 凭据状态里**影响当前功能**的那部分（人话，不含内部标识） */
    credentialNotice: null as string | null,
    /** 按 turn_id 归属的用量（不再把累计值显示成单条消息用量） */
    usageByTurn: {} as Record<string, TurnUsage>,
    /** 最近结束/开始的 turn_id：USAGE 事件紧随 TURN_END，用它归属 */
    lastTurnId: null as string | null,
    _source: null as EventStreamHandle | null,
    /** 已结束的 turn（防重连重放重复生效），有界 */
    endedTurns: [] as string[],
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
            // 系统驱动的轮（例如独立任务完成后的收尾）不是用户发起的消息轮
            session.turnStarted(Boolean(d.notify));
          }
          break;
        case "TURN_END": {
          const d = event.data as Record<string, unknown>;
          const tid = String(d.turn_id ?? session.activeTurnId ?? this.lastTurnId ?? "");
          /**
           * 迟到事件防护：只有当结束事件属于「当前活跃的 turn」时才结束界面状态。
           * 否则一个早先被取消/已结束的 turn 的收尾事件会把后来那次运行标记成已结束，
           * 让正在跑的任务看起来停了。
           */
          if (session.activeTurnId && tid && tid !== session.activeTurnId) {
            break;
          }
          // 重连重放：同一个 turn 的 TURN_END 只能生效一次
          if (tid && this.endedTurns.includes(tid)) break;
          if (tid) {
            this.endedTurns.push(tid);
            if (this.endedTurns.length > 200) this.endedTurns.shift();
          }
          this.recordUsage(tid, d);
          const status = String(d.status ?? "completed");
          const final = typeof d.final_content === "string" ? d.final_content : "";
          // 落定正在流式输出的助手消息（打字机结束，变为静态；interim 标记保留）
          session.finalizeAssistant();
          if (final.trim()) {
            session.applyFinalAnswer(final);
          } else if (status === "completed") {
            // 正常的空回答：不动内容
          } else {
            // 失败 / 取消：不得把中间话当成最终答案
            session.markLastAssistantInterim();
          }
          // 高影响知识候选：只有在回答完成之后才出现（顺序不能反）
          session.flushKnowledgeCandidates();
          session.turnEnded();
          session.lastTurnOutcome = { turnId: tid, status };
          if (status === "failed") {
            session.lastError = String(d.error ?? d.message ?? "本轮执行失败");
          } else if (status === "unavailable" && !session.warning) {
            // 后端通常已经先发了一条人话 WARNING；兜底也不直接把错误码丢给用户
            session.warning = "当前没有可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key";
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
        case "FALLBACK": {
          /**
           * 能力降级：只在真的降级时出现一次（后端也只在模式变化那次发）。
           * 提示必须低干扰、不阻塞对话，用户知道「功能可能受影响」就够了。
           */
          const d = event.data as Record<string, unknown>;
          const msg = String(d.message ?? "").trim();
          this.fallbackNotice =
            msg || "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）";
          break;
        }
        case "CREDENTIAL_STATUS": {
          /**
           * 凭据状态：只把「影响当前功能」的部分告诉用户，并且用人话。
           * 绝不显示 key_id / keychain 之类的内部标识（spec 第 44~46 条）。
           */
          const d = event.data as Record<string, unknown>;
          const status = String(d.status ?? "");
          if (status === "unavailable") {
            const msg = String(d.message ?? "").trim();
            this.credentialNotice = msg || "当前没有可用的模型凭据，请在「设置 → 凭据」里添加";
            session.warning = this.credentialNotice;
          } else if (status === "paused") {
            this.credentialNotice = "有一项凭据已暂停：依赖它的能力暂时不可用（可在「设置 → 凭据」恢复）";
          } else if (status === "revoked" || status === "deleted") {
            this.credentialNotice = "有一项凭据已失效：依赖它的能力暂时不可用（可在「设置 → 凭据」重新添加）";
          } else if (status === "active") {
            // 恢复正常：不打扰，清掉旧提示
            this.credentialNotice = null;
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
            // 已经有真实内容到达：阶段从「等待响应」转为「正在生成」
            session.turnPhase = "generating";
            if (session.activity !== "approval") session.activity = "generating";
            session.pushAssistant(content, true, true);
          }
          break;
        }
        case "TOOL_START": {
          // 工具开始执行：立刻出现/更新一张「运行中」的卡（不再等结束才可见）
          const d = event.data as Record<string, unknown>;
          const callId = String(d.call_id ?? "");
          session.startTool(
            callId,
            String(d.tool ?? "?"),
            (d.presentation as ToolPresentation | null) ?? null,
            (d.turn_id as string | null) ?? null,
            (d.arguments as Record<string, unknown> | undefined) ?? null,
          );
          if (session.turnRunning) session.activity = "tool";
          break;
        }
        case "TOOL_END": {
          const d = event.data as Record<string, unknown>;
          session.finishTool(
            String(d.call_id ?? ""),
            String(d.tool ?? "?"),
            Boolean(d.ok),
            (d.error as string | null) ?? null,
            String(d.content_preview ?? ""),
            (d.presentation as ToolPresentation | null) ?? null,
            typeof d.duration_ms === "number" ? d.duration_ms : undefined,
          );
          if (session.turnRunning) {
            session.activity = session.turnPhase === "generating" ? "generating" : "waiting";
          }
          break;
        }
        case "TOOL_CREATE_STATUS": {
          // 工具创建是一条流程、一张卡：同一 group_id 原地推进
          const d = event.data as Record<string, unknown>;
          const groupId = String(d.group_id ?? "");
          if (!groupId) break;
          session.upsertToolCreation(groupId, {
            phase: String(d.phase ?? "building"),
            label: (d.label as string | undefined) ?? undefined,
            detail: (d.detail as string | undefined) ?? undefined,
            ok: typeof d.ok === "boolean" ? d.ok : undefined,
            toolName: (d.tool_name as string | undefined) ?? undefined,
            turnId: (d.turn_id as string | null) ?? null,
          });
          if (session.turnRunning) session.activity = "tool";
          break;
        }
        case "KNOWLEDGE_CANDIDATE": {
          // 先缓冲：回答完成（TURN_END）之后才显示，绝不打断正在生成的回答
          const d = event.data as Record<string, unknown>;
          session.queueKnowledgeCandidate({
            knowledgeId: String(d.knowledge_id ?? ""),
            category: String(d.category ?? ""),
            content: String(d.content ?? ""),
            reason: (d.reason as string | undefined) ?? undefined,
          });
          break;
        }
        case "APPROVAL_RESULT": {
          /**
           * 授权的结局（单次使用）。本地通常已经处理过（是我们自己点的），
           * 但同一审批也可能由别处应答或与本地状态不一致 —— 按 id 收敛，
           * 避免留下一个「看不见的待审批项」。
           */
          const d = event.data as Record<string, unknown>;
          const approvalId = String(d.approval_id ?? "");
          if (!approvalId) break;
          useApprovalsStore().resolve(approvalId);
          if (session.pendingContinue?.id === approvalId) session.pendingContinue = null;
          break;
        }
        case "ANCHOR": {
          // agent 切换/创建话题后实时更新锚点（输入框/消息流话题行随之切换）
          const d = event.data as Record<string, unknown>;
          const topicId = String(d.topic_id ?? "");
          if (!topicId) break;
          const fragmentId = (d.fragment_id as string | null) ?? null;
          const fragmentTitle = (d.fragment_title as string | null) ?? null;
          const beforeKey = `${session.currentTopicId ?? ""}|${session.anchorFragmentId ?? ""}|${session.anchorHistoric}`;
          session.setAnchor(
            topicId,
            fragmentId,
            (d.topic_name as string | null) ?? null,
            fragmentId ? { id: fragmentId, title: fragmentTitle } : undefined,
            // historic=false（位置推进到当前片段）→ UI 收起「从…继续」提示
            Boolean(d.historic),
          );
          // 起点换了但界面还留在上一条对话上 → 显示与真实起点不一致。
          // 正在跑任务时不重载（会擦掉正在流式输出的内容），
          // 这种情况下由任务结束/下次进入时的加载来对齐。
          if (
            beforeKey !== `${session.currentTopicId ?? ""}|${session.anchorFragmentId ?? ""}|${session.anchorHistoric}` &&
            !session.turnRunning
          ) {
            void session.loadHistory();
          }
          break;
        }
        case "TOPIC_SWITCH_SUGGESTED": {
          // 推测切换（spec 第 29~30 条）：内容看起来属于另一个话题。
          // 只登记建议、亮出低干扰的确认条；Anchor 必须留在原处，
          // 用户点「转到这里」时才由 confirmPendingSwitch 真正切换。
          const d = event.data as Record<string, unknown>;
          const topicId = String(d.topic_id ?? "");
          if (!topicId || topicId === session.currentTopicId) break;
          session.setPendingSwitch({
            topicId,
            topicName: String(d.topic_name ?? "另一个话题"),
            reason: (d.reason as string | undefined) ?? undefined,
          });
          break;
        }
        case "SUBAGENT_STATUS": {
          const d = event.data as Record<string, unknown>;
          const taskId = String(d.task_id ?? "");
          if (!taskId) break;
          const raw = String(d.status ?? "running");
          const status: "queued" | "running" | "done" | "failed" =
            raw === "queued" || raw === "running" || raw === "done" || raw === "failed"
              ? raw
              : "running";
          session.upsertSubagent(taskId, {
            status,
            toolName: (d.display_name as string | undefined) || String(d.tool ?? "独立任务"),
            goal: (d.goal as string | undefined) ?? undefined,
            ok: typeof d.ok === "boolean" ? d.ok : null,
            preview: String(d.content_preview ?? ""),
            error: (d.error as string | null) ?? null,
          });
          // 独立任务在跑：全局只表达「正在处理独立任务」这一句
          if (status === "queued" || status === "running") session.activity = "subagent";
          else if (session.turnRunning) session.activity = "waiting";
          break;
        }
        case "ERROR": {
          // ERROR 只代表「出错了」：显示错误、记在对应的 turn 上，
          // 但绝不结束当前 turn —— 结束只认 TURN_END。
          const d = event.data as Record<string, unknown>;
          const tid = String(d.turn_id ?? "");
          if (tid && session.activeTurnId && tid !== session.activeTurnId) break;
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
              // 用户正在输入（含凭据表单）时不抢焦点：保留待办 + 亮出「有 N 项操作等待确认」入口
              { autoOpen: !isUserEditing() },
            );
            // 需要用户决定：全局状态说「等待确认」（决定本身在审批卡里）
            session.activity = "approval";
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
