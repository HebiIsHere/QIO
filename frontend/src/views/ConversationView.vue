<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref } from "vue";
import { useSessionStore } from "../stores/session";
import MessageStream from "../components/MessageStream.vue";
import Composer from "../components/Composer.vue";
import PlanetDock from "../components/PlanetDock.vue";
import SettingsFloat from "../components/SettingsFloat.vue";
import PlanetBoot from "../components/PlanetBoot.vue";

// 星球页懒加载：three.js 不进首屏 chunk；加载期间立即显示「正在打开星球…」
const PlanetView = defineAsyncComponent({
  loader: () => import("./PlanetView.vue"),
  loadingComponent: PlanetBoot,
  delay: 0,
});
const planetOpen = ref(false);
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

onMounted(() => {
  session.loadHistory();
});
</script>

<template>
  <div class="conversation">
    <a class="skip-link" href="#composer-input" @click="focusComposer">跳到输入框</a>
    <!-- 错误 / 警告分开表达：错误要查，警告只需知道 -->
    <div v-if="session.lastError" class="notice err" role="alert">
      <span class="kind mono">错误</span>
      <span class="text">{{ session.lastError }}</span>
      <router-link to="/debug" class="link">查看详情</router-link>
      <router-link to="/settings" class="link">前往设置</router-link>
    </div>
    <div v-else-if="session.warning" class="notice warn" role="status">
      <span class="kind mono">提示</span>
      <span class="text">{{ session.warning }}</span>
    </div>
    <MessageStream />
    <Composer />
    <SettingsFloat />
    <PlanetDock @open="openPlanet" />
    <!-- 常驻复用同一实例：v-show 控制可见性，open 变化由星球页自己重置状态；
         旧层的收尾回调带的是它开始关闭时的序号，父级只认当前序号，不会关掉后来打开的新层 -->
    <PlanetView
      v-if="planetMounted"
      v-show="planetOpen"
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
</style>
