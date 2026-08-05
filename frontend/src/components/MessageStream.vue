<script setup lang="ts">
/**
 * 消息流：@tanstack/vue-virtual 虚拟滚动 + 底部跟随。
 */
import { computed, nextTick, ref, watch } from "vue";
import { useVirtualizer } from "@tanstack/vue-virtual";
import { useSessionStore } from "../stores/session";
import MessageItem from "./MessageItem.vue";

const session = useSessionStore();
const containerRef = ref<HTMLDivElement | null>(null);
const followBottom = ref(true);

const messages = computed(() => session.messages);

// options 整体作为 computed：count 依赖消息数变化时自动 setOptions
const virtualizer = useVirtualizer(
  computed(() => ({
    count: messages.value.length,
    getScrollElement: () => containerRef.value,
    estimateSize: () => 120,
    overscan: 6,
  })),
);

function onScroll() {
  const el = containerRef.value;
  if (!el) return;
  followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
}

// 动态测量列表项真实高度（替代固定 estimateSize），避免长消息重叠
function measureItem(el: unknown) {
  if (el instanceof Element) virtualizer.value.measureElement(el);
}

watch(
  () => session.messages.length,
  async () => {
    if (followBottom.value) {
      await nextTick();
      virtualizer.value.scrollToIndex(messages.value.length - 1, { align: "end" });
    }
  },
);

watch(
  () => session.turnRunning,
  (running) => {
    if (running) followBottom.value = true;
  },
);
</script>

<template>
  <div ref="containerRef" class="stream" @scroll.passive="onScroll">
    <div
      class="spacer"
      :style="{ height: virtualizer.getTotalSize() + 'px', position: 'relative' }"
    >
      <div
        v-for="item in virtualizer.getVirtualItems()"
        :key="String(item.key)"
        :data-index="item.index"
        :ref="measureItem"
        class="virtual-item"
        :style="{
          position: 'absolute',
          top: 0,
          left: 0,
          width: '100%',
          transform: `translateY(${item.start}px)`,
        }"
      >
        <MessageItem :message="messages[item.index]" />
      </div>
    </div>
    <div v-if="!messages.length" class="empty">
      开始对话吧。话题星球在右下角。
    </div>
  </div>
</template>

<style scoped>
.stream { flex: 1; overflow-y: auto; padding: 0 16px; }
.spacer { width: 100%; }
.empty { text-align: center; color: var(--text-muted); margin-top: 80px; font-size: 14px; }
</style>