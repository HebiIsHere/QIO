<script setup lang="ts">
/**
 * 输入区与上方五个图标入口（B）。
 *
 * 五入口与模式切换都通过 Composer 的插槽进入输入气泡：
 * 入口在输入框正上方、与输入区同一个玻璃气泡（固定贴底），因此消息流的底部缓冲、
 * 「回到最新消息」的锚定（composerMetrics 量的是整个 .composer）都自动包含它们，
 * 不需要另开一套让位逻辑。
 */
import Composer from "../Composer.vue";
import ComposerEntries from "./ComposerEntries.vue";
import ConversationModeSwitch from "./ConversationModeSwitch.vue";
</script>

<template>
  <div class="composer-dock">
    <Composer>
      <template #entries>
        <ComposerEntries />
      </template>
      <template #leading>
        <ConversationModeSwitch />
      </template>
    </Composer>
  </div>
</template>

<style scoped>
.composer-dock {
  flex: none;
  min-width: 0;
  position: relative;
  z-index: 2;
}
</style>
