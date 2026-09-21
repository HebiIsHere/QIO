import { defineStore } from "pinia";
import {
  connectEvents,
  publishTestEvent,
  type AgentEvent,
  type EventStreamHandle,
  type EventType,
} from "../services/events";
import { useSessionStore, type ToolExecutionSnapshot, type ToolPresentation, type ToolStatus } from "./session";
import { useApprovalsStore } from "./approvals";
import { api } from "../services/api";

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

/** 事件里的可选数值字段（缺失 / 非数字 → null，表示「没有版本信息」）。 */
function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 事件里的可选字符串字段（缺失 / 非字符串 → null）。 */
function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

/**
 * 事件的 turn_id 是否属于**主对话**。
 *
 * 主对话只认「当前正在跑的那一轮」：
 * * `subagent:task_xxx` 是子任务内部循环的 turn_id → 不进主对话；
 * * 没有主 turn 在跑时，任何带 turn_id 的助手输出都不该出现在主对话里。
 *
 * 没有 turn_id 的事件（老事件 / 系统事件）维持旧行为，交给各自的分支处理。
 */
function belongsToMainTurn(session: ReturnType<typeof useSessionStore>, turnId: string): boolean {
  if (!turnId) return true;
  return turnId === session.activeTurnId;
}

export type ModelMode = "native" | "text" | "unsupported";

/** 同步卡住时才显示的那句话（以及它出现前允许的宽限时间） */
export const RESYNC_NOTICE = "连接出现过一次抖动，正在同步最新状态…";
export const RESYNC_NOTICE_DELAY_MS = 1500;

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
    /** 正在 resync（期间实时事件先缓存，不直接与 snapshot 竞争） */
    resyncing: false,
    /** 同步期间再次收到 RESYNC → 完成当前同步后再补一次 */
    _resyncAgain: false,
    /** 同步期间到达的实时事件（按到达顺序暂存） */
    resyncBuffer: [] as AgentEvent[],
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
        /**
         * 连接建立后主动拉一次权威状态。
         *
         * 为什么必须这么做：后端不再把最近一批事件重放给新连接（那是新页面看到
         * 上一次错误提示与排队条的原因）。事件流现在只负责「变化」，
         * 「当前有没有在跑的任务 / 有没有待审批」要客户端自己问一次。
         * 这与 resync 走同一条路径，不新增协议。
         */
        void useSessionStore().resyncTurnState();
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
      /**
       * RESYNC 是控制事件，永远立即处理。
       * 其余事件在同步期间先缓存：否则「HTTP 还在路上时到达的新事件」
       * 会先被应用，随后旧 snapshot 返回又把它覆盖掉。
       */
      if (event.type === "RESYNC") {
        void this.startResync();
        return;
      }
      if (this.resyncing) {
        this.resyncBuffer.push(event);
        return;
      }
      this.dispatch(event);
    },
    dispatch(event: AgentEvent) {
      const session = useSessionStore();
      switch (event.type) {
        case "TURN_START":
          {
            const d = event.data as Record<string, unknown>;
            const tid = String(d.turn_id ?? "");
            // 只有真实的 TURN_START 能把 turn 设为 active。
            // 带上 revision：晚到的旧队列快照不能把这一轮清掉。
            // 陈旧事件整条不生效：连 turnRunning / turnPhase 都不许动。
            const instanceBefore = session.instanceId;
            const instanceChanged =
              session.adoptInstance(stringOrNull(d.instance_id)) && instanceBefore !== null;
            const applied = tid
              ? session.activateTurn(tid, numberOrNull(d.revision), stringOrNull(d.instance_id))
              : true;
            if (!applied) break;
            // 后端重启（instance 变化）：补一次完整同步，别只依赖这一条事件
            if (tid && instanceChanged) void this.startResync();
            if (tid) this.lastTurnId = tid;
            // 系统驱动的轮（例如独立任务完成后的收尾）不是用户发起的消息轮
            session.turnStarted(Boolean(d.notify));
          }
          break;
        case "TURN_END": {
          const d = event.data as Record<string, unknown>;
          const tid = String(d.turn_id ?? session.activeTurnId ?? this.lastTurnId ?? "");
          const endRevision = numberOrNull(d.revision);
          /**
           * 陈旧性判断：RESYNC 之后我们会补发缓冲里的旧事件，其中可能包含
           * 一条**已经被权威快照覆盖**的 TURN_END（服务器早已空闲，本地也按快照对齐过）。
           * 那种 END 不能再被应用 —— 否则它会把旧一轮的最终回答追加到当前对话里。
           * 属于当前 active turn 的 END 永远照常生效（它是唯一能结束运行态的事件）。
           */
          const staleEnd =
            endRevision !== null &&
            endRevision < session.queueRevision &&
            tid !== session.activeTurnId;
          if (staleEnd) break;
          // 结束也是一次状态变化：记下版本，避免更旧的快照事后把状态改回去
          session.noteQueueRevision(endRevision);
          /**
           * 归属规则（active/queued 模型下重新审查）：
           *
           * 1. 属于当前 active turn 的 END **必须生效** —— 它是唯一能结束界面运行
           *    状态的事件，绝不能因为「本地记错了 active」而被丢掉；
           * 2. 属于「已受理但从未开始」的 turn：只把它从排队列表里摘掉，不触碰 active；
           * 3. active 未知（重连后首帧就是 END）：按 lastTurnId / 去重表收敛；
           * 4. 其余（更早的 turn 迟到的收尾）：忽略，不能让旧事件把新任务标记成已结束。
           */
          if (tid) {
            const isActive = tid === session.activeTurnId;
            if (!isActive && session.isQueuedTurn(tid)) {
              // 一个从未开始执行的 turn 结束（或被取消）：只清理排队登记
              session.forgetQueuedTurn(tid);
              break;
            }
            if (!isActive && session.activeTurnId) break;
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
          // 一轮结束 = 这一轮不可能还有工具在跑：还挂着的「运行中」卡片
          // 说明它的 TOOL_END 丢了，先收口；服务器若知道真实结果，
          // 随后的快照核对会把它改回 success / failed / cancelled。
          session.convergeRunningTools(tid);
          session.turnEnded();
          session.lastTurnOutcome = { turnId: tid, status };
          if (status === "failed") {
            session.lastError = String(d.error ?? d.message ?? "本轮执行失败");
          } else if (status === "unavailable" && !session.warning) {
            // 后端通常已经先发了一条人话 WARNING；兜底也不直接把错误码丢给用户
            session.warning = "当前没有可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key";
          }
          if (tid) session.forgetQueuedTurn(tid);
          session.endTurn(tid, endRevision);
          break;
        }
        case "TURN_QUEUE": {
          const d = event.data as Record<string, unknown>;
          const queuedList = (d.queued as { turn_id: string; message: string }[] | undefined) ?? [];
          const running = (d.running as { turn_id: string; message: string } | null) ?? null;
          // 后端重启：revision 基准作废 → 接受新实例状态，并补一次完整同步
          const instanceBefore = session.instanceId;
          const instanceChanged =
            session.adoptInstance(stringOrNull(d.instance_id)) && instanceBefore !== null;
          // 后端的队列快照是权威：既能恢复本地漏掉的状态（重连 / 丢帧），
          // 也能清除本地已经过期的状态（服务器已空闲而本地还以为在跑）。
          // 一份快照**要么全部接受、要么全部拒绝** —— 校验在前，落地在后，
          // 所以这里不再单独给 session.turnQueue 赋值（那会造成半应用）。
          const applied = session.applyTurnQueue({
            instance_id: stringOrNull(d.instance_id),
            running,
            queued: queuedList,
            cancelled: (d.cancelled as { turn_id: string; message: string }[] | undefined) ?? [],
            revision: numberOrNull(d.revision),
          });
          // 后端队列已空：本地「等待中」标记同步清除（排队项被取消时不会残留）
          if (applied && !queuedList.length) session.clearQueuedFlags();
          if (instanceChanged) void this.startResync();
          break;
        }
        case "RESYNC": {
          // RESYNC 在 `route()` 里就已经拦下并触发同步，这里只是兜底
          void this.startResync();
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
          /**
           * 用量必须按**事件自带的 turn_id** 归属，不能拿「当前 active」去猜：
           * 子任务的用量（`subagent:task_x`）如果记到主 turn 上，
           * 界面显示的就不是这一轮的真实消耗了。
           * 事件缺 turn_id 时（老格式）才回退到「最近一轮」。
           */
          const usageTurnId = String(d.turn_id ?? "") || this.lastTurnId || "";
          this.recordUsage(usageTurnId, d);
          break;
        }
        case "ASSISTANT": {
          const d = event.data as Record<string, unknown>;
          const content = String(d.content ?? "");
          // 归属：子任务 / 系统内部的助手输出不得进入主对话
          if (!belongsToMainTurn(session, String(d.turn_id ?? ""))) break;
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
          // 归属：子任务内部的工具调用不属于主对话（它有自己的任务卡）
          if (!belongsToMainTurn(session, String(d.turn_id ?? ""))) break;
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
          // 归属同上：子任务的工具结束不能去改主对话里的卡
          if (!belongsToMainTurn(session, String(d.turn_id ?? ""))) break;
          session.finishTool(
            String(d.call_id ?? ""),
            String(d.tool ?? "?"),
            Boolean(d.ok),
            (d.error as string | null) ?? null,
            String(d.content_preview ?? ""),
            (d.presentation as ToolPresentation | null) ?? null,
            typeof d.duration_ms === "number" ? d.duration_ms : undefined,
            (d.status as ToolStatus | undefined) ?? null,
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
          // 「已登记但还没落实」的接续选择：界面据此显示「将从所选记录继续」，
          // 而不是让人以为已经建了新片段（阶段 1 的两步语义）。
          const pendingIntent = (d.pending_intent_id as string | null) ?? null;
          session.setPendingContinuation(
            pendingIntent
              ? {
                  intentId: pendingIntent,
                  sourceTitle: (d.pending_source_title as string | null) ?? null,
                }
              : null,
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
          // 归属：子任务内部报错 ≠ 整个主会话出错（它的失败由任务卡表达）
          if (!belongsToMainTurn(session, tid)) break;
          if (tid && session.activeTurnId && tid !== session.activeTurnId) break;
          session.lastError = String(d.message ?? "agent error");
          break;
        }
        case "WARNING": {
          // 非致命警告：只记提示，不代表 turn 结束（terminal 事件是 TURN_END/ERROR/取消）
          const d = event.data as Record<string, unknown>;
          // 归属：子任务内部的警告留在它自己的任务卡 / trace 里，不污染主会话的全局提示
          if (!belongsToMainTurn(session, String(d.turn_id ?? ""))) break;
          const msg = String(d.message ?? "agent warning");
          if (msg.trim()) {
            session.warning = msg;
          }
          break;
        }
        case "APPROVAL_REQUIRED": {
          const d = event.data as Record<string, unknown>;
          const approval = (d.approval ?? d) as Record<string, unknown>;
          // 与 RESYNC 恢复走同一个入口：同一个审批，实时收到和断线恢复必须一致
          this.handleApprovalRequired(approval, { autoOpen: !isUserEditing() });
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

    // -- 审批的统一入口 ---------------------------------------------------

    /**
     * 一个审批该走哪条 UI，只在这里决定一次。
     *
     * 实时 `APPROVAL_REQUIRED` 与 RESYNC 恢复出来的 pending approval 都调它，
     * 所以 `kind = continue`（预算耗尽后的继续/停止）在两条路径上都会进
     * ContinueBar，而不会被恢复成一个普通审批弹窗。
     */
    handleApprovalRequired(
      approval: Record<string, unknown>,
      opts: { autoOpen?: boolean } = {},
    ) {
      const session = useSessionStore();
      const id = String(approval.approval_id ?? "");
      if (!id) return;
      const kind = String(approval.kind ?? "");
      if (kind === "continue") {
        // 迭代/输出预算耗尽：进入「继续/停止」操作条，不进入审批队列
        const payload = (approval.payload ?? {}) as Record<string, unknown>;
        session.pendingContinue = {
          id,
          used: Number(payload.used_iterations ?? 0),
          max: Number(payload.max_iterations ?? 0),
        };
        return;
      }
      useApprovalsStore().enqueue(id, kind || "unknown", (approval.payload ?? {}) as Record<string, unknown>, {
        // 用户正在输入（含凭据表单）时不抢焦点；恢复时不抢焦点
        autoOpen: opts.autoOpen ?? !isUserEditing(),
        // 绑定信息随待办一起保存：应答时原样回传（服务端另有自己的校验）
        turnId: (approval.turn_id as string | null) ?? null,
        sessionId: (approval.session_id as string | null) ?? null,
        requestDigest: (approval.request_digest as string | null) ?? null,
      });
      session.activity = "approval";
    },

    // -- RESYNC 恢复协议 --------------------------------------------------

    /**
     * 进入同步：拉一次权威快照、完整应用、再把同步期间缓存的新事件按顺序补放。
     *
     * 关键点：
     * * **单飞**：同一时间只允许一个同步在跑，重复 RESYNC 只做标记（不并发 snapshot）；
     * * **顺序**：snapshot 先应用，之后才是同步期间到达的事件 —— 避免旧快照覆盖新事件；
     * * **状态可见**：成功 → normal 并清掉提示；失败 → failed 且保留错误，不假装已同步。
     */
    async startResync(): Promise<void> {
      const session = useSessionStore();
      session.resyncState = "resyncing";
      /**
       * 同步提示只在**真的卡住**时才出现。
       *
       * 之前的写法是立刻写一条「正在同步最新状态…」，但同步通常几十毫秒就完成、
       * 随后又被清掉 —— 用户什么都看不到（实测：注入 RESYNC 后页面上没有任何提示）。
       * 现在给它一个很短的宽限期：超过这个时间还没同步完，才把话说出来。
       */
      const notice = RESYNC_NOTICE;
      const graceTimer = setTimeout(() => {
        if (session.resyncState === "resyncing") session.warning = notice;
      }, RESYNC_NOTICE_DELAY_MS);
      if (this.resyncing) {
        this._resyncAgain = true;
        return;
      }
      this.resyncing = true;
      try {
        do {
          this._resyncAgain = false;
          const state = await api.getRuntimeState();
          session.adoptInstance(state.instance_id);
          session.applyTurnQueue(state.turn_queue);
          this.applyRuntimeState(state);
          this.flushResyncBuffer();
        } while (this._resyncAgain);
        session.resyncState = "normal";
        // 只撤下**这条同步提示**：同步期间新到的提醒不能被顺手抹掉
        if (session.warning === notice) session.warning = null;
      } catch (e) {
        // 失败必须如实说：界面显示的状态可能已经不是最新的
        session.resyncState = "failed";
        if (session.warning === notice) session.warning = null;
        session.lastError = `状态同步失败，界面显示的状态可能不是最新的：${(e as Error).message}`;
      } finally {
        clearTimeout(graceTimer);
        this.resyncing = false;
        this.flushResyncBuffer();
      }
    },

    /** 同步期间缓存的事件按到达顺序补放（嵌套的 RESYNC 只做标记）。 */
    flushResyncBuffer() {
      if (!this.resyncBuffer.length) return;
      const pending = this.resyncBuffer;
      this.resyncBuffer = [];
      for (const buffered of pending) {
        if (buffered.type === "RESYNC") {
          this._resyncAgain = true;
          continue;
        }
        this.dispatch(buffered);
      }
    },

    /**
     * 应用一份权威运行状态。
     *
     * 规则（不是「全部 merge」，而是按状态性质区分）：
     * * Turn 队列 —— **replace**（`applyTurnQueue`，含 revision / instance 校验）；
     * * 审批 —— **reconcile**：服务器没列出的 = 已经不再 pending，本地移除；
     * * 独立任务 —— **reconcile**：不在活动集合里的 running/queued 卡片按「结果未收到」收口；
     * * 工具 —— **reconcile**：按 `tool_call_id` 用服务器知道的执行事实核对。
     *   服务器给出终态的（success / failed / cancelled）一律以服务器为准 ——
     *   「没收到 TOOL_END」不等于「结果未知」；只有服务器也拿不出记录时才 unknown。
     */
    applyRuntimeState(state: {
      approvals: { approval_id: string; kind: string; payload: Record<string, unknown> }[];
      tasks: { task_id: string; tool: string; status: "queued" | "running" | "done" | "failed"; ok?: boolean | null; content_preview?: string; error?: string | null }[];
      tools?: ToolExecutionSnapshot[];
    }) {
      const session = useSessionStore();
      useApprovalsStore().reconcile(state.approvals.map((a) => a.approval_id));
      for (const approval of state.approvals) {
        // 恢复出来的审批不抢焦点：保留待办 + 亮出入口
        this.handleApprovalRequired(approval as unknown as Record<string, unknown>, {
          autoOpen: false,
        });
      }
      session.reconcileSubagents(state.tasks.map((t) => t.task_id));
      for (const task of state.tasks) {
        session.upsertSubagent(task.task_id, {
          status: task.status,
          toolName: task.tool,
          ok: task.ok ?? null,
          preview: task.content_preview ?? "",
          error: task.error ?? null,
        });
      }
      session.reconcileTools(state.tools ?? []);
    },
    async sendTest(type: EventType) {
      await publishTestEvent(type, { smoke: Date.now() });
    },
    clear() {
      this.events = [];
    },
  },
});
