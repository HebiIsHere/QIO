<script setup lang="ts">
/**
 * 星球页（全屏覆盖层）：3D 球体（左/中）+ 右侧面板（列表/详情/从这里开始）。
 * usePlanetScene 在本页内实例化渲染全屏星球（悬浮球为 SVG 微缩星球，不共享同一
 * WebGL 场景）；相机状态机 overview|planet|focus 语义与悬浮球保持一致：
 * - 打开（悬浮球点击）：场景推进到 planet（有锚点时直接聚焦话题）；
 * - 关闭（✕/Esc/从这里开始）：相机先拉回 overview（悬浮球位置/朝向）再收起覆盖层，
 *   拉回窗口内交互一律禁用。
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { usePlanetScene } from "../composables/usePlanetScene";
import { api, type TopicDetail, type TopicFingerprint, type TopicPosition } from "../services/api";
import { useSessionStore } from "../stores/session";
import QInput from "../components/ui/QInput.vue";
import KnowledgePanel from "../components/planet/KnowledgePanel.vue";
import EntityPanel from "../components/planet/EntityPanel.vue";
import { useUiStore } from "../stores/ui";

const emit = defineEmits<{ close: [] }>();
const session = useSessionStore();
const ui = useUiStore();

const canvasRef = ref<HTMLCanvasElement | null>(null);
const planet = usePlanetScene(canvasRef);
// 场景返回的是 ref 容器对象（普通对象里的 ref 在模板中不会自动解包），
// 这里显式取值：否则 HUD 会渲染出 ref 对象本身。
const webglOK = computed(() => planet.webglOK.value);
const fpsText = computed(() => planet.fps.value);
const cameraStateText = computed(() => planet.cameraState.value);
const topics = ref<TopicFingerprint[]>([]);
const positions = ref<TopicPosition[]>([]);
const detail = ref<TopicDetail | null>(null);
const detailLoading = ref(false);
/** 详情请求序号：只接受「最后一次点击」的结果，慢请求返回不得覆盖（竞态防护） */
let detailSeq = 0;
const search = ref("");
const selectedFragmentId = ref<string | null>(null);
/** 关闭动画进行中（相机拉回 overview），防止重复关闭/重复交互 */
const closing = ref(false);
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
  if (closing.value) return;
  panelOpen.value = !panelOpen.value;
  // 画布宽度随边栏动画变化，结束后若仍有焦点话题，重对焦到新画布中心
  const focused = planet.selectedTopicId.value;
  if (focused) scheduleRecenter(focused);
}

/** 边栏开合动画结束后（~380ms）微调：把焦点话题重新居中到新画布中心。 */
function scheduleRecenter(topicId: string, delay = 380) {
  window.clearTimeout(recenterTimer);
  recenterTimer = window.setTimeout(() => {
    if (closing.value) return;
    if (planet.selectedTopicId.value !== topicId) return;
    planet.focusTopic(topicId, positions.value, { duration: 360, wave: false });
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
  planetReady = true;
  applyPlanetTheme();
  startThemeObserver();
  startCanvasObserver();
  await loadData();
});

onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
  stopThemeObserver();
  stopCanvasObserver();
  window.clearTimeout(recenterTimer);
});

function onKeydown(e: KeyboardEvent) {
  if (e.key !== "Escape") return;
  const t = e.target as HTMLElement | null;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  close();
}

async function loadData() {
  const [t, p] = await Promise.all([api.listTopics(), api.getPositions()]);
  // 等待期间用户已发起关闭：不再推进相机/加载详情，避免打断拉回 tween
  if (closing.value) return;
  topics.value = t.topics;
  positions.value = p.topics;
  planet.setTopics(p.topics);
  planet.loadTopics(p.topics);
  // 若已有锚点话题，聚焦它（详情由 selectedTopicId watcher 单一来源加载）
  if (session.currentTopicId) {
    planet.selectedTopicId.value = session.currentTopicId;
    planet.focusTopic(session.currentTopicId, positions.value);
  } else {
    planet.go("planet");
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
  anchorError.value = "";
  selectedFragmentId.value = null;
  try {
    const d = await api.getTopicDetail(topicId);
    // 期间用户已切到别的话题：丢弃过期响应
    if (seq !== detailSeq || planet.selectedTopicId.value !== topicId) return;
    detail.value = d;
  } catch (e) {
    if (seq === detailSeq) {
      detail.value = null;
      anchorError.value = `加载话题详情失败：${(e as Error).message}`;
    }
  } finally {
    if (seq === detailSeq) detailLoading.value = false;
  }
}

function selectTopic(topicId: string) {
  if (closing.value) return;
  planet.selectedTopicId.value = topicId;
  planet.focusTopic(topicId, positions.value);
}

function onCanvasClick(e: MouseEvent) {
  if (closing.value) return;
  const focused = planet.handleClick(e.clientX, e.clientY);
  if (focused) {
    // 收起态点击话题点：同时展开边栏，画布中心左移 → 动画结束后重对焦
    const wasCollapsed = !panelOpen.value;
    panelOpen.value = true;
    if (wasCollapsed) scheduleRecenter(planet.selectedTopicId.value ?? "");
  }
}

function onCanvasDblClick() {
  if (!closing.value) planet.go("planet");
}

watch(() => planet.selectedTopicId.value, (id) => {
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

async function startHere() {
  if (closing.value || anchorBusy.value) return;
  if (!detail.value) return;
  const target = detail.value;
  const fragmentId = selectedFragmentId.value;
  anchorBusy.value = true;
  anchorError.value = "";
  try {
    // 只用后端返回的权威结果更新本地（标题 / 是否历史位置），
    // 避免与同一动作触发的 SSE ANCHOR 事件互相覆盖
    const res = await api.setAnchor(target.topic_id, fragmentId);
    session.setAnchor(
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
  planet.focusTopic(topicId, positions.value);
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

/** 关闭：相机先拉回悬浮球远景（overview），动画结束后收起覆盖层 */
async function close() {
  if (closing.value) return;
  closing.value = true;
  await planet.go("overview");
  emit("close");
}
</script>

<template>
  <div class="planet-view" :class="{ closing }">
    <canvas ref="canvasRef" class="planet-canvas" @click="onCanvasClick" @dblclick="onCanvasDblClick"></canvas>
    <button class="close-btn qio-btn" :disabled="closing" @click="close">
      {{ closing ? "收起中…" : "✕ 收起星球" }}
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
          <ul class="topic-list">
            <li
              v-for="t in filteredTopics"
              :key="t.topic_id"
              :class="{ active: t.topic_id === planet.selectedTopicId.value }"
              @click="selectTopic(t.topic_id)"
            >
              <span class="name serif">{{ t.title }}</span>
              <span class="meta qio-badge">{{ t.fragment_count }} 片段</span>
            </li>
          </ul>

          <div v-if="detailLoading" class="detail-loading">加载中…</div>
          <div v-else-if="detail" class="detail">
            <h3 class="serif">{{ detail.name }}</h3>

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
              <p v-if="!detail.fragments.length" class="hint">暂无片段。</p>
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

            <p v-if="selectedFragmentId" class="selected-position">
              {{ selectedPositionLabel }}：{{ selectedFragmentSummary }}
            </p>
            <p v-else class="selected-position muted">
              未选片段：将从「{{ detail.name }}」的最新位置继续
            </p>
            <p v-if="anchorError" class="anchor-error" role="alert">{{ anchorError }}</p>
            <button class="start-btn qio-btn primary" :disabled="closing || anchorBusy" @click="startHere">
              {{
                anchorBusy
                  ? "切换中…"
                  : `从这里继续${selectedButtonHint}`
              }}
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
  transition: opacity 0.5s ease;
}
.planet-view.closing { opacity: 0; }
.planet-canvas { flex: 1 1 auto; min-width: 0; cursor: grab; }
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
.panel {
  position: relative;
  flex: 0 0 auto;
  min-width: 0;
  width: 0;
  height: 100%;
  background: var(--bg-panel);
  border-left: 1px solid transparent;
  transition: width 0.32s cubic-bezier(0.22, 0.8, 0.24, 1), border-color 0.32s ease;
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
  overflow-y: auto;
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
</style>
