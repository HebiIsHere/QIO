/**
 * 统一的「操作反馈」状态机（第三阶段 spec 第 71~74、80~87、105、112 条）。
 *
 * 为什么需要它：Knowledge / Entity 的同类操作此前三种表现并存 —— 有的用右上角
 * 全局 Toast、有的写进页面顶部消息、有的只 `console.error`（用户看不到任何反馈，
 * 以为操作生效了）。这里把「进行中 / 成功 / 失败」收敛成一套：
 *
 * - 进行中：`busy`（调用方禁用按钮并显示「保存中…」这类文案）；
 * - 成功：短暂 `ok` 后自动回到 `idle`（成功不必长期占据界面）；
 * - 失败：保留 `failed` 与人话原因，**直到下次尝试**（失败需要用户处理，比成功持久）。
 *
 * 反馈位置由调用方决定：本 composable 只管状态，不管渲染 —— 这样每一处都能把
 * 状态显示在**对应的卡片里**，而不是全都堆到全局。
 */
import { ref, type Ref } from "vue";

export type FeedbackState = "idle" | "busy" | "ok" | "failed";

export interface ActionFeedback {
  stateOf: (key: string) => FeedbackState;
  errorOf: (key: string) => string;
  okTextOf: (key: string) => string;
  run: (
    key: string,
    fn: () => Promise<unknown>,
    opts?: { okText?: string; failText?: string },
  ) => Promise<boolean>;
  reset: (key?: string) => void;
}

/** 成功提示停留时长：够看清，又不长期占位 */
const OK_MS = 1600;

export function useActionFeedback(): ActionFeedback {
  const states = ref<Record<string, FeedbackState>>({}) as Ref<Record<string, FeedbackState>>;
  const errors = ref<Record<string, string>>({});
  const okTexts = ref<Record<string, string>>({});
  const timers = new Map<string, ReturnType<typeof setTimeout>>();

  function clearTimer(key: string) {
    const t = timers.get(key);
    if (t) clearTimeout(t);
    timers.delete(key);
  }

  function stateOf(key: string): FeedbackState {
    return states.value[key] ?? "idle";
  }

  function errorOf(key: string): string {
    return errors.value[key] ?? "";
  }

  function okTextOf(key: string): string {
    return okTexts.value[key] ?? "";
  }

  function reset(key?: string) {
    if (key === undefined) {
      states.value = {};
      errors.value = {};
      okTexts.value = {};
      for (const k of timers.keys()) clearTimer(k);
      return;
    }
    clearTimer(key);
    states.value = { ...states.value, [key]: "idle" };
    errors.value = { ...errors.value, [key]: "" };
    okTexts.value = { ...okTexts.value, [key]: "" };
  }

  async function run(
    key: string,
    fn: () => Promise<unknown>,
    opts: { okText?: string; failText?: string } = {},
  ): Promise<boolean> {
    if (stateOf(key) === "busy") return false;
    clearTimer(key);
    states.value = { ...states.value, [key]: "busy" };
    errors.value = { ...errors.value, [key]: "" };
    okTexts.value = { ...okTexts.value, [key]: "" };
    try {
      await fn();
      states.value = { ...states.value, [key]: "ok" };
      okTexts.value = { ...okTexts.value, [key]: opts.okText ?? "已保存" };
      timers.set(
        key,
        setTimeout(() => {
          timers.delete(key);
          // 期间用户可能又发起了新一次操作：只把仍是 ok 的这次收回 idle
          if (states.value[key] === "ok") {
            states.value = { ...states.value, [key]: "idle" };
            okTexts.value = { ...okTexts.value, [key]: "" };
          }
        }, OK_MS),
      );
      return true;
    } catch (e) {
      const reason = (e as Error).message || "未知原因";
      states.value = { ...states.value, [key]: "failed" };
      errors.value = {
        ...errors.value,
        [key]: `${opts.failText ?? "操作没有成功"}：${reason}（可以重试）`,
      };
      return false;
    }
  }

  return { stateOf, errorOf, okTextOf, run, reset };
}
