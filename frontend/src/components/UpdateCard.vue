<script setup lang="ts">
/**
 * 「更新」卡片（设置 → 数据与维护）：显示当前版本 + 一个动作按钮 + 进度/结果。
 *
 * 契约（spec §3）：按钮文案与可用性完全由真实状态决定；只有 `up-to-date` 才允许写
 * 「已是最新版本」；`failed` 必须写出失败原因并给重试。下载与安装只能由用户点击触发。
 */
import { computed, onMounted } from "vue";
import { useUpdaterStore } from "../stores/updater";

const updater = useUpdaterStore();

onMounted(() => {
  updater.loadAutoCheckPreference();
  // 只读本地版本号；联网检查由用户点击或启动后的 autoCheck 负责
  void updater.loadCurrentVersion();
});

const checkLabel = computed(() => {
  if (updater.phase === "checking") return "正在检查…";
  if (updater.phase === "failed") return "重试";
  return "检查更新";
});

const showProgress = computed(
  () => updater.phase === "downloading" || updater.phase === "ready",
);

const progressText = computed(() => {
  if (updater.progressPercent === null) return "正在下载…";
  const total = updater.total ? ` / ${(updater.total / 1024 / 1024).toFixed(1)}MB` : "";
  const got = updater.downloaded ? `${(updater.downloaded / 1024 / 1024).toFixed(1)}MB` : "";
  return `${got}${total} · ${updater.progressPercent}%`;
});
</script>

<template>
  <div class="update-card qio-card">
    <div class="head">
      <span class="title">应用更新</span>
      <span class="version mono">当前版本 {{ updater.currentVersion || "—" }}</span>
      <span v-if="updater.hasUpdate" class="dot qio-state info">有更新</span>
    </div>

    <p v-if="updater.phase === 'up-to-date'" class="line ok" role="status">
      已是最新版本<span v-if="updater.lastCheckedAt" class="mono hint">
        （检查于 {{ new Date(updater.lastCheckedAt).toLocaleTimeString() }}）</span>
    </p>
    <p v-else-if="updater.phase === 'available'" class="line" role="status">
      发现新版本 <span class="mono strong">{{ updater.availableVersion }}</span>
      <span v-if="updater.notes" class="notes">{{ updater.notes }}</span>
    </p>
    <p v-else-if="updater.phase === 'ready'" class="line ok" role="status">
      已下载完成，重启后生效
    </p>
    <p v-else-if="updater.phase === 'failed'" class="line err" role="alert">
      {{ updater.message }}
    </p>
    <p v-else-if="updater.phase === 'downloading'" class="line" role="status">
      正在下载更新…
    </p>
    <p v-else class="line hint">更新会在应用内完成：下载后由你决定何时重启。</p>

    <div v-if="showProgress" class="progress" role="progressbar"
         :aria-valuenow="updater.progressPercent ?? 0" aria-valuemin="0" aria-valuemax="100">
      <div class="bar" :style="{ width: `${updater.progressPercent ?? 0}%` }"></div>
    </div>
    <p v-if="showProgress" class="mono hint">{{ progressText }}</p>

    <div class="actions">
      <button
        v-if="updater.phase === 'available'"
        class="qio-btn primary"
        data-action="download"
        type="button"
        @click="updater.download()"
      >
        下载并安装
      </button>
      <button
        v-else-if="updater.phase === 'ready'"
        class="qio-btn primary"
        data-action="restart"
        type="button"
        @click="updater.restart()"
      >
        立即重启
      </button>
      <button
        v-else
        class="qio-btn"
        data-action="check"
        type="button"
        :disabled="updater.busy"
        @click="updater.check()"
      >
        {{ checkLabel }}
      </button>

      <label class="auto">
        <input
          type="checkbox"
          :checked="updater.autoCheck"
          @change="updater.setAutoCheck(($event.target as HTMLInputElement).checked)"
        />
        启动后自动检查更新
      </label>
    </div>

    <p class="hint foot">
      更新包经过签名校验后才会安装；安装过程中应用会短暂关闭并自动重启。
    </p>
  </div>
</template>

<style scoped>
.update-card { display: flex; flex-direction: column; gap: 8px; }
.head { display: flex; align-items: center; gap: 10px; }
.title { font-size: var(--fs-md); color: var(--text-strong); }
.version { margin-left: auto; font-size: var(--fs-xs); color: var(--text-muted); }
.dot { font-family: var(--mono); }
.line { margin: 0; font-size: 13px; line-height: 1.6; color: var(--text-primary); }
.line.ok { color: var(--success); }
.line.err { color: var(--danger); }
.line.hint, .hint { color: var(--text-muted); font-size: 12px; }
.strong { color: var(--text-strong); }
.notes { display: block; margin-top: 4px; color: var(--text-secondary); }
.progress {
  height: 4px; border-radius: var(--r-pill); background: var(--bg-inset); overflow: hidden;
}
.bar { height: 100%; background: var(--accent); transition: width var(--mo-2-move) var(--ease-2); }
.actions { display: flex; align-items: center; gap: 12px; margin-top: 2px; flex-wrap: wrap; }
.auto { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--text-secondary); }
.foot { margin: 2px 0 0; }
</style>
