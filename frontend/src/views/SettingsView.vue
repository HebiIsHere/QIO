<script setup lang="ts">
/**
 * 设置页：按用户心智分区（外观 / 对话与记忆 / 模型与联网 / 工具与权限 / 数据与维护 / 凭据 / 高级）。
 * 反馈按分区独立（notices），成功 / 失败 / 警告 / 信息用不同语义类，颜色全部来自令牌。
 * 服务端 tuning（SearXNG、原始阈值、图形诊断）收进「高级」，默认不打扰普通用户。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch, type Ref } from "vue";
import { api, type CredentialMeta } from "../services/api";
import QNumber from "../components/ui/QNumber.vue";
import QSelect from "../components/ui/QSelect.vue";
import { useUiStore, TYPEWRITER_SPEEDS } from "../stores/ui";
import { useOnboardingStore } from "../stores/onboarding";
import CredentialCard from "./settings/CredentialCard.vue";
import CredentialModal, { type CredentialModalMode } from "./settings/CredentialModal.vue";
import QConfirm from "../components/ui/QConfirm.vue";
import UpdateCard from "../components/UpdateCard.vue";
import { usePresence } from "../composables/usePresence";
import {
  getThemePreference,
  setThemePreference,
  type ThemePreference,
} from "../utils/theme";
import { getMotionPreference, setMotionPreference, type MotionPreference } from "../utils/motion";
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

const ui = useUiStore();
/**
 * 打开设置时恢复上次所在分类（录像：从凭据返回后再次进入会跳回外观）。
 * 记忆放在 ui store 里，属于「同次使用」范围，不落盘、不跨重启。
 */
const activeTab = ref<SectionId>(
  SECTIONS.some((s) => s.id === ui.settingsSection) ? (ui.settingsSection as SectionId) : "appearance",
);
const navRef = ref<HTMLElement | null>(null);
watch(activeTab, (v) => {
  ui.settingsSection = v;
});

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

/**
 * 保存请求的归属序号。同一分区的多次修改并发时，只有最后一次请求
 * 允许回填服务端返回值并给出反馈：否则旧响应会把用户后来改的值覆盖回去，
 * 或者用一条过期的「已保存」盖掉更新的失败。
 */
const saveSeq = new Map<string, number>();
function nextSaveSeq(key: string): number {
  const next = (saveSeq.get(key) ?? 0) + 1;
  saveSeq.set(key, next);
  return next;
}
function isLatestSave(key: string, seq: number): boolean {
  return saveSeq.get(key) === seq;
}

/**
 * 自动保存的等待态（任务 04 PART B2）：
 * 自动保存和显式保存必须能被看出来 —— 请求一发出就先显示「保存中…」，
 * 成功/失败再覆盖它。等待态是纯状态，不依赖任何动画或计时器。
 */
function beginSave(key: NoticeKey, text = "保存中…"): void {
  setNotice(key, "info", text);
}

/* ---------------- 凭据 ---------------- */
const credentials = ref<CredentialMeta[]>([]);
const credFilter = ref<"all" | "enabled" | "disabled" | "revoked" | "expired">("enabled");
const modal = ref<{
  open: boolean;
  mode: CredentialModalMode;
  initial: Record<string, unknown> | null;
}>({ open: false, mode: "create", initial: null });
/** 凭据弹窗的退出动画：关闭时先淡出再卸载，功能完成不依赖动画事件 */
const credModal = usePresence(() => modal.value.open, 150);

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
  // 不可恢复的高风险动作：用 QIO 自己的确认层（layer 档），并说清后果。
  askConfirm({
    title: `彻底删除凭据「${keyId}」？`,
    detail: "会删除密钥与这条凭据的全部记录，不可恢复。如果只是想让密钥失效、保留审计历史，请用「撤销」。",
    confirmText: "删除",
    tone: "danger",
    run: async () => {
      clearNotice("cred");
      try {
        await api.deleteCredential(keyId);
        credentials.value = credentials.value.filter((c) => c.key_id !== keyId);
      } catch (e) {
        setNotice("cred", "err", errText("删除凭据", e));
      }
    },
  });
}

/**
 * 撤销密钥：让这把密钥立即作废（从密钥库移除），但**保留**这条记录与审计历史。
 * 以前界面上没有这个入口 —— 「已撤销」筛选与「✕ 已撤销」状态都在，但用户做不到。
 * 与「删除」的区别要在确认文案里说清：删除会连记录一起清掉，不可恢复。
 */
function revoke(keyId: string) {
  askConfirm({
    title: `撤销凭据「${keyId}」？`,
    detail: "密钥会立即作废并从密钥库移除；这条记录与审计历史会保留下来，之后可以恢复或彻底删除。",
    confirmText: "撤销",
    tone: "danger",
    run: async () => {
      clearNotice("cred");
      try {
        await api.revokeCredential(keyId);
        await load();
        setNotice("cred", "ok", "已撤销：这把密钥不能再用，记录与审计历史仍保留");
      } catch (e) {
        setNotice("cred", "err", errText("撤销凭据", e));
      }
    },
  });
}

/**
 * 确认层状态（第四阶段）。
 *
 * 这里取代的是三处浏览器原生 `window.confirm`：原生框说不清「动作 + 影响 + 后果」，
 * 而且它的视觉与 QIO 毫无关系（像是另一个软件的弹窗）。危险动作统一用 layer 档，
 * 确认按钮是中性实心而不是品牌色 —— 品牌色会让危险动作看起来像被推荐的默认选择。
 */
interface PendingConfirm {
  title: string;
  detail: string;
  confirmText: string;
  tone: "normal" | "danger";
  run: () => void | Promise<void>;
}
const pendingConfirm = ref<PendingConfirm | null>(null);
function askConfirm(next: PendingConfirm) {
  pendingConfirm.value = next;
}
async function resolveConfirm() {
  const pending = pendingConfirm.value;
  pendingConfirm.value = null;
  if (pending) await pending.run();
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

/** 首次引导：重跑设置助手（向导挂在 App 层，这里只负责把它抬起来）。 */
const onboarding = useOnboardingStore();
function rerunOnboarding() {
  onboarding.reopen();
  setNotice("appearance", "ok", "设置助手已重新打开");
}
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

/* 动画偏好：跟随系统 / 标准 / 减少动画。落成 html[data-motion]，
   CSS 与星球脚本动画读同一份结果，运行中切换立即生效。 */
const motionPref = ref<MotionPreference>(getMotionPreference());
const MOTION_OPTIONS: { value: MotionPreference; label: string }[] = [
  { value: "system", label: "跟随系统" },
  { value: "standard", label: "标准" },
  { value: "reduced", label: "减少动画" },
];
function chooseMotion(p: MotionPreference) {
  motionPref.value = p;
  const mode = setMotionPreference(p);
  setNotice(
    "appearance",
    "ok",
    `动画已切换：${MOTION_OPTIONS.find((o) => o.value === p)?.label}（当前生效：${mode === "reduced" ? "减少动画" : "标准"}）`,
  );
}

const speedOptions = TYPEWRITER_SPEEDS.map((cps) => ({
  value: String(cps),
  label: cps === 25 ? "慢（25 字/秒）" : cps === 50 ? "中（50 字/秒）" : "快（75 字/秒）",
}));

function onSpeedSelect(v: string) {
  const cps = Number(v);
  if (!TYPEWRITER_SPEEDS.includes(cps as (typeof TYPEWRITER_SPEEDS)[number])) return;
  beginSave("appearance", `正在保存输出速度：${cps} 字/秒…`);
  void ui.setCps(cps).then(
    () => setNotice("appearance", "ok", `输出速度已保存：${cps} 字/秒`),
    (e: unknown) => setNotice("appearance", "err", errText("保存输出速度", e)),
  );
}

/* ---------------- 窗口行为 ---------------- */
const WINDOW_ITEMS: { id: DockId; title: string; desc: string }[] = [
  {
    id: "planet-dock",
    title: "星球入口贴边后自动隐藏",
    desc: "关闭＝入口常显（不是禁用）。开启后贴靠完成时淡化，悬停展开、移出再隐藏",
  },
  {
    id: "settings-float",
    title: "设置入口贴边后自动隐藏",
    desc: "关闭＝入口常显（不是禁用）。开启后贴角完成时淡化，悬停展开、移出再隐藏",
  },
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
/**
 * 单段长度目标（token）。长度只是**兜底**：到了就分段，但不表示这一阶段做完了
 * —— 这句话必须写在界面上，否则用户会把「分段」读成「任务完成」。
 */
const fragmentMaxTokens = ref(4096);
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
    // 字段现在是「轮」（用户 + 助手算一轮），与界面文案一致
    const saved = String(s.fragment_max_turns);
    if (tierPresets.includes(saved)) {
      fragmentTier.value = saved;
    } else {
      fragmentTier.value = "custom";
      customCount.value = s.fragment_max_turns;
    }
    if (typeof s.fragment_max_tokens === "number" && s.fragment_max_tokens > 0) {
      fragmentMaxTokens.value = s.fragment_max_tokens;
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
  const seq = nextSaveSeq("chat");
  beginSave("chat", "正在保存记忆封块…");
  try {
    const r = await api.updateMemorySettings({ fragment_max_turns: value });
    if (!isLatestSave("chat", seq)) return; // 已有更新的修改：不回填、不报成功
    setNotice("chat", "ok", `记忆封块已保存：${r.fragment_max_turns} 轮`);
    const saved = String(r.fragment_max_turns);
    if (tierPresets.includes(saved)) {
      fragmentTier.value = saved;
    } else {
      fragmentTier.value = "custom";
      customCount.value = r.fragment_max_turns;
    }
  } catch (e) {
    if (!isLatestSave("chat", seq)) return;
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

function onMaxTokensInput(v: number | null) {
  fragmentMaxTokens.value = v ?? 0;
}

async function saveMaxTokens() {
  const value = fragmentMaxTokens.value;
  clearNotice("chat");
  if (!Number.isInteger(value) || value < 2000 || value > 200000) {
    setNotice("chat", "err", "单段长度需在 2000-200000 之间");
    return;
  }
  const seq = nextSaveSeq("chat");
  beginSave("chat", "正在保存单段长度…");
  try {
    const r = await api.updateMemorySettings({ fragment_max_tokens: value });
    if (!isLatestSave("chat", seq)) return;
    fragmentMaxTokens.value = r.fragment_max_tokens;
    setNotice("chat", "ok", `单段长度已保存：${r.fragment_max_tokens}`);
  } catch (e) {
    if (!isLatestSave("chat", seq)) return;
    setNotice("chat", "err", errText("保存单段长度", e));
  }
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
  const seq = nextSaveSeq("loop");
  beginSave("loop", "正在保存对话深度…");
  try {
    const r = await api.updateLoopSettings({
      max_iterations: loopMaxIterations.value,
      output_token_budget: loopTokenBudget.value,
    });
    if (!isLatestSave("loop", seq)) return;
    loopMaxIterations.value = r.max_iterations;
    loopTokenBudget.value = r.output_token_budget;
    setNotice("loop", "ok", "对话深度已保存");
  } catch (e) {
    if (!isLatestSave("loop", seq)) return;
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

/**
 * 「不可用时解释为什么」（任务 04 PART B5）：
 * 联网搜索可能一条通道都没有 —— 这时不能只把开关摆在那里，
 * 要说清当前有没有可用通道、缺哪一步。
 */
const searchAvailability = computed(() => {
  if (searchKeyless.value) {
    return { ok: true, text: "当前可用：免密钥通道（Exa / Parallel，失败回退 DuckDuckGo）" };
  }
  if (bochaConfigured.value) {
    return { ok: true, text: "当前可用：博查 API Key（免密钥通道已关闭）" };
  }
  if (searchSearxngUrl.value.trim()) {
    return { ok: true, text: `当前可用：自建 SearXNG 实例（${searchSearxngUrl.value.trim()}）` };
  }
  return {
    ok: false,
    text: "当前没有可用的联网搜索通道：请开启免密钥搜索，或配置博查密钥 / 自建 SearXNG 实例。在此之前，联网搜索不可用。",
  };
});

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
  beginSave("model", "正在保存免密钥搜索开关…");
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
  const seq = nextSaveSeq("model");
  beginSave("model", "正在保存搜索配置…");
  try {
    const r = await api.updateSearchSettings(searchPayload());
    if (!isLatestSave("model", seq)) return;
    searchTopK.value = r.top_k_default;
    searchMaxChars.value = r.max_fetch_chars;
    searchSearxngUrl.value = r.searxng_url;
    bochaConfigured.value = r.bocha_has_key;
    bochaEditing.value = false;
    bochaKeyDraft.value = "";
    setNotice("model", "ok", "已保存搜索配置");
  } catch (e) {
    if (!isLatestSave("model", seq)) return;
    setNotice("model", "err", errText("保存搜索配置", e));
  }
}

/** 清除凭据必须是显式动作（二次确认 + 只发送该字段） */
async function clearBochaKey() {
  askConfirm({
    title: "清除已保存的博查 API Key？",
    detail: "清除后联网搜索会回退到免密钥通道（Exa / DuckDuckGo 等）。需要时可以重新填写。",
    confirmText: "清除",
    tone: "danger",
    run: async () => {
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
    },
  });
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
  beginSave("tools", "正在保存电脑操控配置…");
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

/* ---------------- 工具调用历史 ---------------- */
const toolRecordOutputs = ref(true);
const toolOutputRetentionDays = ref(90);

async function loadToolHistorySettings() {
  try {
    const s = await api.getToolHistorySettings();
    toolRecordOutputs.value = s.record_outputs;
    toolOutputRetentionDays.value = s.output_retention_days;
  } catch (e) {
    setNotice("tools", "err", errText("加载工具调用历史设置", e));
  }
}

async function saveToolHistorySettings() {
  clearNotice("tools");
  const seq = nextSaveSeq("tools");
  beginSave("tools", "正在保存工具调用历史设置…");
  try {
    const r = await api.updateToolHistorySettings({
      record_outputs: toolRecordOutputs.value,
      output_retention_days: toolOutputRetentionDays.value,
    });
    if (!isLatestSave("tools", seq)) return;
    toolRecordOutputs.value = r.record_outputs;
    toolOutputRetentionDays.value = r.output_retention_days;
    setNotice(
      "tools",
      "ok",
      r.purged
        ? `已保存；按保留期清理了 ${r.purged} 条旧输出（参数与失败原因仍在）`
        : "已保存",
    );
  } catch (e) {
    if (!isLatestSave("tools", seq)) return;
    setNotice("tools", "err", errText("保存工具调用历史设置", e));
  }
}

function toggleToolRecordOutputs() {
  toolRecordOutputs.value = !toolRecordOutputs.value;
  void saveToolHistorySettings();
}

function onToolRetentionInput(v: number | null) {
  toolOutputRetentionDays.value = v ?? 0;
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
  const seq = nextSaveSeq("data");
  beginSave("data", "正在保存维护设置…");
  try {
    const r = await api.updateMaintenanceSettings({
      enabled: maintenanceEnabled.value,
      interval_hours: maintenanceInterval.value,
    });
    // 旧响应不回填：用户可能已经在等待期间改了间隔或开关
    if (!isLatestSave("data", seq)) return;
    maintenanceEnabled.value = r.enabled;
    maintenanceInterval.value = r.interval_hours;
    setNotice(
      "data",
      "ok",
      `离线维护已保存：${r.enabled ? "开启" : "关闭"}（每 ${r.interval_hours} 小时）`,
    );
  } catch (e) {
    if (!isLatestSave("data", seq)) return;
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

/**
 * 非敏感表单草稿：离开设置页后仍然保留用户改到一半的值。
 * 敏感字段（凭据密钥、博查 Key 草稿）故意不在这个表里——它们只活在表单自身的内存状态中。
 */
const draftFields = {
  fragmentTier,
  customCount,
  loopMaxIterations,
  loopTokenBudget,
  searchTopK,
  searchMaxChars,
  searchSearxngUrl,
  searchKeyless,
  computerRootDir,
  computerPermissionMode,
  toolRecordOutputs,
  toolOutputRetentionDays,
  maintenanceEnabled,
  maintenanceInterval,
} as unknown as Record<string, Ref<unknown>>;

/**
 * 进入页面时先把上一轮的草稿快照下来：加载过程会把服务端值写进这些 ref，
 * 而 watcher 又会把 ref 写回 store —— 不快照就会在 load 阶段把自己刚读的草稿覆盖掉。
 */
const savedDraft: Record<string, unknown> = { ...ui.settingsDraft };

function applyFormDraft() {
  for (const [key, value] of Object.entries(savedDraft)) {
    if (key in draftFields) draftFields[key].value = value;
  }
}

watch(
  () => Object.fromEntries(Object.entries(draftFields).map(([k, r]) => [k, r.value])),
  (v) => {
    ui.settingsDraft = v;
  },
);

onMounted(async () => {
  ui.settingsSection = activeTab.value;
  void nextTick(() => {
    if (navRef.value) navRef.value.scrollTop = ui.settingsNavScroll;
  });
  await Promise.all([
    load(),
    loadMemorySettings(),
    loadMaintenanceSettings(),
    loadSearchSettings(),
    loadComputerSettings(),
    loadToolHistorySettings(),
    loadLoopSettings(),
    ui.load(),
  ]);
  // 服务端值先落地，再覆盖上用户尚未提交的草稿（草稿优先，但不会凭空产生值）
  applyFormDraft();
});

onUnmounted(() => {
  // 记住分类列表的滚动位置：分类多、窗口小时，回来不该又滚到顶部
  if (navRef.value) ui.settingsNavScroll = navRef.value.scrollTop;
  ui.settingsSection = activeTab.value;
});

/**
 * 切分类时只让右侧内容做一次很短的淡入（左侧分类栏与页头不动）。
 * 先移除类、下一帧再加回来，这样同一个 CSS 动画才会重新播放；
 * 面板本身用 v-show 保留，未提交的草稿不会被重建掉。
 */
const panelIn = ref(false);
watch(activeTab, async () => {
  panelIn.value = false;
  await nextTick();
  requestAnimationFrame(() => {
    panelIn.value = true;
  });
});
</script>

<template>
  <div class="settings">
    <!-- 危险动作的确认层（取代原生 confirm）：layer 档 + 中性实心确认按钮 -->
    <QConfirm
      :open="!!pendingConfirm"
      variant="layer"
      :tone="pendingConfirm?.tone ?? 'danger'"
      :title="pendingConfirm?.title ?? ''"
      :detail="pendingConfirm?.detail ?? ''"
      :confirm-text="pendingConfirm?.confirmText ?? '确定'"
      cancel-text="取消"
      title-id="settings-confirm-title"
      @confirm="resolveConfirm"
      @cancel="pendingConfirm = null"
    />
    <header>
      <h1>设置</h1>
      <span class="sub mono">设置 · QIO</span>
      <span class="spacer"></span>
      <router-link to="/" class="back">← 返回对话</router-link>
    </header>

    <div class="layout">
      <nav ref="navRef" class="nav" role="tablist" aria-label="设置分区">
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

      <div class="panels" :class="{ 'panel-in': panelIn }">
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
            <h2>首次引导</h2>
            <p class="desc">
              重新走一遍设置助手：连接模型、认识你、偏好与目标。已设置项会预填，
              重复提交不会产生重复的记忆、实体卡或话题。
            </p>
            <div class="pref">
              <div class="txt">
                <div class="t">设置助手</div>
                <div class="d">随时可以重跑，完成后回到对话页</div>
              </div>
              <div class="ctl">
                <button class="qio-btn" type="button" @click="rerunOnboarding">重新运行设置助手</button>
              </div>
            </div>
          </section>

          <section class="sec">
            <h2>动画</h2>
            <p class="desc">
              跟随系统，或固定标准 / 减少动画。减少动画会同时作用于页面过渡与星球镜头，
              切换立即生效，不需要刷新。
            </p>
            <div class="seg" role="group" aria-label="动画偏好">
              <button
                v-for="o in MOTION_OPTIONS"
                :key="o.value"
                type="button"
                class="motion-opt"
                :class="{ on: motionPref === o.value }"
                :aria-pressed="motionPref === o.value"
                @click="chooseMotion(o.value)"
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
              下面两个开关只控制「贴边后要不要自动隐藏」；关闭不等于禁用入口。
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
                  :aria-label="item.title"
                  @click="toggleWindowHide(item.id)"
                ></button>
                <span class="mono">贴边后自动隐藏：{{ floatingState[item.id].hideEnabled ? "开" : "关" }}</span>
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
            <p class="desc">
              根据讨论的进展分段：同一阶段可以跨多段，进入新的一段不等于上一段的任务已经完成。
              下面两个值控制单段规模（轮数与长度），达到任一上限就在**完整的一轮之后**分段。
            </p>
            <p class="mode-hint mono">选择后自动保存</p>
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
            <div class="pref">
              <div class="txt">
                <div class="t">单段长度目标</div>
                <div class="d">
                  内容长度的兜底上限（2000-200000，按估算字符数计）。到点会分段，
                  但不表示这一阶段的任务已经做完。
                </div>
              </div>
              <div class="ctl">
                <QNumber
                  class="num"
                  :model-value="fragmentMaxTokens"
                  :min="2000"
                  :max="200000"
                  :step="1000"
                  mono
                  label="单段长度目标"
                  @update:model-value="onMaxTokensInput"
                  @change="saveMaxTokens"
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
            <p class="mode-hint mono">修改后自动保存（失焦或点步进按钮即提交）</p>
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
            <p class="mode-hint mono">开关即时保存；数字参数在点「保存搜索配置」后一起生效</p>
            <p class="avail" :class="{ off: !searchAvailability.ok }">
              {{ searchAvailability.text }}
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
                <div class="t">保存搜索配置</div>
                <div class="d">
                  返回条数、读正文字符预算、SearXNG 地址一起提交；SearXNG 实例地址在「高级」中填写
                </div>
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
            <p class="mode-hint mono">修改后自动保存</p>
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

          <section class="sec">
            <h2>工具调用历史</h2>
            <p class="desc">
              保存每次工具调用的参数与输出，供你刷新或重开应用后回看（模型不会读到这些内容）。
            </p>
            <p class="mode-hint mono">修改后自动保存</p>
            <div class="pref">
              <div class="txt">
                <div class="t">保存输出全文</div>
                <div class="d">关闭后仍保留参数、状态、失败原因与耗时，只是没有完整输出</div>
              </div>
              <div class="ctl">
                <button
                  type="button"
                  class="qio-switch"
                  :class="{ on: toolRecordOutputs }"
                  role="switch"
                  :aria-checked="toolRecordOutputs"
                  aria-label="保存工具输出全文开关"
                  @click="toggleToolRecordOutputs"
                ></button>
              </div>
            </div>
            <div class="pref">
              <div class="txt">
                <div class="t">输出保留天数</div>
                <div class="d">
                  到期只清空输出全文，参数与失败原因继续保留；填 0 表示永久保留
                </div>
              </div>
              <div class="ctl">
                <QNumber
                  class="num"
                  :model-value="toolOutputRetentionDays"
                  :min="0"
                  :max="3650"
                  mono
                  label="输出保留（天）"
                  unit="天"
                  @update:model-value="onToolRetentionInput"
                  @change="saveToolHistorySettings"
                />
              </div>
            </div>
          </section>
        </div>

        <!-- 数据与维护 -->
        <div v-show="activeTab === 'data'" class="panel">
          <section class="sec">
            <h2>更新</h2>
            <p class="desc">在应用内完成检查、下载与安装；下载后由你决定何时重启。</p>
            <UpdateCard />
          </section>
          <section class="sec">
            <h2>维护</h2>
            <p class="desc">离线整理记忆与知识（后台执行）。</p>
            <p class="mode-hint mono">修改后自动保存</p>
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
                  unit="小时"
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
              @revoke="revoke(c.key_id)"
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
      v-if="credModal.mounted.value"
      :mode="modal.mode"
      :initial="modal.initial"
      :leaving="credModal.leaving.value"
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
  /* 从对话页进入设置：220ms 淡入 + 2px 上移，不推迟内容（内容同帧可见） */
  animation: settings-in var(--dur-page-in) var(--ease-out) both;
}
@keyframes settings-in {
  from { opacity: 0; }
  to { opacity: 1; }
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
/* 切分类：只对右侧内容做一次很短的淡入，整页与左栏保持不动（任务 04 A） */
.panels.panel-in { animation: panel-in var(--dur-menu) var(--ease-out) both; }
@keyframes panel-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
@media (max-width: 860px) {
  .layout { flex-direction: column; gap: 12px; }
  .nav { flex-direction: row; flex-wrap: wrap; min-width: 0; position: static; gap: 6px; }
  .tab { border-left: none; border-bottom: 2px solid transparent; border-radius: var(--r-sm); }
  .tab.active { border-bottom-color: var(--accent); }
}

.sec { margin-bottom: 26px; }
.sec h2 { font-family: var(--serif); font-size: 16px; font-weight: 600; color: var(--text-strong); margin-bottom: 4px; }
.desc { font-size: 12.5px; color: var(--text-muted); margin-bottom: 12px; line-height: 1.65; }
/* 保存模型与可用性提示：让「自动保存 / 显式保存」「能不能用」一眼可见 */
.mode-hint {
  font-size: 11px; letter-spacing: 0.02em; color: var(--text-muted);
  margin: -6px 0 12px; padding-left: 9px; border-left: 2px solid var(--border-strong);
}
.avail {
  font-size: 12px; line-height: 1.6; color: var(--text-secondary);
  margin: -4px 0 12px; padding: 7px 10px;
  border: 1px solid var(--border-subtle); border-radius: var(--r-sm); background: var(--bg-inset);
}
.avail.off { color: var(--warning); border-color: var(--warning); background: var(--warning-soft); }

/* 反馈：成功 / 失败 / 警告 / 信息 语义独立，颜色全部来自令牌 */
.msg { font-size: 12.5px; margin: 10px 0; line-height: 1.55; }
.msg.ok { color: var(--success); }
.msg.err { color: var(--danger); }
.msg.warn { color: var(--warning); }
.msg.info { color: var(--text-secondary); }
.empty { font-size: 12px; color: var(--text-muted); }

/* 主题分段控件 */
.seg { display: inline-flex; gap: 4px; padding: 3px; border: 1px solid var(--border-subtle); border-radius: var(--r-md); background: var(--bg-inset); }
.theme-opt,
.motion-opt {
  font-family: var(--sans); font-size: 12.5px; padding: 6px 16px; border-radius: var(--r-sm);
  border: none; background: transparent; color: var(--text-secondary); cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease),
    transform var(--dur-press) var(--ease-out);
}
.theme-opt:hover,
.motion-opt:hover { color: var(--text-strong); }
.theme-opt:active,
.motion-opt:active { transform: translateY(var(--press-shift)); }
/* 选中用底色 + 字重 + 下划线三重区分，不只靠颜色 */
.theme-opt.on,
.motion-opt.on {
  background: var(--accent-soft); color: var(--text-strong); font-weight: 600;
  box-shadow: inset 0 -2px 0 var(--accent);
}

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
