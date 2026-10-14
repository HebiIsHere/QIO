<script setup lang="ts">
/**
 * QIO 共享滚动容器原语。
 *
 * 只在**需要独立滚动视口**的地方使用；「内容自然展开」的区域不要用它（那会多造一层滚动）。
 * 指示条由共享引擎（composables/useQioScrollbars.ts）提供：
 * 浮在边缘、不占布局列、实际溢出才出现、停止滚动后淡出。
 *
 * policy:
 * - auto   —— 需要时可滚；QIO 指示条按需显示（默认）；
 * - hidden —— 保留滚动与定位能力，始终不画指示条（板面、软换行输入区等）。
 */
import { ref } from "vue";
import { useQioScrollbars, type QioScrollPolicy } from "../../composables/useQioScrollbars";

const props = withDefaults(
  defineProps<{
    policy?: QioScrollPolicy;
    /** 传给容器的附加类（布局由调用方决定：flex:1 / min-height:0 等） */
    label?: string;
  }>(),
  { policy: "auto", label: "" },
);

const hostRef = ref<HTMLElement | null>(null);
useQioScrollbars(hostRef, () => ({ policy: props.policy }));
</script>

<template>
  <div
    ref="hostRef"
    class="qio-scroll-area"
    :data-qio-scroll="props.policy"
    :aria-label="props.label || undefined"
  >
    <slot />
  </div>
</template>
