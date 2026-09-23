<script setup lang="ts">
/**
 * 首次引导 v2：七步向导（欢迎 / 连接模型 / 认识你 / 偏好 / 目标 / 追问 / 核对并完成）。
 *
 * 三条硬性规则（来自产品决策）：
 * - **核对之前什么都不写**：所有输入只存在这里，走完最后一步确认后才一次性提交；
 * - **中途关闭 = 当没填过**：关掉再打开是空白，草稿不落任何地方；
 * - **新用户不能在密钥这一步跳过**（老用户主页已有内容，可以整场关闭）。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { useOnboardingStore } from "../../stores/onboarding";
import { api, type OnboardingSubmitPayload } from "../../services/api";
import { identifyCredential } from "../../services/identify";
import { getTheme, setTheme, type Theme } from "../../utils/theme";
import QInput from "../ui/QInput.vue";
import { GOAL_EXAMPLES, ONBOARDING_STEPS, PREFERENCE_DIMENSIONS, type StepKey } from "./steps";

const emit = defineEmits<{ done: [] }>();
const store = useOnboardingStore();

const index = ref(0);
const step = computed<StepKey>(() => ONBOARDING_STEPS[index.value].key);
const isFirst = computed(() => index.value === 0);
const isLast = computed(() => index.value === ONBOARDING_STEPS.length - 1);
/** 老用户（主页已经有内容）才允许关掉整场引导 */
const closable = computed(() => Boolean(store.status?.has_content));
/** 已经配过密钥的老用户不必再填一次 */
const credentialReady = computed(
  () => credentialState.value === "ok" || Boolean(store.status?.has_credential),
);
const canLeaveCredential = computed(() => credentialReady.value || Boolean(store.status?.has_content));

const draft = reactive({
  name: "",
  background: "",
  currentFocus: "",
  currentFocusEnded: false,
  interests: "",
  familiarity: "",
  dontDo: "",
  howToTalk: "",
  preferences: {} as Record<string, string>,
  goals: [] as string[],
  answers: {} as Record<string, string>,
});

const goalInput = ref("");
const nameError = ref("");
const showOptional = ref(false);

/** 连接模型 */
const apiKey = ref("");
const credentialState = ref<"idle" | "saving" | "ok" | "err">("idle");
const credentialNote = ref("");
const identifiedProvider = ref("");
const identifiedEndpoint = ref("");
const identifiedModel = ref("");

/** 偏好 */
const theme = ref<Theme>(getTheme());

/** 追问 */
const questions = ref<string[]>([]);
const followUpState = ref<"idle" | "loading" | "ready">("idle");

/** 核对清单 */
const removed = reactive<Record<string, boolean>>({});
const editing = ref<string | null>(null);
const editBuffer = ref("");

interface ReviewItem {
  key: string;
  label: string;
  value: string;
}

const description = computed(() =>
  [draft.background, draft.currentFocus, draft.interests, Object.values(draft.preferences).join("；")]
    .map((part) => part.trim())
    .filter(Boolean)
    .join("\n"),
);

const reviewItems = computed<ReviewItem[]>(() => {
  const items: ReviewItem[] = [];
  const push = (key: string, label: string, value: string) => {
    if (value.trim() && !removed[key]) items.push({ key, label, value: value.trim() });
  };
  push("name", "称呼", draft.name);
  push("background", "学习 / 工作背景", draft.background);
  push("currentFocus", draft.currentFocusEnded ? "最近在做（已结束）" : "最近在做", draft.currentFocus);
  push("interests", "长期关注", draft.interests);
  push("familiarity", "熟悉程度", draft.familiarity);
  push("dontDo", "不要做什么", draft.dontDo);
  push("howToTalk", "希望怎么表达", draft.howToTalk);
  for (const dim of PREFERENCE_DIMENSIONS) {
    const value = draft.preferences[dim.key] ?? "";
    push(`pref:${dim.key}`, `偏好 · ${dim.label}`, value);
  }
  draft.goals.forEach((goal, i) => push(`goal:${i}`, `目标（会新建话题）`, goal));
  questions.value.forEach((question, i) =>
    push(`answer:${i}`, `追问：${question}`, draft.answers[question] ?? ""),
  );
  return items;
});

onMounted(() => {
  void store.markSeen();
});

function goNext() {
  index.value = Math.min(index.value + 1, ONBOARDING_STEPS.length - 1);
}

async function next() {
  if (step.value === "profile") {
    if (!draft.name.trim()) {
      nameError.value = "称呼是完成设置的必填项";
      return;
    }
    nameError.value = "";
  }
  if (step.value === "credential" && !canLeaveCredential.value) {
    credentialNote.value = "没有可用的密钥就无法继续：QIO 需要它才能回答、记忆和提炼。";
    return;
  }
  if (step.value === "goal" || step.value === "preference") {
    // 追问基于用户自己写下的描述：进入追问步骤时现问一次
    goNext();
    const reached = step.value as StepKey;
    if (reached === "followup") await loadQuestions();
    return;
  }
  goNext();
}

function back() {
  nameError.value = "";
  index.value = Math.max(0, index.value - 1);
}

function skip() {
  nameError.value = "";
  goNext();
  if (step.value === "followup") void loadQuestions();
}

async function loadQuestions() {
  if (followUpState.value === "loading") return;
  const text = description.value;
  if (!text) {
    questions.value = [];
    followUpState.value = "ready";
    return;
  }
  followUpState.value = "loading";
  try {
    const result = await api.suggestFollowUps(text);
    questions.value = result.questions ?? [];
  } catch {
    questions.value = [];
  } finally {
    followUpState.value = "ready";
  }
}

async function saveCredential() {
  const secret = apiKey.value.trim();
  if (!secret) {
    credentialState.value = "err";
    credentialNote.value = "请先填入 API Key";
    return;
  }
  if (!looksLikeKey(secret)) {
    credentialState.value = "err";
    credentialNote.value = "这看起来不是 API Key（像链接 / 路径 / 报错文本）：请粘贴完整的 Key 本身";
    return;
  }
  credentialState.value = "saving";
  credentialNote.value = "";
  const identified = await identifyKey(secret);
  try {
    const payload: Record<string, unknown> = { secret };
    if (identified && identifiedEndpoint.value) {
      payload.endpoint = identifiedEndpoint.value;
      if (identifiedModel.value) payload.default_model = identifiedModel.value;
    }
    const created = await api.createCredential(payload);
    await api.testCredential(created.key_id);
    credentialState.value = "ok";
    credentialNote.value = identifiedProvider.value
      ? `已连接 ${identifiedProvider.value}，模型可用`
      : "已连接，模型可用";
  } catch (error) {
    credentialState.value = "err";
    credentialNote.value = error instanceof Error ? error.message : "连接失败，可以稍后在设置页重试";
  }
}

async function identifyKey(secret: string): Promise<boolean> {
  try {
    const result = await identifyCredential(secret);
    if (!result.identified || !result.base_url) return false;
    identifiedProvider.value = result.provider ?? "";
    identifiedEndpoint.value = result.base_url;
    identifiedModel.value = result.default_model ?? "";
    return true;
  } catch {
    return false;
  }
}

function looksLikeKey(secret: string): boolean {
  if (secret.length < 20 || secret.length > 200) return false;
  if (/\s/.test(secret)) return false;
  if (secret.includes("://") || secret.includes("->")) return false;
  return !secret.startsWith("/") && !secret.startsWith("http");
}

function chooseTheme(nextTheme: Theme) {
  theme.value = nextTheme;
  setTheme(nextTheme);
}

function addGoal() {
  const value = goalInput.value.trim();
  if (!value) return;
  if (!draft.goals.includes(value)) draft.goals.push(value);
  goalInput.value = "";
}

function removeGoal(at: number) {
  draft.goals.splice(at, 1);
}

function startEdit(item: ReviewItem) {
  editing.value = item.key;
  editBuffer.value = item.value;
}

function applyEdit() {
  const key = editing.value;
  if (!key) return;
  const value = editBuffer.value.trim();
  if (key.startsWith("pref:")) draft.preferences[key.slice(5)] = value;
  else if (key.startsWith("goal:")) draft.goals[Number(key.slice(5))] = value;
  else if (key.startsWith("answer:")) {
    const question = questions.value[Number(key.slice(7))];
    if (question) draft.answers[question] = value;
  } else if (key === "name") draft.name = value;
  else if (key === "background") draft.background = value;
  else if (key === "currentFocus") draft.currentFocus = value;
  else if (key === "interests") draft.interests = value;
  else if (key === "familiarity") draft.familiarity = value;
  else if (key === "dontDo") draft.dontDo = value;
  else if (key === "howToTalk") draft.howToTalk = value;
  editing.value = null;
}

function buildPayload(): OnboardingSubmitPayload {
  const preferences = PREFERENCE_DIMENSIONS.filter(
    (dim) => (draft.preferences[dim.key] ?? "").trim() && !removed[`pref:${dim.key}`],
  ).map((dim) => ({
    kind: dim.key,
    value: draft.preferences[dim.key].trim(),
    // 引导只收集"你希望怎么被对待"；"只在某个话题里生效"属于星球·知识页的管理
    scope: { type: "global" as const },
  }));
  const payload: OnboardingSubmitPayload = {
    name: draft.name.trim(),
    preferences,
    goals: draft.goals.filter((goal, i) => goal.trim() && !removed[`goal:${i}`]),
  };
  if (!removed.background && draft.background.trim()) payload.background = draft.background.trim();
  if (!removed.currentFocus && draft.currentFocus.trim()) {
    payload.current_focus = draft.currentFocus.trim();
    payload.current_focus_ended = draft.currentFocusEnded;
  }
  const interests = draft.interests
    .split(/[、,，\s]+/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (!removed.interests && interests.length) payload.interests = interests;
  if (!removed.familiarity && draft.familiarity.trim()) payload.familiarity = draft.familiarity.trim();
  const limits: { dont_do?: string; how_to_talk?: string } = {};
  if (!removed.dontDo && draft.dontDo.trim()) limits.dont_do = draft.dontDo.trim();
  if (!removed.howToTalk && draft.howToTalk.trim()) limits.how_to_talk = draft.howToTalk.trim();
  if (Object.keys(limits).length) payload.limits = limits;
  return payload;
}

async function finish() {
  try {
    await store.submit(buildPayload());
  } catch {
    /* 写不进去时留在清单页：store.error 会给出原因，用户可以重试 */
    return;
  }
  emit("done");
}
</script>

<template>
  <div class="onboarding" role="dialog" aria-modal="true" aria-label="首次引导">
    <div class="onboarding-card">
      <button
        v-if="closable"
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
            接下来几步会让 QIO 真正认识你。全部内容只在你最后核对确认后才写进去；中途关掉就等于没填过。
          </p>
        </section>

        <section v-else-if="step === 'credential'" class="panel">
          <h2>连接模型</h2>
          <p class="hint">
            填入 API Key，QIO 会自动识别提供方并做一次连通测试。
            <template v-if="!closable">没有可用密钥时 QIO 无法回答任何问题，所以这一步不能跳过。</template>
          </p>
          <QInput v-model="apiKey" type="password" placeholder="粘贴 API Key…" />
          <button class="qio-btn mini" type="button" :disabled="credentialState === 'saving'" @click="saveCredential">
            {{ credentialState === "saving" ? "测试中…" : "保存并测试" }}
          </button>
          <p v-if="credentialNote" class="note" :class="credentialState === 'ok' ? 'ok' : 'err'">
            {{ credentialNote }}
          </p>
          <p v-if="identifiedProvider" class="note ok">已识别：{{ identifiedProvider }} · {{ identifiedEndpoint }}</p>
        </section>

        <section v-else-if="step === 'profile'" class="panel">
          <h2>认识你</h2>
          <label class="field">
            <span>称呼</span>
            <QInput v-model="draft.name" placeholder="名字或昵称…" :error="Boolean(nameError)" />
          </label>
          <p v-if="nameError" class="note err">{{ nameError }}</p>
          <label class="field">
            <span>最近主要在做什么</span>
            <QInput v-model="draft.currentFocus" placeholder="例如：开发 QIO。可以留空" />
          </label>
          <label class="check">
            <input v-model="draft.currentFocusEnded" type="checkbox" />
            <span>这件事已经结束了（结束不等于删掉，只是不再当作当前状态）</span>
          </label>
          <label class="field">
            <span>学习或工作背景</span>
            <QInput v-model="draft.background" placeholder="例如：学生。可以留空" />
          </label>
          <button class="qio-btn quiet optional-toggle" type="button" @click="showOptional = !showOptional">
            {{ showOptional ? "收起可选项" : "展开可选项（都可以跳过）" }}
          </button>
          <template v-if="showOptional">
            <label class="field">
              <span>长期关注</span>
              <QInput v-model="draft.interests" placeholder="多个用顿号分隔…" />
            </label>
            <label class="field">
              <span>熟悉程度</span>
              <QInput v-model="draft.familiarity" placeholder="例如：刚入门" />
            </label>
            <label class="field">
              <span>不要做什么</span>
              <QInput v-model="draft.dontDo" placeholder="例如：不要自动动用外部工具" />
            </label>
            <label class="field">
              <span>希望怎么表达</span>
              <QInput v-model="draft.howToTalk" placeholder="例如：先讲逻辑再给代码" />
            </label>
          </template>
        </section>

        <section v-else-if="step === 'preference'" class="panel">
          <h2>偏好</h2>
          <p class="hint">
            这些是"你希望 QIO 怎么对待你"。不填就是不设这条偏好；
            如果某一条只想在某个话题里生效，之后可以在「星球 → 知识」里改它的适用范围。
          </p>
          <div v-for="dim in PREFERENCE_DIMENSIONS" :key="dim.key" class="pref-row">
            <span class="pref-label">{{ dim.label }}</span>
            <div class="chips">
              <button
                v-for="option in dim.options"
                :key="option"
                type="button"
                class="chip"
                :class="{ on: draft.preferences[dim.key] === option }"
                @click="draft.preferences[dim.key] = option"
              >
                {{ option }}
              </button>
            </div>
          </div>
          <div class="theme-row">
            <span>主题</span>
            <button type="button" class="chip" :class="{ on: theme === 'light' }" @click="chooseTheme('light')">亮色</button>
            <button type="button" class="chip" :class="{ on: theme === 'dark' }" @click="chooseTheme('dark')">暗色</button>
          </div>
        </section>

        <section v-else-if="step === 'goal'" class="panel">
          <h2>目标</h2>
          <p class="hint">
            写下你想推进的事（例如：{{ GOAL_EXAMPLES[0] }}）。每一条都会新建一个话题；一条都不写也可以。
          </p>
          <div class="goal-input">
            <QInput v-model="goalInput" placeholder="你想推进的事…" @keyup.enter="addGoal" />
            <button class="qio-btn mini add-goal" type="button" @click="addGoal">添加</button>
          </div>
          <ul class="goal-list">
            <li v-for="(goal, i) in draft.goals" :key="`${goal}-${i}`">
              <span>{{ goal }}</span>
              <button class="qio-btn mini quiet" type="button" @click="removeGoal(i)">删除</button>
            </li>
          </ul>
        </section>

        <section v-else-if="step === 'followup'" class="panel">
          <h2>追问</h2>
          <p class="hint">这些问题根据你前面写的内容现问，可以跳过；跳过的不会出现在清单里。</p>
          <p v-if="followUpState === 'loading'" class="note">正在根据你的描述准备问题…</p>
          <p v-else-if="!questions.length" class="note">这次没有需要追问的内容。</p>
          <label v-for="(question, i) in questions" :key="question" class="field">
            <span>{{ question }}</span>
            <QInput v-model="draft.answers[question]" placeholder="可以留空" />
          </label>
        </section>

        <section v-else class="panel">
          <h2>核对并完成</h2>
          <p class="hint">下面是将要写进 QIO 的内容。可以逐条修改或删除；删除的不会写进去。</p>
          <ul class="review-list">
            <li v-for="item in reviewItems" :key="item.key" class="review-item">
              <div class="review-text">
                <span class="review-label">{{ item.label }}</span>
                <QInput
                  v-if="editing === item.key"
                  v-model="editBuffer"
                  class="review-edit"
                />
                <span v-else class="review-value">{{ item.value }}</span>
              </div>
              <div class="review-actions">
                <button v-if="editing === item.key" class="qio-btn mini" type="button" @click="applyEdit">保存</button>
                <button v-else class="qio-btn mini quiet edit" type="button" @click="startEdit(item)">修改</button>
                <button class="qio-btn mini quiet remove" type="button" @click="removed[item.key] = true">删除</button>
              </div>
            </li>
          </ul>
          <p v-if="!reviewItems.length" class="note">目前没有要写入的内容。</p>
        </section>
      </div>

      <footer class="onboarding-actions">
        <button
          v-if="step === 'credential' && closable"
          class="qio-btn quiet skip"
          type="button"
          @click="skip"
        >
          跳过
        </button>
        <button v-if="step === 'followup'" class="qio-btn quiet skip-followup" type="button" @click="goNext">
          跳过
        </button>
        <button v-if="!isFirst" class="qio-btn quiet back" type="button" @click="back">上一步</button>
        <button v-if="!isLast" class="qio-btn primary" type="button" @click="next">
          {{ isFirst ? "开始设置" : "下一步" }}
        </button>
        <button v-else class="qio-btn primary finish" type="button" :disabled="store.saving" @click="finish">
          {{ store.saving ? "正在写入…" : "完成设置" }}
        </button>
      </footer>
      <p v-if="store.error" class="note err submit-error">{{ store.error }}</p>
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
  width: min(780px, 100%);
  max-height: 100%;
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
  cursor: pointer;
}
.onboarding-close:hover { color: var(--text-strong); }
.onboarding-steps {
  display: flex;
  gap: 12px;
  padding: 18px 26px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 12px;
  color: var(--text-faint);
}
.onboarding-steps .step { padding-bottom: 4px; border-bottom: 2px solid transparent; }
.onboarding-steps .step.current { border-bottom-color: var(--accent); color: var(--accent); }
.onboarding-body { flex: 1; overflow-y: auto; padding: 26px; }
.panel { display: flex; flex-direction: column; gap: 12px; }
.panel h1, .panel h2 { margin: 0; color: var(--text-strong); font-size: 20px; font-weight: 600; }
.panel .lede { margin: 0; color: var(--text-primary); font-size: 15px; }
.panel .hint { margin: 0; color: var(--text-muted); font-size: 13px; line-height: 1.7; }
.welcome .brand { font-size: 30px; color: var(--accent); }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--text-secondary); }
.check { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-muted); }
.pref-row { display: grid; grid-template-columns: 88px 1fr; gap: 8px; align-items: center; }
.pref-label { font-size: 12px; color: var(--text-secondary); }
.chips, .theme-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 12px; color: var(--text-secondary); }
.chip {
  padding: 5px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.chip.on { border-color: var(--accent); background: var(--accent-soft); color: var(--text-strong); }
.goal-input { display: flex; gap: 8px; }
.goal-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.goal-list li { display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: var(--text-primary); }
.review-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.review-item { display: flex; justify-content: space-between; gap: 12px; align-items: center; border-bottom: 1px solid var(--border-subtle); padding-bottom: 6px; }
.review-label { display: block; font-size: 11px; color: var(--text-faint); }
.review-value { font-size: 13px; color: var(--text-primary); }
.review-actions { display: flex; gap: 6px; }
.note { margin: 0; font-size: 12px; color: var(--text-muted); }
.note.ok { color: var(--success, #5fbf8f); }
.note.err { color: var(--danger, #e06a6a); }
.submit-error { padding: 0 26px 8px; }
.onboarding-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 16px 26px; border-top: 1px solid var(--border-subtle); }
</style>
