<!--
  演示入口（子智能体 C）。

  第一阶段**没有**接入真实的 QIO 理解与执行：这里生成与推进的都是可控演示，
  界面上必须写明「演示」，不能描述成真实 QIO 判断。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import type { Intent } from "../../interactive/types";

const store = useInteractiveStore();
const busy = ref(false);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);

const OUTCOMES: { value: "done" | "failed" | "paused" | "cancelled"; label: string }[] = [
  { value: "done", label: "演示：成功" },
  { value: "failed", label: "演示：失败（撤回）" },
  { value: "paused", label: "演示：暂停" },
  { value: "cancelled", label: "演示：取消" },
];

const settable = computed(() =>
  store.intents.filter(
    (item) => item.demo && ["running", "paused", "done"].includes(item.status),
  ),
);

function outcomesFor(intent: Intent): { value: "done" | "failed" | "paused" | "cancelled"; label: string }[] {
  // 已完成的任务只能再演示「失败 / 取消」——它用来展示「撤回该任务造成的改动」
  if (intent.status === "done") {
    return OUTCOMES.filter((item) => item.value === "failed" || item.value === "cancelled");
  }
  return OUTCOMES;
}

async function create(): Promise<void> {
  busy.value = true;
  error.value = null;
  notice.value = null;
  try {
    const result = await store.createDemoIntents();
    notice.value =
      "已生成 / 复用 " + (result.created?.length ?? 0) + " 项演示意图（标记为演示）。";
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

async function advance(
  intentId: string,
  outcome: "done" | "failed" | "paused" | "cancelled",
): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.advanceDemo(intentId, outcome);
    notice.value = result.detail ?? "演示推进完成。";
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="demo-entry" data-im="demo-entry">
    <header class="head">
      <h3 class="title">演示入口</h3>
      <span class="badge">演示</span>
    </header>
    <p class="hint">
      第一阶段没有接入真实的 QIO 理解与执行。这里生成的是<strong>演示意图</strong>：内容是可控样例，
      只用来走通虚线预览、审批、依赖等待与失败撤回；<strong>不代表 QIO 的真实判断</strong>。
    </p>
    <button
      class="btn primary"
      type="button"
      data-im="demo-create"
      :disabled="busy"
      @click="create"
    >
      {{ busy ? "处理中…" : "生成 4 项演示意图" }}
    </button>

    <div v-if="settable.length" class="advance">
      <p class="hint">选择一个可控结果推进演示（推进结果不代表真实执行）：</p>
      <div
        v-for="item in settable"
        :key="item.id"
        class="advance-row"
        :data-intent-id="item.id"
      >
        <span class="name">{{ item.title }}</span>
        <span class="status">{{ item.status }}</span>
        <span class="buttons">
          <button
            v-for="outcome in outcomesFor(item)"
            :key="outcome.value"
            class="btn"
            type="button"
            :data-im="'demo-advance-' + outcome.value"
            :data-intent-id="item.id"
            :disabled="busy"
            @click="advance(item.id, outcome.value)"
          >
            {{ outcome.label }}
          </button>
        </span>
      </div>
    </div>

    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="error" class="error" role="alert">演示操作失败：{{ error }}</p>
  </section>
</template>

<style scoped>
.demo-entry {
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-md);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  background: var(--bg-inset);
}
.head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.badge {
  font-size: var(--fs-xs);
  border-radius: var(--r-pill);
  padding: 1px var(--sp-2);
  border: 1px solid var(--warning);
  color: var(--warning);
  background: var(--warning-soft);
}
.hint,
.notice,
.error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: var(--lh-base);
}
.notice { color: var(--text-secondary); }
.error { color: var(--danger); }
.advance {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.advance-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.name {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.buttons {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
}
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.btn.primary {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
  align-self: flex-start;
}
.btn.primary:hover:enabled { background: var(--accent-hover); }
.btn:disabled { opacity: 0.55; cursor: default; }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
</style>
