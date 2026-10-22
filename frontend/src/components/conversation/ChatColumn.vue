<script setup lang="ts">
/**
 * 中央对话工作区（B 负责完整实现；这里先保证基线能力可用）。
 *
 * 结构：话题栏 → 全局异常/轻提示 → 消息流 → 高影响知识候选等底部块 → 五入口+输入区。
 * 基线内容来自旧 ConversationView 的真实能力（流式、工具详情、历史加载、候选卡、
 * 中断/恢复收件箱、话题切换、降级提示），B 在此基础上重排与补齐。
 */
import { computed } from "vue";
import { useSessionStore } from "../../stores/session";
import { useEventStore } from "../../stores/events";
import { useOnboardingStore } from "../../stores/onboarding";
import MessageStream from "../MessageStream.vue";
import KnowledgeCandidateCard from "../KnowledgeCandidateCard.vue";
import InterruptedTurnEntry from "../InterruptedTurnEntry.vue";
import RecoveryInbox from "../RecoveryInbox.vue";
import TopicSwitchPrompt from "../TopicSwitchPrompt.vue";
import ComposerDock from "./ComposerDock.vue";

const session = useSessionStore();
const events = useEventStore();
const onboarding = useOnboardingStore();

const topicTitle = computed(() => session.topicName || "当前对话");
const anchorTitle = computed(() => session.anchorFragment?.title || "");

function focusComposer(e: MouseEvent) {
  const el = document.getElementById("composer-input");
  if (!el) return;
  e.preventDefault();
  (el as HTMLTextAreaElement).focus();
}

function retryHistory() {
  void session.retryHistory();
}
</script>

<template>
  <section class="chat-column" aria-label="对话工作区">
    <a class="skip-link" href="#composer-input" @click="focusComposer">跳到输入框</a>

    <!-- 话题栏：标题常驻，点击查看完整说明（完整版由 B 实现） -->
    <header class="topic-bar">
      <span class="topic-mark mono">TOPIC</span>
      <span class="topic-divider" aria-hidden="true"></span>
      <button class="topic-button" type="button" :title="topicTitle">
        <span class="topic-title">{{ topicTitle }}</span>
        <span v-if="anchorTitle" class="topic-anchor">{{ anchorTitle }}</span>
      </button>
    </header>

    <div v-if="onboarding.hintVisible" class="setup-hint" role="status">
      <span class="text">还没设置完 QIO —— 补上模型、称呼与目标，它才会真正记得你</span>
      <button class="link resume" type="button" @click="onboarding.reopen()">继续设置</button>
      <button class="close" type="button" aria-label="关闭提示" @click="onboarding.setHintDismissed(true)">×</button>
    </div>

    <div class="interrupted-slot">
      <InterruptedTurnEntry />
      <RecoveryInbox />
    </div>

    <div v-if="session.history.status === 'error'" class="notice quiet" role="status">
      <span class="text">历史记录暂时无法读取</span>
      <button class="link" type="button" @click="retryHistory">重试</button>
    </div>
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
    <div v-else-if="events.credentialNotice" class="notice warn" role="status">
      <span class="kind mono qio-state warn">提示</span>
      <span class="text">{{ events.credentialNotice }}</span>
      <router-link to="/settings" class="link">前往设置</router-link>
    </div>

    <MessageStream class="message-stage" />

    <div class="bottom-cluster">
      <div v-if="session.knowledgeCandidates.length" class="candidates">
        <KnowledgeCandidateCard v-for="c in session.knowledgeCandidates" :key="c.knowledgeId" :candidate="c" />
      </div>
      <div v-if="events.fallbackNotice" class="notice quiet fallback" role="status">
        <span class="kind mono qio-state quiet">兼容模式</span>
        <span class="text">{{ events.fallbackNotice }}</span>
        <button class="link" type="button" @click="events.fallbackNotice = null">知道了</button>
      </div>
      <TopicSwitchPrompt
        v-if="session.pendingSwitch"
        :topic-name="session.pendingSwitch.topicName"
        :busy="session.pendingSwitchBusy"
        @confirm="session.confirmPendingSwitch()"
        @keep="session.rejectPendingSwitch()"
      />
    </div>

    <ComposerDock />
  </section>
</template>

<style scoped>
.chat-column {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
  position: relative;
}
.topic-bar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  margin: 8px 12px 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
}
.topic-mark {
  font-size: 9px;
  letter-spacing: 0.1em;
  color: var(--text-faint);
}
.topic-divider {
  width: 1px;
  height: 14px;
  background: var(--border-subtle);
}
.topic-button {
  border: 0;
  background: transparent;
  padding: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
  text-align: left;
  cursor: pointer;
  color: inherit;
}
.topic-title {
  font-family: var(--serif);
  font-size: 15px;
  color: var(--text-strong);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.topic-anchor {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  white-space: nowrap;
}
.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 5;
}
.skip-link:focus {
  left: 12px;
  top: 8px;
  padding: 6px 10px;
  background: var(--bg-elevated);
  border: 1px solid var(--accent);
  border-radius: var(--r-sm);
  color: var(--text-strong);
}
.setup-hint {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  margin: 6px 12px 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--accent-soft);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.setup-hint .text { flex: 1; }
.setup-hint .close {
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
.interrupted-slot { flex: none; }
.notice {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 6px 12px 0;
  padding: 7px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-surface);
  font-size: var(--fs-sm);
}
.notice.err { border-color: var(--border-danger); color: var(--danger); }
.notice.warn { border-color: var(--warning); color: var(--warning); }
.notice button.link {
  background: none;
  border: 0;
  padding: 0;
  font: inherit;
  color: var(--link);
  cursor: pointer;
}
.notice .text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notice .kind {
  font-size: 10.5px;
  letter-spacing: 0.08em;
  padding: 1px 8px;
  border-radius: var(--r-pill);
  border: 1px solid currentColor;
  flex-shrink: 0;
}
.message-stage {
  flex: 1;
  min-height: 0;
  display: flex;
}
.bottom-cluster {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 0 12px;
  max-height: 42vh;
  overflow-y: auto;
}
.candidates { display: flex; flex-direction: column; }
</style>
