<!--
  右下角独立聊天入口（子智能体 C 负责实现）。

  契约：docs/interactive-mode-contract.md §8.1 / §8.3。

  这一版落地四件事：
  1. 点击展开 / 收起悬浮对话框；收起不清消息、不删草稿、不打断进行中的对话 ——
     消息、轮次、草稿都在 stores/session.ts 里，面板只是它的一扇窗；
  2. 直接复用会话能力（messages / turnRunning / draft / send / lastError），
     **不新建事件订阅**：订阅由 App.vue 统一建立，这里只读状态、只发文字，
     所以不会重复写消息、不会重复建轮次；
  3. 只发送输入的文字（POST /api/turns），不带板面、未提交改动、注释或选择范围，
     也不调用板面提交接口（POST /api/interactive/boards/{id}/submissions）；
  4. 外层容器透明：能看到后方板面，只有气泡、输入区这类必要控件用令牌里的轻量底色，
     不整块不透明、也不做重度模糊。

  为什么没有直接复用 MessageStream：它是**整页**虚拟滚动 + 底部跟随，会写
  session.streamScrollTop / streamFollowing，并订阅输入区几何做「回到最新」定位。
  放进 360–420px、固定高度的浮层会串改对话页的阅读位置与跟随状态。
  这里改用 MessageItem 做紧凑渲染（回答、工具卡、Markdown 都还是同一套渲染），
  滚动由面板自己管。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { useSessionStore } from "../../stores/session";
import MessageItem from "../MessageItem.vue";
import FailedSendNotice from "./FailedSendNotice.vue";
import {
  canSend,
  chatDraftHintText,
  chatScopeText,
  chatStatusText,
  normalizeSendText,
  olderMessagesText,
  sendFailureText,
  shouldSendOnKeydown,
} from "../../interactive/chat";

/**
 * 面板里最多渲染多少条消息。
 * 更早的内容没有被删：它们仍在会话状态里（对话页看得到），这里只是不重绘整个历史。
 */
const TAIL_LIMIT = 60;
/** 输入框最大高度：超过后输入框内部滚动，不把消息区挤没 */
const MAX_INPUT_PX = 132;

const store = useInteractiveStore();
const session = useSessionStore();

const inputRef = ref<HTMLTextAreaElement | null>(null);
const streamRef = ref<HTMLElement | null>(null);
const contentRef = ref<HTMLElement | null>(null);
/** 发送请求进行中（不是「对话在跑」：turnRunning 由 store 表达） */
const sending = ref(false);
/** 发送失败的真实原因（保留输入，不伪装成功） */
const failure = ref("");
/** 这条失败属于哪个话题：切到别的话题后不再把它显示成那边的失败（契约 §10.1） */
const failureTopic = ref<string | null>(null);
/** 对着空白按 Enter 时的提示：说清为什么不发，而不是静默 */
const blankNotice = ref("");
/** 是否跟随最新消息（用户上翻阅读后不再强行拉回） */
const following = ref(true);

const draft = computed({
  get: () => session.draft,
  set: (value: string) => {
    // 草稿属于会话状态，不属于本组件：收起面板再展开仍然在（与卡片草稿 store.drafts 分开）
    session.draft = value;
  },
});
const verdict = computed(() => canSend(draft.value));
const canSubmitNow = computed(() => verdict.value.ok && !sending.value);
const statusText = computed(() =>
  chatStatusText({
    turnRunning: session.turnRunning,
    queuedCount: session.queuedMessageIds.length,
  }),
);
const shownMessages = computed(() =>
  session.messages.length > TAIL_LIMIT ? session.messages.slice(-TAIL_LIMIT) : session.messages,
);
const hiddenText = computed(() => olderMessagesText(session.messages.length - shownMessages.value.length));
const scopeText = chatScopeText();
const hintText = chatDraftHintText();
/** 轮次级错误（例如后端说没有模型凭据）：如实显示，不当成发送失败 */
const turnError = computed(() => (failure.value ? "" : (session.lastError ?? "")));
/**
 * 会话的非致命警告（WARNING 事件，例如「还没有配置可用的模型凭据」）。
 * 这是「为什么没有回答」最直接的原因，必须显示，不能让用户对着一个空的等待框猜。
 */
const sessionWarning = computed(() => session.warning ?? "");

/**
 * 失败事实只属于它自己的话题（契约 §10.1）：
 * - 当前就在原话题：显示失败原因与**取回入口**（不自动写回、不自动重发）；
 * - 当前在别的话题：一个字都不显示、不改输入框、不改草稿 —— 那边没有发生过失败。
 */
const failureVisible = computed(
  () => Boolean(failure.value) && failureTopic.value === session.currentTopicId,
);

/**
 * 草稿保存状态（契约 §9.4）：失败必须显示真实原因并可重试，
 * 保存成功才说「已保存在本机」—— 绝不把失败说成已保存。
 */
const draftStatusText = computed(() => {
  if (session.draftSaveStatus === "saving") return "草稿保存中…";
  if (session.draftSaveStatus === "saved") return "草稿已保存在本机";
  return "";
});
const draftSaveError = computed(() =>
  session.draftSaveStatus === "error"
    ? "草稿未保存：" + (session.draftSaveError || "原因未知") + "（文字还在输入框里，可以重试）"
    : "",
);

/** 收起面板时把还没到防抖时间的内容落盘（草稿不清、也不丢） */
watch(
  () => store.chatOpen,
  (open) => {
    if (!open) session.flushDraft();
  },
);

function nearBottom(): boolean {
  const el = streamRef.value;
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
}

/**
 * 这段时间内到达的 scroll 事件**不算用户意图**。
 *
 * 两种事件会自己制造出来：程序化贴底，以及窗口/面板尺寸变化导致的重新换行。
 * 实测（360px 宽、160 条消息）：尺寸一变，浏览器就按新高度发一次 scroll，
 * 那一刻的位置看起来「离底部很远」（差 157px）。不设防就会把「跟随最新」
 * 误判成「用户在上翻阅读」，面板停在半截并弹出「回到最新」——而用户什么都没做。
 */
let ignoreScrollUntil = 0;

function onScroll() {
  if (Date.now() < ignoreScrollUntil) return;
  following.value = nearBottom();
}

/**
 * 用户自己滚动（滚轮）时才立刻解除上面的保护。
 * 滚轮事件先于它引起的 scroll 事件到达，所以「用户上翻」不会被误挡。
 */
function onUserScroll() {
  ignoreScrollUntil = 0;
}

/**
 * 窗口尺寸变化：先设保护再贴底。
 *
 * 顺序很关键：浏览器在尺寸变化时会**先**发出由布局引起的 scroll 事件、
 * 之后才轮到 ResizeObserver 回调。只在 ResizeObserver 里设保护就晚了一步 ——
 * 「跟随最新」会先被误判成 false，回调里的跟随判断直接不成立。
 */
function onWindowResize() {
  ignoreScrollUntil = Date.now() + 400;
  measureToolbar();
  if (following.value) void scrollToBottom(true);
}

async function scrollToBottom(force = false) {
  await nextTick();
  const el = streamRef.value;
  if (!el) return;
  if (!force && !following.value) return;
  ignoreScrollUntil = Date.now() + 300;
  el.scrollTop = el.scrollHeight;
  following.value = true;
}

// 新消息、以及一轮的阶段变化（等待中 → 生成中）都要跟着走，除非用户正在上翻阅读
watch(() => session.messages.length, () => void scrollToBottom());
watch(() => session.turnPhase, () => void scrollToBottom());

/**
 * 避让底部工具栏：**量真实工具栏**，而不是猜一个高度。
 *
 * 悬浮聊天在右下角，底部工具栏右端是提交区，两者抢同一个角落。工具栏高度随窗口
 * 换行变化（实测 1440px 下 ~110px、360px 下更高），写死一个值必然在某个宽度上压住提交按钮。
 * 这里用契约里冻结的钩子 [data-im="board-toolbar"] 量它的上边缘，换算成「离视口底部多少」，
 * 通过 --chat-dock-clearance 传给定位；量不到时退回可覆盖的令牌与默认值。
 */
const dockStyle = ref<Record<string, string>>({});

function measureToolbar() {
  const el = document.querySelector('[data-im="board-toolbar"]');
  if (!el) {
    dockStyle.value = {};
    return;
  }
  const rect = el.getBoundingClientRect();
  const clearance = Math.max(0, Math.round(window.innerHeight - rect.top)) + 8;
  /**
   * 面板最高只能到「工具栏上方剩下的空间」：否则窄窗口里工具栏一高，面板顶部会被挤出视口。
   *
   * 独立复核实测（800×600）：工具栏占了下半屏 304px，剩下的空间装不下 220px 的面板 +
   * 入口按钮，容器顶部被顶到 -193px，聊天内容完全看不到。
   * 所以这里**不再给 220px 的下限**：空间不够就让面板变矮、由它自己滚动（下限 160px，
   * 并且预留入口按钮 + 顶部身份栏的高度）。
   */
  const panelMax = Math.max(160, Math.round(window.innerHeight - clearance - 112));
  dockStyle.value = {
    "--chat-dock-clearance": clearance + "px",
    "--chat-panel-max-h": panelMax + "px",
  };
}

/** 内容或容器尺寸变化时重新贴底（窄窗口换行变多、面板被拉动都算） */
let resizeObserver: ResizeObserver | null = null;
/** 工具栏尺寸变化时重新量一次避让值 */
let toolbarObserver: ResizeObserver | null = null;

onMounted(async () => {
  measureToolbar();
  await scrollToBottom(true);
  inputRef.value?.focus({ preventScroll: true });
  // jsdom 里没有 ResizeObserver：没有就跳过（不影响功能，只影响「尺寸变化后自动贴底」）
  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(() => {
      // 尺寸/换行变化自带的 scroll 事件不是用户意图
      ignoreScrollUntil = Date.now() + 300;
      if (following.value) void scrollToBottom(true);
    });
    // 容器与内容都要观察：只观察容器时，「窗口变窄 → 文字换行变多 → 内容变高」
    // 不会再触发回调，画面就停在半截（实测过：390px 下 atBottom 变 false）。
    if (streamRef.value) resizeObserver.observe(streamRef.value);
    if (contentRef.value) resizeObserver.observe(contentRef.value);
  }
    const toolbar = document.querySelector('[data-im="board-toolbar"]');
    if (toolbar) {
      toolbarObserver = new ResizeObserver(() => measureToolbar());
      toolbarObserver.observe(toolbar);
    }
  window.addEventListener("resize", onWindowResize);
});

onBeforeUnmount(() => {
  // 面板被路由切换卸载：把待保存的草稿落盘（本机写入同步完成，不依赖网络）
  session.flushDraft();
  resizeObserver?.disconnect();
  resizeObserver = null;
  toolbarObserver?.disconnect();
  toolbarObserver = null;
  window.removeEventListener("resize", onWindowResize);
});

function autosize() {
  const el = inputRef.value;
  if (!el) return;
  if (!el.value.trim()) {
    el.style.height = "";
    return;
  }
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, MAX_INPUT_PX) + "px";
}

async function submit() {
  if (sending.value) return;
  const check = canSend(draft.value);
  if (!check.ok) {
    // 空白不发：把原因说出来，不要静默丢掉这次操作
    blankNotice.value = check.reason ?? "还没有输入内容";
    return;
  }
  const snapshot = draft.value;
  // 只发输入的文字：不追加板面、未提交改动、注释或选择范围
  const payload = normalizeSendText(snapshot);
  blankNotice.value = "";
  failure.value = "";
  /**
   * 归属在**点击这一刻**定下（契约 §10.1）：清空输入框、等待回执期间切换话题，
   * 都不许改变这条消息的去向，也不许把失败原文放进别的话题的输入框。
   */
  const attribution = session.sendAttribution();
  failureTopic.value = attribution.topicId;
  sending.value = true;
  draft.value = "";
  await nextTick();
  autosize();
  try {
    const ok = await session.send(payload);
    if (!ok) {
      // 失败事实由会话层统一记录：这里只显示原因。
      // 恢复交给会话层判断（只在原话题且输入框为空时放回），组件不自己写回任何输入框。
      failure.value = sendFailureText(session.lastError);
      session.retryFailedSend(attribution.draftId);
    }
  } catch (err) {
    failure.value = sendFailureText((err as Error).message);
  } finally {
    sending.value = false;
    await nextTick();
    autosize();
    void scrollToBottom(true);
    inputRef.value?.focus({ preventScroll: true });
  }
}

function onKeydown(event: KeyboardEvent) {
  // Enter 发送 / Shift+Enter 换行 / 输入法选字中不发送，判定都在纯函数里（可离线验证）
  if (!shouldSendOnKeydown(event)) return;
  event.preventDefault();
  void submit();
}
</script>

<template>
  <div class="chat-dock" :style="dockStyle">
    <section
      v-if="store.chatOpen"
      id="im-chat-panel"
      class="panel"
      data-im="chat-panel"
      aria-label="与 QIO 对话"
      @wheel.stop
    >
      <header class="panel-head">
        <span class="panel-title serif">对话</span>
        <span class="panel-status mono" data-im="chat-status">{{ statusText }}</span>
        <button class="panel-close" type="button" aria-label="收起对话" @click="store.chatOpen = false">
          收起
        </button>
      </header>

      <!--
        常驻只留一句；完整范围说明放进可打开的详情（契约 §10.8）。
        这里必须是 div：details 是块级元素，放进 p 里是无效 HTML（契约 §11.8 要求修掉）。
      -->
      <div class="panel-scope" data-im="chat-scope">
        <p class="scope-line">文字发送不会提交板面</p>
        <details class="scope-details">
          <summary data-im="chat-scope-details">说明</summary>
          <p class="scope-full">{{ scopeText }}</p>
        </details>
      </div>

      <div
        ref="streamRef"
        class="stream"
        data-im="chat-stream"
        @scroll="onScroll"
        @wheel.stop="onUserScroll"
      >
        <div ref="contentRef" class="stream-inner">
          <p v-if="!shownMessages.length" class="stream-empty" data-im="chat-empty">
            还没有消息。这里发送的文字会进入当前 QIO 会话（与对话页是同一个会话）。
          </p>
          <p v-else-if="hiddenText" class="stream-older mono" data-im="chat-older">{{ hiddenText }}</p>
          <MessageItem v-for="message in shownMessages" :key="message.id" :message="message" class="stream-item" />
        </div>
      </div>

      <button v-if="!following" class="to-latest" type="button" @click="scrollToBottom(true)">
        回到最新
      </button>

      <p v-if="sessionWarning" class="notice warn" role="status" data-im="chat-warning">{{ sessionWarning }}</p>
      <p v-if="turnError" class="notice err" role="status" data-im="chat-turn-error">{{ turnError }}</p>
      <!--
        失败原因 + 待恢复原文：与对话页共用同一个组件（契约 §11.4），
        只在原话题出现，不自动写回、不自动重发。长文限高滚动，不挤掉下面的输入行。
      -->
      <FailedSendNotice
        scope="chat"
        :notice="failureVisible ? failure : ''"
        :reason="failureVisible ? session.lastError : ''"
      />
      <p v-if="blankNotice" class="notice warn" role="status" data-im="chat-blank">{{ blankNotice }}</p>

      <p v-if="draftSaveError" class="notice err draft-status" role="alert" data-im="chat-draft-status">
        <span>{{ draftSaveError }}</span>
        <button class="draft-retry" type="button" data-im="chat-draft-retry" @click="session.retryDraftSave()">
          重试保存
        </button>
      </p>
      <p v-else-if="draftStatusText" class="draft-status mono" role="status" data-im="chat-draft-status">
        {{ draftStatusText }}
      </p>

      <div class="input-row">
        <textarea
          ref="inputRef"
          v-model="draft"
          class="input"
          data-im="chat-input"
          rows="2"
          aria-label="聊天输入"
          placeholder="和 QIO 说点什么…"
          @keydown.stop="onKeydown"
          @input="autosize"
        ></textarea>
        <button
          class="send"
          type="button"
          data-im="chat-send"
          :disabled="!canSubmitNow"
          :title="session.turnRunning ? '发送（会排队）' : '发送'"
          @click="submit"
        >
          {{ sending ? "发送中…" : "发送" }}
        </button>
      </div>
      <p class="panel-hint mono" :title="hintText">Enter 发送 · Shift+Enter 换行 · 中文选字时不发送</p>
    </section>

    <!-- 开关常驻右下角：展开后面板向上生长，入口不会被推到面板顶部 -->
    <button
      class="toggle"
      type="button"
      data-im="chat-toggle"
      :aria-expanded="store.chatOpen"
      aria-controls="im-chat-panel"
      @click="store.chatOpen = !store.chatOpen"
    >
      <span class="toggle-mark" aria-hidden="true">◍</span>
      <span>{{ store.chatOpen ? "收起对话" : "对话" }}</span>
      <span v-if="session.turnRunning" class="toggle-live mono" aria-hidden="true">进行中</span>
    </button>
  </div>
</template>

<style scoped>
/* 外层容器透明：面板外面什么都不铺，能看见后方板面 */
.chat-dock {
  position: absolute;
  right: var(--sp-4);
  /* 抬到底部工具栏上方：运行时量到的值优先，其次 A 可覆盖的令牌，最后默认值 */
  bottom: calc(var(--sp-4) + var(--chat-dock-clearance, var(--im-toolbar-clearance, 72px)));
  z-index: 25;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-2);
  background: transparent;
  /* 正文与操作一律无衬线（三声部）；标题单独用衬线，数据/时间用等宽 */
  font-family: var(--sans);
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font: inherit;
  font-size: var(--fs-sm);
  font-family: var(--sans);
  color: var(--on-accent);
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--r-pill);
  padding: var(--sp-2) var(--sp-4);
  cursor: pointer;
  box-shadow: var(--shadow-1);
}
.toggle:hover { background: var(--accent-hover); }
.toggle:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.toggle-mark { font-size: var(--fs-base); line-height: 1; }
.toggle-live {
  font-size: var(--fs-xs);
  color: var(--on-accent);
  border-left: 1px solid var(--on-accent);
  padding-left: var(--sp-2);
}
/* 面板：令牌里的高透低雾底色（不是不透明整块，也不做重度模糊），能看见后面的板面 */
.panel {
  width: min(420px, 92vw);
  height: min(62vh, 520px, var(--chat-panel-max-h, 520px));
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-2);
  overflow: hidden;
}
.panel-head {
  display: flex;
  flex-wrap: nowrap;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
  flex: none;
  /* 标题条局部稳定底色（§12.7）：既有半实色令牌，明暗两主题自动成立 */
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-1);
  padding: var(--sp-2) var(--sp-3);
}
.panel-title {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--text-strong);
}
.panel-status {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.panel-close {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.panel-close:hover { color: var(--accent-hover); }
.panel-close:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
/*
  能力边界：常驻一句「文字发送不会提交板面」，完整说明放在可打开的详情里。
  这里不再用 line-clamp 裁切完整说明（契约 §10.8 / §11.8）：详情是用户自己要打开的，
  打开后就要能读全。
*/
.panel-scope {
  flex: none;
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--text-muted);
  /* 说明条用与标题条一致的稳定底色，长说明展开后也不与板面文字相叠 */
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-1);
  padding: var(--sp-2) var(--sp-3);
}
.scope-line {
  margin: 0;
}
.scope-details {
  margin-top: 2px;
}
.scope-details summary {
  cursor: pointer;
  color: var(--link);
}
.scope-details summary:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.scope-full {
  margin: var(--sp-1) 0 0;
  overflow-wrap: anywhere;
}
.stream {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  display: flex;
  flex-direction: column;
  padding-right: var(--sp-1);
}
.stream-inner {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
/*
  可读性（契约 §10.8）：聊天**外层保持透明**（仍能看见板面），
  但消息与过程说明各自带一层轻底色 —— 板面上有长注释、代码块与虚线预览时，
  文字不能和后方文字混在一起。样式只作用于互动聊天（:deep 限定在 .stream 内），
  普通对话页的 MessageItem 外观不受影响。
*/
.stream-item { max-width: 100%; }
.stream :deep(.message .plain) {
  background: var(--glass-bg-strong);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-3);
}
.stream :deep(.message.user .plain) {
  /* 用户消息给更强的底色：它是「我说过的话」，最不该被背景吃掉 */
  background: var(--bg-elevated);
  border-color: var(--border-strong);
}
.stream :deep(.message .meta) {
  background: var(--glass-bg);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  display: inline-flex;
  gap: var(--sp-1);
}
.stream :deep(.tool-card),
.stream :deep(.qio-card) {
  background: var(--glass-bg-strong);
}
.stream :deep(.tool-detail-wrap) {
  background: var(--glass-bg);
}
.stream-empty,
.stream-older {
  margin: var(--sp-2) 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.stream-empty {
  /* 空闲说明也铺在透明区上：给它与 notice 一致的稳定底色，不与板面长文字相叠 */
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  overflow-wrap: anywhere;
}
.stream-older { color: var(--text-faint); }
.to-latest {
  align-self: center;
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 1px var(--sp-3);
  cursor: pointer;
}
/*
  提示条也要有稳定的局部底色（契约 §11.8）：板面后方有密集文字或连线时，
  直接铺在透明面板上的小字会与背景混在一起。这里不整块加不透明背景，
  只给提示自己一层轻底色 + 边界。
*/
.notice {
  flex: none;
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  overflow-wrap: anywhere;
}
.notice.err { color: var(--danger); }
.notice.warn { color: var(--warning); }
/* 草稿状态：贴着输入区的一行小字，不抢消息区的空间 */
.draft-status {
  flex: none;
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--text-faint);
  /* 草稿状态行同样贴着透明区铺字：给它与 notice 一致的稳定局部底色（§12.7） */
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
}
.draft-status.err { color: var(--danger); }
.draft-retry {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.draft-retry:hover { border-color: var(--link); }
.draft-retry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.input-row {
  flex: none;
  display: flex;
  align-items: flex-end;
  gap: var(--sp-2);
}
.input {
  flex: 1;
  min-width: 0;
  min-height: 40px;
  max-height: 132px;
  resize: none;
  font: inherit;
  font-family: var(--sans);
  font-size: var(--fs-base);
  line-height: var(--lh-tight);
  color: var(--text-strong);
  /*
    输入框用**稳定的局部底色**（契约 §11.8）：面板本身仍是透明玻璃，
    但正在写字的这一块不能和后方板面文字混在一起 —— 半透明渐变在密集
    注释/连线上会把光标前后的字吃掉。
  */
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: var(--sp-2) var(--sp-3);
}
.input::placeholder { color: var(--text-faint); }
.input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.send {
  flex: none;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-4);
  cursor: pointer;
}
.send:hover:not(:disabled) { background: var(--accent-hover); }
.send:disabled { opacity: 0.5; cursor: default; }
.send:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.panel-hint {
  flex: none;
  margin: 0;
  align-self: flex-start;
  font-size: 10.5px;
  line-height: 1.5;
  color: var(--text-faint);
  /*
    快捷键说明是最长的一行小字，直接铺在透明面板上时与后方板面文字相叠
    （800×600 暗色实机复测发现，同批 §12.7 口径）：给它一个紧凑的局部底色，
    明暗两主题都稳定；窄窗口下该行隐藏（既有规则不变）。
  */
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
}
/*
  矮窗口（480×600 这类浏览器边缘档、以及 800×600 的桌面最小窗口）：
  输入框不必保留整块最大高度 —— 面板里还要同时容下失败恢复块与发送按钮。
  实机复现过：输入框按 132px 撑满时，恢复块被压到 6px（看不见）或输入行被顶出面板被裁掉。
*/
@media (max-height: 700px) {
  .input { max-height: 84px; }
}
@media (max-width: 900px) {
  /* 窄窗口里工具栏会换行变高：默认避让值抬一档（量到真实高度时以量到的为准） */
  .chat-dock { bottom: calc(var(--sp-4) + var(--chat-dock-clearance, var(--im-toolbar-clearance, 112px))); }
}
@media (max-width: 560px) {
  .chat-dock { right: var(--sp-3); left: var(--sp-3); align-items: stretch; }
  .toggle { align-self: flex-end; }
  .panel { width: auto; height: min(58vh, 440px, var(--chat-panel-max-h, 440px)); }
  .panel-hint { display: none; }
}
</style>