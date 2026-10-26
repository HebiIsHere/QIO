<script setup lang="ts">
/**
 * 权限模式常驻文字（全局栏）：当前模式是常驻事实，但不再用「胶囊/徽标」样式。
 *
 * - 显示服务端恢复的真实模式；读取中 / 失败如实显示，不冒充默认值；
 * - 点击进入设置 →「工具与权限」由用户主动切换（这里不改模式）；
 * - 对话页与互动页共用同一条全局栏，所以两边看到的是同一处文字、同一个 store。
 */
import { computed } from "vue";
import { useSecurityStore } from "../../stores/security";
import { useUiStore } from "../../stores/ui";

const security = useSecurityStore();
const ui = useUiStore();

const text = computed(() => {
  if (security.loaded) return security.currentLabel;
  if (security.error) return "权限模式未知";
  return "权限模式读取中…";
});

const title = computed(() => {
  if (!security.loaded) {
    return security.error
      ? security.error + "（点一下进入设置查看）"
      : "正在从 QIO 读取当前权限模式";
  }
  return "当前权限模式：" + security.currentLabel + "。点一下进入设置 →「工具与权限」由你切换。";
});

function openSettings() {
  ui.settingsSection = "tools";
  void import("../../router")
    .then(({ default: router }) => router.push("/settings"))
    .catch(() => {
      // 路由不可用（单独挂载组件的测试）：不能把一次点击变成未处理异常
    });
}
</script>

<template>
  <button
    type="button"
    class="perm-mode-text"
    data-im="permission-mode-text"
    :data-mode="security.loaded ? security.mode : 'pending'"
    :data-loaded="security.loaded ? '1' : '0'"
    :title="title"
    :aria-label="title"
    @click="openSettings"
  >
    {{ text }}
  </button>
</template>

<style scoped>
/* 纯文字：没有胶囊、没有边框、没有底色；hover/focus 只做可读性提示。 */
.perm-mode-text {
  appearance: none;
  background: none;
  border: 0;
  padding: 0 2px;
  margin: 0;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-xs);
  line-height: 1;
  cursor: pointer;
  white-space: nowrap;
}
.perm-mode-text:hover { color: var(--text-strong); }
.perm-mode-text:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.perm-mode-text[data-loaded="0"] { color: var(--text-muted); }
</style>
