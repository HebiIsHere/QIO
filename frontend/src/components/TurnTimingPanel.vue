<script setup lang="ts">
/**
 * 一轮耗时的入口 + 明细（契约 §3）。
 *
 * 三条产品规则：
 * 1. 展示的是用户分类（排队 / 准备环境 / 上下文准备 / …），不是内部阶段名
 *    （内部名只在开发者模式出现）；
 * 2. 折叠态**直接显示总耗时**（来自 TURN_END 的 duration_ms，不展开也能看到）；
 * 3. 五种状态互不混淆：未请求 / 加载中 / 成功但无分项 / 失败 / 旧记录缺字段；
 *    明细失败**不抹掉**已知总耗时；缺失 ≠ 0，不永久转圈，可重试。
 *
 * 数据只在用户展开时拉取（见 composables/useTurnTiming.ts），失败不打断对话。
 */
import { computed } from "vue";
import { useUiStore } from "../stores/ui";
import { useTurnTiming } from "../composables/useTurnTiming";
import { formatMs, type TimingRow } from "../services/trace";

const props = defineProps<{
  turnId?: string | null;
  dev?: boolean;
  /** TURN_END 的权威**执行**时长（毫秒，不含排队） */
  durationMs?: number | null;
  /** TURN_END 的权威**排队**时长（毫秒，受理之后、真正开始之前） */
  queueMs?: number | null;
  /** 这一轮的系统状态（completed / failed / cancelled / unavailable…） */
  status?: string | null;
}>();
const ui = useUiStore();
/** 开发者模式看得到内部阶段名；普通模式只给用户分类 */
const devMode = computed(() => props.dev ?? ui.developerMode);

const { state, timing, load } = useTurnTiming(() => props.turnId ?? null);

const STATUS_WORD: Record<string, string> = {
  completed: "已完成",
  failed: "已失败",
  cancelled: "已停止",
  stopped: "已停止",
  unavailable: "未完成",
};

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

/** TURN_END 的两个权威分量：执行（duration_ms）与排队（queue_ms）。 */
const execMs = computed(() => num(props.durationMs));
const queueMs = computed(() => num(props.queueMs));
/**
 * 用户可见「总耗时」= 排队 + 执行（契约 C8）。
 *
 * 只有两个分量都拿到才是**完整**总耗时；缺一个就只能显示可证明的那部分，
 * 并且用明确标签说清（不猜测未知时间、不把执行冒充总耗时）。
 */
const authoritativeTotal = computed(() =>
  execMs.value !== null && queueMs.value !== null ? execMs.value + queueMs.value : null,
);
const traceTotal = computed(() => {
  const value = timing.value?.totalMs;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
});
const knownTotal = computed(() => {
  if (authoritativeTotal.value !== null) return authoritativeTotal.value;
  if (traceTotal.value !== null) return traceTotal.value;
  if (execMs.value !== null) return execMs.value;
  if (queueMs.value !== null) return queueMs.value;
  return null;
});
/** 完整总耗时（排队 + 执行都已知）才敢称「总耗时」。 */
const totalComplete = computed(() => authoritativeTotal.value !== null || traceTotal.value !== null);
const queueOnly = computed(() => execMs.value === null && queueMs.value !== null);
const totalText = computed(() => (knownTotal.value === null ? "" : formatMs(knownTotal.value)));

/**
 * 分项占比：分母始终用界面上显示的那个总耗时 ——
 * 明细加载**不能**把已知总耗时改小/改没，也不能让占比与显示的总数对不上。
 */
const rows = computed<TimingRow[]>(() => {
  const list = timing.value?.rows ?? [];
  const base = knownTotal.value;
  if (base === null || base <= 0) return list;
  return list.map((row) => ({ ...row, percent: Math.round((row.ms / base) * 1000) / 10 }));
});

/**
 * 折叠态文案。只有**真的在请求明细**时才说「读取中」；
 * 已知总耗时永远优先显示 —— 明细失败也不把它换成「读取中」或 0。
 */
const summaryText = computed(() => {
  const word = props.status ? (STATUS_WORD[props.status] ?? "已结束") : "";
  if (totalText.value) {
    const body = totalComplete.value
      ? `总耗时 ${totalText.value}`
      : queueOnly.value
        ? `排队 ${totalText.value}`
        : `执行耗时 ${totalText.value}（排队时间未知）`;
    return word ? `${word} · ${body}` : body;
  }
  if (state.value === "loading") return "读取中";
  if (state.value === "error") return "耗时（明细没读到）";
  if (state.value === "missing") return "没有耗时记录";
  // 未请求：安静的入口，不假装已经在读
  return "耗时";
});

/**
 * 读屏句子：总耗时 + 占比最大的几项。
 * 明细和 TURN_END 不一致时以界面显示的总耗时为准（口径唯一）。
 */
const sentence = computed(() => {
  if (knownTotal.value === null) return "这次没有耗时记录";
  if (!totalComplete.value) {
    return queueOnly.value
      ? `排队 ${formatMs(knownTotal.value)}`
      : `执行耗时 ${formatMs(knownTotal.value)}，排队时间未知`;
  }
  const head = `总耗时 ${formatMs(knownTotal.value)}`;
  const top = [...rows.value]
    .sort((a, b) => b.ms - a.ms)
    .slice(0, 3)
    .map((row) => `${row.label} ${formatMs(row.ms)}`);
  return top.length ? `${head}，其中${top.join("、")}` : head;
});

function onToggle(event: Event) {
  const el = event.target as HTMLDetailsElement | null;
  if (el?.open) void load();
}

/** 明细失败后的重试：force 绕过缓存（缓存里没有失败结果，force 只是语义明确） */
function retry() {
  void load(true);
}

const rawText = computed(() =>
  (timing.value?.rawStages ?? [])
    .filter((s) => s.ms > 0 || s.count > 0)
    .map((s) => `${s.name}×${s.count} ${formatMs(s.ms)}`)
    .join(" · "),
);

const queueNote = computed(() => {
  const t = timing.value;
  if (!t || t.queueMs === null || t.queueMs <= 0) return "";
  return `其中排队 ${formatMs(t.queueMs)}（受理之后、真正开始之前）`;
});
</script>

<template>
  <details v-if="turnId" class="tt" data-test="turn-timing" @toggle="onToggle">
    <!-- 折叠态：▸ + 一句「已完成 · 耗时 12 秒」（总耗时来自 TURN_END，不展开也显示）。
         这里不再单独写一个「耗时」标签，避免同一行出现两次「耗时」。 -->
    <summary class="tt-summary" :aria-label="sentence">
      <span class="tt-title" aria-hidden="true"></span>
      <span class="tt-total mono" :data-state="state">{{ summaryText }}</span>
    </summary>

    <div class="tt-body">
      <!-- 成功且有分项 -->
      <template v-if="timing && rows.length">
        <ul class="tt-rows">
          <li v-for="row in rows" :key="row.key" class="tt-row">
            <span class="tt-label">{{ row.label }}</span>
            <span class="tt-track" aria-hidden="true">
              <span class="tt-fill" :style="{ width: Math.max(row.percent, 1.5) + '%' }" />
            </span>
            <span class="tt-value mono">{{ formatMs(row.ms) }}</span>
            <span class="tt-pct mono">{{ Math.round(row.percent) }}%</span>
          </li>
        </ul>
        <p v-if="queueNote" class="tt-note">{{ queueNote }}</p>
        <p v-if="timing.afterTurnMs > 0" class="tt-note">
          这轮结束之后还整理了 {{ formatMs(timing.afterTurnMs) }}（不计入上面的耗时）
        </p>
        <p v-if="devMode" class="tt-dev mono" data-test="tt-dev">
          内部阶段：{{ rawText || "无" }}
          <template v-if="timing.unknownStages.length">
            · 未归类：{{ timing.unknownStages.join("、") }}
          </template>
        </p>
      </template>

      <!-- 正在请求明细（且还没有任何可显示的分项） -->
      <p v-else-if="state === 'loading'" class="tt-note" role="status">正在读取耗时明细…</p>

      <!-- 失败：说清没读到，给重试；已知总耗时仍在上面那一行 -->
      <p v-else-if="state === 'error'" class="tt-note" role="status">
        这次没读到耗时明细（不影响回答本身）
        <button class="tt-retry" type="button" @click.stop.prevent="retry">重试</button>
      </p>

      <!-- 旧记录：有总耗时但没有 phases 账本 -->
      <p v-else-if="timing && timing.totalMs !== null && timing.legacy" class="tt-note" role="status">
        总耗时 {{ formatMs(timing.totalMs) }}，但这次没有分阶段记录（旧版本留下的数据）
      </p>

      <!-- 成功但无分项：账本在，只是这次没有可分解的阶段 -->
      <p v-else-if="timing && timing.totalMs !== null" class="tt-note" role="status">
        总耗时 {{ formatMs(timing.totalMs) }}，这次没有可分解的耗时记录
      </p>

      <!-- 没有记录：缺失就是缺失，不显示 0 -->
      <p v-else class="tt-note" role="status">这次没有留下耗时记录</p>
    </div>
  </details>
</template>

<style scoped>
.tt {
  margin: 2px 0 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.tt-summary {
  display: inline-flex;
  align-items: baseline;
  gap: var(--sp-2);
  cursor: pointer;
  list-style: none;
  color: var(--text-muted);
  user-select: none;
}
.tt-summary::-webkit-details-marker {
  display: none;
}
.tt-summary:hover {
  color: var(--text-secondary);
}
.tt-title::after {
  content: "▸";
  font-size: 10px;
}
.tt[open] .tt-title::after {
  content: "▾";
}
.tt-total {
  color: var(--text-secondary);
}
.tt-retry {
  margin-left: var(--sp-2);
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: var(--link);
  cursor: pointer;
  text-decoration: underline;
}
.tt-retry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.tt-body {
  margin-top: var(--sp-2);
  padding-left: var(--sp-1);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.tt-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-width: 420px;
}
.tt-row {
  display: grid;
  grid-template-columns: 5.6em 1fr auto auto;
  align-items: center;
  gap: var(--sp-2);
}
.tt-label {
  color: var(--text-secondary);
  white-space: nowrap;
}
.tt-track {
  display: block;
  height: 4px;
  border-radius: var(--r-pill);
  background: var(--bg-inset);
  overflow: hidden;
}
.tt-fill {
  display: block;
  height: 100%;
  border-radius: var(--r-pill);
  background: var(--accent);
  opacity: 0.75;
}
.tt-value {
  color: var(--text-primary);
  min-width: 4.6em;
  text-align: right;
}
.tt-pct {
  color: var(--text-faint);
  min-width: 2.6em;
  text-align: right;
}
.tt-note {
  margin: 0;
  color: var(--text-muted);
}
.tt-dev {
  margin: 0;
  color: var(--text-faint);
  word-break: break-all;
}
</style>
