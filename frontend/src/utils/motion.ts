/**
 * 动画偏好读写：html[data-motion] + localStorage(qio-motion)。
 * 与 utils/theme.ts 同一套做法：持久化的是「偏好」（system/standard/reduced），
 * 应用到 <html> 的是解析后的结果（standard/reduced）。
 *
 * 为什么要落到属性而不只靠 CSS 媒体查询：
 * - 用户可以在系统没有开启「减少动画」时手动选择「减少动画」；
 * - 也可以选择「标准」覆盖系统的减少动画；
 * - 星球相机补间是 JS 的 rAF 动画，CSS 媒体查询管不到它，必须能读到同一份偏好。
 */
export type MotionPreference = "system" | "standard" | "reduced";
export type MotionMode = "standard" | "reduced";

export const MOTION_KEY = "qio-motion";

let memoryPref: MotionPreference | null = null;

function readStored(): MotionPreference | null {
  try {
    const v = localStorage.getItem(MOTION_KEY);
    return v === "system" || v === "standard" || v === "reduced" ? v : null;
  } catch {
    return memoryPref;
  }
}

function writeStored(p: MotionPreference): void {
  memoryPref = p;
  try {
    localStorage.setItem(MOTION_KEY, p);
  } catch {
    // 忽略：内存已兜底，本次会话内仍生效
  }
}

function systemPrefersReduced(): boolean {
  try {
    return window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
  } catch {
    return false;
  }
}

export function getMotionPreference(): MotionPreference {
  return readStored() ?? "system";
}

export function resolveMotion(pref: MotionPreference): MotionMode {
  if (pref === "system") return systemPrefersReduced() ? "reduced" : "standard";
  return pref;
}

/** 只应用（不写偏好）：系统偏好变化时同步 data-motion */
export function applyMotion(mode: MotionMode): MotionMode {
  document.documentElement.dataset.motion = mode;
  return mode;
}

export function setMotionPreference(pref: MotionPreference): MotionMode {
  writeStored(pref);
  return applyMotion(resolveMotion(pref));
}

/** 当前生效的模式：优先读已应用的属性（脚本动画要跟 UI 完全一致） */
export function currentMotionMode(): MotionMode {
  const attr = document.documentElement.getAttribute("data-motion");
  if (attr === "reduced" || attr === "standard") return attr;
  return resolveMotion(getMotionPreference());
}

export function prefersReducedMotion(): boolean {
  return currentMotionMode() === "reduced";
}

/** 监听系统偏好：仅当偏好为 system 时实时同步。返回取消订阅函数。 */
export function watchSystemMotion(): () => void {
  let mq: MediaQueryList | null = null;
  try {
    mq = window.matchMedia?.("(prefers-reduced-motion: reduce)") ?? null;
  } catch {
    mq = null;
  }
  if (!mq) return () => {};
  const onChange = () => {
    if (getMotionPreference() === "system") applyMotion(resolveMotion("system"));
  };
  if (typeof mq.addEventListener === "function") mq.addEventListener("change", onChange);
  else if (typeof mq.addListener === "function") mq.addListener(onChange);
  return () => {
    if (typeof mq?.removeEventListener === "function") mq.removeEventListener("change", onChange);
    else if (typeof mq?.removeListener === "function") mq.removeListener(onChange);
  };
}
