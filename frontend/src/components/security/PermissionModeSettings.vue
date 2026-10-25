<script setup lang="ts">
/**
 * 设置 →「工具与权限」里的权限模式段（R9 契约 §1）。
 *
 * 规则只有三条：
 * 1. 三模式（只读 / 受限 / 完全无限制）由用户选择，**只有这个按钮事件**会调
 *    PUT /api/security/mode（user_confirmed=true）；模型/工具/子智能体没有入口；
 * 2. 旧数据值必须展示映射关系（旧值 default → 受限模式 等），不悄悄改用户的历史；
 * 3. 切换说明必须诚实：新操作立即生效，已开始的执行沿用开始时的权限；收紧不中断已开始的执行，
 *    放宽不自动批准等待中的审批。
 */
import { computed, ref } from "vue";
import { useSecurityStore } from "../../stores/security";
import {
  LEGACY_VALUE_LABELS,
  PERMISSION_MODES,
  PERMISSION_MODE_DESCRIPTIONS,
  PERMISSION_MODE_LABELS,
  PERMISSION_MODE_SAFETY_NOTE,
  permissionModeSwitchNote,
  type PermissionMode,
} from "../../services/security";
import { LEGACY_MODE_MAP } from "../../services/security";

const security = useSecurityStore();

/** 本次刚成功的切换说明（切换后仍能看到「已生效/沿用」的边界） */
const switchNote = ref("");

const current = computed(() => security.currentLabel);
const legacyText = computed(() => {
  const value = security.legacyValue;
  if (!value) return "";
  const mapped = LEGACY_MODE_MAP[value] ? PERMISSION_MODE_LABELS[LEGACY_MODE_MAP[value]] : "";
  const label = LEGACY_VALUE_LABELS[value] ?? "旧值：" + value;
  if (security.compat) {
    return "当前读取的是旧设置接口的值「" + value + "」（" + label + "）；按新语义展示为「" + current.value + "」" +
      (mapped ? "（旧值映射：" + value + " → " + mapped + "）" : "");
  }
  return "兼容旧客户端的等价旧值：" + value + "（" + label + "）→ " + current.value;
});

function choose(mode: PermissionMode) {
  if (security.saving || mode === security.mode) return;
  const from = security.mode;
  switchNote.value = permissionModeSwitchNote(from, mode);
  void security
    .changeMode(mode)
    .then(() => {
      // 成功：说明保留（它讲的是真实生效边界，不是「正在切换」）
      switchNote.value = permissionModeSwitchNote(from, security.mode);
    })
    .catch(() => {
      // 失败：store.error 已有原因；说明改成「未切换」的事实
      switchNote.value = "";
    });
}

/**
 * 读取动作只由**用户操作**触发（打开设置页后点「重新读取」，或 App 启动时的唯一一次恢复）。
 * 组件挂载不自动发请求：权限模式是全局状态，重复读取没有意义，还会在多实例挂载时互相干扰。
 */
function reload() {
  if (!security.loading) void security.load();
}
</script>

<template>
  <section class="sec perm-mode" data-im="permission-mode-settings">
    <h2>权限模式</h2>
    <p class="desc">
      权限模式决定 QIO 能直接做什么、什么必须经过你确认。当前模式常驻显示在输入区上方。
    </p>

    <p class="current-row" data-im="permission-mode-current">
      <span class="current-label">当前：{{ security.loaded ? current : security.loading ? "读取中…" : "尚未读取" }}</span>
      <span v-if="security.compat" class="compat-tag">兼容旧接口</span>
      <span v-if="security.loading" class="saving-tag">读取中…</span>
      <span v-else-if="security.saving" class="saving-tag">正在保存…</span>
      <button
        v-if="!security.loading && (!security.loaded || security.error)"
        type="button"
        class="reload-btn"
        data-im="permission-mode-reload"
        @click="reload"
      >重新读取</button>
    </p>
    <p v-if="legacyText && security.loaded" class="legacy mono" data-im="permission-mode-legacy">
      {{ legacyText }}
    </p>

    <div
      class="mode-list"
      role="radiogroup"
      aria-label="权限模式（只有你的选择能修改）"
      data-im="permission-mode-options"
    >
      <button
        v-for="mode in PERMISSION_MODES"
        :key="mode"
        type="button"
        class="mode-option"
        :class="{ selected: security.loaded && security.mode === mode }"
        role="radio"
        :aria-checked="security.loaded && security.mode === mode"
        :data-mode="mode"
        :data-selected="security.loaded && security.mode === mode ? '1' : '0'"
        :disabled="security.saving"
        @click="choose(mode)"
      >
        <span class="mode-name">
          {{ PERMISSION_MODE_LABELS[mode] }}
          <span v-if="mode === 'restricted'" class="mode-tag">默认</span>
        </span>
        <span class="mode-desc">{{ PERMISSION_MODE_DESCRIPTIONS[mode] }}</span>
      </button>
    </div>

    <p v-if="switchNote" class="switch-note" role="status" data-im="permission-mode-switch-note">
      {{ switchNote }}
    </p>
    <p v-if="security.error" class="msg err" role="alert" data-im="permission-mode-error">
      {{ security.error }}
    </p>

    <p class="safety-note">{{ PERMISSION_MODE_SAFETY_NOTE }}</p>
    <p class="safety-note">
      诚实说明：QIO 的受限子进程不是安全沙箱；它靠操作系统完整性级别与执行边界降低风险，但不等同于隔离环境。
    </p>
  </section>
</template>

<style scoped>
.perm-mode .desc { margin: 0 0 10px; }
.current-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--text-secondary);
}
.current-label { color: var(--text-strong); }
.compat-tag,
.saving-tag {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 5px;
}
.reload-btn {
  font: inherit;
  font-size: 11px;
  color: var(--link);
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 6px;
  height: 20px;
  cursor: pointer;
}
.reload-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.legacy {
  margin: 0 0 10px;
  font-size: 11.5px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.mode-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 10px 0 8px;
}
.mode-option {
  display: flex;
  flex-direction: column;
  gap: 3px;
  text-align: left;
  font: inherit;
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: 10px 12px;
  cursor: pointer;
}
.mode-option:hover:not(:disabled) { border-color: var(--border-strong); }
.mode-option:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.mode-option.selected {
  border-color: var(--link);
  background: var(--bg-inset);
}
.mode-option:disabled { opacity: 0.65; cursor: default; }
.mode-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  color: var(--text-strong);
}
.mode-tag {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 5px;
}
.mode-desc {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.switch-note {
  margin: 8px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--link);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.safety-note {
  margin: 10px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
.msg.err {
  color: var(--danger);
  font-size: 12px;
  margin: 8px 0 0;
}
</style>
