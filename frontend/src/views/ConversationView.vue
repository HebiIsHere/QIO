<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref } from "vue";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { useComposerClearance } from "../composables/useComposerClearance";
import MessageStream from "../components/MessageStream.vue";
import Composer from "../components/Composer.vue";
import PlanetDock from "../components/PlanetDock.vue";
import SettingsFloat from "../components/SettingsFloat.vue";
import PlanetBoot from "../components/PlanetBoot.vue";
import TopicSwitchPrompt from "../components/TopicSwitchPrompt.vue";
import KnowledgeCandidateCard from "../components/KnowledgeCandidateCard.vue";

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
  <div class="conversation">
    <a class="skip-link" href="#composer-input" @click="focusComposer">跳到输入框</a>
    <!-- 历史读取失败 ≠ 没有历史：低干扰提示 + 重试，且不清空已加载的内容 -->
    <!-- 状态行出现/消失必须有连续性（不再瞬切）；四类状态共用一套安静的行样式 -->
    <Transition name="qio-fade">
      <div v-if="session.history.status === 'error'" class="notice quiet" role="status">
        <span class="text">历史记录暂时无法读取</span>
        <button class="link" type="button" @click="retryHistory">重试</button>
      </div>
    </Transition>
    <!-- 错误 / 警告分开表达：错误要查，警告只需知道 -->
    <Transition name="qio-fade">
      <div v-if="session.lastError" class="notice err" role="alert">
        <span class="kind mono qio-state err">错误</span>
        <span class="text">{{ session.lastError }}</span>
        <router-link to="/debug" class="link">查看详情</router-link>
        <router-link to="/settings" class="link">前往设置</router-link>
      </div>
      <div v-else-if="session.warning" class="notice warn" role="status">
        <span class="kind mono qio-state warn">提示</span>
        <span class="text">{{ session.warning }}</span>
      </div>
      <!-- 凭据状态：暂停 / 撤销之后依赖它的能力会不可用，必须说出来（去哪里恢复）。
           这段文案来自后端事件，只在真的影响当前使用时出现；恢复后自己消失。 -->
      <div v-else-if="events.credentialNotice" class="notice warn" role="status">
        <span class="kind mono qio-state warn">提示</span>
        <span class="text">{{ events.credentialNotice }}</span>
        <router-link to="/settings" class="link">前往设置</router-link>
      </div>
      <!-- 取消是正常结局：安静地说一声，不当错误 -->
      <div
        v-else-if="session.lastTurnOutcome?.status === 'cancelled'"
        class="notice quiet"
        role="status"
      >
        <span class="kind mono qio-state quiet">已停止</span>
        <span class="text">这一轮已按你的要求停止，可以继续输入</span>
      </div>
    </Transition>
    <MessageStream />
    <!-- 底部三块（候选卡 / 兼容模式 / 话题切换）：用同一个「输入区让位」的量测
         整体抬到输入气泡上方。不这么做的话它们会被固定悬浮的输入区压住，
         按钮看得见、点不到（实测：按钮中点命中的是输入框）。 -->
    <div ref="bottomCluster" class="bottom-cluster">
    <!-- 高影响知识候选：只在回答完成之后出现，低干扰、不遮罩、不抢焦点 -->
    <div v-if="session.knowledgeCandidates.length" class="candidates">
      <KnowledgeCandidateCard
        v-for="c in session.knowledgeCandidates"
        :key="c.knowledgeId"
        :candidate="c"
      />
    </div>
    <!-- 能力降级：一次性、可忽略，不阻塞对话 -->
    <Transition name="qio-fade">
      <div v-if="events.fallbackNotice" class="notice quiet fallback" role="status">
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
  align-items: center;
  gap: 12px;
  /* 右侧留白避开右上角浮动 ⚙（44px 按钮 + 26px 边距） */
  padding: 8px 84px 8px 20px;
  background: var(--bg-surface);
  font-size: 12px;
  flex-shrink: 0;
}
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
}
.notice .text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
     输入区是固定悬浮层，不给它留位置的话这几块会被压住 */
}
.candidates {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0 24px;
  flex-shrink: 0;
  /* 候选多时自己滚，不把输入区挤出屏幕 */
  max-height: 42vh;
  overflow-y: auto;
}
.notice.fallback {
  padding-right: 84px;
}
</style>
