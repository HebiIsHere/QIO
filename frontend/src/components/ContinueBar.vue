<script setup lang="ts">
/**
 * 迭代/输出预算耗尽时的「继续/停止」操作条。
 *
 * 第四阶段：并入统一卡片语言（`.qio-card` + `data-state` + `.qio-state` + `.qio-feedback`），
 * 并且**在流程内失败必须就地可见** —— 这一步决定任务是否继续，静默写控制台会让用户
 * 以为已经处理过了（旧实现正是如此）。
 */
import { ref } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const session = useSessionStore();
const busy = ref(false);
/** 提交失败原因：失败比成功更值得停留，所以保留到下次尝试 */
const error = ref("");

async function decide(decision: "approved" | "rejected") {
  const pending = session.pendingContinue;
  if (!pending || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await api.respondApproval(pending.id, decision);
    session.pendingContinue = null;
  } catch (e) {
    error.value = `提交没有成功：${(e as Error).message}（可以重试）`;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    v-if="session.pendingContinue"
    class="continue-bar qio-card"
    data-state="waiting"
    role="group"
    aria-label="已达迭代上限，需要你决定是否继续"
  >
    <div class="line">
      <span class="qio-state warn">
        已达迭代上限 {{ session.pendingContinue.used }}/{{ session.pendingContinue.max }}
      </span>
      <span class="msg">可以让它继续多做几轮，也可以就此停下。</span>
      <span class="spacer"></span>
      <button class="qio-btn primary" type="button" :disabled="busy" @click="decide('approved')">
        {{ busy ? "提交中…" : "继续" }}
      </button>
      <button class="qio-btn quiet" type="button" :disabled="busy" @click="decide('rejected')">
        停止
      </button>
    </div>
    <p v-if="error" class="qio-feedback err" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.continue-bar {
  margin: 8px 0;
  max-width: min(760px, 100%);
  padding: var(--sp-3) var(--sp-4);
}
.continue-bar .line {
  display: flex;
  align-items: center;
  gap: 12px;
}
.continue-bar .msg {
  font-size: 12px;
  color: var(--text-secondary);
}
.continue-bar .spacer {
  flex: 1;
}
.continue-bar .qio-feedback {
  margin: 6px 0 0;
}
</style>
