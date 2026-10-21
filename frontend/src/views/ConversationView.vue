<script setup lang="ts">
import { defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { useOnboardingStore } from "../stores/onboarding";
import { useComposerClearance } from "../composables/useComposerClearance";
import MessageStream from "../components/MessageStream.vue";
import Composer from "../components/Composer.vue";
import PlanetDock from "../components/PlanetDock.vue";
import SettingsFloat from "../components/SettingsFloat.vue";
import PlanetBoot from "../components/PlanetBoot.vue";
import TopicSwitchPrompt from "../components/TopicSwitchPrompt.vue";
import KnowledgeCandidateCard from "../components/KnowledgeCandidateCard.vue";
// P73 Stage 2：普通对话（mode=board）里 QIO 提议的板面意图，在对话内直接批准 / 拒绝
import BoardIntentInbox from "../components/interactive/BoardIntentInbox.vue";
import InterruptedTurnEntry from "../components/InterruptedTurnEntry.vue";
import RecoveryInbox from "../components/RecoveryInbox.vue";
import { titlebarHeight } from "../utils/titlebar";
import { vQioScrollbar } from "../composables/useQioScrollbars";

// 星球页懒加载：three.js 不进首屏 chunk；加载期间立即显示「正在打开星球…」
const PlanetView = defineAsyncComponent({
  loader: () => import("./PlanetView.vue"),
  loadingComponent: PlanetBoot,
  delay: 0,
});
const planetOpen = ref(false);
const events = useEventStore();
/** 底部三块的容器：让位高度由输入区实际高度决定（见 useComposerClearance） */
const bottomCluster = ref<HTMLElement | null>(null);
/**
 * 首次打开后才挂载，之后一直保留（关闭只是隐藏）。
 * 这样第二次打开不再重建整个 WebGL 场景（实测每次重建要 1.5–3.4s 冷启动）。
 */
const planetMounted = ref(false);
/**
 * 每次打开分配一个序号，关闭时星球页把序号带回来。
 * 只认当前序号：关闭动画进行中用户又打开了星球时，迟到的旧回调不会关掉新层
 * （否则会出现「刚打开就被上一次的关闭回调关掉」）。
 */
const planetSeq = ref(0);
const session = useSessionStore();
const onboarding = useOnboardingStore();
useComposerClearance(bottomCluster);

/**
 * 空闲时就把星球场景挂上：入口小球从此由**真实星球渲染**承担（同一个场景缩到入口尺度），
 * 而不是另画一张 2D 图。three.js 仍然是懒加载的 —— 它只是不在首屏 chunk 里，
 * 页面空闲后再取；在它就绪之前，入口由 `PlanetOrb` 顶着首屏。
 *
 * 两个克制的点：
 * - 正在跑一轮对话时不抢主线程（场景初始化实测 1.5–3.4s）；
 * - 用 requestIdleCallback，让浏览器自己挑空闲窗口。
 */
function mountPlanetWhenIdle() {
  const start = () => {
    if (planetMounted.value) return;
    if (session.turnRunning) {
      window.setTimeout(start, 1500);
      return;
    }
    planetMounted.value = true;
  };
  const ric = (window as unknown as { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number })
    .requestIdleCallback;
  if (typeof ric === "function") ric(start, { timeout: 4000 });
  else window.setTimeout(start, 2000);
}

function openPlanet() {
  planetSeq.value += 1;
  planetMounted.value = true;
  planetOpen.value = true;
}

/**
 * 顶部提示条（待确认的审批 / 上次没执行的操作 / 没做完的开发任务）是固定的
 * top-center 浮层，而「还没设置完」与「上次没执行的消息」都是正常流里的横幅 ——
 * 同时出现会叠在一起（真机验收看到的实际情况）。横幅在的时候，把浮层让到它们下面。
 *
 * 「上次没执行的消息」展开后高度会变，所以按**实测高度**让位，而不是写死一个常数：
 * ResizeObserver 盯着它，展开 / 收起 / 列表条数变化都会重新算。
 */
const interruptedRef = ref<HTMLElement | null>(null);
let interruptedObserver: ResizeObserver | null = null;

function syncTopNotesOffset() {
  const style = document.documentElement.style;
  const hint = onboarding.hintVisible ? 52 : 0;
  const entry = interruptedRef.value?.getBoundingClientRect().height ?? 0;
  const total = Math.round(hint + (entry ? entry + 8 : 0));
  // 这个值是「从视口顶端算起」的，横幅本身落在自定义标题条下面 —— 不加这一条，
  // 浮层会反过来压到横幅上（横幅在 y=32 起，浮层却按 y=52 放）。
  if (total > 0) style.setProperty("--qio-top-notes-offset", `${titlebarHeight() + total}px`);
  else style.removeProperty("--qio-top-notes-offset");
}

watch(
  [
    () => onboarding.hintVisible,
    () => session.interruptedTurns.length,
    // 收件箱（未完成事项）与它同处一个槽：条数或详情展开都会改高度，
    // 所以按实测高度重新让位，而不是写死一个常数。
    () => session.recoveryRecords.length,
    () => session.recoveryLoading,
  ],
  () => void nextTick(syncTopNotesOffset),
  { immediate: true },
);
watch(
  interruptedRef,
  (el, _prev, onCleanup) => {
    interruptedObserver?.disconnect();
    interruptedObserver = null;
    if (el && typeof ResizeObserver !== "undefined") {
      interruptedObserver = new ResizeObserver(() => syncTopNotesOffset());
      interruptedObserver.observe(el);
    }
    syncTopNotesOffset();
    onCleanup(() => {
      interruptedObserver?.disconnect();
      interruptedObserver = null;
    });
  },
  { flush: "post" },
);
onUnmounted(() =>
  document.documentElement.style.removeProperty("--qio-top-notes-offset"),
);

function closePlanet(seq?: number) {
  if (seq !== undefined && seq !== planetSeq.value) return;
  planetOpen.value = false;
}

/**
 * 「跳到输入框」：聊天页里每条消息都带复制按钮，键盘用户按 Tab 会被整条消息流
 * 挡住（实测长对话里前 8 个停靠点全是复制按钮）。第一个焦点位放一个跳过入口，
 * 键盘用户一步就能到输入框，鼠标用户看不到它。
 */
function focusComposer(e: MouseEvent) {
  const el = document.getElementById("composer-input");
  if (!el) return;
  e.preventDefault();
  (el as HTMLTextAreaElement).focus();
}

function retryHistory() {
  void session.retryHistory();
}

onMounted(() => {
  session.loadHistory();
  mountPlanetWhenIdle();
});
</script>

<template>
  <div class="conversation" data-qio-scope="chat">
    <!--
      普通对话根：data-qio-scope="chat" 的另一处落点。
      消息区的根（.stream）已经带这个属性（契约 §2.1）；这里再挂一份，
      让「对话页 = 一个对话根」在 DOM 上自证，也让固定悬浮的输入区同样拿到
      整套对话适配（长串换行 / 按钮重排）。两处作用域等价，不改变任何语义。
      注意：注释必须放在根元素**里面** —— 模板里根元素之前的注释会让组件根变成
      片段（Fragment），挂载方拿到的不再是那一个根元素。
    -->
    <a class="skip-link" href="#composer-input" @click="focusComposer">跳到输入框</a>
    <!-- 未完成首次引导时的轻提示：可关闭，关闭状态落库（spec §1） -->
    <div v-if="onboarding.hintVisible" class="setup-hint" role="status" data-qio-fit>
      <span class="text">还没设置完 QIO —— 补上模型、称呼与目标，它才会真正记得你</span>
      <button class="link resume" type="button" @click="onboarding.reopen()">继续设置</button>
      <button
        class="close"
        type="button"
        aria-label="关闭提示"
        @click="onboarding.setHintDismissed(true)"
      >
        ×
      </button>
    </div>
    <!--
      上次退出时「已经收下、但没有执行」的消息：不自动重发，只如实说出这件事，
      继续 / 忽略都由用户点。默认收起，出现时不抢焦点。
    -->
    <div ref="interruptedRef" class="interrupted-slot">
      <InterruptedTurnEntry />
      <!--
        「未完成事项」收件箱（A01 / A03）：与上面同一个槽、同一处入口，不另起页面。
        它覆盖上面那个入口管不到的部分 —— 孤儿重发、升级前无归属的历史行、
        归属判不出来 / 已死的派生任务。列表安静常驻，详情默认收起。
      -->
      <RecoveryInbox />
    </div>
    <!-- 历史读取失败 ≠ 没有历史：低干扰提示 + 重试，且不清空已加载的内容 -->
    <!-- 状态行出现/消失必须有连续性（不再瞬切）；四类状态共用一套安静的行样式 -->
    <Transition name="qio-fade">
      <div
        v-if="session.history.status === 'error'"
        class="notice quiet"
        role="status"
        data-qio-fit
      >
        <span class="text">历史记录暂时无法读取</span>
        <button class="link" type="button" @click="retryHistory">重试</button>
      </div>
    </Transition>
    <!-- 错误 / 警告分开表达：错误要查，警告只需知道 -->
    <Transition name="qio-fade">
      <div v-if="session.lastError" class="notice err" role="alert" data-qio-fit>
        <span class="kind mono qio-state err">错误</span>
        <span class="text">{{ session.lastError }}</span>
        <router-link to="/debug" class="link">查看详情</router-link>
        <router-link to="/settings" class="link">前往设置</router-link>
      </div>
      <div v-else-if="session.warning" class="notice warn" role="status" data-qio-fit>
        <span class="kind mono qio-state warn">提示</span>
        <span class="text">{{ session.warning }}</span>
      </div>
      <!-- 凭据状态：暂停 / 撤销之后依赖它的能力会不可用，必须说出来（去哪里恢复）。
           这段文案来自后端事件，只在真的影响当前使用时出现；恢复后自己消失。 -->
      <div v-else-if="events.credentialNotice" class="notice warn" role="status" data-qio-fit>
        <span class="kind mono qio-state warn">提示</span>
        <span class="text">{{ events.credentialNotice }}</span>
        <router-link to="/settings" class="link">前往设置</router-link>
      </div>
      <!-- 取消是正常结局：安静地说一声，不当错误 -->
      <div
        v-else-if="session.lastTurnOutcome?.status === 'cancelled'"
        class="notice quiet"
        role="status"
        data-qio-fit
      >
        <span class="kind mono qio-state quiet">已停止</span>
        <span class="text">这一轮已按你的要求停止，可以继续输入</span>
      </div>
      <!--
        不完整结束（契约 §七 C2：流在结束标记之前 EOF）：已确认正文保留，但这**不是完成**。
        安静地说清「未完成」并指出重试入口 —— 既不能收成「已完成」的样子，
        也不能悄悄吞掉（它不是错误，所以不进错误横幅；具体原因在过程区里）。
      -->
      <div
        v-else-if="session.lastTurnOutcome?.status === 'incomplete'"
        class="notice quiet"
        role="status"
        data-qio-fit
        data-test="turn-incomplete-notice"
      >
        <span class="kind mono qio-state quiet">未完成</span>
        <span class="text">这一轮没有正常结束：回答可能不完整，可以在过程区里「重试」</span>
      </div>
    </Transition>
    <MessageStream />
    <!-- 底部三块（候选卡 / 兼容模式 / 话题切换）：用同一个「输入区让位」的量测
         整体抬到输入气泡上方。不这么做的话它们会被固定悬浮的输入区压住，
         按钮看得见、点不到（实测：按钮中点命中的是输入框）。 -->
    <div ref="bottomCluster" class="bottom-cluster">
    <!--
      P73 Stage 2：待批准的板面意图（普通对话的 board 模式提案）。
      没有待批准条目不渲染；确认/拒绝后与每轮对话结束都会重读，不轮询。
    -->
    <BoardIntentInbox />
    <!--
      高影响知识候选：只在回答完成之后出现，低干扰、不遮罩、不抢焦点。
      候选多时**自己滚动**（不把输入区挤出屏幕）——它是一块独立并列的列表，
      用统一策略：只声明纵向指示条，原生滚动条外观已全局消除（不预留轨道空白），
      横向一律不滚动（data-qio-fit）。
    -->
    <div
      v-if="session.knowledgeCandidates.length"
      class="candidates"
      data-qio-fit
      v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
    >
      <KnowledgeCandidateCard
        v-for="c in session.knowledgeCandidates"
        :key="c.knowledgeId"
        :candidate="c"
      />
    </div>
    <!-- 能力降级：一次性、可忽略，不阻塞对话 -->
    <Transition name="qio-fade">
      <div v-if="events.fallbackNotice" class="notice quiet fallback" role="status" data-qio-fit>
        <span class="kind mono qio-state quiet">兼容模式</span>
        <span class="text">{{ events.fallbackNotice }}</span>
        <button class="link" type="button" @click="events.fallbackNotice = null">知道了</button>
      </div>
    </Transition>
    <!-- 推测切换：低干扰地问一句，不遮罩、不抢焦点；Anchor 在用户表态前一动不动 -->
    <TopicSwitchPrompt
      v-if="session.pendingSwitch"
      :topic-name="session.pendingSwitch.topicName"
      :busy="session.pendingSwitchBusy"
      @confirm="session.confirmPendingSwitch()"
      @keep="session.rejectPendingSwitch()"
    />
    <!-- 互动模式入口：与对话模式并列，安静的一行；不改变对话本身的行为 -->
    <div class="mode-entry-row">
      <router-link to="/interactive" class="link mode-entry">互动模式：一起整理材料与关系</router-link>
    </div>
    </div>
    <Composer />
    <SettingsFloat />
    <PlanetDock @open="openPlanet" />
    <!-- 常驻复用同一实例：**不再用 v-show 隐藏** —— 关着的时候它是对话页入口上的那颗小球
         （同一个 WebGL 场景缩到入口尺度渲染），藏起来就没有小球了。
         可见性与层级由星球页自己按 open / ball 状态决定；
         旧层的收尾回调带的是它开始关闭时的序号，父级只认当前序号，不会关掉后来打开的新层 -->
    <PlanetView
      v-if="planetMounted"
      :seq="planetSeq"
      :open="planetOpen"
      @close="closePlanet"
    />
  </div>
</template>

<style scoped>
/* 分层：消息流/Composer 依次铺在 --bg-base 上，输入区用 --bg-surface；顶部无状态条 */
.conversation {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
  /* 从设置返回：短淡入（内容同帧可见，不做整页位移与串联等待） */
  animation: conversation-in var(--dur-page-out) var(--ease-out) both;
}
@keyframes conversation-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.notice {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  /* 右侧留白避开右上角浮动 ⚙（44px 按钮 + 26px 边距） */
  padding: 8px 84px 8px 20px;
  background: var(--bg-surface);
  font-size: 12px;
  flex-shrink: 0;
  min-width: 0;
}
/* 「继续设置」轻提示：安静的一行，不抢消息流，可关闭 */
.setup-hint {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 84px 8px 20px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--accent-soft);
  color: var(--text-secondary);
  font-size: 12px;
  flex-shrink: 0;
  min-width: 0;
}
.setup-hint .text { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.setup-hint .close {
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
.setup-hint .close:hover { color: var(--text-strong); }
.notice.err {
  border-bottom: 1px solid var(--border-danger);
  color: var(--danger);
}
.notice.err .kind { color: var(--danger); }
.notice.warn {
  border-bottom: 1px solid var(--warning);
  color: var(--warning);
}
/* 安静的状态行：不抢注意力，也不显得像故障 */
.notice.quiet {
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
.notice.quiet .kind {
  color: var(--text-secondary);
}
.notice button.link {
  background: none;
  border: 0;
  padding: 0;
  font: inherit;
  cursor: pointer;
}
.notice .kind {
  font-size: 10.5px;
  letter-spacing: 0.08em;
  padding: 1px 8px;
  border-radius: var(--r-pill);
  border: 1px solid currentColor;
  flex-shrink: 0;
  /* 与首行文字对齐（错误原文会换成多行） */
  margin-top: 1px;
}
.notice .text {
  flex: 1;
  min-width: 0;
  /* 错误 / 提示原文完整换行显示：单行截断会把真实原因藏起来（换个行也不会横向滚动） */
  overflow-wrap: anywhere;
}
.notice .link {
  color: var(--link);
  text-decoration: none;
  flex-shrink: 0;
  letter-spacing: 0.04em;
}
.notice .link:hover {
  text-decoration: underline;
}
/* 知识候选与降级提示都贴在对话流底部、输入区上方：位置贴近发生的地方 */
.bottom-cluster {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  /* 下内边距由 useComposerClearance 按输入区的实际高度写入：
     输入区是固定悬浮层，不给它留位置的话这几块会被压住。
     **空块不写内边距**：以前即使没有任何内容也留出「输入区高度 + 16px」，
     消息区底边因此被抬高，靠 sticky 贴底的那类元素也跟着飘高。 */
}
.candidates {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
  min-width: 0;
  /* 候选多时自己滚，不把输入区挤出屏幕；横向不建立滚动范围 */
  max-height: 42vh;
  overflow-y: auto;
  overflow-x: clip;
}
.notice.fallback {
  padding-right: 84px;
}
/* 互动模式入口：安静的一行，居中在输入区上方，不抢消息流与输入焦点 */
.mode-entry-row {
  display: flex;
  justify-content: center;
  padding: 0 24px 4px;
  flex-shrink: 0;
}
.mode-entry {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  text-decoration: none;
}
.mode-entry:hover { color: var(--link); }
.mode-entry:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
