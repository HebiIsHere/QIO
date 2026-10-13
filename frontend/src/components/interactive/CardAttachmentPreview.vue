<!--
  卡片附件预览（P2）：把「这张卡片上的附件能看见什么、点一下会发生什么」直接渲染出来。

  契约：docs/interactive-mode-contract.md §14.1 —— 有真实 attachmentId 才算有关联；
  没有关联时**不给假预览、不给假按钮**，如实说「QIO 读不到它的正文」。

  交付给 BoardCard.vue（由 P1 同事接线，见 _diag_qio/p2-report.md）：
    props: kind  = 卡片种类（file / image 才有内容）、
           meta  = 卡片 meta（读 meta.file / meta.image = {name, attachmentId, size}）、
           cardId（可选，用于 data-card-id 钩子）
    open    = 可注入的执行函数，默认就是 services/attachments 的 openAttachment
              （预留的测试缝：组件测试注入 spy，生产代码不传）

  行为：
  * 有关联：图片且是位图 → 带认证 fetch（fetchAttachmentContent）+ blob URL 缩略图；
    卸载 / 换附件时 revoke（生命周期在 interactive/attachmentPreview.ts 里，可单测）；
  * 有关联但不可预览 / 超限 / 加载失败 → 显示**原因**，不留空白；
  * 可执行 / 脚本类 → 「在文件夹中显示」并说明不自动运行的原因（桌面端）；
  * 浏览器 → 带认证 fetch 的 blob 新标签页（不能内联的类型改为下载）；
  * 没有关联 → 只显示诚实文案，没有按钮、没有缩略图。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { fetchAttachmentContent, isDesktopShell, openAttachment } from "../../services/attachments";
import type { AttachmentOpenResult, AttachmentRef } from "../../services/attachments";
import { createPreviewImageState, IMAGE_PREVIEW_MAX_BYTES } from "../../interactive/attachmentPreview";
import { attachmentPreviewPlan } from "../../interactive/attachmentPreview";
import type { CardKind } from "../../interactive/types";

const props = withDefaults(
  defineProps<{
    /** 卡片种类：只有 file / image 有附件预览 */
    kind: CardKind | string;
    /** 卡片 meta（meta.file / meta.image = { name, attachmentId, size }） */
    meta?: Record<string, unknown> | null;
    /** 可选：卡片 id，仅用于 data-card-id 钩子 */
    cardId?: string;
    /** 测试缝：默认就是被接线的 openAttachment（生产不传） */
    open?: (ref: AttachmentRef, options: { desktop: boolean }) => Promise<AttachmentOpenResult>;
    /**
     * 紧凑档（任务书 §6.3）：卡片进入阅读展开时，主体让给真实预览 ——
     * 这里只留**外部打开**这一个入口，不再重复铺缩略图、关联状态与大段说明。
     * 默认 false，既有调用方行为一字不变。
     */
    compact?: boolean;
  }>(),
  { meta: null, cardId: "", open: undefined, compact: false },
);

const attachKind = computed<"file" | "image" | null>(() =>
  props.kind === "file" || props.kind === "image" ? props.kind : null,
);

/** 桌面壳判定只在这里读一次：计划、缩略图、执行三条路共用同一个事实。 */
const desktop = ref(false);
if (typeof window !== "undefined") desktop.value = isDesktopShell();

const plan = computed(() =>
  attachmentPreviewPlan(props.kind, props.meta, { desktop: desktop.value }),
);

const linked = computed(() => plan.value.state === "linked" && Boolean(plan.value.attachmentId));
const runOpen = computed(
  () => props.open ?? ((ref: AttachmentRef, options: { desktop: boolean }) => openAttachment(ref, options)),
);

function attachmentRef(): AttachmentRef {
  return {
    id: plan.value.attachmentId,
    name: plan.value.displayName,
    sizeBytes: plan.value.sizeBytes ?? 0,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
  };
}

// ---------------------------------------------------------------------------
// 缩略图：带认证 fetch + blob URL；卸载 / 换附件时 revoke（生命周期在纯模块里）
// ---------------------------------------------------------------------------

const thumb = ref<HTMLImageElement | null>(null);
const previewTick = ref(0);

const previewImage = createPreviewImageState({
  fetchContent: (id: string) => fetchAttachmentContent(id),
  clearUrl: () => {
    if (thumb.value) thumb.value.removeAttribute("src");
  },
  onUpdate: () => {
    previewTick.value += 1;
  },
});

/** 只认「关联了哪个附件 / 要不要缩略图」：同一张卡片的其它字段变了不该重取字节。 */
const previewKey = computed(() => plan.value.attachmentId + "|" + (plan.value.imagePreviewable ? "1" : "0"));

watch(
  previewKey,
  () => {
    schedulePreview();
  },
  { immediate: true },
);

function schedulePreview(): void {
  if (!plan.value.imagePreviewable || !plan.value.attachmentId) {
    // release 只在**确实持有东西**时才做（避免挂载时白通知一轮）
    if (previewImage.url() || previewImage.loading() || previewImage.error()) previewImage.release();
    return;
  }
  previewImage.start(attachmentRef(), {
    sizeBytes: plan.value.sizeBytes,
    sizeLimitBytes: plan.value.imageSizeLimitBytes || IMAGE_PREVIEW_MAX_BYTES,
  });
}

const previewUrl = computed(() => (previewTick.value, previewImage.url()));
const previewError = computed(() => (previewTick.value, previewImage.error()));
const previewLoading = computed(() => (previewTick.value, previewImage.loading()));

/**
 * blob URL 只在**渲染之后**绑到 <img>：元素由 v-if="previewUrl" 控制，
 * 在状态回调里绑会撞上「元素还不存在」的竞态（这正是组件测试第一次抓到的 bug）。
 */
watch(
  previewUrl,
  (url) => {
    if (url) {
      if (thumb.value) thumb.value.setAttribute("src", url);
      return;
    }
    if (thumb.value) thumb.value.removeAttribute("src");
  },
  { flush: "post" },
);

/** 缩略图位置的两句说明：加载中 / 不可用（含失败与超限），都不留空白。 */
const previewNote = computed(() => {
  if (previewLoading.value) return "正在读取缩略图…";
  return previewError.value ?? plan.value.unavailableReason;
});

// ---------------------------------------------------------------------------
// 点一下：走既有 openAttachment（桌面原生 / 浏览器 blob），结果如实显示
// ---------------------------------------------------------------------------

const actionStatus = ref<{ tone: "busy" | "ok" | "error"; text: string } | null>(null);
const actionBusy = ref(false);

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function onOpen(): Promise<void> {
  if (!linked.value || actionBusy.value) return;
  const current = plan.value;
  if (current.action === "blocked" || current.action === "open_in_folder") {
    // 计划说这条路现在走不通：如实显示原因，不去执行一个注定报错的调用
    actionStatus.value = { tone: "error", text: current.openNote || "这个附件现在打不开" };
    return;
  }
  actionBusy.value = true;
  actionStatus.value = {
    tone: "busy",
    text: current.action === "reveal" ? "正在定位到文件夹…" : "正在打开「" + current.displayName + "」…",
  };
  try {
    const result = await runOpen.value(attachmentRef(), { desktop: desktop.value });
    actionStatus.value = { tone: "ok", text: result.note };
  } catch (error) {
    actionStatus.value = { tone: "error", text: "打不开：" + messageOf(error) };
  } finally {
    actionBusy.value = false;
  }
}

const actionText = computed(() => {
  const action = plan.value.action;
  if (action === "reveal" || action === "open_in_folder") return "在文件夹中显示";
  if (action === "download") return "下载副本";
  if (action === "blocked") return "无法打开（详见说明）";
  if (action === "open") return "打开";
  return "查看";
});

/** 真点下去会发生什么（计划里那句），或者在文件夹中显示会调用哪个原生命令。 */
const actionWillDo = computed(() => {
  const current = plan.value;
  if (current.action === "reveal") return "点一下会调用「在文件夹中显示」，不会运行这个文件";
  if (current.action === "open_in_folder") return "先在卡片上重新关联文件，才能「在文件夹中显示」";
  return current.openNote;
});

/** 可读标签：动作 + 对象名 + 一句话说明（可执行类说明「不自动运行」）。 */
const actionAriaLabel = computed(() => {
  const current = plan.value;
  if (!linked.value) return current.actionLabel;
  // 标签说清「会做什么」；可执行类在「在文件夹中显示」这一路要写明不自动运行
  const what = actionText.value === "在文件夹中显示" ? "（不自动运行）" : "";
  return current.actionLabel + what + "：" + actionWillDo.value;
});

const usageNote = computed(() => {
  const current = plan.value;
  if (!linked.value) return "";
  if (current.action === "reveal") return current.openNote;
  if (current.action === "open_in_folder") return current.openNote;
  if (current.action === "blocked") return current.openNote;
  if (current.action === "open") return "用系统默认程序打开；不会复制或上传内容";
  return current.openNote;
});

onBeforeUnmount(() => {
  // 卸载即释放：blob URL 不留给下一个页面（缩略图与「查看」用的是不同 URL，这里只管缩略图）
  previewImage.release();
});
</script>

<template>
  <div
    v-if="attachKind"
    class="card-attach-preview"
    data-qio-fit
    :class="{ compact }"
    :data-im="'card-attachment-preview'"
    :data-card-id="cardId"
    :data-state="linked ? 'linked' : 'unlinked'"
    :data-open-action="plan.action ?? 'none'"
  >
    <!-- 紧凑档不再重复关联状态：文件名与说明留在标题附近，这里只说「怎么外部打开」 -->
    <p
      v-if="!compact || !linked"
      class="attach-line"
      :class="linked ? 'is-linked' : 'is-missing'"
      :data-im="'card-attachment'"
    >{{ plan.statusText }}</p>

    <template v-if="linked">
      <div v-if="plan.imagePreviewable && !compact" class="thumb-box" :data-card-id="cardId">
        <img
          v-if="previewUrl"
          ref="thumb"
          class="thumb"
          :alt="'附件缩略图：' + plan.displayName"
          :data-im="'card-attachment-thumb'"
          :data-card-id="cardId"
        />
        <p class="thumb-note" :data-im="'card-attachment-preview-state'">{{ previewNote }}</p>
      </div>
      <p v-else-if="!compact" class="preview-note" :data-im="'card-attachment-preview-state'">{{ previewNote }}</p>

      <div class="actions">
        <button
          type="button"
          class="btn"
          :data-im="'card-attachment-open'"
          :data-card-id="cardId"
          :data-open-action="plan.action ?? 'none'"
          :aria-label="actionAriaLabel"
          :disabled="actionBusy"
          @click="onOpen"
        >{{ actionText }}</button>
      </div>
      <p v-if="usageNote && !compact" class="usage" :data-im="'card-attachment-open-note'">{{ usageNote }}</p>
      <p v-if="actionStatus" class="note" :class="'is-' + actionStatus.tone" :data-im="'card-attachment-open-status'" role="status">{{ actionStatus.text }}</p>
    </template>
  </div>
</template>

<style scoped>
/* 颜色一律走令牌；容器只负责排版，不抢卡片标题与连接点的位置（连接点在卡片四边，这里只占内容流） */
.card-attach-preview {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin-top: var(--sp-2);
  min-width: 0;
  max-width: 100%;
  font-size: var(--fs-xs);
}
.attach-line {
  margin: 0;
  line-height: var(--lh-tight);
  overflow-wrap: anywhere;
}
.attach-line.is-linked { color: var(--text-secondary); }
.attach-line.is-missing { color: var(--text-muted); }
.thumb-box { display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0; }
.thumb {
  display: block;
  max-width: 100%;
  /* 窄窗口里缩略图不能把卡片撑开：高度落在卡片正文区，超出由 .card-body 滚动接住 */
  max-height: 140px;
  width: auto;
  height: auto;
  object-fit: contain;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  background: var(--bg-inset);
}
.thumb-note, .preview-note, .usage, .note {
  margin: 0;
  line-height: var(--lh-tight);
  overflow-wrap: anywhere;
}
.thumb-note, .preview-note { color: var(--text-muted); }
.usage { color: var(--text-muted); }
.note.is-busy { color: var(--text-muted); }
.note.is-ok { color: var(--text-secondary); }
.note.is-error { color: var(--danger, var(--text-strong)); }
.actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; }
/* 紧凑档：一行放得下「外部打开」，缩略图与大段说明都不再出现（主体是预览） */
.card-attach-preview.compact { margin-top: 0; }
.btn {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-3);
  cursor: pointer;
}
.btn:hover { border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.btn:disabled { color: var(--text-muted); cursor: progress; }
</style>