<!--
  启动状态（BootStatus）：App.vue 的 boot 段专用。

  规则（统一改造 · 启动/更新/关闭）：
  - 只保留「简洁标识 + 状态」：不铺一整块说明、不做成大卡片；
  - 只有**超过 10 秒**才显示「已等待 N 秒」（更短的时间不打扰）；
  - 技术细节默认折叠（日志路径、可能拖慢启动的系统程序），需要时再展开；
  - 失败时如实显示真实原因 + 重试，并立即可见 —— 不一直冒充「启动中」。
  - 状态与计时归 App.vue 的 boot()/waitForBackend 真实结果，本组件只负责显示。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { BOOT_SLOW_MS } from "../services/boot";

const props = withDefaults(
  defineProps<{
    state?: "starting" | "failed";
    /** 已经等待的秒数（由调用方每秒更新；0 表示刚开始） */
    waitedSeconds?: number;
    /** 失败的真实原因（waitForBackend 抛出的原文）；成功/进行中为空 */
    detail?: string;
  }>(),
  { state: "starting", waitedSeconds: 0, detail: "" },
);

const emit = defineEmits<{ retry: [] }>();

const showDetails = ref(false);
/** >10s 才提示等待时长；更短的时间保持安静。 */
const slow = computed(
  () => props.state === "starting" && props.waitedSeconds * 1000 >= BOOT_SLOW_MS,
);
const failed = computed(() => props.state === "failed");
</script>

<template>
  <div class="boot-status" :class="{ failed }" role="status" :aria-live="failed ? 'assertive' : 'polite'">
    <!-- 简洁标识：一个安静的状态点（不做品牌大图，也不放 emoji/字符图标） -->
    <span class="boot-mark" :class="{ pulse: !failed }" aria-hidden="true"></span>
    <p class="boot-title">
      {{ failed ? "QIO 没有启动完成" : "正在启动 QIO" }}
    </p>
    <p v-if="failed" class="boot-reason">{{ detail || "后端没有应答" }}</p>
    <p v-else-if="slow" class="boot-wait mono">已等待 {{ waitedSeconds }} 秒</p>
    <p v-else class="boot-wait-placeholder" aria-hidden="true"></p>

    <p v-if="failed" class="boot-hint">可以点「重试」接着等；如果本机有程序拦住系统命令，启动会比平时慢。</p>

    <div class="boot-actions">
      <button v-if="failed" class="qio-btn primary boot-retry" type="button" @click="emit('retry')">
        重试
      </button>
      <button
        class="qio-btn quiet boot-details-toggle"
        type="button"
        :aria-expanded="showDetails"
        @click="showDetails = !showDetails"
      >
        {{ showDetails ? "收起技术细节" : "技术细节" }}
      </button>
    </div>

    <div v-if="showDetails" class="boot-details mono">
      <p v-if="detail">原因：{{ detail }}</p>
      <p>日志：%LOCALAPPDATA%\com.qio.app\logs\QIO.log</p>
      <p v-if="!failed">
        本机若有程序拦住系统命令（例如 reg.exe 报 0xC0000142），启动会被拖住。
      </p>
    </div>
  </div>
</template>

<style scoped>
/* 居中对称：状态、原因、动作都在同一条中轴上；不铺满、不做成大卡片。 */
.boot-status {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
}
.boot-mark {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  opacity: 0.9;
}
.boot-mark.pulse {
  animation: boot-mark-pulse 1.4s var(--ease) infinite alternate;
}
@keyframes boot-mark-pulse {
  from {
    opacity: 0.3;
    transform: scale(0.86);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}
.boot-title {
  margin: 0;
  font-size: 15px;
  color: var(--text-strong);
}
.boot-wait {
  margin: 0;
  min-height: 18px;
  font-size: 12.5px;
  color: var(--text-secondary);
}
/* 未到 10 秒：占位但不显示任何数字，避免文字在 1s 后突然出现时上下跳。 */
.boot-wait-placeholder {
  margin: 0;
  min-height: 18px;
}
.boot-reason {
  margin: 0;
  max-width: min(46em, 100%);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--danger);
  overflow-wrap: anywhere;
}
.boot-hint {
  margin: 0;
  max-width: min(46em, 100%);
  font-size: 12px;
  line-height: 1.7;
}
.boot-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}
.boot-details {
  width: min(46em, 100%);
  margin-top: 4px;
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-muted);
  text-align: left;
  overflow-wrap: anywhere;
}
.boot-details p {
  margin: 0 0 4px;
}
.boot-details p:last-child {
  margin-bottom: 0;
}
@media (max-width: 520px) {
  .boot-actions {
    flex-direction: column;
    width: 100%;
  }
  .boot-actions .qio-btn {
    width: 100%;
  }
}
</style>
