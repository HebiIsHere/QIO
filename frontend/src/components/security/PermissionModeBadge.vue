<script setup lang="ts">
/**
 * 权限模式常驻徽标（R9 契约 §1：当前模式必须可见）。
 *
 * * 显示的是**服务端恢复的模式**；还没读到时不冒充默认值，显示「读取中…」；
 * * 点击进入设置 →「工具与权限」由用户主动切换（徽标本身不改模式：提高模式只有用户确认入口）；
 * * 颜色只是辅助，文案始终写出模式名（颜色不是唯一通道）。
 */
import { computed } from "vue";
import { useSecurityStore } from "../../stores/security";
import { useUiStore } from "../../stores/ui";
import { PERMISSION_MODE_LABELS } from "../../services/security";

const security = useSecurityStore();
const ui = useUiStore();

const text = computed(() => {
  if (security.loaded) return security.currentLabel;
  if (security.error) return "权限模式未知";
  return "权限模式读取中…";
});

/** 会话状态色：只读 / 受限 / 无限制各有一个；颜色之外始终有文字。 */
const tone = computed(() => (security.loaded ? security.mode : "pending"));

const title = computed(() => {
  if (!security.loaded) {
    return security.error
      ? security.error + "（点一下进入设置查看）"
      : "正在从 QIO 读取当前权限模式";
  }
  return "当前权限模式：" + security.currentLabel + "。点一下进入设置 →「工具与权限」由你切换。";
});

/** 进入设置 →「工具与权限」：动态取 router，避免在只挂徽标的组件测试里强依赖路由注入。 */
function openSettings() {
  ui.settingsSection = "tools";
  void import("../../router")
    .then(({ default: router }) => router.push("/settings"))
    .catch(() => {
      // 路由不可用（例如单独挂载徽标的组件测试）：不能把一次点击变成未处理异常
    });
}


</script>

<template>
  <button
    type="button"
    class="perm-badge"
    :class="'is-' + tone"
    :data-mode="security.loaded ? security.mode : 'pending'"
    :data-loaded="security.loaded ? '1' : '0'"
    :title="title"
    :aria-label="title"
    data-im="permission-mode-badge"
    @click="openSettings"
  >
    <span class="dot" aria-hidden="true"></span>
    <span class="label">{{ text }}</span>
  </button>
</template>

<style scoped>
/* 清爽的一枚小徽标：不抢正文注意力，但任何时候都在。 */
.perm-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 9px;
  height: 22px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--bg-surface);
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--fs-xs);
  line-height: 1;
  cursor: pointer;
  white-space: nowrap;
}
.perm-badge:hover { color: var(--text-strong); border-color: var(--border-strong); }
.perm-badge:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-muted);
  flex: 0 0 auto;
}
/* 只读 = 中性；受限 = 强调边；无限制 = 警示色（文字始终写明模式名） */
.perm-badge.is-read_only .dot { background: var(--link); }
.perm-badge.is-restricted .dot { background: var(--success, var(--link)); }
.perm-badge.is-unrestricted { border-color: var(--danger); color: var(--text-primary); }
.perm-badge.is-unrestricted .dot { background: var(--danger); }
.perm-badge.is-pending .dot { background: var(--text-muted); }
.perm-badge.is-pending { color: var(--text-muted); }
</style>
