/**
 * 运行时状态的**唯一恢复入口**（M12）。
 *
 * 为什么要有它：以前「连接建立」「RESYNC」「后端换实例」各自调不同的入口
 * （`connect().onopen` → `session.resyncTurnState()`；`RESYNC` → `events.startResync()`；
 * 实例变化 → 又一条 `startResync()`）。它们各自持有一半协议，于是：
 * 同步期间到达的实时事件有的被缓冲、有的被直接应用；旧实例的快照可能覆盖新状态；
 * 「已结束」的事项会在快照里复活成一个点不动的假待办。
 *
 * 这个模块把协议收敛成一条：
 *
 * 1. **单飞**：同一时间只跑一轮恢复；期间的重复请求只标记「完成后再来一轮」，
 *    绝不并发第二个快照请求；
 * 2. **generation**：每一轮领一个代次；外部失效（重连 / 显式 reset）会让代次 +1，
 *    迟到的旧结果整段丢弃并自动重来一轮 —— 旧快照不得覆盖新状态；
 * 3. **旧实例丢弃**：快照返回时若本地已经采用了**更新的实例**（例如同步期间
 *    缓冲事件里的 TURN_QUEUE 带来了新实例），这份属于旧实例的快照不应用，直接重来；
 * 4. **同步期间缓冲事件**：同步开始后到达的实时事件先进入缓冲区
 *    （`events.resyncBuffer`，有明确上限），快照应用完再按到达顺序补放 ——
 *    所以「同步期间结束的事项」不会被随后返回的旧快照复活；
 * 5. **覆盖全部权威状态**：turn 队列、待审批、独立任务、工具执行事实、叙事、
 *    上次没执行完的用户消息与「那次操作没执行」的审批，一次拉齐；
 * 6. **可恢复记录收件箱**：快照里的 `orphaned_turns` 与专用接口
 *    `/api/recovery/records` 并成**同一份**清单，并按「已解决 id 集合」过滤 ——
 *    旧快照不得复活已处理记录；专用接口失败时快照里的孤儿仍可见。
 *
 * 首次连接、页面刷新、普通重连、RESYNC、实例变化全部走 `restoreRuntimeState(reason)`。
 */
import { resetBackend } from "../services/backend";
import { api } from "../services/api";
import { RESYNC_NOTICE, RESYNC_NOTICE_DELAY_MS, useEventStore } from "./events";
import { useSessionStore } from "./session";

export type RestoreReason =
  | "connect"
  | "refresh"
  | "reconnect"
  | "resync"
  | "instance-change"
  | (string & {});

export interface RestoreOutcome {
  ok: boolean;
  reason: string;
  /** 本轮结束时生效的代次 */
  generation: number;
  /** 实际完成的快照轮数（不含被丢弃的轮次） */
  rounds: number;
  /** 有结果被丢弃时说明原因（旧代次 / 旧实例） */
  discarded?: "stale-generation" | "stale-instance";
}

/** 恢复代次：每开始一轮 +1；`invalidateRestore()` 也会 +1（让在飞的旧结果作废）。 */
let generation = 0;
/** 当前正在跑的那一轮（单飞句柄）。 */
let running: Promise<RestoreOutcome> | null = null;

let completedRounds = 0;

/** 当前代次（测试与诊断用）。 */
export function restoreGeneration(): number {
  return generation;
}

/** 是否有恢复在飞（单飞状态的只读视图）。 */
export function isRestoring(): boolean {
  return running !== null;
}

/** 已完成（未被丢弃）的快照轮数，诊断用。 */
export function restoreRoundCount(): number {
  return completedRounds;
}

/**
 * 让**在飞的**恢复结果全部作废（代次 +1）。
 *
 * 用在「连接被断开 / 事件流重建 / 页面重新进入」这类外部失效上：旧连接上发出的
 * 快照请求可能带回旧实例、旧 revision 的数据，绝不能覆盖新的本地状态。
 */
export function invalidateRestore(): number {
  generation += 1;
  return generation;
}

/**
 * 恢复入口。并发调用是**单飞**的：后来者只会登记「完成后再来一轮」，
 * 并拿到同一轮的结果。
 */
export async function restoreRuntimeState(reason: RestoreReason = "resync"): Promise<RestoreOutcome> {
  const events = useEventStore();
  if (running) {
    // 单飞：重复请求只做标记（并在本轮结束时按顺序补放缓冲事件）。
    // 注意：这里绝不能建提示定时器 —— 以前每次重复 RESYNC 都会留下一个
    // 没人清理的 graceTimer（稍后可能写出一条过期提示）。
    events._resyncAgain = true;
    return running;
  }

  const session = useSessionStore();
  session.resyncState = "resyncing";
  /**
   * 同步提示只在**真的卡住**时才出现：同步通常几十毫秒就完成，
   * 立刻写提示会被随后清掉、用户什么都看不到。超过宽限期还没完成才说话。
   */
  const notice = RESYNC_NOTICE;
  const graceTimer = setTimeout(() => {
    if (session.resyncState === "resyncing") session.warning = notice;
  }, RESYNC_NOTICE_DELAY_MS);
  events.resyncing = true;

  const outcome = runRestore(reason, notice);
  running = outcome;
  try {
    return await outcome;
  } finally {
    // 顺序固定：先清提示定时器、再解除缓冲，最后才放开单飞锁。
    clearTimeout(graceTimer);
    events.resyncing = false;
    events.flushResyncBuffer();
    running = null;
  }
}

async function runRestore(reason: RestoreReason, notice: string): Promise<RestoreOutcome> {
  const events = useEventStore();
  const session = useSessionStore();
  let rounds = 0;
  let discarded: RestoreOutcome["discarded"];
  try {
    do {
      events._resyncAgain = false;
      // 本轮的溢出计数单独计：中间到过上限就必须再来一轮，不能静默丢事件
      events.resyncDroppedEvents = 0;
      const gen = ++generation;
      const instanceAtStart = session.instanceId;
      const state = await api.getRuntimeState();

      // ---- 归属校验：旧代次 / 旧实例的结果一律丢弃 ------------------
      if (gen !== generation) {
        // 期间发生了外部失效（重连 / 显式作废）→ 这份结果可能来自旧实例，丢弃重来
        discarded = "stale-generation";
        events._resyncAgain = true;
        continue;
      }
      if (
        instanceAtStart !== null &&
        session.instanceId !== null &&
        session.instanceId !== instanceAtStart &&
        state.instance_id !== session.instanceId
      ) {
        // 本地已经采用了一个**更新的实例**（缓冲区里的 TURN_QUEUE / TURN_START 带来了它）：
        // 这份旧实例的快照不能把它盖回去。
        discarded = "stale-instance";
        events._resyncAgain = true;
        continue;
      }

      const instanceBefore = session.instanceId;
      session.adoptInstance(state.instance_id);
      // 后端重启（实例变化）→ 地址与令牌都可能变：下一次请求重新解析
      if (instanceBefore && session.instanceId !== instanceBefore) resetBackend();

      // ---- 快照是权威：整份应用（队列先于其它状态，保证「在跑」的归属正确） ----
      session.applyTurnQueue(state.turn_queue);
      events.applyRuntimeState(state);
      // 上次没执行完的用户消息与「那次操作没执行」的审批（与队列同一份快照）。
      // 第三个参数是孤儿记录：它与 interrupted_turns 是两个出口，合并进同一份收件箱 ——
      // 专用接口失败时它们也必须在界面上看得见。
      session.applyInterruptedState(
        state.interrupted_approvals ?? [],
        state.interrupted_turns ?? [],
        state.orphaned_turns ?? [],
      );
      /**
       * 收件箱（A01 / A03 的专用接口）。它是**同一批事项的权威清单**，
       * 所以在这里一并拉齐；失败时 `loadRecoveryInbox` 内部已经把原因写在
       * `session.recoveryError` 上并保留快照里的孤儿 —— 不会把「拉不到」
       * 擦成「没有未完成的事」，也不会因为一个补充请求打断整个恢复。
       */
      await session.loadRecoveryInbox();
      // 开发任务是另一份权威状态（独立接口）：连上/抖动之后一起拉齐
      await session.refreshDevTasks();

      // 快照应用完，再按到达顺序补放同步期间缓冲的实时事件
      events.flushResyncBuffer();
      rounds += 1;
      completedRounds += 1;
    } while (events._resyncAgain);

    session.resyncState = "normal";
    // 只撤下**这条同步提示**：同步期间新到的提醒不能被顺手抹掉
    if (session.warning === notice) session.warning = null;
    return { ok: true, reason: String(reason), generation, rounds, discarded };
  } catch (e) {
    // 失败必须如实说：界面显示的状态可能已经不是最新的
    session.resyncState = "failed";
    if (session.warning === notice) session.warning = null;
    session.lastError = `状态同步失败，界面显示的状态可能不是最新的：${(e as Error).message}`;
    return { ok: false, reason: String(reason), generation, rounds, discarded };
  }
}
