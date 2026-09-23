<script setup lang="ts">
/**
 * 首次引导（欢迎页）：全屏六步向导（spec 2026-08-18-onboarding-design §2–§5）。
 *
 * 两条硬性行为：
 * - 打开即 `markSeen()` —— 后端记下「本版本已展示过欢迎页」，所以每个版本只强制展开一次；
 * - 「认识你」的称呼是完成判定的必填项，没填就停在原地并提示。
 */
import { computed, onMounted, ref } from "vue";
import { useOnboardingStore } from "../../stores/onboarding";
import { api } from "../../services/api";
import { getTheme, setTheme, type Theme } from "../../utils/theme";
import QInput from "../ui/QInput.vue";
import {
  GOAL_OPTIONS,
  ONBOARDING_STEPS,
  STYLE_OPTIONS,
  TAG_SUGGESTIONS,
  type StepKey,
} from "./steps";

const emit = defineEmits<{ done: [] }>();
const store = useOnboardingStore();

const index = ref(0);
const step = computed<StepKey>(() => ONBOARDING_STEPS[index.value].key);
const isLast = computed(() => index.value === ONBOARDING_STEPS.length - 1);
const isFirst = computed(() => index.value === 0);

/** 连接模型 */
const apiKey = ref("");
const credentialState = ref<"idle" | "saving" | "ok" | "err">("idle");
const credentialNote = ref("");

/** 认识你 */
const name = ref("");
const intro = ref("");
const tagValues = ref<Record<string, string>>(
  Object.fromEntries(TAG_SUGGESTIONS.map((key) => [key, ""])),
);
const style = ref(STYLE_OPTIONS[0].value);
const nameError = ref("");

/** 偏好 */
const theme = ref<Theme>(getTheme());

/** 目标 */
const goals = ref<string[]>([]);

const filledTags = computed(() =>
  TAG_SUGGESTIONS.map((key) => ({ key, value: (tagValues.value[key] ?? "").trim() })).filter(
    (tag) => tag.value,
  ),
);

onMounted(() => {
  void store.markSeen();
});

function goNext() {
  index.value = Math.min(index.value + 1, ONBOARDING_STEPS.length - 1);
}

function back() {
  nameError.value = "";
  index.value = Math.max(0, index.value - 1);
}

function skip() {
  nameError.value = "";
  goNext();
}

async function next() {
  if (step.value === "profile") {
    if (!name.value.trim()) {
      nameError.value = "称呼是完成设置的必填项";
      return;
    }
    nameError.value = "";
    try {
      await store.saveProfile({
        name: name.value.trim(),
        intro: intro.value.trim(),
        tags: filledTags.value,
        style: style.value,
        goals: goals.value,
      });
    } catch {
      /* 落库失败不挡路：提示由 store.error 承载，用户可在设置页重跑 */
    }
  }
  goNext();
}

async function saveCredential() {
  const secret = apiKey.value.trim();
  if (!secret) {
    credentialState.value = "err";
    credentialNote.value = "请先填入 API Key";
    return;
  }
  credentialState.value = "saving";
  credentialNote.value = "";
  try {
    const created = await api.createCredential({ secret });
    await api.testCredential(created.key_id);
    credentialState.value = "ok";
    credentialNote.value = "已连接，模型可用";
  } catch (error) {
    credentialState.value = "err";
    credentialNote.value = error instanceof Error ? error.message : "连接失败，可稍后在设置页重试";
  }
}

function chooseTheme(nextTheme: Theme) {
  theme.value = nextTheme;
  setTheme(nextTheme);
}

function toggleGoal(goal: string) {
  const at = goals.value.indexOf(goal);
  if (at >= 0) goals.value.splice(at, 1);
  else goals.value.push(goal);
}

async function finish() {
  try {
    await store.complete();
  } catch {
    /* 标记完成失败时不挡路：下一轮仍会给出「继续设置」提示 */
  }
  emit("done");
}
</script>

<template>
  <div class="onboarding" role="dialog" aria-modal="true" aria-label="首次引导">
    <div class="onboarding-card">
      <button
        class="onboarding-close"
        type="button"
        aria-label="关闭引导"
        @click="emit('done')"
      >
        ×
      </button>
      <nav class="onboarding-steps" aria-label="设置步骤">
        <span
          v-for="(item, i) in ONBOARDING_STEPS"
          :key="item.key"
          class="step"
          :class="{ current: i === index, done: i < index }"
          :aria-current="i === index ? 'step' : undefined"
        >
          {{ item.title }}
        </span>
      </nav>

      <div class="onboarding-body">
        <section v-if="step === 'welcome'" class="panel welcome">
          <div class="brand" aria-hidden="true">◍</div>
          <h1>QIO</h1>
          <p class="lede">你的私人记忆星球</p>
          <p class="hint">
            接下来几步会帮你连上模型、认识你，并把话题星球搭起来。每一步都可以跳过，之后在设置里随时补。
          </p>
        </section>

        <section v-else-if="step === 'credential'" class="panel">
          <h2>连接模型</h2>
          <p class="hint">填入 API Key，QIO 会自动识别提供方并做一次连通测试。没有也可以先跳过。</p>
          <QInput v-model="apiKey" type="password" placeholder="粘贴 API Key…" />
          <button class="qio-btn mini" type="button" :disabled="credentialState === 'saving'" @click="saveCredential">
            {{ credentialState === "saving" ? "测试中…" : "保存并测试" }}
          </button>
          <p v-if="credentialNote" class="note" :class="credentialState === 'ok' ? 'ok' : 'err'">
            {{ credentialNote }}
          </p>
        </section>

        <section v-else-if="step === 'profile'" class="panel">
          <h2>认识你</h2>
          <p class="hint">怎么称呼你？这些会写进 QIO 的记忆（画像知识 + 「我」实体卡），首日就有记忆底座。</p>
          <label class="field">
            <span>称呼</span>
            <QInput v-model="name" placeholder="名字或昵称…" :error="Boolean(nameError)" />
          </label>
          <p v-if="nameError" class="note err">{{ nameError }}</p>
          <label class="field">
            <span>一句话介绍（可选）</span>
            <QInput v-model="intro" placeholder="你在做什么、关心什么…" />
          </label>
          <div class="tags">
            <label v-for="key in TAG_SUGGESTIONS" :key="key" class="tag-field">
              <span>{{ key }}</span>
              <QInput v-model="tagValues[key]" :placeholder="`${key}…`" />
            </label>
          </div>
          <div class="style-row">
            <span>回答风格</span>
            <button
              v-for="option in STYLE_OPTIONS"
              :key="option.value"
              type="button"
              class="style-chip"
              :class="{ on: style === option.value }"
              @click="style = option.value"
            >
              {{ option.label }}
            </button>
          </div>
        </section>

        <section v-else-if="step === 'preference'" class="panel">
          <h2>偏好</h2>
          <p class="hint">先定主题，之后也可以在设置里改。</p>
          <div class="theme-row">
            <button
              type="button"
              class="style-chip"
              :class="{ on: theme === 'light' }"
              @click="chooseTheme('light')"
            >
              亮色
            </button>
            <button
              type="button"
              class="style-chip"
              :class="{ on: theme === 'dark' }"
              @click="chooseTheme('dark')"
            >
              暗色
            </button>
          </div>
        </section>

        <section v-else-if="step === 'goal'" class="panel">
          <h2>目标</h2>
          <p class="hint">你最想让它帮你做什么？勾选的每一项都会生成一个种子话题。</p>
          <div class="goals">
            <button
              v-for="goal in GOAL_OPTIONS"
              :key="goal"
              type="button"
              class="goal"
              :class="{ on: goals.includes(goal) }"
              @click="toggleGoal(goal)"
            >
              {{ goal }}
            </button>
          </div>
        </section>

        <section v-else class="panel">
          <h2>完成</h2>
          <ul class="summary">
            <li>称呼：{{ name || "（未填）" }}</li>
            <li>目标：{{ goals.length ? goals.join("、") : "（未选）" }}</li>
            <li>主题：{{ theme === "dark" ? "暗色" : "亮色" }}</li>
          </ul>
          <p class="hint">这些已经写进 QIO 的记忆，星球上就能看到你的话题与「我」实体卡。</p>
        </section>
      </div>

      <footer class="onboarding-actions">
        <button v-if="!isFirst && !isLast" class="qio-btn quiet skip" type="button" @click="skip">
          跳过
        </button>
        <button v-if="!isFirst" class="qio-btn quiet back" type="button" @click="back">上一步</button>
        <button
          v-if="!isLast"
          class="qio-btn primary"
          type="button"
          @click="next"
        >
          {{ isFirst ? "开始设置" : "下一步" }}
        </button>
        <button v-else class="qio-btn primary" type="button" @click="finish">进入对话</button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.onboarding {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  background: var(--bg-overlay);
  backdrop-filter: blur(6px);
}
.onboarding-card {
  position: relative;
  display: flex;
  flex-direction: column;
  width: min(760px, 100%);
  max-height: 100%;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: 20px;
  background: var(--bg-panel);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
}
.onboarding-close {
  position: absolute;
  top: 12px;
  right: 14px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}
.onboarding-close:hover { color: var(--text-strong); }
.onboarding-steps {
  display: flex;
  gap: 14px;
  padding: 18px 26px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 12px;
  color: var(--text-faint);
}
.onboarding-steps .step {
  padding-bottom: 4px;
  border-bottom: 2px solid transparent;
}
.onboarding-steps .step.current {
  border-bottom-color: var(--accent);
  color: var(--accent);
}
.onboarding-steps .step.done {
  color: var(--text-secondary);
}
.onboarding-body {
  flex: 1;
  overflow-y: auto;
  padding: 26px;
}
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.panel h1,
.panel h2 {
  margin: 0;
  color: var(--text-strong);
  font-size: 20px;
  font-weight: 600;
}
.panel .lede {
  margin: 0;
  color: var(--text-primary);
  font-size: 15px;
}
.panel .hint {
  margin: 0;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.7;
}
.welcome .brand {
  font-size: 30px;
  color: var(--accent);
}
.field,
.tag-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.tags {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
}
.style-row,
.theme-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.style-chip,
.goal {
  padding: 6px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
}
.style-chip.on,
.goal.on {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--text-strong);
}
.goals {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.summary {
  margin: 0;
  padding-left: 18px;
  color: var(--text-primary);
  font-size: 13px;
  line-height: 1.9;
}
.note {
  margin: 0;
  font-size: 12px;
}
.note.ok {
  color: var(--success, #5fbf8f);
}
.note.err {
  color: var(--danger, #e06a6a);
}
.onboarding-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 26px;
  border-top: 1px solid var(--border-subtle);
}
</style>
