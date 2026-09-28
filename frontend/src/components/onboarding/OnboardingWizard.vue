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
import { api, type CredentialMeta, type OnboardingSubmitPayload, type VerifyReport } from "../../services/api";
import { getTheme, setTheme, type Theme } from "../../utils/theme";
import CredentialForm from "../credentials/CredentialForm.vue";
import QInput from "../ui/QInput.vue";
import { usageSummary } from "../../services/credentials";
import { GOAL_EXAMPLES, ONBOARDING_STEPS, PREFERENCE_DIMENSIONS, type StepKey } from "./steps";

const emit = defineEmits<{ done: [] }>();
const store = useOnboardingStore();

const index = ref(0);
const step = computed<StepKey>(() => ONBOARDING_STEPS[index.value].key);
const isFirst = computed(() => index.value === 0);
const isLast = computed(() => index.value === ONBOARDING_STEPS.length - 1);
/**
 * 老用户（主页已经有内容）才允许关掉整场引导。
 *
 * 另外：曾经完成过设置的人再次运行助手，也随时可以关 —— 这份引导对他是"回看/修改"，
 * 不是"必须先过的新手门槛"。
 */
const closable = computed(
  () => Boolean(store.status?.has_content) || Boolean(store.status?.done),
);
/**
 * 「连接模型」这一步和设置页共用同一个表单（默认厂商、默认用途、保存与验证逻辑全一致）。
 * 这里只记最后一次结果：验证通过才算准备好了。
 */
const credentialReport = ref<VerifyReport | null>(null);
const credentialSaved = ref<CredentialMeta | null>(null);
/** 共用表单的句柄：这一步的主按钮由引导页给，点了就提交它 */
const credentialForm = ref<{ submit: () => Promise<void>; busy: boolean; touched: boolean } | null>(
  null,
);
const credentialReady = computed(
  () => credentialReport.value?.ok === true || Boolean(store.status?.has_credential),
);
const canLeaveCredential = computed(() => credentialReady.value || Boolean(store.status?.has_content));
/**
 * 「连接模型」这一步只有一个主按钮。
 *
 * 以前是表单自己的「保存」+ 底部「下一步」两个按钮并排：用户得先保存、再前进，
 * 而且两步之间的关系看不出来。现在合并成一个 —— 需要保存时它叫「保存」（保存
 * 通过就自动进入下一步），已经有可用凭据又没在填新的时候它就叫「下一步」。
 */
const credentialNeedsSave = computed(
  () => !credentialReady.value || Boolean(credentialForm.value?.touched),
);
const credentialPrimaryLabel = computed(() => {
  if (credentialForm.value?.busy) return "保存中…";
  return credentialNeedsSave.value ? "保存" : "下一步";
});
/**
 * 「跳过」只在它比主按钮多做一件事的时候出现。
 *
 * 已有可用凭据、用户也没在填新的 → 主按钮就是「下一步」，此时再放一个「跳过」
 * 是同义重复（两个按钮做的是同一件事）；只有「确实有一份还没保存的填写」时，
 * 「保存」与「这次不配了，直接往下走」才是两件不同的事。
 */
const showCredentialSkip = computed(() => closable.value && credentialNeedsSave.value);
const credentialHint = computed(() => {
  if (credentialReady.value && !credentialNeedsSave.value) {
    return "已有一把通过验证的密钥，点「下一步」继续就行；下面也可以再添加一把。";
  }
  if (credentialReady.value) {
    return "下面可以直接再添加一把密钥；不想现在配就点「跳过」。";
  }
  if (closable.value) {
    return "选厂商、填 API Key，然后点「保存」：QIO 会把它配成主对话用途，并真的调用一次来确认模型可用。不想现在配就点「跳过」，之后可以在设置里补。";
  }
  return "选厂商、填 API Key，然后点「保存」：QIO 会把它配成主对话用途，并真的调用一次来确认模型可用。没有可用密钥时 QIO 无法回答任何问题，所以这一步不能跳过。";
});

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
  /** 每个维度勾了「其他」之后，用户自己写的内容 */
  customPreferences: {} as Record<string, string>,
  /** 该维度当前是否用的是「其他」 */
  useCustom: {} as Record<string, boolean>,
  goals: [] as string[],
  answers: {} as Record<string, string>,
});

const goalInput = ref("");
const nameError = ref("");
const showOptional = ref(false);

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

/** 一个维度的最终取值：选了「其他」就用自己写的，否则用选项。 */
function preferenceValue(key: string): string {
  return draft.useCustom[key] ? (draft.customPreferences[key] ?? "").trim() : (draft.preferences[key] ?? "");
}

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
    const value = preferenceValue(dim.key);
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
  // 兜底：密钥这一步没有可用凭据时不前进（正常入口是主按钮 → 表单校验，
  // 错误显示在表单里；这里只是防止将来别处直接调用 next() 绕过它）。
  if (step.value === "credential" && !canLeaveCredential.value) return;
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

function onCredentialSaved(payload: {
  credential: CredentialMeta | null;
  report: VerifyReport | null;
}) {
  credentialReport.value = payload.report;
  credentialSaved.value = payload.credential ?? credentialSaved.value;
  // 引导页要在本机也立刻反映「已经有一把可用凭据」，否则同一份状态会被判定成没配。
  if (payload.report?.ok) {
    void store.load();
    // 合并后的主按钮：保存通过就继续下一步（没通过则留在原地看原因并重试）
    goNext();
  }
}

/** 「连接模型」的主按钮：该保存就保存（保存通过后自动前进），否则直接前进。 */
async function onCredentialPrimary() {
  if (!credentialNeedsSave.value) {
    await next();
    return;
  }
  await credentialForm.value?.submit();
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

/** 选预设选项：退出「其他」态。 */
function choosePreference(key: string, option: string) {
  draft.useCustom[key] = false;
  draft.preferences[key] = option;
}

/** 选「其他」：改成自己写。 */
function chooseCustomPreference(key: string) {
  draft.useCustom[key] = true;
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
  if (key.startsWith("pref:")) {
    // 在核对清单里改偏好 = 改成自己写的值（相当于自动切到「其他」）
    const dim = key.slice(5);
    draft.customPreferences[dim] = value;
    draft.useCustom[dim] = true;
  }
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
    (dim) => preferenceValue(dim.key) && !removed[`pref:${dim.key}`],
  ).map((dim) => ({
    kind: dim.key,
    value: preferenceValue(dim.key),
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
          <p class="hint">{{ credentialHint }}</p>
          <CredentialForm
            ref="credentialForm"
            mode="create"
            variant="onboarding"
            :show-actions="false"
            @saved="onCredentialSaved"
          />
          <p v-if="credentialSaved && credentialReport?.ok" class="note ok">
            已保存，模型可用；用途：{{ usageSummary(credentialSaved.tags) }}。
          </p>
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
                :class="{ on: !draft.useCustom[dim.key] && draft.preferences[dim.key] === option }"
                @click="choosePreference(dim.key, option)"
              >
                {{ option }}
              </button>
              <button
                type="button"
                class="chip other"
                :class="{ on: draft.useCustom[dim.key] }"
                @click="chooseCustomPreference(dim.key)"
              >
                其他
              </button>
            </div>
            <QInput
              v-if="draft.useCustom[dim.key]"
              v-model="draft.customPreferences[dim.key]"
              class="pref-custom"
              :placeholder="`自己写一个${dim.label}的要求…`"
            />
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
          v-if="step === 'credential' && showCredentialSkip"
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
        <button
          v-if="step === 'credential'"
          class="qio-btn primary credential-primary"
          type="button"
          :disabled="Boolean(credentialForm?.busy)"
          :aria-busy="credentialForm?.busy ? 'true' : undefined"
          @click="onCredentialPrimary"
        >
          {{ credentialPrimaryLabel }}
        </button>
        <button v-else-if="!isLast" class="qio-btn primary" type="button" @click="next">
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
/* 「其他」自己写的内容是句子长度：独占一整行 */
.pref-row > input.qio-input { grid-column: 1 / -1; width: 100%; }
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
