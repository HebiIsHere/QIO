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
  padding: 4px 12px 10px;
}
.composer-line {
  display: flex;
  align-items: flex-end;
  justify-content: center;
  gap: 10px;
  width: 100%;
  /* 胶囊 860 + 模式 90 + 星球槽 96 + 两个间隙 ≈ 1066：模式紧邻胶囊左侧，
     星球紧邻胶囊右侧，整行在中央列居中。 */
  max-width: 1066px;
  margin: 0 auto;
  min-width: 0;
}
.composer-stack {
  flex: 1 1 860px;
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
  flex: none;
  align-self: flex-end;
  /* 与胶囊垂直居中（下面还有 Enter 提示的高度） */
  padding-bottom: 36px;
}
.composer-planet-cell {
  flex: none;
  width: 96px;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  /* 96px 球心与 54px 胶囊中心对齐 */
  padding-bottom: 8px;
}
@media (max-width: 1100px) {
  .composer-dock {
    padding: 2px 8px 8px;
  }
  .composer-line {
    gap: 8px;
  }
  .composer-planet-cell {
    width: 80px;
    padding-bottom: 16px;
  }
  .composer-planet-cell :deep(.dock) {
    transform: scale(0.82);
    transform-origin: bottom center;
  }
}
@media (max-width: 700px) {
  .composer-mode-cell :deep(.mode-text) {
    display: none;
  }
  .composer-mode-cell {
    padding-bottom: 32px;
  }
  .composer-planet-cell {
    width: 56px;
    padding-bottom: 28px;
  }
  .composer-planet-cell :deep(.dock) {
    transform: scale(0.56);
  }
}
</style>
