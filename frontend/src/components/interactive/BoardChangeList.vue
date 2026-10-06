<!--
  本次有效改动列表（子智能体 B）。

  数据来自服务端求差（提交返回的 expressions，或保存后重算的未提交改动）：
  不是操作流水，普通移动 / 缩放会写明「只影响显示，不作为表达」。
  状态一律用文字说明，颜色只做辅助。
-->
<script setup lang="ts">
import { computed } from "vue";
import { changeBearingText, changeKindLabel, summarizeChanges } from "../../interactive/submission";
import type { Expression } from "../../interactive/types";

const props = withDefaults(
  defineProps<{
    expressions?: Expression[];
    title?: string;
    emptyText?: string;
    /** true = 这些改动还没有提交（预览） */
    pending?: boolean;
  }>(),
  {
    expressions: () => [],
    title: "本次有效改动",
    emptyText: "本次还没有可提交的改动（服务端在每次保存后重算）。",
    pending: true,
  },
);

const summary = computed(() => summarizeChanges(props.expressions));
</script>

<template>
  <section class="changes" aria-label="本次有效改动">
    <header class="changes-head">
      <h3 class="changes-title">{{ title }}</h3>
      <p class="changes-headline" data-im="change-summary">{{ summary.headline }}</p>
    </header>
    <ul v-if="summary.total" class="changes-list" data-im="change-list">
      <li
        v-for="item in expressions"
        :key="item.id"
        class="change"
        data-im="change-item"
        :data-change-kind="item.kind"
        :data-intent-bearing="item.intentBearing ? 'true' : 'false'"
      >
        <span class="change-kind">{{ changeKindLabel(item.kind) }}</span>
        <span class="change-summary">{{ item.summary }}</span>
        <span class="change-bearing" :class="{ faint: !item.intentBearing }">
          {{ changeBearingText(item) }}
        </span>
      </li>
    </ul>
    <p v-else class="changes-empty" data-im="change-list">{{ emptyText }}</p>
  </section>
</template>

<style scoped>
.changes {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}
.changes-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  min-width: 0;
}
.changes-title {
  margin: 0;
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-secondary);
  white-space: nowrap;
}
.changes-headline {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.changes-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 118px;
  overflow: auto;
}
.change {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-primary);
  min-width: 0;
}
.change-kind {
  flex: none;
  color: var(--link);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  white-space: nowrap;
}
.change-summary {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}
.change-bearing {
  flex: none;
  color: var(--text-muted);
  white-space: nowrap;
}
.change-bearing.faint { color: var(--text-faint); }
.changes-empty {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
</style>
