<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, type CredentialMeta } from "../services/api";

const credentials = ref<CredentialMeta[]>([]);
const form = ref({
  key_id: "",
  secret: "",
  tags: "main-loop",
  endpoint: "",
  default_model: "",
  budget: null as number | null,
});
const notice = ref("");
const error = ref("");

async function load() {
  try {
    credentials.value = (await api.listCredentials()).credentials;
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function create() {
  error.value = "";
  notice.value = "";
  try {
    const payload: Record<string, unknown> = {
      key_id: form.value.key_id.trim(),
      secret: form.value.secret,
      tags: form.value.tags.split(",").map((t) => t.trim()).filter(Boolean),
    };
    if (form.value.endpoint.trim()) payload.endpoint = form.value.endpoint.trim();
    if (form.value.default_model.trim()) payload.default_model = form.value.default_model.trim();
    if (form.value.budget != null) payload.budget = form.value.budget;
    await api.createCredential(payload);
    notice.value = "凭据已创建";
    form.value = { key_id: "", secret: "", tags: "main-loop", endpoint: "", default_model: "", budget: null };
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function revoke(keyId: string) {
  try {
    await api.revokeCredential(keyId);
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function test(keyId: string) {
  try {
    const result = await api.testCredential(keyId);
    notice.value = `${keyId}: ${result.probe.mode} (${result.probe.detail.slice(0, 80)})`;
  } catch (e) {
    error.value = (e as Error).message;
  }
}

onMounted(load);
</script>

<template>
  <div class="settings">
    <header>
      <h1>设置</h1>
      <router-link to="/">← 返回对话</router-link>
    </header>

    <section class="card">
      <h2>凭据（BYOK）</h2>
      <p class="hint">密钥只写不读：保存后不再显示明文。标签用逗号分隔（main-loop/subagent/vision/research/embedding 等）。</p>
      <div class="form">
        <input v-model="form.key_id" placeholder="key_id（如 main-key）" />
        <input v-model="form.secret" type="password" placeholder="API Key" />
        <input v-model="form.tags" placeholder="标签，逗号分隔" />
        <input v-model="form.endpoint" placeholder="endpoint（可选）" />
        <input v-model="form.default_model" placeholder="默认模型（可选）" />
        <input v-model.number="form.budget" type="number" placeholder="预算 token（可选）" />
        <button @click="create">创建</button>
      </div>
      <p v-if="notice" class="ok">{{ notice }}</p>
      <p v-if="error" class="err">{{ error }}</p>

      <ul class="cred-list">
        <li v-for="c in credentials" :key="c.key_id">
          <div class="cred-head">
            <span class="name">{{ c.key_id }}</span>
            <span class="status" :class="c.status">{{ c.status }}</span>
            <span class="version">#{{ c.version }}</span>
          </div>
          <div class="cred-meta">
            tags: {{ c.tags.join(", ") }} · {{ c.default_model || "无模型" }} ·
            预算 {{ c.budget ?? "∞" }}（已用 {{ c.budget_used }}）
          </div>
          <div class="cred-actions">
            <button @click="test(c.key_id)">测试连接</button>
            <button class="danger" @click="revoke(c.key_id)">撤销</button>
          </div>
        </li>
      </ul>
      <p v-if="!credentials.length" class="hint">尚无凭据。</p>
    </section>

    <section class="card">
      <h2>偏好</h2>
      <p class="hint">记忆强度默认值、冷热阈值、行为开关将在设置持久化后提供。</p>
    </section>
  </div>
</template>

<style scoped>
.settings { max-width: 760px; margin: 0 auto; padding: 24px; height: 100%; overflow-y: auto; }
header { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; }
header h1 { font-size: 20px; color: var(--text-strong); }
header a { color: var(--link); text-decoration: none; font-size: 13px; }
.card { background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 14px; padding: 18px; margin-bottom: 18px; }
.card h2 { font-size: 15px; color: var(--text-primary); margin: 0 0 8px; }
.hint { font-size: 12px; color: var(--text-muted); }
.form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0; }
.form input { background: var(--bg-inset); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 8px 10px; color: var(--text-primary); font-size: 13px; }
.form input:focus { outline: none; border-color: var(--accent); }
.form button { grid-column: 1 / -1; background: var(--accent); border: none; border-radius: 8px; color: var(--on-accent); cursor: pointer; padding: 8px 10px; }
.form button:hover { background: var(--accent-hover); }
.ok { color: var(--success); font-size: 13px; }
.err { color: var(--danger); font-size: 13px; }
.cred-list { list-style: none; padding: 0; }
.cred-list li { border: 1px solid var(--border-subtle); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; }
.cred-head { display: flex; gap: 10px; align-items: center; }
.name { font-weight: 600; color: var(--text-strong); }
.status { font-size: 11px; padding: 1px 8px; border-radius: 10px; }
.status.active { background: var(--success-soft); color: var(--success); }
.status.revoked { background: var(--danger-soft); color: var(--danger); }
.version { color: var(--text-muted); font-size: 12px; }
.cred-meta { font-size: 12px; color: var(--text-secondary); margin: 6px 0; }
.cred-actions { display: flex; gap: 8px; }
.cred-actions button { background: var(--bg-accent-subtle); border: 1px solid var(--border-strong); border-radius: 8px; color: var(--text-primary); font-size: 12px; padding: 4px 12px; cursor: pointer; }
.cred-actions .danger { border-color: var(--border-danger); color: var(--danger); }
</style>