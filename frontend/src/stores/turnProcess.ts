/**
 * 过程区的展开状态（会话内 + 跨刷新）。
 *
 * 为什么必须放在 store：消息流是虚拟列表，条目按需挂载 / 卸载 ——
 * 组件里的 ref 会在滚出视野时丢掉，用户滚回去会发现自己展开的历史又收起了。
 * 键里带 topic / turn_id / stage_id，跨轮、跨话题都不会串。
 *
 * 跨刷新用 localStorage；存不下（隐私模式 / 配额满）只是不持久，不影响使用。
 */
import { reactive } from "vue";

const STORAGE_KEY = "qio.turnProcess.expanded";
/** 持久化的键上限：只保留最近的一批，避免长期使用后无界增长 */
const LIMIT = 300;

interface Entry {
  open: boolean;
  /** 用户自己开合过：之后的自动展开 / 收起不再覆盖用户的选择 */
  manual: boolean;
}

const state = reactive<Record<string, Entry>>({});
let loaded = false;

function load(): void {
  if (loaded) return;
  loaded = true;
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(STORAGE_KEY) : null;
    if (!raw) return;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return;
    for (const [key, value] of Object.entries(parsed)) {
      if (value === true || value === false) {
        state[key] = { open: value, manual: false };
        continue;
      }
      if (value && typeof value === "object") {
        const row = value as { open?: unknown; manual?: unknown };
        state[key] = { open: row.open === true, manual: row.manual === true };
      }
    }
  } catch {
    // 读不出来就当没有：过程区照常能用
  }
}

function persist(): void {
  try {
    if (typeof localStorage === "undefined") return;
    const keys = Object.keys(state);
    const bounded = keys.slice(Math.max(0, keys.length - LIMIT));
    const out: Record<string, Entry> = {};
    for (const key of bounded) out[key] = state[key] as Entry;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(out));
  } catch {
    // 存不下就不存（不影响本次会话内的展开状态）
  }
}

/** 键：话题 + 轮 + 阶段（阶段为空表示整轮级区域）。 */
export function processKey(
  topicId: string | null | undefined,
  turnId: string,
  stageId = "",
): string {
  return `${topicId ?? "-"}|${turnId || "-"}|${stageId || "-"}`;
}

export function isProcessExpanded(key: string): boolean {
  load();
  return state[key]?.open === true;
}

export function isProcessManual(key: string): boolean {
  load();
  return state[key]?.manual === true;
}

/** 用户自己点开 / 收起：记 manual。 */
export function toggleProcess(key: string, open: boolean): void {
  load();
  state[key] = { open, manual: true };
  persist();
}

/** 自动展开 / 收起（运行中展开、结束收起）：不覆盖用户的手动选择。 */
export function setProcessExpanded(key: string, open: boolean): void {
  load();
  state[key] = { open, manual: false };
  persist();
}

/**
 * 当前阶段**明细**（更早的说明 / 逐项调用）的展开状态。
 *
 * 契约 §1.5：它与整轮的「历史」抽屉**分开管理** —— 键里带 stage_id，
 * 所以「展开历史」不会顺带展开当前阶段明细，反之亦然。
 */
export function stageDetailKey(
  topicId: string | null | undefined,
  turnId: string,
  stageId: string,
): string {
  return processKey(topicId, turnId, stageId);
}

export function isStageDetailOpen(key: string): boolean {
  load();
  return state[key]?.open === true;
}

export function isStageDetailManual(key: string): boolean {
  load();
  return state[key]?.manual === true;
}

/** 用户自己点开 / 收起当前阶段明细：记 manual。 */
export function toggleStageDetail(key: string, open: boolean): void {
  load();
  state[key] = { open, manual: true };
  persist();
}

/** 自动收起当前阶段明细：不覆盖用户的手动选择。 */
export function setStageDetailOpen(key: string, open: boolean): void {
  load();
  state[key] = { open, manual: false };
  persist();
}

/** 测试用：清空会话内状态与持久化记录。 */
export function resetProcessState(): void {
  for (const key of Object.keys(state)) delete state[key];
  loaded = true;
  try {
    if (typeof localStorage !== "undefined") localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 忽略
  }
}
