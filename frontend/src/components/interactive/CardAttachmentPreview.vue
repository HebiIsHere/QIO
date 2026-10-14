<!--
  卡片附件预览（P2）：把「这张卡片上的附件能看见什么、点一下会发生什么」直接渲染出来。

  契约：docs/interactive-mode-contract.md §14.1 —— 有真实 attachmentId 才算有关联；
  没有关联时**不给假预览、不给假按钮**，如实说「QIO 读不到它的正文」。

  交付给 BoardCard.vue（由 P1 同事接线，见 _diag_qio/p2-report.md）：
    props: kind  = 卡片种类（只有 file 有附件内容 —— 图片统一是 kind="file" + meta.file）、
           meta  = 卡片 meta（读 meta.file = {name, attachmentId, size}）、
           cardId（可选，用于 data-card-id 钩子）
    open    = 可注入的执行函数，默认就是 services/attachments 的 openAttachment
              （预留的测试缝：组件测试注入 spy，生产代码不传）
    emits:  relink-attachment —— 封面变体里的失败入口「重新关联」交给卡片用既有 pickAttachment 处理

  行为：
  * 有关联：图片且是位图 → 带认证 fetch（fetchAttachmentContent）+ blob URL；
    卸载 / 换附件时 revoke（生命周期在 interactive/attachmentPreview.ts 里，可单测）；
  * 有关联但不可预览 / 超限 / 加载失败 / **解码失败** → 显示**原因**与可用入口，不留空白；
  * 可执行 / 脚本类 → 「在文件夹中显示」并说明不自动运行的原因（桌面端）；
  * 浏览器 → 带认证 fetch 的 blob 新标签页（不能内联的类型改为下载）；
  * 没有关联 → 只显示诚实文案，没有按钮、没有缩略图。

  本轮（互动模式图片展示简化）：
  * 封面变体承担**完整**的图片展示 —— object-fit: contain（等比完整、不裁切、留白走 --bg-inset）；
  * 不再有上下各 42% 的大面积蒙版，也不再重复文件名或常驻占位说明；
  * 图片层不吃指针事件（连接点 / 尺寸把手 / 工具栏不被它盖住），禁止原生拖动；
  * 入口文案与真实动作一致：action === "view" 且是可预览图片 →「查看原图」。
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
    /** 卡片种类：只有 file 有附件预览（图片统一是 kind="file" + meta.file） */
    kind: CardKind | string;
    /** 卡片 meta（meta.file = { name, attachmentId, size }） */
    meta?: Record<string, unknown> | null;
    /** 可选：卡片 id，仅用于 data-card-id 钩子 */
    cardId?: string;
    /** 测试缝：默认就是被接线的 openAttachment（生产不传） */
    open?: (ref: AttachmentRef, options: { desktop: boolean }) => Promise<AttachmentOpenResult>;
    /**
     * 紧凑档（任务书 §6.3）：卡片进入阅读展开时，主体让给真实预览 ——
     * 这里只留**外部打开**这一个入口，不再重复铺缩略图、关联状态与大段说明。
     * 默认 false，既有调用方行为一字不变。封面变体用同一口径：说明行让位。
     */
    compact?: boolean;
    /**
     * 呈现变体（2026-10-14 第六个问题，2026-10-15 收敛）：
     * - `default`：既有形态（关联状态 + 缩略图 + 查看按钮 + 说明）；
     * - `cover`：**常态图片展示** —— 图片等比完整铺在卡片上，只留一行真实说明 + 「查看原图」，
     *   供「附件是图片的文件卡」在常态与折叠态使用（编辑态不铺封面）。
     */
    variant?: "default" | "cover";
    /** 封面变体底部那一行补充说明（卡片正文；空串 = 没有真实说明，不渲染任何占位） */
    desc?: string;
  }>(),
  { meta: null, cardId: "", open: undefined, compact: false, variant: "default", desc: "" },
);

/** 「重新关联」：封面变体里的失败入口，交给卡片用既有 pickAttachment 处理（组件不自己选文件）。 */
const emit = defineEmits<{ (e: "relink-attachment"): void }>();

/** 附件只认 kind="file"：图片是文件卡的附件，不再是独立卡片类型。 */
const attachKind = computed<"file" | null>(() => (props.kind === "file" ? "file" : null));

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
// 缩略图 / 封面图：带认证 fetch + blob URL；卸载 / 换附件时 revoke（生命周期在纯模块里）
// ---------------------------------------------------------------------------

const thumb = ref<HTMLImageElement | null>(null);
/** 封面变体的图片（与缩略图共用同一条 blob 通道与生命周期：同一时刻只持有一个 URL） */
const coverImg = ref<HTMLImageElement | null>(null);
const previewTick = ref(0);
/**
 * 解码失败只可能是 <img> 的 error 事件（字节拿到了，但浏览器解不开）。
 * 声明在 preview 的 watch 之前：schedulePreview() 每一代都会把它复位（immediate watch 先跑）。
 */
const decodeFailed = ref(false);
const COVER_DECODE_FAILURE = "这张图片解不开：文件可能已损坏，或它不是真正的图片";

function onCoverDecodeError(): void {
  decodeFailed.value = true;
}

const previewImage = createPreviewImageState({
  fetchContent: (id: string) => fetchAttachmentContent(id),
  clearUrl: () => {
    if (thumb.value) thumb.value.removeAttribute("src");
    if (coverImg.value) coverImg.value.removeAttribute("src");
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
  // 每一代预览都从「没有解码失败」开始：换了附件 / 重试之后旧结论作废
  decodeFailed.value = false;
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
      if (coverImg.value) coverImg.value.setAttribute("src", url);
      return;
    }
    if (thumb.value) thumb.value.removeAttribute("src");
    if (coverImg.value) coverImg.value.removeAttribute("src");
  },
  { flush: "post" },
);

/** 缩略图位置的两句说明：加载中 / 不可用（含失败与超限），都不留空白。 */
const previewNote = computed(() => {
  if (previewLoading.value) return "正在读取缩略图…";
  return previewError.value ?? plan.value.unavailableReason;
});

// ---------------------------------------------------------------------------
// 封面变体的「解不开」与失败入口
//
// 图片**解码失败**（文件损坏 / 不是真正的图片）与「取字节失败 / 文件丢失 / 超限」是两回事，
// 但界面口径一致：说清真实原因，并给出**重试**（重走既有预览 blob 加载，不新建加载链路）、
// **查看原图**（既有 openAttachment）与**重新关联**（交给卡片）三个可用入口。
// ---------------------------------------------------------------------------

/** 原图片区域里要显示的状态：解码失败 > 取字节失败 / 超限 / 不可用 > 加载中。 */
const coverMessage = computed(() => {
  if (decodeFailed.value) return COVER_DECODE_FAILURE;
  if (previewUrl.value) return "";
  if (previewLoading.value) return "正在读取图片…";
  return previewError.value ?? plan.value.unavailableReason;
});

/** 失败（不是加载中）时给三个可用入口；没有真附件时不给「重试 / 查看原图」这类假入口。 */
const coverFailed = computed(
  () => decodeFailed.value || (!previewUrl.value && !previewLoading.value && Boolean(previewError.value)),
);
const coverActionsVisible = computed(() => coverFailed.value && (linked.value || decodeFailed.value));

/** 重试：重走既有预览 blob 加载（在途请求由 attachmentPreview.ts 的 host 按附件 id 复用）。 */
function retryPreview(): void {
  decodeFailed.value = false;
  schedulePreview();
}

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

/**
 * 入口文案与**真实动作**一致：
 * reveal / open_in_folder → 在文件夹中显示；download → 下载副本；blocked → 无法打开；
 * action === "view" 且附件是可预览图片 → 「查看原图」；其余 view 保持「查看」。
 */
const actionText = computed(() => {
  const action = plan.value.action;
  if (action === "reveal" || action === "open_in_folder") return "在文件夹中显示";
  if (action === "download") return "下载副本";
  if (action === "blocked") return "无法打开（详见说明）";
  if (action === "view") return plan.value.imagePreviewable ? "查看原图" : "查看";
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
  // 卸载即释放：blob URL 不留给下一个页面（缩略图与「查看」用的是不同 URL，这里只管预览字节）
  previewImage.release();
});
</script>

<template>
  <!--
    封面式（variant="cover"）：图片等比完整地铺在卡片上，文字退到上下两条**小条**里。
    这一层**不吃指针事件**（pointer-events: none）：卡片拖动 / 选中 / 双击手势照旧落在卡片上，
    只有按钮自己接事件。图片是装饰（aria-hidden + 空 alt）：卡片名由卡片头部承担，不在这里重复。
  -->
  <div
    v-if="attachKind && variant === 'cover'"
    class="card-cover"
    data-qio-fit
    :data-im="'card-cover'"
    :data-card-id="cardId"
    :data-state="linked ? 'linked' : 'unlinked'"
    :data-open-action="plan.action ?? 'none'"
    :data-preview-state="decodeFailed ? 'decode-failed' : previewUrl ? 'ready' : previewLoading ? 'loading' : 'unavailable'"
  >
    <img
      v-if="previewUrl && !decodeFailed"
      ref="coverImg"
      class="cover-img"
      alt=""
      aria-hidden="true"
      draggable="false"
      :data-im="'card-cover-img'"
      :data-card-id="cardId"
      @error="onCoverDecodeError"
    />
    <!-- 解不开 / 取不到字节：原因落在**原图片区域**中间，并给出三个可用入口 -->
    <div v-if="coverFailed" class="cover-failed" :data-im="'card-cover-failed'">
      <p class="cover-reason" :data-im="'card-attachment-preview-state'">{{ coverMessage }}</p>
      <div v-if="coverActionsVisible" class="cover-actions">
        <button type="button" class="btn" :data-im="'card-cover-retry'" @click.stop="retryPreview">重试</button>
        <button
          v-if="linked"
          type="button"
          class="btn"
          :data-im="'card-cover-original'"
          :data-open-action="plan.action ?? 'none'"
          :disabled="actionBusy"
          @click.stop="onOpen"
        >{{ actionText }}</button>
        <button
          type="button"
          class="btn"
          :data-im="'card-cover-relink'"
          @click.stop="emit('relink-attachment')"
        >重新关联</button>
      </div>
    </div>
    <p
      v-else-if="!previewUrl && coverMessage"
      class="cover-loading"
      :data-im="'card-attachment-preview-state'"
    >{{ coverMessage }}</p>

    <!-- 底部一条小蒙版：说明与「查看原图」贴在这一条上（不再盖住半张图） -->
    <div class="cover-foot">
      <p
        v-if="actionStatus"
        class="cover-status"
        :class="'is-' + actionStatus.tone"
        :data-im="'card-attachment-open-status'"
        role="status"
      >{{ actionStatus.text }}</p>
      <div class="cover-foot-row">
        <p v-if="desc && !compact" class="cover-desc" :data-im="'card-cover-desc'" :title="desc">{{ desc }}</p>
        <button
          v-if="linked && !coverFailed"
          type="button"
          class="btn cover-open"
          :data-im="'card-cover-open'"
          :data-card-id="cardId"
          :data-open-action="plan.action ?? 'none'"
          :aria-label="actionAriaLabel"
          :disabled="actionBusy"
          @click.stop="onOpen"
        >{{ actionText }}</button>
      </div>
    </div>
  </div>

  <div
    v-else-if="attachKind"
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

/* --- 封面式：图片是**常态展示主体**（本轮：完整等比展示 + 小条蒙版） ------------------ */
/*
  这一层是**背景**：position: absolute 填满卡片（.card 本身是定位元素），
  border-radius: inherit 让它跟着卡片圆角；overflow: hidden 保证不产生任何滚动条；
  pointer-events: none 保证卡片拖动 / 选中 / 双击手势照旧落在卡片上，只有按钮自己接事件。
  留白用既有令牌 --bg-inset（图片比例与卡片不同时等比完整显示，四周留白）。
*/
.card-cover {
  position: absolute;
  inset: 0;
  z-index: 0;
  border-radius: inherit;
  overflow: hidden;
  pointer-events: none;
  background: var(--bg-inset);
}
.cover-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  /* 等比完整显示：不拉伸、不裁切；卡片与图片比例不同时留白 */
  object-fit: contain;
  display: block;
  /* 图片层是背景：不吃指针事件（连接点 / 尺寸把手 / 工具栏不被它盖住），也不做原生拖动与选中 */
  pointer-events: none;
  user-select: none;
  -webkit-user-drag: none;
}
/*
  失败 / 解不开：原因与可用入口落在**原图片区域**中间。
  文字只在自己身后带一条窄蒙版（token 派生，不写死色值），不再有覆盖整张图的大面积蒙版。
*/
.cover-failed {
  position: absolute;
  left: var(--sp-2);
  right: var(--sp-2);
  top: var(--sp-2);
  bottom: calc(var(--sp-2) + 54px);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  text-align: center;
}
.cover-reason {
  margin: 0;
  max-width: 100%;
  padding: 2px var(--sp-2);
  border-radius: var(--r-xs);
  background: color-mix(in srgb, var(--bg-surface) 88%, transparent);
  font-size: var(--fs-xs);
  line-height: var(--lh-tight);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.cover-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); justify-content: center; }
.cover-actions .btn { pointer-events: auto; }
.cover-loading {
  position: absolute;
  left: var(--sp-3);
  right: var(--sp-3);
  top: 50%;
  transform: translateY(-50%);
  margin: 0;
  padding: 2px var(--sp-2);
  border-radius: var(--r-xs);
  background: color-mix(in srgb, var(--bg-surface) 88%, transparent);
  font-size: var(--fs-xs);
  color: var(--text-muted);
  text-align: center;
  overflow-wrap: anywhere;
}
/*
  底部小条：一行真实说明 + 「查看原图」。
  蒙版只贴在这一条后面（linear-gradient 落在这一条的盒子里），卡片页脚另有一条自己的窄蒙版（BoardCard）。
*/
.cover-foot {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(var(--sp-2) + 22px);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-3);
  background: linear-gradient(to top, color-mix(in srgb, var(--bg-surface) 92%, transparent), transparent);
  pointer-events: none;
}
.cover-foot-row { display: flex; align-items: center; gap: var(--sp-2); min-width: 0; }
.cover-desc {
  flex: 1 1 auto;
  min-width: 0;
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cover-open { flex: none; margin-left: auto; pointer-events: auto; }
.cover-status {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-tight);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cover-status.is-busy { color: var(--text-muted); }
.cover-status.is-ok { color: var(--text-secondary); }
.cover-status.is-error { color: var(--danger, var(--text-strong)); }
</style>
