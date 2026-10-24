<script setup lang="ts">
/**
 * 保存失败常驻提示（Phase 3-C）：**持续可见 + 可重试**。
 *
 * 与顶栏那一句保存状态（InteractiveChromeTools）分工：
 * - 顶栏说的是「当前保存状态」；
 * - 这里是**失败时的行动入口**：不自动消失、不带关闭按钮（失败没解决就不能装作看不见），
 *   重试只调用既有 store 动作，不重复提交、不改板面。
 *
 * 只显示真实失败：saveStatus==="error" / draftSaveStatus==="error" / 提交后刷新失败。
 */
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { saveStatusView } from "./saveStatus";

const store = useInteractiveStore();
const busy = ref("");
const notice = ref("");

const saveView = computed(() =>
  saveStatusView({
    status: store.saveStatus as "idle" | "saving" | "saved" | "error",
    dirty: store.dirty,
    error: store.saveError,
  }),
);
const saveFailed = computed(() => store.saveStatus === "error");
const draftFailed = computed(() => store.draftSaveStatus === "error");
const refreshFailed = computed(() => store.submitRefreshState === "error");
const visible = computed(() => saveFailed.value || draftFailed.value || refreshFailed.value);
const impactBlocked = computed(() => Boolean(store.pendingImpact));

async function retrySave(): Promise<void> {
  if (busy.value) return;
  busy.value = "save";
  notice.value = "";
  try {
    await store.saveNow();
    notice.value =
      store.saveStatus === "error"
        ? "保存仍然失败：" + (store.saveError || "原因未知")
        : "已重新保存，结果以服务端回执为准";
  } finally {
    busy.value = "";
  }
}

async function retryDraft(): Promise<void> {
  if (busy.value) return;
  busy.value = "draft";
  notice.value = "";
  try {
    await store.retryDraftSave();
    notice.value = store.draftSaveStatus === "error" ? "草稿仍然没有保存：" + (store.draftSaveError || "原因未知") : "已重试保存草稿";
  } finally {
    busy.value = "";
  }
}

async function retryRefresh(): Promise<void> {
  if (busy.value) return;
  busy.value = "refresh";
  notice.value = "";
  try {
    const result = await store.retrySubmitRefresh();
    notice.value = result.ok ? "已重新读取提交后的状态（没有重复提交）" : "刷新没有完成：" + (result.error || "原因未知");
  } finally {
    busy.value = "";
  }
}
</script>

<template>
  <div
    v-if="visible"
    class="save-failure"
    role="alert"
    data-im="p3c-save-failure"
    :data-kind="saveFailed ? 'save' : draftFailed ? 'draft' : 'refresh'"
  >
    <span class="kind mono" aria-hidden="true">{{ saveFailed ? "保存" : draftFailed ? "草稿" : "刷新" }}</span>
    <p v-if="saveFailed" class="text" data-im="p3c-save-failure-text">
      {{ saveView.text }}<template v-if="impactBlocked">（被影响确认挡住：先在提交区处理）</template>
    </p>
    <p v-else-if="draftFailed" class="text" data-im="p3c-save-failure-text">
      草稿没有保存：{{ store.draftSaveError || "原因未知" }}（文字还在编辑器里）
    </p>
    <p v-else class="text" data-im="p3c-save-failure-text">
      提交成功但页面没刷新过来：{{ store.submitRefreshError || "原因未知" }}
    </p>
    <button v-if="saveFailed" type="button" class="act" data-im="p3c-save-failure-retry" :disabled="Boolean(busy)" @click="retrySave">
      {{ busy === "save" ? "重试中…" : "重试保存" }}
    </button>
    <button v-else-if="draftFailed" type="button" class="act" data-im="p3c-save-failure-retry" :disabled="Boolean(busy)" @click="retryDraft">
      {{ busy === "draft" ? "重试中…" : "重试保存草稿" }}
    </button>
    <button v-else type="button" class="act" data-im="p3c-save-failure-retry" :disabled="Boolean(busy)" @click="retryRefresh">
      {{ busy === "refresh" ? "读取中…" : "重试读取" }}
    </button>
    <p v-if="notice" class="notice" role="status" data-im="p3c-save-failure-notice">{{ notice }}</p>
  </div>
</template>

<style scoped>
.save-failure {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
  min-width: 0;
  padding: 4px var(--sp-2);
  border: 1px solid var(--danger);
  border-radius: var(--r-lg);
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-elevated));
  color: var(--danger);
  font-size: var(--fs-xs);
}
.kind {
  flex: none;
  font-size: 10.5px;
  letter-spacing: 0.06em;
}
.text { margin: 0; flex: 1; min-width: 0; overflow-wrap: anywhere; }
.act {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-strong);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  padding: 1px var(--sp-2);
  cursor: pointer;
}
.act:disabled { color: var(--text-muted); cursor: default; }
.notice { flex: 1 1 100%; margin: 0; color: var(--text-secondary); overflow-wrap: anywhere; }
</style>
