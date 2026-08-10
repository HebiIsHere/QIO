import { reactive } from "vue";

export type DockId = "composer" | "planet-dock" | "settings-float";
/** 贴靠目标：边（edge 模式）或角（corner 模式） */
export type DockTarget = "left" | "right" | "top" | "bottom" | "tl" | "tr" | "bl" | "br";

export interface FloatingEntry {
  /** 当前元素左上角位置（px，left/top） */
  x: number;
  y: number;
  width: number;
  height: number;
  /** 是否已贴靠（互斥避让仅统计已贴靠组件） */
  docked: boolean;
  /** 贴靠目标；未贴靠为 null */
  dockedTo: DockTarget | null;
  /** 是否允许贴靠隐藏（设置页「窗口管理」开关读写） */
  hideEnabled: boolean;
  /** 当前是否处于隐藏态（细边/淡化） */
  hidden: boolean;
}

export const FLOAT_IDS: DockId[] = ["composer", "planet-dock", "settings-float"];
export const FLOAT_STORAGE_KEY = "qio-float-positions";
/** 贴靠隐藏偏好的独立持久化键：组件未挂载（设置页）时也能保存开关 */
export const FLOAT_HIDE_KEY = "qio-float-hide";

function makeEntry(): FloatingEntry {
  return { x: 0, y: 0, width: 0, height: 0, docked: false, dockedTo: null, hideEnabled: false, hidden: false };
}

/** 共享状态：模块级单例，设置页与各浮动组件共同读写 */
export const floatingState = reactive<Record<DockId, FloatingEntry>>({
  composer: makeEntry(),
  "planet-dock": makeEntry(),
  "settings-float": makeEntry(),
});

export interface PersistedEntry {
  x: number;
  y: number;
  docked: boolean;
  dockedTo: DockTarget | null;
  hideEnabled: boolean;
}
export type PersistedMap = Partial<Record<DockId, PersistedEntry>>;

/** 持久化当前浮动状态到 localStorage（异常时静默降级；未初始化的组件不写入） */
export function savePositions(): void {
  try {
    const data: PersistedMap = {};
    for (const id of FLOAT_IDS) {
      const e = floatingState[id];
      // width<=0 表示该组件从未初始化（未挂载/未测量），避免把占位 0,0 当真实位置恢复
      if (e.width <= 0) continue;
      data[id] = { x: e.x, y: e.y, docked: e.docked, dockedTo: e.dockedTo, hideEnabled: e.hideEnabled };
    }
    localStorage.setItem(FLOAT_STORAGE_KEY, JSON.stringify(data));
  } catch {
    // localStorage 不可用（隐私模式/受限 WebView）：仅本次会话生效
  }
}

/** 读取持久化状态；无数据/解析失败返回空表 */
export function loadPositions(): PersistedMap {
  try {
    const raw = localStorage.getItem(FLOAT_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as PersistedMap;
    const out: PersistedMap = {};
    for (const id of FLOAT_IDS) {
      const p = parsed[id];
      if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) {
        out[id] = {
          x: p.x,
          y: p.y,
          docked: !!p.docked,
          dockedTo: p.dockedTo ?? null,
          hideEnabled: !!p.hideEnabled,
        };
      }
    }
    return out;
  } catch {
    return {};
  }
}

/** 持久化贴靠隐藏偏好（独立键：组件未挂载时也能保存开关，位置键跳过未初始化组件） */
export function saveHidePrefs(): void {
  try {
    const data: Partial<Record<DockId, boolean>> = {};
    for (const id of FLOAT_IDS) data[id] = floatingState[id].hideEnabled;
    localStorage.setItem(FLOAT_HIDE_KEY, JSON.stringify(data));
  } catch {
    // localStorage 不可用：仅本次会话生效
  }
}

/** 读取贴靠隐藏偏好；无数据/解析失败返回空表 */
export function loadHidePrefs(): Partial<Record<DockId, boolean>> {
  try {
    const raw = localStorage.getItem(FLOAT_HIDE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<Record<DockId, boolean>>;
    const out: Partial<Record<DockId, boolean>> = {};
    for (const id of FLOAT_IDS) if (typeof parsed[id] === "boolean") out[id] = parsed[id];
    return out;
  } catch {
    return {};
  }
}

/** 设置某组件是否允许贴靠隐藏；关闭时立即解除隐藏态（组件 watcher 负责视觉还原） */
export function setHideEnabled(id: DockId, enabled: boolean): void {
  floatingState[id].hideEnabled = enabled;
  if (!enabled) floatingState[id].hidden = false;
  savePositions();
  saveHidePrefs();
}

/** 重置全部浮动状态（测试隔离用；设置页也可提供「还原默认布局」入口） */
export function resetFloatPositions(): void {
  for (const id of FLOAT_IDS) {
    const e = floatingState[id];
    e.x = 0;
    e.y = 0;
    e.width = 0;
    e.height = 0;
    e.docked = false;
    e.dockedTo = null;
    e.hidden = false;
    e.hideEnabled = false;
  }
  try {
    localStorage.removeItem(FLOAT_STORAGE_KEY);
    localStorage.removeItem(FLOAT_HIDE_KEY);
  } catch {
    // ignore
  }
}
