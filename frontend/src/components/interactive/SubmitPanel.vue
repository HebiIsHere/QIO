<!-- 提交区域（子智能体 B 负责：有效改动、允许查看范围、提交前后状态、保存恢复）。 -->
<script setup lang="ts">
import { computed } from "vue";
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
const busy = computed(() => store.submitStatus === "submitting");
</script>

<template>
  <footer class="submit-placeholder">
    <div class="left">
      <p class="line">提交后 QIO 才会拿到本次允许查看的表达；保存不会调用 QIO。</p>
    </div>
    <button class="submit" type="button" :disabled="busy || !store.board" @click="store.submit()">
      {{ busy ? "正在提交…" : "提交" }}
    </button>
  </footer>
</template>

<style scoped>
.submit-placeholder {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}
.line { margin: 0; font-size: var(--fs-sm); color: var(--text-muted); }
.submit {
  font: inherit;
  font-size: var(--fs-base);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-6);
  cursor: pointer;
}
.submit:hover { background: var(--accent-hover); }
.submit:disabled { opacity: 0.55; cursor: default; }
.submit:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
</style>
