<script setup lang="ts">
import { computed } from "vue";
import type { CredentialMeta } from "../../services/api";

const props = defineProps<{ credential: CredentialMeta }>();
defineEmits<{ test: []; edit: []; remove: [] }>();

const connected = computed(() => props.credential.status === "active");
const scopes = computed(() => props.credential.scope ?? []);
const pct = computed(() => {
  const b = props.credential.budget;
  if (!b || b <= 0) return 0;
  return Math.min(100, Math.round((props.credential.budget_used / b) * 100));
});
const warn = computed(() => pct.value >= 80);
const hasBudget = computed(() => props.credential.budget != null && props.credential.budget > 0);
const keyLine = computed(() => {
  const ep = props.credential.endpoint;
  return `${ep ? ep + " · " : ""}••••••••••••••••••••••••••（只写不读）`;
});
</script>

<template>
  <article class="qio-card cred-card">
    <div class="row">
      <span class="name">{{ credential.note || credential.key_id }}</span>
      <div class="tags">
        <span v-for="t in credential.tags" :key="'tag-' + t" class="qio-badge">{{ t }}</span>
        <span v-for="s in scopes" :key="'scope-' + s" class="qio-badge link">scope: {{ s }}</span>
      </div>
      <span class="status" :class="connected ? 'ok' : 'err'">
        {{ connected ? "✓ 已连接" : "✕ 未连接" }}
      </span>
      <div class="actions">
        <button type="button" class="qio-btn" @click="$emit('test')">测试</button>
        <button type="button" class="qio-btn" @click="$emit('edit')">编辑</button>
        <button type="button" class="qio-btn danger" @click="$emit('remove')">删除</button>
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
.actions { margin-left: auto; display: flex; gap: 8px; }
.key { font-size: 12px; color: var(--text-muted); letter-spacing: 0.12em; margin-top: 10px; }
.meta { display: flex; align-items: center; gap: 14px; margin-top: 12px; font-size: 10.5px; color: var(--text-muted); flex-wrap: wrap; }
.budget { flex: 1; min-width: 80px; height: 4px; border-radius: 4px; background: var(--border-subtle); position: relative; }
.budget .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 4px; background: var(--success); }
.budget .fill.warn { background: var(--warning); }
</style>
