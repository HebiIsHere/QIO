/**
 * 主题读写工具：html[data-theme] + localStorage(qio-theme)（与 v2 设计令牌一致）。
 * 持久化的是「偏好」（system/dark/light），而不是解析后的当前主题：
 * 偏好为 system 时跟随 prefers-color-scheme，系统切换无需刷新。
 */
export type Theme = "dark" | "light";
export type ThemePreference = "system" | Theme;
export const THEME_KEY = "qio-theme";

/** localStorage 不可用（隐私模式/WebView 抛 SecurityError）时的内存兜底 */
let memoryPref: ThemePreference | null = null;

function readStored(): ThemePreference | null {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" || v === "system" ? v : null;
  } catch {
    return memoryPref;
  }
}

function writeStored(p: ThemePreference): void {
  memoryPref = p;
  try {
    localStorage.setItem(THEME_KEY, p);
  } catch {
    // 忽略：内存已兜底，主题在本次会话内仍生效
  }
}

/** 系统是否偏好深色；matchMedia 缺失（jsdom / 受限 WebView）时按深色兜底 */
function systemPrefersDark(): boolean {
  try {
    return window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ?? true;
  } catch {
    return true;
  }
}

/** 偏好 → 实际主题 */
export function resolveTheme(pref: ThemePreference): Theme {
  if (pref === "system") return systemPrefersDark() ? "dark" : "light";
  return pref;
}

export function getThemePreference(): ThemePreference {
  return readStored() ?? "system";
}

/** 只应用（不写偏好）：系统主题变化时用这个同步 data-theme */
export function applyTheme(t: Theme): Theme {
  document.documentElement.dataset.theme = t;
  return t;
}

/** 显式选择主题：同时写入偏好 */
export function setTheme(t: Theme): Theme {
  writeStored(t);
  return applyTheme(t);
}

/** 设置偏好（system 时按当前系统解析）并立即应用 */
export function setThemePreference(pref: ThemePreference): Theme {
  writeStored(pref);
  return applyTheme(resolveTheme(pref));
}

export function getTheme(): Theme {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  const stored = readStored();
  if (stored === "light" || stored === "dark") return stored;
  return resolveTheme(stored ?? "system");
}

export function toggleTheme(): Theme {
  return setTheme(getTheme() === "light" ? "dark" : "light");
}

/**
 * 监听系统主题：偏好为 system 时实时同步 data-theme，无需刷新。
 * 返回取消订阅函数。
 */
export function watchSystemTheme(): () => void {
  let mq: MediaQueryList | null = null;
  try {
    mq = window.matchMedia?.("(prefers-color-scheme: dark)") ?? null;
  } catch {
    mq = null;
  }
  if (!mq) return () => {};
  const onChange = () => {
    if (getThemePreference() === "system") applyTheme(resolveTheme("system"));
  };
  if (typeof mq.addEventListener === "function") mq.addEventListener("change", onChange);
  else if (typeof mq.addListener === "function") mq.addListener(onChange);
  return () => {
    if (typeof mq?.removeEventListener === "function") mq.removeEventListener("change", onChange);
    else if (typeof mq?.removeListener === "function") mq.removeListener(onChange);
  };
}
