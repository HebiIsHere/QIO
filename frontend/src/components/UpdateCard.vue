<script setup lang="ts">
/**
 * 「更新」卡片（设置 → 数据与维护）：显示当前版本 + 一个动作按钮 + 进度/结果。
 *
 * 契约（spec §3）：按钮文案与可用性完全由真实状态决定；只有 `up-to-date` 才允许写
 * 「已是最新版本」；`failed` 必须写出失败原因并给重试。下载与安装只能由用户点击触发。
 *
 * 2026-10-26 统一改造：
 * - 下载 / 校验 / 结束本实例后端 / 安装是**不同阶段**，界面跟着真实阶段说，不把 100% 下载
 *   说成「安装完成」；拿不到总大小就不画百分比进度条（不伪造 0%）。
 * - 失败按「应用是否已恢复」分开：后端已恢复时更新设置仍在、可重试；后端没恢复时
 *   给出真实动作（完全退出 QIO 后重新启动），不再让用户以为重试会解决一切。
 * - 自动检查不再用原生外观 checkbox（自定义开关），更新发现后也**不会**自己弹提示：
 *   只在设置入口留一个小标记（见 WindowChrome）。
 */
import { computed, onMounted, watch } from "vue";
import { useUpdaterStore } from "../stores/updater";
import { useGlobalIssuesStore } from "../stores/globalIssues";

const updater = useUpdaterStore();
const issues = useGlobalIssuesStore();
/** 更新失败且后端未恢复 = 整体异常：进顶部全局栏入口（仍保留更新设置与真实重试）。 */
const UPDATE_ISSUE_ID = "updater_install_failed";

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

const downloading = computed(() => updater.phase === "downloading");
/** 有真实总大小时才显示百分比进度条；未知大小不画 0%（那是伪造进度）。 */
const showPercentBar = computed(() => downloading.value && updater.progressPercent !== null);

const stageText = computed(() => {
  if (updater.phase === "ready") return "安装已完成，重启 QIO 后生效";
  if (!downloading.value) return "";
  if (updater.stage === "verifying") return "下载完成，正在校验更新包（签名未通过不会安装）…";
  if (updater.stage === "preparing") return "校验通过，正在结束本实例后端（准备安装）…";
  if (updater.stage === "installing") return "正在安装更新（安装器启动中）…";
  return updater.progressPercent === null ? "正在下载更新（大小未知）…" : "正在下载更新…";
});

const progressText = computed(() => {
  if (updater.progressPercent === null) return "";
  const total = updater.total ? ` / ${(updater.total / 1024 / 1024).toFixed(1)}MB` : "";
  const got = updater.downloaded ? `${(updater.downloaded / 1024 / 1024).toFixed(1)}MB` : "";
  return `${got}${total} · ${updater.progressPercent}%`;
});

/**
 * 失败时应用是否已经恢复：`services/updater` 在安装失败路径会把后端恢复结果写进 message。
 * - 已恢复：应用还能继续用，更新设置仍在，重试/退出都可行；
 * - 未恢复：必须完全退出再启动，不能把「重试」当成解决办法。
 */
const backendRecovered = computed(() =>
  updater.phase === "failed" ? updater.message.includes("后端已恢复") : false,
);
const backendBroken = computed(() =>
  updater.phase === "failed"
    ? updater.message.includes("后端未能自动恢复") || updater.message.includes("后端未能恢复")
    : false,
);

function toggleAutoCheck() {
  updater.setAutoCheck(!updater.autoCheck);
}

/**
 * 失败分处（按应用是否已恢复）：
 * - 已恢复：留在这张更新卡片里 —— 更新设置仍在，可重试；不进全局异常；
 * - 未恢复：说明后端需要完全退出再启动 → 进顶部全局栏「独立问题」入口，
 *   带真实处理动作（打开数据与维护重新检查更新），入口数量与状态可在任何页面看到。
 * 成功/恢复后自动移除这一项（部分恢复只移除对应项）。
 */
function syncUpdateIssue(): void {
  if (updater.phase === "failed" && backendBroken.value) {
    issues.upsert({
      id: UPDATE_ISSUE_ID,
      kind: "restore_failed",
      title: "更新没有安装成功，后端仍未恢复",
      reason: updater.message,
      actions: [
        { label: "打开「数据与维护」重新检查更新", kind: "settings", section: "data" },
      ],
      diagnostics: [updater.message],
      severity: "error",
    });
    return;
  }
  issues.resolve(UPDATE_ISSUE_ID);
}

watch(() => [updater.phase, updater.message] as const, () => syncUpdateIssue(), { immediate: true });
</script>

<template>
  <div class="update-card qio-card" data-qio-fit>
    <div class="head">
      <span class="title">应用更新</span>
      <span class="version mono">当前版本 {{ updater.currentVersion || "—" }}</span>
      <span v-if="updater.hasUpdate" class="qio-state info update-mark">有更新</span>
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
      已下载并安装完成，重启后生效
    </p>
    <p v-else-if="updater.phase === 'failed'" class="line err" role="alert">
      <span class="err-reason">{{ updater.message }}</span>
      <span v-if="backendBroken" class="hint err-action">
        更新没有装上，且后端未能自动恢复。请完全退出 QIO 再重新启动；重启后可在设置里重新检查更新。
      </span>
      <span v-else-if="backendRecovered" class="hint err-action">
        应用已恢复运行，可以继续使用。更新没有安装，可以点「重试」重新下载安装。
      </span>
      <span v-else class="hint err-action">
        更新没有安装。可以点「重试」重新检查；若安装被中止（例如 "Can't write …qio-backend.exe"），
        请完全退出 QIO 后重试安装。
      </span>
    </p>
    <p v-else-if="downloading" class="line" role="status">
      {{ stageText }}
    </p>
    <p v-else class="line hint">更新会在应用内完成：下载并安装后由你决定何时重启。</p>

    <!-- 阶段：下载（有真实大小才画百分比）→ 校验 → 结束本实例后端 → 安装 -->
    <div
      v-if="showPercentBar"
      class="progress"
      role="progressbar"
      :aria-valuenow="updater.progressPercent ?? 0"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <div class="bar" :style="{ width: `${updater.progressPercent ?? 0}%` }"></div>
    </div>
    <p v-if="downloading || updater.phase === 'ready'" class="mono hint stage-line" aria-live="polite">
      {{ stageText }}<span v-if="showPercentBar && progressText"> · {{ progressText }}</span>
    </p>

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

      <button
        type="button"
        class="update-toggle auto-switch"
        role="switch"
        :aria-checked="updater.autoCheck"
        aria-label="启动后自动检查更新"
        :disabled="updater.phase === 'downloading'"
        @click="toggleAutoCheck"
      ></button>
      <span class="auto-text">启动后自动检查更新</span>
    </div>

    <p v-if="updater.phase === 'available'" class="hint note-ending">
      下载并安装会让你退出当前运行：安装时 QIO 会关闭，安装完成后重新打开；更新包经过签名校验后才会安装。
    </p>
    <p v-else-if="updater.phase === 'ready'" class="hint note-ending">
      重启会结束当前运行并重新打开 QIO；未完成的工作按现有恢复规则保留。
    </p>

    <p class="hint foot">
      连不上更新源时可在 <span class="mono">%APPDATA%\qio\updater-proxy.txt</span> 写一行代理地址；
      排查记录在 <span class="mono">%APPDATA%\qio\logs\qio.log</span>。自动检查只检查、不会自动下载或安装。
    </p>
  </div>
</template>

<style scoped>
.update-card { display: flex; flex-direction: column; gap: 8px; }
.head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.title { font-size: var(--fs-md); color: var(--text-strong); }
.version { margin-left: auto; font-size: var(--fs-xs); color: var(--text-muted); }
.update-mark { flex: none; }
.line { margin: 0; font-size: 13px; line-height: 1.6; color: var(--text-primary); }
.line.ok { color: var(--success); }
.line.err { color: var(--danger); display: flex; flex-direction: column; gap: 4px; }
.line.hint, .hint { color: var(--text-muted); font-size: 12px; }
.strong { color: var(--text-strong); }
.notes { display: block; margin-top: 4px; color: var(--text-secondary); }
.err-action { display: block; line-height: 1.6; overflow-wrap: anywhere; }
.progress {
  height: 3px; border-radius: var(--r-pill); background: var(--layer-hover); overflow: hidden;
}
.bar { height: 100%; background: var(--accent); transition: width var(--mo-2-move) var(--ease-2); }
.stage-line { margin: 0; overflow-wrap: anywhere; }
.actions { display: flex; align-items: center; gap: 10px; margin-top: 2px; flex-wrap: wrap; }
/* 自定义开关（不使用系统默认 checkbox；沿用 QIO 开关的视觉语言，
   但用独立 class 以免与设置页其它开关的选择器混淆）。 */
.auto-switch {
  flex: none;
  width: 38px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: var(--qio-ui-radius);
  background: var(--border-strong);
  position: relative;
  cursor: pointer;
  transition: background var(--dur-base) var(--ease);
}
.auto-switch::after {
  content: "";
  position: absolute;
  top: 3px;
  left: 3px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  transition: left var(--dur-base) var(--ease);
}
.auto-switch[aria-checked="true"] { background: var(--accent); }
.auto-switch[aria-checked="true"]::after { left: 19px; }
.auto-switch:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.auto-switch:disabled { opacity: 0.45; cursor: not-allowed; }
.auto-text { font-size: 12.5px; color: var(--text-secondary); }
.note-ending { margin: 2px 0 0; line-height: 1.6; }
.foot { margin: 2px 0 0; overflow-wrap: anywhere; }
@media (max-width: 520px) {
  .actions { align-items: flex-start; }
}
</style>
