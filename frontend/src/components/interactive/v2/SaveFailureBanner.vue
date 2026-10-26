<script setup lang="ts">
/**
 * 草稿 / 刷新失败的就近行动入口（Phase 3-C，2026-10-26 统一整改）。
 *
 * 与顶栏保存状态（InteractiveChromeTools）的分工（**同一问题只留一处常驻**）：
 * - 板面保存失败 = 保存状态问题：只在顶栏的「唯一保存状态」里说，并在那里就地重试；
 *   这里不再重复一条同样的常驻横幅（两条说同一件事会让人以为有两件事要处理）。
 * - 这里只承接另外两件**不是保存状态**的事：
 *   1) 卡片草稿在本机没保存成功（文字还在编辑器里，属本机存储失败）；
 *   2) 提交已经成功、但页面刷新（重新读取）失败（不是提交失败，也不能重复提交）。
 *
 * 两条都不自动消失、不带关闭按钮（没解决就不能装作看不见），重试只调用既有 store 动作。
 */
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";

const store = useInteractiveStore();
const busy = ref("");
const notice = ref("");

const draftFailed = computed(() => store.draftSaveStatus === "error");
const refreshFailed = computed(() => store.submitRefreshState === "error");
const visible = computed(() => draftFailed.value || refreshFailed.value);

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
    :data-kind="draftFailed ? 'draft' : 'refresh'"
  >
    <span class="kind mono" aria-hidden="true">{{ draftFailed ? "草稿" : "刷新" }}</span>
    <p v-if="draftFailed" class="text" data-im="p3c-save-failure-text">
      草稿没有保存：{{ store.draftSaveError || "原因未知" }}（文字还在编辑器里）
    </p>
    <p v-else class="text" data-im="p3c-save-failure-text">
      提交成功但页面没刷新过来：{{ store.submitRefreshError || "原因未知" }}
    </p>
    <button v-if="draftFailed" type="button" class="act" data-im="p3c-save-failure-retry" :disabled="Boolean(busy)" @click="retryDraft">
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
