<script setup lang="ts">
/**
 * 用途分配：每个用途实际用哪条连接的哪个模型。
 *
 * 语义（_STG-COMMON.md §6.2，别处已定，这里只做界面）：
 * - 先默认模型，各用途按需单独指定；未指定 = 跟随默认；
 * - 换默认模型**不覆盖**显式指定；「恢复跟随默认」才清掉覆盖；
 * - video / audio / embedding 显示为「沿用现有处理方式」，不继承默认对话模型；
 *   embedding 显式换外部模型时展示维度影响并复用中央确认（QConfirm）；
 * - research 如实标注尚未接通独立调用路径；
 * - main-chat 与 general-chat 日常入口合并成一行显示（调用路径已核实等价），
 *   但两个 key 都保留在数据里，保存时一起写。
 */
import { computed, ref } from "vue";
import CandidatePicker from "./CandidatePicker.vue";
import QConfirm from "../ui/QConfirm.vue";
import type { CatalogResponse, ModelConnection, UsageAssignment } from "../../services/modelCatalog";

const props = defineProps<{
  connections: ModelConnection[];
  catalog: CatalogResponse | null;
  /** 正在保存的用途（合并行用主 key 标记） */
  busyKey?: string | null;
  /** 每个用途上一次保存失败的原因 */
  errors?: Record<string, string>;
}>();
const emit = defineEmits<{
  assign: [keys: string[], target: { credentialId: string; modelId: string } | null];
}>();

const GROUP_LABELS: Record<string, string> = {
  primary: "日常入口",
  special: "专门能力",
  background: "后台处理",
  legacy: "历史自定义标签",
};
const GROUP_ORDER = ["primary", "special", "background", "legacy"];

interface Row {
  /** 保存时一起写的用途 key（日常对话两个 key 合并为一行） */
  keys: string[];
  primary: UsageAssignment;
  label: string;
  subtitle: string;
  kind: string;
  group: string;
  note: string;
  daily: boolean;
}

const rows = computed<Row[]>(() => {
  const usages = props.catalog?.usages ?? [];
  const merged = new Set<string>();
  const out: Row[] = [];
  for (const usage of usages) {
    if (merged.has(usage.usageKey)) continue;
    if (usage.usageKey === "main-chat") {
      const general = usages.find((u) => u.usageKey === "general-chat") ?? null;
      if (general) merged.add("general-chat");
      merged.add("main-chat");
      out.push({
        keys: general ? ["main-chat", "general-chat"] : ["main-chat"],
        primary: usage,
        label: "日常对话",
        subtitle: general ? "主对话 / 通用对话合并显示（同一条调用路径）" : "主对话",
        kind: usage.kind,
        group: usage.group,
        note: usage.note,
        daily: usage.daily,
      });
      continue;
    }
    out.push({
      keys: [usage.usageKey],
      primary: usage,
      label: usage.label,
      subtitle: usage.kind === "vision" ? "看图 / 视觉" : "",
      kind: usage.kind,
      group: usage.group,
      note: usage.note,
      daily: usage.daily,
    });
  }
  // 主区域优先：daily 在前，组内保持后端顺序（后端顺序即产品顺序）。
  return out;
});

const groupedRows = computed(() => {
  const result: { group: string; label: string; rows: Row[] }[] = [];
  for (const group of GROUP_ORDER) {
    const items = rows.value.filter((row) => row.group === group);
    if (items.length) result.push({ group, label: GROUP_LABELS[group] ?? group, rows: items });
  }
  return result;
});

const expanded = ref<string | null>(null);
const pendingEmbedding = ref<{ row: Row; target: { credentialId: string; modelId: string } } | null>(null);

function toggle(row: Row) {
  const key = row.keys[0];
  expanded.value = expanded.value === key ? null : key;
}

function pick(row: Row, target: { credentialId: string; modelId: string } | null) {
  expanded.value = null;
  if (row.kind === "embedding" && target) {
    // 换外部嵌入模型会改变向量维度：先让用户确认影响，再保存。
    pendingEmbedding.value = { row, target };
    return;
  }
  emit("assign", row.keys, target);
}

function confirmEmbedding() {
  const pending = pendingEmbedding.value;
  pendingEmbedding.value = null;
  if (pending) emit("assign", pending.row.keys, pending.target);
}

function isFollowing(row: Row): boolean {
  return row.primary.followsDefault;
}

function resolvedText(row: Row): string {
  if (row.primary.kind === "video" || row.primary.kind === "audio") {
    return row.primary.resolved
      ? row.primary.resolved.credentialName + " · " + row.primary.resolved.modelId
      : "沿用现有处理方式（不继承默认对话模型）";
  }
  return row.primary.resolved
    ? row.primary.resolved.credentialName + " · " + row.primary.resolved.modelId
    : "尚未设置";
}

function statusText(row: Row): string {
  if (!row.primary.resolved) {
    return row.primary.kind === "embedding" ? "沿用现有本地 / 远程方案" : "—";
  }
  return row.primary.resolved.usable ? "当前可用" : "当前不可用";
}

function statusTone(row: Row): string {
  if (!row.primary.resolved) return "quiet";
  return row.primary.resolved.usable ? "ok" : "err";
}

function badgeOf(row: Row): { text: string; tone: string } | null {
  if (row.kind === "video" || row.kind === "audio" || row.kind === "embedding") {
    return { text: "沿用现有处理方式", tone: "quiet" };
  }
  if (row.kind === "vision") return { text: "需要能读图的模型", tone: "quiet" };
  if (row.primary.usageKey === "research") return { text: "未接通独立调用路径", tone: "quiet" };
  return null;
}

const usageError = computed(() => props.errors ?? {});
</script>

<template>
  <section class="section" aria-labelledby="usage-title">
    <header class="section__head">
      <div>
        <h3 id="usage-title" class="section__title">用途分配</h3>
        <p class="section__desc">
          每个用途实际用哪条连接的哪个模型。未单独指定时跟随默认模型；换默认模型不会覆盖你单独指定的用途。
        </p>
      </div>
    </header>

    <div v-for="group in groupedRows" :key="group.group" class="group">
      <h4 class="group__title">{{ group.label }}</h4>
      <div v-for="row in group.rows" :key="row.keys[0]" class="row">
        <div class="row__main">
          <div class="row__label">
            <span class="row__name">{{ row.label }}</span>
            <span v-if="row.subtitle" class="row__sub">{{ row.subtitle }}</span>
            <span v-if="isFollowing(row)" class="qio-state quiet">跟随默认</span>
            <span v-for="badge in [badgeOf(row)].filter(Boolean)" :key="badge!.text" class="qio-state" :class="badge!.tone">
              {{ badge!.text }}
            </span>
          </div>
          <div class="row__target mono">
            <span>{{ resolvedText(row) }}</span>
            <span class="qio-state" :class="statusTone(row)">{{ statusText(row) }}</span>
            <span v-if="row.primary.resolved && !row.primary.resolved.usable && row.primary.resolved.unavailableReason" class="row__reason">
              不可用：{{ row.primary.resolved.unavailableReason }}
            </span>
          </div>
          <p v-if="row.note" class="row__note">{{ row.note }}</p>
          <p v-if="usageError[row.keys[0]]" class="qio-feedback err" role="alert">
            {{ usageError[row.keys[0]] }}
          </p>
          <p v-if="busyKey === row.keys[0]" class="row__note" role="status">正在保存…</p>
        </div>
        <div class="row__actions">
          <button
            type="button"
            class="qio-btn mini"
            :aria-expanded="expanded === row.keys[0]"
            :aria-label="'为' + row.label + '指定模型'"
            @click="toggle(row)"
          >
            {{ expanded === row.keys[0] ? "收起候选" : "指定模型" }}
          </button>
          <button
            v-if="!isFollowing(row)"
            type="button"
            class="qio-btn mini quiet"
            :aria-label="'恢复' + row.label + '跟随默认'"
            @click="emit('assign', row.keys, null)"
          >
            恢复跟随默认
          </button>
        </div>
        <CandidatePicker
          v-if="expanded === row.keys[0]"
          :connections="connections"
          :selected="row.primary.credentialId && row.primary.modelId ? { credentialId: row.primary.credentialId, modelId: row.primary.modelId } : null"
          :allow-inherit="!isFollowing(row)"
          :usage-label="row.label"
          @pick="pick(row, $event)"
          @cancel="expanded = null"
        />
      </div>
    </div>

    <p v-if="!groupedRows.length" class="muted">正在加载用途分配…</p>

    <QConfirm
      :open="!!pendingEmbedding"
      title="更换嵌入模型会改变向量维度"
      detail="新的嵌入模型维度可能与现有索引不一致：既有向量需要重建，否则检索结果会不可靠。这不会影响对话模型。确认把这个用途换成所选模型吗？"
      confirm-text="确认更换"
      variant="layer"
      @confirm="confirmEmbedding"
      @cancel="pendingEmbedding = null"
    />
  </section>
</template>

<style scoped>
.section { display: flex; flex-direction: column; gap: 8px; }
.section__head { display: flex; align-items: flex-start; gap: 12px; justify-content: space-between; }
.section__title { margin: 0; font-size: var(--fs-sm); color: var(--text-strong); }
.section__desc { margin: 2px 0 0; font-size: 10.5px; color: var(--text-muted); line-height: 1.5; }
.group { display: flex; flex-direction: column; gap: 6px; }
.group__title { margin: 6px 0 0; font-size: 10.5px; color: var(--text-muted); font-weight: 600; letter-spacing: .04em; }
.row {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start;
  padding: 8px 10px; border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius); background: var(--bg-elevated);
}
.row__main { flex: 1; min-width: 240px; display: flex; flex-direction: column; gap: 4px; }
.row__label { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.row__name { font-size: var(--fs-xs); color: var(--text-strong); font-weight: 600; }
.row__sub { font-size: 10px; color: var(--text-muted); }
.row__target { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 10.5px; color: var(--text-secondary); }
.row__reason { flex-basis: 100%; font-size: 10px; color: var(--warning); }
.row__note { margin: 0; font-size: 10px; color: var(--text-muted); line-height: 1.5; }
.row__actions { display: flex; gap: 6px; flex-wrap: wrap; }
.muted { margin: 0; font-size: 10.5px; color: var(--text-muted); }
.qio-state.ok { color: var(--success); background: var(--success-soft); border-color: var(--success-soft); }
.qio-state.err { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-soft); }
.qio-state.quiet { color: var(--text-muted); border-color: var(--border-subtle); }
</style>
