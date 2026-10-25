<script setup lang="ts">
/**
 * 输入区外壳（v3）：入口图标行 / 模式切换 / 输入胶囊 / 记忆星球入口。
 *
 * 结构（对齐用户提供的 HTML 与确认稿）：
 *   入口图标行（与胶囊同宽、居中；在胶囊外）
 *   [模式切换]  [附件图标 · 输入框 · 发送圆钮]  [记忆星球固定槽]
 *   Enter 提示（胶囊外）
 *
 * - 模式切换与星球入口都不在输入胶囊内部；
 * - 入口行与胶囊同一个 .composer-stack，宽度天然一致；
 * - 星球入口的位置由右侧固定槽给出（PlanetDock anchored），不参与浮动/避让。
 */
import Composer from "../Composer.vue";
import ComposerEntries from "./ComposerEntries.vue";
import ConversationModeSwitch from "./ConversationModeSwitch.vue";
</script>

<template>
  <div class="composer-dock">
    <div class="composer-line">
      <div class="composer-mode-cell">
        <ConversationModeSwitch />
      </div>
      <div class="composer-stack">
        <div class="composer-entries">
          <ComposerEntries />
        </div>
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
  /* 说明行已删：只留底部的 8px 外沿；高度由输入行/星球槽共同决定 */
  padding: 0 12px 8px;
}
.composer-line {
  display: grid;
  /* 三列硬约束：模式 | 对话栈 | 星球槽；列间距就是星球与胶囊的保底安全距。 */
  grid-template-columns: auto minmax(0, 860px) var(--planet-size, 96px);
  column-gap: 12px;
  align-items: center;
  justify-content: center;
  width: 100%;
  /* 模式 90 + 间隙 12 + 胶囊 860 + 间隙 12 + 星球 96 = 1070 */
  max-width: 1070px;
  margin: 0 auto;
  min-width: 0;
}
.composer-stack {
  grid-column: 2;
  width: 100%;
  max-width: 860px;
  min-width: 0;
}
/* 入口行与胶囊同宽：都在 composer-stack 里，宽度天然一致；图标行居中。
   只有真的有入口时才占一行的高度（没有事项时完全收起，不留空白）。 */
.composer-entries {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
  min-width: 0;
}
.composer-entries:has(.entry-row) {
  margin-bottom: 8px;
}
.composer-mode-cell {
  grid-column: 1;
  align-self: center;
  min-width: 0;
  /* 说明行已删：模式与胶囊直接共用同一条中心线 */
  padding-bottom: 0;
}
.composer-planet-cell {
  grid-column: 3;
  width: var(--planet-size, 96px);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  /* 球心与胶囊中心对齐；不再用 transform 缩放布局盒，避免热区/视觉尺寸错位 */
  padding-bottom: 0;
}
.composer-planet-cell :deep(.dock.anchored) {
  width: var(--planet-size, 96px);
  height: var(--planet-size, 96px);
}
.composer-planet-cell :deep(.orb) {
  width: 100% !important;
  height: 100% !important;
}
@media (max-width: 1100px) {
  .composer-dock {
    padding: 0 8px 8px;
  }
  .composer-line {
    --planet-size: 80px;
    column-gap: 12px;
  }
}
@media (max-width: 700px) {
  .composer-mode-cell :deep(.mode-text) {
    display: none;
  }
  .composer-line {
    --planet-size: 56px;
    column-gap: 12px;
  }
}
</style>
