<script setup lang="ts">
/**
 * 星球页（全屏覆盖层）：3D 球体（左/中）+ 右侧面板（列表/详情/从这里开始）。
 * usePlanetScene 在本页内实例化渲染全屏星球（悬浮球为 SVG 微缩星球，不共享同一
 * WebGL 场景）；相机状态机 overview|planet|focus 语义与悬浮球保持一致：
 * - 打开（悬浮球点击）：场景推进到 planet（有锚点时直接聚焦话题）；
 * - 关闭（✕/Esc/从这里开始）：相机先拉回 overview（悬浮球位置/朝向）再收起覆盖层，
 *   拉回窗口内交互一律禁用。
 */
import { onMounted, onUnmounted, ref, watch } from "vue";
import { usePlanetScene } from "../composables/usePlanetScene";
import { api, type TopicDetail, type TopicFingerprint, type TopicPosition } from "../services/api";
import { useSessionStore } from "../stores/session";
import QInput from "../components/ui/QInput.vue";

const emit = defineEmits<{ close: [] }>();
const session = useSessionStore();

const canvasRef = ref<HTMLCanvasElement | null>(null);
const planet = usePlanetScene(canvasRef);
const topics = ref<TopicFingerprint[]>([]);
const positions = ref<TopicPosition[]>([]);
const detail = ref<TopicDetail | null>(null);
const detailLoading = ref(false);
const search = ref("");
const selectedFragmentId = ref<string | null>(null);
/** 关闭动画进行中（相机拉回 overview），防止重复关闭/重复交互 */
const closing = ref(false);
/** 右侧话题边栏：默认收起；点击话题点/展开按钮展开，点击收起按钮收起 */
const panelOpen = ref(false);
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
  detailLoading.value = true;
  selectedFragmentId.value = null;
  try {
    detail.value = await api.getTopicDetail(topicId);
  } finally {
    detailLoading.value = false;
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

async function startHere() {
  if (closing.value) return;
  if (!detail.value) return;
  try {
    await api.setAnchor(detail.value.topic_id, selectedFragmentId.value);
  } catch (e) {
    console.error("[planet] set anchor failed:", e);
  }
  session.setAnchor(detail.value.topic_id, selectedFragmentId.value, detail.value.name);
  await close();
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
    <div class="hud mono">
      <span v-if="planet.webglOK">WebGL · {{ planet.fps }} fps · 视角: {{ planet.cameraState }}</span>
      <span v-else>WebGL 不可用</span>
    </div>

    <aside class="panel" :class="{ open: panelOpen }">
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
          <div class="section-title serif">片段（点击选择起点）</div>
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
            <span v-for="e in detail.entities" :key="e.id" class="entity-tag">{{ e.name }}</span>
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

        <button class="start-btn qio-btn primary" :disabled="closing" @click="startHere">
          从这里开始{{ selectedFragmentId ? "（选中片段）" : "（整个话题）" }}
        </button>
      </div>
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
  width: 340px;
  border-left-color: var(--border-subtle);
}
.panel-inner {
  width: 340px;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
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
.entity-tag { background: var(--bg-accent-subtle); border-radius: 20px; padding: 3px 10px; font-size: 12px; color: var(--text-secondary); }
.knowledge-item { display: flex; gap: 8px; align-items: center; margin-bottom: 5px; font-size: 12px; flex-wrap: wrap; }
.knowledge-edit { flex: 1; min-width: 160px; height: auto; padding: 5px 10px; font-size: 12px; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; border-radius: 8px; }
.qio-btn.mini.danger { color: var(--danger); border-color: var(--border-danger); }
.k-state { background: var(--border-subtle); color: var(--text-secondary); border-color: var(--border-subtle); }
.k-state.active { background: var(--success-soft); color: var(--success); border-color: var(--success-soft); }
.k-content { color: var(--text-primary); }
.start-btn { width: 100%; height: 40px; margin-top: 12px; font-size: 14px; }
.start-btn:disabled { opacity: 0.6; cursor: default; }
.hint { font-size: 12px; color: var(--text-muted); }
.detail-loading { padding: 14px; color: var(--text-muted); font-size: 13px; }
</style>
