<script setup lang="ts">
/**
 * 输入区外壳（v4）：入口行 / 模式切换 / 输入胶囊 / 记忆星球入口。
 *
 * 结构（本轮统一辅助入口）：
 *   入口行（与输入胶囊**同一列**；在胶囊外、向上生长）
 *   [模式切换]  [附件图标 · 输入框 · 发送圆钮]  [记忆星球固定槽]
 *
 * 对齐规则：
 * - 入口行、模式切换、输入胶囊、星球入口共用一个 grid；入口行放在**胶囊所在列**
 *   （第 2 列），所以整行以真实输入胶囊为基准居中，而不是按包含左右控件的整行居中；
 * - 入口行出现 / 换行只让整块向上长，不推动输入胶囊；模式与星球仍与胶囊同一条垂直中心线；
 * - 展开事项面板时向上生长，入口行与胶囊位置稳定，不需要滚动跳转；
 * - 没有任何入口时入口行高度为 0，整行收起。
 */
import Composer from "../Composer.vue";
import ComposerEntries from "./ComposerEntries.vue";
import ConversationModeSwitch from "./ConversationModeSwitch.vue";
</script>

<template>
  <div class="composer-dock">
    <div class="composer-grid">
      <!-- 入口行：与输入胶囊同一列（第 2 列），出现时向上生长 -->
      <div class="composer-entries">
        <ComposerEntries />
      </div>
      <div class="composer-mode-cell">
        <ConversationModeSwitch />
      </div>
      <div class="composer-stack">
        <Composer />
      </div>
      <div v-if="$slots.planet" class="composer-planet-cell">
        <slot name="planet" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.composer-dock {
  flex: none;
  min-width: 0;
  position: relative;
  z-index: 2;
  /* 左右留白与消息流同一套（.chat-box 上的 --chat-pad-inline）；
     底部留白 = 胶囊到消息框下缘的距离。 */
  padding: 0 var(--chat-pad-inline, 12px) var(--chat-pad-bottom, 12px);
  /* 星球入口直径 = 输入胶囊单行高度（球直径 = 输入框高度） */
  --planet-size: var(--composer-pill-h, 54px);
}
/*
 * 一个 grid 同时排「入口行」与「输入行」：
 * 列 = 模式 | 胶囊（≤860px） | 星球；入口行固定放在胶囊列，
 * 所以它的水平中心与胶囊完全一致，不受左侧模式、右侧星球宽度影响。
 */
.composer-grid {
  display: grid;
  grid-template-columns: auto minmax(0, 860px) var(--planet-size, 54px);
  grid-template-rows: auto auto;
  column-gap: 12px;
  align-items: center;
  justify-content: center;
  width: 100%;
  max-width: 1070px;
  margin: 0 auto;
  min-width: 0;
}
.composer-entries {
  grid-column: 2;
  grid-row: 1;
  width: 100%;
  min-width: 0;
}
/* 只有真的有入口时才占一行的高度（没有事项时完全收起，不留空白）。
   上下间距由 .entry-row 的对称内边距提供：按钮在消息区底边与输入胶囊顶边之间垂直居中。 */
.composer-entries:has(.entry-row) {
  margin-bottom: 0;
}
.composer-mode-cell {
  grid-column: 1;
  grid-row: 2;
  align-self: center;
  min-width: 0;
}
.composer-stack {
  grid-column: 2;
  grid-row: 2;
  width: 100%;
  max-width: 860px;
  min-width: 0;
}
.composer-planet-cell {
  grid-column: 3;
  grid-row: 2;
  width: var(--planet-size, 54px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.composer-planet-cell :deep(.dock.anchored) {
  width: var(--planet-size, 54px);
  height: var(--planet-size, 54px);
}
.composer-planet-cell :deep(.orb) {
  width: 100% !important;
  height: 100% !important;
}
@media (max-width: 1100px) {
  .composer-grid {
    column-gap: 12px;
  }
}
@media (max-width: 700px) {
  .composer-mode-cell :deep(.mode-text) {
    display: none;
  }
  .composer-grid {
    column-gap: 8px;
  }
}
</style>
