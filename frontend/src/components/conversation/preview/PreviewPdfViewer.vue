<script setup lang="ts">
/**
 * 预览栏里的 PDF 阅读面：pdf.js **逐页渲染**（只读，翻页 / 缩放）。
 *
 * 与卡片内预览同一套硬约束：
 *   * 字节必须由调用方用带认证 fetch 取好（pdf.js 自己 GET 会 401）；
 *   * worker 走同源地址（渲染器模块内部处理，CSP 允许）；
 *   * 页面渲染任务可中止；换附件 / 卸载时 destroy 文档、清空 canvas；
 *   * 加密 / 损坏如实说明，不留空白、不假装渲染成功。
 *
 * pdfjs-dist 只在打开 PDF 时动态 import，不进首屏包。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { fetchAttachmentBytes } from "../../../services/attachments";
import { loadRenderer } from "../../../interactive/renderers";
import type { PdfFactsView, PdfHandle } from "../../../interactive/renderers/pdf";
import { searchPdfTextPages, type PreviewSearchHit } from "../../../conversation/preview";
import { vQioScrollbar } from "../../../composables/useQioScrollbars";

/** 逐页搜索上限：扫描件/超大 PDF 不能为了一个搜索把所有页都解一遍（诚实提示只搜了前 N 页）。 */
const PDF_SEARCH_MAX_PAGES = 120;

interface PdfModule {
  view: (value: unknown) => { ok: true; view: PdfFactsView } | { ok: false; reason: string };
  openPdf: (bytes: ArrayBuffer) => Promise<PdfHandle>;
}

const props = defineProps<{
  attachmentId: string;
  version?: string;
  /** 当前页码（1 起；由父级保存/恢复）。 */
  page: number;
  zoom: number;
  initialScrollTop: number;
  /** 后端给出的 PDF 事实（envelope.data），用来显示页数/标题/加密原因。 */
  factsData: Record<string, unknown> | null;
}>();

const emit = defineEmits<{
  "page-change": [page: number];
  pages: [count: number];
  facts: [facts: PdfFactsView];
  scroll: [top: number];
  "load-error": [message: string];
  "search-result": [
    hits: PreviewSearchHit[],
    info: { truncated: boolean; scannedPages: number; totalPages: number },
  ];
}>();

const canvasRef = ref<HTMLCanvasElement | null>(null);
const boxRef = ref<HTMLElement | null>(null);
const loading = ref(false);
const rendering = ref(false);
const error = ref("");
const facts = ref<PdfFactsView | null>(null);
const pages = ref(0);
const rendered = ref("");

let generation = 0;
let handle: PdfHandle | null = null;
let renderController: AbortController | null = null;
let bytesController: AbortController | null = null;
let searchController: AbortController | null = null;
let pendingSearch = "";

/** 逐页提取文字并搜索（reader 里的搜索框通过 defineExpose 调用）。 */
async function search(query: string): Promise<void> {
  pendingSearch = query;
  searchController?.abort();
  const pdf = handle;
  const needle = String(query || "").trim();
  if (!pdf || !needle) {
    emit("search-result", [], { truncated: false, scannedPages: 0, totalPages: pages.value });
    return;
  }
  const controller = new AbortController();
  searchController = controller;
  try {
    const maxPages = Math.min(pdf.pages, PDF_SEARCH_MAX_PAGES);
    const texts: string[] = [];
    for (let page = 1; page <= maxPages; page += 1) {
      if (controller.signal.aborted) return;
      texts.push(await pdf.pageText(page, controller.signal));
    }
    if (controller.signal.aborted) return;
    const result = searchPdfTextPages(texts, needle, {
      limit: 200,
      maxPages: PDF_SEARCH_MAX_PAGES,
    });
    emit("search-result", result.hits, {
      truncated: result.truncated,
      scannedPages: result.scannedPages,
      totalPages: result.totalPages,
    });
  } catch {
    if (!controller.signal.aborted) {
      emit("search-result", [], { truncated: false, scannedPages: 0, totalPages: pages.value });
    }
  }
}

function messageOf(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function clearCanvas(): void {
  const canvas = canvasRef.value;
  if (!canvas) return;
  try {
    canvas.getContext?.("2d")?.clearRect(0, 0, canvas.width, canvas.height);
  } catch {
    /* jsdom 里没有真实 2D 上下文：清不掉就算了 */
  }
  canvas.width = 0;
  canvas.height = 0;
}

async function release(pdf: PdfHandle | null): Promise<void> {
  renderController?.abort();
  renderController = null;
  searchController?.abort();
  searchController = null;
  bytesController?.abort();
  bytesController = null;
  clearCanvas();
  if (pdf) {
    try {
      await pdf.destroy();
    } catch {
      /* 已经销毁 / 中止：释放失败不该打断收起 */
    }
  }
}

function currentPage(): number {
  const max = pages.value || 1;
  return Math.min(Math.max(1, Math.round(props.page || 1)), max);
}

async function renderCurrent(mine: number): Promise<void> {
  const pdf = handle;
  const canvas = canvasRef.value;
  if (!pdf || !canvas || mine !== generation) return;
  const page = currentPage();
  if (page !== props.page) emit("page-change", page);
  const key = page + ":" + props.zoom;
  if (rendered.value === key) return;
  rendering.value = true;
  renderController?.abort();
  const controller = new AbortController();
  renderController = controller;
  try {
    await pdf.renderPage(page, canvas, props.zoom, controller.signal);
    if (mine === generation) rendered.value = key;
  } catch (err) {
    const name = err instanceof Error ? err.name : "";
    if (name !== "PdfAbortedError" && !controller.signal.aborted && mine === generation) {
      error.value = "这一页渲染失败：" + messageOf(err);
    }
  } finally {
    if (mine === generation) rendering.value = false;
  }
}

function restoreScroll(): void {
  void nextTick(() => {
    const box = boxRef.value;
    if (box) box.scrollTop = Math.max(0, props.initialScrollTop || 0);
  });
}

async function load(): Promise<void> {
  const mine = ++generation;
  rendered.value = "";
  error.value = "";
  loading.value = true;
  pages.value = 0;
  const previous = handle;
  handle = null;
  void release(previous);
  try {
    const module = await loadRenderer<PdfModule>("pdf");
    if (mine !== generation) return;
    if (props.factsData) {
      const parsed = module.view(props.factsData);
      if (parsed.ok) {
        facts.value = parsed.view;
        emit("facts", parsed.view);
        if (!parsed.view.renderable) {
          loading.value = false;
          error.value = parsed.view.reason || "这份 PDF 不能逐页渲染（加密或页数未知）";
          return;
        }
      }
    }
    const byteAbort = new AbortController();
    bytesController = byteAbort;
    const bytes = await fetchAttachmentBytes(props.attachmentId, { signal: byteAbort.signal });
    if (mine !== generation) return;
    const opened = await module.openPdf(bytes);
    if (mine !== generation) {
      await opened.destroy();
      return;
    }
    handle = opened;
    pages.value = opened.pages;
    emit("pages", opened.pages);
    loading.value = false;
    await nextTick();
    await renderCurrent(mine);
    restoreScroll();
    if (pendingSearch) void search(pendingSearch);
  } catch (err) {
    if (mine !== generation) return;
    if (isAbort(err)) return;
    loading.value = false;
    error.value = "读取 PDF 失败：" + messageOf(err);
    emit("load-error", error.value);
  }
}

function isAbort(error: unknown): boolean {
  const name = error instanceof Error ? error.name : "";
  return name === "AbortError" || name === "PdfAbortedError";
}

const shownPage = computed(() => currentPage());
const totalLabel = computed(() => (pages.value ? String(pages.value) : "?"));

watch(
  () => [props.attachmentId, props.version, props.factsData] as const,
  () => {
    void load();
  },
  { immediate: true },
);

watch(
  () => [props.page, props.zoom] as const,
  () => {
    if (handle) void renderCurrent(generation);
  },
);

function onScroll(): void {
  const box = boxRef.value;
  if (box) emit("scroll", Math.max(0, Math.round(box.scrollTop)));
}

onBeforeUnmount(() => {
  generation += 1;
  pendingSearch = "";
  const pdf = handle;
  handle = null;
  void release(pdf);
});

defineExpose({
  /** reader 的搜索框调用：等文字提取完成后通过 search-result 事件回传命中。 */
  search: (query: string) => {
    void search(query);
  },
});
</script>

<template>
  <div class="pv-pdf" data-pv-pdf>
    <p v-if="loading" class="pv-state" aria-busy="true">正在读取 PDF…</p>
    <div v-else-if="error" class="pv-state pv-state-error" role="alert">
      <p>{{ error }}</p>
      <button type="button" class="pv-mini-btn" @click="load">重试</button>
    </div>
    <div v-else class="pv-pdf-body">
      <div
        ref="boxRef"
        class="pv-canvas-pan"
        v-qio-scrollbar="{ policy: 'auto', axis: 'both' }"
        @scroll="onScroll"
      >
        <canvas ref="canvasRef" data-pv-pdf-canvas></canvas>
        <p v-if="rendering" class="pv-busy">正在渲染这一页…</p>
      </div>
      <p class="pv-fact">
        第 {{ shownPage }} / {{ totalLabel }} 页 · 缩放 {{ Math.round(zoom * 100) }}%
        <template v-if="facts">
          <template v-if="facts.title"> · {{ facts.title }}</template>
          · 可提取文本 {{ facts.textChars }} 字符
          <template v-if="facts.textChars === 0">（可能没有文字层，页面仍按原样渲染）</template>
        </template>
      </p>
    </div>
  </div>
</template>

<style scoped>
.pv-pdf {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
}
.pv-pdf-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  flex: 1 1 auto;
}
.pv-canvas-pan {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  display: flex;
  justify-content: center;
  align-items: flex-start;
}
canvas {
  display: block;
  max-width: none;
  background: var(--bg-surface);
}
.pv-busy {
  position: sticky;
  bottom: var(--sp-2);
  margin: 0;
  padding: 2px var(--sp-2);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  color: var(--text-muted);
  font-size: var(--fs-tech);
}
.pv-fact {
  flex: none;
  margin: var(--sp-1) 0 0;
  color: var(--text-muted);
  font-size: var(--fs-tech);
  overflow-wrap: anywhere;
}
.pv-state {
  margin: 0;
  padding: var(--sp-4);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}
.pv-state-error {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  align-items: flex-start;
  color: var(--text-secondary);
}
.pv-state-error p {
  margin: 0;
}
.pv-mini-btn {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  padding: 1px var(--sp-3);
  cursor: pointer;
}
.pv-mini-btn:hover {
  border-color: var(--border-strong);
}
.pv-mini-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
</style>
