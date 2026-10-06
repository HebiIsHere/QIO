<!-- 提交区（子智能体 C 负责实现；这里是骨架占位，放在底部工具栏右端）。 -->
<script setup lang="ts">
import { computed } from "vue";
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
const busy = computed(() => store.submitStatus === "submitting");
</script>

<template>
  <div class="submit-cluster-placeholder">
    <button
      class="submit"
      type="button"
      data-im="submit"
      :disabled="busy || !store.board"
      @click="store.submit()"
    >
      {{ busy ? "正在提交…" : "提交给 QIO" }}
    </button>
  </div>
</template>

<style scoped>
.submit-cluster-placeholder { display: flex; align-items: center; }
.submit {
  font: inherit;
  font-size: var(--fs-base);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-5);
  cursor: pointer;
}
.submit:hover { background: var(--accent-hover); }
.submit:disabled { opacity: 0.55; cursor: default; }
</style>
