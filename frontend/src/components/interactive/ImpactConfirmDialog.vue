<!-- 中央影响确认框（子智能体 D 负责实现；这里是骨架占位）。 -->
<script setup lang="ts">
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
</script>

<template>
  <div v-if="store.pendingImpact" class="impact-backdrop">
    <div class="impact-dialog" data-im="impact-dialog" role="alertdialog" aria-modal="true">
      <p class="title">这次改动还没有生效：它会影响正在执行的任务。</p>
      <ul class="list">
        <li v-for="item in store.pendingImpact.affected" :key="item.intentId">
          <span class="task">{{ item.title }}</span>
          <span class="text">{{ item.consequence }}</span>
        </li>
      </ul>
      <p class="actions">
        <button class="btn" type="button" data-im="impact-continue" @click="store.confirmImpact()">
          继续：改动生效，相关任务暂停并保留进度
        </button>
        <button class="btn ghost" type="button" data-im="impact-cancel" @click="store.cancelImpact()">
          取消：改动不生效，任务继续
        </button>
      </p>
    </div>
  </div>
</template>

<style scoped>
.impact-backdrop {
  position: absolute;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.42);
}
.impact-dialog {
  width: min(520px, 92vw);
  padding: var(--sp-5);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  color: var(--text-primary);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
}
.title { margin: 0 0 var(--sp-3); font-family: var(--serif); color: var(--text-strong); }
.list { margin: 0 0 var(--sp-4); padding-left: var(--sp-5); }
.task { color: var(--text-strong); margin-right: var(--sp-2); }
.text { color: var(--text-secondary); }
.actions { margin: 0; display: flex; gap: var(--sp-3); flex-wrap: wrap; }
.btn {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--on-accent);
  background: var(--accent);
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-4);
  cursor: pointer;
}
.btn.ghost { background: none; color: var(--text-secondary); border-color: var(--border-strong); }
.btn:hover { background: var(--accent-hover); }
.btn.ghost:hover { background: var(--layer-hover); color: var(--text-strong); }
</style>
