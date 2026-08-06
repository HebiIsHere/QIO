<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type CredentialMeta } from "../services/api";
import { identifyCredential } from "../services/identify";

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
const identifyState = ref("idle" as "idle" | "working" | "done" | "failed");
const identifiedProvider = ref("");
const modelOptions = ref<string[]>([]);
let identifyTimer: ReturnType<typeof setTimeout> | null = null;

watch(
  () => form.value.secret,
  (secret) => {
    if (identifyTimer) clearTimeout(identifyTimer);
    if (!secret.trim()) {
      identifyState.value = "idle";
      modelOptions.value = [];
      return;
    }
    identifyTimer = setTimeout(async () => {
      identifyState.value = "working";
      try {
        const result = await identifyCredential(secret.trim());
        if (result.identified) {
          identifiedProvider.value = result.provider ?? "";
          form.value.endpoint = result.base_url ?? form.value.endpoint;
          form.value.default_model = result.default_model ?? form.value.default_model;
          modelOptions.value = result.models ?? [];
          identifyState.value = "done";
        } else {
          identifyState.value = "failed";
          modelOptions.value = [];
        }
      } catch (e) {
        console.error("[identify] failed:", e);
        identifyState.value = "failed";
        modelOptions.value = [];
      }
    }, 800);
  },
);
// 只显示 active 凭据；撤销后卡片立即消失
const activeCredentials = computed(() => credentials.value.filter((c) => c.status === "active"));
const error = ref("");
const fragmentTier = ref("10");
const customCount = ref(10);
const settingsNotice = ref("");
const maintenanceEnabled = ref(true);
const maintenanceInterval = ref(24);

async function loadMaintenanceSettings() {
  try {
    const s = await api.getMaintenanceSettings();
    maintenanceEnabled.value = s.enabled;
    maintenanceInterval.value = s.interval_hours;
  } catch (e) {
    console.error("[settings] load maintenance failed:", e);
  }
}

async function saveMaintenance() {
  try {
    const r = await api.updateMaintenanceSettings({
      enabled: maintenanceEnabled.value,
      interval_hours: maintenanceInterval.value,
    });
    maintenanceEnabled.value = r.enabled;
    maintenanceInterval.value = r.interval_hours;
    settingsNotice.value = `已保存：离线维护${r.enabled ? "开启" : "关闭"}（每 ${r.interval_hours} 小时）`;
  } catch (e) {
    settingsNotice.value = `保存失败：${(e as Error).message}`;
  }
}

async function runMaintenanceNow() {
  try {
    const r = await api.runMaintenance();
    settingsNotice.value = r.started ? "离线维护已启动（后台执行）" : "维护未启动";
  } catch (e) {
    settingsNotice.value = `启动失败：${(e as Error).message}`;
  }
}
const tiers = [
  { value: "5", label: "短（5 轮）" },
  { value: "10", label: "标准（10 轮）" },
  { value: "15", label: "长（15 轮）" },
];

async function loadMemorySettings() {
  try {
    const s = await api.getMemorySettings();
    fragmentTier.value = String(s.fragment_max_messages);
    customCount.value = s.fragment_max_messages;
  } catch (e) {
    console.error("[settings] load memory settings failed:", e);
  }
}

async function saveMemorySettings() {
  const value =
    fragmentTier.value === "custom" ? customCount.value : Number(fragmentTier.value);
  if (!Number.isInteger(value) || value < 1 || value > 30) {
    settingsNotice.value = "自定义轮数需在 1-30 之间";
    return;
  }
  try {
    const r = await api.updateMemorySettings(value);
    settingsNotice.value = `已保存：${r.fragment_max_messages} 轮封块`;
    fragmentTier.value = String(r.fragment_max_messages);
    customCount.value = r.fragment_max_messages;
  } catch (e) {
    settingsNotice.value = `保存失败：${(e as Error).message}`;
  }
}

function onTierChange() {
  if (fragmentTier.value !== "custom") {
    void saveMemorySettings();
  }
}

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
    // revoked cards disappear from the list immediately
    credentials.value = credentials.value.filter((c) => c.key_id !== keyId);
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

onMounted(() => {
  void load();
  void loadMemorySettings();
  void loadMaintenanceSettings();
});
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
        <input v-model="form.key_id" placeholder="key_id（留空自动生成）" />
        <div class="secret-wrap">
            <input v-model="form.secret" type="password" placeholder="API Key（粘贴后自动识别）" />
            <span class="identify" :class="identifyState">
              {{ identifyState === 'working' ? '识别中…' : identifyState === 'done' ? '已识别：' + identifiedProvider : identifyState === 'failed' ? '未能自动识别，请手动填写' : '' }}
            </span>
          </div>
        <input v-model="form.tags" placeholder="标签，逗号分隔" />
        <input v-model="form.endpoint" placeholder="endpoint（可选）" />
        <template v-if="modelOptions.length">
            <select v-model="form.default_model" class="model-select">
              <option v-for="m in modelOptions" :key="m" :value="m">{{ m }}</option>
            </select>
          </template>
          <input v-else v-model="form.default_model" placeholder="默认模型（可选）" />
        <input v-model.number="form.budget" type="number" placeholder="预算 token（可选）" />
        <button @click="create">创建</button>
      </div>
      <p v-if="notice" class="ok">{{ notice }}</p>
      <p v-if="error" class="err">{{ error }}</p>

      <ul class="cred-list">
        <li v-for="c in activeCredentials" :key="c.key_id">
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
      <div class="pref-row">
        <span class="pref-label">记忆封块分档</span>
        <div class="tier-options">
          <label v-for="t in tiers" :key="t.value" class="tier-option">
            <input type="radio" :value="t.value" v-model="fragmentTier" @change="onTierChange" />
            <span>{{ t.label }}</span>
          </label>
          <label class="tier-option">
            <input type="radio" value="custom" v-model="fragmentTier" @change="onTierChange" />
            <span>自定义</span>
            <input
              class="custom-count"
              type="number"
              min="1"
              max="30"
              v-model.number="customCount"
              :disabled="fragmentTier !== 'custom'"
              @change="fragmentTier === 'custom' && saveMemorySettings()"
            />
          </label>
        </div>
        <p class="hint">每个片段的消息数上限：达到后封块并生成摘要。短=5 / 标准=10 / 长=15 / 自定义 1-30。</p>
      </div>
      <div class="pref-row maintenance-row">
        <span class="pref-label">离线维护</span>
        <label class="tier-option">
          <input type="checkbox" v-model="maintenanceEnabled" @change="saveMaintenance" />
          <span>启用（后台整理记忆与知识）</span>
        </label>
        <label class="tier-option">
          <span>间隔（小时）</span>
          <input
            class="custom-count"
            type="number"
            min="1"
            max="720"
            v-model.number="maintenanceInterval"
            @change="saveMaintenance"
          />
        </label>
        <button class="maintenance-run" @click="runMaintenanceNow">立即运行</button>
      </div>
      <p v-if="settingsNotice" class="ok">{{ settingsNotice }}</p>
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
.secret-wrap { display: contents; }
.identify { font-size: 11px; color: var(--text-muted); grid-column: span 2; }
.identify.done { color: var(--success); }
.identify.failed { color: var(--warning); }
.model-select { background: var(--bg-inset); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 8px 10px; color: var(--text-primary); font-size: 13px; }
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