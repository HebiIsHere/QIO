<script setup lang="ts">
/**
 * 预览栏里的图片阅读面（只读）：等比显示、缩放、内部平移。
 *
 * 资源纪律：字节走既有带认证 fetch，blob URL 在换附件 / 卸载时 **revoke**；
 * 迟到的旧请求按代次丢弃；解码失败与读取失败分别如实说明并给重试，
 * 不留空白、不假装展示成功。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { fetchAttachmentContent } from "../../../services/attachments";
import { fitScale } from "../../../interactive/renderers/image";
import { vQioScrollbar } from "../../../composables/useQioScrollbars";

const props = defineProps<{
  attachmentId: string;
  version?: string;
  title: string;
  zoom: number;
  initialScrollTop: number;
}>();

const emit = defineEmits<{
  scroll: [top: number];
  fit: [scale: number];
  facts: [{ width: number; height: number; bytes: number }];
}>();

const panRef = ref<HTMLElement | null>(null);
const imgRef = ref<HTMLImageElement | null>(null);
const url = ref("");
const loading = ref(false);
const error = ref("");
const decodeFailed = ref(false);
const natural = ref<{ width: number; height: number } | null>(null);
const bytes = ref(0);

let generation = 0;
let objectUrl = "";

function revoke(): void {
  if (objectUrl) {
    try {
      URL.revokeObjectURL(objectUrl);
    } catch {
      /* 已释放过就算了：释放失败不该影响阅读 */
    }
    objectUrl = "";
  }
  url.value = "";
}

function releaseImage(): void {
  generation += 1;
  revoke();
  if (imgRef.value) imgRef.value.removeAttribute("src");
}

function messageOf(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

async function load(): Promise<void> {
  releaseImage();
  const mine = generation;
  decodeFailed.value = false;
  error.value = "";
  natural.value = null;
  bytes.value = 0;
  if (!props.attachmentId) {
    error.value = "这个预览没有可读取的附件";
    return;
  }
  loading.value = true;
  try {
    const blob = await fetchAttachmentContent(props.attachmentId);
    if (mine !== generation) return; // 换过附件 / 已卸载：迟到的字节直接丢弃
    const create = (URL as unknown as { createObjectURL?: (value: Blob) => string }).createObjectURL;
    if (typeof create !== "function") {
      error.value = "这个环境不支持 Blob 图片预览（可以打开或下载副本）";
      return;
    }
    objectUrl = create(blob);
    bytes.value = blob.size;
    url.value = objectUrl;
  } catch (err) {
    if (mine !== generation) return;
    error.value = "读取图片失败：" + messageOf(err);
  } finally {
    if (mine === generation) loading.value = false;
  }
}

function onImageLoad(): void {
  const img = imgRef.value;
  if (!img) return;
  const width = img.naturalWidth || 0;
  const height = img.naturalHeight || 0;
  natural.value = { width, height };
  emit("facts", { width, height, bytes: bytes.value });
  const box = panRef.value;
  if (box) emit("fit", fitScale({ width, height }, { width: box.clientWidth, height: box.clientHeight }));
  restoreScroll();
}

function onImageError(): void {
  decodeFailed.value = true;
}

/** 图片的实际显示宽度：按原始像素 × 缩放；拿不到原始尺寸时退回容器宽度。 */
const imageStyle = computed(() => {
  const size = natural.value;
  if (!size || !size.width) return { maxWidth: "100%", maxHeight: "100%" };
  return { width: Math.max(1, Math.round(size.width * props.zoom)) + "px", maxWidth: "none" };
});

function onScroll(): void {
  const box = panRef.value;
  if (box) emit("scroll", Math.max(0, Math.round(box.scrollTop)));
}

let restoredFor = "";
function restoreScroll(): void {
  const key = props.attachmentId + "|" + (props.version ?? "");
  if (restoredFor === key) return;
  restoredFor = key;
  void nextTick(() => {
    const box = panRef.value;
    if (box) box.scrollTop = Math.max(0, props.initialScrollTop || 0);
  });
}

watch(
  () => [props.attachmentId, props.version] as const,
  () => {
    restoredFor = "";
    void load();
  },
  { immediate: true },
);

watch(
  () => props.initialScrollTop,
  (top) => {
    const box = panRef.value;
    if (box && Math.abs(box.scrollTop - top) > 2) box.scrollTop = Math.max(0, top);
  },
);

onMounted(() => {
  restoreScroll();
});

onBeforeUnmount(() => {
  releaseImage();
});
</script>

<template>
  <div class="pv-image">
    <p v-if="loading" class="pv-state" aria-busy="true">正在读取图片…</p>
    <div v-else-if="decodeFailed" class="pv-state pv-state-error" role="alert">
      <p>这张图片解不开：文件可能已损坏，或它不是真正的图片</p>
      <button type="button" class="pv-mini-btn" @click="load">重试</button>
    </div>
    <div v-else-if="error" class="pv-state pv-state-error" role="alert">
      <p>{{ error }}</p>
      <button type="button" class="pv-mini-btn" @click="load">重试</button>
    </div>
    <div
      v-else-if="url"
      ref="panRef"
      class="pv-pan"
      data-pv-image-pan
      v-qio-scrollbar="{ policy: 'auto', axis: 'both' }"
      @scroll="onScroll"
    >
      <img
        ref="imgRef"
        class="pv-image-el"
        :src="url"
        :alt="'图片预览：' + title"
        :style="imageStyle"
        draggable="false"
        @load="onImageLoad"
        @error="onImageError"
      />
    </div>
    <p v-else class="pv-state">这张图片暂时无法显示</p>
    <p v-if="natural" class="pv-fact pv-image-facts">
      {{ natural.width }}×{{ natural.height }} 像素<template v-if="bytes"> · {{ bytes.toLocaleString() }} 字节</template> · 缩放 {{ Math.round(zoom * 100) }}%
    </p>
  </div>
</template>

<style scoped>
.pv-image {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
}
.pv-pan {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
}
.pv-image-el {
  display: block;
  margin: 0 auto;
  height: auto;
  object-fit: contain;
  user-select: none;
  -webkit-user-drag: none;
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
  border-radius: var(--r-xs);
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
