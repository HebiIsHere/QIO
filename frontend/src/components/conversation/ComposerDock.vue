<script setup lang="ts">
/**
 * 输入区外壳（v3）：入口图标行 / 模式切换 / 输入胶囊 / 记忆星球入口。
 *
 * 结构（2026-10-27 改版：与消息流一起被 .chat-box 包住）：
 *   入口图标行（与胶囊同宽、居中；在胶囊外，向上生长）
 *   [模式切换]  [附件图标 · 输入框 · 发送圆钮]  [记忆星球固定槽]
 *
 * - 模式切换与星球入口都不在输入胶囊内部；
 * - 入口行与胶囊宽度相同（同样的左右留白），icon 行居中；
 * - 星球入口的位置由右侧固定槽给出（PlanetDock anchored），不参与浮动/避让。
 *
 * 对齐规则（改版核心）：
 * - 入口行是 .composer-line **之外**的一行 —— 它出现/换行只让整块向上长，
 *   不会把输入胶囊往下推，也不会改变模式切换与输入胶囊的对齐关系；
 * - .composer-line 是「模式切换 | 输入胶囊 | 星球入口」的对齐单元，
 *   align-items: center 让三者共用同一条垂直中心线；
 * - 星球入口直径 = 输入胶囊单行高度（--composer-pill-h），球心与胶囊中心天然重合。
 */
import Composer from "../Composer.vue";
import ComposerEntries from "./ComposerEntries.vue";
import ConversationModeSwitch from "./ConversationModeSwitch.vue";
</script>

<template>
  <div class="composer-dock">
    <!-- 入口行：在输入行之外，出现时向上生长（不参与输入行的对齐） -->
    <div class="composer-entries">
      <ComposerEntries />
    </div>
    <div class="composer-line">
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
.composer-line {
  display: grid;
  /* 三列硬约束：模式 | 对话栈 | 星球槽；列间距就是星球与胶囊的保底安全距。 */
  grid-template-columns: auto minmax(0, 860px) var(--planet-size, 54px);
  column-gap: 12px;
  /* 只看这一行：模式切换、输入胶囊、星球入口共用同一条垂直中心线 */
  align-items: center;
  justify-content: center;
  width: 100%;
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
/* 入口行与胶囊同宽：同在一个 composer-dock 里、同样的左右留白；图标行居中。
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
}
.composer-planet-cell {
  grid-column: 3;
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
  .composer-line {
    column-gap: 12px;
  }
}
@media (max-width: 700px) {
  .composer-mode-cell :deep(.mode-text) {
    display: none;
  }
  .composer-line {
    column-gap: 8px;
  }
}
</style>
