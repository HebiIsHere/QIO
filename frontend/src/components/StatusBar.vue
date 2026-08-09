<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useEventStore } from "../stores/events";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const events = useEventStore();
const session = useSessionStore();

/* ---- 主题切换：tokens.css 用 html[data-theme] ---- */
const isLight = ref(false);
function applyTheme(light: boolean) {
  isLight.value = light;
  document.documentElement.dataset.theme = light ? "light" : "dark";
  localStorage.setItem("qio-theme", light ? "light" : "dark");
}
function toggleTheme() {
  applyTheme(!isLight.value);
}

/* ---- budget 徽标（现有凭据服务的预算数据） ---- */
const budgetPct = ref<number | null>(null);
async function loadBudget() {
  try {
    const { credentials } = await api.listCredentials();
    const active = credentials.find(
      (c) => c.status === "active" && c.budget != null && c.budget > 0,
    );
    if (active && active.budget) {
      budgetPct.value = Math.round((active.budget_used / active.budget) * 100);
    }
  } catch {
    // 后端未启动：隐藏 budget 徽标
  }
}

const modelClass = computed(() => events.modelMode);
const tokensText = computed(() => `tok ${events.usageTokens.toLocaleString("en-US")}`);

onMounted(() => {
  applyTheme(localStorage.getItem("qio-theme") === "light");
  void loadBudget();
});
</script>

<template>
  <div class="status-bar">
    <span class="dot" :class="{ on: events.connected }" aria-hidden="true"></span>
    <span class="conn">{{ events.connected ? "SSE 已连接" : "SSE 连接中…" }}</span>
    <span v-if="session.anchorFragment?.title" class="focus" :title="'聚焦片段: ' + session.anchorFragment.id">
      {{ session.anchorFragment.title }}
    </span>
    <span v-if="session.turnRunning" class="running">agent 运行中…</span>
    <span v-if="session.lastError" class="error" :title="session.lastError">出错</span>

    <span class="qio-badge model" :class="modelClass">{{ events.modelMode }}</span>
    <span v-if="budgetPct != null" class="qio-badge budget">budget {{ budgetPct }}%</span>

    <span class="spacer"></span>
    <span class="stat mono">{{ tokensText }}</span>
    <button class="theme-btn" type="button" @click="toggleTheme">
      {{ isLight ? "🌙 暗色" : "☀️ 亮色" }}
    </button>
    <router-link to="/settings" class="settings-link">设置</router-link>
  </div>
</template>

<style scoped>
.status-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 8px 20px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}
.dot.on {
  background: var(--success);
  box-shadow: 0 0 6px var(--success);
}
.conn {
  color: var(--text-secondary);
  letter-spacing: 0.04em;
}
.focus {
  color: var(--link);
  letter-spacing: 0.04em;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.running {
  color: var(--warning);
  letter-spacing: 0.04em;
}
.error {
  color: var(--danger);
  letter-spacing: 0.04em;
}
.qio-badge.model.native {
  color: var(--success);
  border-color: var(--success);
}
.qio-badge.model.text {
  color: var(--warning);
  border-color: var(--warning);
}
.qio-badge.model.unsupported {
  color: var(--danger);
  border-color: var(--danger);
}
.spacer {
  flex: 1;
}
.stat {
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.theme-btn {
  cursor: pointer;
  background: var(--bg-elevated);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 3px 10px;
  font-family: var(--mono);
  font-size: 11px;
  transition: border-color 0.18s, color 0.18s;
}
.theme-btn:hover {
  border-color: var(--border-strong);
  color: var(--text-strong);
}
.settings-link {
  margin-left: 4px;
  color: var(--link);
  text-decoration: none;
  cursor: pointer;
  letter-spacing: 0.04em;
}
.settings-link:hover {
  text-decoration: underline;
}
</style>