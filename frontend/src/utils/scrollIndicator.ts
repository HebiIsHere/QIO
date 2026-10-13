/**
 * 滚动指示条偏好读写：html[data-scrollbar] + localStorage(qio-scrollbar)。
 *
 * 与 utils/motion.ts 同一套做法：持久化的是「偏好」（auto/always），
 * 应用到 <html> 的是解析后的结果 —— CSS 与 JS（指示条引擎）读同一份结果。
 *
 * 语义边界（用户规则）：
 * - auto：默认。指示条只在「实际溢出 + 用户正在滚动/靠近边缘/键盘使用」时出现；
 * - always：允许显示且实际溢出的区域常驻显示；**不会**让 hidden 策略的区域（板面、
 *   输入软换行区、对话横向）出现滚动条，也不开放任何横向滚动。
 */
export type ScrollbarPreference = "auto" | "always";

export const SCROLLBAR_KEY = "qio-scrollbar";

let memoryPref: ScrollbarPreference | null = null;
const listeners = new Set<(p: ScrollbarPreference) => void>();

function readStored(): ScrollbarPreference | null {
  try {
    const v = localStorage.getItem(SCROLLBAR_KEY);
    return v === "auto" || v === "always" ? v : null;
  } catch {
    return memoryPref;
  }
}

function writeStored(p: ScrollbarPreference): void {
  memoryPref = p;
  try {
    localStorage.setItem(SCROLLBAR_KEY, p);
  } catch {
    // 忽略：内存已兜底，本次会话内仍生效
  }
}

export function getScrollbarPreference(): ScrollbarPreference {
  return readStored() ?? "auto";
}

/** 只应用（不写偏好）：启动时把持久化偏好落到 <html>。 */
export function applyScrollbarPreference(pref: ScrollbarPreference): ScrollbarPreference {
  document.documentElement.dataset.scrollbar = pref;
  return pref;
}

export function setScrollbarPreference(pref: ScrollbarPreference): ScrollbarPreference {
  writeStored(pref);
  applyScrollbarPreference(pref);
  for (const cb of [...listeners]) {
    try {
      cb(pref);
    } catch {
      // 单个订阅者出错不影响其它区域更新
    }
  }
  return pref;
}

/** 当前生效的偏好：优先读已应用的属性（指示条引擎要跟 UI 完全一致）。 */
export function currentScrollbarPreference(): ScrollbarPreference {
  const attr =
    typeof document !== "undefined" ? document.documentElement.getAttribute("data-scrollbar") : null;
  if (attr === "auto" || attr === "always") return attr;
  return getScrollbarPreference();
}

/** 「始终显示」：只有它会让允许显示的区域常驻指示条。 */
export function scrollIndicatorAlwaysOn(): boolean {
  return currentScrollbarPreference() === "always";
}

/** 订阅偏好变化。返回取消订阅函数（组件卸载时必须调用）。 */
export function subscribeScrollbarPreference(cb: (p: ScrollbarPreference) => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}
