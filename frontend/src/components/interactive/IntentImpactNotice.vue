<!--
  影响说明（子智能体 C）。

  契约 §1.6 / §4.4：影响说明要列出对象、任务、后果，并提供明确的继续与取消。
  组件本身不改变任何东西：决定由父组件处理（批准 / 拒绝 / 撤回 / 暂停）。
-->
<script setup lang="ts">
import { computed } from "vue";
import type { ImpactSection } from "../../interactive/approval";

const props = withDefaults(
  defineProps<{
    title: string;
    sections: ImpactSection[];
    note?: string;
    tone?: "info" | "warning" | "danger";
    confirmLabel?: string;
    cancelLabel?: string;
  }>(),
  {
    note: "",
    tone: "warning",
    confirmLabel: "继续",
    cancelLabel: "取消",
  },
);

const emit = defineEmits<{
  (event: "confirm"): void;
  (event: "cancel"): void;
}>();

const visibleSections = computed(() => props.sections.filter((section) => section.items.length > 0));
</script>

<template>
  <section class="impact-notice" :class="'tone-' + tone" data-im="impact-notice">
    <h4 class="title">{{ title }}</h4>
    <div v-for="section in visibleSections" :key="section.key" class="section">
      <p class="label">{{ section.label }}</p>
      <ul class="items">
        <li v-for="(item, index) in section.items" :key="index">{{ item }}</li>
      </ul>
    </div>
    <p v-if="note" class="note">{{ note }}</p>
    <div class="actions">
      <button class="btn primary" type="button" data-im="impact-continue" @click="emit('confirm')">
        {{ confirmLabel }}
      </button>
      <button class="btn ghost" type="button" data-im="impact-cancel" @click="emit('cancel')">
        {{ cancelLabel }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.impact-notice {
  border: 1px solid var(--warning);
  border-radius: var(--r-md);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  background: var(--warning-soft);
}
.impact-notice.tone-danger {
  border-color: var(--danger);
  background: var(--danger-soft);
}
.impact-notice.tone-info {
  border-color: var(--border-strong);
  background: var(--bg-inset);
}
.title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.label {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  font-weight: 600;
}
.items {
  margin: 2px 0 0;
  padding-left: var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.items li {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: var(--lh-base);
}
.note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-3);
  cursor: pointer;
}
.btn.primary {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}
.btn.primary:hover { background: var(--accent-hover); }
.btn.ghost { background: none; color: var(--link); }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
</style>
