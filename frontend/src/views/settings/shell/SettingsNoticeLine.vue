<script setup lang="ts">
/**
 * 分区内联反馈（成功 / 失败 / 警告 / 信息）。
 *
 * 反馈按分区独立：一个总提交按钮被拆掉之后，「保存了哪个分区」必须由
 * 提示所在位置回答。成功提示 4 秒后由 useQioSettingsSave 收回，失败留到下次操作。
 *
 * #65 的口径与各分区一致：正文只放一句短话；失败原因（技术诊断）收在
 * 「技术详情」里按需展开；失败可关闭，能重试的给重试按钮。
 */
import type { SettingsNotice } from "../../../composables/useQioSettingsSave";

withDefaults(defineProps<{ notice: SettingsNotice | null; label?: string }>(), { label: "" });
const emit = defineEmits<{ dismiss: [] }>();
</script>

<template>
  <div v-if="notice" :data-settings-notice="label || notice.kind">
    <p class="set-notice" :class="notice.kind" :role="notice.kind === 'err' ? 'alert' : 'status'">
      {{ notice.text }}
    </p>
    <div class="set-channel-actions">
      <button
        v-if="notice.retry"
        type="button"
        class="qio-btn mini"
        data-settings-action="retry"
        @click="notice.retry()"
      >
        重试
      </button>
      <button
        type="button"
        class="qio-btn mini quiet"
        aria-label="关闭这项提示"
        @click="emit('dismiss')"
      >
        关闭
      </button>
    </div>
    <details v-if="notice.detail">
      <summary>技术详情</summary>
      <p class="set-notice info mono">{{ notice.detail }}</p>
    </details>
  </div>
</template>
