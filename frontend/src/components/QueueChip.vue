<script setup lang="ts">
/** 主 turn 队列：折叠气泡（运行中 / 排队中），可展开并逐条取消。 */
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const session = useSessionStore();
const open = ref(false);

const running = computed(() => session.turnQueue.running);
const queued = computed(() => session.turnQueue.queued);
const visible = computed(() => !!running.value || queued.value.length > 0);

async function cancelTurn(turnId: string) {
  try {
    await api.cancelTurn(turnId);
  } catch (e) {
    console.error("[queue] cancel failed:", e);
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
      <span class="chev">{{ open ? "▾" : "▸" }}</span>
    </button>
    <div v-show="open" class="list">
      <div v-if="running" class="row running">
        <span class="dots"><i></i><i></i><i></i></span>
        <span class="txt">{{ running.message }}</span>
        <button class="qbtn stop" type="button" @click="cancelTurn(running.turn_id)">取消</button>
      </div>
      <div v-for="(q, i) in queued" :key="q.turn_id" class="row queued">
        <span class="txt">{{ q.message }}</span>
        <span class="badge mono">排队中 · 第 {{ i + 1 }} 位</span>
        <button class="qbtn" type="button" @click="cancelTurn(q.turn_id)">移除</button>
      </div>
    </div>
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
}
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
  border-radius: 10px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-inset);
}
.row.running {
  border-color: var(--accent);
  background: var(--accent-soft);
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
  padding: 2px 8px;
  border-radius: 20px;
  border: 1px solid currentColor;
  color: var(--text-muted);
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
}
.qbtn.stop {
  border-color: var(--accent);
  color: var(--accent);
}
.dots {
  display: inline-flex;
  gap: 4px;
  flex: none;
}
.dots i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  animation: qc-bounce 1.2s infinite ease-in-out;
}
.dots i:nth-child(2) { animation-delay: 0.15s; }
.dots i:nth-child(3) { animation-delay: 0.3s; }
@keyframes qc-bounce {
  0%, 60%, 100% { opacity: 0.35; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-3px); }
}
</style>
