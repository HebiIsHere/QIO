<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import { useEventStore } from "./stores/events";
import { useUiStore } from "./stores/ui";
import { useOnboardingStore } from "./stores/onboarding";
import { useApprovalsStore } from "./stores/approvals";
import { useSecurityStore } from "./stores/security";
import NonChatApprovalEntry from "./components/approval/NonChatApprovalEntry.vue";
import UnifiedApprovalLayer from "./components/conversation/UnifiedApprovalLayer.vue";
import DevTaskEntry from "./components/DevTaskEntry.vue";
import WindowChrome from "./components/WindowChrome.vue";
import OnboardingWizard from "./components/onboarding/OnboardingWizard.vue";
import QTipLayer from "./components/ui/QTipLayer.vue";
import GlobalFeedbackHost from "./components/feedback/GlobalFeedbackHost.vue";
import BootStatus from "./components/BootStatus.vue";
import SetupIncompleteNotice from "./components/onboarding/SetupIncompleteNotice.vue";
import { waitForBackend } from "./services/boot";
import { useWindowFrame } from "./composables/useWindowFrame";

/**
 * 窗口外框（0.3.2）：透明留白 + 20px 圆角 + 亮暗两套阴影；最大化/贴靠归零。
 * 必须在 setup 阶段就订阅壳状态通道（首屏 get + qio://window-state），
 * 否则 BootStatus 期间窗口已经是透明留白，data-window-* 却要等后端就绪才可能被写。
 */
useWindowFrame();

const events = useEventStore();
const ui = useUiStore();
const onboarding = useOnboardingStore();
const approvals = useApprovalsStore();
/**
 * 路由可能不存在（组件单测会直接挂载 App，不经过 router）。
 * 读不到路由时按「非对话页」处理：旧的全局入口保持原样，不因为缺路由而渲染崩溃。
 */
let routeRef: ReturnType<typeof useRoute> | undefined;
try {
  routeRef = useRoute();
} catch {
  routeRef = undefined;
}
/**
 * 对话页 v2 自己承载审批与任务入口（统一弹窗 / 左侧任务栏 / 输入区五入口）。
 * 旧的顶部居中悬浮入口只留给互动、设置、调试等页面，避免同一时刻出现两套按钮。
 */
const isConversation = computed(() => routeRef?.name === "conversation");
/**
 * 聊天路由 = 对话 + 互动。两页都由统一审批层接管；旧顶部审批入口/旧弹窗
 * 只留给设置、调试等非聊天页面，避免同一时刻出现两套按钮。
 * 与 WindowChrome 的 hasGlobalBar 口径保持一致（Phase 1-A）。
 */
const isChatRoute = computed(
  () => routeRef?.name === "conversation" || routeRef?.name === "interactive",
);
// R9：当前权限模式是常驻可见的事实，后端一就绪就恢复真实值（见下方 boot()）。
const security = useSecurityStore();

/**
 * 启动状态：后端没应答之前不渲染主界面。
 *
 * 真实事故（2026-09-24）：外壳启动被本机系统程序拖住 1 分 42 秒，窗口一直空白，
 * 用户只能判断"它没启动"。现在这段等待是有内容的，等不到还给原因与重试。
 */
const bootState = ref<"starting" | "ready" | "failed">("starting");
const bootDetail = ref("");
const waitedSeconds = ref(0);
let ticker: ReturnType<typeof setInterval> | null = null;

function stopTicker() {
  if (ticker) {
    clearInterval(ticker);
    ticker = null;
  }
}

async function boot() {
  bootState.value = "starting";
  bootDetail.value = "";
  waitedSeconds.value = 0;
  const started = Date.now();
  stopTicker();
  ticker = setInterval(() => {
    waitedSeconds.value = Math.floor((Date.now() - started) / 1000);
  }, 1000);
  try {
    await waitForBackend();
    bootState.value = "ready";
    // 后端确实在应答了，才建立事件流与首屏数据（提前连只会连到一个不存在的端口）
    events.connect();
    void ui.load();
    void onboarding.load();
    // 当前权限模式是常驻可见的事实：后端一就绪就恢复真实值（不靠事件）
    void security.load();
    // 审批生命周期 v2：断线/刷新/重启后主动恢复待审批项（旧后端没有该接口时静默跳过）
    void approvals.recoverPending();
  } catch (err) {
    bootState.value = "failed";
    bootDetail.value = (err as Error)?.message || "未知原因";
  } finally {
    stopTicker();
  }
}

onMounted(boot);
onUnmounted(stopTicker);
</script>

<template>
  <!-- 窗口外框：外层 fixed 铺满客户区，留白区透明（透出桌面/阴影带）；
       内层 surface 才有实底、20px 圆角与自绘阴影，并裁切所有非 fixed 内容。
       native/dwm 模式与最大化/贴靠时 --window-frame-pad/--window-radius 归零（见 tokens.css）。 -->
  <div class="window-frame">
    <div class="window-frame__surface">
      <div class="app-shell">
    <!-- 无边框窗口的自定义标题条：系统页眉没了，可拖拽与三个窗口按钮由它补上。
         它是 fixed 的窗口层，所以这里用 padding-top 把页面内容整体让下去一个条高，
         而不是靠浮在上面 —— 否则顶部那一条里的控件会「看得见、点不到」。 -->
    <WindowChrome />
    <!-- 启动状态：简洁标识 + 真实状态；超过 10 秒才显示等待秒数，技术细节折叠，失败可重试 -->
    <BootStatus
      v-if="bootState !== 'ready'"
      :state="bootState === 'failed' ? 'failed' : 'starting'"
      :waited-seconds="waitedSeconds"
      :detail="bootDetail"
      @retry="boot"
    />
    <template v-else>
      <router-view />
      <!--
        对话页顶部的常驻提示都放在这一个容器里：它们各自 fixed 定位会互相盖住
        （真实情况：一句「上次那项操作没有执行」和「有 N 项操作等待确认」重叠）。
        容器本身不接收点击，只有里面的按钮可点。
      -->
      <!--
        非聊天路由（设置 / 调试 / 星球）的统一审批入口：与聊天页同一个入口语义
        （有待办时亮出、可打开列表），没有待办时也能打开「结果与历史」。
        互动的 DevTaskEntry 仍可见（不与审批混在一起）。
      -->
      <div v-if="!isConversation" class="top-notes">
        <NonChatApprovalEntry v-if="!isChatRoute" />
        <DevTaskEntry />
      </div>
      <!-- 非聊天路由的审批弹窗：与聊天页是同一个 UnifiedApprovalModal（不是第二套按钮） -->
      <UnifiedApprovalLayer v-if="!isChatRoute" :show-memory="false" />
      <!-- 首次引导：真正首次启动，或「本版本还没展示过欢迎页」（刚更新的用户）时展开一次 -->
      <OnboardingWizard v-if="onboarding.showWizard" @done="onboarding.closeForSession()" />
    </template>
    <!-- 全局唯一的用途说明浮层（500ms 悬停 / 聚焦 / 触控长按；由 v-qio-tip 驱动） -->
    <QTipLayer />
    <!-- 顶部短通知 + 全局异常入口/面板 + 任务失败监听 + 输入门控（只挂一次） -->
    <GlobalFeedbackHost />
    <!-- 初始设置未完成的一次性中性提醒：只有真正显示过才记为已提醒（Lead 挂一次） -->
    <SetupIncompleteNotice />
      </div>
    </div>
  </div>
</template>

<style>
:root {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--text-primary);
  /* 透明窗：最外层不能有实底（实底在 .window-frame__surface） */
  background: transparent;
}
* { box-sizing: border-box; }
body { margin: 0; background: transparent; }
/*
 * 窗口外框（0.3.2，唯一一份窗口级几何）：
 * - 外层铺满 WebView 客户区，只负责透明留白；pointer-events:none 让留白带不拦截点击
 *   （留白处的桌面连接/穿透由壳处理，网页层不额外捕获）。
 * - surface 是唯一实底/圆角/阴影来源；overflow:hidden 只裁普通内容，
 *   不影响 fixed 后代（没有 transform/filter/contain，不创建包含块）。
 */
.window-frame {
  position: fixed;
  inset: 0;
  padding: var(--window-frame-pad);
  pointer-events: none;
}
.window-frame__surface {
  position: relative;
  width: 100%;
  height: 100%;
  border-radius: var(--window-radius);
  background: var(--bg-base);
  box-shadow: var(--window-shadow);
  overflow: hidden;
  pointer-events: auto;
}
/* 页面内容相对 surface 铺满；标题条是 fixed 窗口层，这里用 padding-top 让出一条 */
.app-shell { width: 100%; height: 100%; overflow: hidden; padding-top: var(--titlebar-height); }
/*
 * 顶部居中的提示条（待确认的审批 / 上次没执行的操作 / 没做完的开发任务）。
 * 容器 shrink-to-fit，空白区域不挡下面页面的点击。
 */
.top-notes {
  /* 顶部有「还没设置完」横幅时，对话页会把这条浮层让到横幅下面（见 ConversationView）；
     没有横幅时默认落在自定义标题条下方一个边距 —— 标题条是窗口层（z-index 240），
     比这里的 190 高，压在它底下会「看得见、点不到」。 */
  position: fixed; top: var(--qio-top-notes-offset, calc(var(--chrome-top) + 12px)); left: 50%; transform: translateX(-50%);
  z-index: 190; display: flex; flex-direction: column; align-items: center; gap: 8px;
  max-width: min(560px, calc(100vw - 2 * var(--window-frame-inset) - 32px));
  pointer-events: none;
}
</style>
