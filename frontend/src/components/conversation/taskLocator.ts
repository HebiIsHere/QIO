/**
 * 消息流「简短执行记录」→ 左侧任务栏定位的接线桥（B 提供，Lead/A 集成时注册）。
 *
 * 为什么是桥而不是直接 import：左栏任务 store 属于 A（conversationTasks），
 * 消息流属于 B；两边在同一屏上，但代码归属不同。这里只暴露一个可注册的定位函数，
 * 没有注册时按钮不渲染 —— 不出现点不动的假按钮，也不复制一套任务状态。
 *
 * 集成方式（Lead 在 ConversationView.vue 里，A 的 store 就绪后）：
 *   registerExecutionTaskLocator((ref) => taskStore.locateTask(ref));
 * locateTask 返回 true 表示左侧确实定位到了对应任务（已归档任务应定位历史，不打开空详情）。
 */
export interface ExecutionTaskRef {
  /** 任务来源：工具调用 / 独立任务（与契约 TaskSource 对齐） */
  source: "tool" | "subagent";
  /** 来源 id：工具调用 id 或独立任务 id */
  sourceId: string;
  /** 这条执行记录属于哪一轮（归档任务定位历史时可能需要） */
  turnId: string | null;
  /** 展示用标题（找不到任务时给用户一句人话） */
  title?: string;
}

export type ExecutionTaskLocator = (ref: ExecutionTaskRef) => boolean;

let locator: ExecutionTaskLocator | null = null;

export function registerExecutionTaskLocator(fn: ExecutionTaskLocator | null): void {
  locator = fn;
}

export function hasExecutionTaskLocator(): boolean {
  return locator !== null;
}

/** 定位失败（没注册 / 左栏里没有对应任务）返回 false，由调用方如实说明。 */
export function locateExecutionTask(ref: ExecutionTaskRef): boolean {
  if (!locator) return false;
  try {
    return locator(ref) === true;
  } catch {
    return false;
  }
}
