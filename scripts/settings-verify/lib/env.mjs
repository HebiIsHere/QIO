/**
 * 设置页验收脚本的共享路径 / 端口 / 约定（唯一来源）。
 */
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(here, "..", "..", "..");
export const SCRIPTS = resolve(here, "..");
export const STATE_DIR = join(SCRIPTS, ".state");
export const EVIDENCE_DIR = join(SCRIPTS, "evidence");
export const TEMP_ROOT = process.env.QIO_SETTINGS_TEMP || join(process.env.TEMP || ".", "qio-settings-verify");
export const SHOTS_DIR = join(REPO, "docs", "verification-shots-settings");
export const VIDEO_DIR = join(REPO, "docs", "verification-video-settings");
export const DEFAULT_APP_PORT = 5106;
export const DEFAULT_BACKEND_PORT = 8756;
export const DEFAULT_CDP_PORT = 9806;
export const DEFAULT_VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 800, height: 600 },
  { width: 480, height: 600 },
];
export const SETTINGS_CATEGORIES = [
  { id: "regular", label: "常规", aliases: ["常规", "外观", "通用"] },
  { id: "models", label: "模型与服务", aliases: ["模型与服务", "模型与联网", "模型"] },
  { id: "memory", label: "记忆与整理", aliases: ["记忆与整理", "对话与记忆", "记忆"] },
  { id: "tools", label: "工具与权限", aliases: ["工具与权限", "工具"] },
  { id: "about", label: "关于与更新", aliases: ["关于与更新", "关于"] },
];
export const BROWSER_CANDIDATES = [
  process.env.QIO_SETTINGS_BROWSER,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].filter(Boolean);
export function browserPath() {
  for (const candidate of BROWSER_CANDIDATES) if (existsSync(candidate)) return candidate;
  throw new Error("找不到 Edge / Chrome：用 QIO_SETTINGS_BROWSER 指定可执行文件");
}
export function dataDir(label) {
  return join(TEMP_ROOT, "data", label || "d");
}
export function logDir(label) {
  return join(TEMP_ROOT, "logs", label || "d");
}
export function stateFile(label) {
  return join(STATE_DIR, (label || "d") + ".json");
}
export function stamp(date = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return date.getFullYear() + p(date.getMonth() + 1) + p(date.getDate()) + "-" +
    p(date.getHours()) + p(date.getMinutes()) + p(date.getSeconds());
}
