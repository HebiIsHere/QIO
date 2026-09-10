<script setup lang="ts">
/** Trace Viewer：最小、可折叠的 Agent 调试视图（只读）。 */
import { computed, onMounted, ref } from "vue";
import {
  api,
  type TraceDetail,
  type TraceSummary,
} from "../services/api";

const traces = ref<TraceSummary[]>([]);
const total = ref(0);
const selected = ref<TraceDetail | null>(null);
const loading = ref(false);
const error = ref("");
const enabled = ref(true);

const PAGE = 20;
const offset = ref(0);

async function loadList() {
  loading.value = true;
  error.value = "";
  try {
    const r = await api.listTraces(PAGE, offset.value);
    traces.value = r.traces;
    total.value = r.total;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}

async function select(turnId: string) {
  error.value = "";
  try {
    selected.value = await api.getTrace(turnId);
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function loadEnabled() {
  try {
    enabled.value = (await api.getTraceSettings()).enabled;
  } catch {
    /* ignore */
  }
}

async function toggleEnabled() {
  try {
    enabled.value = (await api.updateTraceSettings(!enabled.value)).enabled;
  } catch (e) {
    error.value = (e as Error).message;
  }
}

function prev() {
  offset.value = Math.max(0, offset.value - PAGE);
  void loadList();
}
function next() {
  if (offset.value + PAGE < total.value) {
    offset.value += PAGE;
    void loadList();
  }
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function pretty(obj: unknown): string {
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}

const hasRows = computed(() => traces.value.length > 0);

onMounted(() => {
  void loadEnabled();
  void loadList();
});
</script>

<template>
  <div class="debug">
    <header class="head">
      <span class="title serif">Agent Trace</span>
      <span class="sub mono">只读调试视图 · 回答“这一轮为什么这样做”</span>
      <span class="spacer"></span>
      <button class="qio-btn" type="button" @click="toggleEnabled">
        记录{{ enabled ? "已开启" : "已关闭" }}
      </button>
      <router-link to="/" class="qio-btn">← 返回对话</router-link>
    </header>

    <p v-if="error" class="msg err mono">{{ error }}</p>

    <div class="cols">
      <section class="list-sec">
        <div class="sec-head">
          <h2>最近 Turn</h2>
          <span class="mono count">{{ total }} 条</span>
        </div>
        <p v-if="!hasRows && !loading" class="empty mono">还没有 trace。</p>
        <button
          v-for="t in traces"
          :key="t.turn_id"
          type="button"
          class="row"
          :class="{ active: selected?.turn_id === t.turn_id, bad: t.status === 'failed' }"
          @click="select(t.turn_id)"
        >
          <span class="row-top">
            <span class="tid mono">{{ t.turn_id }}</span>
            <span class="badge mono" :class="t.status">{{ t.status }}</span>
          </span>
          <span class="row-sub mono">
            {{ fmtTime(t.started_at) }} · {{ t.duration_ms ?? "—" }}ms
            <template v-if="t.final_topic"> · {{ t.final_topic }}</template>
          </span>
        </button>
        <div class="pager">
          <button class="qio-btn" type="button" :disabled="offset === 0" @click="prev">上一页</button>
          <button class="qio-btn" type="button" :disabled="offset + PAGE >= total" @click="next">下一页</button>
        </div>
      </section>

      <section class="detail-sec">
        <p v-if="!selected" class="empty mono">← 选一条 turn 查看它的决策链路。</p>
        <template v-else>
          <div class="sec-head">
            <h2>{{ selected.turn_id }}</h2>
            <span class="badge mono" :class="selected.status">{{ selected.status }}</span>
          </div>
          <p class="meta mono">
            {{ fmtTime(selected.started_at) }} · {{ selected.duration_ms ?? "—" }}ms
            <template v-if="selected.error"> · {{ selected.error }}</template>
          </p>

          <details open>
            <summary>Topic 决策</summary>
            <pre class="json mono">{{ pretty(selected.topic) }}</pre>
          </details>
          <details>
            <summary>Context 注入</summary>
            <pre class="json mono">{{ pretty(selected.injection) }}</pre>
          </details>
          <details>
            <summary>Model 调用（{{ (selected.model_calls || []).length }}）</summary>
            <pre class="json mono">{{ pretty(selected.model_calls) }}</pre>
          </details>
          <details>
            <summary>工具调用（{{ (selected.tool_runs || []).length }}）</summary>
            <pre class="json mono">{{ pretty(selected.tool_runs) }}</pre>
          </details>
          <details>
            <summary>记忆 / 知识写入</summary>
            <pre class="json mono">{{ pretty(selected.writes) }}</pre>
          </details>
          <details :open="(selected.warnings || []).length > 0">
            <summary>警告（{{ (selected.warnings || []).length }}）</summary>
            <pre class="json mono">{{ pretty(selected.warnings) }}</pre>
          </details>
          <details>
            <summary>Output</summary>
            <pre class="json mono">{{ selected.final_preview }}</pre>
          </details>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.debug {
  height: 100%;
  overflow: auto;
  padding: 26px 32px 40px;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
}
.head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}
.head .title {
  font-size: 22px;
  font-weight: 600;
  color: var(--text-strong);
}
.head .sub {
  font-size: 11.5px;
  color: var(--text-muted);
}
.head .spacer { flex: 1; }
.cols {
  display: grid;
  grid-template-columns: minmax(240px, 340px) 1fr;
  gap: 18px;
  align-items: start;
}
@media (max-width: 820px) {
  .cols { grid-template-columns: 1fr; }
}
.list-sec, .detail-sec {
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  background: var(--bg-surface);
  padding: 14px 16px;
}
.sec-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
}
.sec-head h2 {
  font-size: 15px;
  margin: 0;
  color: var(--text-strong);
}
.sec-head .count, .detail-sec .meta {
  font-size: 11px;
  color: var(--text-muted);
}
.empty {
  font-size: 12px;
  color: var(--text-muted);
}
.row {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  margin-bottom: 6px;
  border: 1px solid var(--border-subtle);
  border-radius: 9px;
  background: var(--bg-inset);
  color: var(--text-primary);
  cursor: pointer;
}
.row:hover { border-color: var(--border-strong); }
.row.active { border-color: var(--accent); background: var(--accent-soft); }
.row-top { display: flex; align-items: center; gap: 8px; }
.tid {
  font-size: 11.5px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-sub { font-size: 10.5px; color: var(--text-muted); }
.badge {
  font-size: 10px;
  padding: 1px 7px;
  border-radius: 20px;
  border: 1px solid currentColor;
  flex: none;
  color: var(--text-muted);
}
.badge.done { color: var(--success); }
.badge.failed { color: var(--danger); }
.badge.cancelled { color: var(--text-muted); }
.pager { display: flex; gap: 8px; margin-top: 10px; }
details {
  border-top: 1px solid var(--border-subtle);
  padding: 8px 0;
}
summary {
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-strong);
}
.json {
  margin: 8px 0 0;
  padding: 10px 12px;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-x: auto;
  max-height: 320px;
}
.msg.err { color: var(--danger); font-size: 12px; }
</style>
