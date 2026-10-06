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
import { buildTimingSentence, formatMs } from "../services/trace";

const props = defineProps<{
  turnId?: string | null;
  dev?: boolean;
  /** TURN_END 的权威总耗时（毫秒）：折叠态直接显示它，不依赖明细是否加载 */
  durationMs?: number | null;
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

/** 已知总耗时：TURN_END 的权威值优先，没有才退回明细里的（排队 + 执行） */
const knownTotal = computed(() => {
  const authoritative = props.durationMs;
  if (typeof authoritative === "number" && Number.isFinite(authoritative) && authoritative >= 0) {
    return authoritative;
  }
  const fromTrace = timing.value?.totalMs;
  return typeof fromTrace === "number" && Number.isFinite(fromTrace) ? fromTrace : null;
});

const totalText = computed(() => (knownTotal.value === null ? "" : formatMs(knownTotal.value)));

/**
 * 折叠态文案。只有**真的在请求明细**时才说「读取中」；
 * 已知总耗时永远优先显示 —— 明细失败也不把它换成「读取中」或 0。
 */
const summaryText = computed(() => {
  const word = props.status ? (STATUS_WORD[props.status] ?? "已结束") : "";
  if (totalText.value) return word ? `${word} · 耗时 ${totalText.value}` : `耗时 ${totalText.value}`;
  if (state.value === "loading") return "读取中";
  if (state.value === "error") return "耗时（明细没读到）";
  if (state.value === "missing") return "没有耗时记录";
  // 未请求：安静的入口，不假装已经在读
  return "耗时";
});

/** 读屏句子：优先用明细；明细还没有但已知总耗时时，也要念得出总耗时 */
const sentence = computed(() => {
  const base = buildTimingSentence(timing.value);
  if (timing.value?.totalMs !== null && timing.value?.totalMs !== undefined) return base;
  if (totalText.value) return `总耗时 ${totalText.value}`;
  return base;
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
    <summary class="tt-summary" :aria-label="sentence">
      <span class="tt-title">耗时</span>
      <span class="tt-total mono" :data-state="state">{{ summaryText }}</span>
    </summary>

    <div class="tt-body">
      <!-- 成功且有分项 -->
      <template v-if="timing && timing.rows.length">
        <ul class="tt-rows">
          <li v-for="row in timing.rows" :key="row.key" class="tt-row">
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
  margin-left: 4px;
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
