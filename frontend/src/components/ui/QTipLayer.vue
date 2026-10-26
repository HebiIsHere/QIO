<script setup lang="ts">
/**
 * QTipLayer —— 全局唯一的用途说明浮层（只挂一次）。
 *
 * 挂载位置：Lead 在 App.vue 里挂一次（产品），ui-catalog / 单测可各自挂一次；
 * `${TIP_LAYER_ATTRIBUTE}` 标记用于“只挂一次”判断（见 mountQioTipLayer.ts）。
 *
 * 行为：
 * - 显示/隐藏完全由 useQioTip 的状态驱动，本组件不持有第二份状态；
 * - 位置：先按估算值渲染，再用真实尺寸调用 computeTipPosition 写回（含翻转到上方）；
 * - 卸载时隐藏并清掉路由监听：换页/重连后不会留下一条悬空的说明；
 * - role="tooltip" 由读屏器按需朗读；浮层自身 pointer-events:none，绝不挡点击。
 */
import { computed, getCurrentInstance, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { computeTipPosition, hideTip, setTipPosition, tipState } from "../../composables/useQioTip";

const tipEl = ref<HTMLElement | null>(null);

const style = computed(() => {
  const pos = tipState.position;
  if (!pos) return { visibility: "hidden" as const, top: "0px", left: "0px" };
  return {
    visibility: "visible" as const,
    top: `${pos.top}px`,
    left: `${pos.left}px`,
    "--qio-tip-placement": pos.placement,
  };
});

async function measureAndPlace(): Promise<void> {
  if (!tipState.open || !tipState.rect) return;
  await nextTick();
  const el = tipEl.value;
  if (!el || typeof window === "undefined") return;
  const rect = el.getBoundingClientRect();
  const width = rect.width || el.offsetWidth || 0;
  const height = rect.height || el.offsetHeight || 0;
  // jsdom 不做排版（尺寸为 0）时也写出一个确定性位置：便于断言“算过定位”，
  // 真实浏览器里下一帧就会量到正确尺寸。
  setTipPosition(
    computeTipPosition(tipState.rect, { width, height }, { width: window.innerWidth, height: window.innerHeight }),
  );
}

watch(
  () => [tipState.open, tipState.text, tipState.rect] as const,
  () => {
    void measureAndPlace();
  },
  { deep: false },
);

// 路由切换（含通过浏览器后退）：浮层立即消失。
// 只有应用里装了 router 才 useRoute：画廊/单测没有 router，直接调用会打 inject 警告。
let stopRouteWatch: (() => void) | null = null;
try {
  const hasRouter = Boolean(getCurrentInstance()?.appContext.config.globalProperties.$router);
  if (hasRouter) {
    const route = useRoute();
    if (route) stopRouteWatch = watch(() => route.fullPath, () => hideTip());
  }
} catch {
  stopRouteWatch = null;
}

onBeforeUnmount(() => {
  stopRouteWatch?.();
  hideTip();
});
</script>

<template>
  <Teleport to="body">
    <div class="qio-tip-layer" :data-qio-tip-layer="''">
      <Transition name="qio-fade">
        <div
          v-if="tipState.open && tipState.text"
          ref="tipEl"
          class="qio-tip"
          role="tooltip"
          data-qio-tip
          :style="style"
        >
          {{ tipState.text }}
        </div>
      </Transition>
    </div>
  </Teleport>
</template>

<style scoped>
.qio-tip-layer {
  position: fixed;
  inset: 0;
  z-index: var(--qio-z-tip, 320);
  pointer-events: none;
}
.qio-tip {
  position: fixed;
  z-index: var(--qio-z-tip, 320);
  max-width: min(280px, calc(100vw - 2 * var(--sp-2)));
  padding: 6px 10px;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  color: var(--text-primary);
  box-shadow: var(--elev-floating);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  line-height: 1.45;
  text-align: center;
  overflow-wrap: anywhere;
  pointer-events: none;
  /* 轻玻璃感：只提亮一点，不把说明做成实心黑块 */
  backdrop-filter: blur(calc(var(--glass-blur) / 2));
  -webkit-backdrop-filter: blur(calc(var(--glass-blur) / 2));
}
</style>
