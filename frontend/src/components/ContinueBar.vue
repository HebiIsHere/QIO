<script setup lang="ts">
/**
 * 迭代/输出预算耗尽时的「继续/停止」操作条。
 *
 * 第四阶段：并入统一卡片语言（`.qio-card` + `data-state` + `.qio-state` + `.qio-feedback`），
 * 并且**在流程内失败必须就地可见** —— 这一步决定任务是否继续，静默写控制台会让用户
 * 以为已经处理过了（旧实现正是如此）。
 */
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const session = useSessionStore();
const busy = ref(false);
/** 提交失败原因：失败比成功更值得停留，所以保留到下次尝试 */
const error = ref("");

/**
 * 暂停原因：**用后端给的原因，不猜**。
 *
 * 这条操作条现在服务两种暂停：预算耗尽（payload 里只有 used/max）与无进展暂停
 * （payload.reason = "no_progress"）。旧实现一律写「已达迭代上限 X/Y」，于是
 * 无进展暂停会显示「已达迭代上限 3/128」——一句与事实不符的话，用户据此做了决定。
 */
const REASON_NO_PROGRESS = "no_progress";

const headline = computed(() => {
  const pending = session.pendingContinue;
  if (!pending) return "";
  if (pending.reason === REASON_NO_PROGRESS) return "这一轮没有新的进展";
  return `已达迭代上限 ${pending.used}/${pending.max}`;
});

const detail = computed(() => {
  const pending = session.pendingContinue;
  if (!pending) return "";
  if (pending.message) return pending.message;
  return pending.reason === REASON_NO_PROGRESS
    ? "连续几次调用拿到完全一样的结果，继续下去只会重复。"
    : "可以让它继续多做几轮，也可以就此停下。";
});

const ariaLabel = computed(() =>
  headline.value ? `${headline.value}，需要你决定是否继续` : "需要你决定是否继续",
);

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
    :aria-label="ariaLabel"
  >
    <div class="line">
      <span class="qio-state warn">{{ headline }}</span>
      <span class="msg">{{ detail }}</span>
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
