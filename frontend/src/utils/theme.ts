/** 主题读写工具：html[data-theme] + localStorage(qio-theme)（与 v2 设计令牌一致）。 */
export type Theme = "dark" | "light";
export const THEME_KEY = "qio-theme";

/** localStorage 不可用（隐私模式/WebView 抛 SecurityError）时的内存兜底 */
let memoryTheme: Theme | null = null;

function readStored(): Theme | null {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return memoryTheme;
  }
}

function writeStored(t: Theme): void {
  memoryTheme = t;
  try {
    localStorage.setItem(THEME_KEY, t);
  } catch {
    // 忽略：内存已兜底，主题在本次会话内仍生效
  }
}

export function getTheme(): Theme {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  return readStored() ?? "dark";
}

export function setTheme(t: Theme): Theme {
  document.documentElement.dataset.theme = t;
  writeStored(t);
  return t;
}

export function toggleTheme(): Theme {
  return setTheme(getTheme() === "light" ? "dark" : "light");
}
