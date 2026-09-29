<script setup lang="ts">
/**
 * 凭据卡片：默认只回答四个问题 —— 这是哪家/叫什么、用哪个模型、给什么用途、
 * 现在能不能用（以及是不是当前默认）。地址、内部标识、版本、审计记录都收进「详情」。
 *
 * 「已启用」与「已验证可用」是两件事：启用是用户的开关，验证是服务真的答应了。
 * 两者分开显示，避免「已启用」被读成「一定能用」。
 */
import { computed, ref } from "vue";
import { api, type CredentialMeta } from "../../services/api";
import { resolvedKind, usageLabel, verifyLabel } from "../../services/credentials";

const props = defineProps<{
  credential: CredentialMeta;
  /** 当前默认使用（可能来自显式默认，也可能是排序回落），由设置页统一判定 */
  isDefault?: boolean;
}>();
const emit = defineEmits<{
  "edit-meta": [];
  rotate: [];
  verify: [];
  "toggle-enabled": [];
  "set-default": [];
  revoke: [];
  remove: [];
}>();

const expanded = ref(false);
/**
 * 管理动作是否展开（第四阶段：默认阅读、按需管理）。
 *
 * 以前一张凭据卡上常驻 7 个按钮，读一行凭据得在按钮堆里找；
 * 现在默认只给结论 + 两个入口（编辑 / 更多操作），管理动作收起时同时 `inert`——
 * 否则它们虽然看不见，键盘 Tab 仍会掉进去。
 */
const managing = ref(false);
const audit = ref<{ action: string; from_version: number | null; to_version: number | null; created_at: string }[]>([]);
const auditState = ref<"idle" | "loading" | "done" | "failed">("idle");

const c = computed(() => props.credential);
const name = computed(() => c.value.note || c.value.provider_name || c.value.key_id);
const purposes = computed(() =>
  c.value.tags.map((t) => usageLabel(t)).join("、") || "未设置用途",
);
const enabled = computed(() => c.value.enabled);
const verifyState = computed(() => c.value.verify_state ?? "legacy");
const verifyText = computed(() => verifyLabel(verifyState.value));
const hasBudget = computed(() => c.value.budget != null && c.value.budget > 0);
// 真实调用累计的进 / 出（老数据没有这两个字段，按 0 处理）
const usageInput = computed(() => c.value.usage_input ?? 0);
const usageOutput = computed(() => c.value.usage_output ?? 0);
const hasUsage = computed(() => usageInput.value > 0 || usageOutput.value > 0);
const pct = computed(() => {
  const b = c.value.budget;
  if (!b || b <= 0) return 0;
  return Math.min(100, Math.round((c.value.budget_used / b) * 100));
});
const warn = computed(() => pct.value >= 80);

const status = computed(() => {
  if (c.value.status === "revoked") return { text: "已撤销", cls: "err" };
  if (c.value.status === "expired") return { text: "已过期", cls: "err" };
  if (enabled.value) return { text: "已启用", cls: "ok" };
  return { text: "已停用", cls: "paused" };
});
const verifyBadge = computed(() => {
  if (verifyState.value === "verified") return { text: "已验证可用", cls: "ok" };
  if (verifyState.value === "failed") return { text: "验证未通过", cls: "warn" };
  if (verifyState.value === "unverified") return { text: "尚未验证", cls: "quiet" };
  return null;
});
const protocolLabel = computed(() =>
  resolvedKind(c.value) === "anthropic" ? "Anthropic 接口" : "OpenAI 兼容接口",
);
const canSetDefault = computed(
  () =>
    !props.isDefault &&
    enabled.value &&
    c.value.status === "active" &&
    c.value.tags.includes("main-loop") &&
    (verifyState.value === "verified" || verifyState.value === "legacy"),
);

function formatTokens(value: number): string {
  return value.toLocaleString("zh-CN");
}

async function toggleExpand() {
  expanded.value = !expanded.value;
  if (expanded.value && auditState.value === "idle") {
    auditState.value = "loading";
    try {
      const r = await api.getCredentialAudit(c.value.key_id);
      audit.value = r.audit.map((a) => ({
        action: a.action,
        from_version: a.from_version,
        to_version: a.to_version,
        created_at: a.created_at,
      }));
      auditState.value = "done";
    } catch {
      auditState.value = "failed";
    }
  }
}
</script>

<template>
  <article class="qio-card cred-card">
    <div class="row">
      <span class="name">{{ name }}</span>
      <span v-if="isDefault" class="qio-state default-badge">当前默认</span>
      <div class="tags">
        <span class="qio-tag purpose">{{ purposes }}</span>
      </div>
      <span class="status qio-state" :class="status.cls">{{ status.text }}</span>
      <span v-if="verifyBadge" class="status qio-state" :class="verifyBadge.cls">{{ verifyBadge.text }}</span>
      <!-- 阅读态的两个入口：编辑（常用）与更多操作（其余动作，默认收起） -->
      <div class="entries">
        <button type="button" class="qio-btn mini btn-edit" @click="emit('edit-meta')">编辑</button>
        <button
          type="button"
          class="qio-btn mini btn-manage"
          :aria-expanded="managing"
          @click="managing = !managing"
        >
          {{ managing ? "收起" : "更多操作" }}
        </button>
      </div>
    </div>
    <div class="meta mono">
      <span v-if="credential.default_model">{{ credential.default_model }}</span>
      <span v-else class="warn-text">未设置模型</span>
      <template v-if="hasBudget">
        <span>用量</span>
        <div class="budget"><div class="fill" :class="{ warn }" :style="{ width: pct + '%' }"></div></div>
        <span>
          进 {{ formatTokens(usageInput) }} · 出 {{ formatTokens(usageOutput) }} ·
          合计 {{ formatTokens(credential.budget_used) }} / {{ formatTokens(credential.budget ?? 0) }} token
        </span>
      </template>
      <span v-else-if="hasUsage">进 {{ formatTokens(usageInput) }} · 出 {{ formatTokens(usageOutput) }} token（不限）</span>
      <span v-else>用量不限</span>
    </div>
    <!-- 管理动作：默认收起且 inert（不可见也不可聚焦），点「更多操作」才展开 -->
    <div class="manage" :class="{ open: managing }" :inert="managing ? undefined : true">
      <div class="manage-clip">
        <div class="actions">
          <button
            v-if="canSetDefault"
            type="button"
            class="qio-btn mini btn-default"
            @click="emit('set-default')"
          >
            设为默认
          </button>
          <button type="button" class="qio-btn mini btn-verify" @click="emit('verify')">重新验证</button>
          <button type="button" class="qio-btn mini btn-rotate" @click="emit('rotate')">更换 API Key</button>
          <button type="button" class="qio-btn mini btn-toggle" @click="emit('toggle-enabled')">
            {{ enabled ? "停用" : "启用" }}
          </button>
          <button type="button" class="qio-btn mini quiet btn-detail" :aria-expanded="expanded" @click="toggleExpand">
            {{ expanded ? "收起详情" : "详情" }}
          </button>
          <!-- 撤销 ≠ 删除：撤销让这把密钥在 QIO 里作废但保留记录（可审计）；
               两者都不会去厂商那边吊销 Key。 -->
          <button
            v-if="credential.status !== 'revoked'"
            type="button"
            class="qio-btn mini quiet btn-revoke"
            title="只在 QIO 里作废这把密钥，保留记录与审计历史"
            @click="emit('revoke')"
          >
            撤销密钥
          </button>
          <button type="button" class="qio-btn mini danger btn-danger" @click="emit('remove')">删除</button>
        </div>
      </div>
    </div>
    <div v-if="expanded" class="detail mono">
      <div><span class="k">厂商</span><span class="v">{{ credential.provider_name || "自定义/未知" }}</span></div>
      <div><span class="k">服务地址</span><span class="v">{{ credential.endpoint || "—" }}</span></div>
      <div><span class="k">连接协议</span><span class="v">{{ protocolLabel }}</span></div>
      <div><span class="k">用途</span><span class="v">{{ credential.tags.map(usageLabel).join("、") || "—" }}</span></div>
      <div><span class="k">内部标识</span><span class="v">{{ credential.key_id }}</span></div>
      <div><span class="k">版本</span><span class="v">v{{ credential.version }}</span></div>
      <div><span class="k">用量上限</span><span class="v">{{ hasBudget ? `${formatTokens(credential.budget_used)} / ${formatTokens(credential.budget ?? 0)} token` : "不限" }}</span></div>
      <div v-if="hasUsage">
        <span class="k">用量明细</span>
        <span class="v">进 {{ formatTokens(usageInput) }} · 出 {{ formatTokens(usageOutput) }} token</span>
      </div>
      <div>
        <span class="k">上次验证</span>
        <span class="v">{{ credential.verified_at || (verifyText || "—") }}</span>
      </div>
      <div class="audit">
        <span v-if="auditState === 'loading'">审计加载中…</span>
        <span v-else-if="auditState === 'failed'">审计加载失败</span>
        <template v-else>
          <div v-for="a in audit" :key="a.action + a.created_at" class="audit-line">
            {{ a.action }} · v{{ a.from_version ?? "—" }}→v{{ a.to_version ?? "—" }} · {{ a.created_at }}
          </div>
        </template>
      </div>
    </div>
  </article>
</template>

<style scoped>
.cred-card { margin-bottom: var(--sp-3); }
.row { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.name { font-weight: 600; color: var(--text-strong); font-size: var(--fs-base); }
.tags { display: flex; gap: 6px; flex-wrap: wrap; }
.purpose { font-size: var(--fs-xs); }
.default-badge { color: var(--link); background: var(--accent-soft); border-color: var(--accent-soft); font-size: var(--fs-xs); }
.status { font-size: 10px; }
.status.ok { color: var(--success); background: var(--success-soft); border-color: var(--success-soft); }
.status.err { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-soft); }
.status.paused { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-soft); }
.status.warn { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-soft); }
.status.quiet { color: var(--text-muted); background: transparent; border-color: var(--border-subtle); }
.entries { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; }
/* 管理区：真实高度过渡（不是 v-show 瞬切）；收起时不占位、不拦截、不参与 Tab */
.manage { display: grid; grid-template-rows: 0fr; transition: grid-template-rows var(--mo-2-in) var(--ease-2); }
.manage.open { grid-template-rows: 1fr; }
.manage-clip { overflow: hidden; min-height: 0; opacity: 0; transition: opacity var(--mo-2-in) var(--ease-2); }
.manage.open .manage-clip { opacity: 1; }
.actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}
.meta { display: flex; align-items: center; gap: var(--sp-3); margin-top: var(--sp-3); font-size: 10.5px; color: var(--text-muted); flex-wrap: wrap; }
.warn-text { color: var(--warning); }
.budget { flex: 1; min-width: 80px; max-width: 220px; height: 4px; border-radius: 4px; background: var(--border-subtle); position: relative; }
.budget .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 4px; background: var(--success); }
.budget .fill.warn { background: var(--warning); }
.detail { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px; margin-top: var(--sp-3); padding-top: var(--sp-3); border-top: 1px solid var(--border-subtle); font-size: var(--fs-xs); color: var(--text-muted); }
.detail .v { color: var(--text-strong); word-break: break-all; }
.detail .k { margin-right: 6px; }
.audit { grid-column: 1 / -1; display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.audit-line { font-size: 10px; color: var(--text-muted); }
</style>
