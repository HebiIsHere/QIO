<script setup lang="ts">
/**
 * 待确认切换条（spec 第 29~30 条）。
 *
 * 「这段内容看起来属于另一个话题」时，低干扰地问一句：
 * 转到「X」？ 保留当前 / 转到这里
 *
 * 刻意不是模态弹窗：不遮罩、不抢焦点、不出现在窗口中央，
 * 只贴在输入区上方，用户不理它也不影响继续聊。
 */
withDefaults(defineProps<{ topicName: string; busy?: boolean }>(), { busy: false });
const emit = defineEmits<{ confirm: []; keep: [] }>();
</script>

<template>
  <div class="topic-switch" role="status" aria-live="polite">
    <span class="text">转到「{{ topicName }}」？</span>
    <span class="hint">这段内容看起来属于另一个话题</span>
    <span class="spacer" />
    <button class="qio-btn mini quiet" type="button" :disabled="busy" @click="emit('keep')">保留当前</button>
    <button class="qio-btn mini primary" type="button" :disabled="busy" @click="emit('confirm')">
      {{ busy ? "切换中…" : "转到这里" }}
    </button>
  </div>
</template>

<style scoped>
.topic-switch {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 auto 8px;
  max-width: 860px;
  padding: 7px 12px;
  /* 低干扰浮条：与状态徽章同一套原语，不再引用不存在的 --bg-raised/--dur-pop */
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-surface);
  font-size: 12.5px;
  color: var(--text-muted);
  /* 中频进入：短淡入 + 轻位移（reduced-motion 下自动降级为保留淡入、去掉位移） */
  animation: topic-switch-in var(--mo-2-in) var(--ease-2) both;
}

.topic-switch .text {
  color: var(--text-strong);
}

.topic-switch .hint {
  color: var(--text-faint);
  font-size: 11.5px;
}

.topic-switch .spacer {
  flex: 1;
}

@keyframes topic-switch-in {
  from {
    opacity: 0;
    transform: translateY(var(--shift-4));
  }
  to {
    opacity: 1;
    transform: none;
  }
}
</style>
