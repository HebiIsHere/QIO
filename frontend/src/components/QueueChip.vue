<script setup lang="ts">
/** 主 turn 队列：折叠气泡（运行中 / 排队中），可展开并逐条取消。 */
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const session = useSessionStore();
const open = ref(false);
/** 正在等待后端确认取消的 turn_id（取消请求 ≠ 已经取消） */
const cancellingId = ref<string | null>(null);
const cancelError = ref("");

const running = computed(() => session.turnQueue.running);
const queued = computed(() => session.turnQueue.queued);
const cancelled = computed(() => session.turnQueue.cancelled);
const visible = computed(
  () => !!running.value || queued.value.length > 0 || cancelled.value.length > 0,
);

async function cancelTurn(turnId: string) {
  if (cancellingId.value) return; // 防重复提交：同一条只发一次
  cancellingId.value = turnId;
  cancelError.value = "";
  try {
    await api.cancelTurn(turnId);
  } catch (e) {
    // 取消失败必须可见：任务可能仍在运行/排队，不能假装已经取消
    cancelError.value = `取消失败：${(e as Error).message}（任务状态未改变，可重试）`;
  } finally {
    cancellingId.value = null;
  }
}
</script>

<template>
  <div v-if="visible" class="queue">
    <button
      class="chip"
      type="button"
      :aria-expanded="open"
      aria-label="查看运行与排队中的消息"
      @click="open = !open"
    >
      <span v-if="running" class="live"></span>
      <b v-if="running">1 运行中</b>
      <span v-if="running && queued.length">·</span>
      <span v-if="queued.length">{{ queued.length }} 排队中</span>
      <span v-if="(running || queued.length) && cancelled.length">·</span>
      <span v-if="cancelled.length">{{ cancelled.length }} 已取消</span>
      <span class="chev">{{ open ? "▾" : "▸" }}</span>
    </button>
    <!-- 折叠区用全局过渡原语（同一套中频进入/离开曲线），不再自定义一份 -->
    <transition name="qio-rise">
      <div v-show="open" class="list">
      <div v-if="running" class="row running">
        <!-- 等待动画只在回答附近播一次；这里用静态标记表达「运行中」 -->
        <span class="live-mark" aria-hidden="true"></span>
        <span class="badge mono qio-state info">运行中</span>
        <span class="txt">{{ running.message }}</span>
        <!-- 与输入区停止按钮指向同一个对象（当前运行的任务），文案统一为「停止」 -->
        <button
          class="qbtn stop"
          type="button"
          :disabled="!!cancellingId"
          :aria-busy="cancellingId === running.turn_id"
          :aria-label="`停止当前任务：${running.message}`"
          @click="cancelTurn(running.turn_id)"
        >
          {{ cancellingId === running.turn_id ? "正在停止…" : "停止" }}
        </button>
      </div>
      <div v-for="(q, i) in queued" :key="q.turn_id" class="row queued">
        <span class="txt">{{ q.message }}</span>
        <span class="badge mono qio-state quiet">排队中 · 第 {{ i + 1 }} 位</span>
        <button
          class="qbtn"
          type="button"
          :disabled="!!cancellingId"
          :aria-busy="cancellingId === q.turn_id"
          :aria-label="`取消排队：${q.message}`"
          @click="cancelTurn(q.turn_id)"
        >
          {{ cancellingId === q.turn_id ? "正在取消…" : "取消排队" }}
        </button>
      </div>
      <div v-for="c in cancelled" :key="c.turn_id" class="row cancelled">
        <span class="txt">{{ c.message }}</span>
        <span class="badge mono qio-state quiet">已取消</span>
      </div>
      <p v-if="cancelError" class="cancel-err qio-feedback err" role="alert">{{ cancelError }}</p>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.queue {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 8px 0;
  max-width: min(760px, 100%);
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  padding: 6px 12px;
  border-radius: 20px;
  border: 1px solid var(--border-strong);
  background: var(--bg-elevated);
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1),
    color var(--dur-fast) var(--ease-1);
}
.chip:hover { border-color: var(--accent); color: var(--text-secondary); }
.chip:active { transform: translateY(var(--shift-1)); }
.chip b {
  color: var(--text-strong);
  font-weight: 600;
}
.chip .live {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
.chev {
  color: var(--text-muted);
}
.list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--r-md);
  border: 1px solid var(--border-subtle);
  background: var(--bg-inset);
}
.row.running {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.row.cancelled { opacity: 0.75; }
.row.cancelled .txt { text-decoration: line-through; color: var(--text-secondary); }
.row.cancelled .badge { color: var(--text-muted); }
.cancel-err {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--danger);
}
.txt {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 13px;
  color: var(--text-primary);
}
.badge {
  flex: none;
  font-size: 10.5px;
  letter-spacing: 0.04em;
}
.qbtn {
  flex: none;
  font: inherit;
  font-size: 12px;
  padding: 5px 12px;
  border-radius: 8px;
  border: 1px solid var(--border-strong);
  background: transparent;
  color: var(--text-primary);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1),
    color var(--dur-fast) var(--ease-1), transform var(--dur-press) var(--ease-1-out);
}
.qbtn.stop {
  border-color: var(--accent);
  color: var(--accent);
}
.qbtn:disabled {
  opacity: 0.55;
  cursor: default;
}
.qbtn:active:not(:disabled) { transform: translateY(var(--press-shift)); }
/* 运行中的静态标记：等待动画交给回答附近那一处，避免同一套动画在多处重复播放 */
.live-mark {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}
</style>
