<script setup lang="ts">
/**
 * 星球页（全屏覆盖层）：3D 球体（左/中）+ 右侧面板（列表/详情/从这里开始）。
 * usePlanetScene 在本页内实例化渲染全屏星球（悬浮球为 SVG 微缩星球，不共享同一
 * WebGL 场景）；相机状态机 overview|planet|focus 语义与悬浮球保持一致：
 * - 打开（悬浮球点击）：场景推进到 planet（有锚点时直接聚焦话题）；
 * - 关闭（✕/Esc/从这里开始）：相机先拉回 overview（悬浮球位置/朝向）再收起覆盖层，
 *   拉回窗口内交互一律禁用。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { usePlanetScene } from "../composables/usePlanetScene";
import { api, type PlanetTopicSummary, type TopicDetail, type TopicFingerprint } from "../services/api";
import { PlanetBrowseSession, VISIBLE_CAPACITY } from "../planet/browseSession";
import { useSessionStore } from "../stores/session";
import QInput from "../components/ui/QInput.vue";
import KnowledgePanel from "../components/planet/KnowledgePanel.vue";
import EntityPanel from "../components/planet/EntityPanel.vue";
import { useUiStore } from "../stores/ui";
import { prefersReducedMotion } from "../utils/motion";
import { anchorSignatureOf, planetSession } from "../composables/planetSession";

/**
 * 展开/收起的时间线（演示版，比任务02 的表格更慢一点，用户反馈原时长"过快"）：
 * 展开：先铺底 140ms → 内容淡入与相机小幅收敛同时进行 420ms（不再从小球拉近）
 * 收起：收势 180ms（轻微后撤 + 星球变淡、面板退场）→ 整体淡出 280ms → 卸载
 */
/**
 * 演示开关：`?planetdemo=slow` 把这条时间线整体放慢 3 倍，
 * 方便逐帧观察「铺底 → 内容淡入 → 收势 → 淡出」的先后顺序。
 * 正常访问不带参数，走下面的真实时长。
 */
const slowDemo = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("planetdemo") === "slow";
const SPEED = slowDemo ? 3 : 1;
const OPEN_MS = 420 * SPEED;
const SETTLE_MS = 180 * SPEED;
const FADE_MS = 280 * SPEED;
const demoStyle = slowDemo
  ? ({
      "--dur-planet-backdrop": `${140 * SPEED}ms`,
      "--dur-planet-in": `${420 * SPEED}ms`,
      "--dur-planet-settle": `${180 * SPEED}ms`,
      "--dur-planet-out": `${280 * SPEED}ms`,
    } as Record<string, string>)
  : undefined;

/**
 * `seq` 是父级给的「这次打开」的序号：关闭回调把它带回去，
 * 父级只认当前序号 —— 关闭动画期间用户又打开一次时，迟到的旧回调不会关掉新层。
 */
const props = withDefaults(defineProps<{ seq?: number; open?: boolean }>(), { open: true });
const emit = defineEmits<{ close: [seq?: number] }>();
const session = useSessionStore();
const ui = useUiStore();

const canvasRef = ref<HTMLCanvasElement | null>(null);
const planet = usePlanetScene(canvasRef);
// 场景返回的是 ref 容器对象（普通对象里的 ref 在模板中不会自动解包），
// 这里显式取值：否则 HUD 会渲染出 ref 对象本身。
const webglOK = computed(() => planet.webglOK.value);
const fpsText = computed(() => planet.fps.value);
const cameraStateText = computed(() => planet.cameraState.value);
/** 指针指向的话题点（场景 raycast 结果）与它的简短名称 */
const hoverTopicId = computed(() => planet.hoverTopicId?.value ?? null);
const hoverLabel = computed(() => planet.hoverLabel?.value ?? null);
/** 悬停标签的定位样式（在脚本里算，模板里不写嵌套模板字符串） */
const hoverLabelStyle = computed(() =>
  hoverLabel.value ? { left: hoverLabel.value.x + "px", top: hoverLabel.value.y + "px" } : undefined,
);
/** 选中话题的固定名称标签（悬停标签优先显示，避免两个标签叠在一起） */
const selectedLabel = computed(() => planet.selectedLabel?.value ?? null);
const selectedLabelStyle = computed(() =>
  selectedLabel.value ? { left: selectedLabel.value.x + "px", top: selectedLabel.value.y + "px" } : undefined,
);
const topics = ref<TopicFingerprint[]>([]);
/**
 * 浏览会话：序列 / 游标 / 展示窗口（见 planet/browseSession.ts）。
 * 星球上「同时显示哪些话题」由它决定，渲染层只负责画；总话题数无上限，
 * 同屏数量固定 ≤ VISIBLE_CAPACITY（融合环 shader 的 uniform 上限是 16）。
 */
const browse = new PlanetBrowseSession({ capacity: VISIBLE_CAPACITY });
/** 当前展示窗口里的 topic_id（用于断言 / 调试，也用于判断话题是否已经可见） */
const windowTopicIds = ref<(string | null)[]>([]);
/** 后端浏览游标：只有它知道「下一批」是什么、以及是否已经绕完一圈 */
let nextCursor: string | null = null;
let sessionSeed: number | null = null;
let prefetching = false;
const detail = ref<TopicDetail | null>(null);
const detailLoading = ref(false);
/** 详情加载失败原因。与「从这里继续」的 anchorError 分开：两者来源不同，不能互相覆盖 */
const detailError = ref("");
/** 详情请求序号：只接受「最后一次点击」的结果，慢请求返回不得覆盖（竞态防护） */
let detailSeq = 0;
/** 收起动作的代号：期间被重新打开时，旧收尾的续行必须放弃写状态与 emit */
let closeEpoch = 0;
const search = ref("");
const selectedFragmentId = ref<string | null>(null);
/** 关闭动画进行中（相机拉回 overview），防止重复关闭/重复交互 */
const closing = ref(false);
/** 收起第一段「收势」进行中（还没开始整体淡出） */
const settling = ref(false);
/** 「正在收起」= 收势或淡出任一阶段：这段时间里画布/列表/起点等交互都要锁住 */
const isClosing = computed(() => closing.value || settling.value);
/** 入场：铺底之后内容层淡入（用类切换而不是 keyframes，见样式注释） */
const entered = ref(false);
/** 首次数据加载失败原因（话题列表/位置）——不能只留一个空球让用户猜 */
const loadError = ref("");
/** 首次数据仍在加载：长等待要有明确提示，不以空球冒充完整内容 */
const dataLoading = ref(false);
/** 右侧话题边栏：默认收起；点击话题点/展开按钮展开，点击收起按钮收起 */
const panelOpen = ref(false);
/** 面板页签：话题 / 知识 / 实体；知识/实体页签自动进入管理模式（面板 640px 宽） */
const activeTab = ref<"topic" | "knowledge" | "entity">("topic");
const manageMode = ref(false);
/** 实体页签挂载后按 node_id 自动打开的实体卡 */
const entityOpenId = ref("");
/** 画布尺寸观察器：边栏开合/窗口缩放时同步 WebGL 渲染尺寸 */
let canvasObserver: ResizeObserver | null = null;
/** 边栏开合导致画布中心偏移后的重对焦定时器 */
let recenterTimer = 0;

/* ---- 主题同步：utils/theme.ts 写 html[data-theme]，planet.setTheme 换 WebGL 配色 ---- */
/** 读取当前主题：tokens.css 依据 data-theme 切变量，缺省按 dark */
function readTheme(): "dark" | "light" {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

/** 场景是否已初始化（init 之后才有 scene/材质可换色，observer 回调前守卫） */
let planetReady = false;
let themeObserver: MutationObserver | null = null;

function applyPlanetTheme() {
  if (!planetReady) return;
  planet.setTheme(readTheme());
}

function startThemeObserver() {
  if (themeObserver) return;
  themeObserver = new MutationObserver(applyPlanetTheme);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
}

function stopThemeObserver() {
  themeObserver?.disconnect();
  themeObserver = null;
}

/* ---- 右侧话题边栏：画布尺寸同步 + 开合后重对焦 ---- */
function startCanvasObserver() {
  if (typeof ResizeObserver === "undefined" || !canvasRef.value) return;
  canvasObserver = new ResizeObserver(() => planet.resize());
  canvasObserver.observe(canvasRef.value);
}

function stopCanvasObserver() {
  canvasObserver?.disconnect();
  canvasObserver = null;
}

/** 展开/收起右侧边栏（收起为细边 + 展开钮，展开为 340px 面板）。 */
function togglePanel() {
  if (isClosing.value) return;
  panelOpen.value = !panelOpen.value;
  // 画布宽度随边栏动画变化，结束后若仍有焦点话题，重对焦到新画布中心
  const focused = planet.selectedTopicId.value;
  if (focused) scheduleRecenter(focused);
}

/**
 * 边栏开合后把焦点话题重新居中到新画布中心。
 * 旧规则是「等 380ms 动画结束再补一次移动」，用户会看到明显两次位移；
 * 现在改成与 320ms 宽度过渡同时发生的短补间，整个过程只有一次连续移动。
 */
function scheduleRecenter(topicId: string, delay = 0) {
  window.clearTimeout(recenterTimer);
  recenterTimer = window.setTimeout(() => {
    if (isClosing.value) return;
    if (planet.selectedTopicId.value !== topicId) return;
    planet.focusTopic(topicId, [], { duration: 210, wave: false });
  }, delay);
}

/** 面板宽度变化（管理模式开合/页签切换/实体联动）后，把焦点话题重对焦到新画布中心。 */
function onPanelWidthChange() {
  const focused = planet.selectedTopicId.value;
  if (focused) scheduleRecenter(focused);
}

onMounted(async () => {
  window.addEventListener("keydown", onKeydown);
  planet.init();
  // 一开始就接近最终构图：不再出现「远景小球 → 明显放大」这一段
  planet.primeCamera("planet");
  planetReady = true;
  applyPlanetTheme();
  startThemeObserver();
  startCanvasObserver();
  // 下一帧才切到 entered，让「铺底 → 内容淡入」这两步真的有先后
  requestAnimationFrame(() => {
    entered.value = true;
  });
  await loadData();
});

/**
 * 复用同一个星球实例：关闭时不卸载（省掉每次重建 WebGL 场景的 1.5–3.4s 冷启动），
 * 由父级用 v-show 隐藏，`open` 变化时在这里重置状态。
 */
watch(
  // 同时盯 open 与 seq：收起动画中再次点入口时 open 一直是 true，
  // 只盯 open 会漏掉「打开状态下又打开一次」，界面就会停在上一次的收起状态里。
  () => [props.open, props.seq] as const,
  async ([isOpen], prev) => {
    if (!isOpen) {
      planet.setPaused(true); // 隐藏时不再绘帧
      return;
    }
    if (prev && prev[0] === isOpen && prev[1] === props.seq) return;
    await reopen();
  },
);

/** 再次打开：重置上一次的收尾状态，重新入场、重新按落点定位 */
async function reopen() {
  // 作废「进行中的收起」：它的续行还会写 closing 并 emit，必须让它认到已被取代
  closeEpoch += 1;
  closing.value = false;
  settling.value = false;
  entered.value = false;
  anchorError.value = "";
  detailError.value = "";
  anchorBusy.value = false;
  planet.setPaused(false);
  planet.primeCamera("planet"); // 回到接近最终构图，避免从上一帧残留位置开始
  await nextTick();
  planet.resize(true); // 隐藏期间绘图缓冲可能被丢弃：强制重建一次
  requestAnimationFrame(() => {
    entered.value = true;
  });
  await loadData();
}

onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
  stopThemeObserver();
  stopCanvasObserver();
  window.clearTimeout(recenterTimer);
});

function onKeydown(e: KeyboardEvent) {
  if (!props.open) return; // 已关闭但实例常驻：不能再响应 Esc
  if (e.key !== "Escape") return;
  const t = e.target as HTMLElement | null;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  // 最上层优先：边栏是用户刚打开的一层，Esc 先收它，再按才收起整个星球
  if (panelOpen.value) {
    panelOpen.value = false;
    return;
  }
  close();
}

async function loadData() {
  loadError.value = "";
  dataLoading.value = true;
  try {
    // 侧栏列表仍然走准确导航（List/Search 的职责），星球本体只取轻量概览 + 第一批窗口。
    const [t, overview] = await Promise.all([api.listTopics(), api.planetOverview()]);
    // 等待期间用户已发起关闭：不再推进相机/加载详情，避免打断拉回 tween
    if (isClosing.value) return;
    topics.value = t.topics;
    const page = await api.planetBrowse({
      direction: "forward",
      count: VISIBLE_CAPACITY,
      current_topic_id: session.currentTopicId ?? null,
    });
    if (isClosing.value) return;
    nextCursor = page.next_cursor;
    sessionSeed = page.seed;
    browse.setSequence(page.items);
    browse.fill(performance.now());
    // 起点话题必须能被看见：后端已经把它排在第一批首位；
    // 万一它不在这一批（话题刚被创建/被过滤），这里显式注入窗口。
    const anchorTopic = pickTopicSummary(overview.topics, session.currentTopicId);
    if (anchorTopic && !browse.windowSlots().some((item) => item?.topic_id === anchorTopic.topic_id)) {
      browse.pinTopic(anchorTopic);
    }
    planet.attachBrowse(browse, { seed: page.seed, onWindowChange: syncWindow });
    planet.setTopics([]);
    syncWindow();
    focusInitialTopic();
  } catch (e) {
    if (isClosing.value) return;
    loadError.value = `星球数据加载失败：${(e as Error).message}`;
  } finally {
    dataLoading.value = false;
  }
}

function pickTopicSummary(
  list: PlanetTopicSummary[],
  topicId: string | null | undefined,
): PlanetTopicSummary | null {
  if (!topicId) return null;
  return list.find((item) => item.topic_id === topicId) ?? null;
}

/**
 * 展示窗口变化后：同步可见话题、预取下一页、把选中态锁在窗口里。
 * 视觉连续性优先于严格数据顺序——这里的顺序不代表任何「相关性」。
 */
function syncWindow() {
  windowTopicIds.value = planet.windowTopicIds();
  if (!prefetching && browse.needsMore() && nextCursor) void prefetchMore();
}

async function prefetchMore() {
  if (prefetching || !nextCursor) return;
  prefetching = true;
  try {
    const page = await api.planetBrowse({
      cursor: nextCursor,
      direction: "forward",
      count: VISIBLE_CAPACITY,
      exclude: browse.windowSlots().filter(Boolean).map((item) => item!.topic_id),
      seed: sessionSeed,
    });
    nextCursor = page.next_cursor;
    browse.appendSequence(page.items);
  } catch (e) {
    // 预取失败不影响已经看到的窗口：下一次滚动会再试
    console.warn("[planet] 预取下一批话题失败：", e);
  } finally {
    prefetching = false;
  }
}

/**
 * 打开时的落点：起点没变就回到上次浏览的话题；起点明确变化则以起点为准。
 * 有落点时一次到位（相机已按最终构图预备，只做朝向收敛），不再「先总览再聚焦」。
 */
function focusInitialTopic() {
  const anchorId = session.currentTopicId ?? null;
  const signature = anchorSignatureOf(anchorId, session.anchorFragmentId);
  const anchorChanged = planetSession.anchorSignature !== signature;
  planetSession.anchorSignature = signature;

  const browsed = planetSession.browsedTopicId;
  const has = (id: string | null) => Boolean(id) && browse.windowSlots().some((t) => t?.topic_id === id);
  const target = !anchorChanged && has(browsed) ? browsed : anchorId;

  if (has(target)) {
    planet.selectedTopicId.value = target;
    browse.lock(target);
    planet.focusTopic(target!, [], { duration: OPEN_MS });
  } else {
    planet.go("planet", null, OPEN_MS);
  }
}

const filteredTopics = ref<TopicFingerprint[]>([]);
watch([topics, search], () => {
  const q = search.value.trim().toLowerCase();
  filteredTopics.value = q
    ? topics.value.filter((t) => t.title.toLowerCase().includes(q) || t.keywords.some((k) => k.toLowerCase().includes(q)))
    : topics.value;
}, { immediate: true });

async function loadDetail(topicId: string) {
  const seq = ++detailSeq;
  detailLoading.value = true;
  detailError.value = "";
  selectedFragmentId.value = null;
  try {
    const d = await api.getTopicDetail(topicId);
    // 期间用户已切到别的话题：丢弃过期响应
    if (seq !== detailSeq || planet.selectedTopicId.value !== topicId) return;
    detail.value = d;
  } catch (e) {
    if (seq === detailSeq) {
      detail.value = null;
      // 失败必须自己可见：此时没有详情可展示，错误不能挂在「有详情」的分支里
      detailError.value = `加载话题详情失败：${(e as Error).message}`;
    }
  } finally {
    if (seq === detailSeq) detailLoading.value = false;
  }
  await revealDetailArea();
}

/**
 * 话题列表很长时，详情（或它的错误）会落在列表下方看不见的位置，
 * 用户点完话题只看到行高亮、不知道结果。这里只把详情区域带回视野：
 * 已经在视野内时 `block:"nearest"` 不会移动，符合「页面变化尽量少」。
 */
async function revealDetailArea() {
  await nextTick();
  const el = document.querySelector<HTMLElement>(".detail, .detail-error, .detail-loading");
  if (!el || typeof el.scrollIntoView !== "function") return;
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  el.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
}

/** 详情加载失败后的重试：重新按当前选中话题拉一次 */
function retryDetail() {
  const id = planet.selectedTopicId.value;
  if (id) void loadDetail(id);
}

function selectTopic(topicId: string) {
  if (isClosing.value) return;
  planet.selectedTopicId.value = topicId;
  ensureTopicInWindow(topicId);
  planet.focusTopic(topicId, []);
}

/**
 * 保证某个话题在展示窗口里（搜索命中 / 列表点击）。
 * 这正是 spec 第 24 条：搜索找到的话题「加入当前/下一组展示窗口」，
 * 而不是要求它本来就待在某个固定地点。
 */
function ensureTopicInWindow(topicId: string) {
  if (browse.windowSlots().some((item) => item?.topic_id === topicId)) return;
  const summary = topics.value.find((item) => item.topic_id === topicId);
  if (!summary) return;
  browse.pinTopic({
    topic_id: summary.topic_id,
    title: summary.title,
    fragment_count: summary.fragment_count,
    last_activity: summary.last_activity,
    summary_preview: summary.summary_preview,
  });
  planet.refreshWindow(performance.now());
  syncWindow();
}

function onCanvasClick(e: MouseEvent) {
  if (isClosing.value) return;
  const focused = planet.handleClick(e.clientX, e.clientY);
  if (focused) {
    // 收起态点击话题点：同时展开边栏，画布中心左移 → 动画结束后重对焦
    const wasCollapsed = !panelOpen.value;
    panelOpen.value = true;
    if (wasCollapsed) scheduleRecenter(planet.selectedTopicId.value ?? "");
  }
}

function onCanvasDblClick() {
  if (!isClosing.value) planet.go("planet");
}

watch(() => planet.selectedTopicId.value, (id) => {
  // 浏览记忆：重开星球时用它回到上次看的地方（见 planetSession 注释）
  if (id) planetSession.browsedTopicId = id;
  // 选中 = 正在查看：查看期间这个槽位不会被回收（spec 第 52 条）
  browse.lock(id ?? null);
  if (id && id !== detail.value?.topic_id) loadDetail(id);
});

// 知识纠错：修正/删除
const knowledgeEditingId = ref<string | null>(null);
const knowledgeDraft = ref("");

function startEditKnowledge(k: { id: string; content: string }) {
  knowledgeEditingId.value = k.id;
  knowledgeDraft.value = k.content;
}

async function saveKnowledgeEdit(k: { id: string }) {
  const content = knowledgeDraft.value.trim();
  if (!content) return;
  try {
    await api.reviseKnowledge(k.id, content);
    knowledgeEditingId.value = null;
    if (detail.value) await loadDetail(detail.value.topic_id);
  } catch (e) {
    console.error("[planet] revise knowledge failed:", e);
  }
}

async function deleteKnowledge(k: { id: string; content: string }) {
  if (!window.confirm(`删除知识条目？\n${k.content.slice(0, 60)}`)) return;
  try {
    await api.revokeKnowledge(k.id);
    if (detail.value) await loadDetail(detail.value.topic_id);
  } catch (e) {
    console.error("[planet] revoke knowledge failed:", e);
  }
}

/** 锚点设置进行中 / 失败原因（失败必须可见、可重试，且不得假装已切换） */
const anchorBusy = ref(false);
const anchorError = ref("");

/** 用户当前选中的历史片段（用于「已选择历史位置」提示，不暴露内部 id） */
const selectedFragmentSummary = computed(() => {
  if (!selectedFragmentId.value) return "";
  const frag = detail.value?.fragments.find((f) => f.fragment_id === selectedFragmentId.value);
  const text = (frag?.summary || "").trim().replace(/\s+/g, " ");
  if (!text) return "（该片段暂无摘要）";
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
});

/**
 * 选中的是不是「历史位置」：已封块的片段才是历史；当前开放片段是对话的最新位置，
 * 不该显示成「从这里继续（历史位置）」（与后端 AnchorService.focus_fragment 同一规则）。
 */
const selectedIsHistoric = computed(() => {
  if (!selectedFragmentId.value) return false;
  const frag = detail.value?.fragments.find((f) => f.fragment_id === selectedFragmentId.value);
  return Boolean(frag?.closed_at);
});
const selectedPositionLabel = computed(() =>
  selectedIsHistoric.value ? "已选择历史位置" : "已选择当前片段",
);
const selectedButtonHint = computed(() => {
  if (!selectedFragmentId.value) return "（整个话题）";
  return selectedIsHistoric.value ? "（这个历史位置）" : "（当前片段）";
});
/**
 * 「查看 → 进入」的分界（spec 第 22 / 53 / 54 条）：
 * 选中话题只是浏览，按这个按钮才是真正改变对话位置的动作。
 */
const startButtonLabel = computed(() => {
  if (anchorBusy.value) return "切换中…";
  if (!selectedFragmentId.value) return `进入「${detail.value?.name ?? "这个话题"}」`;
  return `从这里继续${selectedButtonHint.value}`;
});

/**
 * 面板里三个概念必须分开写清（任务05 D）：
 * - 浏览的话题：现在正在看的这个话题（可能只是浏览，不代表起点变了）；
 * - 选中的片段：用户在片段列表里点的那一个（还没生效）；
 * - 已生效的起点：服务端已经接受的起点（来自会话状态，不是本地猜测）。
 */
const browsedTopicName = computed(() => detail.value?.name ?? "（未选择）");
const selectedFragmentLabel = computed(() =>
  selectedFragmentId.value ? selectedFragmentSummary.value : "未选择片段（用这个话题的最新位置）",
);
const anchorLabel = computed(() => {
  const id = session.currentTopicId;
  if (!id) return "未设置（按最新位置继续）";
  const name =
    session.topicName ||
    topics.value.find((t) => t.topic_id === id)?.title ||
    id;
  const fragmentTitle = session.anchorFragment?.title;
  if (fragmentTitle) {
    return `${name} · ${fragmentTitle}${session.anchorHistoric ? "（历史位置）" : "（当前片段）"}`;
  }
  return `${name} · 最新位置`;
});

async function startHere() {
  if (isClosing.value || anchorBusy.value) return;
  if (!detail.value) return;
  const target = detail.value;
  const fragmentId = selectedFragmentId.value;
  anchorBusy.value = true;
  anchorError.value = "";
  try {
    // 只用后端返回的权威结果更新本地（标题 / 是否历史位置），
    // 避免与同一动作触发的 SSE ANCHOR 事件互相覆盖
    //
    // 两个明确不同的动作（spec 第 53 / 54 条）：
    //  - 选中了历史片段 → 从这里继续：旧片段保持不变，后端新建接续片段；
    //  - 没选片段 → 进入这个话题：从最新位置继续。
    const res = fragmentId
      ? await api.continueFromHistory(target.topic_id, fragmentId)
      : await api.setAnchor(target.topic_id, null);
    // setAnchorAndSync：起点真的变了就重新拉取可见消息。
    // 否则「从这里继续」到别的话题后，对话页还停在上一个话题的对话上。
    await session.setAnchorAndSync(
      res.topic_id || target.topic_id,
      res.fragment_id,
      target.name,
      res.fragment_id ? { id: res.fragment_id, title: res.fragment_title } : undefined,
      res.historic,
    );
  } catch (e) {
    // 失败：本地锚点不变、不关闭，用户可重试
    anchorError.value = `切换失败，未切换话题：${(e as Error).message}`;
    anchorBusy.value = false;
    // 星球已经被收起来了：错误必须回到对话页可见，否则用户以为起点已经切好
    if (isClosing.value) session.lastError = anchorError.value;
    return;
  }
  anchorBusy.value = false;
  await close();
}

/* ---- 星球记忆中心：三页签 + 管理模式 + 与知识/实体面板联动 ---- */
function switchTab(tab: "topic" | "knowledge" | "entity") {
  activeTab.value = tab;
  manageMode.value = tab !== "topic";
  entityOpenId.value = "";
  onPanelWidthChange();
}

/** 「管理模式」按钮：手动切换面板宽度并重对焦 */
function toggleManageMode() {
  manageMode.value = !manageMode.value;
  onPanelWidthChange();
}

/** 知识面板「聚焦话题」→ 切回话题页签并聚焦/加载该话题（详情由 selectedTopicId watcher 单一来源加载） */
function focusTopicFromPanel(topicId: string) {
  planet.selectedTopicId.value = topicId;
  ensureTopicInWindow(topicId);
  planet.focusTopic(topicId, []);
  panelOpen.value = true;
  switchTab("topic");
  // 同话题时 watcher 会跳过 reload，这里强制刷新以拿到面板编辑后的最新数据
  if (detail.value?.topic_id === topicId) loadDetail(topicId);
}

/** 话题详情实体标签 → 打开实体页签并自动展开该实体卡（按 node_id 匹配） */
function openEntityByNode(nodeId: string) {
  activeTab.value = "entity";
  manageMode.value = true;
  entityOpenId.value = nodeId;
  onPanelWidthChange();
}

/**
 * 收起：两段式，避免「一边缩回全景、一边整体淡出」两条时间线互相打架。
 * 1) 收势：相机轻微后撤 + 星球变淡、面板退场（180ms）
 * 2) 整体淡出：连着背景一起淡掉后卸载（280ms）
 * 减少动画时不拖这两段，直接卸载。
 */
async function close() {
  if (!props.open || closing.value || settling.value) return;
  // 捕获「这次关闭属于哪一次打开」：实例常驻后 props.seq 会被下一次打开改写，
  // 若不捕获，迟到的收尾回调会带上新序号、把刚打开的新层关掉。
  const seqAtClose = props.seq;
  const epoch = ++closeEpoch;
  if (prefersReducedMotion()) {
    closing.value = true;
    emit("close", seqAtClose);
    return;
  }
  settling.value = true;
  await Promise.all([planet.pullBack(0.45, SETTLE_MS), new Promise((r) => setTimeout(r, SETTLE_MS))]);
  // 收势期间用户又打开了：放弃这次收尾，否则会把刚打开的层重新变成「收起中」
  if (epoch !== closeEpoch) return;
  closing.value = true;
  await new Promise((r) => setTimeout(r, FADE_MS));
  if (epoch !== closeEpoch) return;
  emit("close", seqAtClose);
}
</script>

<template>
  <div class="planet-view" :class="{ entered, closing, settling }" :style="demoStyle">
    <canvas
      ref="canvasRef"
      class="planet-canvas"
      :class="{ hovering: hoverTopicId }"
      @click="onCanvasClick"
      @dblclick="onCanvasDblClick"
    ></canvas>
    <!-- 指向话题点时显示简短名称：只显示当前指到的那一个，不让标签常驻遮挡 -->
    <div v-if="hoverLabel" class="topic-hint" :style="hoverLabelStyle">{{ hoverLabel.title }}</div>
    <!-- 选中话题的名称常驻（悬停其它点时让位给悬停标签） -->
    <div v-else-if="selectedLabel" class="topic-hint selected" :style="selectedLabelStyle">{{ selectedLabel.title }}</div>
    <button class="close-btn qio-btn" :disabled="closing || settling" @click="close">
      {{ closing || settling ? "收起中…" : "✕ 收起星球" }}
    </button>
    <!-- 图形诊断只在开发者模式出现：正常模式保持「像产品，不像 debugger」 -->
    <div v-if="ui.developerMode" class="hud mono">
      <span v-if="webglOK">WebGL · {{ fpsText }} fps · 视角: {{ cameraStateText }}</span>
      <span v-else>WebGL 不可用</span>
    </div>
    <!-- WebGL 不可用时不白屏/黑屏：给一段可读的降级说明，管理功能仍可用 -->
    <div v-if="!webglOK" class="webgl-fallback" role="alert">
      <div class="wf-title serif">无法启用 3D 星球</div>
      <p class="wf-text">
        当前环境不支持 WebGL（或显卡驱动不可用）。话题数据不受影响，仍可在右侧面板查看与管理。
      </p>
    </div>
    <!-- 首次数据加载失败：说明 + 重试，不拿空球冒充完整内容 -->
    <div v-if="loadError" class="load-error" role="alert">
      <span class="le-text">{{ loadError }}</span>
      <button class="qio-btn" type="button" @click="loadData">重试</button>
    </div>
    <!-- 首次加载中：入口已经响应，长等待要有明确状态 -->
    <div v-else-if="dataLoading" class="planet-loading" role="status">正在加载话题…</div>

    <aside class="panel" :class="{ open: panelOpen, manage: manageMode }">
      <button
        class="panel-toggle"
        :title="panelOpen ? '收起话题列表' : '展开话题列表'"
        :aria-label="panelOpen ? '收起话题列表' : '展开话题列表'"
        :aria-expanded="panelOpen"
        @click="togglePanel"
      >
        <svg class="chev" :class="{ flip: !panelOpen }" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
          <path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
      <div class="panel-inner">
        <div class="tabs">
          <button class="tab tab-topic" :class="{ active: activeTab === 'topic' }" @click="switchTab('topic')">话题</button>
          <button class="tab tab-knowledge" :class="{ active: activeTab === 'knowledge' }" @click="switchTab('knowledge')">知识</button>
          <button class="tab tab-entity" :class="{ active: activeTab === 'entity' }" @click="switchTab('entity')">实体</button>
          <span class="spacer"></span>
          <button class="mode-btn" @click="toggleManageMode">{{ manageMode ? "✕ 管理模式" : "管理模式" }}</button>
        </div>

        <template v-if="activeTab === 'topic'">
          <div class="panel-head">
            <h2 class="serif">话题</h2>
            <QInput v-model="search" placeholder="搜索话题…" />
          </div>
          <!-- 可滚动中部：话题列表自己滚（高度封顶），详情正文占满剩余空间自己滚；
               底部操作区在两者之外，始终可见且不覆盖内容 -->
          <div class="panel-body">
            <div class="topic-scroll">
              <ul class="topic-list">
                <li
                  v-for="t in filteredTopics"
                  :key="t.topic_id"
                  :class="{ active: t.topic_id === planet.selectedTopicId.value }"
                  tabindex="0"
                  role="option"
                  :aria-selected="t.topic_id === planet.selectedTopicId.value"
                  @click="selectTopic(t.topic_id)"
                  @keydown.enter.prevent="selectTopic(t.topic_id)"
                  @keydown.space.prevent="selectTopic(t.topic_id)"
                >
                  <span class="name serif">{{ t.title }}</span>
                  <span class="meta qio-badge">{{ t.fragment_count }} 片段</span>
                </li>
              </ul>
            </div>

            <div class="detail-scroll">
              <div v-if="detailLoading" class="detail-loading">加载中…</div>
              <!-- 加载失败：没有详情也要能看到失败原因并有重试入口 -->
              <div v-else-if="detailError" class="detail-error" role="alert">
                <p class="err-text">{{ detailError }}</p>
                <button class="qio-btn" type="button" @click="retryDetail">重试</button>
              </div>
              <div v-else-if="detail" class="detail">
                <h3 class="serif">{{ detail.name }}</h3>

              <!-- 浏览的话题 / 选中的片段 / 已经生效的起点：三者分开写，不混为一谈 -->
              <dl class="detail-identity">
                <div class="id-row">
                  <dt>浏览的话题</dt>
                  <dd>{{ browsedTopicName }}</dd>
                </div>
                <div class="id-row">
                  <dt>选中的片段</dt>
                  <dd>{{ selectedFragmentLabel }}</dd>
                </div>
                <div class="id-row">
                  <dt>已生效的起点</dt>
                  <dd class="mono">{{ anchorLabel }}</dd>
                </div>
              </dl>

              <div class="detail-section">
                <div class="section-title serif">片段（选择要接续的历史位置）</div>
                <div
                  v-for="f in detail.fragments"
                  :key="f.fragment_id"
                  class="fragment-item"
                  :class="{ selected: f.fragment_id === selectedFragmentId }"
                  @click="selectedFragmentId = f.fragment_id"
                >
                  <div class="fragment-summary">{{ f.summary || "（无摘要）" }}</div>
                  <div class="fragment-meta mono">{{ f.message_count }} 条消息 · {{ f.closed_at ? "已封块" : "开放中" }}</div>
                </div>
                <p v-if="!detail.fragments.length" class="hint">暂无片段：这个话题还没有可接续的历史位置。</p>
              </div>

              <div class="detail-section">
                <div class="section-title serif">实体</div>
                <div class="entity-tags">
                  <span v-for="e in detail.entities" :key="e.id" class="entity-tag" @click="openEntityByNode(e.id)">{{ e.name }}</span>
                  <span v-if="!detail.entities.length" class="hint">无</span>
                </div>
              </div>

              <div class="detail-section">
                <div class="section-title serif">知识</div>
                <div v-for="k in detail.knowledge" :key="k.id" class="knowledge-item">
                  <span class="k-state qio-badge" :class="k.state">{{ k.state }}</span>
                  <template v-if="knowledgeEditingId === k.id">
                    <input
                      v-model="knowledgeDraft"
                      class="qio-input knowledge-edit"
                      @keyup.enter="saveKnowledgeEdit(k)"
                    />
                    <button class="qio-btn mini" @click="saveKnowledgeEdit(k)">保存</button>
                    <button class="qio-btn mini" @click="knowledgeEditingId = null">取消</button>
                  </template>
                  <template v-else>
                    <span class="k-content">{{ k.content }}</span>
                    <button class="qio-btn mini" @click="startEditKnowledge(k)">修正</button>
                    <button class="qio-btn mini danger" @click="deleteKnowledge(k)">删除</button>
                  </template>
                </div>
                <p v-if="!detail.knowledge.length" class="hint">无知识条目。</p>
              </div>
            </div>
            </div>
          </div>

          <!-- 固定底部操作区：选中的片段、起点失败原因与动作按钮始终可见 -->
          <div v-if="detail" class="detail-actions">
            <p v-if="selectedFragmentId" class="selected-position">
              {{ selectedPositionLabel }}：{{ selectedFragmentSummary }}
            </p>
            <p v-else class="selected-position muted">
              未选片段：将从「{{ detail.name }}」的最新位置继续
            </p>
            <p v-if="anchorError" class="anchor-error" role="alert">{{ anchorError }}</p>
            <button class="start-btn qio-btn primary" :disabled="closing || anchorBusy" @click="startHere">
              {{ startButtonLabel }}
            </button>
          </div>
        </template>
        <KnowledgePanel v-else-if="activeTab === 'knowledge'" @focus-topic="focusTopicFromPanel" />
        <EntityPanel v-else :open-by-node-id="entityOpenId" />
      </div>
    </aside>
  </div>
</template>

<style scoped>
.planet-view {
  position: fixed; inset: 0; z-index: 50; background: var(--bg-base); display: flex;
  overflow: hidden;
  /* 出现：先铺底 → 内容再淡入（两步有先后，聊天文字不会长时间透出来）；
     消失：收势后整体淡出，淡出结束时才卸载 */
  transition: opacity var(--dur-planet-out) var(--ease-in);
}
/* 铺底：快速盖住聊天，避免两层内容长时间叠加 */
.planet-view::before {
  content: ""; position: absolute; inset: 0; z-index: 1; background: var(--bg-base);
  animation: planet-backdrop-in var(--dur-planet-backdrop) var(--ease-out) both;
}
@keyframes planet-backdrop-in { from { opacity: 0; } to { opacity: 1; } }
/* 内容层压在铺底之上（关闭按钮/诊断/错误条各有自己的 z-index，保持更高） */
.planet-canvas, .panel { z-index: 2; }
/* 内容用类切换而不是 keyframes：动画的 fill 会盖住「收势变淡」的过渡，
   用普通 transition 才能让两次 opacity 变化都生效，也让状态不依赖动画事件。 */
.planet-canvas {
  opacity: 0;
  transition: opacity var(--dur-planet-in) var(--ease-out);
}
.planet-view.entered .planet-canvas { opacity: 1; }
.planet-view.entered.settling .planet-canvas {
  opacity: 0.25;
  transition-duration: var(--dur-planet-settle);
}
.planet-view.closing { opacity: 0; }
/* 收起阶段（整体淡出中）：不再拦截点击与焦点 —— 页面已经在消失，不能继续扣着输入 */
.planet-view.closing { pointer-events: none; }
.planet-canvas { flex: 1 1 auto; min-width: 0; cursor: grab; }
.planet-canvas.hovering { cursor: pointer; }
.close-btn {
  position: absolute; top: 14px; left: 14px; z-index: 60;
  background: var(--accent-soft); border-color: var(--border-strong); color: var(--text-strong);
  border-radius: 20px;
}
.close-btn:hover { border-color: var(--accent); color: var(--accent); }
.close-btn:disabled { opacity: 0.6; cursor: default; }
.hud {
  position: absolute; bottom: 14px; left: 14px; font-size: 12px; color: var(--text-muted);
  background: var(--bg-overlay); border: 1px solid var(--border-subtle); border-radius: 20px;
  padding: 6px 12px; backdrop-filter: blur(6px);
}
.webgl-fallback {
  position: absolute; inset: 0; display: flex; flex-direction: column; gap: 10px;
  align-items: center; justify-content: center; text-align: center; padding: 0 24px;
  pointer-events: none;
}
.webgl-fallback .wf-title { font-size: 20px; color: var(--text-strong); }
.webgl-fallback .wf-text { font-size: 13px; color: var(--text-secondary); max-width: 46ch; line-height: 1.7; }
.load-error {
  position: absolute; top: 14px; left: 50%; transform: translateX(-50%);
  z-index: 61; display: flex; align-items: center; gap: 10px; max-width: min(560px, calc(100vw - 200px));
  padding: 8px 12px; border-radius: 10px;
  border: 1px solid var(--danger); background: var(--bg-elevated); color: var(--danger);
  font-size: 12.5px;
}
.load-error .le-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.planet-loading {
  position: absolute; top: 14px; left: 50%; transform: translateX(-50%); z-index: 61;
  padding: 8px 14px; border-radius: 10px; font-size: 12.5px;
  border: 1px solid var(--border-strong); background: var(--bg-elevated); color: var(--text-secondary);
}
/* 悬停标签：跟随指针，避开指针本身；不可交互，不抢焦点 */
.topic-hint {
  position: absolute; z-index: 58; transform: translate(14px, -50%);
  max-width: 240px; padding: 3px 9px; border-radius: 20px;
  font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  background: var(--bg-overlay); border: 1px solid var(--border-strong); color: var(--text-strong);
  pointer-events: none;
}
/* 选中话题的名称：与「已选中」状态同一套强调色，和悬停标签区分开 */
.topic-hint.selected {
  /* 选中点外面有选中环，标签再往外让一点，避免压在环上 */
  transform: translate(46px, -50%);
  border-color: var(--accent);
  color: var(--accent);
  background: var(--bg-accent-subtle);
}
.panel {
  position: relative;
  flex: 0 0 auto;
  min-width: 0;
  width: 0;
  height: 100%;
  background: var(--bg-panel);
  border-left: 1px solid transparent;
  opacity: 0;
  /* 侧栏开合 210ms，与 210ms 的重定位补间同时发生（不再串联成两次移动）；
     opacity 负责入场（420ms）与收起时的收势（180ms）。 */
  transition: width var(--dur-panel) var(--ease-out), border-color var(--dur-panel) var(--ease-out),
    opacity var(--dur-planet-in) var(--ease-out);
}
.planet-view.entered .panel { opacity: 1; }
.planet-view.entered.settling .panel {
  opacity: 0.35;
  transform: translateX(8px);
  transition: width var(--dur-panel) var(--ease-out), border-color var(--dur-panel) var(--ease-out),
    opacity var(--dur-planet-settle) var(--ease-out), transform var(--dur-planet-settle) var(--ease-out);
}
.panel.open {
  width: clamp(340px, 32vw, 480px);
  border-left-color: var(--border-subtle);
}
.panel-inner {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
/* 可滚动中部：顶部标题与底部操作区保持稳定，只有这两个区域各自滚动 */
.panel-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 话题多时列表自己滚，且高度封顶：详情永远留在可见区域，不用先滚过整份列表 */
.topic-scroll {
  flex: 0 1 auto;
  max-height: 42%;
  min-height: 0;
  overflow-y: auto;
  border-bottom: 1px solid var(--border-subtle);
}
.detail-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}
/* 固定底部操作区（在正常流里，不覆盖正文） */
.detail-actions {
  flex: 0 0 auto;
  padding: 10px 14px 14px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-panel);
}
.detail-actions .start-btn { margin-top: 10px; }
.detail-identity {
  margin: 8px 0 4px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: var(--bg-inset);
}
.detail-identity .id-row { display: flex; gap: 8px; font-size: 12px; line-height: 1.7; }
.detail-identity dt { color: var(--text-muted); flex: 0 0 84px; margin: 0; }
.detail-identity dd { margin: 0; color: var(--text-primary); min-width: 0; overflow-wrap: anywhere; }
.topic-list li:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.panel.open.manage { width: clamp(360px, 46vw, 640px); }
/* 中等窗口：面板按视口比例收窄，别把星球挤成一条 */
@media (max-width: 1199px) {
  .panel.open { width: 46vw; }
  .panel.open.manage { width: 50vw; }
}
/* 窄窗口（接近 Tauri 最小尺寸）：面板变成覆盖式抽屉，星球保持可用 */
@media (max-width: 899px) {
  .panel.open,
  .panel.open.manage {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(92vw, 520px);
    z-index: 55;
    border-left: 1px solid var(--border-strong);
    box-shadow: var(--shadow-3);
  }
}
.tabs { display: flex; align-items: center; gap: 6px; padding: 10px 14px 6px; border-bottom: 1px solid var(--border-subtle); }
.tab { font-size: 13px; padding: 6px 12px; color: var(--text-secondary); cursor: pointer; background: none; border: none; border-bottom: 2px solid transparent; font-family: var(--sans); }
.tab:hover { color: var(--text-strong); }
.tab.active { color: var(--text-strong); border-bottom-color: var(--accent); font-weight: 600; }
.tabs .spacer { flex: 1; }
.mode-btn { font-size: 11px; padding: 4px 10px; border-radius: 20px; border: 1px solid var(--border-strong); background: none; color: var(--text-secondary); cursor: pointer; }
.mode-btn:hover { color: var(--accent); border-color: var(--accent); }
.panel-toggle {
  position: absolute;
  top: 50%;
  left: -30px;
  transform: translateY(-50%);
  z-index: 3;
  width: 30px;
  height: 78px;
  padding: 0;
  border: 1px solid var(--border-strong);
  border-right: none;
  border-radius: 12px 0 0 12px;
  background: var(--bg-panel);
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.2s ease, color 0.2s ease, border-color 0.2s ease;
}
.panel-toggle:hover {
  background: var(--accent-soft);
  color: var(--accent);
  border-color: var(--accent);
}
.panel-toggle .chev {
  transition: transform 0.3s cubic-bezier(0.22, 0.8, 0.24, 1);
}
.panel-toggle .chev.flip {
  transform: rotate(180deg);
}
.panel-head { padding: 14px 14px 8px; }
.panel-head h2 { font-size: 16px; color: var(--text-strong); margin: 0 0 8px; }
.topic-list { list-style: none; padding: 0 14px; margin: 6px 0; }
.topic-list li {
  display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 10px;
  border-radius: 10px; cursor: pointer; font-size: 13px;
}
.topic-list li:hover { background: var(--accent-soft); }
.topic-list li.active { background: var(--accent-softer); }
.topic-list .name { font-size: 14px; color: var(--text-primary); }
.topic-list .meta { flex-shrink: 0; }
.detail { padding: 0 14px 20px; border-top: 1px solid var(--border-subtle); }
.detail h3 { font-size: 16px; color: var(--text-strong); margin: 12px 0 8px; }
.detail-section { margin: 10px 0; }
.section-title { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; letter-spacing: 0.02em; }
.fragment-item {
  border: 1px solid var(--border-subtle); border-radius: 10px; padding: 8px 10px; margin-bottom: 6px;
  cursor: pointer; background: var(--bg-inset);
}
.fragment-item:hover { border-color: var(--border-strong); }
.fragment-item.selected { border-color: var(--accent); background: var(--bg-accent-subtle); }
.fragment-summary { font-size: 13px; color: var(--text-primary); }
.fragment-meta { font-size: 11px; color: var(--text-muted); margin-top: 3px; }
.entity-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.entity-tag { background: var(--bg-accent-subtle); border-radius: 20px; padding: 3px 10px; font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.knowledge-item { display: flex; gap: 8px; align-items: center; margin-bottom: 5px; font-size: 12px; flex-wrap: wrap; }
.knowledge-edit { flex: 1; min-width: 160px; height: auto; padding: 5px 10px; font-size: 12px; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; border-radius: 8px; }
.qio-btn.mini.danger { color: var(--danger); border-color: var(--border-danger); }
.k-state { background: var(--border-subtle); color: var(--text-secondary); border-color: var(--border-subtle); }
.k-state.active { background: var(--success-soft); color: var(--success); border-color: var(--success-soft); }
.k-content { color: var(--text-primary); }
.start-btn { width: 100%; height: 40px; margin-top: 12px; font-size: 14px; }
.start-btn:disabled { opacity: 0.6; cursor: default; }
.anchor-error {
  margin-top: 12px; padding: 8px 12px; border-radius: 8px;
  border: 1px solid var(--danger); background: var(--danger-soft);
  color: var(--danger); font-size: 12px; line-height: 1.5;
}
.selected-position {
  margin-top: 12px; font-size: 12px; line-height: 1.6;
  color: var(--text-secondary);
}
.selected-position.muted { color: var(--text-muted); }
.hint { font-size: 12px; color: var(--text-muted); }
.detail-loading { padding: 14px; color: var(--text-muted); font-size: 13px; }
.detail-error {
  display: flex; flex-direction: column; gap: 10px; align-items: flex-start;
  padding: 0 14px 20px; border-top: 1px solid var(--border-subtle);
}
.detail-error .err-text {
  margin-top: 12px; padding: 8px 12px; border-radius: 8px; width: 100%;
  border: 1px solid var(--danger); background: var(--danger-soft);
  color: var(--danger); font-size: 12px; line-height: 1.5;
}
</style>
