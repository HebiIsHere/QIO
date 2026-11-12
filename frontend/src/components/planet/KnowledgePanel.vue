<script setup lang="ts">
/**
 * 知识面板（B · 管理流程）。
 *
 * 信息架构：列表 → 点条目 → 详情 → 返回列表。
 *
 * 三条硬约束（改版要求，全部用真实数据与既有接口实现）：
 * 1. **列表不重建**：列表与详情用 v-show 切换，搜索/筛选/滚动位置由组件自己保存与恢复
 *    （返回后回到原来那一段，而不是跳回顶部）。A 用 v-show 保活面板；本组件即使被
 *    销毁重建，也能重新从服务端读到真实状态。
 * 2. **草稿化编辑**：点「修正」进入明确的草稿态，只有点「保存」才提交一次（CAS 带
 *    expected_version）；取消丢弃本次修改；保存失败保留草稿与可读错误。
 * 3. **冲突不静默覆盖**：编辑期间这条知识被别处改了（409 / 重新读到新版本），
 *    提示冲突、保留用户输入并把「服务端当前版本」摆出来，由用户决定刷新还是继续。
 *    返回列表时若有未保存修改，先走确认层，不悄悄丢掉。
 *
 * 基线能力一个都不少：分类/状态/来源筛选、搜索、新建、批准/打回、修订（新版本）、
 * 归档、标记结束/恢复、适用范围调整、关联话题跳转、依据数、版本与更新时间。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { getActivePinia } from "pinia";
import {
  api,
  conflictCurrentVersion,
  isVersionConflict,
  type KnowledgeItem,
  type TopicFingerprint,
} from "../../services/api";
import QInput from "../ui/QInput.vue";
import QSelect from "../ui/QSelect.vue";
import QConfirm from "../ui/QConfirm.vue";
import QIcon from "../ui/QIcon.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { vQioTip } from "../../composables/useQioTip";
import { useActionFeedback } from "../../composables/useActionFeedback";
import { usePlanetPendingStore } from "../../stores/planetPending";

const props = withDefaults(defineProps<{ focusId?: string | null }>(), { focusId: null });
const emit = defineEmits<{ "focus-topic": [topicId: string]; changed: [] }>();

const CATEGORY_LABELS: Record<string, string> = {
  user_profile: "用户画像",
  agent_self: "自我",
  goal: "目标",
  general_fact: "常识",
  tool_experience: "工具经验",
};
const STATE_LABELS: Record<string, string> = {
  draft: "草稿",
  pending_review: "待审核",
  verified: "已验证",
  active: "生效",
  expired: "过期",
  revoked: "已归档",
};

/** 状态色调统一走 .qio-state 的语义档，不各写一套徽章底色。 */
function stateTone(state: string): string {
  if (state === "active" || state === "verified") return "ok";
  if (state === "pending_review" || state === "draft") return "warn";
  return "quiet";
}

/** 事实的确定程度（epistemic_status）不是审批状态，只用来解释「这条从哪来」。 */
const EPISTEMIC_LABELS: Record<string, string> = {
  user_explicit: "用户明确",
  shared_understanding: "共同认识",
  qio_observed: "实际观测",
  qio_inference: "QIO 推测",
};
function epistemicLabel(status?: string | null): string {
  return status ? EPISTEMIC_LABELS[status] || status : "";
}

/**
 * 适用范围标签：有可点的「关联话题」链接时，不要再把「话题：X」打印一遍
 * （同一张卡里同一个名字出现两次，还会把正文挤到第二眼）。
 * 全局范围 / 实体范围 / 未指定归属仍然照实显示 —— 只有话题重复时省略。
 */
function scopeLabelOf(k: KnowledgeItem): string {
  const scope = String(k.scope || "").trim();
  if (!scope) return "";
  if (k.topic_name && scope.startsWith("话题")) return "";
  return scope;
}

/** 反馈键：同一面板里不同条目的动作结果互相隔离。 */
function fbKey(k: KnowledgeItem | null): string {
  return k ? "k:" + k.id : "k:none";
}

// -- 列表数据与筛选 ---------------------------------------------------------

const items = ref<KnowledgeItem[]>([]);
const topics = ref<TopicFingerprint[]>([]);
const q = ref("");
const category = ref("");
const state = ref("");
const source = ref("");
const loading = ref(false);
/** 读取失败原因：失败必须自己可见，不能显示成「暂无记录」 */
const loadError = ref("");
const feedback = useActionFeedback();
const creating = ref(false);
const createForm = ref({ category: "general_fact", content: "", topic_id: "" });
const confirmId = ref<string | null>(null);
/**
 * 待处理角标 store：A 负责渲染，B 负责在写入成功后刷新。
 * 单测/画廊环境没有 pinia 时跳过（与 QConfirm / QModal 的处理一致），不改变任何业务行为。
 */
const pendingStore = getActivePinia() ? usePlanetPendingStore() : null;
function refreshPendingCount() {
  void pendingStore?.refresh();
}

/** 面板级成功提示：成功后条目会刷新甚至换行，条目内提示可能跟着消失。 */
const notice = ref("");
let noticeTimer: ReturnType<typeof setTimeout> | null = null;
function announce(text: string) {
  notice.value = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.value = "";
  }, 4200);
}

const categoryOptions = [
  { value: "", label: "全部分类" },
  ...Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label })),
];
const stateOptions = [
  { value: "", label: "全部状态" },
  ...Object.entries(STATE_LABELS).map(([value, label]) => ({ value, label })),
];
const SOURCE_LABELS = ["引导", "对话", "你的修正", "后台整理", "未记录"];
const sourceOptions = [
  { value: "", label: "全部来源" },
  ...SOURCE_LABELS.map((label) => ({ value: label, label })),
];
const topicOptions = computed(() => [
  { value: "", label: "不关联话题" },
  ...topics.value.map((t) => ({ value: t.topic_id, label: t.title })),
]);

const filtered = computed(() => {
  const query = q.value.trim().toLowerCase();
  return items.value.filter((k) => {
    if (category.value && k.category !== category.value) return false;
    if (state.value && k.state !== state.value) return false;
    if (source.value && k.source !== source.value) return false;
    if (query && !k.content.toLowerCase().includes(query)) return false;
    return true;
  });
});

const hasFilters = computed(() =>
  Boolean(q.value.trim() || category.value || state.value || source.value),
);

function clearFilters() {
  q.value = "";
  category.value = "";
  state.value = "";
  source.value = "";
}

// -- 列表 ↔ 详情（v-show 保留列表，返回恢复滚动位置） -------------------------

type PanelView = "list" | "detail";
const view = ref<PanelView>("list");
const activeId = ref<string | null>(null);
const rootRef = ref<HTMLElement | null>(null);
/** 打开详情前列表的滚动位置：返回时原样恢复（不重建列表、不跳顶部）。 */
let listScrollTop = 0;

const activeItem = computed<KnowledgeItem | null>(
  () => items.value.find((k) => k.id === activeId.value) ?? null,
);

// 编辑草稿
const editing = ref(false);
const draft = ref("");
const saving = ref(false);
/** 开始编辑时看到的服务端版本（CAS 基准 + 「有没有改过」的比较基准）。 */
const baseline = ref<{ content: string; version: number | null }>({ content: "", version: null });
const saveError = ref("");
/** 冲突：保留用户输入，把服务端当前版本也摆出来，由用户决定下一步。 */
const conflict = ref<{ message: string; remoteContent: string; remoteVersion: number | null } | null>(null);
/**
 * 离开确认：列表点击 / 页面内返回 / 外部 requestOpenKnowledge 共用的**唯一**检查。
 *
 * 三条路径都先过 {@link requestNavigate}：没有未保存修改就直接跳；有就挂起本次目标，
 * 等用户明确选择（继续编辑 / 保存并打开 / 放弃并打开）。确认期间再次请求另一个目标时，
 * 只更新目标、旧的 Promise 以 false 结束 —— 最终只跳转最后一次明确选择。
 */
type NavTarget = { kind: "open"; id: string } | { kind: "back" };

const pendingNav = ref<NavTarget | null>(null);
let navResolvers: Array<(ok: boolean) => void> = [];
/** 确认层的键盘焦点管理：打开时移入，继续编辑后回到草稿输入框。 */
const guardRef = ref<HTMLElement | null>(null);
const draftAreaRef = ref<HTMLTextAreaElement | null>(null);
const detailRegionRef = ref<HTMLElement | null>(null);
const listRegionRef = ref<HTMLElement | null>(null);

const dirty = computed(
  () => editing.value && draft.value.trim() !== baseline.value.content.trim(),
);

function resetDraftState() {
  editing.value = false;
  draft.value = "";
  saveError.value = "";
  conflict.value = null;
}

function scrollRootTo(top: number) {
  const root = rootRef.value;
  if (!root) return;
  root.scrollTop = Math.max(0, top);
}

/** 结束所有挂起的离开请求：ok=true 表示导航已发生（或承诺会发生），false 表示没有跳转。 */
function settlePendingNav(ok: boolean) {
  const resolvers = navResolvers;
  navResolvers = [];
  pendingNav.value = null;
  for (const resolve of resolvers) resolve(ok);
}

/** 真正执行导航（调用前必须已经确定没有未保存修改，或用户已明确选择放弃/保存）。 */
function applyNavigation(target: NavTarget) {
  if (target.kind === "back") leaveDetail();
  else openDetailNow(target.id);
}

/**
 * 统一的离开检查：无草稿 → 直接导航并 resolve true；有草稿 → 打开同一确认层并挂起，
 * 用户明确选择后才 resolve（保存失败/继续编辑为 false）。
 */
async function requestNavigate(target: NavTarget): Promise<boolean> {
  if (target.kind === "open" && view.value === "detail" && activeId.value === target.id) {
    // 已经在这条上：不构成离开，也不需要确认。
    return true;
  }
  if (!dirty.value) {
    applyNavigation(target);
    return true;
  }
  // 确认期间又点了另一个目标：更新目标，旧请求按「未跳转」结束。
  settlePendingNav(false);
  pendingNav.value = target;
  return new Promise<boolean>((resolve) => {
    navResolvers.push(resolve);
  });
}

/** 列表点击（视图内路径）：与搜索/返回走同一检查。 */
async function openDetail(id: string) {
  await requestNavigate({ kind: "open", id });
}

function openDetailNow(id: string) {
  if (view.value === "detail" && activeId.value === id) return;
  if (view.value === "list") listScrollTop = rootRef.value?.scrollTop ?? 0;
  resetDraftState();
  activeId.value = id;
  view.value = "detail";
  confirmId.value = null;
  void nextTick(() => scrollRootTo(0));
}

function leaveDetail() {
  resetDraftState();
  confirmId.value = null;
  view.value = "list";
  activeId.value = null;
  const top = listScrollTop;
  void nextTick(() => scrollRootTo(top));
}

/** 页面内返回：与列表点击、搜索跳转共用同一离开检查。 */
async function requestBack() {
  await requestNavigate({ kind: "back" });
}

/**
 * 导航后的焦点落点：打开详情落在详情区，返回列表落回刚才那一条（键盘用户不丢位置）。
 */
async function focusAfterNavigation(target: NavTarget, previousId: string | null) {
  await nextTick();
  if (target.kind === "open") {
    detailRegionRef.value?.focus();
    return;
  }
  const root = rootRef.value;
  const item = previousId && root
    ? root.querySelector<HTMLElement>(`[data-k-id="${previousId}"]`)
    : null;
  (item ?? listRegionRef.value)?.focus();
}

// -- 离开确认层的三个明确动作 ------------------------------------------------

function resolveKeepEditing() {
  settlePendingNav(false);
  void nextTick(() => draftAreaRef.value?.focus());
}

async function resolveDiscardAndGo() {
  const target = pendingNav.value;
  if (!target) return;
  const previousId = activeId.value;
  settlePendingNav(true);
  applyNavigation(target);
  await focusAfterNavigation(target, previousId);
}

async function resolveSaveAndGo() {
  const target = pendingNav.value;
  if (!target) return;
  const previousId = activeId.value;
  if (!activeItem.value) {
    saveError.value = "这条知识已经不在当前列表里，无法保存。可以放弃修改后继续。";
    settlePendingNav(false);
    return;
  }
  const saved = await saveDraftCore();
  if (!saved) {
    // 停在原条目：草稿、编辑态与真实失败原因都还在（409 走冲突面板）。
    settlePendingNav(false);
    return;
  }
  applyNavigation(target);
  settlePendingNav(true);
  await focusAfterNavigation(target, previousId);
}

/** Esc 先退这一层确认（并阻止冒泡到 PlanetView，避免顺手收起整个管理区）。 */
function onGuardKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    resolveKeepEditing();
    return;
  }
  if (event.key !== "Tab") return;
  const root = guardRef.value;
  if (!root) return;
  const buttons = Array.from(root.querySelectorAll<HTMLElement>("button:not([disabled])"));
  if (!buttons.length) return;
  const first = buttons[0];
  const last = buttons[buttons.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (event.shiftKey && (active === first || active === root)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

/** 确认层出现时把焦点移进去（可读屏、可 Esc、Tab 在三个动作之间循环）。 */
watch(pendingNav, async (target) => {
  if (!target) return;
  await nextTick();
  guardRef.value?.focus();
});

// -- 读取 -------------------------------------------------------------------

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const r = await api.listKnowledge();
    items.value = r.knowledge;
    // 详情开着但这一行已经不在当前列表里（例如被修订成了新版本）：详情自己会说明。
    if (
      view.value === "detail" &&
      activeId.value &&
      !items.value.some((k) => k.id === activeId.value)
    ) {
      activeId.value = null;
    }
  } catch (e) {
    console.error("[knowledge] load failed:", e);
    loadError.value = "加载知识失败：" + (e as Error).message;
  } finally {
    loading.value = false;
  }
}

async function loadTopics() {
  try {
    const r = await api.listTopics();
    topics.value = r.topics;
  } catch (e) {
    console.error("[knowledge] load topics failed:", e);
  }
}

/** 成功写入后：刷新权威数据 + 让 A 的待处理角标跟着真实数量走。 */
async function afterMutation() {
  await load();
  emit("changed");
  refreshPendingCount();
}

// -- 条目动作（详情里，基线能力全部保留） ------------------------------------

async function toggleEnded(k: KnowledgeItem) {
  await feedback.run(fbKey(k), async () => {
    if (k.ended) await api.resumeKnowledge(k.id);
    else await api.endKnowledge(k.id);
    await afterMutation();
  }, {
    okText: k.ended ? "已恢复" : "已标记结束",
    failText: k.ended ? "恢复没有成功" : "标记结束没有成功",
  });
}

const scopeOptions = computed(() => [
  { value: "global", label: "全局（你）" },
  ...topics.value.map((t) => ({ value: "topic:" + t.topic_id, label: "仅话题：" + t.title })),
]);
function scopeValueOf(k: KnowledgeItem): string {
  return k.topic_id ? "topic:" + k.topic_id : "global";
}
/**
 * 适用范围只在现有下拉控件里显示一次。仅当当前值不在下拉选项里
 * （例如关联话题已经从话题列表消失）才补一行原文，避免信息丢失而不是重复打印。
 */
function scopeNeedsHint(k: KnowledgeItem | null): boolean {
  if (!k) return false;
  return !scopeOptions.value.some((option) => option.value === scopeValueOf(k));
}
async function changeScope(k: KnowledgeItem, value: string) {
  await feedback.run(fbKey(k), async () => {
    if (value.startsWith("topic:")) {
      await api.setKnowledgeScope(k.id, { type: "topic", topic_id: value.slice(6) });
    } else {
      await api.setKnowledgeScope(k.id, { type: "global" });
    }
    await afterMutation();
  }, { okText: "适用范围已更新", failText: "改适用范围没有成功" });
}

async function approve(k: KnowledgeItem) {
  await feedback.run(fbKey(k), async () => {
    await api.verifyKnowledge(k.id);
    await afterMutation();
  }, { okText: "已批准并生效", failText: "批准没有成功" });
}

async function reject(k: KnowledgeItem) {
  await feedback.run(fbKey(k), async () => {
    await api.rejectKnowledge(k.id);
    await afterMutation();
  }, { okText: "已打回草稿", failText: "打回没有成功" });
}

async function archive(k: KnowledgeItem) {
  confirmId.value = null;
  await feedback.run(fbKey(k), async () => {
    await api.revokeKnowledge(k.id);
    await afterMutation();
  }, { okText: "已归档", failText: "归档没有完成" });
}

// -- 草稿化编辑 -------------------------------------------------------------

function startEdit(k: KnowledgeItem) {
  editing.value = true;
  draft.value = k.content;
  baseline.value = {
    content: k.content,
    version: typeof k.version === "number" ? k.version : null,
  };
  saveError.value = "";
  conflict.value = null;
}

function cancelEdit() {
  settlePendingNav(false);
  editing.value = false;
  draft.value = "";
  saveError.value = "";
  conflict.value = null;
}

/** 409：这条知识已经不是用户看到的那一版（可能被别处修订成了新版本）。 */
async function onConflict(error: unknown) {
  const remoteVersion = conflictCurrentVersion(error);
  // 刷新权威数据：保留草稿，只更新「服务端现在是什么」。
  await load();
  const fresh = activeItem.value;
  baseline.value = {
    content: fresh?.content ?? baseline.value.content,
    // 用户看清楚之后再次保存是一次明确决定（不是静默覆盖）。
    version: typeof fresh?.version === "number" ? fresh.version : remoteVersion,
  };
  if (!fresh) {
    conflict.value = {
      message:
        "这条知识已经在别处被修订成新的当前版本，原来的这一条不再作为当前版本。你的修改还在下面；可以复制后返回列表打开新版本。",
      remoteContent: "",
      remoteVersion,
    };
    return;
  }
  conflict.value = {
    message:
      "这条知识在你编辑期间被更新过。你的修改还在，没有被覆盖；下面是服务端当前内容，确认后再决定是否再次保存。",
    remoteContent: fresh.content,
    remoteVersion: typeof fresh.version === "number" ? fresh.version : remoteVersion,
  };
}

/**
 * 保存草稿的唯一实现（普通「保存」与「保存并打开」共用）。
 * 返回是否真的写入了新版本；失败时草稿、编辑态与真实原因都保留。
 */
async function saveDraftCore(): Promise<boolean> {
  const item = activeItem.value;
  if (!item || saving.value) return false;
  const content = draft.value.trim();
  if (!content) {
    saveError.value = "内容不能为空。";
    return false;
  }
  saving.value = true;
  saveError.value = "";
  conflict.value = null;
  try {
    const result = await api.reviseKnowledge(item.id, content, {
      expectedVersion: baseline.value.version,
    });
    editing.value = false;
    draft.value = "";
    await afterMutation();
    // 修订会生成新的当前版本行：详情跟着走，用户不会看到「保存了但内容没变」。
    if (result?.knowledge_id && items.value.some((k) => k.id === result.knowledge_id)) {
      activeId.value = result.knowledge_id;
    }
    announce("已保存新版本。");
    return true;
  } catch (e) {
    if (isVersionConflict(e)) {
      await onConflict(e);
    } else {
      // 失败保留草稿与错误：用户改完可以直接重试。
      saveError.value = "保存没有成功：" + (e as Error).message + "。草稿还在，可以重试。";
    }
    return false;
  } finally {
    saving.value = false;
  }
}

async function saveDraft() {
  await saveDraftCore();
}

/**
 * 编辑期间权威数据变了（例如同一面板里刷新到了新版本）：
 * 提示冲突但**不覆盖**草稿 —— 用户还没保存，界面上看到的仍是他的输入。
 */
watch(
  () => activeItem.value,
  (next) => {
    if (!editing.value || !next) return;
    const version = typeof next.version === "number" ? next.version : null;
    if (
      version !== null &&
      baseline.value.version !== null &&
      version !== baseline.value.version
    ) {
      conflict.value = {
        message:
          "这条知识在服务端已经有了新的版本。你的修改还在，没有被覆盖；可以查看服务端当前内容后再决定。",
        remoteContent: next.content,
        remoteVersion: version,
      };
      baseline.value = { content: next.content, version };
    }
  },
  { deep: false },
);

// -- 新建 -------------------------------------------------------------------

function openCreate() {
  creating.value = true;
  createForm.value = { category: "general_fact", content: "", topic_id: "" };
}

async function submitCreate() {
  const content = createForm.value.content.trim();
  if (!content) return;
  await feedback.run("create", async () => {
    await api.createKnowledge({
      category: createForm.value.category,
      content,
      topic_id: createForm.value.topic_id || null,
    });
    creating.value = false;
    await afterMutation();
  }, { okText: "已创建并生效", failText: "创建没有成功" });
}

// -- 对外接口（供 ManagementArea 的搜索结果 / 外部落点调用） -----------------

/**
 * 有没有未保存的修改：调用方据此决定是否需要先切换到知识标签页
 * （面板被 v-show 保活时，确认层可能不在当前视野里）。
 */
function hasUnsavedDraft(): boolean {
  return dirty.value;
}

/**
 * 打开一条知识（外部入口：搜索结果 / 深链）。**必须**与列表点击、页面内返回
 * 走同一离开检查：
 * - 没有未保存修改：直接打开并 resolve true；
 * - 有未保存修改：显示同一个确认层，用户选择后才 resolve（继续编辑 / 保存失败为 false）。
 *
 * 确认期间又请求另一条时，只更新目标；旧的 Promise 以 false 结束。
 */
async function requestOpenKnowledge(id: string): Promise<boolean> {
  const targetId = String(id ?? "").trim();
  if (!targetId) return false;
  if (view.value === "detail" && activeId.value === targetId) return true;
  if (!items.value.some((k) => k.id === targetId)) {
    // 外部可能在首次加载完成前就要求定位：等这一次真实读取，而不是直接说「找不到」。
    if (bootstrapLoad) await bootstrapLoad;
    if (!items.value.some((k) => k.id === targetId)) await load();
    if (!items.value.some((k) => k.id === targetId)) {
      announce("这条知识暂时不在当前列表里，可能已被修订成新版本。");
      return false;
    }
  }
  return requestNavigate({ kind: "open", id: targetId });
}

defineExpose({ hasUnsavedDraft, requestOpenKnowledge });

// -- 生命周期 ---------------------------------------------------------------

let bootstrapLoad: Promise<void> | null = null;

/** 聚焦 prop（搜索结果 / 深链）也走 requestOpenKnowledge，不绕开离开检查。 */
async function openFocusId() {
  const id = String(props.focusId || "");
  if (!id) return;
  if (!items.value.some((k) => k.id === id)) return;
  await requestOpenKnowledge(id);
}

watch(
  () => props.focusId,
  (id) => {
    if (id) void openFocusId();
  },
);
watch(
  () => items.value.length,
  () => void openFocusId(),
);

async function bootstrap() {
  bootstrapLoad = Promise.all([load(), loadTopics()]).then(() => {});
  await bootstrapLoad;
  openFocusId();
  refreshPendingCount();
}

void bootstrap();

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
  // 组件销毁时不再跳转：挂起的请求以 false 结束（不让调用方永远等）。
  settlePendingNav(false);
});
</script>

<template>
  <div
    ref="rootRef"
    class="kpanel"
    v-qio-scrollbar
    data-qio-fit
    data-scroll="knowledge-list"
  >
    <!-- 列表（v-show 保活：返回时搜索、筛选与滚动位置都还在） -->
    <div v-show="view === 'list'" ref="listRegionRef" class="k-list-view" data-k-list tabindex="-1">
      <div class="k-toolbar">
        <QInput v-model="q" placeholder="搜索知识…" class="grow" data-k-search />
        <button class="qio-btn mini k-create-open" type="button" @click="openCreate">
          <QIcon name="plus" :size="12" /> 新建
        </button>
      </div>
      <div class="row filters">
        <QSelect v-model="category" :options="categoryOptions" class="grow" data-k-filter-category />
        <QSelect v-model="state" :options="stateOptions" class="grow" data-k-filter-state />
        <QSelect v-model="source" :options="sourceOptions" class="grow k-source-filter" data-k-filter-source />
      </div>

      <form v-if="creating" class="create-form" @submit.prevent="submitCreate">
        <QSelect v-model="createForm.category" :options="categoryOptions" class="k-category" />
        <QSelect v-model="createForm.topic_id" :options="topicOptions" class="k-topic" />
        <textarea v-model="createForm.content" class="qio-input k-content" placeholder="知识内容…"></textarea>
        <div class="row">
          <button class="qio-btn mini" type="button" @click="creating = false">取消</button>
          <button
            class="qio-btn mini primary k-create-submit"
            type="submit"
            :disabled="feedback.stateOf('create') === 'busy'"
          >
            {{ feedback.stateOf("create") === "busy" ? "创建中…" : "创建" }}
          </button>
        </div>
        <p v-if="feedback.stateOf('create') === 'failed'" class="k-feedback err" role="alert">
          {{ feedback.errorOf("create") }}
        </p>
      </form>

      <div v-if="loading && !items.length" class="hint">加载中…</div>
      <!-- 失败与「没有数据」必须区分：失败给原因和重试，不伪装成空集合 -->
      <div v-else-if="loadError" class="k-load-error" role="alert">
        <span>{{ loadError }}</span>
        <button class="qio-btn mini" type="button" @click="load">重试</button>
      </div>
      <ul v-else class="k-list">
        <li
          v-for="k in filtered"
          :key="k.id"
          class="k-item qio-card qio-card--quiet"
          :data-k-id="k.id"
          role="button"
          tabindex="0"
          @click="openDetail(k.id)"
          @keydown.enter.prevent="openDetail(k.id)"
          @keydown.space.prevent="openDetail(k.id)"
        >
          <div class="k-head">
            <span class="qio-tag k-cat">{{ CATEGORY_LABELS[k.category] || k.category }}</span>
            <span class="qio-state k-state" :class="[k.state, stateTone(k.state)]">
              {{ STATE_LABELS[k.state] || k.state }}
            </span>
            <span v-if="k.ended" class="qio-state k-ended quiet">已结束</span>
            <span v-if="k.epistemic_status" class="qio-tag k-epistemic">{{ epistemicLabel(k.epistemic_status) }}</span>
            <span class="k-source">{{ k.source }}</span>
            <span v-if="scopeLabelOf(k)" class="k-scope">{{ scopeLabelOf(k) }}</span>
            <!-- 关联话题：列表里也能直接跳到话题（基线能力；点它不打开详情）。
                 话题名只出现这一处（不再与 .k-scope 的「话题：X」重复），单行省略 + 悬停看全名。 -->
            <button
              v-if="k.topic_name"
              class="link k-topic-link"
              type="button"
              :title="k.topic_name"
              v-qio-tip="k.topic_name"
              @click.stop="emit('focus-topic', k.topic_id!)"
            >{{ k.topic_name }}</button>
          </div>
          <p class="k-content">{{ k.content }}</p>
        </li>
        <li v-if="!filtered.length" class="hint k-empty">
          <template v-if="items.length">没有匹配「{{ q.trim() || '当前筛选' }}」的知识记录。</template>
          <template v-else>暂无知识记录。</template>
          <button v-if="items.length" class="link k-clear-filters" type="button" @click="clearFilters">清除筛选</button>
        </li>
      </ul>
    </div>

    <!-- 详情 -->
    <div v-show="view === 'detail'" ref="detailRegionRef" class="k-detail" data-k-detail tabindex="-1">
      <div class="k-detail-head">
        <button class="qio-btn mini quiet k-back" type="button" data-k-back @click="requestBack">
          <QIcon name="chevron-left" :size="12" /> 返回
        </button>
        <span class="spacer"></span>
        <button
          v-if="activeItem && !editing"
          class="qio-btn mini k-edit-open"
          type="button"
          data-k-edit
          @click="startEdit(activeItem)"
        >修正</button>
      </div>

      <p v-if="notice" class="k-notice" role="status">{{ notice }}</p>

      <!--
        统一离开确认层（列表点击 / 页面内返回 / 外部 requestOpenKnowledge 共用）：
        标题只说一次，动作就是真实的三条路 —— 不再重复解释「返回会丢修改」。
      -->
      <div
        v-if="pendingNav"
        ref="guardRef"
        class="k-guard"
        data-unsaved-guard
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="k-guard-title"
        tabindex="-1"
        @keydown="onGuardKeydown"
      >
        <p id="k-guard-title" class="k-guard-title">还有未保存的修改</p>
        <div class="row k-guard-actions">
          <button class="qio-btn mini" type="button" data-unsaved-keep @click="resolveKeepEditing">继续编辑</button>
          <button
            class="qio-btn mini primary"
            type="button"
            data-unsaved-save-open
            :disabled="saving"
            :aria-busy="saving ? 'true' : undefined"
            @click="resolveSaveAndGo"
          >
            {{ saving ? "保存中…" : pendingNav.kind === "open" ? "保存并打开" : "保存并返回" }}
          </button>
          <button
            class="qio-btn mini danger-solid"
            type="button"
            data-unsaved-discard
            :disabled="saving"
            @click="resolveDiscardAndGo"
          >
            {{ pendingNav.kind === "open" ? "放弃并打开" : "放弃并返回" }}
          </button>
        </div>
      </div>

      <!-- 详情开着但这条已经不在当前列表（例如被合成新版本）：如实说明，并保留草稿。 -->
      <div v-if="!activeItem" class="k-card-content">
        <p class="hint k-missing">这条知识已经不在当前列表里（可能被修订成了新的当前版本，或被归档后筛掉了）。</p>
        <div v-if="editing" class="k-draft" data-k-draft>
          <label class="k-label" for="k-draft-missing">你的修改（草稿）</label>
          <textarea id="k-draft-missing" ref="draftAreaRef" v-model="draft" class="qio-input" rows="4"></textarea>
          <div class="row">
            <button class="qio-btn mini" type="button" data-k-cancel @click="cancelEdit">丢弃草稿</button>
          </div>
          <p v-if="saveError" class="k-feedback err" role="alert">{{ saveError }}</p>
        </div>
        <button class="qio-btn mini primary" type="button" @click="requestBack">返回列表</button>
      </div>

      <div v-else class="k-card qio-card">
        <div class="k-head">
          <span class="qio-tag k-cat">{{ CATEGORY_LABELS[activeItem.category] || activeItem.category }}</span>
          <span class="qio-state k-state" :class="[activeItem.state, stateTone(activeItem.state)]">
            {{ STATE_LABELS[activeItem.state] || activeItem.state }}
          </span>
          <span v-if="activeItem.ended" class="qio-state k-ended quiet">已结束</span>
          <span v-if="activeItem.epistemic_status" class="qio-tag k-epistemic">
            {{ epistemicLabel(activeItem.epistemic_status) }}
          </span>
        </div>

        <!-- 阅读态 / 草稿态：草稿是明确的一层，保存前不改服务端任何东西 -->
        <template v-if="editing">
          <div class="k-draft" data-k-draft>
            <label class="k-label" for="k-draft">内容（保存后才写入新版本）</label>
            <textarea id="k-draft" ref="draftAreaRef" v-model="draft" class="qio-input k-edit" rows="5"></textarea>
            <p class="hint k-draft-hint">
              保存会在服务端生成一个新的当前版本；取消丢弃这次修改，原内容不变。
            </p>
          </div>
          <div class="row k-actions open">
            <button class="qio-btn mini quiet k-cancel" type="button" data-k-cancel @click="cancelEdit">取消</button>
            <button
              class="qio-btn mini primary k-save"
              type="button"
              data-k-save
              :disabled="saving"
              :aria-busy="saving ? 'true' : undefined"
              @click="saveDraft"
            >
              {{ saving ? "保存中…" : "保存" }}
            </button>
          </div>
          <p v-if="saveError" class="k-feedback err" role="alert">{{ saveError }}</p>
        </template>
        <template v-else>
          <p class="k-detail-content">{{ activeItem.content }}</p>
        </template>

        <!-- 冲突：不覆盖草稿，把服务端当前内容摆出来 -->
        <div v-if="conflict" class="k-conflict" role="alert">
          <p class="k-conflict-text">{{ conflict.message }}</p>
          <div v-if="conflict.remoteContent" class="k-conflict-remote">
            <span class="k-label">服务端当前内容（v{{ conflict.remoteVersion ?? "?" }}）</span>
            <p class="k-detail-content">{{ conflict.remoteContent }}</p>
          </div>
          <div class="row">
            <button class="qio-btn mini quiet" type="button" @click="load">重新读取</button>
            <button class="qio-btn mini quiet" type="button" @click="conflict = null">知道了</button>
          </div>
        </div>

        <!-- 详情元数据：来源/分类/状态/适用范围/关联话题/依据数/修订与归档 -->
        <dl class="k-meta">
          <div class="k-meta-row">
            <dt>来源</dt>
            <dd class="k-source">{{ activeItem.source || "未记录" }}</dd>
          </div>
          <div class="k-meta-row">
            <dt>分类</dt>
            <dd>{{ CATEGORY_LABELS[activeItem.category] || activeItem.category }}</dd>
          </div>
          <div class="k-meta-row">
            <dt>状态</dt>
            <dd>{{ STATE_LABELS[activeItem.state] || activeItem.state }}{{ activeItem.ended ? " · 已结束" : "" }}</dd>
          </div>
          <div class="k-meta-row">
            <dt>适用范围</dt>
            <dd>
              <QSelect
                class="k-scope-select"
                :model-value="scopeValueOf(activeItem)"
                :options="scopeOptions"
                :busy="feedback.stateOf(fbKey(activeItem)) === 'busy'"
                @update:model-value="(v: string) => changeScope(activeItem!, v)"
              />
              <!-- 只在现有下拉控件里显示一次；仅当下拉无法表示当前值时才补原文。 -->
              <span v-if="scopeNeedsHint(activeItem)" class="hint k-scope-detail">{{ activeItem.scope }}</span>
            </dd>
          </div>
          <div class="k-meta-row">
            <dt>关联话题</dt>
            <dd>
              <button
                v-if="activeItem.topic_name"
                class="link k-topic-link"
                type="button"
                :title="activeItem.topic_name"
                v-qio-tip="activeItem.topic_name"
                @click="emit('focus-topic', activeItem.topic_id!)"
              >{{ activeItem.topic_name }}</button>
              <span v-else class="hint">未关联</span>
            </dd>
          </div>
          <div class="k-meta-row">
            <dt>依据</dt>
            <dd>
              <span v-if="activeItem.evidence_ids && activeItem.evidence_ids.length" class="k-evidence">
                {{ activeItem.evidence_ids.length }} 条真实记录
              </span>
              <span v-else class="hint">没有记录依据（多为用户手动创建）</span>
            </dd>
          </div>
          <div class="k-meta-row">
            <dt>修订</dt>
            <dd class="mono k-revise">
              v{{ activeItem.version ?? 1 }}
              <span v-if="activeItem.supersedes_id" class="hint">（由上一版修订而来）</span>
            </dd>
          </div>
        </dl>

        <!--
          用户查证需要的详情留在默认可见层（来源/分类/状态/范围/话题/依据/修订）；
          内部编号与原始时间戳放进按需展开区，默认不铺开 —— 不是删掉。
        -->
        <details class="k-more" data-k-more>
          <summary class="k-more-summary">更多信息</summary>
          <dl class="k-meta k-more-list">
            <div class="k-meta-row">
              <dt>时间</dt>
              <dd class="mono k-times">
                创建 {{ activeItem.created_at || "未记录" }} · 更新 {{ activeItem.updated_at || "未记录" }}
              </dd>
            </div>
            <div v-if="activeItem.ended_at" class="k-meta-row">
              <dt>结束于</dt>
              <dd class="mono">{{ activeItem.ended_at }}</dd>
            </div>
            <div class="k-meta-row">
              <dt>编号</dt>
              <dd class="mono k-id">{{ activeItem.id }}</dd>
            </div>
          </dl>
        </details>

        <!-- 管理动作（阅读态才出现；编辑中只留保存/取消） -->
        <div v-if="!editing" class="row k-actions open">
          <button
            v-if="activeItem.state === 'pending_review'"
            class="qio-btn mini primary k-approve"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="approve(activeItem)"
          >批准</button>
          <button
            v-if="activeItem.state === 'pending_review'"
            class="qio-btn mini k-reject"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="reject(activeItem)"
          >打回</button>
          <button
            class="qio-btn mini quiet k-toggle-ended"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="toggleEnded(activeItem)"
          >{{ activeItem.ended ? "恢复" : "标记结束" }}</button>
          <button
            v-if="activeItem.state !== 'revoked'"
            class="qio-btn mini quiet k-archive"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="confirmId = activeItem.id"
          >归档</button>
          <span v-if="feedback.stateOf(fbKey(activeItem)) === 'ok'" class="k-feedback ok" role="status">
            {{ feedback.okTextOf(fbKey(activeItem)) }}
          </span>
        </div>
        <p v-if="feedback.stateOf(fbKey(activeItem)) === 'failed'" class="k-feedback err" role="alert">
          {{ feedback.errorOf(fbKey(activeItem)) }}
        </p>

        <!-- 归档是中等风险动作：就地确认并说明真实影响（不是一句「确定吗？」） -->
        <QConfirm
          v-if="activeItem && confirmId === activeItem.id"
          :open="true"
          variant="inline"
          tone="danger"
          title="归档这条知识？"
          detail="归档后它不再参与回答，也不会被删除：之后可以在筛选「已归档」里找到它。"
          confirm-text="归档"
          @confirm="archive(activeItem)"
          @cancel="confirmId = null"
        />
      </div>

    </div>
  </div>
</template>

<style scoped>
/* 面板正文层：只有这一层纵向滚动（列表与详情共用同一个阅读区）。 */
.kpanel {
  display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px;
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: clip; overscroll-behavior: contain;
}
.k-list-view { display: flex; flex-direction: column; gap: 8px; }
/* 焦点落点用 tabindex="-1" 的区域承接（返回列表 / 打开详情后），不显示成可交互控件。 */
.k-list-view:focus, .k-detail:focus, .k-guard:focus { outline: none; }
.k-toolbar { display: flex; align-items: center; gap: 8px; }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; }
.k-create-open { display: inline-flex; align-items: center; gap: 4px; }
.grow { flex: 1; }
.filters { margin-bottom: 4px; }
.create-form { border: 1px dashed var(--accent); border-radius: var(--qio-ui-radius); padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.k-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
/* 列表卡是**列方向**的：显式 stretch，避免被全局行式类（qio-row）的
   align-items:center 变成「内容水平居中 + 缩窄」。 */
.k-item {
  display: flex; flex-direction: column; align-items: stretch; justify-content: flex-start;
  gap: 4px; width: 100%; padding: var(--sp-3); border-radius: var(--qio-ui-radius);
  cursor: pointer; text-align: left;
  transition: background var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1);
}
/* hover / 焦点表面自己写（不再依赖全局 .qio-row，但它提供的那三档视觉保留） */
.k-item:hover { background: var(--layer-hover); }
.k-item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.k-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.k-content { margin: 2px 0 0; font-size: 13.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; }
.k-source, .k-scope { font-size: 11px; color: var(--text-muted); }
/* 长话题名只占一行：省略号 + title / v-qio-tip 看全名，把主视觉留给正文 */
.k-topic-link {
  display: inline-block; max-width: 100%; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  vertical-align: bottom;
}
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
.hint { font-size: 12px; color: var(--text-muted); }
.k-empty { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.k-load-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: var(--qio-ui-radius); font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}

/* 详情 */
.k-detail { display: flex; flex-direction: column; gap: 10px; }
.k-detail-head { display: flex; align-items: center; gap: 8px; }
.k-detail-head .spacer { flex: 1; }
.k-card { display: flex; flex-direction: column; gap: 10px; padding: var(--sp-4); }
.k-card-content { display: flex; flex-direction: column; gap: 8px; }
.k-detail-content { margin: 0; font-size: 14px; line-height: 1.75; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.k-label { font-size: 11px; color: var(--text-secondary); letter-spacing: 0.03em; }
.k-draft { display: flex; flex-direction: column; gap: 6px; }
.k-edit { width: 100%; font-size: 13.5px; resize: vertical; }
.k-draft-hint { margin: 0; }
.k-actions { margin-top: 2px; }
.k-meta { display: flex; flex-direction: column; gap: 6px; margin: 0; padding: 10px 0 0; border-top: 1px solid var(--border-subtle); }
/* 「更多信息」：默认收起，展开后仍是同一套元数据排版（不新增第二套视觉）。 */
.k-more { border-top: 1px solid var(--border-subtle); padding-top: 8px; }
.k-more-summary {
  cursor: pointer; font-size: 11.5px; color: var(--text-secondary);
  letter-spacing: 0.03em; user-select: none;
}
.k-more-summary:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.k-more-list { border-top: none; padding-top: 6px; }
.k-meta-row { display: flex; gap: 10px; align-items: flex-start; }
.k-meta-row dt { flex: none; width: 64px; font-size: 11px; color: var(--text-secondary); }
.k-meta-row dd { margin: 0; min-width: 0; font-size: 12.5px; color: var(--text-primary); display: flex; flex-direction: column; gap: 4px; }
.k-meta-row dd.mono { font-family: var(--mono); font-size: 11.5px; color: var(--text-muted); overflow-wrap: anywhere; }
.k-evidence { font-size: 12px; color: var(--text-secondary); }
.k-notice { margin: 0; font-size: 12px; color: var(--text-secondary); }
.k-missing { color: var(--warning); }
.k-conflict {
  display: flex; flex-direction: column; gap: 6px; padding: 10px;
  border: 1px solid var(--warning); border-radius: var(--qio-ui-radius);
  background: var(--warning-soft); color: var(--warning);
}
.k-conflict-text { margin: 0; font-size: 12.5px; line-height: 1.65; overflow-wrap: anywhere; }
.k-conflict-remote { display: flex; flex-direction: column; gap: 4px; padding: 8px; border-radius: var(--qio-ui-radius); background: var(--bg-inset); }
.k-conflict-remote .k-detail-content { color: var(--text-primary); font-size: 13px; }
.k-guard {
  display: flex; flex-direction: column; gap: 6px; padding: 12px;
  border: 1px solid var(--border-strong); border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.k-guard-title { margin: 0; font-family: var(--serif); font-size: 14px; color: var(--text-strong); }
.k-guard-actions { justify-content: flex-end; }
.k-feedback { font-size: 11.5px; }
.k-feedback.ok { color: var(--success); }
.k-feedback.err { margin: 4px 0 0; color: var(--danger); }
.k-id { font-size: 10.5px; color: var(--text-faint); }
/* 状态底色由 .qio-state 的语义档提供，这里只保留状态名类供脚本/测试辨认 */
@media (hover: none) {
  .k-item { cursor: default; }
}
</style>
