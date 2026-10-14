/**
 * 临时提示的生命周期原语（0.2.5 回归修复 §二）。
 *
 * 为什么要有这个原语：附件成功提示曾在 AddMenu 与 BoardCard 里各写一遍
 * 「设 status = ok / error / busy」，没有任何自动清除与竞态处理 —— 成功提示会一直挂在那里，
 * 而且两次操作叠加时旧操作的迟到回调会覆盖新提示。这里把「谁有权写提示」集中成一处：
 *
 * - **busy（进行中）**：真实进度一直显示到被替换 / 取消，绝不参与自动消失
 *   （等待选择、上传、处理期间不能被成功提示的计时器提前藏掉）。
 * - **ok（成功）**：显示 okMs（默认 3000ms）后自动移除；移除的只是这条**提示**，
 *   卡片、附件关联、已保存副本、预览能力与卡片内必要状态都不受影响。
 * - **error（失败）**：不自动消失；可由用户关闭，或由重新操作替换，可带一个明确的重新操作入口。
 * - **每次 `begin()` 都是新一代**：清掉上一条提示与它的计时器，并让此前所有迟到回调失效。
 * - **卸载即失效**：组件卸载时清计时器、清提示，此后到达的迟到结果写不进任何状态。
 *
 * 所有写入口（progress / succeed / fail）都要求传入 `begin()` 返回的 token，
 * 并返回「这次写入是否生效」—— 调用方据此知道自己的结果是不是已经过期。
 */
import { getCurrentInstance, onBeforeUnmount, ref, type Ref } from "vue";

export type NoticeTone = "busy" | "ok" | "error";

/** 失败提示上的「明确重新操作方式」：文字 + 一个真的会重新发起那次操作的入口。 */
export interface NoticeRetry {
  label: string;
  run: () => void;
}

export interface NoticeState {
  tone: NoticeTone;
  text: string;
  /** 只有失败（且调用方提供了）才有；成功 / 进行中永不带重新操作入口 */
  retry?: NoticeRetry;
}

export interface TransientNotice {
  /** 当前提示；null = 没有提示 */
  notice: Ref<NoticeState | null>;
  /** 开始一次新操作：作废旧提示、旧计时器与旧 token，返回本次操作的 token */
  begin: () => number;
  /** 这次结果是否仍属于当前操作（false = 过期结果，调用方必须放弃写状态 / 提示） */
  isCurrent: (token: number) => boolean;
  /** 真实进度：一直显示，不自动消失 */
  progress: (token: number, text: string) => boolean;
  /** 成功：显示 okMs 后自动移除 */
  succeed: (token: number, text: string) => boolean;
  /** 失败：不自动消失，可带重新操作入口 */
  fail: (token: number, text: string, retry?: NoticeRetry) => boolean;
  /** 用户主动关闭当前提示（关闭入口用） */
  dismiss: () => void;
  /** 立即清掉计时器与提示，并让此后一切迟到回调失效（卸载 / 放弃操作时用） */
  dispose: () => void;
}

export interface UseTransientNoticeOptions {
  /** 成功提示的存活时间（ms），默认 3000 */
  okMs?: number;
}

/** 成功提示默认存活约 3 秒（任务书 §二.1） */
export const NOTICE_OK_MS = 3000;

export function useTransientNotice(options: UseTransientNoticeOptions = {}): TransientNotice {
  const okMs = Number.isFinite(options.okMs) && (options.okMs as number) >= 0 ? (options.okMs as number) : NOTICE_OK_MS;
  const notice = ref<NoticeState | null>(null);
  let token = 0;
  /** 每次状态写入都 +1：计时器只在「还是它调度的那一次」时才清 */
  let serial = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let disposed = false;

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  /** 作废当前代：清计时器、清提示。旧 token 从此写不进来。 */
  function invalidate(): void {
    clearTimer();
    serial += 1;
  }

  function begin(): number {
    invalidate();
    notice.value = null;
    token += 1;
    return token;
  }

  function isCurrent(candidate: number): boolean {
    return !disposed && candidate === token;
  }

  function write(candidate: number, next: NoticeState): boolean {
    if (!isCurrent(candidate)) return false;
    serial += 1;
    clearTimer();
    notice.value = next;
    return true;
  }

  function progress(candidate: number, text: string): boolean {
    return write(candidate, { tone: "busy", text });
  }

  function succeed(candidate: number, text: string): boolean {
    const mine = serial + 1;
    if (!write(candidate, { tone: "ok", text })) return false;
    // 只清「这一次的成功提示」：期间任何新操作 / 新提示都会让 serial 变化，计时器自然失效
    const scheduled = mine;
    timer = setTimeout(() => {
      timer = null;
      if (disposed || serial !== scheduled) return;
      notice.value = null;
    }, okMs);
    return true;
  }

  function fail(candidate: number, text: string, retry?: NoticeRetry): boolean {
    return write(candidate, retry ? { tone: "error", text, retry } : { tone: "error", text });
  }

  function dismiss(): void {
    invalidate();
    notice.value = null;
  }

  function dispose(): void {
    disposed = true;
    token += 1;
    invalidate();
    notice.value = null;
  }

  if (getCurrentInstance()) onBeforeUnmount(() => dispose());

  return { notice, begin, isCurrent, progress, succeed, fail, dismiss, dispose };
}
