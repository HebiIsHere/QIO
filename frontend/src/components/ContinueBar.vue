<script setup lang="ts">
/** 迭代/输出预算耗尽时的「继续/停止」操作条。 */
import { ref } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const session = useSessionStore();
const busy = ref(false);

async function decide(decision: "approved" | "rejected") {
  const pending = session.pendingContinue;
  if (!pending || busy.value) return;
  busy.value = true;
  try {
    await api.respondApproval(pending.id, decision);
    session.pendingContinue = null;
  } catch (e) {
    console.error("[continue] respond failed:", e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div v-if="session.pendingContinue" class="continue-bar" role="alertdialog">
    <span class="msg mono">
      已达迭代上限（{{ session.pendingContinue.used }}/{{ session.pendingContinue.max }}）
    </span>
    <span class="spacer"></span>
    <button class="qio-btn primary" type="button" :disabled="busy" @click="decide('approved')">
      继续
    </button>
    <button class="qio-btn" type="button" :disabled="busy" @click="decide('rejected')">
      停止
    </button>
  </div>
</template>

<style scoped>
.continue-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 8px 0;
  padding: 10px 16px;
  border: 1px solid var(--accent);
  border-radius: 12px;
  background: var(--bg-elevated);
}
.continue-bar .msg {
  font-size: 12px;
  color: var(--text-secondary);
}
.continue-bar .spacer {
  flex: 1;
}
</style>
