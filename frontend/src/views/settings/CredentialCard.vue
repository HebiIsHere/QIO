<script setup lang="ts">
import { computed, ref } from "vue";
import { api, type CredentialMeta } from "../../services/api";

const props = defineProps<{ credential: CredentialMeta }>();
const emit = defineEmits<{
  test: [];
  "edit-meta": [];
  rotate: [];
  "toggle-enabled": [];
  remove: [];
}>();

const expanded = ref(false);
const audit = ref<{ action: string; from_version: number | null; to_version: number | null; created_at: string }[]>([]);
const auditState = ref<"idle" | "loading" | "done" | "failed">("idle");

const c = computed(() => props.credential);
const enabled = computed(() => c.value.enabled);
const pct = computed(() => {
  const b = c.value.budget;
  if (!b || b <= 0) return 0;
  return Math.min(100, Math.round((c.value.budget_used / b) * 100));
});
const warn = computed(() => pct.value >= 80);
const hasBudget = computed(() => c.value.budget != null && c.value.budget > 0);
const keyLine = computed(() => {
  const ep = c.value.endpoint;
  return `${ep ? ep + " · " : ""}••••••••••••••••••••••••••（只写不读）`;
});

const status = computed(() => {
  if (c.value.status === "revoked") return { text: "✕ 已撤销", cls: "err" };
  if (c.value.status === "expired") return { text: "✕ 已过期", cls: "err" };
  if (enabled.value) return { text: "✓ 已启用", cls: "ok" };
  return { text: "已停用", cls: "paused" };
});

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
      <span class="name">{{ credential.note || credential.key_id }}</span>
      <div class="tags">
        <span v-for="t in credential.tags" :key="'tag-' + t" class="qio-badge">{{ t }}</span>
      </div>
      <span class="status" :class="status.cls">{{ status.text }}</span>
      <div class="actions">
        <button type="button" class="qio-btn btn-test" @click="emit('test')">测试</button>
        <button type="button" class="qio-btn btn-detail" @click="toggleExpand">{{ expanded ? "收起" : "详情" }}</button>
        <button type="button" class="qio-btn btn-meta" @click="emit('edit-meta')">编辑</button>
        <button type="button" class="qio-btn btn-rotate" @click="emit('rotate')">换钥</button>
        <button type="button" class="qio-btn btn-toggle" @click="emit('toggle-enabled')">
          {{ enabled ? "停用" : "启用" }}
        </button>
        <button type="button" class="qio-btn danger btn-danger" @click="emit('remove')">删除</button>
      </div>
    </div>
    <div class="key mono">{{ keyLine }}</div>
    <div class="meta mono">
      <template v-if="hasBudget">
        <span>预算</span>
        <div class="budget"><div class="fill" :class="{ warn }" :style="{ width: pct + '%' }"></div></div>
        <span>¥ {{ credential.budget_used }} / {{ credential.budget }}</span>
      </template>
      <span v-else>预算 ∞</span>
      <span v-if="credential.default_model">· {{ credential.default_model }}</span>
      <span>· v{{ credential.version }}</span>
    </div>
    <div v-if="expanded" class="detail mono">
      <div><span class="k">key_id</span><span class="v">{{ credential.key_id }}</span></div>
      <div><span class="k">端点</span><span class="v">{{ credential.endpoint || "—" }}</span></div>
      <div><span class="k">模型</span><span class="v">{{ credential.default_model || "—" }}</span></div>
      <div><span class="k">标签</span><span class="v">{{ credential.tags.join(", ") }}</span></div>
      <div><span class="k">版本</span><span class="v">v{{ credential.version }}</span></div>
      <div><span class="k">预算</span><span class="v">{{ hasBudget ? `${credential.budget_used}/${credential.budget}` : "∞" }}</span></div>
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
.cred-card { margin-bottom: 12px; }
.row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.name { font-weight: 600; color: var(--text-strong); font-size: 14px; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; }
.status { font-size: 10px; padding: 2px 9px; border-radius: 20px; }
.status.ok { color: var(--success); border: 1px solid var(--success); }
.status.err { color: var(--danger); border: 1px solid var(--danger); }
.status.paused { color: var(--warning); border: 1px solid var(--warning); }
.actions { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }
.key { font-size: 12px; color: var(--text-muted); letter-spacing: 0.12em; margin-top: 10px; }
.meta { display: flex; align-items: center; gap: 14px; margin-top: 12px; font-size: 10.5px; color: var(--text-muted); flex-wrap: wrap; }
.budget { flex: 1; min-width: 80px; height: 4px; border-radius: 4px; background: var(--border-subtle); position: relative; }
.budget .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 4px; background: var(--success); }
.budget .fill.warn { background: var(--warning); }
.detail { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border-subtle); font-size: 11px; color: var(--text-muted); }
.detail .k { color: var(--text-muted); }
.detail .v { color: var(--text-strong); }
.audit { grid-column: 1 / -1; display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.audit-line { font-size: 10px; color: var(--text-muted); }
</style>
