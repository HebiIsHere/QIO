<script setup lang="ts">
/**
 * 按组保存的分组操作条：未保存标记 + 取消 + 保存。
 *
 * 为什么取消与保存必须成对出现在组尾部：
 * 用户改了几个字段后需要一眼看到「这组还没保存」，以及「怎么放弃」。
 * 保存中禁止重复提交；失败错误就在同一位置（不弹全局提示）。
 */
withDefaults(
  defineProps<{
    /** 分组 id，落在 data-settings-group 上，供验收脚本定位 */
    group: string;
    dirty: boolean;
    saving: boolean;
    error?: string | null;
    /** 未修改时显示的状态文案 */
    savedHint?: string;
  }>(),
  { error: null, savedHint: "已保存" },
);
defineEmits<{ save: []; cancel: [] }>();
</script>

<template>
  <div class="set-groupbar" :data-settings-group="group">
    <p v-if="error" class="set-notice err" role="alert">{{ error }}</p>
    <div class="set-groupbar__row">
      <span class="set-groupbar__state" :class="{ dirty }" role="status" data-settings-dirty>
        {{ dirty ? "有未保存的修改" : savedHint }}
      </span>
      <button
        type="button"
        class="qio-btn"
        data-settings-action="cancel"
        :disabled="!dirty || saving"
        @click="$emit('cancel')"
      >
        取消
      </button>
      <button
        type="button"
        class="qio-btn primary"
        data-settings-action="save"
        :disabled="!dirty || saving"
        :aria-busy="saving || undefined"
        @click="$emit('save')"
      >
        {{ saving ? "保存中…" : "保存" }}
      </button>
    </div>
  </div>
</template>
