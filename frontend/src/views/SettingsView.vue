<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type CredentialMeta } from "../services/api";
import { identifyCredential } from "../services/identify";
import QInput from "../components/ui/QInput.vue";
import QNumber from "../components/ui/QNumber.vue";
import QSelect from "../components/ui/QSelect.vue";
import CredentialCard from "./settings/CredentialCard.vue";
import { getTheme, toggleTheme } from "../utils/theme";
import {
  floatingState,
  resetFloatPositions,
  setHideEnabled,
  type DockId,
} from "../composables/floatingState";

const credentials = ref<CredentialMeta[]>([]);
const form = ref({
  key_id: "",
  secret: "",
  tags: "", // 附加标签（逗号分隔）
  endpoint: "",
  default_model: "",
  budget: null as number | null,
  scope: "", // scope 授权（逗号分隔，可选）
});
const formCategory = ref("main-loop");
const notice = ref("");
const identifyState = ref("idle" as "idle" | "working" | "done" | "failed");
const identifiedProvider = ref("");
const modelOptions = ref<string[]>([]);
let identifyTimer: ReturnType<typeof setTimeout> | null = null;

watch(
  () => form.value.secret,
  (secret) => {
    if (identifyTimer) clearTimeout(identifyTimer);
    if (!secret.trim()) {
      identifyState.value = "idle";
      modelOptions.value = [];
      return;
    }
    identifyTimer = setTimeout(async () => {
      identifyState.value = "working";
      try {
        const result = await identifyCredential(secret.trim());
        if (result.identified) {
          identifiedProvider.value = result.provider ?? "";
          form.value.endpoint = result.base_url ?? form.value.endpoint;
          form.value.default_model = result.default_model ?? form.value.default_model;
          modelOptions.value = result.models ?? [];
          identifyState.value = "done";
        } else {
          identifyState.value = "failed";
          modelOptions.value = [];
        }
      } catch (e) {
        console.error("[identify] failed:", e);
        identifyState.value = "failed";
        modelOptions.value = [];
      }
    }, 800);
  },
);
// 只显示 active 凭据；撤销后卡片立即消失
const activeCredentials = computed(() => credentials.value.filter((c) => c.status === "active"));

const CATEGORY_PRESETS = ["chat", "code", "embed", "main-loop", "subagent", "vision", "research", "embedding"];
const categoryOptions = computed(() => {
  const set = new Set(CATEGORY_PRESETS);
  if (formCategory.value.trim()) set.add(formCategory.value.trim());
  return Array.from(set).map((v) => ({ value: v, label: v }));
});
const modelSelectOptions = computed(() => modelOptions.value.map((m) => ({ value: m, label: m })));
const identifyText = computed(() =>
  identifyState.value === "working"
    ? "识别中…"
    : identifyState.value === "done"
      ? `已识别：${identifiedProvider.value}`
      : identifyState.value === "failed"
        ? "未能自动识别，请手动填写"
        : "",
);
const submitLabel = computed(() => (editTarget.value ? "以此换钥（新建）" : "创建凭据"));

const activeTab = ref<"cred" | "pref" | "win">("cred");
/** 窗口管理：三个浮动组件的贴靠隐藏开关（读写共享 floatingState） */
const WINDOW_ITEMS: { id: DockId; title: string; desc: string }[] = [
  { id: "planet-dock", title: "话题星球入口", desc: "贴靠后淡化隐藏，悬停展开、移出再隐藏" },
  { id: "settings-float", title: "设置入口", desc: "贴角后淡化隐藏，悬停展开、移出再隐藏" },
];
const windowNotice = ref("");
function toggleWindowHide(id: DockId) {
  setHideEnabled(id, !floatingState[id].hideEnabled);
}
function resetWindowLayout() {
  resetFloatPositions();
  windowNotice.value = "已还原默认布局（贴靠隐藏关闭、位置回到默认）";
}
/** 主题：偏好页开关（暗紫晶/净白），读写 html[data-theme] + localStorage qio-theme */
const isLight = ref(getTheme() === "light");
function toggleThemePref() {
  isLight.value = toggleTheme() === "light";
}
const error = ref("");
const fragmentTier = ref("10");
const customCount = ref(10);
const settingsNotice = ref("");
const maintenanceEnabled = ref(true);
const maintenanceInterval = ref(24);
const formEl = ref<HTMLElement | null>(null);
const editTarget = ref<string | null>(null);
const tierPresets = ["5", "10", "15"];

async function loadMaintenanceSettings() {
  try {
    const s = await api.getMaintenanceSettings();
    maintenanceEnabled.value = s.enabled;
    maintenanceInterval.value = s.interval_hours;
  } catch (e) {
    console.error("[settings] load maintenance failed:", e);
  }
}

async function saveMaintenance() {
  try {
    const r = await api.updateMaintenanceSettings({
      enabled: maintenanceEnabled.value,
      interval_hours: maintenanceInterval.value,
    });
    maintenanceEnabled.value = r.enabled;
    maintenanceInterval.value = r.interval_hours;
    settingsNotice.value = `已保存：离线维护${r.enabled ? "开启" : "关闭"}（每 ${r.interval_hours} 小时）`;
  } catch (e) {
    settingsNotice.value = `保存失败：${(e as Error).message}`;
  }
}

function toggleMaintenance() {
  maintenanceEnabled.value = !maintenanceEnabled.value;
  void saveMaintenance();
}

async function runMaintenanceNow() {
  try {
    const r = await api.runMaintenance();
    settingsNotice.value = r.started ? "离线维护已启动（后台执行）" : "维护未启动";
  } catch (e) {
    settingsNotice.value = `启动失败：${(e as Error).message}`;
  }
}

const tierOptions = [
  { value: "5", label: "短（5 轮）" },
  { value: "10", label: "标准（10 轮）" },
  { value: "15", label: "长（15 轮）" },
  { value: "custom", label: "自定义" },
];

async function loadMemorySettings() {
  try {
    const s = await api.getMemorySettings();
    const saved = String(s.fragment_max_messages);
    if (tierPresets.includes(saved)) {
      fragmentTier.value = saved;
    } else {
      fragmentTier.value = "custom";
      customCount.value = s.fragment_max_messages;
    }
  } catch (e) {
    console.error("[settings] load memory settings failed:", e);
  }
}

async function saveMemorySettings() {
  const value =
    fragmentTier.value === "custom" ? customCount.value : Number(fragmentTier.value);
  if (!Number.isInteger(value) || value < 1 || value > 30) {
    settingsNotice.value = "自定义轮数需在 1-30 之间";
    return;
  }
  try {
    const r = await api.updateMemorySettings(value);
    settingsNotice.value = `已保存：${r.fragment_max_messages} 轮封块`;
    const saved = String(r.fragment_max_messages);
    if (tierPresets.includes(saved)) {
      fragmentTier.value = saved;
    } else {
      fragmentTier.value = "custom";
      customCount.value = r.fragment_max_messages;
    }
  } catch (e) {
    settingsNotice.value = `保存失败：${(e as Error).message}`;
  }
}

function onTierSelect(v: string) {
  fragmentTier.value = v;
  if (v !== "custom") {
    void saveMemorySettings();
  }
}

async function load() {
  try {
    credentials.value = (await api.listCredentials()).credentials;
  } catch (e) {
    error.value = (e as Error).message;
  }
}

function buildTags(): string[] {
  return Array.from(
    new Set(
      [formCategory.value, ...form.value.tags.split(",").map((t) => t.trim()).filter(Boolean)]
        .map((t) => t.trim())
        .filter(Boolean),
    ),
  );
}

function resetForm() {
  editTarget.value = null;
  form.value = {
    key_id: "",
    secret: "",
    tags: "",
    endpoint: "",
    default_model: "",
    budget: null,
    scope: "",
  };
  formCategory.value = "main-loop";
  modelOptions.value = [];
  identifyState.value = "idle";
  identifiedProvider.value = "";
}

async function create() {
  error.value = "";
  notice.value = "";
  if (!form.value.secret.trim()) {
    error.value = "请先粘贴 API Key";
    return;
  }
  try {
    const payload: Record<string, unknown> = {
      key_id: form.value.key_id.trim(),
      secret: form.value.secret,
      tags: buildTags(),
    };
    if (form.value.endpoint.trim()) payload.endpoint = form.value.endpoint.trim();
    if (form.value.default_model.trim()) payload.default_model = form.value.default_model.trim();
    if (form.value.budget && form.value.budget > 0) payload.budget = form.value.budget;
    if (form.value.scope.trim()) payload.scope = form.value.scope.split(",").map((s) => s.trim()).filter(Boolean);
    const editedKey = editTarget.value;
    await api.createCredential(payload);
    notice.value = editedKey
      ? `凭据已创建。请撤销旧凭据「${editedKey}」，避免双 key 并存（预算各计）。`
      : "凭据已创建";
    resetForm();
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  }
}

function edit(c: CredentialMeta) {
  activeTab.value = "cred";
  editTarget.value = c.key_id;
  form.value = {
    key_id: c.key_id,
    secret: "",
    tags: (c.tags ?? []).slice(1).join(", "),
    endpoint: c.endpoint ?? "",
    default_model: c.default_model ?? "",
    budget: c.budget,
    scope: (c.scope ?? []).join(", "),
  };
  formCategory.value = (c.tags ?? [])[0] ?? "main-loop";
  modelOptions.value = [];
  identifyState.value = "idle";
  identifiedProvider.value = "";
  notice.value = `编辑「${c.key_id}」：密钥只写不读，粘贴新密钥后「以此换钥（新建）」；保存后请撤销旧凭据，避免双 key 并存。`;
  formEl.value?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function revoke(keyId: string) {
  try {
    await api.revokeCredential(keyId);
    // revoked cards disappear from the list immediately
    credentials.value = credentials.value.filter((c) => c.key_id !== keyId);
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function test(keyId: string) {
  try {
    const result = await api.testCredential(keyId);
    showToast(`${keyId}: ${result.probe.mode}（${result.probe.detail.slice(0, 60)}）`, "ok");
  } catch (e) {
    showToast((e as Error).message, "err");
  }
}

/** 测试凭据结果：以浮现又消失的 toast 气泡展示 */
const toast = ref<{ text: string; kind: "ok" | "err" } | null>(null);
let toastTimer: ReturnType<typeof setTimeout> | null = null;
function showToast(text: string, kind: "ok" | "err") {
  if (toastTimer) clearTimeout(toastTimer);
  toast.value = { text, kind };
  toastTimer = setTimeout(() => {
    toast.value = null;
    toastTimer = null;
  }, 2600);
}

function onBudgetInput(v: number | null) {
  form.value.budget = v;
}
function onCustomCountInput(v: number | null) {
  customCount.value = v ?? 0;
}
function onIntervalInput(v: number | null) {
  maintenanceInterval.value = v ?? 0;
}

onMounted(() => {
  void load();
  void loadMemorySettings();
  void loadMaintenanceSettings();
});
</script>

<template>
  <div class="settings">
    <header>
      <h1>设置</h1>
      <span class="sub mono">settings · qio</span>
      <span class="spacer"></span>
      <router-link to="/" class="back">← 返回对话</router-link>
    </header>

    <div class="tabs" role="tablist">
      <button
        type="button"
        class="tab" :class="{ active: activeTab === 'cred' }" role="tab"
        :aria-selected="activeTab === 'cred'" @click="activeTab = 'cred'"
      >凭据</button>
      <button
        type="button"
        class="tab" :class="{ active: activeTab === 'pref' }" role="tab"
        :aria-selected="activeTab === 'pref'" @click="activeTab = 'pref'"
      >偏好</button>
      <button
        type="button"
        class="tab" :class="{ active: activeTab === 'win' }" role="tab"
        :aria-selected="activeTab === 'win'" @click="activeTab = 'win'"
      >窗口</button>
    </div>

    <!-- 凭据 -->
    <div v-show="activeTab === 'cred'" class="panel">
      <section class="sec">
        <h2>凭据</h2>
        <p class="desc">密钥只写不读：保存后不再显示明文，仅本地写入 keyring，服务器不落盘。</p>
        <p v-if="error" class="msg err">{{ error }}</p>
        <p v-if="notice" class="msg ok">{{ notice }}</p>

        <CredentialCard
          v-for="c in activeCredentials"
          :key="c.key_id"
          :credential="c"
          @test="test(c.key_id)"
          @edit="edit(c)"
          @remove="revoke(c.key_id)"
        />
        <p v-if="!activeCredentials.length" class="empty mono">尚无凭据。</p>
      </section>

      <section class="sec">
        <h2>{{ editTarget ? "编辑凭据（换钥）" : "新建凭据" }}</h2>
        <p class="desc">粘贴 API Key 后自动识别端点与模型；类别标签 + scope 授权控制工具访问。</p>
        <p v-if="editTarget" class="edit-hint mono">编辑态：保存将新建凭据（key_id 同名会提示已存在，可改名），旧凭据需手动撤销。</p>
        <div ref="formEl" class="form">
          <div class="field">
            <span class="label">名称</span>
            <QInput v-model="form.key_id" placeholder="留空自动生成" />
          </div>
          <div class="field">
            <span class="label">模型端点（base_url）</span>
            <QInput v-model="form.endpoint" mono placeholder="https://api.openai.com/v1" />
          </div>
          <div class="field">
            <span class="label">API Key（只写不读）</span>
            <QInput v-model="form.secret" type="password" mono placeholder="sk-…（粘贴后自动识别）" />
          </div>
          <div class="field">
            <span class="label">类别标签</span>
            <QSelect :options="categoryOptions" v-model="formCategory" />
          </div>
          <div class="field">
            <span class="label">附加标签</span>
            <QInput v-model="form.tags" placeholder="逗号分隔，可选（如 vision, research）" />
          </div>
          <div class="field">
            <span class="label">默认模型</span>
            <QSelect v-if="modelOptions.length" :options="modelSelectOptions" v-model="form.default_model" />
            <QInput v-else v-model="form.default_model" mono placeholder="自动识别或手动填写" />
          </div>
          <div class="field">
            <span class="label">预算 token</span>
            <QNumber
              :model-value="form.budget"
              :min="1" mono placeholder="可选" label="预算 token"
              @update:model-value="onBudgetInput"
            />
          </div>
          <div class="field span2">
            <span class="label">Scope 授权</span>
            <QInput v-model="form.scope" placeholder="default, tools, memory（逗号分隔，可选）" />
          </div>
          <p class="identify mono" :class="identifyState">{{ identifyText }}</p>
          <button type="button" class="qio-btn primary create" @click="create">{{ submitLabel }}</button>
        </div>
      </section>
    </div>

    <!-- 偏好 -->
    <div v-show="activeTab === 'pref'" class="panel">
      <section class="sec">
        <h2>主题</h2>
        <p class="desc">界面配色：暗色「暗紫晶」/ 亮色「净白」，即时生效。</p>
        <div class="pref">
          <div class="txt">
            <div class="t">主题</div>
            <div class="d">{{ isLight ? "亮色（净白）" : "暗色（暗紫晶）" }}</div>
          </div>
          <div class="ctl">
            <button
              type="button" class="qio-switch theme-switch" :class="{ on: isLight }"
              role="switch" :aria-checked="isLight" aria-label="主题开关"
              @click="toggleThemePref"
            ></button>
            <span class="mono">{{ isLight ? "净白" : "暗紫晶" }}</span>
          </div>
        </div>
      </section>

      <section class="sec">
        <h2>记忆</h2>
        <p class="desc">每个片段的消息数上限：达到后封块并生成摘要。</p>
        <div class="pref">
          <div class="txt">
            <div class="t">记忆封块分档</div>
            <div class="d">短 5 / 标准 10 / 长 15 / 自定义 1-30 轮</div>
          </div>
          <div class="ctl">
            <QSelect
              :options="tierOptions"
              :model-value="fragmentTier"
              @update:model-value="onTierSelect"
            />
            <QNumber
              v-if="fragmentTier === 'custom'"
              class="num" :model-value="customCount"
              :min="1" :max="30" mono label="自定义轮数"
              @update:model-value="onCustomCountInput"
              @change="saveMemorySettings"
            />
          </div>
        </div>
      </section>

      <section class="sec">
        <h2>维护</h2>
        <p class="desc">离线整理记忆与知识（后台执行）。</p>
        <div class="pref">
          <div class="txt">
            <div class="t">离线维护</div>
            <div class="d">启用后台整理；间隔 1-720 小时可调</div>
          </div>
          <div class="ctl">
            <button
              type="button" class="qio-switch" :class="{ on: maintenanceEnabled }"
              role="switch" :aria-checked="maintenanceEnabled" aria-label="离线维护开关"
              @click="toggleMaintenance"
            ></button>
            <QNumber
              class="num" :model-value="maintenanceInterval"
              :min="1" :max="720" mono label="维护间隔（小时）"
              @update:model-value="onIntervalInput"
              @change="saveMaintenance"
            />
            <button type="button" class="qio-btn" @click="runMaintenanceNow">立即运行</button>
          </div>
        </div>
        <p v-if="settingsNotice" class="msg ok">{{ settingsNotice }}</p>
      </section>
    </div>

    <!-- 窗口 -->
    <div v-show="activeTab === 'win'" class="panel">
      <section class="sec">
        <h2>窗口</h2>
        <p class="desc">对话页的输入框、话题星球入口与设置入口均为可拖动浮动组件：松手自动贴靠（输入框/星球贴边，设置入口贴角）。开启「贴靠隐藏」后，贴靠完成的组件会自动隐藏为细边或淡化，鼠标悬停展开、移出再隐藏。</p>
        <div class="pref" v-for="item in WINDOW_ITEMS" :key="item.id">
          <div class="txt">
            <div class="t">{{ item.title }}</div>
            <div class="d">{{ item.desc }}</div>
          </div>
          <div class="ctl">
            <button
              type="button" class="qio-switch" :class="{ on: floatingState[item.id].hideEnabled }"
              role="switch" :aria-checked="floatingState[item.id].hideEnabled"
              :aria-label="item.title + '贴靠隐藏'" @click="toggleWindowHide(item.id)"
            ></button>
            <span class="mono">{{ floatingState[item.id].hideEnabled ? "隐藏" : "常显" }}</span>
          </div>
        </div>
        <div class="pref">
          <div class="txt">
            <div class="t">布局</div>
            <div class="d">将所有浮动组件还原到默认位置并关闭贴靠隐藏</div>
          </div>
          <div class="ctl">
            <button type="button" class="qio-btn" @click="resetWindowLayout">还原默认布局</button>
          </div>
        </div>
        <p v-if="windowNotice" class="msg ok">{{ windowNotice }}</p>
      </section>
    </div>

    <transition name="toast">
      <div v-if="toast" class="toast" :class="toast.kind" role="status">{{ toast.text }}</div>
    </transition>
  </div>
</template>

<style scoped>
/* 分层：--bg-base 底，卡片/输入区用 --bg-elevated / --bg-inset，全部走令牌 */
.settings {
  max-width: 880px;
  margin: 0 auto;
  padding: 28px 24px 60px;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
}
header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 22px; }
header h1 { font-family: var(--serif); font-size: 26px; font-weight: 600; color: var(--text-strong); }
.sub { font-size: 11px; color: var(--text-muted); letter-spacing: 0.06em; }
.spacer { flex: 1; }
.back { color: var(--link); text-decoration: none; font-size: 13px; }
.back:hover { text-decoration: underline; }

/* tabs */
.tabs { display: flex; gap: 6px; border-bottom: 1px solid var(--border-subtle); margin-bottom: 24px; }
.tab {
  font-size: 14px; padding: 9px 18px; color: var(--text-secondary); cursor: pointer;
  background: none; border: none; border-bottom: 2px solid transparent; font-family: var(--sans);
}
.tab:hover { color: var(--text-strong); }
.tab.active { color: var(--text-strong); border-bottom-color: var(--accent); font-weight: 600; }

/* section */
.sec { margin-bottom: 26px; }
.sec h2 { font-family: var(--serif); font-size: 16px; font-weight: 600; color: var(--text-strong); margin-bottom: 4px; }
.desc { font-size: 12px; color: var(--text-muted); margin-bottom: 12px; }

.msg { font-size: 13px; margin-bottom: 10px; }
.msg.ok { color: var(--success); }
.msg.err { color: var(--danger); }
.empty { font-size: 12px; color: var(--text-muted); }

/* form */
.form { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 12px 0; }
.field { display: block; }
.field .label { display: block; font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.identify { grid-column: 1 / -1; font-size: 11px; color: var(--text-muted); min-height: 16px; }
.identify.done { color: var(--success); }
.identify.failed { color: var(--warning); }
.span2 { grid-column: 1 / -1; }
.edit-hint { font-size: 11px; color: var(--warning); margin: 0 0 10px; }
.create { grid-column: 1 / -1; justify-self: start; padding: 0 24px; }

/* preferences */
.pref {
  display: flex; align-items: center; gap: 14px; padding: 13px 4px;
  border-bottom: 1px solid var(--border-subtle); flex-wrap: wrap;
}
.pref .txt { flex: 1; min-width: 200px; }
.pref .txt .t { font-size: 13.5px; color: var(--text-strong); }
.pref .txt .d { font-size: 11.5px; color: var(--text-muted); margin-top: 2px; }
.pref .ctl { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 11px; color: var(--text-secondary); flex-wrap: wrap; }
.num { width: 90px; }
/* 测试凭据提示：浮现又消失的 toast 气泡 */
.toast {
  position: fixed;
  top: 20px;
  right: 20px;
  bottom: auto;
  z-index: 60;
  max-width: 340px;
  padding: 10px 16px;
  border-radius: 12px;
  font-size: 12.5px;
  line-height: 1.5;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.3);
  color: var(--text-primary);
}
.toast.ok { border-color: var(--success); color: var(--success); }
.toast.err { border-color: var(--danger); color: var(--danger); }
.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.28s ease, transform 0.28s cubic-bezier(0.22, 0.8, 0.24, 1);
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(10px);
}
</style>

