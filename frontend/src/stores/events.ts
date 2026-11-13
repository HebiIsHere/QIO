import { defineStore } from "pinia";
import {
  connectEvents,
  publishTestEvent,
  type AgentEvent,
  type EventStreamHandle,
  type EventType,
} from "../services/events";
import {
  useSessionStore,
  type RuntimeAnchorSnapshot,
  type ToolExecutionSnapshot,
  type ToolFileReadFact,
  type ToolPresentation,
  type ToolStatus,
} from "./session";
import { useUnifiedApprovalsStore } from "./unifiedApprovals";
import { approvalMetaFromPending, useApprovalsStore } from "./approvals";
import { toPendingApproval } from "../services/approvalLifecycle";
import { api, type TurnFactsRow } from "../services/api";
import { resetBackend } from "../services/backend";
import { invalidateRestore, restoreRuntimeState } from "./restore";
import { useInteractiveStore } from "./interactive";

/**
 * 续作轮里「本轮 user 消息已经落库之后」才会出现的事件（见后端 turn_orchestrator 的写入顺序：
 * 先写 user 消息，再开始模型阶段）。首个这样的事件到达时，意图执行记录已经读得到。
 */
const INTENT_RECORD_PROGRESS_EVENTS: ReadonlySet<string> = new Set([
  "STAGE",
  "ASSISTANT",
  "TOOL_START",
  "TOOL_END",
]);

/**
 * 审批自动弹出的门控**不在这里判断**：统一的自动弹出规则已收敛到
 * stores/unifiedApprovals 的 maybeAutoOpen() + stores/feedbackGate（输入法组合输入、
 * 失焦延迟、中央弹窗、记忆星球覆盖层、设置/调试页）。
 * 事件层只负责把申请放进队列，避免两处门控各说一套。
 */

/** 事件里的可选数值字段（缺失 / 非数字 → null，表示「没有版本信息」）。 */
function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 事件里的可选字符串字段（缺失 / 非字符串 → null）。 */
function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

/**
 * TURN_END 的**独立系统核对注记**字段（B 的出口字段名待定）。
 *
 * 后端当前把注记拼进 final_content（旧形态）；B 会把它拆成独立字段。
 * 这里同时兼容几个常见命名：独立字段优先，final_content 里的内嵌形态由 store 兜底识别。
 */
function systemAnnotationOf(d: Record<string, unknown>): string | null {
  for (const key of [
    "final_annotation",
    "annotation",
    "system_note",
    "system_annotation",
    "fact_note",
  ]) {
    const value = d[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

/**
 * TOOL_END 的 file_read 事实（真实读取失败时后端才给）：
 * 后端字段是 snake_case；这里只做形状归一，缺失内容不推断、不编造。
 */
function fileReadFactOf(value: unknown): ToolFileReadFact | null {
  if (!value || typeof value !== "object") return null;
  const d = value as Record<string, unknown>;
  const fact: ToolFileReadFact = {
    attachmentId: String(d.attachment_id ?? "").trim(),
    name: String(d.name ?? "").trim(),
    reason: String(d.reason ?? "").trim(),
    nextStep: String(d.next_step ?? "").trim(),
    diagnostic: String(d.diagnostic ?? "").trim(),
  };
  if (
    !fact.attachmentId &&
    !fact.name &&
    !fact.reason &&
    !fact.nextStep &&
    !fact.diagnostic
  ) {
    return null;
  }
  return fact;
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

/**
 * 同步期间事件缓冲的**明确上限**。
 *
 * 同步迟迟不返回（或一直失败）时实时事件会一直到达；没有上限就是内存无界增长。
 * 到顶之后丢**最旧**的一条：ASSISTANT / NARRATIVE 这类事件是累计或幂等的，
 * 旧的丢掉、保留最新的才不会丢内容；真正被丢掉的增量由紧接着的权威快照补回来。
 */
export const RESYNC_BUFFER_LIMIT = 500;

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
    /** 同步期间到达的实时事件（按到达顺序暂存；有上限，见 RESYNC_BUFFER_LIMIT） */
    resyncBuffer: [] as AgentEvent[],
    /**
     * 本轮同步里因为缓冲到顶被丢掉的实时事件条数。
     *
     * 只要不为 0，就说明这一轮缓冲已经不完整 —— 必须再拉一次权威状态之后
     * 才能宣布同步成功，不能静默丢事件还说「已经同步」。
     */
    resyncDroppedEvents: 0,
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
         * 连接建立后走**唯一恢复入口**拉一次权威状态。
         *
         * 后端不再把最近一批事件重放给新连接（那是新页面看到上一次错误提示与
         * 排队条的原因）。事件流只负责「变化」，「当前有没有在跑的任务 /
         * 有没有待审批 / 有没有没执行完的消息」由这一次快照说清。
         * 首次连接、页面刷新后重连、普通重连都是这一条路径。
         */
        void restoreRuntimeState("connect").then(() =>
          /**
           * 补读与运行状态快照**分开**：pending 是审批生命周期的权威集合，history 是
           * 执行终态。断线期间错过的「结局」只能从这里补回来（快照只表达还活着的项）。
           * 失败不阻塞事件分发，原因分别落在 recoveryError / historyError 上。
           */
          useApprovalsStore().refreshApprovalsAndHistory("connect"),
        );
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
      /**
       * 连接断开 = 事件流不再连续：在飞的那次恢复可能带回旧连接上的旧状态，
       * 必须作废（代次 +1）；重连时由唯一入口重新拉一份权威快照。
       */
      invalidateRestore();
    },
    route(event: AgentEvent) {
      /**
       * RESYNC 是控制事件，永远立即处理。
       * 其余事件在同步期间先缓存：否则「HTTP 还在路上时到达的新事件」
       * 会先被应用，随后旧 snapshot 返回又把它覆盖掉。
       */
      if (event.type === "RESYNC") {
        void restoreRuntimeState("resync").then(() =>
          // RESYNC 完成后补读审批生命周期（pending + 终态历史），与 onopen 同一入口
          useApprovalsStore().refreshApprovalsAndHistory("resync"),
        );
        return;
      }
      if (this.resyncing) {
        if (this.resyncBuffer.length >= RESYNC_BUFFER_LIMIT) {
          // 到顶：丢最旧的一条（保留最新状态），并登记「这一轮缓冲已经不完整」。
          // 置 _resyncAgain 会让本轮结束后**再拉一次权威快照**，把丢掉的增量补回来。
          this.resyncBuffer.shift();
          this.resyncDroppedEvents += 1;
          this._resyncAgain = true;
        }
        this.resyncBuffer.push(event);
        return;
      }
      this.dispatch(event);
    },
    /**
     * 续作轮的「消息已落库之后」的事件到达：让当前话题用既有历史读取刷一次记录。
     *
     * 为什么是这几类事件：它们都发生在本轮 user 消息落库之后（topic 预判 / 写入在前，
     * 模型阶段在后），所以这是「记录现在读得到」的最早时机。每轮只兑现一次，不轮询；
     * 与意图执行无关的普通聊天轮不会登记，自然也不会走到这里。
     */
    refreshIntentRecordOnTurnProgress(
      event: AgentEvent,
      session: ReturnType<typeof useSessionStore>,
    ): void {
      if (!INTENT_RECORD_PROGRESS_EVENTS.has(event.type)) return;
      const turnId = String((event.data as Record<string, unknown> | undefined)?.turn_id ?? "");
      if (!turnId) return;
      void session.refreshIntentExecutionTurnProgress(turnId);
    },
    dispatch(event: AgentEvent) {
      const session = useSessionStore();
      this.refreshIntentRecordOnTurnProgress(event, session);
      switch (event.type) {
        case "TURN_START":
          {
            const d = event.data as Record<string, unknown>;
            const tid = String(d.turn_id ?? "");
            /**
             * 已经结束过的 turn 不可能再开始（事件倒序 / 重连重放）：
             * 不能把已取消的排队轮重新点亮成 active，否则界面会永远停在「运行中」。
             */
            if (tid && this.endedTurns.includes(tid)) break;
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
            // 后端重启（instance 变化）：地址与令牌都可能变 → 重新解析连接信息，
            // 并补一次完整同步，别只依赖这一条事件
            if (tid && instanceChanged) {
              resetBackend();
              void restoreRuntimeState("instance-change");
            }
            if (tid) this.lastTurnId = tid;
            /**
             * 换了一轮：上一轮的话题阶段不能延续（阶段只属于它自己那一轮）。
             * 同一轮重复到达的 TURN_START（重连重放）不清 —— 否则会丢掉已经收到的阶段。
             */
            if (tid && session.topicStageTurnId !== tid) {
              session.resetTopicPhase();
              // 上一轮暂存的正文不可能再等来 answerable：新一轮开始即作废
              session.clearDeferredFormalAnswers();
            }
            // TURN_START 带 request_id 时精确关联到那次发送动作（契约 5）：
            // 「这一轮已经开始」由事件认定；回执/查证就不得再改写该轮运行态，
            // 查证 404（记录已丢）也不得把真实在跑的轮次误报成「未发送」。
            const requestId = stringOrNull(d.request_id);
            if (requestId) session.noteTurnStartedForRequest(requestId, tid || undefined);
            // 系统驱动的轮（例如独立任务完成后的收尾）不是用户发起的消息轮
            session.turnStarted(Boolean(d.notify));
            /**
             * 意图执行记录（R2 返修二）：确认 / 恢复 / 重试由**服务端**发起续作轮，
             * 本机没有这次发送动作（TURN_START 不带 request_id，也不是 notify 系统轮）。
             * 登记它，等这一轮的消息真正落库后用既有历史读取把记录刷出来。
             */
            if (tid && !requestId && !Boolean(d.notify)) {
              session.noteIntentExecutionTurn(tid);
            }
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
          if (staleEnd) {
            /**
             * 陈旧 END 的**唯一例外：已经确定发生的结束事实**。
             *
             * 一条比权威队列快照更旧、但属于本会话**已知轮**的 END 不能整条丢掉：
             * 否则「为什么停下 + 还能不能重试」会随着一次 revision 竞争（队列快照先
             * 到、取消的 END 后到）一起消失（§七 C1/C5：重复 / 迟到 / 竞争取消必须
             * 幂等，并且事实先落地、再清排队标记）。
             *
             * 例外范围严格限定：**只记事实**（外加清理「等待中」标记）——
             * 绝不应用 final_content、绝不触碰 active / turnRunning / 全局结局。
             */
            const stillQueued =
              session.isQueuedTurn(tid) || (Boolean(tid) && session.hasQueuedMessage(tid));
            if (
              tid &&
              !this.endedTurns.includes(tid) &&
              (stillQueued || Boolean(session.userMessageFor(tid)))
            ) {
              this.endedTurns.push(tid);
              if (this.endedTurns.length > 200) this.endedTurns.shift();
              session.recordTurnFacts(tid, d);
              if (stillQueued) session.concludeQueuedTurn(tid);
            }
            break;
          }
          // 结束也是一次状态变化：记下版本，避免更旧的快照事后把状态改回去
          session.noteQueueRevision(endRevision);
          const isActive = Boolean(tid) && tid === session.activeTurnId;
          /**
           * 归属规则（active/queued 模型下重新审查）：
           *
           * 1. 属于「已受理但从未开始」的排队 turn：它也有自己的结局（取消 / 准备失败）。
           *    **先**把结束事实（原因 / 动作 / 耗时）可靠落地，**再**清理排队标记；
           *    绝不触碰 active —— 正在跑的 A 与它无关。
           * 2. 属于当前 active turn 的 END **必须生效** —— 它是唯一能结束界面运行
           *    状态的事件，绝不能因为「本地记错了 active」而被丢掉；
           * 3. active 未知（重连后首帧就是 END）：按 lastTurnId / 去重表收敛；
           * 4. 其余（更早的 turn 迟到的收尾）：不能让旧事件把新任务标记成已结束。
           */
          if (!isActive && tid && session.isQueuedTurn(tid)) {
            if (this.endedTurns.includes(tid)) break;
            this.endedTurns.push(tid);
            if (this.endedTurns.length > 200) this.endedTurns.shift();
            session.recordTurnFacts(tid, d);
            session.concludeQueuedTurn(tid);
            break;
          }
          if (tid && !isActive && session.activeTurnId) {
            /**
             * 另一轮正在跑，而这条 END 属于本会话**已知**的某一轮（有它的用户消息）：
             * 典型是「排队项先被 TURN_QUEUE 摘出队列，END 随后才到」。
             * 事实必须留下，但绝不能触碰 active 那一轮。
             * 完全未知的旧轮（本地没有它的用户消息）才忽略 —— 旧事件不能把新任务标记成已结束。
             */
            if (session.userMessageFor(tid)) {
              if (this.endedTurns.includes(tid)) break;
              this.endedTurns.push(tid);
              if (this.endedTurns.length > 200) this.endedTurns.shift();
              session.recordTurnFacts(tid, d);
              session.concludeQueuedTurn(tid);
            }
            break;
          }
          // 重连重放：同一个 turn 的 TURN_END 只能生效一次
          if (tid && this.endedTurns.includes(tid)) break;
          if (tid) {
            this.endedTurns.push(tid);
            if (this.endedTurns.length > 200) this.endedTurns.shift();
          }
          this.recordUsage(tid, d);
          // 权威耗时事实（契约 §3）：折叠态不展开也要显示总耗时
          if (tid) session.recordTurnFacts(tid, d);
          const status = String(d.status ?? "completed");
          /**
           * 经 Lead 授权（fb-c R6，仅此处 TURN_END 分流）：
           * * final_content 的**存在性**必须保留 —— 缺省（undefined/null）= 不校准正文，
           *   显式空串 = 清空目标正文（契约 K2.2）。旧写法把 null 归一成 ""，两者再也分不开。
           * * answer_id（目标回答的 delta_id，fb-d 生产）必须传给校准：有身份只改那一条，
           *   没有身份才按「该轮最后一条正式回答」校准。
           */
          const hasFinal = d.final_content !== undefined && d.final_content !== null;
          const final = hasFinal ? String(d.final_content) : null;
          const annotation = systemAnnotationOf(d);
          const answerId = stringOrNull(d.answer_id);
          // 落定正在流式输出的助手消息（打字机结束，变为静态；interim 标记保留）
          session.finalizeAssistant();
          if (hasFinal || annotation) {
            session.applyFinalAnswer(final, d.verification, { turnId: tid, annotation, answerId });
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
          /**
           * 一轮结束：本轮的话题申请登记与还没消费的草稿跟随一起收口。
           * 没有发生 ANCHOR（拒绝/失败/过期/后端重启）时草稿留在原话题、归属不变；
           * 这里只清「一次性」标记，不移动任何文字。
           */
          session.clearTopicApplicationsForTurn(tid);
          session.turnEnded();
          session.lastTurnOutcome = { turnId: tid, status };
          if (status === "failed") {
            session.lastError = String(d.error ?? d.message ?? "本轮执行失败");
          } else if (status === "incomplete") {
            /**
             * 不完整结束（契约 §七 C2）：已确认正文照常校准（上面那条分支），
             * 但这一轮**不是失败** —— lastError 绝不能写成「本轮执行失败」。
             * 原因 / 原因码 / 原始错误都随 recordTurnFacts 落到了这一轮（过程区
             * 默认显示一句话原因、详情折叠），顶部还有由 lastTurnOutcome 驱动的
             * 安静提示说「未完成」—— 所以这个结局既不被说成失败，也不会被吞掉。
             */
          } else if (status === "unavailable" && !session.warning) {
            // 后端通常已经先发了一条人话 WARNING；兜底也不直接把错误码丢给用户
            session.warning = "当前没有可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key";
          }
          if (tid) session.forgetQueuedTurn(tid);
          session.endTurn(tid, endRevision);
          /**
           * 意图执行记录（R2 返修二）：相关续作轮结束后刷新**真实终态**。
           *
           * 只对登记过的续作轮生效（确认 / 恢复 / 手动重试 / 服务端自动重试都在这条路径上），
           * 走既有历史读取；失败按既有 history 错误口径，不会把「没读到」说成已更新。
           */
          void session.refreshIntentExecutionTurnEnd(tid);
          /**
           * 一轮结束 = 这一轮里可能刚好创建、测试或提交了开发任务。
           * 顶部那行「有 N 个工具开发任务没做完」必须马上跟上，
           * 否则用户得刷新页面才看得到刚发生的事。
           */
          void session.refreshDevTasks();
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
          /**
           * 权威队列显示服务端已空闲：这一轮已经结束（TURN_END 可能丢帧）——
           * 话题申请的一次性草稿跟随不能跨轮残留，否则下次普通导航会带走草稿。
           */
          if (applied && !running && !queuedList.length) {
            session.clearTopicApplicationsForTurn(session.activeTurnId);
          }
          if (instanceChanged) {
            // 换了实例：旧的连接信息（端口 / 令牌）不再可信
            resetBackend();
            void restoreRuntimeState("instance-change");
          }
          break;
        }
        case "RESYNC": {
          // RESYNC 在 `route()` 里就已经拦下并触发同步，这里只是兜底
          void restoreRuntimeState("resync").then(() =>
            useApprovalsStore().refreshApprovalsAndHistory("resync"),
          );
          break;
        }
        case "RECOVERY_INBOX_CHANGED": {
          /**
           * R19-B：普通回答保存失败后，后端已在**已提交事务**里写好 pending 恢复
           * 记录并发来这条通知。事件不是清单权威 —— 复用既有收件箱读取刷新，
           * 权威仍是 GET /api/recovery/records；重复 / 重放事件幂等（最多多拉一次）。
           */
          session.requestRecoveryInboxRefresh();
          break;
        }
        case "INTERACTIVE_INTENTS_CHANGED": {
          /**
           * PM-QIO032 #26（Lead 授权的最小订阅，仅互动事件；其它事件语义不动）：
           * 后端在提案 / 更新 / 确认成功后发来「这批意图的真实状态」。
           * store 按 intent_id 幂等 upsert（重复投递 / 重连补发不生成重复卡），
           * 非当前板面的事件直接忽略；未知 id 不编造卡片，走权威 intent-views 合并刷新。
           */
          useInteractiveStore().applyInteractiveIntentsChanged(
            event.data as unknown as {
              board_id?: string;
              intent_ids?: string[];
              statuses?: Record<string, string>;
              reason?: string;
            },
          );
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
            // 全局问题面板只显示「暂停状态 + 打开凭据设置」，这里不再重复恢复入口文案。
            this.credentialNotice = "有一项凭据已暂停：依赖它的能力暂时不可用";
          } else if (status === "revoked" || status === "deleted") {
            this.credentialNotice = "有一项凭据已失效：依赖它的能力暂时不可用";
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
            /**
             * 契约 §2.1：interim 由**事件字段**决定 —— 旧代码把它写死成 true，
             * 结果正式回答也被标成「过程」。streaming 缺省 true 保持旧后端
             * 「整段推送 + 打字机」的行为；显式 false（text 兼容档）时不假装实时生成。
             * delta_id / seq：同轮多段正文各自保真，重复 / 迟到事件按 seq 丢弃。
             */
            /**
             * 正文门（契约 §3 / §8）：本轮有阶段事实且还没到 answerable 时，
             * 不把**正式正文**写进回答流。
             *
             * 顺序容错：契约 §3 原文顺序是「先交付缓冲正文，随后进入 ANSWERABLE」，
             * 因此这里不丢弃，而是按原事件暂存（有界、不提前展示、不改写）；
             * TOPIC_PHASE(answerable) 到达后原样回放。后端改成 answerable 先发时
             * 走正常展示路径，两种顺序都不丢回答 —— 门本身没有放宽。
             */
            if (d.interim !== true && !session.acceptsTopicFormalAnswer(String(d.turn_id ?? ""))) {
              session.holdDeferredFormalAnswer({
                turnId: String(d.turn_id ?? ""),
                content,
                streaming: d.streaming !== false,
                meta: {
                  deltaId: stringOrNull(d.delta_id),
                  seq: numberOrNull(d.seq),
                  stageId:
                    d.stage_id === null
                      ? null
                      : typeof d.stage_id === "string" && d.stage_id.trim()
                        ? d.stage_id
                        : undefined,
                  callIds: Array.isArray(d.call_ids) ? (d.call_ids as string[]).map(String) : [],
                },
              });
              break;
            }
            session.pushAssistant(content, d.interim === true, d.streaming !== false, {
              deltaId: stringOrNull(d.delta_id),
              seq: numberOrNull(d.seq),
              // 契约最终版：interim 时后端给同批 STAGE 的 stage_id 与这一批的 call_ids
              // （正式回答 stage_id=null、call_ids=[]）。
              //
              // 显式 null 与「事件里没有这个键」语义不同（Lead 追加契约 task-6）：
              // 显式 null = 这段文字**未归属**（等同一 delta_id 的累计快照就地归位）；
              // 缺字段才是旧后端，才允许回落到「到达时的当前阶段」。
              stageId:
                d.stage_id === null
                  ? null
                  : typeof d.stage_id === "string" && d.stage_id.trim()
                    ? d.stage_id
                    : undefined,
              callIds: Array.isArray(d.call_ids) ? (d.call_ids as string[]).map(String) : [],
            });
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
            // 契约 §1.3：可选 stage_id（缺省 null = 归入「整轮」）
            (d.stage_id as string | null) ?? null,
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
            // 工具调用历史的记录 id：卡片展开时按它取全文（实时与历史同一条路径）
            (d.record_id as string | null) ?? null,
            // 契约 §1.3：可选 stage_id（缺省 null = 归入「整轮」）
            (d.stage_id as string | null) ?? null,
            /**
             * 结构化事实（用户要求 §四.7）：界面用稳定 code / 类别标签生成可读结论，
             * 不从错误文本猜。旧事件没有这些字段 → null，保持历史行为。
             * result_status 是 ToolResult.status 原值（awaiting_approval 等）：
             * TOOL_END 的 status 按 ok 归一，不能替代「还在等审批」这个事实。
             */
            {
              code: stringOrNull(d.code),
              category: stringOrNull(d.category),
              categoryLabel: stringOrNull(d.category_label),
              recoverable: typeof d.recoverable === "boolean" ? d.recoverable : null,
              resultStatus: stringOrNull(d.result_status ?? d.tool_result_status),
              // 知识提交的稳定身份（同一条知识重试成功时据此更新该项，见问题 3）
              knowledgeKey: stringOrNull(d.knowledge_key),
              // 真实读取失败的用户安全事实（哪个附件 / 为什么 / 下一步）：只做形状归一。
              fileRead: fileReadFactOf(d.file_read),
            },
          );
          if (session.turnRunning) {
            session.activity = session.turnPhase === "generating" ? "generating" : "waiting";
          }
          break;
        }
        case "NARRATIVE": {
          /**
           * 执行叙事：模型自己决定说的一句话（announce / progress / warning / result）。
           *
           * 与工具事实完全分开：这里只落一条叙事消息，绝不改动工具卡、风险或审批。
           * 归属规则同工具事件 —— 子任务内部的过程说明不进主对话。
           */
          const d = event.data as Record<string, unknown>;
          if (!belongsToMainTurn(session, String(d.turn_id ?? ""))) break;
          session.applyNarrative({
            narrative_id: String(d.narrative_id ?? ""),
            turn_id: (d.turn_id as string | null) ?? null,
            kind: String(d.kind ?? "progress"),
            text: String(d.text ?? ""),
            call_ids: Array.isArray(d.call_ids) ? (d.call_ids as string[]) : [],
            created_at: (d.created_at as string | null) ?? null,
          });
          break;
        }
        case "STAGE": {
          /**
           * 过程阶段（契约 §1.3）：系统生成，前端只消费 —— 阶段集合、顺序、说明、状态。
           * 归属规则同其它主轮事件：子任务内部的阶段不进主对话。
           */
          const d = event.data as Record<string, unknown>;
          if (!belongsToMainTurn(session, String(d.turn_id ?? ""))) break;
          session.upsertStage({
            stage_id: String(d.stage_id ?? ""),
            turn_id: (d.turn_id as string | null) ?? null,
            index: numberOrNull(d.index),
            status: String(d.status ?? ""),
            name: String(d.name ?? ""),
            text: String(d.text ?? ""),
            kind: String(d.kind ?? ""),
            op: String(d.op ?? ""),
            narrative_id: (d.narrative_id as string | null) ?? null,
            call_ids: Array.isArray(d.call_ids) ? (d.call_ids as string[]) : [],
            created_at: (d.created_at as string | null) ?? null,
          });
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
          const phase = String(d.phase ?? "");
          const decision = String(d.decision ?? "").trim();
          const terminal = new Set([
            "executed",
            "failed",
            "invalidated",
            "interrupted_unverified",
            "expired",
            "rejected",
            "cancelled",
          ]);
          /**
           * 话题申请（topic_switch / topic_create）的结局只做两件 UI 派生的事：
           * 1. 批准/执行中 → 武装**一次性**草稿跟随（等待期间的草稿跟到确认后的话题）；
           * 2. 拒绝/取消/失败/过期 → 清除跟随（草稿留在原话题，归属不变）。
           *
           * legacy 的 /respond 事件不带 kind / payload，所以用 APPROVAL_REQUIRED
           * 时的本地登记认身份；执行结果事件带顶层 kind 也能认出来。
           */
          const approvalEnvelope = (d.approval ?? {}) as Record<string, unknown>;
          const eventKind = String(approvalEnvelope.kind ?? d.kind ?? "");
          const registered = session.topicApplicationFor(approvalId);
          const isTopicApplication =
            Boolean(registered) || eventKind === "topic_switch" || eventKind === "topic_create";
          /**
           * 事件身份校验（契约 §5）：结果事件带 operation_id 且与本地登记不同 =
           * 旧操作的事件 —— 不改当前位置、不消费，也不误解锁别的申请。
           */
          const eventOperationId = String(
            d.operation_id ?? (approvalEnvelope as Record<string, unknown>).operation_id ?? "",
          ).trim();
          const registeredOperationId = String(registered?.operationId ?? "").trim();
          if (
            isTopicApplication &&
            eventOperationId &&
            registeredOperationId &&
            eventOperationId !== registeredOperationId
          ) {
            break;
          }
          if (isTopicApplication) {
            const payload = (approvalEnvelope.payload ?? d.payload ?? {}) as Record<string, unknown>;
            const topicKind =
              registered?.kind ??
              (eventKind === "topic_create" ? "topic_create" : "topic_switch");
            const targetTopicId =
              registered?.targetTopicId ?? (String(payload.target_topic_id ?? "").trim() || null);
            /**
             * 失效 ≠ 拒绝（契约 §5 / §8）：后端结构化判定的失效（decision=stale 或
             * code=topic_application_stale）**不**写拒绝范围、**不**自动重弹、不覆盖当前选择。
             * 申请保留在队列里说明原因并保留恢复入口，由用户主动收口；
             * 也绝不把别的申请一起解锁（只按 approval_id 处理这一条）。
             */
            const invalidatedCode = String(
              d.code ?? (approvalEnvelope as Record<string, unknown>).code ?? "",
            ).trim();
            const invalidatedByBackend =
              decision === "stale" ||
              invalidatedCode === "topic_application_stale" ||
              String(d.state ?? "").trim() === "stale";
            if (invalidatedByBackend) {
              const reasonRaw =
                d.message ?? d.reason ?? (typeof d.detail === "string" ? d.detail : "");
              const reason =
                String(reasonRaw ?? "").trim() ||
                "这条话题申请已经失效（本轮绑定的话题已变化或申请不再等待处理），不会再执行。";
              useUnifiedApprovalsStore().markTopicInvalidated(approvalId, {
                code: invalidatedCode || "stale",
                reason,
                operationId: registered?.operationId ?? "",
              });
              session.dropTopicApplication(approvalId);
              session.clearTopicDraftFollow();
              // R2：后端判定的失效操作不会再成功 —— 登记失效操作身份，
              // 它迟到的 ANCHOR 必须整条拒绝（不重新导航、不迁移草稿）。
              session.rememberInvalidTopicOperation(
                registeredOperationId || eventOperationId,
              );
              if (session.activity === "approval") {
                session.activity = session.turnPhase === "generating" ? "generating" : "waiting";
              }
              // 失效不是执行终态：保留申请说明与恢复入口，不走 recordResult 的终态收口
              break;
            }
            /**
             * 已在执行 = 用户已经选过话题：等待期间写的草稿只跟随**本次操作**的
             * 权威成功结果（F12）。身份绑定 operation_id + param_version + turn_id +
             * 原话题/片段；create 的目标未知只保留待关联，绝不当作通配。
             * 旧后端没有 operation_id → 不武装（草稿留原话题，安全侧）。
             */
            if (phase === "approved" || phase === "executing") {
              const operationId = String(registered?.operationId ?? eventOperationId ?? "").trim();
              if (operationId) {
                session.armTopicDraftFollow({
                  operationId,
                  // 审批稳定身份：用户弹窗里改名后确认时 param_version 会推进，
                  // 但 approval_id 不变 —— 权威 ANCHOR 据此仍认出「同一次确认」。
                  approvalId: registered?.approvalId ?? approvalId,
                  // 事件若带最新参数版本（用户改名后确认的新版本）优先于申请登记时的旧版本
                  paramVersion:
                    typeof d.param_version === "number" && Number.isFinite(d.param_version)
                      ? d.param_version
                      : typeof payload.param_version === "number" &&
                          Number.isFinite(payload.param_version)
                        ? payload.param_version
                        : registered?.paramVersion ?? null,
                  turnId: registered?.turnId ?? String(d.turn_id ?? ""),
                  kind: topicKind,
                  expectedTargetTopicId: targetTopicId,
                  fromTopicId:
                    registered?.fromTopicId ??
                    (String(payload.from_topic_id ?? "").trim() || session.currentTopicId),
                  fromFragmentId:
                    registered?.fromFragmentId ??
                    (String(payload.from_fragment_id ?? "").trim() || session.anchorFragmentId),
                });
              } else {
                session.clearTopicDraftFollow();
              }
            } else {
              // executed / rejected / cancelled / expired / invalidated / failed：登记收口。
              // executed 不动已武装的跟随（ANCHOR 可能就在同一顺序里）；其余结局一律清除。
              session.dropTopicApplication(approvalId);
              if (phase !== "executed") {
                session.clearTopicDraftFollow();
                // R2：非 executed 终态 = 这次操作不会再成功 → 登记失效操作身份，
                // 它迟到的 ANCHOR 不得重新导航或迁移草稿。
                session.rememberInvalidTopicOperation(registeredOperationId || eventOperationId);
              }
            }
            /**
             * 话题申请不把整轮标成 activity="approval"（等待状态由统一队列派生）；
             * 若兼容路径留下了该状态，决定后按真实阶段恢复 —— 不一直停在「等待确认」。
             */
            if (session.activity === "approval") {
              session.activity = session.turnPhase === "generating" ? "generating" : "waiting";
            }
          }
          /**
           * R16（契约 §14.8）：审批执行成功事件携带整份 position → 走唯一接受入口。
           *
           * 导航命令的真实执行结果优先以这条事件为准（B/A 保证 executed 事件携带
           * position）；前端消费点就在这里，业务层不再自己 setAnchor。
           */
          if (
            phase === "executed" &&
            d.position &&
            typeof d.position === "object" &&
            !Array.isArray(d.position)
          ) {
            const positionRecord = d.position as Record<string, unknown>;
            session.acceptPositionState(
              {
                ...positionRecord,
                origin: "topic_operation",
                operation_id:
                  registeredOperationId ||
                  eventOperationId ||
                  (typeof positionRecord.operation_id === "string"
                    ? String(positionRecord.operation_id)
                    : undefined),
                approval_id: approvalId,
                param_version:
                  typeof d.param_version === "number" && Number.isFinite(d.param_version)
                    ? d.param_version
                    : (registered?.paramVersion ?? undefined),
                turn_id: String(d.turn_id ?? registered?.turnId ?? "").trim() || undefined,
                instance_id: typeof d.instance_id === "string" ? d.instance_id : undefined,
              },
              "command",
            );
          }
          /**
           * R9：事件不新增类型，用 payload.phase 表达状态（W1 冻结）。
           * 前端只呈现：中间态（confirmed / approved / executing）保留待办；
           * 终态（executed / failed / invalidated / interrupted_unverified / …）才收口。
           * 旧后端没有 phase → recordResult 内部按旧的「有结局即收敛」处理。
           */
          useApprovalsStore().recordResult(d);
          if ((!phase || terminal.has(phase)) && session.pendingContinue?.id === approvalId) {
            session.pendingContinue = null;
          }
          break;
        }
        case "TOPIC_PHASE": {
          /**
           * 本轮话题阶段（契约 §3）：后端唯一来源，前端只消费。
           * 旧 / 乱序 / 别的轮 / 别的 operation 的事件由 session.applyTopicPhase 丢弃。
           */
          const d = event.data as Record<string, unknown>;
          const advanced = session.applyTopicPhase(d);
          /**
           * F12：本次操作进入 recoverable_failure = 没有成功 → 草稿跟随按失败收口，
           * 不被迟到的成功事件再带走（草稿留在原话题）。
           */
          if (String(d.phase ?? "").trim() === "recoverable_failure") {
            session.clearTopicDraftFollowForOperation({
              operationId: d.operation_id as string | null,
              turnId: d.turn_id as string | null,
            });
            // R2：recoverable_failure 不是「被拒绝 / 已失效 / 已被新导航替代」的
            // 终态证据（恢复重试可能成功，且它的导航可能已真实发生），因此这里不登记
            // invalid 操作；它迟到的位置事件仍交给 nav_version 顺序规则判定，不能被
            // 整条拉黑（否则恢复成功后的权威事件会被误拒）。草稿跟随已按失败收口。
          }
          /**
           * answerable = 允许发布正式正文：把顺序容错暂存的那份按原事件回放
           * （只有真的推进到 answerable 才回放；旧 / 乱序事件不放行）。
           */
          if (advanced && session.stage === "answerable") {
            session.releaseDeferredFormalAnswers(d.turn_id as string | null);
          }
          break;
        }
        case "ANCHOR": {
          /**
           * R16：ANCHOR 是**完整位置状态**事件（契约 §7.1 / §14.3）。
           *
           * 唯一接受入口 session.acceptPositionState 负责全部判定与写入：
           * * 主排序 = position_identity + position_version；旧整份拒绝、同版本同内容幂等、
           *   同版本内容冲突记录并触发一次权威恢复；
           * * 位置 facts / Anchor 视图 / pending / 草稿跟随由它整份处理；
           * * 旧后端没有 position_version 时走 legacy_nav_order 兼容路径（文档标注）。
           *
           * 事件层**不再**自己 setAnchor / setPendingContinuation / 判断历史重载。
           */
          const d = event.data as Record<string, unknown>;
          session.acceptPositionState(d, "anchor");
          break;
        }
        case "TOPIC_SWITCH_SUGGESTED": {
          /**
           * 旧路径**已退役**（本轮契约 §2.4）：后端不再发布可执行的切换建议，
           * 相似度只作为本轮主模型的证据，唯一判断入口是受控工具调用产生的
           * 话题申请（APPROVAL_REQUIRED 的 topic_switch / topic_create）。
           *
           * 事件类型保留只为兼容旧后端/旧回放：这里**不建立任何可执行状态**，
           * 不改 Anchor、不亮确认条、不调用任何提交接口。
           */
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
          /**
           * F12：话题操作失败的可观察错误 = 没有成功 → 草稿跟随按失败收口，
           * 不被迟到的成功事件再带走（草稿留在原话题）。
           */
          const failureCode = String(d.code ?? "").trim();
          if (
            failureCode === "topic_ownership_failed" ||
            failureCode === "topic_context_prepare_failed" ||
            failureCode === "topic_operation_failed"
          ) {
            session.clearTopicDraftFollowForOperation({
              operationId: d.operation_id as string | null,
              turnId: tid,
            });
          }
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
          // 实时到达的申请按统一门控自动弹出（输入中/覆盖层/弹窗由 unifiedApprovals 判定）
          this.handleApprovalRequired(approval, { autoOpen: true });
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
        // 「继续/停止」操作条，不进入审批队列。原因可能是预算耗尽（reason = "budget"，
        // 再用 budget_kind 区分「迭代次数」与「输出 token 预算」），也可能是无进展暂停
        // （payload.reason = "no_progress"）——原因与说明一律原样带着，界面不去猜
        // （猜错会把无进展说成「已达迭代上限」，也会把 token 预算说成迭代上限）。
        const payload = (approval.payload ?? {}) as Record<string, unknown>;
        session.pendingContinue = {
          id,
          used: Number(payload.used_iterations ?? 0),
          max: Number(payload.max_iterations ?? 0),
          reason: String(payload.reason ?? ""),
          budgetKind: String(payload.budget_kind ?? ""),
          message: String(payload.message ?? ""),
        };
        return;
      }
      /**
       * R9：新事件带 v2 字段（state / confirmed_stage / requires_confirmations / purpose …）。
       * 原样转成待办字段，弹窗才能展示状态机进度与三次确认；老事件没有这些字段 → 不写。
       */
      const meta =
        typeof approval.state === "string"
          ? approvalMetaFromPending(toPendingApproval(approval))
          : undefined;
      const payload = (approval.payload ?? {}) as Record<string, unknown>;
      useApprovalsStore().enqueue(id, kind || "unknown", payload, {
        // 恢复路径显式传 autoOpen:false（不抢焦点）；实时到达交统一门控决定
        autoOpen: opts.autoOpen ?? true,
        // 绑定信息随待办一起保存：应答时原样回传（服务端另有自己的校验）
        turnId: (approval.turn_id as string | null) ?? null,
        sessionId: (approval.session_id as string | null) ?? null,
        requestDigest: (approval.request_digest as string | null) ?? null,
        ...(meta ? { meta } : {}),
      });
      /**
       * 话题申请（topic_switch / topic_create）：正文与决定入口在统一审批队列，
       * 这里只登记「哪个 approval_id 是话题申请」（legacy 的 APPROVAL_RESULT
       * 不带 kind/payload，结局要靠这份登记认出来）。
       *
       * 不把整轮标成 activity="approval"：等待状态由 session.topicApprovalPending
       * 从统一队列派生；决定后没有额外的「审批结束」事件能把它恢复成生成中。
       */
      if (kind === "topic_switch" || kind === "topic_create") {
        /**
         * 操作身份随申请一起登记（P1-05）：TOPIC_PHASE / 结局事件只按
         * operation_id + turn 归属；旧后端没有这些字段 → 空串 / null，不猜。
         */
        const numberField = (value: unknown): number | null => {
          if (typeof value === "number" && Number.isFinite(value)) return value;
          if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
            return Number(value);
          }
          return null;
        };
        session.registerTopicApplication({
          approvalId: id,
          kind,
          turnId: (approval.turn_id as string | null) ?? null,
          targetTopicId: String(payload.target_topic_id ?? "").trim() || null,
          operationId: String(payload.operation_id ?? approval.operation_id ?? "").trim(),
          paramVersion: numberField(payload.param_version ?? approval.param_version),
          baseNavVersion: numberField(payload.base_nav_version ?? approval.base_nav_version),
          // F12：申请起点（草稿跟随的身份绑定；旧后端缺失 → null，不猜）
          fromTopicId: String(payload.from_topic_id ?? "").trim() || null,
          fromFragmentId: String(payload.from_fragment_id ?? "").trim() || null,
        });
        return;
      }
      session.activity = "approval";
    },

    // -- RESYNC 恢复协议 --------------------------------------------------

    /**
     * 兼容旧入口：RESYNC 恢复协议的**唯一实现**已经收敛到 `stores/restore.ts`
     * 的 `restoreRuntimeState(reason)`（单飞 + generation + 同步期间缓冲事件 +
     * 快照后按序补事件）。这里只做转发，避免再出现第二套恢复语义。
     *
     * 旧行为（单飞、缓冲、快照应用顺序）由唯一入口保持：`resyncing` /
     * `resyncBuffer` / `resyncDroppedEvents` / `flushResyncBuffer` 仍然由本 store
     * 持有（事件路由发生在这一层），状态机与提示由唯一入口驱动。
     */
    async startResync(): Promise<void> {
      await restoreRuntimeState("resync");
      // 手动 / 页面触发的同步同样补读审批生命周期（与 onopen、RESYNC 同一入口）
      await useApprovalsStore().refreshApprovalsAndHistory("resync");
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
      /**
       * R2：权威位置快照（/api/runtime/state）。旧后端没有 anchor 字段 → 不传，
       * 下面保持现状（不伪造位置 / 接续）。
       */
      anchor?: RuntimeAnchorSnapshot | null;
      /** R2：快照所属实例（顶层已有；旧后端可能没有）。 */
      instance_id?: string | null;
      approvals: { approval_id: string; kind: string; payload: Record<string, unknown> }[];
      /**
       * 旧审批中断事实（B-CP1 /api/runtime/state）：只读展示，不是待办。
       * 旧后端没有这个字段 → 不传，store 保持现状。
       */
      interrupted_approvals?: {
        approval_id: string;
        kind?: string;
        what?: string;
        created_at?: string;
        status?: string;
        legacy?: boolean;
        outcome?: string;
        turn_id?: string | null;
        session_id?: string | null;
        request_digest?: string | null;
      }[];
      tasks: { task_id: string; tool: string; status: "queued" | "running" | "done" | "failed"; ok?: boolean | null; content_preview?: string; error?: string | null }[];
      tools?: ToolExecutionSnapshot[];
      /**
       * 每轮结束事实（契约 §1.2）：重连恢复要把 reason / actions 一起带回来 ——
       * 旧后端没有这个字段 → 什么也不写（不伪造原因）；
       * incomplete 与其他终态一样原样带回（前端消费见 services/api.ts::TurnFactsRow）。
       */
      turn_facts?: TurnFactsRow[];
      narratives?: {
        narrative_id?: string;
        turn_id?: string | null;
        kind?: string;
        text?: string;
        calls?: {
          call_id?: string;
          tool?: string;
          title?: string;
          status?: string;
          error?: string | null;
          duration_ms?: number | null;
        }[];
        created_at?: string | null;
      }[];
    }) {
      const session = useSessionStore();
      /**
       * R2：权威位置快照。位置只由 ANCHOR / 这份快照更新（前端不另造位置权威）：
       * 先采用快照，再把同步期间缓冲的位置事件按接受规则补放（restore 负责）。
       * 旧后端没有 anchor 字段时这里什么也不写 —— 保持现状，不伪造。
       */
      if (state.anchor) {
        session.applyAuthoritativePositionSnapshot(state.anchor, state.instance_id ?? null);
      }
      // 每轮结束事实：重连后失败的那一轮仍然说得出为什么（旧记录没有就不写）
      session.applyTurnFactsSnapshot(state.turn_facts);
      useApprovalsStore().reconcile(state.approvals.map((a) => a.approval_id));
      /**
       * 旧审批中断事实（B-CP1）：单独存放、只读展示（结果面板「旧审批中断」区）。
       * 绝不放进入审批队列 —— 它们不是等待用户确认的待办，数进去就是假待办。
       */
      useApprovalsStore().ingestInterruptedLegacy(state.interrupted_approvals);
      /**
       * 「继续 / 停止」操作条也属于待办：快照里已经没有它（事务结束了 / 别处应答了）
       * 就必须本地收口 —— 否则用户会看到一个点不动的假待办。
       */
      const continueIds = new Set(
        state.approvals
          .filter((a) => String(a.kind ?? "") === "continue")
          .map((a) => a.approval_id),
      );
      if (session.pendingContinue && !continueIds.has(session.pendingContinue.id)) {
        session.pendingContinue = null;
      }
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
      // 叙事是历史事实：服务器知道的都要回来，且按 id 去重（不回放到末尾，按时间插入）
      session.mergeNarratives(state.narratives ?? []);
    },
    async sendTest(type: EventType) {
      await publishTestEvent(type, { smoke: Date.now() });
    },
    clear() {
      this.events = [];
    },
  },
});