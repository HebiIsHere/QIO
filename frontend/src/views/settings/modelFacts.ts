/**
 * 「当前生效参数」里的模型事实（跨分区只读展示）。
 *
 * A 不直接调用模型目录 API（契约 §6.4：模型/Key 由 B 的面板负责）。
 * 因此这里留一个**显式的数据入口**：B 的 ModelServicesPanel 在
 * 「目录加载完成」「默认模型或用途保存成功」两个时点调用 `setModelFacts`，
 * 工具与权限里的只读区就能展示真实生效的默认模型与各用途实际使用的模型/Key。
 *
 * 字段契约（与 Lead/B 对齐，2026-10-28）：
 * - `defaultModel`: { credentialId, modelId, connectionName?, providerName? }
 * - `usages[]`: { usageKey, label, followsDefault, credentialId?, modelId?,
 *                  connectionName?, providerName?, resolved? }
 *   - 显式分配的用途用 credentialId/modelId；跟随默认的用途以 `resolved`
 *     （后端解析后的真实目标）为准。
 *   - `connectionName` / `providerName` 只用于**展示**（Key 名称/服务商名）；
 *     没有时界面回退到 credentialId，不猜测、不把 id 伪装成名称。
 *
 * 没有数据时界面如实显示「由「模型与服务」提供，尚未读取」——
 * 不猜测、不展示草稿、不把「已配置」当成「已验证」。
 */
import { reactive } from "vue";

export interface ModelFactsConnection {
  credentialId: string;
  modelId: string;
  /** 展示用：连接/Key 名称（可选；没有时回退到 credentialId） */
  connectionName?: string | null;
  /** 展示用：服务商名称（可选） */
  providerName?: string | null;
}

export interface ModelUsageFact {
  usageKey: string;
  label: string;
  /** 是否跟随默认模型 */
  followsDefault: boolean;
  /** 显式分配的连接/模型（followsDefault=true 时可能为空） */
  credentialId?: string | null;
  modelId?: string | null;
  connectionName?: string | null;
  providerName?: string | null;
  /** 后端解析后的真实目标（跟随默认时等于默认模型） */
  resolved?: { credentialId: string; modelId: string } | null;
  /**
   * 这个用途「现在到底怎么用」的一句话事实（来自后端的 usage note）。
   *
   * video / audio / embedding 不继承默认对话模型、research 没有独立调用路径时，
   * 目标本身就是空的；只写「尚未解析」会让用户以为还没加载完，所以必须有原话。
   */
  note?: string | null;
  /**
   * 分配来源：follow（跟随默认）/ explicit（用户显式指定）/
   * existing（沿用现有处理方式）/ unsupported（未接通）。
   * 只用于展示，不参与任何授权判断。
   */
  status?: "follow" | "explicit" | "existing" | "unsupported" | null;
}

export interface ModelFacts {
  /** 已保存且真实生效的默认模型 */
  defaultModel: ModelFactsConnection | null;
  usages: ModelUsageFact[];
  /** 是否已经由模型面板写入过数据 */
  loaded: boolean;
}

export interface ModelFactsInput {
  defaultModel: ModelFactsConnection | null;
  usages: ModelUsageFact[];
}

export const modelFacts = reactive<ModelFacts>({
  defaultModel: null,
  usages: [],
  loaded: false,
});

export function setModelFacts(next: ModelFactsInput): void {
  modelFacts.defaultModel = next.defaultModel;
  modelFacts.usages = next.usages;
  modelFacts.loaded = true;
}

export function resetModelFacts(): void {
  modelFacts.defaultModel = null;
  modelFacts.usages = [];
  modelFacts.loaded = false;
}
