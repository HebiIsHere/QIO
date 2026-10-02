<script setup lang="ts">
/**
 * 「这次为什么等这么久」：一轮耗时的用户可读分解。
 *
 * 三条产品规则（对应任务书 C1/C3）：
 * 1. 展示的是用户分类（排队 / 准备环境 / 上下文准备 / 记忆检索 / 模型 / 工具 /
 *    等待确认 / 保存 / 记忆整理 / 其它），不是内部阶段名；内部名只在开发者模式出现。
 * 2. 能一眼看出占比：一行一条 + 一条细比例条，不做需要解读的图表。
 * 3. 老数据（没有 phases）与读取失败都优雅降级：给出总耗时或一句说明，绝不显示 0 或空白。
 *
 * 数据只在用户展开时拉取（见 composables/useTurnTiming.ts），失败不打断对话。
 */
import { computed, ref } from "vue";
import { useUiStore } from "../stores/ui";
import { useTurnTiming } from "../composables/useTurnTiming";
import { buildTimingSentence, formatMs } from "../services/trace";

const props = defineProps<{ turnId?: string | null; dev?: boolean }>();
const ui = useUiStore();
/** 开发者模式看得到内部阶段名；普通模式只给用户分类 */
const devMode = computed(() => props.dev ?? ui.developerMode);

const { state, timing, load } = useTurnTiming(() => props.turnId ?? null);

const sentence = computed(() => buildTimingSentence(timing.value));
const totalText = computed(() => {
  const total = timing.value?.totalMs;
  return total === null || total === undefined ? "" : formatMs(total);
});

function onToggle(event: Event) {
  const el = event.target as HTMLDetailsElement | null;
  if (el?.open) void load();
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
      <span v-if="totalText" class="tt-total mono">{{ totalText }}</span>
      <span v-else class="tt-total mono">读取中</span>
    </summary>

    <div class="tt-body">
      <p v-if="state === 'loading'" class="tt-note" role="status">正在读取耗时明细…</p>

      <template v-else-if="timing && timing.rows.length">
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

      <p v-else-if="state === 'error'" class="tt-note" role="status">
        这次没读到耗时明细（不影响回答本身）
      </p>

      <p v-else class="tt-note" role="status">
        <template v-if="timing && timing.totalMs !== null">
          总耗时 {{ formatMs(timing.totalMs) }}，但这次没有分阶段记录（旧版本留下的数据）
        </template>
        <template v-else>这次没有留下耗时记录</template>
      </p>
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
