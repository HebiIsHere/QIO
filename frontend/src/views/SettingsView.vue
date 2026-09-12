<script setup lang="ts">
/**
 * 设置页：按用户心智分区（外观 / 对话与记忆 / 模型与联网 / 工具与权限 / 数据与维护 / 凭据 / 高级）。
 * 反馈按分区独立（notices），成功 / 失败 / 警告 / 信息用不同语义类，颜色全部来自令牌。
 * 服务端 tuning（SearXNG、原始阈值、图形诊断）收进「高级」，默认不打扰普通用户。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { api, type CredentialMeta } from "../services/api";
import QNumber from "../components/ui/QNumber.vue";
import QSelect from "../components/ui/QSelect.vue";
import { useUiStore, TYPEWRITER_SPEEDS } from "../stores/ui";
import CredentialCard from "./settings/CredentialCard.vue";
import CredentialModal, { type CredentialModalMode } from "./settings/CredentialModal.vue";
import {
  getThemePreference,
  setThemePreference,
  type ThemePreference,
} from "../utils/theme";
import {
  floatingState,
  resetFloatPositions,
  setHideEnabled,
  type DockId,
} from "../composables/floatingState";

type SectionId = "appearance" | "chat" | "model" | "tools" | "data" | "cred" | "advanced";
type NoticeKind = "ok" | "err" | "warn" | "info";
type NoticeKey = SectionId | "loop" | "window";
interface Notice {
  kind: NoticeKind;
  text: string;
}

const SECTIONS: { id: SectionId; label: string }[] = [
  { id: "appearance", label: "外观" },
  { id: "chat", label: "对话与记忆" },
  { id: "model", label: "模型与联网" },
  { id: "tools", label: "工具与权限" },
  { id: "data", label: "数据与维护" },
  { id: "cred", label: "凭据" },
  { id: "advanced", label: "高级" },
];

const activeTab = ref<SectionId>("appearance");
const ui = useUiStore();

/** 每个设置组自己的反馈：不共用一个 notice，避免「看起来是搜索保存了，其实是记忆报错」 */
const notices = reactive<Record<NoticeKey, Notice | null>>({
  appearance: null,
  chat: null,
  loop: null,
  model: null,
  tools: null,
  data: null,
  cred: null,
  advanced: null,
  window: null,
});

function setNotice(key: NoticeKey, kind: NoticeKind, text: string) {
  notices[key] = { kind, text };
}
function clearNotice(key: NoticeKey) {
  notices[key] = null;
}
function errText(action: string, e: unknown): string {
  return `${action}失败：${(e as Error).message}`;
}

/* ---------------- 凭据 ---------------- */
const credentials = ref<CredentialMeta[]>([]);
const credFilter = ref<"all" | "enabled" | "disabled" | "revoked" | "expired">("enabled");
const modal = ref<{
  open: boolean;
  mode: CredentialModalMode;
  initial: Record<string, unknown> | null;
}>({ open: false, mode: "create", initial: null });

const filteredCredentials = computed(() => {
  const list = credentials.value;
  switch (credFilter.value) {
    case "enabled":
      return list.filter((c) => c.status === "active" && c.enabled);
    case "disabled":
      return list.filter((c) => c.status === "active" && !c.enabled);
    case "revoked":
      return list.filter((c) => c.status === "revoked");
    case "expired":
      return list.filter((c) => c.status === "expired");
    default:
      return list;
  }
});

const FILTERS = [
  { value: "all", label: "全部" },
  { value: "enabled", label: "已启用" },
  { value: "disabled", label: "已停用" },
  { value: "revoked", label: "已撤销" },
  { value: "expired", label: "已过期" },
] as const;

function openCreate() {
  modal.value = { open: true, mode: "create", initial: null };
}
function openMeta(c: CredentialMeta) {
  modal.value = { open: true, mode: "meta", initial: { ...c } };
}
function openRotate(c: CredentialMeta) {
  modal.value = {
    open: true,
    mode: "rotate",
    initial: { ...c, key_id: `${c.key_id}_new` },
  };
}

async function load() {
  try {
    credentials.value = (await api.listCredentials()).credentials;
  } catch (e) {
    setNotice("cred", "err", errText("加载凭据", e));
  }
}

async function onModalSave(payload: Record<string, unknown>) {
  clearNotice("cred");
  const mode = modal.value.mode;
  const target = modal.value.initial?.key_id;
  try {
    if (mode === "meta" && target) {
      await api.updateCredentialMeta(String(target), payload);
      setNotice("cred", "ok", "凭据元数据已更新（密钥未变）");
    } else {
      await api.createCredential(payload);
      setNotice(
        "cred",
        "ok",
        mode === "rotate" && target
          ? `新凭据已创建。请撤销旧凭据「${target}」，避免双 key 并存（预算各计）。`
          : "凭据已创建",
      );
    }
    modal.value.open = false;
    await load();
  } catch (e) {
    setNotice("cred", "err", `${mode === "meta" ? "更新" : "创建"}凭据失败：${(e as Error).message}`);
  }
}

function toggleEnabled(c: CredentialMeta) {
  void (async () => {
    clearNotice("cred");
    try {
      await api.setCredentialEnabled(c.key_id, !c.enabled);
      await load();
    } catch (e) {
      setNotice("cred", "err", errText("切换凭据状态", e));
    }
  })();
}

function remove(keyId: string) {
  if (!window.confirm(`确定彻底删除凭据「${keyId}」？将删除密钥与全部记录，不可恢复。`)) return;
  void (async () => {
    clearNotice("cred");
    try {
      await api.deleteCredential(keyId);
      credentials.value = credentials.value.filter((c) => c.key_id !== keyId);
    } catch (e) {
      setNotice("cred", "err", errText("删除凭据", e));
    }
  })();
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

/* ---------------- 外观 ---------------- */
const themePref = ref<ThemePreference>(getThemePreference());
const THEME_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "系统" },
  { value: "dark", label: "暗紫晶" },
  { value: "light", label: "净白" },
];
function chooseTheme(p: ThemePreference) {
  themePref.value = p;
  setThemePreference(p);
  setNotice("appearance", "ok", `主题已切换：${THEME_OPTIONS.find((o) => o.value === p)?.label}`);
}

const speedOptions = TYPEWRITER_SPEEDS.map((cps) => ({
  value: String(cps),
  label: cps === 25 ? "慢（25 字/秒）" : cps === 50 ? "中（50 字/秒）" : "快（75 字/秒）",
}));

function onSpeedSelect(v: string) {
  const cps = Number(v);
  if (!TYPEWRITER_SPEEDS.includes(cps as (typeof TYPEWRITER_SPEEDS)[number])) return;
  void ui.setCps(cps);
  setNotice("appearance", "ok", `输出速度已保存：${cps} 字/秒`);
}

/* ---------------- 窗口行为 ---------------- */
const WINDOW_ITEMS: { id: DockId; title: string; desc: string }[] = [
  { id: "planet-dock", title: "话题星球入口", desc: "贴靠后淡化隐藏，悬停展开、移出再隐藏" },
  { id: "settings-float", title: "设置入口", desc: "贴角后淡化隐藏，悬停展开、移出再隐藏" },
];
function toggleWindowHide(id: DockId) {
  setHideEnabled(id, !floatingState[id].hideEnabled);
}
function resetWindowLayout() {
  resetFloatPositions();
  setNotice("window", "ok", "已还原默认布局（贴靠隐藏关闭、位置回到默认）");
}

/* ---------------- 对话与记忆 ---------------- */
const fragmentTier = ref("10");
const customCount = ref(10);
const tierPresets = ["5", "10", "15"];
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
    setNotice("chat", "err", errText("加载记忆设置", e));
  }
}

async function saveMemorySettings() {
  const value = fragmentTier.value === "custom" ? customCount.value : Number(fragmentTier.value);
  clearNotice("chat");
  if (!Number.isInteger(value) || value < 1 || value > 30) {
    setNotice("chat", "err", "自定义轮数需在 1-30 之间");
    return;
  }
  try {
    const r = await api.updateMemorySettings(value);
    setNotice("chat", "ok", `记忆封块已保存：${r.fragment_max_messages} 轮`);
    const saved = String(r.fragment_max_messages);
    if (tierPresets.includes(saved)) {
      fragmentTier.value = saved;
    } else {
      fragmentTier.value = "custom";
      customCount.value = r.fragment_max_messages;
    }
  } catch (e) {
    setNotice("chat", "err", errText("保存记忆设置", e));
  }
}

function onTierSelect(v: string) {
  fragmentTier.value = v;
  if (v !== "custom") void saveMemorySettings();
}
function onCustomCountInput(v: number | null) {
  customCount.value = v ?? 0;
}

const loopMaxIterations = ref(128);
const loopTokenBudget = ref(51200);

async function loadLoopSettings() {
  try {
    const s = await api.getLoopSettings();
    loopMaxIterations.value = s.max_iterations;
    loopTokenBudget.value = s.output_token_budget;
  } catch (e) {
    setNotice("loop", "err", errText("加载对话深度", e));
  }
}

async function saveLoopSettings() {
  clearNotice("loop");
  if (!Number.isInteger(loopMaxIterations.value) || loopMaxIterations.value < 1 || loopMaxIterations.value > 1000) {
    setNotice("loop", "err", "迭代上限需在 1-1000 之间");
    return;
  }
  if (!Number.isInteger(loopTokenBudget.value) || loopTokenBudget.value < 0) {
    setNotice("loop", "err", "输出预算需为不小于 0 的整数");
    return;
  }
  try {
    const r = await api.updateLoopSettings({
      max_iterations: loopMaxIterations.value,
      output_token_budget: loopTokenBudget.value,
    });
    loopMaxIterations.value = r.max_iterations;
    loopTokenBudget.value = r.output_token_budget;
    setNotice("loop", "ok", "对话深度已保存");
  } catch (e) {
    setNotice("loop", "err", errText("保存对话深度", e));
  }
}
function onLoopIterationsInput(v: number | null) {
  loopMaxIterations.value = v ?? 0;
}
function onLoopTokensInput(v: number | null) {
  loopTokenBudget.value = v ?? 0;
}

/* ---------------- 模型与联网 ---------------- */
const searchTopK = ref(5);
const searchMaxChars = ref(15000);
const searchSearxngUrl = ref("");
/** 博查 Key 是否已配置（真实状态来自后端，不在前端保存明文） */
const bochaConfigured = ref(false);
/** 用户显式点击「替换」后才进入编辑态：空输入默认不修改已有凭据 */
const bochaEditing = ref(false);
const bochaKeyDraft = ref("");
/** 免密钥通道（Exa / Parallel 免费 MCP + DuckDuckGo HTML）：默认开启 */
const searchKeyless = ref(true);

async function loadSearchSettings() {
  try {
    const s = await api.getSearchSettings();
    searchTopK.value = s.top_k_default;
    searchMaxChars.value = s.max_fetch_chars;
    searchSearxngUrl.value = s.searxng_url;
    bochaConfigured.value = s.bocha_has_key;
    searchKeyless.value = s.keyless_fallback ?? true;
  } catch (e) {
    setNotice("model", "err", errText("加载联网搜索设置", e));
  }
}

/** 免密钥开关：单独即时保存（与「保存搜索配置」解耦，不会连带写凭据字段） */
async function toggleKeyless() {
  const next = !searchKeyless.value;
  searchKeyless.value = next;
  clearNotice("model");
  try {
    const r = await api.updateSearchSettings({ keyless_fallback: next });
    searchKeyless.value = r.keyless_fallback ?? next;
    setNotice(
      "model",
      "ok",
      searchKeyless.value
        ? "已开启免密钥联网搜索（Exa / Parallel / DuckDuckGo）"
        : "已关闭免密钥联网搜索（只用博查 / 自建 SearXNG / 必应 / 百度）",
    );
  } catch (e) {
    searchKeyless.value = !next; // 失败回滚，不假装保存成功
    setNotice("model", "err", errText("保存免密钥搜索开关", e));
  }
}

/** 只发送「用户明确修改过」的凭据字段；留空永远不等于清除 */
function searchPayload(): Record<string, unknown> {
  const body: Record<string, unknown> = {
    top_k_default: searchTopK.value,
    max_fetch_chars: searchMaxChars.value,
    searxng_url: searchSearxngUrl.value.trim(),
  };
  if (bochaEditing.value && bochaKeyDraft.value.trim()) {
    body.bocha_api_key = bochaKeyDraft.value.trim();
  }
  return body;
}

async function saveSearchSettings() {
  clearNotice("model");
  if (!Number.isInteger(searchTopK.value) || searchTopK.value < 1 || searchTopK.value > 20) {
    setNotice("model", "err", "默认返回条数需在 1-20 之间");
    return;
  }
  if (!Number.isInteger(searchMaxChars.value) || searchMaxChars.value < 1000 || searchMaxChars.value > 40000) {
    setNotice("model", "err", "读正文字符预算需在 1000-40000 之间");
    return;
  }
  if (bochaEditing.value && !bochaKeyDraft.value.trim()) {
    setNotice("model", "err", "请输入新的博查 API Key，或取消替换");
    return;
  }
  try {
    const r = await api.updateSearchSettings(searchPayload());
    searchTopK.value = r.top_k_default;
    searchMaxChars.value = r.max_fetch_chars;
    searchSearxngUrl.value = r.searxng_url;
    bochaConfigured.value = r.bocha_has_key;
    bochaEditing.value = false;
    bochaKeyDraft.value = "";
    setNotice("model", "ok", "已保存搜索配置");
  } catch (e) {
    setNotice("model", "err", errText("保存搜索配置", e));
  }
}

/** 清除凭据必须是显式动作（二次确认 + 只发送该字段） */
async function clearBochaKey() {
  if (!window.confirm("确定清除已保存的博查 API Key？清除后将回退到免密钥搜索。")) return;
  clearNotice("model");
  try {
    const r = await api.updateSearchSettings({ bocha_api_key: "" });
    bochaConfigured.value = r.bocha_has_key;
    bochaEditing.value = false;
    bochaKeyDraft.value = "";
    setNotice("model", "warn", "博查 API Key 已清除，当前使用免密钥搜索");
  } catch (e) {
    setNotice("model", "err", errText("清除博查 API Key", e));
  }
}

function onSearchTopKInput(v: number | null) {
  searchTopK.value = v ?? 0;
}
function onSearchMaxCharsInput(v: number | null) {
  searchMaxChars.value = v ?? 0;
}

/* ---------------- 工具与权限 ---------------- */
const computerRootDir = ref("");
const computerPermissionMode = ref("default");
const permissionModeOptions = [
  { value: "default", label: "默认（低危自动，高危审批）" },
  { value: "plan", label: "只读规划（不写不执行）" },
  { value: "accept-edits", label: "接受编辑（文件编辑自动，命令仍审批）" },
  { value: "bypass", label: "全放行（危险，仅信任环境）" },
];

async function loadComputerSettings() {
  try {
    const s = await api.getComputerSettings();
    computerRootDir.value = s.root_dir || "";
    computerPermissionMode.value = s.permission_mode || "default";
  } catch (e) {
    setNotice("tools", "err", errText("加载电脑操控设置", e));
  }
}

async function saveComputerSettings() {
  clearNotice("tools");
  try {
    const r = await api.updateComputerSettings({
      root_dir: computerRootDir.value.trim(),
      permission_mode: computerPermissionMode.value,
    });
    computerRootDir.value = r.root_dir || "";
    computerPermissionMode.value = r.permission_mode || "default";
    setNotice("tools", "ok", "电脑操控配置已保存");
  } catch (e) {
    setNotice("tools", "err", errText("保存电脑操控配置", e));
  }
}

function onPermissionModeSelect(v: string) {
  computerPermissionMode.value = v;
  void saveComputerSettings();
}

/* ---------------- 数据与维护 ---------------- */
const maintenanceEnabled = ref(true);
const maintenanceInterval = ref(24);

async function loadMaintenanceSettings() {
  try {
    const s = await api.getMaintenanceSettings();
    maintenanceEnabled.value = s.enabled;
    maintenanceInterval.value = s.interval_hours;
  } catch (e) {
    setNotice("data", "err", errText("加载维护设置", e));
  }
}

async function saveMaintenance() {
  clearNotice("data");
  try {
    const r = await api.updateMaintenanceSettings({
      enabled: maintenanceEnabled.value,
      interval_hours: maintenanceInterval.value,
    });
    maintenanceEnabled.value = r.enabled;
    maintenanceInterval.value = r.interval_hours;
    setNotice(
      "data",
      "ok",
      `离线维护已保存：${r.enabled ? "开启" : "关闭"}（每 ${r.interval_hours} 小时）`,
    );
  } catch (e) {
    setNotice("data", "err", errText("保存维护设置", e));
  }
}

function toggleMaintenance() {
  maintenanceEnabled.value = !maintenanceEnabled.value;
  void saveMaintenance();
}

async function runMaintenanceNow() {
  clearNotice("data");
  try {
    const r = await api.runMaintenance();
    setNotice("data", r.started ? "ok" : "info", r.started ? "离线维护已启动（后台执行）" : "维护未启动");
  } catch (e) {
    setNotice("data", "err", errText("启动维护", e));
  }
}

function onIntervalInput(v: number | null) {
  maintenanceInterval.value = v ?? 0;
}

/* ---------------- 高级 / Developer ---------------- */
function toggleDeveloperMode() {
  ui.setDeveloperMode(!ui.developerMode);
  setNotice(
    "advanced",
    "ok",
    ui.developerMode ? "开发者模式已开启：显示图形诊断与 token 明细" : "开发者模式已关闭",
  );
}

onMounted(() => {
  void load();
  void loadMemorySettings();
  void loadMaintenanceSettings();
  void loadSearchSettings();
  void loadComputerSettings();
  void loadLoopSettings();
  void ui.load();
});
</script>

<template>
  <div class="settings">
    <header>
      <h1>设置</h1>
      <span class="sub mono">设置 · QIO</span>
      <span class="spacer"></span>
      <router-link to="/" class="back">← 返回对话</router-link>
    </header>

    <div class="layout">
      <nav class="nav" role="tablist" aria-label="设置分区">
        <button
          v-for="s in SECTIONS"
          :key="s.id"
          type="button"
          class="tab"
          role="tab"
          :class="{ active: activeTab === s.id }"
          :aria-selected="activeTab === s.id"
          @click="activeTab = s.id"
        >
          {{ s.label }}
        </button>
      </nav>

      <div class="panels">
        <!-- 外观 -->
        <div v-show="activeTab === 'appearance'" class="panel">
          <section class="sec">
            <h2>主题</h2>
            <p class="desc">跟随系统，或固定暗紫晶 / 净白；系统切换实时生效，无需刷新。</p>
            <div class="seg" role="group" aria-label="主题偏好">
              <button
                v-for="o in THEME_OPTIONS"
                :key="o.value"
                type="button"
                class="theme-opt"
                :class="{ on: themePref === o.value }"
                :aria-pressed="themePref === o.value"
                @click="chooseTheme(o.value)"
              >
                {{ o.label }}
              </button>
            </div>
          </section>

          <section class="sec">
            <h2>输出</h2>
            <p class="desc">助手回答的打字机显示速度：逐字输出，结构完整。</p>
            <div class="pref">
              <div class="txt">
                <div class="t">输出速度</div>
                <div class="d">慢 25 字/秒 · 中 50 字/秒 · 快 75 字/秒</div>
              </div>
              <div class="ctl">
                <QSelect
                  :options="speedOptions"
                  :model-value="String(ui.typewriterCps)"
                  @update:model-value="onSpeedSelect"
                />
              </div>
            </div>
          </section>

          <p v-if="notices.appearance" class="msg" :class="notices.appearance.kind" role="status">
            {{ notices.appearance.text }}
          </p>

          <section class="sec">
            <h2>窗口行为</h2>
            <p class="desc">
              输入框固定在对话底部；星球入口与设置入口为可拖动浮动组件，松手自动贴靠。
              开启「贴靠隐藏」后贴靠完成的组件会淡化，悬停展开、移出再隐藏。
            </p>
            <div class="pref" v-for="item in WINDOW_ITEMS" :key="item.id">
              <div class="txt">
                <div class="t">{{ item.title }}</div>
                <div class="d">{{ item.desc }}</div>
              </div>
              <div class="ctl">
                <button
                  type="button"
                  class="qio-switch"
                  :class="{ on: floatingState[item.id].hideEnabled }"
                  role="switch"
                  :aria-checked="floatingState[item.id].hideEnabled"
                  :aria-label="item.title + '贴靠隐藏'"
                  @click="toggleWindowHide(item.id)"
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
            <p v-if="notices.window" class="msg" :class="notices.window.kind" role="status">
              {{ notices.window.text }}
            </p>
          </section>
        </div>

        <!-- 对话与记忆 -->
        <div v-show="activeTab === 'chat'" class="panel">
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
                  class="num"
                  :model-value="customCount"
                  :min="1"
                  :max="30"
                  mono
                  label="自定义轮数"
                  @update:model-value="onCustomCountInput"
                  @change="saveMemorySettings"
                />
              </div>
            </div>
            <p v-if="notices.chat" class="msg" :class="notices.chat.kind" role="status">
              {{ notices.chat.text }}
            </p>
          </section>

          <section class="sec">
            <h2>对话深度</h2>
            <p class="desc">agent 单轮最多迭代次数与输出 token 预算；达上限时可选择继续。</p>
            <div class="pref">
              <div class="txt">
                <div class="t">单轮迭代上限</div>
                <div class="d">每轮任务最多重新决策的次数（1-1000）</div>
              </div>
              <div class="ctl">
                <QNumber
                  class="num"
                  :model-value="loopMaxIterations"
                  :min="1"
                  :max="1000"
                  mono
                  label="迭代上限"
                  @update:model-value="onLoopIterationsInput"
                  @change="saveLoopSettings"
                />
              </div>
            </div>
            <div class="pref">
              <div class="txt">
                <div class="t">输出 token 预算</div>
                <div class="d">单轮模型输出累计上限（0 表示不限）</div>
              </div>
              <div class="ctl">
                <QNumber
                  class="num"
                  :model-value="loopTokenBudget"
                  :min="0"
                  :max="500000"
                  mono
                  label="输出预算"
                  @update:model-value="onLoopTokensInput"
                  @change="saveLoopSettings"
                />
              </div>
            </div>
            <p v-if="notices.loop" class="msg" :class="notices.loop.kind" role="status">
              {{ notices.loop.text }}
            </p>
          </section>
        </div>

        <!-- 模型与联网 -->
        <div v-show="activeTab === 'model'" class="panel">
          <section class="sec">
            <h2>联网搜索</h2>
            <p class="desc">
              让 agent 能联网检索实时资讯并读取网页正文。免密钥通道用 Exa / Parallel 的
              免费搜索服务（失败时回退 DuckDuckGo）；填写博查密钥或自建 SearXNG 更稳定。
            </p>
            <div class="pref">
              <div class="txt">
                <div class="t">免密钥联网搜索</div>
                <div class="d">
                  用 Exa / Parallel 免费搜索服务，失败回退 DuckDuckGo；关闭后只用
                  博查 / 自建 SearXNG / 必应 / 百度
                </div>
              </div>
              <div class="ctl">
                <button
                  type="button"
                  class="qio-switch keyless-switch"
                  :class="{ on: searchKeyless }"
                  role="switch"
                  :aria-checked="searchKeyless"
                  aria-label="免密钥联网搜索"
                  @click="toggleKeyless"
                ></button>
                <span class="mono">{{ searchKeyless ? "开启" : "关闭" }}</span>
              </div>
            </div>
            <div class="pref">
              <div class="txt">
                <div class="t">默认返回条数</div>
                <div class="d">每次搜索返回的结果数（1-20）</div>
              </div>
              <div class="ctl">
                <QNumber
                  class="num"
                  :model-value="searchTopK"
                  :min="1"
                  :max="20"
                  mono
                  label="返回条数"
                  @update:model-value="onSearchTopKInput"
                />
              </div>
            </div>
            <div class="pref">
              <div class="txt">
                <div class="t">读正文字符预算</div>
                <div class="d">抓取网页正文的字符上限（1000-40000）</div>
              </div>
              <div class="ctl">
                <QNumber
                  class="num"
                  :model-value="searchMaxChars"
                  :min="1000"
                  :max="40000"
                  mono
                  label="字符预算"
                  @update:model-value="onSearchMaxCharsInput"
                />
              </div>
            </div>

            <div class="pref cred-row">
              <div class="txt">
                <div class="t">博查 API Key</div>
                <div class="d">
                  {{ bochaConfigured ? "已配置（留空保存不会改动它）" : "未配置：使用免密钥检索兜底" }}
                </div>
              </div>
              <div class="ctl">
                <template v-if="bochaEditing">
                  <input
                    v-model="bochaKeyDraft"
                    class="qio-input mono bocha-key-input"
                    type="password"
                    placeholder="粘贴新的博查 API Key"
                    autocomplete="off"
                  />
                  <button type="button" class="qio-btn" @click="bochaEditing = false; bochaKeyDraft = ''">
                    取消替换
                  </button>
                </template>
                <template v-else>
                  <span class="state mono">{{ bochaConfigured ? "已配置" : "未配置" }}</span>
                  <button type="button" class="qio-btn" @click="bochaEditing = true">替换</button>
                  <button
                    v-if="bochaConfigured"
                    type="button"
                    class="qio-btn danger"
                    @click="clearBochaKey"
                  >
                    清除
                  </button>
                </template>
              </div>
            </div>

            <div class="pref">
              <div class="txt">
                <div class="t">SearXNG 实例</div>
                <div class="d">自建实例地址在「高级」中配置</div>
              </div>
              <div class="ctl">
                <button type="button" class="qio-btn primary" @click="saveSearchSettings">
                  保存搜索配置
                </button>
              </div>
            </div>
            <p v-if="notices.model" class="msg" :class="notices.model.kind" role="status">
              {{ notices.model.text }}
            </p>
          </section>
        </div>

        <!-- 工具与权限 -->
        <div v-show="activeTab === 'tools'" class="panel">
          <section class="sec">
            <h2>电脑操控</h2>
            <p class="desc">允许 qio 读写文件、执行命令、查看进程。分级授权，高危操作会触发审批。</p>
            <div class="pref">
              <div class="txt">
                <div class="t">工作区根目录</div>
                <div class="d">qio 默认可访问的目录；之外默认要审批</div>
              </div>
              <div class="ctl">
                <input
                  v-model="computerRootDir"
                  class="qio-input mono computer-root"
                  type="text"
                  placeholder="留空则用默认工作区"
                  @change="saveComputerSettings"
                />
              </div>
            </div>
            <div class="pref">
              <div class="txt">
                <div class="t">权限模式</div>
                <div class="d">决定动作是否需要人工确认</div>
              </div>
              <div class="ctl">
                <QSelect
                  :options="permissionModeOptions"
                  :model-value="computerPermissionMode"
                  @update:model-value="onPermissionModeSelect"
                />
              </div>
            </div>
            <p v-if="notices.tools" class="msg" :class="notices.tools.kind" role="status">
              {{ notices.tools.text }}
            </p>
          </section>
        </div>

        <!-- 数据与维护 -->
        <div v-show="activeTab === 'data'" class="panel">
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
                  type="button"
                  class="qio-switch"
                  :class="{ on: maintenanceEnabled }"
                  role="switch"
                  :aria-checked="maintenanceEnabled"
                  aria-label="离线维护开关"
                  @click="toggleMaintenance"
                ></button>
                <QNumber
                  class="num"
                  :model-value="maintenanceInterval"
                  :min="1"
                  :max="720"
                  mono
                  label="维护间隔（小时）"
                  @update:model-value="onIntervalInput"
                  @change="saveMaintenance"
                />
                <button type="button" class="qio-btn" @click="runMaintenanceNow">立即运行</button>
              </div>
            </div>
            <p v-if="notices.data" class="msg" :class="notices.data.kind" role="status">
              {{ notices.data.text }}
            </p>
          </section>
        </div>

        <!-- 凭据 -->
        <div v-show="activeTab === 'cred'" class="panel">
          <section class="sec">
            <div class="sec-head">
              <h2>凭据</h2>
              <button type="button" class="qio-btn primary new-cred" @click="openCreate">＋ 新建凭据</button>
            </div>
            <p class="desc">密钥只写不读：保存后不再显示明文，仅本地写入 keyring，服务器不落盘。</p>
            <p v-if="notices.cred" class="msg" :class="notices.cred.kind" role="alert">
              {{ notices.cred.text }}
            </p>

            <div class="filters">
              <button
                v-for="f in FILTERS"
                :key="f.value"
                type="button"
                class="qio-btn filter"
                :class="{ active: credFilter === f.value }"
                @click="credFilter = f.value"
              >
                {{ f.label }}
              </button>
            </div>

            <CredentialCard
              v-for="c in filteredCredentials"
              :key="c.key_id"
              :credential="c"
              @test="test(c.key_id)"
              @edit-meta="openMeta(c)"
              @rotate="openRotate(c)"
              @toggle-enabled="toggleEnabled(c)"
              @remove="remove(c.key_id)"
            />
            <p v-if="!filteredCredentials.length" class="empty mono">
              {{ credFilter === "all" ? "尚无凭据。" : "该筛选下无凭据。" }}
            </p>
          </section>
        </div>

        <!-- 高级 -->
        <div v-show="activeTab === 'advanced'" class="panel">
          <section class="sec">
            <h2>开发者</h2>
            <p class="desc">
              打开后显示 FPS / WebGL / 视角等图形诊断，以及每条消息的 token 明细。
              日常使用建议保持关闭。
            </p>
            <div class="pref">
              <div class="txt">
                <div class="t">开发者模式</div>
                <div class="d">图形诊断与 token 明细仅在开启后出现</div>
              </div>
              <div class="ctl">
                <button
                  type="button"
                  class="qio-switch"
                  :class="{ on: ui.developerMode }"
                  role="switch"
                  :aria-checked="ui.developerMode"
                  aria-label="开发者模式"
                  @click="toggleDeveloperMode"
                ></button>
                <span class="mono">{{ ui.developerMode ? "开启" : "关闭" }}</span>
              </div>
            </div>
            <p v-if="notices.advanced" class="msg" :class="notices.advanced.kind" role="status">
              {{ notices.advanced.text }}
            </p>
          </section>

          <section class="sec">
            <h2>检索服务</h2>
            <details class="adv-fold">
              <summary>自建 SearXNG 实例（默认折叠）</summary>
              <div class="adv-body">
                <p class="desc">可选，需自建或自备可达实例；与「联网搜索」共用保存动作。</p>
                <input
                  v-model="searchSearxngUrl"
                  class="qio-input mono"
                  type="text"
                  placeholder="https://your-searxng.example"
                  autocomplete="off"
                />
                <button type="button" class="qio-btn" @click="saveSearchSettings">保存搜索配置</button>
              </div>
            </details>
          </section>

          <section class="sec">
            <h2>当前生效参数</h2>
            <p class="desc">只读展示，便于排查；普通使用无需关心。</p>
            <dl class="raw mono">
              <dt>记忆封块</dt><dd>{{ fragmentTier === "custom" ? customCount : fragmentTier }} 轮</dd>
              <dt>迭代上限</dt><dd>{{ loopMaxIterations }}</dd>
              <dt>输出预算</dt><dd>{{ loopTokenBudget }}</dd>
              <dt>搜索条数</dt><dd>{{ searchTopK }}</dd>
              <dt>正文字符预算</dt><dd>{{ searchMaxChars }}</dd>
              <dt>免密钥搜索</dt>
              <dd>{{ searchKeyless ? "开启（Exa / Parallel / DuckDuckGo）" : "关闭" }}</dd>
              <dt>SearXNG</dt><dd>{{ searchSearxngUrl || "（未配置）" }}</dd>
              <dt>离线维护</dt>
              <dd>{{ maintenanceEnabled ? `开启 · 每 ${maintenanceInterval} 小时` : "关闭" }}</dd>
            </dl>
          </section>
        </div>
      </div>
    </div>

    <CredentialModal
      v-if="modal.open"
      :mode="modal.mode"
      :initial="modal.initial"
      @save="onModalSave"
      @cancel="modal.open = false"
    />

    <transition name="toast">
      <div v-if="toast" class="toast" :class="toast.kind" role="status">{{ toast.text }}</div>
    </transition>
  </div>
</template>

<style scoped>
.settings {
  max-width: 1080px;
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

/* 布局：左侧分区导航 + 右侧内容（窄窗口自动堆叠） */
.layout { display: flex; gap: 26px; align-items: flex-start; }
.nav {
  display: flex; flex-direction: column; gap: 2px; flex: 0 0 auto;
  min-width: 148px; position: sticky; top: 0;
}
.tab {
  font-size: 13.5px; padding: 8px 12px; color: var(--text-secondary); cursor: pointer;
  background: none; border: none; border-left: 2px solid transparent; text-align: left;
  font-family: var(--sans); border-radius: 0 var(--r-sm) var(--r-sm) 0;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.tab:hover { color: var(--text-strong); background: var(--bg-surface); }
.tab.active { color: var(--text-strong); border-left-color: var(--accent); font-weight: 600; }
.panels { flex: 1; min-width: 0; }
@media (max-width: 860px) {
  .layout { flex-direction: column; gap: 12px; }
  .nav { flex-direction: row; flex-wrap: wrap; min-width: 0; position: static; gap: 6px; }
  .tab { border-left: none; border-bottom: 2px solid transparent; border-radius: var(--r-sm); }
  .tab.active { border-bottom-color: var(--accent); }
}

.sec { margin-bottom: 26px; }
.sec h2 { font-family: var(--serif); font-size: 16px; font-weight: 600; color: var(--text-strong); margin-bottom: 4px; }
.desc { font-size: 12.5px; color: var(--text-muted); margin-bottom: 12px; line-height: 1.65; }

/* 反馈：成功 / 失败 / 警告 / 信息 语义独立，颜色全部来自令牌 */
.msg { font-size: 12.5px; margin: 10px 0; line-height: 1.55; }
.msg.ok { color: var(--success); }
.msg.err { color: var(--danger); }
.msg.warn { color: var(--warning); }
.msg.info { color: var(--text-secondary); }
.empty { font-size: 12px; color: var(--text-muted); }

/* 主题分段控件 */
.seg { display: inline-flex; gap: 4px; padding: 3px; border: 1px solid var(--border-subtle); border-radius: var(--r-md); background: var(--bg-inset); }
.theme-opt {
  font-family: var(--sans); font-size: 12.5px; padding: 6px 16px; border-radius: var(--r-sm);
  border: none; background: transparent; color: var(--text-secondary); cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.theme-opt:hover { color: var(--text-strong); }
.theme-opt.on { background: var(--accent-soft); color: var(--text-strong); font-weight: 600; }

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
.pref .ctl .qio-input {
  min-width: 240px;
  padding: 8px 12px;
  border-radius: var(--r-md);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  color: var(--text-primary);
  font-family: var(--mono);
  font-size: 12px;
  outline: none;
}
.pref .ctl .qio-input::placeholder { color: var(--text-muted); }
.pref .ctl .qio-input:focus { border-color: var(--accent); }
.pref .ctl .qio-input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.state { color: var(--text-secondary); letter-spacing: 0.04em; }

.sec-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.sec-head h2 { margin: 0; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 14px; }
.filter { font-size: 12px; padding: 4px 12px; }
.filter.active { color: var(--text-strong); border-color: var(--accent); background: var(--accent-soft); }

/* 高级：原始参数折叠区 */
.adv-fold { border: 1px solid var(--border-subtle); border-radius: var(--r-md); background: var(--bg-inset); padding: 4px 12px; }
.adv-fold summary { font-size: 12.5px; color: var(--text-secondary); cursor: pointer; padding: 8px 0; }
.adv-fold summary:hover { color: var(--text-strong); }
.adv-body { display: flex; flex-direction: column; gap: 10px; padding: 4px 0 12px; align-items: flex-start; }
.adv-body .qio-input { width: min(420px, 100%); }
.raw { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; font-size: 11.5px; color: var(--text-secondary); }
.raw dt { color: var(--text-muted); }
.raw dd { color: var(--text-secondary); word-break: break-all; }

.toast {
  position: fixed;
  top: 20px;
  right: 20px;
  z-index: 60;
  max-width: 340px;
  padding: 10px 16px;
  border-radius: var(--r-md);
  font-size: 12.5px;
  line-height: 1.5;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  box-shadow: var(--shadow-2);
  color: var(--text-primary);
}
.toast.ok { border-color: var(--success); color: var(--success); }
.toast.err { border-color: var(--danger); color: var(--danger); }
.toast-enter-active,
.toast-leave-active {
  transition: opacity var(--dur-slow) var(--ease), transform var(--dur-slow) var(--ease);
}
.toast-enter-from,
.toast-leave-to { opacity: 0; transform: translateY(10px); }
</style>
