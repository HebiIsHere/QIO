<script setup lang="ts">
/**
 * 一条连接（一把 Key）的详情卡：概览 / 模型 / 管理 / 用量与记录。
 *
 * 为什么分页签：以前一张卡上常驻七八个按钮与两行技术字段，读一行要在一堆操作里找。
 * 现在默认只给「结论 + 模型」，管理动作集中在「管理」，账本与审计集中在「用量与记录」。
 *
 * 四个状态必须分开显示（不许用一个绿点概括）：
 *   配置已保存 / Key 已启用 / 模型发现（上次检查）/ 模型调用验证（Key 级与按模型）。
 */
import { computed, ref, watch } from "vue";
import QIcon from "../ui/QIcon.vue";
import { api, type CredentialMeta } from "../../services/api";
import type { ModelConnection, UsageAssignment } from "../../services/modelCatalog";

const props = defineProps<{
  connection: ModelConnection;
  credential: CredentialMeta | null;
  usages: UsageAssignment[];
  discovery: { checkedAt: string | null; error: string | null; source: string | null; reasonCode?: string | null } | null;
  defaultModel: { credentialId: string; modelId: string } | null;
  busy?: boolean;
}>();
const emit = defineEmits<{
  edit: [];
  rotate: [];
  verifyKey: [];
  toggle: [];
  revoke: [];
  remove: [];
  discover: [refresh: boolean];
  verifyModel: [modelId: string];
}>();

type Tab = "overview" | "models" | "manage" | "usage";
const tab = ref<Tab>("overview");
const tabs: { id: Tab; label: string }[] = [
  { id: "overview", label: "概览" },
  { id: "models", label: "模型" },
  { id: "manage", label: "管理" },
  { id: "usage", label: "用量与记录" },
];

const c = computed(() => props.connection);
const meta = computed(() => props.credential);

const providerLabel = computed(() => c.value.providerName || "自定义 / 未知服务");
const protocolLabel = computed(() =>
  c.value.kind === "anthropic" ? "Anthropic 接口" : "OpenAI 兼容接口",
);
const statusText = computed(() => {
  if (c.value.status === "revoked") return "已撤销";
  if (c.value.status === "expired") return "已过期";
  return c.value.enabled ? "已启用" : "已停用";
});
const statusTone = computed(() => {
  if (c.value.status === "revoked" || c.value.status === "expired") return "err";
  return c.value.enabled ? "ok" : "warn";
});
const verifyText = computed(() => {
  switch (c.value.verifyState) {
    case "verified":
      return "Key 已验证可用";
    case "failed":
      return "Key 验证未通过";
    case "legacy":
      return "旧凭据（未按当前配置验证）";
    default:
      return "Key 尚未验证";
  }
});
const verifyTone = computed(() => {
  if (c.value.verifyState === "verified") return "ok";
  if (c.value.verifyState === "failed") return "err";
  return "quiet";
});

const usableModels = computed(() => c.value.models.filter((m) => m.usable).length);
const verifiedModels = computed(
  () => c.value.models.filter((m) => m.verifyState === "verified").length,
);
const referencedUsages = computed(() =>
  props.usages.filter(
    (usage) => usage.credentialId === c.value.credentialId && usage.modelId,
  ),
);
const defaultModelHere = computed(() =>
  props.defaultModel?.credentialId === c.value.credentialId ? props.defaultModel : null,
);

const budget = computed(() => c.value.budget);
const hasBudget = computed(() => budget.value != null && budget.value > 0);
const budgetPct = computed(() => {
  if (!hasBudget.value) return 0;
  return Math.min(100, Math.round((c.value.budgetUsed / (budget.value ?? 1)) * 100));
});
const usageInput = computed(() => meta.value?.usage_input ?? 0);
const usageOutput = computed(() => meta.value?.usage_output ?? 0);

function formatTokens(value: number): string {
  return value.toLocaleString("zh-CN");
}

const discoveryText = computed(() => {
  const state = props.discovery;
  if (!state || !state.checkedAt) return "还没有获取过模型";
  const when = state.checkedAt.replace("T", " ").slice(0, 19);
  if (state.error) return "上次检查失败：" + state.error;
  return "上次检查：" + when;
});

// 审计按需加载：只进「用量与记录」时读一次，避免每次展开都打接口。
const audit = ref<{ action: string; from_version: number | null; to_version: number | null; triggered_by: string | null; created_at: string }[]>([]);
const auditState = ref<"idle" | "loading" | "done" | "failed">("idle");
async function loadAudit() {
  if (auditState.value === "loading" || auditState.value === "done") return;
  auditState.value = "loading";
  try {
    const reply = await api.getCredentialAudit(c.value.credentialId);
    audit.value = reply.audit;
    auditState.value = "done";
  } catch {
    auditState.value = "failed";
  }
}
watch(tab, (value) => {
  if (value === "usage") void loadAudit();
});
</script>

<template>
  <article class="conn">
    <header class="conn__head">
      <div class="conn__title">
        <span class="conn__name">{{ c.name }}</span>
        <span class="qio-state" :class="statusTone">{{ statusText }}</span>
        <span class="qio-state" :class="verifyTone">{{ verifyText }}</span>
        <span v-if="defaultModelHere" class="qio-state info">默认模型在此</span>
      </div>
      <div class="conn__facts mono">
        <span>{{ providerLabel }}</span>
        <span class="dot">·</span>
        <span>{{ c.endpoint || "—" }}</span>
        <span class="dot">·</span>
        <span>{{ protocolLabel }}</span>
      </div>
    </header>

    <nav class="tabs" aria-label="连接详情">
      <button
        v-for="item in tabs"
        :key="item.id"
        type="button"
        class="tab"
        :class="{ 'is-active': tab === item.id }"
        :aria-selected="tab === item.id"
        role="tab"
        @click="tab = item.id"
      >
        {{ item.label }}
        <span v-if="item.id === 'models'" class="tab__count">{{ c.models.length }}</span>
        <span v-if="item.id === 'usage' && referencedUsages.length" class="tab__count">{{ referencedUsages.length }}</span>
      </button>
    </nav>

    <!-- 概览：四个状态分开说，不合并成一个绿点。 -->
    <section v-if="tab === 'overview'" class="panel" role="tabpanel">
      <dl class="states">
        <div class="state-row">
          <dt>配置已保存</dt>
          <dd>
            <span class="qio-state ok">已保存</span>
            <span class="mono">版本 v{{ meta?.version ?? "—" }} · 内部标识 {{ c.credentialId }}</span>
          </dd>
        </div>
        <div class="state-row">
          <dt>Key 已启用</dt>
          <dd>
            <span class="qio-state" :class="statusTone">{{ statusText }}</span>
            <span v-if="c.status === 'revoked' || c.status === 'expired'" class="hint">
              撤销或过期后不会自动恢复；需要重新填写 Key。
            </span>
          </dd>
        </div>
        <div class="state-row">
          <dt>模型发现</dt>
          <dd>
            <span class="qio-state" :class="discovery?.error ? 'err' : discovery?.checkedAt ? 'ok' : 'quiet'">
              {{ discovery?.checkedAt ? (discovery.error ? "上次失败" : "已获取") : "未获取" }}
            </span>
            <span class="hint">{{ discoveryText }}</span>
            <button type="button" class="qio-btn mini quiet" :disabled="busy" @click="emit('discover', true)">
              重新获取
            </button>
          </dd>
        </div>
        <div class="state-row">
          <dt>模型调用验证</dt>
          <dd>
            <span class="qio-state" :class="verifyTone">{{ verifyText }}</span>
            <span class="hint">
              按模型：{{ verifiedModels }} / {{ c.models.length }} 个已验证；可用 {{ usableModels }} 个。
            </span>
          </dd>
        </div>
      </dl>

      <div class="target">
        <h4>实际连接目标</h4>
        <p class="mono">{{ c.endpoint || "—" }}（{{ protocolLabel }} · {{ providerLabel }}）</p>
        <p class="hint">
          只有你保存并确认后，QIO 才会用这把 Key 向这个地址获取模型或做验证；不会自动试别的厂商。
        </p>
      </div>

      <div class="target">
        <h4>当前引用它的用途</h4>
        <p v-if="!referencedUsages.length" class="hint">暂无用途引用这条连接。</p>
        <ul v-else class="refs">
          <li v-for="usage in referencedUsages" :key="usage.usageKey">
            <span>{{ usage.label }}</span>
            <span class="mono">{{ usage.modelId }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- 模型：每个模型的能力与验证结果；失败只影响这一个模型。 -->
    <section v-else-if="tab === 'models'" class="panel" role="tabpanel">
      <div class="panel__head">
        <button type="button" class="qio-btn mini" :disabled="busy" @click="emit('discover', true)">
          获取 / 刷新模型
        </button>
      </div>
      <p v-if="discovery?.error" class="qio-feedback err" role="alert">{{ discovery.error }}</p>
      <ul class="models">
        <li v-for="model in c.models" :key="model.modelId" class="model">
          <div class="model__main">
            <span class="model__name mono">{{ model.displayName || model.modelId }}</span>
            <span v-if="model.displayName && model.displayName !== model.modelId" class="model__id mono">
              {{ model.modelId }}
            </span>
            <span class="qio-tag">{{ model.source === "manual" ? "手动添加" : model.source === "legacy" ? "历史记录" : "自动获取" }}</span>
            <span v-if="model.capabilities.length" class="qio-tag">能力 {{ model.capabilities.join(" / ") }}</span>
            <span class="qio-state" :class="model.usable ? 'ok' : 'err'">
              {{ model.usable ? "当前可用" : "当前不可用" }}
            </span>
            <span class="qio-state" :class="model.verifyState === 'verified' ? 'ok' : model.verifyState === 'failed' ? 'err' : 'quiet'">
              {{ model.verifyState === "verified" ? "验证通过" : model.verifyState === "failed" ? "验证未通过" : model.verifyState === "legacy" ? "旧配置结论" : "尚未验证" }}
            </span>
          </div>
          <p v-if="!model.usable && model.unavailableReason" class="model__reason">{{ model.unavailableReason }}</p>
          <div class="model__actions">
            <button
              type="button"
              class="qio-btn mini"
              :disabled="busy || !c.enabled || c.status !== 'active'"
              :aria-label="'验证模型 ' + model.modelId"
              @click="emit('verifyModel', model.modelId)"
            >
              验证这个模型
            </button>
            <span v-if="model.verifiedAt" class="hint">上次验证：{{ model.verifiedAt.replace("T", " ").slice(0, 19) }}</span>
          </div>
        </li>
      </ul>
      <p v-if="!c.models.length" class="hint">还没有模型记录。点「获取 / 刷新模型」从这条连接保存的地址拉取。</p>
    </section>

    <!-- 管理：默认只放「命名 / 开关 / 换 Key / 撤销 / 删除」，危险操作走 QConfirm（父级） -->
    <section v-else-if="tab === 'manage'" class="panel" role="tabpanel">
      <div class="manage-row">
        <div>
          <h4>显示名称</h4>
          <p class="hint">只改 QIO 里显示的名字，不改发送目标。</p>
        </div>
        <button type="button" class="qio-btn mini" @click="emit('edit')">编辑名称与连接</button>
      </div>
      <div class="manage-row">
        <div>
          <h4>启停</h4>
          <p class="hint">停用后不会自动选中这条连接；记录与账本保留，可随时启用。</p>
        </div>
        <button type="button" class="qio-btn mini" :disabled="busy" @click="emit('toggle')">
          {{ c.enabled ? "停用" : "启用" }}
        </button>
      </div>
      <div class="manage-row">
        <div>
          <h4>更换 API Key</h4>
          <p class="hint">只换密钥，不动地址与协议；换完会重新验证一次。</p>
        </div>
        <button type="button" class="qio-btn mini" @click="emit('rotate')">更换 Key</button>
      </div>
      <div class="manage-row">
        <div>
          <h4>重新验证这把 Key</h4>
          <p class="hint">用当前默认模型真的发一次最小请求；不会检查同 Key 下其它模型。</p>
        </div>
        <button type="button" class="qio-btn mini" :disabled="busy" @click="emit('verifyKey')">重新验证</button>
      </div>
      <div class="manage-row danger">
        <div>
          <h4>撤销</h4>
          <p class="hint">移除本机保存的密钥并保留记录与审计；不会去厂商后台吊销这把 Key。</p>
        </div>
        <button
          v-if="c.status !== 'revoked'"
          type="button"
          class="qio-btn mini"
          :disabled="busy"
          @click="emit('revoke')"
        >
          撤销密钥
        </button>
        <span v-else class="hint">已撤销</span>
      </div>
      <div class="manage-row danger">
        <div>
          <h4>删除</h4>
          <p class="hint">删除这条连接的元数据、模型记录与审计；仍被已保存用途引用时不允许删除。</p>
        </div>
        <button type="button" class="qio-btn mini" :disabled="busy" @click="emit('remove')">删除连接</button>
      </div>
    </section>

    <!-- 用量与记录：累计上限与单轮输出预算分开；账本挂在连接上，不按模型复制额度。 -->
    <section v-else class="panel" role="tabpanel">
      <div class="budget">
        <div class="budget__head">
          <span>累计用量上限</span>
          <span class="mono">
            {{ hasBudget ? formatTokens(c.budgetUsed) + " / " + formatTokens(budget ?? 0) + " token" : "不限" }}
          </span>
        </div>
        <div v-if="hasBudget" class="budget__bar">
          <div class="budget__fill" :class="{ warn: budgetPct >= 80 }" :style="{ width: budgetPct + '%' }"></div>
        </div>
        <p class="hint">
          累计用量是「进 + 出」的合计；单轮输出 token 预算在「记忆与整理 → 高级选项」里。
        </p>
        <p class="mono">进 {{ formatTokens(usageInput) }} · 出 {{ formatTokens(usageOutput) }} token</p>
      </div>
      <dl class="states">
        <div class="state-row">
          <dt>上次验证</dt>
          <dd class="mono">{{ meta?.verified_at || "—" }}<span v-if="meta?.verify_error" class="hint">（{{ meta.verify_error }}）</span></dd>
        </div>
        <div class="state-row">
          <dt>版本 / 标识</dt>
          <dd class="mono">v{{ meta?.version ?? "—" }} · {{ c.credentialId }}</dd>
        </div>
      </dl>
      <div>
        <h4>审计记录</h4>
        <p v-if="auditState === 'loading'" class="hint">审计加载中…</p>
        <p v-else-if="auditState === 'failed'" class="hint">审计加载失败，可以稍后重试。</p>
        <ul v-else class="audit">
          <li v-for="entry in audit" :key="entry.action + entry.created_at" class="mono">
            {{ entry.action }} · v{{ entry.from_version ?? "—" }} → v{{ entry.to_version ?? "—" }} ·
            {{ entry.triggered_by || "—" }} · {{ entry.created_at.replace("T", " ").slice(0, 19) }}
          </li>
        </ul>
        <p v-if="auditState === 'done' && !audit.length" class="hint">还没有审计记录。</p>
      </div>
    </section>

    <p v-if="busy" class="hint" role="status">正在处理…</p>
  </article>
</template>

<style scoped>
/* 一条连接 = 一条顶部分隔线 + 标题 / 页签 / 内容；不套卡片面，也不给每个状态套胶囊。
   状态徽标与按钮的底色/描边由设置页表面层统一收口（.qio-settings 内的共享原语），
   组件这里只保留版式（含选中的 4px 圆点标记）。 */
.conn { display: flex; flex-direction: column; gap: 12px; padding: 14px 0 8px; border-top: 1px solid var(--border-subtle); }
.conn__head { display: flex; flex-direction: column; gap: 4px; }
.conn__title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.conn__name { font-size: var(--fs-base); color: var(--text-strong); font-weight: 600; }
.conn__facts { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 10.5px; color: var(--text-muted); min-width: 0; overflow-wrap: anywhere; }
.dot { opacity: .5; }

/* 页签：透明文字标签；选中 = 品牌色文字 + 4px 圆点；内容只用一条中性细线分隔。 */
.tabs { display: flex; gap: 2px; flex-wrap: wrap; border-bottom: 1px solid var(--border-subtle); }
.tab {
  position: relative;
  border: 0; background: none; border-radius: 0; color: var(--text-secondary);
  font: inherit; font-size: 11.5px; padding: 6px 10px;
  cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
  transition: color var(--mo-1-state) var(--ease-1);
}
.tab::before {
  content: ""; position: absolute; left: 0; top: 50%; width: 4px; height: 4px; margin-top: -2px;
  border-radius: 50%; background: var(--accent);
  opacity: 0; transform: scale(.6);
  transition: opacity var(--mo-1-state) var(--ease-1), transform var(--mo-1-state) var(--ease-1);
}
.tab:hover { background: none; color: var(--text-strong); }
.tab:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; border-radius: 4px; }
.tab.is-active { background: none; color: var(--link); font-weight: 600; }
.tab.is-active::before { opacity: 1; transform: scale(1); }
.tab__count { font-size: 10px; color: var(--text-muted); font-weight: 400; }

.panel { display: flex; flex-direction: column; gap: 12px; }
.panel__head { display: flex; align-items: flex-start; justify-content: flex-end; gap: 12px; }
.states { margin: 0; display: flex; flex-direction: column; gap: 8px; }
.state-row { display: flex; gap: 12px; align-items: flex-start; }
.state-row dt { flex: 0 0 96px; font-size: 10.5px; color: var(--text-muted); }
.state-row dd { margin: 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 10.5px; color: var(--text-secondary); }
.target { display: flex; flex-direction: column; gap: 4px; }
.target h4, .manage-row h4, .budget h4, .panel h4 { margin: 0 0 2px; font-size: 11px; color: var(--text-strong); }
.target p, .budget p { margin: 0; font-size: 10.5px; color: var(--text-secondary); }
.refs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; font-size: 10.5px; color: var(--text-secondary); }
.refs li { display: flex; gap: 8px; }

/* 模型行：透明行 + 1px 中性分隔线（不再套灰底盒子）。 */
.models { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.model { padding: 9px 0; border: 0; border-radius: 0; background: none; display: flex; flex-direction: column; gap: 4px; }
.model + .model { border-top: 1px solid var(--border-subtle); }
.model__main { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.model__name { font-size: var(--fs-xs); color: var(--text-strong); overflow-wrap: anywhere; }
.model__id { font-size: 10px; color: var(--text-muted); overflow-wrap: anywhere; }
.model__reason { margin: 0; font-size: 10px; color: var(--warning); }
.model__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* 管理动作：一行一条，靠中性细线分隔；危险项只染标题。 */
.manage-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-subtle); }
.manage-row:last-of-type { border-bottom: 0; padding-bottom: 2px; }
.manage-row.danger h4 { color: var(--danger); }
.manage-row p { margin: 0; font-size: 10.5px; color: var(--text-secondary); }

.budget { display: flex; flex-direction: column; gap: 5px; }
.budget__head { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--text-secondary); }
/* 轨道是一条细线，不是一块灰色填充 */
.budget__bar { height: 3px; border-radius: 0; background: var(--border-subtle); position: relative; overflow: hidden; }
.budget__fill { position: absolute; inset: 0 auto 0 0; background: var(--success); border-radius: 0; }
.budget__fill.warn { background: var(--warning); }
.audit { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 10px; color: var(--text-muted); }
.hint { margin: 0; font-size: 10px; color: var(--text-muted); line-height: 1.5; }
</style>
