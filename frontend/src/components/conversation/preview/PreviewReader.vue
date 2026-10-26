<script setup lang="ts">
/**
 * 预览栏的阅读器主体（C 负责）：负责一个标签的完整阅读体验。
 *
 * 它做四件事：
 *   1. 按类别分派渲染（图片 / PDF / DOCX / XLSX / PPTX / 文本 / 代码 / Markdown）；
 *   2. 提供适用能力的工具条（搜索、复制、缩放、翻页、外部打开、下载）——没有的能力不假装有；
 *   3. 保存/恢复**这个标签自己的**阅读位置（滚动、页码、工作表、缩放、搜索）；
 *   4. 打开期间检查真实文件版本：内容变了原地刷新并提示；失败时保留上次内容并说明；
 *      不切换标签、不重新打开已收起的预览栏、不读到写了一半的文件（debounce + 次数上限）。
 *
 * 资源：PDF/图片的重资源由子组件持有并在换内容/卸载时释放；本组件只负责轻量位置与请求调度。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import { useSessionStore } from "../../../stores/session";
import type { PreviewTabState } from "../../../conversation/previewTypes";
import {
  FILE_UPDATE_CHECK_DELAY_MS,
  FILE_UPDATE_POLL_MS,
  copyTextToClipboard,
  isSearchableKind,
  searchCodeView,
  searchDocxView,
  searchPptxView,
  searchTextLines,
  searchXlsxView,
  supportsTextCopy,
  supportsZoom,
  zoomStep,
  type PreviewSearchHit,
} from "../../../conversation/preview";
import { vQioScrollbar } from "../../../composables/useQioScrollbars";
import { loadRenderer } from "../../../interactive/renderers";
import type { RenderResult } from "../../../interactive/renderers/types";
import { languageLabel } from "../../../interactive/renderers/code";
import type { CodeView } from "../../../interactive/renderers/code";
import type { DocxView } from "../../../interactive/renderers/docx";
import type { PptxView } from "../../../interactive/renderers/pptx";
import type { XlsxView } from "../../../interactive/renderers/xlsx";
import PreviewTextPane from "./PreviewTextPane.vue";
import PreviewStructuredPane from "./PreviewStructuredPane.vue";
import PreviewImageViewer from "./PreviewImageViewer.vue";
import PreviewPdfViewer from "./PreviewPdfViewer.vue";
import { vQioTip } from "../../../composables/useQioTip";

const props = withDefaults(defineProps<{ tab: PreviewTabState; full?: boolean }>(), { full: false });

const store = useConversationPreviewStore();
const session = useSessionStore();

const tabId = computed(() => props.tab.id);
const kind = computed(() => props.tab.target.kind);
const target = computed(() => props.tab.target);
const runtime = computed(() => store.runtimeOf(tabId.value));
const documentInput = computed(() => store.documentOf(tabId.value));

// ── 视图模型（后端结构化数据 → 渲染器视图；按需动态 import） ──────────────────

const codeView = ref<CodeView | null>(null);
const docxView = ref<DocxView | null>(null);
const xlsxView = ref<XlsxView | null>(null);
const pptxView = ref<PptxView | null>(null);
const viewError = ref("");
const wrapLines = ref(true);
const pdfFactsData = computed<Record<string, unknown> | null>(() => {
  const envelope = runtime.value?.envelope;
  return envelope?.status === "ready" ? envelope.data : null;
});

let parseSequence = 0;

function linesView(text: string, language: string): CodeView {
  const lines = text.split("\n").map((line, index) => ({ number: index + 1, text: line }));
  return {
    language,
    languageLabel: language ? languageLabel(language) : "纯文本",
    lines,
    totalLines: lines.length,
    encoding: "未说明",
    truncated: false,
  };
}

/** 回复代码 / 任务输出正文（内存里的正文；不进布局持久化）。 */
const documentView = computed<CodeView | null>(() => {
  const doc = documentInput.value;
  if (!doc || typeof doc.text !== "string") return null;
  return linesView(doc.text, doc.language ?? "");
});

async function parseEnvelope(): Promise<void> {
  const envelope = runtime.value?.envelope;
  if (!envelope || envelope.status !== "ready" || !envelope.data) return;
  const current = kind.value;
  if (current === "image" || current === "pdf") return;
  const rendererName =
    current === "markdown" ? "markdown" : current === "text" || current === "code" ? "code" : current;
  if (!["docx", "xlsx", "pptx", "code", "markdown"].includes(rendererName)) return;
  const mine = ++parseSequence;
  try {
    const module = await loadRenderer<{ view: (value: unknown) => RenderResult<unknown> }>(rendererName);
    if (mine !== parseSequence) return;
    const result = module.view(envelope.data);
    if (mine !== parseSequence) return;
    if (!result.ok) {
      viewError.value = result.reason;
      return;
    }
    viewError.value = "";
    if (rendererName === "docx") docxView.value = result.view as DocxView;
    else if (rendererName === "xlsx") xlsxView.value = result.view as XlsxView;
    else if (rendererName === "pptx") pptxView.value = result.view as PptxView;
    else codeView.value = result.view as CodeView;
    initialisePanes();
    runSearch();
  } catch (error) {
    if (mine === parseSequence) viewError.value = "这份内容解析失败：" + messageOf(error);
  }
}

watch(
  () => [runtime.value?.phase, runtime.value?.version, kind.value] as const,
  () => {
    void parseEnvelope();
  },
  { immediate: true },
);

watch(documentView, (view) => {
  if (view && kind.value !== "markdown" && !codeView.value) {
    runSearch();
  }
});

/** 文本类最终使用的行视图：后端解析结果优先，回复代码/任务正文作为兜底。 */
const effectiveCodeView = computed<CodeView | null>(() => codeView.value ?? documentView.value);
const markdownSource = computed(() => {
  if (kind.value !== "markdown") return "";
  return effectiveCodeView.value?.lines.map((line) => line.text).join("\n") ?? "";
});

// ── 阅读位置（每个标签各存各的） ──────────────────────────────────────────────

const bodyRef = ref<HTMLElement | null>(null);
const zoom = ref(Number(props.tab.position.zoom) || 1);
const pdfPage = ref(Number(props.tab.position.page) || 1);
const pdfPages = ref(0);
const slidePage = ref(Number(props.tab.position.page) || 1);
const sheetName = ref(String(props.tab.position.sheet || ""));
const activeLine = ref<number | null>(null);
const imageFit = ref(1);
let restored = false;
let scrollTimer: ReturnType<typeof setTimeout> | null = null;

const isPanKind = computed(() => kind.value === "image" || kind.value === "pdf");
const isTextKind = computed(
  () => kind.value === "text" || kind.value === "code" || kind.value === "markdown",
);

function savePosition(patch: Parameters<typeof store.savePosition>[1]): void {
  store.savePosition(tabId.value, patch);
}

function restoreScroll(): void {
  if (isPanKind.value) return;
  void nextTick(() => {
    const body = bodyRef.value;
    if (!body || restored) return;
    body.scrollTop = Math.max(0, Number(props.tab.position.scrollTop) || 0);
    restored = true;
  });
}

function onBodyScroll(): void {
  if (scrollTimer) clearTimeout(scrollTimer);
  scrollTimer = setTimeout(() => {
    scrollTimer = null;
    store.savePosition(tabId.value, { scrollTop: bodyRef.value?.scrollTop ?? 0 });
  }, 260);
}

function flushScroll(): void {
  if (scrollTimer) {
    clearTimeout(scrollTimer);
    scrollTimer = null;
  }
  if (!isPanKind.value && bodyRef.value) {
    store.savePosition(tabId.value, { scrollTop: bodyRef.value.scrollTop });
  }
}

// ── 子面板的初始状态 ──────────────────────────────────────────────────────────

function initialisePanes(): void {
  if (kind.value === "xlsx" && xlsxView.value) {
    const sheets = xlsxView.value.sheets;
    if (sheets.length && !sheets.some((sheet) => sheet.name === sheetName.value)) {
      sheetName.value = sheets[0].name;
    }
  }
  if (kind.value === "pptx" && pptxView.value) {
    const slides = pptxView.value.slides;
    if (slides.length && !slides.some((slide) => slide.index === slidePage.value)) {
      slidePage.value = slides[0].index;
    }
  }
  if (kind.value === "pdf" && pdfPages.value && pdfPage.value > pdfPages.value) {
    pdfPage.value = pdfPages.value;
  }
}

// ── 搜索 ─────────────────────────────────────────────────────────────────────

const searchQuery = ref(String(props.tab.position.search || ""));
const activeHitIndex = ref(-1);
const activeHit = ref<PreviewSearchHit | null>(null);
let searchTimer: ReturnType<typeof setTimeout> | null = null;

const pdfViewerRef = ref<{ search: (query: string) => void } | null>(null);
const pdfSearchHits = ref<PreviewSearchHit[]>([]);
const pdfSearchInfo = ref<{ truncated: boolean; scannedPages: number; totalPages: number } | null>(null);

const searchHits = computed<PreviewSearchHit[]>(() => {
  const query = searchQuery.value;
  if (!query.trim()) return [];
  if (kind.value === "pdf") return pdfSearchHits.value;
  if (kind.value === "docx") return searchDocxView(docxView.value, query).hits;
  if (kind.value === "xlsx") return searchXlsxView(xlsxView.value, query).hits;
  if (kind.value === "pptx") return searchPptxView(pptxView.value, query).hits;
  if (kind.value === "markdown") return [];
  const view = effectiveCodeView.value;
  return view ? searchCodeView(view, query).hits : [];
});

const markdownMarks = ref<HTMLElement[]>([]);
const markdownMarkKey = ref(0);
const totalHits = computed(() =>
  kind.value === "markdown" ? markdownMarks.value.length : searchHits.value.length,
);
const activeHitNumber = computed(() => (activeHitIndex.value >= 0 ? activeHitIndex.value + 1 : 0));

function runSearch(): void {
  savePosition({ search: searchQuery.value.trim() });
  activeHitIndex.value = -1;
  activeHit.value = null;
  activeLine.value = null;
  if (kind.value === "markdown") {
    markdownMarkKey.value += 1;
    markdownMarks.value = [];
    void refreshMarkdownMarks();
    return;
  }
  if (kind.value === "pdf") {
    pdfSearchHits.value = [];
    pdfSearchInfo.value = null;
    pdfViewerRef.value?.search(searchQuery.value);
  }
}

function scheduleSearch(): void {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchTimer = null;
    runSearch();
  }, 200);
}

watch(searchQuery, () => scheduleSearch());

async function refreshMarkdownMarks(): Promise<void> {
  await nextTick();
  const host = bodyRef.value?.querySelector(".pv-markdown") as HTMLElement | null;
  const query = searchQuery.value.trim();
  if (!host || !query || kind.value !== "markdown") {
    markdownMarks.value = [];
    return;
  }
  const lower = query.toLowerCase();
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  const marks: HTMLElement[] = [];
  for (const node of nodes) {
    const text = node.textContent ?? "";
    const haystack = text.toLowerCase();
    let index = haystack.indexOf(lower);
    if (index < 0) continue;
    const fragment = document.createDocumentFragment();
    let from = 0;
    while (index >= 0) {
      if (index > from) fragment.appendChild(document.createTextNode(text.slice(from, index)));
      const mark = document.createElement("mark");
      mark.className = "pv-hit";
      mark.textContent = text.slice(index, index + query.length);
      fragment.appendChild(mark);
      marks.push(mark);
      from = index + query.length;
      index = haystack.indexOf(lower, from);
    }
    if (from < text.length) fragment.appendChild(document.createTextNode(text.slice(from)));
    node.parentNode?.replaceChild(fragment, node);
  }
  markdownMarks.value = marks;
}

function scrollElementIntoView(element: HTMLElement): void {
  const host = bodyRef.value;
  if (!host) return;
  if (typeof element.scrollIntoView === "function") {
    element.scrollIntoView({ block: "center" });
    return;
  }
  // 没有 scrollIntoView 的环境（测试 / 极简 webview）：退回到容器内滚动。
  host.scrollTop = Math.max(0, element.offsetTop - 60);
}

function scrollTo(selector: string): void {
  const host = bodyRef.value;
  if (!host) return;
  const element = host.querySelector(selector) as HTMLElement | null;
  if (element) scrollElementIntoView(element);
}

function jumpToHit(hit: PreviewSearchHit): void {
  activeHit.value = hit;
  if (kind.value === "pdf" && hit.page) {
    goPdfPage(hit.page);
    return;
  }
  if (hit.line && (kind.value === "code" || kind.value === "text")) {
    activeLine.value = hit.line;
    scrollTo('[data-line="' + hit.line + '"]');
    return;
  }
  if (hit.block !== undefined) {
    scrollTo('[data-block="' + hit.block + '"]');
    return;
  }
  if (hit.table !== undefined) {
    scrollTo('[data-table="' + hit.table + '"]');
    return;
  }
  if (hit.sheet !== undefined && hit.cell) {
    sheetName.value = hit.sheet;
    void nextTick(() => scrollTo('[data-cell="' + hit.sheet + "!" + hit.cell + '"]'));
    return;
  }
  if (hit.page !== undefined && kind.value === "pptx") {
    slidePage.value = hit.page;
  }
}

function gotoHit(index: number): void {
  const total = totalHits.value;
  if (!total) return;
  const next = ((index % total) + total) % total;
  activeHitIndex.value = next;
  if (kind.value === "markdown") {
    activeHit.value = null;
    const mark = markdownMarks.value[next];
    if (mark) scrollElementIntoView(mark);
    return;
  }
  const hit = searchHits.value[next];
  if (hit) jumpToHit(hit);
}

// ── 缩放 / 翻页 / 工作表 ──────────────────────────────────────────────────────

const zoomPercent = computed(() => Math.round(zoom.value * 100) + "%");
const zoomBounds = computed<[number, number]>(() => (isPanKind.value ? [0.25, 4] : [0.5, 2]));

function setZoom(value: number): void {
  const [min, max] = zoomBounds.value;
  zoom.value = Math.min(max, Math.max(min, Math.round(value * 100) / 100));
  savePosition({ zoom: zoom.value });
}

function zoomIn(): void {
  setZoom(zoomStep(zoom.value, 1, zoomBounds.value[0], zoomBounds.value[1]));
}

function zoomOut(): void {
  setZoom(zoomStep(zoom.value, -1, zoomBounds.value[0], zoomBounds.value[1]));
}

function resetZoom(): void {
  setZoom(1);
}

function useFit(): void {
  setZoom(imageFit.value || 1);
}

function goPdfPage(page: number): void {
  const max = pdfPages.value || 1;
  const next = Math.min(Math.max(1, Math.round(page || 1)), max);
  if (next === pdfPage.value) return;
  pdfPage.value = next;
  savePosition({ page: next, scrollTop: 0 });
}

const slideIndex = computed(() => {
  const slides = pptxView.value?.slides ?? [];
  if (!slides.length) return -1;
  return Math.max(0, slides.findIndex((slide) => slide.index === slidePage.value));
});

const slideTotal = computed(() => pptxView.value?.slides.length ?? 0);

function goSlide(delta: number): void {
  const slides = pptxView.value?.slides ?? [];
  if (!slides.length) return;
  const next = Math.min(Math.max(0, slideIndex.value + delta), slides.length - 1);
  slidePage.value = slides[next].index;
  savePosition({ page: slidePage.value });
}

function changeSheet(name: string): void {
  sheetName.value = name;
  savePosition({ sheet: name });
}

const currentSheet = computed(() => {
  const sheets = xlsxView.value?.sheets ?? [];
  return sheets.find((sheet) => sheet.name === sheetName.value) ?? sheets[0] ?? null;
});

const currentSlide = computed(() => {
  const slides = pptxView.value?.slides ?? [];
  return slides.find((slide) => slide.index === slidePage.value) ?? slides[0] ?? null;
});

// ── 复制 ─────────────────────────────────────────────────────────────────────

const copyText = computed(() => {
  const current = kind.value;
  if (current === "code" || current === "text") {
    return effectiveCodeView.value?.lines.map((line) => line.text).join("\n") ?? "";
  }
  if (current === "markdown") return markdownSource.value;
  if (current === "docx" && docxView.value) {
    const parts: string[] = docxView.value.blocks.map((block) => block.text);
    for (const table of docxView.value.tables) {
      for (const row of table.rows) parts.push(row.join("\t"));
    }
    return parts.join("\n");
  }
  if (current === "xlsx" && currentSheet.value) {
    return currentSheet.value.rows
      .map((row) => row.cells.map((cell) => cell?.display ?? "").join("\t"))
      .join("\n");
  }
  if (current === "pptx" && currentSlide.value) {
    const slide = currentSlide.value;
    const parts = [slide.title, ...slide.texts, ...slide.notes].filter(Boolean);
    for (const table of slide.tables) for (const row of table) parts.push(row.join("\t"));
    return parts.join("\n");
  }
  return "";
});

const canCopy = computed(() => supportsTextCopy(kind.value) && copyText.value.length > 0);

// ── 动作反馈（打开 / 下载 / 复制 的真实结果） ─────────────────────────────────

const actionNote = ref("");
let actionTimer: ReturnType<typeof setTimeout> | null = null;

function noteAction(text: string): void {
  actionNote.value = text;
  if (actionTimer) clearTimeout(actionTimer);
  actionTimer = setTimeout(() => {
    actionTimer = null;
    actionNote.value = "";
  }, 5_000);
}

function messageOf(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

async function copyContent(): Promise<void> {
  if (!copyText.value) {
    noteAction("没有可复制的内容");
    return;
  }
  try {
    await copyTextToClipboard(copyText.value);
    noteAction("已复制当前预览内容");
  } catch (error) {
    noteAction("复制失败：" + messageOf(error));
  }
}

async function openExternal(): Promise<void> {
  const result = await store.openInSystem(tabId.value);
  noteAction(result.note);
}

async function downloadCopy(): Promise<void> {
  const result = await store.downloadCopy(tabId.value);
  noteAction(result.note);
}

function retryTab(): void {
  store.retryTab(tabId.value);
}

// ── 文件更新检查（真实版本；打开期间轻量轮询 + 轮次结束后的 debounce） ─────────

let updateTimer: ReturnType<typeof setTimeout> | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

function scheduleUpdateCheck(): void {
  if (updateTimer) clearTimeout(updateTimer);
  updateTimer = setTimeout(() => {
    updateTimer = null;
    if (session.turnRunning) return;
    void store.checkForUpdate(tabId.value);
  }, FILE_UPDATE_CHECK_DELAY_MS);
}

watch(
  () => session.turnRunning,
  (running, previous) => {
    if (previous && !running) scheduleUpdateCheck();
  },
);

function startPolling(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (session.turnRunning) return; // 正在写的时候不查，避免读到半成品
    if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
    void store.checkForUpdate(tabId.value);
  }, FILE_UPDATE_POLL_MS);
}

// ── 生命周期 ─────────────────────────────────────────────────────────────────

onMounted(() => {
  store.ensureLoaded(tabId.value);
  restoreScroll();
  if (searchQuery.value.trim()) runSearch();
  // 之前读过这个标签（运行时还在）：挂载时先查一次真实版本，避免显示过期内容。
  if (runtime.value?.phase === "ready") scheduleUpdateCheck();
  startPolling();
});

watch(
  () => routeKey(),
  () => {
    flushScroll();
    restored = false;
    store.ensureLoaded(tabId.value);
    restoreScroll();
    initialisePanes();
    if (searchQuery.value.trim()) runSearch();
  },
);

function routeKey(): string {
  return tabId.value + "|" + kind.value;
}

watch(
  () => runtime.value?.phase,
  (phase) => {
    if (phase === "ready") restoreScroll();
  },
);

watch(
  () => props.full,
  () => {
    void nextTick(() => {
      const body = bodyRef.value;
      if (body) body.scrollTop = Math.max(0, Number(props.tab.position.scrollTop) || 0);
    });
  },
);

onBeforeUnmount(() => {
  flushScroll();
  if (scrollTimer) clearTimeout(scrollTimer);
  if (searchTimer) clearTimeout(searchTimer);
  if (updateTimer) clearTimeout(updateTimer);
  if (actionTimer) clearTimeout(actionTimer);
  if (pollTimer) clearInterval(pollTimer);
});

function onPdfPages(count: number): void {
  pdfPages.value = count;
  if (pdfPage.value > count && count > 0) {
    pdfPage.value = count;
    savePosition({ page: count });
  }
  // 页数就绪后补一次搜索（搜索框可能在 PDF 打开之前就输入了）。
  if (searchQuery.value.trim()) pdfViewerRef.value?.search(searchQuery.value);
}

function onPdfSearchResult(
  hits: PreviewSearchHit[],
  info: { truncated: boolean; scannedPages: number; totalPages: number },
): void {
  pdfSearchHits.value = hits;
  pdfSearchInfo.value = info;
  if (activeHitIndex.value >= hits.length) activeHitIndex.value = -1;
}
function onPdfScrolled(top: number): void {
  store.savePosition(tabId.value, { scrollTop: top });
}
function onImageFit(scale: number): void {
  imageFit.value = scale;
}
function onViewerScroll(top: number): void {
  store.savePosition(tabId.value, { scrollTop: top });
}

function onSlidePage(page: number): void {
  slidePage.value = page;
  savePosition({ page });
}
</script>

<template>
  <section class="pv-reader" :class="{ 'is-full': full }" :data-pv-kind="kind">
    <!-- 工具条：只放这一类文件真的有的能力；没有的不显示，绝不给假按钮 -->
    <div v-if="runtime?.phase === 'ready'" class="pv-tools">
      <div v-if="isSearchableKind(kind)" class="pv-tools-group pv-search">
        <input
          v-model="searchQuery"
          type="search"
          class="pv-search-input"
          placeholder="在当前文件里搜索"
          aria-label="搜索当前文件内容"
        />
        <span v-if="searchQuery.trim()" class="pv-count mono" data-pv-search-count>
          {{ totalHits ? (activeHitNumber || "—") + " / " + totalHits : "无匹配" }}
        </span>
        <span
          v-if="kind === 'pdf' && pdfSearchInfo?.truncated && searchQuery.trim()"
          class="pv-count mono"
          v-qio-tip="'只在前 ' + pdfSearchInfo.scannedPages + ' 页里搜索'"
        >仅前 {{ pdfSearchInfo.scannedPages }} 页</span>
        <button
          v-if="totalHits"
          type="button"
          class="pv-mini-btn"
          aria-label="上一个匹配"
          @click="gotoHit(activeHitIndex - 1)"
        >↑</button>
        <button
          v-if="totalHits"
          type="button"
          class="pv-mini-btn"
          aria-label="下一个匹配"
          @click="gotoHit(activeHitIndex + 1)"
        >↓</button>
      </div>
      <div v-if="supportsZoom(kind)" class="pv-tools-group">
        <button type="button" class="pv-mini-btn" aria-label="缩小" @click="zoomOut">−</button>
        <span class="pv-zoom mono">{{ zoomPercent }}</span>
        <button type="button" class="pv-mini-btn" aria-label="放大" @click="zoomIn">＋</button>
        <button v-if="kind === 'image'" type="button" class="pv-mini-btn" @click="useFit">适应</button>
        <button v-else type="button" class="pv-mini-btn" @click="resetZoom">100%</button>
      </div>
      <div v-if="kind === 'code' || kind === 'text'" class="pv-tools-group">
        <button type="button" class="pv-mini-btn" :aria-pressed="wrapLines" @click="wrapLines = !wrapLines">
          {{ wrapLines ? "不换行" : "自动换行" }}
        </button>
      </div>
      <div class="pv-tools-group pv-tools-right">
        <button v-if="canCopy" type="button" class="pv-mini-btn" @click="copyContent">复制</button>
        <button v-if="target.attachmentId || target.filePath" type="button" class="pv-mini-btn" @click="openExternal">打开</button>
        <button v-if="target.attachmentId" type="button" class="pv-mini-btn" @click="downloadCopy">下载</button>
      </div>
    </div>

    <!-- PDF / 幻灯片翻页：只在这两类出现 -->
    <div v-if="runtime?.phase === 'ready' && kind === 'pdf'" class="pv-pagenav">
      <button type="button" class="pv-mini-btn" :disabled="pdfPage <= 1" @click="goPdfPage(pdfPage - 1)">上一页</button>
      <span class="pv-pageno mono">{{ pdfPage }} / {{ pdfPages || "?" }}</span>
      <button type="button" class="pv-mini-btn" :disabled="pdfPages > 0 && pdfPage >= pdfPages" @click="goPdfPage(pdfPage + 1)">下一页</button>
      <input
        class="pv-page-jump mono"
        type="number"
        min="1"
        :max="pdfPages || 1"
        :value="pdfPage"
        aria-label="跳转到页码"
        @change="goPdfPage(Number(($event.target as HTMLInputElement).value))"
      />
    </div>
    <div v-if="runtime?.phase === 'ready' && kind === 'pptx' && slideTotal" class="pv-pagenav">
      <button type="button" class="pv-mini-btn" :disabled="slideIndex <= 0" @click="goSlide(-1)">上一张</button>
      <span class="pv-pageno mono">{{ slidePage }} / {{ slideTotal }}</span>
      <button type="button" class="pv-mini-btn" :disabled="slideIndex >= slideTotal - 1" @click="goSlide(1)">下一张</button>
    </div>

    <!-- 更新 / 失败提示：不抢标签，不隐藏「现在看到的是不是旧内容」 -->
    <p v-if="runtime?.notice" class="pv-notice" role="status" data-pv-updated>{{ runtime.notice }}</p>
    <p v-if="runtime?.updateError" class="pv-warn" role="alert" data-pv-update-error>
      {{ runtime.updateError }}
      <button type="button" class="pv-mini-btn" @click="retryTab">重试</button>
    </p>
    <p v-if="actionNote" class="pv-notice" role="status">{{ actionNote }}</p>
    <p v-if="viewError" class="pv-warn" role="alert">{{ viewError }}</p>

    <!-- 加载中 -->
    <div v-if="!runtime || runtime.phase === 'loading'" class="pv-state" aria-busy="true" data-pv-loading>
      <p>正在读取这个文件…</p>
    </div>

    <!-- 真实失败 / 不支持 / 音视频：说明范围与可用入口 -->
    <div v-else-if="runtime.phase === 'problem'" class="pv-problem" role="alert" data-pv-problem>
      <p class="pv-problem-title">{{ runtime.problem?.title }}</p>
      <p v-if="runtime.problem?.detail" class="pv-problem-detail">{{ runtime.problem.detail }}</p>
      <div class="pv-problem-actions">
        <button v-if="runtime.problem?.retryable" type="button" class="pv-mini-btn" @click="retryTab">重试</button>
        <button v-if="runtime.problem?.openable && (target.attachmentId || target.filePath)" type="button" class="pv-mini-btn" @click="openExternal">打开</button>
        <button v-if="runtime.problem?.downloadable && target.attachmentId" type="button" class="pv-mini-btn" @click="downloadCopy">下载</button>
        <button v-if="runtime.problem?.copyable && canCopy" type="button" class="pv-mini-btn" @click="copyContent">复制</button>
      </div>
      <p class="pv-problem-note">预览是只读的，不会修改原文件。</p>
    </div>

    <!-- 就绪：按类别分派 -->
    <div v-else class="pv-content">
      <div v-if="isPanKind" class="pv-content-pan">
        <PreviewImageViewer
          v-if="kind === 'image' && target.attachmentId"
          :attachment-id="target.attachmentId"
          :version="target.version"
          :title="target.title"
          :zoom="zoom"
          :initial-scroll-top="Number(tab.position.scrollTop) || 0"
          @scroll="onViewerScroll"
          @fit="onImageFit"
        />
        <PreviewPdfViewer
          v-else-if="kind === 'pdf' && target.attachmentId"
          ref="pdfViewerRef"
          :attachment-id="target.attachmentId"
          :version="target.version"
          :page="pdfPage"
          :zoom="zoom"
          :initial-scroll-top="Number(tab.position.scrollTop) || 0"
          :facts-data="pdfFactsData"
          @page-change="goPdfPage"
          @pages="onPdfPages"
          @scroll="onPdfScrolled"
          @search-result="onPdfSearchResult"
        />
      </div>
      <div
        v-else
        ref="bodyRef"
        class="pv-content-scroll"
        data-pv-scroll
        v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
        @scroll="onBodyScroll"
      >
        <PreviewTextPane
          v-if="isTextKind"
          :key="kind === 'markdown' ? 'md' + markdownMarkKey : 'text'"
          :mode="kind === 'markdown' ? 'markdown' : 'code'"
          :lines="effectiveCodeView?.lines.map((line) => line.text) ?? []"
          :markdown-source="markdownSource"
          :language-label="effectiveCodeView?.languageLabel ?? '纯文本'"
          :encoding="effectiveCodeView?.encoding ?? '未说明'"
          :total-lines="effectiveCodeView?.totalLines ?? 0"
          :truncated="Boolean(effectiveCodeView?.truncated)"
          :wrap="wrapLines"
          :query="searchQuery.trim()"
          :active-line="activeLine"
          :mark-key="markdownMarkKey"
          :style="{ '--pv-scale': zoom }"
        />
        <PreviewStructuredPane
          v-else-if="kind === 'docx' || kind === 'xlsx' || kind === 'pptx'"
          :kind="kind"
          :docx="docxView"
          :xlsx="xlsxView"
          :pptx="pptxView"
          :zoom="zoom"
          :page="slidePage"
          :sheet="sheetName"
          :query="searchQuery.trim()"
          :active-hit="activeHit"
          @sheet-change="changeSheet"
          @page-change="onSlidePage"
        />
        <p v-else class="pv-state">这个文件暂时没有可显示的正文</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.pv-reader {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.pv-tools {
  flex: none;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1) var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
}
.pv-tools-group {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}
.pv-tools-right {
  margin-left: auto;
}
.pv-search {
  flex: 1 1 150px;
  min-width: 110px;
}
.pv-search-input {
  flex: 1 1 auto;
  min-width: 0;
  height: 24px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  color: var(--text-primary);
  font: inherit;
  font-size: var(--fs-xs);
}
.pv-search-input:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.pv-count,
.pv-zoom {
  flex: none;
  color: var(--text-muted);
  font-size: var(--fs-tech);
}
.pv-mini-btn {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  padding: 1px var(--sp-2);
  cursor: pointer;
  max-width: 100%;
}
.pv-mini-btn:hover {
  color: var(--text-strong);
  border-color: var(--border-strong);
}
.pv-mini-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.pv-mini-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.pv-pagenav {
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
}
.pv-pageno {
  color: var(--text-secondary);
  font-size: var(--fs-tech);
}
.pv-page-jump {
  width: 56px;
  height: 22px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  color: var(--text-primary);
  font-size: var(--fs-tech);
  padding: 0 var(--sp-1);
}
.pv-notice {
  flex: none;
  margin: 0;
  padding: var(--sp-1) var(--sp-3);
  background: var(--success-soft);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.pv-warn {
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0;
  padding: var(--sp-1) var(--sp-3);
  background: var(--warning-soft);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.pv-state {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-5);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}
.pv-state p {
  margin: 0;
}
.pv-problem {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: var(--sp-5) var(--sp-4);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-2);
}
.pv-problem-title {
  margin: 0;
  color: var(--text-strong);
  font-family: var(--serif);
  font-size: var(--fs-md);
  line-height: var(--lh-tight);
  overflow-wrap: anywhere;
}
.pv-problem-detail {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  overflow-wrap: anywhere;
}
.pv-problem-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-top: var(--sp-1);
}
.pv-problem-note {
  margin: var(--sp-2) 0 0;
  color: var(--text-muted);
  font-size: var(--fs-tech);
}
.pv-content {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.pv-content-pan {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  padding: var(--sp-2) var(--sp-3) var(--sp-3);
  display: flex;
  flex-direction: column;
}
.pv-content-scroll {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: var(--sp-3) var(--sp-4) var(--sp-6);
}
.pv-reader.is-full .pv-content-scroll {
  padding-left: max(var(--sp-4), calc((100% - var(--measure)) / 2));
  padding-right: max(var(--sp-4), calc((100% - var(--measure)) / 2));
}
.pv-reader.is-full .pv-tools,
.pv-reader.is-full .pv-pagenav {
  padding-left: max(var(--sp-3), calc((100% - var(--measure)) / 2));
  padding-right: max(var(--sp-3), calc((100% - var(--measure)) / 2));
}
@media (prefers-reduced-motion: reduce) {
  .pv-reader *,
  .pv-reader *::before,
  .pv-reader *::after {
    transition: none !important;
    animation: none !important;
  }
}
</style>
