/** 主题读写工具：html[data-theme] + localStorage(qio-theme)（与 v2 设计令牌一致）。 */
export type Theme = "dark" | "light";
export const THEME_KEY = "qio-theme";

export function getTheme(): Theme {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
}

export function setTheme(t: Theme): Theme {
  document.documentElement.dataset.theme = t;
  localStorage.setItem(THEME_KEY, t);
  return t;
}

export function toggleTheme(): Theme {
  return setTheme(getTheme() === "light" ? "dark" : "light");
}
