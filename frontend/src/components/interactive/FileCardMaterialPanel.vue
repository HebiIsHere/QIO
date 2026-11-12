<script setup lang="ts">
import { vQioTip } from "../../composables/useQioTip";
/**
 * 文件卡的源引用信息（R9 契约 §6 / §8）。
 *
 * 文件卡 = 提供材料，不等于授权修改源文件。这个面板只呈现**服务端事实**与用户说明：
 *   文件名 / 类型 / 引用 id / 来源 / 可用或缺失 / 用户说明。
 * 数据来源顺序：GET /api/attachments/{id}（权威）→ 卡片 meta（离线兜底，明确标注）。
 * #17：默认只显示文件名与可读状态；路径 / 来源 / 引用细节收进「材料详情」按需展开。
 * #08/#54 + 文案 12：参与控制只有卡片头部那一个三态入口，面板不再有旧材料开关与权限解释。
 *
 * 边界（必须诚实）：
 * * 删卡只删卡片与引用，源文件保留（由父级 commit，删除入口在卡片工具栏）；
 * * 复制卡片共享同一个 attachmentId（meta 里只存引用 id，不存内容）；
 * * 源文件缺失时提供「重新指定位置」：先让用户确认「这是同一个文件」，
 *   再带 confirmed_identity=true 重新定位；不自动采纳同名/同大小候选。
 */
import { computed, onMounted, ref, watch } from "vue";
import QConfirm from "../ui/QConfirm.vue";
import {
  getAttachment,
  humanSize,
  pickLocalSource,
  relocateAttachment,
  type AttachmentRef,
} from "../../services/attachments";
import {
  fileCardRefFromAttachment,
  fileCardRefFromMeta,
  mergeAttachmentIntoMeta,
  relocateConfirmDetail,
  relocateConfirmTitle,
} from "../../interactive/fileCardRef";

const props = withDefaults(
  defineProps<{
    cardId: string;
    meta?: Record<string, unknown> | null;
    /** 卡片上登记的附件 id（空串 = 还没有关联附件） */
    attachmentId?: string;
    /** 桌面壳：只有桌面端能「重新指定位置」拿到真实路径 */
    desktop?: boolean;
  }>(),
  { meta: null, attachmentId: "", desktop: false },
);

/** 由父级 commit（组件不直接改板面）：meta 补丁 + 操作说明 */
const emit = defineEmits<{
  (e: "patch-meta", meta: Record<string, unknown>, label: string): void;
}>();

const detail = ref<AttachmentRef | null>(null);
const loading = ref(false);
const loadError = ref("");
const actionError = ref("");
const actionNote = ref("");

/** 服务端事实优先；拿不到时用卡片 meta 兜底（并明确标注不是最新核对结果） */
const view = computed(() => {
  if (detail.value) return fileCardRefFromAttachment(detail.value);
  return fileCardRefFromMeta(props.meta);
});

const hasAttachment = computed(() => Boolean(props.attachmentId));
const stale = computed(() => !detail.value && !loading.value && hasAttachment.value);

async function refresh(): Promise<void> {
  const id = String(props.attachmentId ?? "").trim();
  if (!id) {
    detail.value = null;
    loadError.value = "";
    return;
  }
  loading.value = true;
  loadError.value = "";
  try {
    detail.value = await getAttachment(id);
  } catch (err) {
    loadError.value = "读取附件情况失败：" + ((err as Error)?.message || "未知原因");
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);
watch(
  () => props.attachmentId,
  () => {
    void refresh();
  },
);

/** 成功把服务端事实写回卡片 meta（只存引用与展示事实，不存文件内容） */
function patchMeta(next: AttachmentRef | null, label: string): void {
  if (!next) return;
  const merged = mergeAttachmentIntoMeta(props.meta ?? {}, next);
  emit("patch-meta", merged, label);
}

// --- 重新指定位置（缺失/位置待确认时） ------------------------------------
const relocateOpen = ref(false);
const relocatePath = ref("");
const relocating = ref(false);

async function startRelocate(): Promise<void> {
  actionError.value = "";
  actionNote.value = "";
  if (!props.desktop) {
    actionError.value =
      "浏览器里拿不到本地真实路径，无法重新指定位置；请重新上传这个文件（会作为新的工作区材料源文件）";
    return;
  }
  try {
    const picked = await pickLocalSource();
    if (!picked) return; // 用户取消：什么都不发生
    relocatePath.value = picked.path;
    relocateOpen.value = true;
  } catch (err) {
    actionError.value = "选择新位置失败：" + ((err as Error)?.message || "未知原因");
  }
}

async function confirmRelocate(): Promise<void> {
  const id = String(props.attachmentId ?? "").trim();
  if (!id || relocating.value) return;
  relocating.value = true;
  actionError.value = "";
  try {
    const updated = await relocateAttachment(id, relocatePath.value, { confirmedIdentity: true });
    detail.value = updated;
    patchMeta(updated, "重新指定源文件位置");
    actionNote.value = "已重新指定位置：" + updated.name + "（" + (updated.availability === "relocated" ? "已重新定位" : "可用性待核对") + "）";
    relocateOpen.value = false;
  } catch (err) {
    actionError.value = "重新指定位置失败：" + ((err as Error)?.message || "未知原因");
  } finally {
    relocating.value = false;
  }
}

function cancelRelocate(): void {
  relocateOpen.value = false;
  relocatePath.value = "";
}

const sizeText = computed(() => {
  const size = view.value?.sizeBytes;
  return size && size > 0 ? humanSize(size) : "";
});
</script>

<template>
  <div class="file-ref" data-im="file-card-ref" :data-card-id="cardId" :data-stale="stale ? '1' : '0'">
    <template v-if="hasAttachment && view">
      <p class="ref-name" data-im="file-card-name" v-qio-tip="view.name">
        <span class="kind-tag mono">{{ view.typeLabel }}</span>
        <span class="name">{{ view.name }}</span>
        <span v-if="sizeText" class="size mono">{{ sizeText }}</span>
      </p>
      <!-- #17：路径 / 来源 / 引用细节按需展开；默认只留文件名、可读状态与常用操作。 -->
      <details class="ref-details" data-im="file-card-details">
        <summary>材料详情（路径 / 来源 / 引用）</summary>
      <dl class="ref-facts">
        <dt>引用 id</dt>
        <dd class="mono" data-im="file-card-reference-id">{{ view.referenceId }}</dd>
        <dt>来源</dt>
        <dd data-im="file-card-source">
          {{ view.sourceLabel }}<template v-if="view.originLabel"> · {{ view.originLabel }}</template>
          <span class="note">{{ view.sourceNote }}</span>
        </dd>
        <dt>可用性</dt>
        <dd data-im="file-card-availability" :class="{ missing: view.missing }">{{ view.availabilityText }}</dd>
        <template v-if="view.userNote">
          <dt>用户说明</dt>
          <dd data-im="file-card-user-note">{{ view.userNote }}</dd>
        </template>
      </dl>
      </details>
      <p v-if="stale" class="stale-note" data-im="file-card-stale">
        服务端事实暂时取不到（上方信息来自卡片记录，不是最新核对结果）
      </p>
      <p v-if="loading" class="stale-note" role="status">正在向服务端核对这份材料…</p>

      <button
        v-if="view.missing"
        type="button"
        class="relocate-btn"
        data-im="file-card-relocate"
        @click="startRelocate"
      >重新指定位置</button>

      <p v-if="loadError" class="err" role="alert" data-im="file-card-error">{{ loadError }}</p>
      <p v-if="actionError" class="err" role="alert" data-im="file-card-action-error">{{ actionError }}</p>
      <p v-if="actionNote" class="ok" role="status" data-im="file-card-action-note">{{ actionNote }}</p>

      <QConfirm
        :open="relocateOpen"
        variant="layer"
        :title="relocateConfirmTitle()"
        :detail="relocateConfirmDetail(relocatePath)"
        confirm-text="是，继续引用这个文件"
        :tone="'normal'"
        @confirm="confirmRelocate"
        @cancel="cancelRelocate"
      />
      <p v-if="relocating" class="stale-note" role="status">正在重新指定位置…</p>
    </template>
    <p v-else-if="hasAttachment" class="stale-note" role="status">正在读取材料情况…</p>
    <p v-else class="missing-note" data-im="file-card-unlinked">
      还没有关联源文件：QIO 读不到它的正文。关联入口在卡片工具栏的「关联附件」。
    </p>
  </div>
</template>

<style scoped>
.file-ref {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-secondary);
}
.ref-name {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  margin: 0;
  flex-wrap: wrap;
}
.kind-tag {
  flex: 0 0 auto;
  font-size: 10px;
  color: var(--link);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  padding: 0 4px;
}
.name {
  font-size: 12.5px;
  color: var(--text-strong);
  overflow-wrap: anywhere;
  word-break: break-word;
}
.size { color: var(--text-muted); font-size: 10.5px; }
.ref-details { margin: 0; }
.ref-details > summary {
  cursor: pointer;
  color: var(--text-muted);
  font-size: 10.5px;
  list-style: none;
}
.ref-details > summary::-webkit-details-marker { display: none; }
.ref-details > summary::before { content: "▸ "; }
.ref-details[open] > summary::before { content: "▾ "; }
.ref-facts {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  gap: 2px 8px;
  margin: 0;
}
.ref-facts dt { color: var(--text-muted); font-size: 10.5px; }
.ref-facts dd { margin: 0; overflow-wrap: anywhere; word-break: break-word; }
.ref-facts dd.missing { color: var(--danger); }
.ref-facts .note { display: block; color: var(--text-muted); font-size: 10.5px; }
.stale-note { margin: 0; color: var(--text-muted); font-size: 10.5px; }
.missing-note { margin: 0; color: var(--text-muted); }
.material-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
}
.material-label { color: var(--text-secondary); font-size: 11px; }
.material-hint { margin: 0; color: var(--text-muted); font-size: 10px; line-height: 1.5; }
.relocate-btn {
  align-self: flex-start;
  font: inherit;
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  padding: 0 var(--sp-2);
  height: 22px;
  cursor: pointer;
}
.relocate-btn:hover { color: var(--text-strong); }
.relocate-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.err { margin: 0; color: var(--danger); font-size: 11px; }
.ok { margin: 0; color: var(--success, var(--link)); font-size: 11px; }
</style>
