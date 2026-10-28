<script setup lang="ts">
/**
 * 分区内联反馈（成功 / 失败 / 警告 / 信息）。
 *
 * 反馈按分区独立：一个总提交按钮被拆掉之后，「保存了哪个分区」必须由
 * 提示所在位置回答。成功提示 4 秒后由 useQioSettingsSave 收回，失败留到下次操作。
 */
import type { SettingsNotice } from "../../../composables/useQioSettingsSave";

withDefaults(defineProps<{ notice: SettingsNotice | null; label?: string }>(), { label: "" });
</script>

<template>
  <p
    v-if="notice"
    class="set-notice"
    :class="notice.kind"
    :role="notice.kind === 'err' ? 'alert' : 'status'"
    :data-settings-notice="label || notice.kind"
  >
    {{ notice.text }}
  </p>
</template>
