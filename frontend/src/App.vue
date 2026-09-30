<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useEventStore } from "./stores/events";
import { useUiStore } from "./stores/ui";
import { useOnboardingStore } from "./stores/onboarding";
import ApprovalModal from "./components/ApprovalModal.vue";
import ApprovalEntry from "./components/ApprovalEntry.vue";
import DevTaskEntry from "./components/DevTaskEntry.vue";
import OnboardingWizard from "./components/onboarding/OnboardingWizard.vue";
import { waitForBackend } from "./services/boot";

const events = useEventStore();
const ui = useUiStore();
const onboarding = useOnboardingStore();

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
  <div class="app-shell">
    <!-- 后端还没应答：说清在等什么（以前这里是一片空白，看起来像卡死） -->
    <div v-if="bootState === 'starting'" class="boot-note" role="status">
      <p class="boot-title">正在启动 QIO 后端…</p>
      <p class="boot-meta mono">已等 {{ waitedSeconds }} 秒</p>
      <p v-if="waitedSeconds >= 10" class="boot-hint">
        比平时慢。本机若有程序拦住系统命令（例如 reg.exe 报 0xC0000142），启动会被拖住；
        日志在 %LOCALAPPDATA%\com.qio.app\logs\QIO.log。
      </p>
    </div>
    <div v-else-if="bootState === 'failed'" class="boot-note err" role="alert">
      <p class="boot-title">QIO 后端没有应答（已经等了 {{ waitedSeconds }} 秒）</p>
      <p class="boot-meta">{{ bootDetail }}</p>
      <p class="boot-hint">
        也可能只是启动很慢：点「重试」会接着等。日志：
        <span class="mono">%LOCALAPPDATA%\com.qio.app\logs\QIO.log</span>
      </p>
      <button class="qio-btn primary" type="button" @click="boot">重试</button>
    </div>
    <template v-else>
      <router-view />
      <!--
        对话页顶部的常驻提示都放在这一个容器里：它们各自 fixed 定位会互相盖住
        （真实情况：一句「上次那项操作没有执行」和「有 N 项操作等待确认」重叠）。
        容器本身不接收点击，只有里面的按钮可点。
      -->
      <div class="top-notes">
        <ApprovalEntry />
        <DevTaskEntry />
      </div>
      <ApprovalModal />
      <!-- 首次引导：真正首次启动，或「本版本还没展示过欢迎页」（刚更新的用户）时展开一次 -->
      <OnboardingWizard v-if="onboarding.showWizard" @done="onboarding.closeForSession()" />
    </template>
  </div>
</template>

<style>
:root {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--text-primary);
  background: var(--bg-base);
}
* { box-sizing: border-box; }
body { margin: 0; }
.app-shell { width: 100vw; height: 100vh; overflow: hidden; }
/* 启动状态：安静的一行说明 + 原因，不做成大卡片（它只是等几秒的事） */
.boot-note {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
}
.boot-title {
  margin: 0;
  font-size: 15px;
  color: var(--text-strong);
}
.boot-meta {
  margin: 0;
  font-size: 12.5px;
}
.boot-hint {
  margin: 0;
  max-width: 46em;
  font-size: 12px;
  line-height: 1.7;
}
.boot-note.err .boot-title { color: var(--danger); }
.boot-note.err button { margin-top: 6px; }
/*
 * 顶部居中的提示条（待确认的审批 / 上次没执行的操作 / 没做完的开发任务）。
 * 容器 shrink-to-fit，空白区域不挡下面页面的点击。
 */
.top-notes {
  position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
  z-index: 190; display: flex; flex-direction: column; align-items: center; gap: 8px;
  max-width: min(560px, calc(100vw - 32px));
  pointer-events: none;
}
</style>
