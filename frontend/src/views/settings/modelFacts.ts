/**
 * 「当前生效参数」里的模型事实（跨分区只读展示）。
 *
 * A 不直接调用模型目录 API（契约 §6.4：模型/Key 由 B 的面板负责）。
 * 因此这里留一个**显式的数据入口**：B 的 ModelServicesPanel 在
 * 「目录加载完成」「默认模型或用途保存成功」两个时点调用 `setModelFacts`，
 * 工具与权限里的只读区就能展示真实生效的默认模型与各用途实际使用的模型/Key。
 *
 * 没有数据时界面如实显示「由「模型与服务」提供，尚未读取」——
 * 不用猜测、不展示草稿、不把「已配置」当成「已验证」。
 */
import { reactive } from "vue";

export interface ModelUsageFact {
  usageKey: string;
  label: string;
  /** 真实解析到的模型；null 表示尚未解析 */
  modelId: string | null;
  /** 真实解析到的连接名称；null 表示尚未解析 */
  connectionName: string | null;
  /** 是否跟随默认模型 */
  followsDefault: boolean;
}

export interface ModelFacts {
  /** 已保存且真实生效的默认模型 */
  defaultModel: { connectionName: string; modelId: string } | null;
  usages: ModelUsageFact[];
  /** 是否已经由模型面板写入过数据 */
  loaded: boolean;
}

export const modelFacts = reactive<ModelFacts>({
  defaultModel: null,
  usages: [],
  loaded: false,
});

export function setModelFacts(next: Omit<ModelFacts, "loaded">): void {
  modelFacts.defaultModel = next.defaultModel;
  modelFacts.usages = next.usages;
  modelFacts.loaded = true;
}

export function resetModelFacts(): void {
  modelFacts.defaultModel = null;
  modelFacts.usages = [];
  modelFacts.loaded = false;
}
