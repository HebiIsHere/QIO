<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const session = useSessionStore();
const text = ref("");
const inputRef = ref<HTMLTextAreaElement | null>(null);
/** 停止请求进行中（取消是可观察动作，不能假装已停） */
const stopping = ref(false);
const cancelError = ref("");

/** 输入框最大高度：不超过 40vh，也不超过 320px（超出后内部滚动） */
const MAX_INPUT_PX = 320;

const topicText = computed(() => session.topicName || (session.currentTopicId ? "当前话题" : "默认话题"));

const anchorText = computed(() => {
  // 只有「历史位置」才提示：成功一轮后位置推进到当前片段，提示自动消失
  if (!session.anchorHistoric) return "";
  const f = session.anchorFragment;
  if (f?.title) return `从「${f.title}」继续`;
  // 不暴露内部片段 ID，也不写 anchor 这类内部术语
  if (session.anchorFragmentId) return "从选中的历史位置继续";
  return "";
});

async function submit() {
  const value = text.value.trim();
  if (!value) return;
  session.send(value);
  text.value = "";
  // v-model 的清空是异步写回 DOM 的：必须等这一帧之后再测量，
  // 否则量到的还是旧内容的高度，输入框发送后不会收回原尺寸。
  await nextTick();
  autosize();
}

function onKeydown(e: KeyboardEvent) {
  // IME：选词/组字过程中的 Enter 属于输入法，不能当发送
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void submit();
  }
}

function autosize() {
  const el = inputRef.value;
  if (!el) return;
  // 空草稿：清掉内联高度，回到浏览器自然尺寸（与刚打开时完全一致）
  if (!el.value.trim()) {
    el.style.height = "";
    return;
  }
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, MAX_INPUT_PX) + "px";
}

/** 停止当前 active turn：显示「正在停止」直到后端真正结束（TURN_END） */
async function stopTurn() {
  const turnId = session.activeTurnId;
  if (!turnId || stopping.value) return;
  stopping.value = true;
  cancelError.value = "";
  session.cancelling = turnId;
  try {
    await api.cancelTurn(turnId);
  } catch (e) {
    cancelError.value = `停止失败：${(e as Error).message}`;
    session.cancelling = null;
  } finally {
    stopping.value = false;
  }
}
</script>

<template>
  <div class="composer bubble">
    <div class="topicbar">
      <span class="tname serif" :title="session.currentTopicId ?? undefined">{{ topicText }}</span>
      <span v-if="anchorText" class="anchor mono">{{ anchorText }}</span>
      <span class="spacer"></span>
      <span class="kbd-hint mono">Enter 发送 · Shift+Enter 换行</span>
    </div>

    <div class="input-row">
      <textarea
        ref="inputRef"
        v-model="text"
        class="qio-input"
        placeholder="和 QIO 说点什么…"
        @keydown="onKeydown"
        @input="autosize"
      ></textarea>
      <button
        v-if="session.turnRunning"
        class="stop-btn"
        type="button"
        :disabled="stopping || !session.activeTurnId"
        :aria-label="stopping ? '正在停止' : '停止当前任务'"
        :title="stopping ? '正在停止…' : '停止当前任务'"
        @click="stopTurn"
      >
        <span class="sq"></span>
        <span class="stop-label">{{ stopping || session.cancelling ? "正在停止" : "停止" }}</span>
      </button>
      <button
        class="send-btn"
        type="button"
        :disabled="!text.trim()"
        :aria-label="session.turnRunning ? '排队发送' : '发送'"
        :title="session.turnRunning ? '排队发送（Enter）' : '发送（Enter）'"
        @click="submit"
      >
        <span>↑</span>
      </button>
    </div>
    <p v-if="cancelError" class="cancel-error" role="alert">{{ cancelError }}</p>
  </div>
</template>

<style scoped>
/* 右下角大气泡：融入对话页 flex 布局（消息流下方、靠右），
   浅色表面 + 玫红描边 + 右下小圆角，不可拖动；
   增高时自动向上生长，消息流结束于气泡上方，天然不遮挡 */
.composer {
  /* 独立悬浮于消息流之上（右下角），不参与消息流布局；
     消息流底部通过滚动缓冲让出本气泡高度，滚到底时最新消息停在气泡上方 */
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 12;
  width: min(560px, calc(100vw - 32px));
  border: 1.5px solid var(--accent);
  border-radius: 18px 18px 4px 18px;
  padding: 10px 18px 16px;
  background: var(--bg-surface);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.4);
}
.topicbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 12px;
}
.tname {
  font-weight: 600;
  color: var(--text-strong);
  font-size: 15px;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.anchor {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.spacer {
  flex: 1;
}
.kbd-hint {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.input-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}
.input-row textarea.qio-input {
  flex: 1;
  min-height: 46px;
  /* 随内容增长，到上限后内部滚动：长输入不会把聊天窗口挤成一条 */
  max-height: min(40vh, 320px);
  resize: none;
}
.stop-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  height: 38px;
  padding: 0 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.stop-btn:hover:not(:disabled) {
  border-color: var(--danger);
  color: var(--danger);
}
.stop-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.stop-btn .sq {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: currentColor;
}
.stop-label {
  white-space: nowrap;
}
.cancel-error {
  margin-top: 8px;
  font-size: 12px;
  color: var(--danger);
}
.send-btn {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  border: none;
  border-radius: 50%;
  background: var(--accent);
  color: var(--on-accent);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.18s, transform 0.15s;
}
.send-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(-1px);
}
.send-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
</style>
