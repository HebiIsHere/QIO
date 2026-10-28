/**
 * 设置搜索的「定位请求」通道（shell → 各分区/折叠组）。
 *
 * 为什么走模块级响应式对象而不是 provide/inject：
 * - 折叠组藏在各分区组件里，provide 会把「搜索」与「分区实现」绑死在同一条组件链上；
 * - 搜索定位要求**先切分类、再展开、再定位**，中间跨越了导航与多个懒展开组件，
 *   一个稳定的小状态比一串事件冒泡更好验证（测试也直接读它）。
 *
 * 请求只描述「目标 key + 需要展开的折叠组」，不携带任何用户数据；
 * 组件按需打开，SettingsView 负责滚动与短暂高亮。
 */
import { reactive } from "vue";

export interface RevealRequest {
  /** 目标设置项的 data-settings-key */
  key: string;
  /** 目标所在的折叠组 id（如 memory.advanced）；不在折叠里时为 null */
  fold: string | null;
  /** 每次请求递增，保证「同一个目标再点一次」也会重新触发 */
  token: number;
}

export const settingsReveal = reactive<{ target: RevealRequest | null }>({ target: null });

let revealToken = 0;

export function requestSettingsReveal(key: string, fold: string | null = null): void {
  revealToken += 1;
  settingsReveal.target = { key, fold, token: revealToken };
}

export function clearSettingsReveal(): void {
  settingsReveal.target = null;
}
