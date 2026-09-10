<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type CredentialMeta } from "../services/api";
import QNumber from "../components/ui/QNumber.vue";
import QSelect from "../components/ui/QSelect.vue";
import { useUiStore, TYPEWRITER_SPEEDS } from "../stores/ui";
import CredentialCard from "./settings/CredentialCard.vue";
import CredentialModal, { type CredentialModalMode } from "./settings/CredentialModal.vue";
import { getTheme, toggleTheme } from "../utils/theme";
import {
  floatingState,
  resetFloatPositions,
  setHideEnabled,
  type DockId,
} from "../composables/floatingState";

const credentials = ref<CredentialMeta[]>([]);
const ui = useUiStore();
const notice = ref("");
// 默认只显示可用凭据；被撤销/过期/停用的需手动切换到对应筛选，避免旧数据突然出现。
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
const tierPresets = ["5", "10", "15"];
const searchTopK = ref(5);
const searchMaxChars = ref(15000);
const searchSearxngUrl = ref("");
const searchBochaKey = ref("");
const searchBochaHasKey = ref(false);

async function loadSearchSettings() {
  try {
    const s = await api.getSearchSettings();
    searchTopK.value = s.top_k_default;
    searchMaxChars.value = s.max_fetch_chars;
    searchSearxngUrl.value = s.searxng_url;
    searchBochaHasKey.value = s.bocha_has_key;
  } catch (e) {
    console.error("[settings] load search settings failed:", e);
  }
}

async function saveSearchSettings() {
  if (!Number.isInteger(searchTopK.value) || searchTopK.value < 1 || searchTopK.value > 20) {
    settingsNotice.value = "默认返回条数需在 1-20 之间";
    return;
  }
  if (
    !Number.isInteger(searchMaxChars.value) ||
    searchMaxChars.value < 1000 ||
    searchMaxChars.value > 40000
  ) {
    settingsNotice.value = "读正文字符预算需在 1000-40000 之间";
    return;
  }
  try {
    const r = await api.updateSearchSettings({
      top_k_default: searchTopK.value,
      max_fetch_chars: searchMaxChars.value,
      searxng_url: searchSearxngUrl.value.trim(),
      bocha_api_key: searchBochaKey.value.trim(),
    });
    searchTopK.value = r.top_k_default;
    searchMaxChars.value = r.max_fetch_chars;
    searchSearxngUrl.value = r.searxng_url;
    searchBochaHasKey.value = r.bocha_has_key;
    searchBochaKey.value = "";
    settingsNotice.value = "已保存搜索配置";
  } catch (e) {
    settingsNotice.value = `保存失败：${(e as Error).message}`;
  }
}

function onSearchTopKInput(v: number | null) {
  searchTopK.value = v ?? 0;
}
function onSearchMaxCharsInput(v: number | null) {
  searchMaxChars.value = v ?? 0;
}

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

const speedOptions = TYPEWRITER_SPEEDS.map((cps) => ({
  value: String(cps),
  label: cps === 25 ? "慢（25 字/秒）" : cps === 50 ? "中（50 字/秒）" : "快（75 字/秒）",
}));

function onSpeedSelect(v: string) {
  const cps = Number(v);
  if (TYPEWRITER_SPEEDS.includes(cps as (typeof TYPEWRITER_SPEEDS)[number])) {
    void ui.setCps(cps);
    settingsNotice.value = `已保存：输出速度 ${cps} 字/秒`;
  }
}

const computerRootDir = ref("");
const computerPermissionMode = ref("default");
const computerNotice = ref("");

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
    console.error("[settings] load computer settings failed:", e);
  }
}

async function saveComputerSettings() {
  try {
    const r = await api.updateComputerSettings({
      root_dir: computerRootDir.value.trim(),
      permission_mode: computerPermissionMode.value,
    });
    computerRootDir.value = r.root_dir || "";
    computerPermissionMode.value = r.permission_mode || "default";
    computerNotice.value = "已保存电脑操控配置";
  } catch (e) {
    computerNotice.value = `保存失败：${(e as Error).message}`;
  }
}

function onPermissionModeSelect(v: string) {
  computerPermissionMode.value = v;
  void saveComputerSettings();
}

const loopMaxIterations = ref(128);
const loopTokenBudget = ref(51200);
const loopNotice = ref("");

async function loadLoopSettings() {
  try {
    const s = await api.getLoopSettings();
    loopMaxIterations.value = s.max_iterations;
    loopTokenBudget.value = s.output_token_budget;
  } catch (e) {
    console.error("[settings] load loop settings failed:", e);
  }
}

async function saveLoopSettings() {
  if (!Number.isInteger(loopMaxIterations.value) || loopMaxIterations.value < 1 || loopMaxIterations.value > 1000) {
    loopNotice.value = "迭代上限需在 1-1000 之间";
    return;
  }
  if (!Number.isInteger(loopTokenBudget.value) || loopTokenBudget.value < 0) {
    loopNotice.value = "输出预算需为不小于 0 的整数";
    return;
  }
  try {
    const r = await api.updateLoopSettings({
      max_iterations: loopMaxIterations.value,
      output_token_budget: loopTokenBudget.value,
    });
    loopMaxIterations.value = r.max_iterations;
    loopTokenBudget.value = r.output_token_budget;
    loopNotice.value = "已保存对话深度";
  } catch (e) {
    loopNotice.value = `保存失败：${(e as Error).message}`;
  }
}

function onLoopIterationsInput(v: number | null) {
  loopMaxIterations.value = v ?? 0;
}
function onLoopTokensInput(v: number | null) {
  loopTokenBudget.value = v ?? 0;
}

async function load() {
  try {
    credentials.value = (await api.listCredentials()).credentials;
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function onModalSave(payload: Record<string, unknown>) {
  error.value = "";
  notice.value = "";
  const mode = modal.value.mode;
  const target = modal.value.initial?.key_id;
  try {
    if (mode === "meta" && target) {
      await api.updateCredentialMeta(String(target), payload);
      notice.value = "凭据已更新（密钥未变）";
    } else {
      await api.createCredential(payload);
      notice.value =
        mode === "rotate" && target
          ? `新凭据已创建。请撤销旧凭据「${target}」，避免双 key 并存（预算各计）。`
          : "凭据已创建";
    }
    modal.value.open = false;
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  }
}

function toggleEnabled(c: CredentialMeta) {
  void (async () => {
    error.value = "";
    try {
      await api.setCredentialEnabled(c.key_id, !c.enabled);
      await load();
    } catch (e) {
      error.value = (e as Error).message;
    }
  })();
}

function remove(keyId: string) {
  if (!window.confirm(`确定彻底删除凭据「${keyId}」？将删除密钥与全部记录，不可恢复。`)) return;
  void (async () => {
    error.value = "";
    try {
      await api.deleteCredential(keyId);
      credentials.value = credentials.value.filter((c) => c.key_id !== keyId);
    } catch (e) {
      error.value = (e as Error).message;
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
        <div class="sec-head">
          <h2>凭据</h2>
          <button type="button" class="qio-btn primary new-cred" @click="openCreate">＋ 新建凭据</button>
        </div>
        <p class="desc">密钥只写不读：保存后不再显示明文，仅本地写入 keyring，服务器不落盘。</p>
        <p v-if="error" class="msg err">{{ error }}</p>
        <p v-if="notice" class="msg ok">{{ notice }}</p>

        <div class="filters">
          <button
            v-for="f in FILTERS" :key="f.value" type="button"
            class="qio-btn filter" :class="{ active: credFilter === f.value }"
            @click="credFilter = f.value"
          >{{ f.label }}</button>
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

    <CredentialModal
      v-if="modal.open"
      :mode="modal.mode"
      :initial="modal.initial"
      @save="onModalSave"
      @cancel="modal.open = false"
    />

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

      <section class="sec">
        <h2>电脑操控</h2>
        <p class="desc">允许 qio 读写文件、执行命令、查看进程。分级授权，高危操作会触发审批。</p>
        <p v-if="computerNotice" class="msg ok">{{ computerNotice }}</p>
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
      </section>

      <section class="sec">
        <h2>对话深度</h2>
        <p class="desc">agent 单轮最多迭代次数与输出 token 预算；达上限时可选择继续。</p>
        <p v-if="loopNotice" class="msg ok">{{ loopNotice }}</p>
        <div class="pref">
          <div class="txt">
            <div class="t">单轮迭代上限</div>
            <div class="d">每轮任务最多重新决策的次数（1-1000）</div>
          </div>
          <div class="ctl">
            <QNumber
              class="num" :model-value="loopMaxIterations"
              :min="1" :max="1000" mono label="迭代上限"
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
              class="num" :model-value="loopTokenBudget"
              :min="0" :max="500000" mono label="输出预算"
              @update:model-value="onLoopTokensInput"
              @change="saveLoopSettings"
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

      <section class="sec">
        <h2>联网搜索</h2>
        <p class="desc">让 agent 能联网检索实时资讯并读取网页正文。未配置密钥时默认走必应/百度免密钥；填写博查密钥后可获得更稳定的结果。</p>
        <div class="pref">
          <div class="txt">
            <div class="t">默认返回条数</div>
            <div class="d">每次搜索返回的结果数（1-20）</div>
          </div>
          <div class="ctl">
            <QNumber
              class="num" :model-value="searchTopK" :min="1" :max="20" mono label="返回条数"
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
              class="num" :model-value="searchMaxChars" :min="1000" :max="40000" mono label="字符预算"
              @update:model-value="onSearchMaxCharsInput"
            />
          </div>
        </div>
        <div class="pref">
          <div class="txt">
            <div class="t">博查 API Key</div>
            <div class="d">
              {{ searchBochaHasKey ? "已配置（留空保存将清除）" : "可选，留空使用免密钥兜底" }}
            </div>
          </div>
          <div class="ctl">
            <input
              v-model="searchBochaKey"
              class="qio-input mono"
              type="password"
              :placeholder="searchBochaHasKey ? '••••••••' : '输入博查 API Key'"
              autocomplete="off"
            />
          </div>
        </div>
        <div class="pref">
          <div class="txt">
            <div class="t">SearXNG 实例 URL</div>
            <div class="d">可选，需自建或自备可达实例</div>
          </div>
          <div class="ctl">
            <input
              v-model="searchSearxngUrl"
              class="qio-input mono"
              type="text"
              placeholder="https://your-searxng.example"
              autocomplete="off"
            />
          </div>
        </div>
        <div class="pref">
          <div class="txt"></div>
          <div class="ctl">
            <button type="button" class="qio-btn" @click="saveSearchSettings">保存搜索配置</button>
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
.sec-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.sec-head h2 { margin: 0; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 14px; }
.filter { font-size: 12px; padding: 4px 12px; }
.filter.active { color: var(--text-strong); border-color: var(--accent); background: var(--accent-soft); }
.pref .ctl .qio-input {
  min-width: 240px;
  padding: 8px 12px;
  border-radius: 10px;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  color: var(--text-primary);
  font-family: var(--mono);
  font-size: 12px;
  outline: none;
}
.pref .ctl .qio-input::placeholder { color: var(--text-muted); }
.pref .ctl .qio-input:focus { border-color: var(--accent); }
</style>
