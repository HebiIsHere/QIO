/**
 * 浏览器侧的固定响应（fixture）：全部是本机构造的数据，不含任何真实 Key / 真实调用。
 *
 * 覆盖：
 * - GET /api/models/catalog：一把 Key 多模型、默认模型、13 个用途（形状按 _STG-COMMON.md §6.3）
 * - GET /api/credentials：四张不同状态的 Key 卡（让「删除 / 撤销」确认层有入口）
 * - GET /api/settings/search/status：搜索通道状态（按 C 的契约）
 *
 * 只挡 GET；写请求永远打到真实后端（或由交互脚本单独注入失败/延迟），
 * 这样「真的保存了」这类结论不会建立在 fixture 上。
 */

const NOW = "2026-10-28T03:00:00.000Z";

export const CREDENTIALS = [
  {
    key_id: "cred_primary",
    version: 3,
    tags: ["main-loop", "subagent"],
    endpoint: "https://api.example-primary.invalid/v1",
    default_model: "example-chat-2",
    budget: 2000000,
    budget_used: 420000,
    usage_input: 300000,
    usage_output: 120000,
    status: "active",
    enabled: true,
    note: "主力连接",
    kind: "openai",
    verify_state: "verified",
    verified_at: NOW,
    verify_error: null,
    is_default: true,
    provider_id: "example-primary",
    provider_name: "示例服务商甲",
  },
  {
    key_id: "cred_budget",
    version: 1,
    tags: ["vision"],
    endpoint: "https://api.example-secondary.invalid/v1",
    default_model: "example-vision-1",
    budget: 100000,
    budget_used: 91000,
    usage_input: 80000,
    usage_output: 11000,
    status: "active",
    enabled: true,
    note: "看图专用",
    kind: "openai",
    verify_state: "verified",
    verified_at: NOW,
    verify_error: null,
    is_default: false,
    provider_id: "example-secondary",
    provider_name: "示例服务商乙",
  },
  {
    key_id: "cred_disabled",
    version: 1,
    tags: [],
    endpoint: "https://api.example-primary.invalid/v1",
    default_model: null,
    budget: null,
    budget_used: 0,
    status: "active",
    enabled: false,
    note: null,
    kind: "openai",
    verify_state: "unverified",
    verified_at: null,
    verify_error: null,
    is_default: false,
    provider_id: "example-primary",
    provider_name: "示例服务商甲",
  },
  {
    key_id: "cred_revoked",
    version: 2,
    tags: ["research"],
    endpoint: null,
    default_model: null,
    budget: null,
    budget_used: 12,
    status: "revoked",
    enabled: false,
    note: "已撤销的旧连接",
    kind: null,
    verify_state: "legacy",
    verified_at: null,
    verify_error: null,
    is_default: false,
    provider_id: null,
    provider_name: null,
  },
];

export const MODEL_CATALOG = {
  connections: [
    {
      credential_id: "cred_primary",
      name: "主力连接",
      endpoint: "https://api.example-primary.invalid/v1",
      kind: "openai",
      provider_id: "example-primary",
      provider_name: "示例服务商甲",
      enabled: true,
      status: "active",
      verify_state: "verified",
      budget: 2000000,
      budget_used: 420000,
      budget_left: 1580000,
      models: [
        { model_id: "example-chat-2", display_name: "示例对话 2", source: "discovered", capabilities: ["chat"], verify_state: "verified", verified_at: NOW, verify_error: null, usable: true, unavailable_reason: "" },
        { model_id: "example-chat-1", display_name: "示例对话 1", source: "discovered", capabilities: ["chat"], verify_state: "verified", verified_at: NOW, verify_error: null, usable: true, unavailable_reason: "" },
        { model_id: "example-reasoner", display_name: "示例推理", source: "discovered", capabilities: ["chat", "reasoning"], verify_state: "unverified", verified_at: null, verify_error: null, usable: true, unavailable_reason: "" },
      ],
    },
    {
      credential_id: "cred_budget",
      name: "看图专用",
      endpoint: "https://api.example-secondary.invalid/v1",
      kind: "openai",
      provider_id: "example-secondary",
      provider_name: "示例服务商乙",
      enabled: true,
      status: "active",
      verify_state: "verified",
      budget: 100000,
      budget_used: 91000,
      budget_left: 9000,
      models: [
        { model_id: "example-vision-1", display_name: "示例视觉 1", source: "discovered", capabilities: ["chat", "vision"], verify_state: "verified", verified_at: NOW, verify_error: null, usable: true, unavailable_reason: "" },
      ],
    },
  ],
  default: { credential_id: "cred_primary", model_id: "example-chat-2" },
  usages: [
    { usage_key: "main-chat", label: "主对话", kind: "chat", group: "primary", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "general-chat", label: "通用对话", kind: "chat", group: "primary", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "code", label: "写代码", kind: "chat", group: "primary", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "subagent", label: "子任务", kind: "chat", group: "primary", model_id: "example-chat-1", credential_id: "cred_primary", follows_default: false, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-1", source: "explicit", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "显式指定", delivery: "model" },
    { usage_key: "vision", label: "看图", kind: "vision", group: "special", model_id: "example-vision-1", credential_id: "cred_budget", follows_default: false, inherits_ok: true, resolved: { credential_id: "cred_budget", model_id: "example-vision-1", source: "explicit", usable: true, unavailable_reason: "", credential_name: "看图专用" }, note: "显式指定", delivery: "model" },
    { usage_key: "video", label: "视频", kind: "video", group: "special", model_id: null, credential_id: null, follows_default: false, inherits_ok: false, resolved: null, note: "沿用现有处理方式", delivery: "existing" },
    { usage_key: "audio", label: "语音", kind: "audio", group: "special", model_id: null, credential_id: null, follows_default: false, inherits_ok: false, resolved: null, note: "沿用现有处理方式", delivery: "existing" },
    { usage_key: "research", label: "研究", kind: "chat", group: "primary", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "未接通独立调用路径", delivery: "unsupported" },
    { usage_key: "embedding", label: "向量与嵌入", kind: "embedding", group: "special", model_id: null, credential_id: null, follows_default: false, inherits_ok: false, resolved: null, note: "沿用现有处理方式", delivery: "existing" },
    { usage_key: "memory-organize", label: "记忆整理", kind: "background", group: "background", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "knowledge-distill", label: "知识提炼", kind: "background", group: "background", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "entity-extract", label: "实体提炼", kind: "background", group: "background", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "custom:local", label: "标签：local", kind: "custom", group: "legacy", model_id: null, credential_id: null, follows_default: false, inherits_ok: false, resolved: null, note: "历史自定义标签", delivery: "model" },
  ],
  discovery: {
    cred_primary: { checked_at: NOW, error: null, source: "provider", provider_id: "example-primary", model_count: 3 },
    cred_budget: { checked_at: null, error: null, source: null, provider_id: "example-secondary", model_count: 1 },
  },
};

export const SEARCH_STATUS = {
  channels: [
    { id: "keyless", label: "免密钥检索（Exa / Parallel / DuckDuckGo）", configured: true, usable: true, checked_at: NOW, checking: false, reason_code: null, message: "" },
    { id: "bocha", label: "博查 API Key", configured: false, usable: false, checked_at: null, checking: false, reason_code: "not_configured", message: "未配置" },
    { id: "searxng", label: "自建 SearXNG", configured: false, usable: false, checked_at: null, checking: false, reason_code: "not_configured", message: "未配置" },
  ],
};

/**
 * 装上只读 fixture。返回计数句柄与卸载函数；写请求不在这里处理。
 */
export async function installFixtures(browser) {
  const hits = { catalog: 0, credentials: 0, searchStatus: 0 };
  const off = [];
  off.push(await browser.route({ method: "GET", path: /\/api\/models\/catalog/ }, () => {
    hits.catalog += 1;
    return { body: MODEL_CATALOG, headers: { "Cache-Control": "no-store" } };
  }));
  off.push(await browser.route({ method: "GET", path: /\/api\/credentials(\?|$)/ }, () => {
    hits.credentials += 1;
    return { body: { credentials: CREDENTIALS, default_key_id: "cred_primary" } };
  }));
  off.push(await browser.route({ method: "GET", path: /\/api\/settings\/search\/status/ }, () => {
    hits.searchStatus += 1;
    return { body: SEARCH_STATUS };
  }));
  return {
    hits,
    uninstall: async () => {
      for (const remove of off) remove();
      await browser.clearRoutes();
    },
  };
}
