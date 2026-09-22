<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";

const session = useSessionStore();
/**
 * 草稿属于会话状态，不属于本组件：`text` 读写 store.draft，
 * 因此打开设置/星球再返回时草稿仍在（组件会被卸载重建）。
 */
const text = computed({
  get: () => session.draft,
  set: (v: string) => {
    session.draft = v;
  },
});
const inputRef = ref<HTMLTextAreaElement | null>(null);
/** 停止请求进行中（取消是可观察动作，不能假装已停） */
const stopping = ref(false);
const cancelError = ref("");

/** 输入框最大高度：不超过 40vh，也不超过 320px（超出后内部滚动） */
const MAX_INPUT_PX = 320;

/**
 * 停止按钮的文案。
 *
 * 提交之后、服务端 TURN_START 到达之前，界面知道「有任务在跑」但没有 turn_id。
 * 这段时间不再是死按钮：停止动作会退化为「取消后端当前的 active turn」，
 * 它只作用于真正在跑的那一轮，不会误伤排队消息。
 */
const stopLabel = computed(() => {
  if (stopping.value || session.cancelling) return "正在停止";
  return "停止";
});
const stopTitle = computed(() => {
  if (stopping.value || session.cancelling) return "正在停止…";
  if (!session.activeTurnId) return "停止当前任务（取消后端正在执行的那一轮）";
  return "停止当前任务";
});

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

/**
 * 「已登记、还没落实」的接续选择。
 *
 * 与 anchorText 的区别：anchorText 说的是「位置就在一段历史上」，
 * 这一条说的是「你选了从这段历史继续，下一条消息才会落实」——
 * 界面必须说清是哪种，否则用户会以为已经建了新片段（其实什么都没建）。
 */
const continuationText = computed(() => {
  const pending = session.pendingContinuation;
  if (!pending) return "";
  return pending.sourceTitle
    ? `下一条消息将从「${pending.sourceTitle}」继续`
    : "下一条消息将从所选历史继续";
});

const cancelling = ref(false);

async function cancelContinuation() {
  if (cancelling.value) return;
  cancelling.value = true;
  try {
    await api.cancelContinuation();
    // 服务端会广播新的 ANCHOR（pending 字段为空）→ 提示自动消失；
    // 这里不自行清空，避免「本地以为取消了、服务端还留着」。
  } catch {
    /* 取消失败：提示保留，状态以服务端为准 */
  } finally {
    cancelling.value = false;
  }
}

async function submit() {
  const value = text.value.trim();
  if (!value) return;
  const draftSnapshot = text.value;
  // 立即反馈：先清空（这一帧就能看到「已经交出去了」），再等请求结果
  text.value = "";
  // v-model 的清空是异步写回 DOM 的：必须等这一帧之后再测量，
  // 否则量到的还是旧内容的高度，输入框发送后不会收回原尺寸。
  await nextTick();
  autosize();
  const ok = await session.send(value);
  // 失败恢复：把草稿放回去，用户不必重写（若期间已输入新内容则不覆盖）
  if (!ok && !text.value.trim()) {
    text.value = draftSnapshot;
    await nextTick();
    autosize();
  }
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
  if (stopping.value) return;
  const target = session.activeTurnId;
  stopping.value = true;
  cancelError.value = "";
  session.cancelling = target ?? "active";
  try {
    const ok = await session.stopActiveTurn();
    if (!ok) {
      cancelError.value = session.lastError ?? "停止失败";
      // 失败才复位：成功时要一直显示「正在停止」，直到后端真的发出 TURN_END
      session.cancelling = null;
    }
  } finally {
    stopping.value = false;
  }
}
</script>

<template>
  <div class="composer bubble">
    <div class="topicbar">
      <span class="tname serif" :title="session.currentTopicId ?? undefined">{{ topicText }}</span>
      <!-- 有「待落实」的接续选择时不再并排显示「位置就在历史上」那一条：
           两句话说的是同一件事的两个阶段，并排会挤成一团、也读不清哪一句生效 -->
      <span v-if="anchorText && !continuationText" class="anchor mono">{{ anchorText }}</span>
      <!-- 已登记、还没落实的接续选择：说清「下一条消息才生效」，并允许取消 -->
      <template v-if="continuationText">
        <span class="anchor mono pending">{{ continuationText }}</span>
        <button
          class="anchor-cancel"
          type="button"
          :disabled="cancelling"
          @click="cancelContinuation"
        >
          {{ cancelling ? "取消中…" : "取消" }}
        </button>
      </template>
      <span class="spacer"></span>
      <span class="kbd-hint mono">Enter 发送 · Shift+Enter 换行</span>
    </div>

    <div class="input-row">
      <textarea
        ref="inputRef"
        id="composer-input"
        v-model="text"
        class="qio-input"
        aria-label="输入消息"
        placeholder="和 QIO 说点什么…"
        @keydown="onKeydown"
        @input="autosize"
      ></textarea>
      <button
        v-if="session.turnRunning"
        class="stop-btn"
        type="button"
        :disabled="stopping || !session.canStopTurn"
        :aria-label="stopLabel"
        :title="stopTitle"
        @click="stopTurn"
      >
        <span class="sq"></span>
        <span class="stop-label">{{ stopLabel }}</span>
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
  /* 独立悬浮于消息流之上，与对话内容同一列（居中 860px），不参与消息流布局；
     消息流底部通过滚动缓冲让出本气泡高度，滚到底时最新消息停在气泡上方。

     左右贴边取「正文列」令牌，不再手算 `calc(50% - Npx)`：手算的偏移与列宽、
     与右侧给入口球留的通道都脱钩 —— 900–1400px 区间里它整体右移了 418px、
     右边缘溢出视口 194px（发送按钮跑到屏幕外），还盖住了停在右下角的入口球。 */
  position: fixed;
  left: var(--column-inset-left);
  right: var(--column-inset-right);
  width: auto;
  max-width: 860px;
  margin: 0 auto;
  bottom: 16px;
  z-index: 12;
  /* 默认中性边框 + 较轻阴影：不靠重描边和重阴影抢注意力 */
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 10px 18px 14px;
  background: var(--bg-surface);
  box-shadow: var(--shadow-2);
  transition: border-color var(--dur-fast) var(--ease-1), box-shadow var(--dur-fast) var(--ease-1);
}
/* 聚焦时只加强一层：外框变强调色；内层输入框不再同时叠一层边框 + 光晕 */
.composer:focus-within { border-color: var(--accent); }
.composer textarea.qio-input:focus {
  border-color: transparent;
  box-shadow: none;
  background: transparent;
}
/* 窄窗口：整宽贴底，不缩成小气泡、不遮挡文字 */
@media (max-width: 899px) {
  .composer {
    left: var(--sp-3);
    right: var(--sp-3);
    max-width: none;
  }
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
.anchor.pending { color: var(--accent); }
.anchor-cancel {
  flex-shrink: 0;
  background: none;
  border: 0;
  padding: 0;
  font: inherit;
  font-size: 10.5px;
  color: var(--link);
  cursor: pointer;
  letter-spacing: 0.04em;
}
.anchor-cancel:hover:not(:disabled) { text-decoration: underline; }
.anchor-cancel:disabled { color: var(--text-muted); cursor: default; }
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
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1),
    background var(--dur-fast) var(--ease-1);
}
.stop-btn:hover:not(:disabled) {
  border-color: var(--danger);
  color: var(--danger);
}
.stop-btn:active:not(:disabled) {
  transform: translateY(var(--shift-1));
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
  /* 状态切换连续：默认 → hover → pressed → disabled 都走同一层令牌 */
  transition: background var(--dur-fast) var(--ease-1), transform var(--dur-press) var(--ease-1-out),
    opacity var(--dur-fast) var(--ease-1);
}
.send-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(calc(var(--shift-1) * -1));
}
.send-btn:active:not(:disabled) {
  transform: translateY(var(--shift-1));
}
.send-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
</style>
