<script setup lang="ts">
/**
 * 用途分配：每个用途实际用哪条连接的哪个模型。
 *
 * 语义（_STG-COMMON.md §6.2 + 发布第二轮 B，别处已定，这里只做界面）：
 * - 先默认模型，各用途按需单独指定；未指定 = 跟随默认；
 * - **只有真正参与调用的用途**才提供独立模型选择（main-chat/general-chat 合并行、
 *   subagent、vision、记忆整理 / 知识提炼 / 实体提炼）。判断来自后端的
 *   delivery === "model" 与 assignmentEffective，前端不自己再猜一遍调用路径；
 * - 没有独立调用路径的用途（研究 / 视频 / 语音 / 嵌入 / 写代码 / 历史自定义标签）
 *   只读展示一句话当前行为，不再画成可用选择器；
 * - 历史配置继续可读，如实标「历史配置（未生效）」，并保留「清除历史覆盖」入口
 *   （不静默删除、不冒充当前实际使用）；
 * - 候选按当前用途判断能力（服务端算），打开候选时按 usage_key 取一次只读目录。
 */
import { computed, ref } from "vue";
import CandidatePicker from "./CandidatePicker.vue";
import {
  modelCatalogApi,
  modelErrorMessage,
  type CatalogResponse,
  type EffectiveTarget,
  type ModelConnection,
  type ResolvedTarget,
  type UsageAssignment,
} from "../../services/modelCatalog";

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
  /** 这一行包含的全部用途（合并行 = main-chat + general-chat） */
  usages: UsageAssignment[];
  /** 后端算好的真实生效目标；只有合并行会有（老后端可能是 null） */
  effective: EffectiveTarget | null;
  label: string;
  subtitle: string;
  kind: string;
  group: string;
  note: string;
  daily: boolean;
  /** 独立分配会不会真的生效（后端事实） */
  assignmentEffective: boolean;
  /** 存在不会生效的历史覆盖 */
  historical: boolean;
  historicalTarget: ResolvedTarget | null;
  delivery: "model" | "existing" | "unsupported";
}

function effectiveOf(usage: UsageAssignment): boolean {
  // 老后端不返回 → 按「会生效」处理（不把正常用途降级成只读）。
  return usage.assignmentEffective !== false;
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
        usages: general ? [usage, general] : [usage],
        // 后端用与运行时同一份 resolve_shared_target 算出的真实生效目标。
        effective: usage.effective ?? general?.effective ?? null,
        label: "日常对话",
        subtitle: general ? "主对话 / 通用对话合并显示" : "主对话",
        kind: usage.kind,
        group: usage.group,
        note: usage.note,
        daily: usage.daily,
        assignmentEffective: effectiveOf(usage) && (!general || effectiveOf(general)),
        historical: usage.historical === true,
        historicalTarget: usage.historicalTarget ?? null,
        delivery: usage.delivery,
      });
      continue;
    }
    out.push({
      keys: [usage.usageKey],
      primary: usage,
      usages: [usage],
      effective: usage.effective ?? null,
      label: usage.label,
      subtitle: "",
      kind: usage.kind,
      group: usage.group,
      note: usage.note,
      daily: usage.daily,
      assignmentEffective: effectiveOf(usage),
      historical: usage.historical === true,
      historicalTarget: usage.historicalTarget ?? null,
      delivery: usage.delivery,
    });
  }
  // 主区域优先：daily 在前，组内保持后端顺序（后端顺序即产品顺序）。
  return out;
});

/** 会生效的用途：正常分组、正常提供独立选择。 */
const groupedRows = computed(() => {
  const result: { group: string; label: string; rows: Row[] }[] = [];
  for (const group of GROUP_ORDER) {
    const items = rows.value.filter(
      (row) => row.group === group && row.assignmentEffective,
    );
    if (items.length) result.push({ group, label: GROUP_LABELS[group] ?? group, rows: items });
  }
  return result;
});

/** 不生效的用途：只读说明 + 历史配置（可清除）。 */
const readonlyRows = computed(() => rows.value.filter((row) => !row.assignmentEffective));

const expanded = ref<string | null>(null);

/**
 * 按用途取候选：GET /api/models/catalog?usage_key=...，服务端算 usage_usable /
 * capability_state（只读本地事实，不发起验证、不联网）。失败不阻塞选择，但如实说明。
 */
const usageConnections = ref<Record<string, ModelConnection[]>>({});
const usageCandidateErrors = ref<Record<string, string>>({});
const usageCandidateBusy = ref<Record<string, boolean>>({});

async function loadCandidates(row: Row): Promise<void> {
  const key = row.keys[0];
  if (usageCandidateBusy.value[key]) return;
  usageCandidateBusy.value = { ...usageCandidateBusy.value, [key]: true };
  try {
    const reply = await modelCatalogApi.getCatalog({ usageKey: key });
    if (reply && Array.isArray(reply.connections)) {
      usageConnections.value = { ...usageConnections.value, [key]: reply.connections };
      const next = { ...usageCandidateErrors.value };
      delete next[key];
      usageCandidateErrors.value = next;
    }
  } catch (exc) {
    usageCandidateErrors.value = {
      ...usageCandidateErrors.value,
      [key]: "没能读到这个用途的候选判定：" + modelErrorMessage(exc),
    };
  } finally {
    usageCandidateBusy.value = { ...usageCandidateBusy.value, [key]: false };
  }
}

function connectionsFor(row: Row): ModelConnection[] {
  return usageConnections.value[row.keys[0]] ?? props.connections;
}

function toggle(row: Row) {
  const key = row.keys[0];
  if (expanded.value === key) {
    expanded.value = null;
    return;
  }
  expanded.value = key;
  if (!usageConnections.value[key] && !usageCandidateBusy.value[key]) void loadCandidates(row);
}

function pick(row: Row, target: { credentialId: string; modelId: string } | null) {
  expanded.value = null;
  emit("assign", row.keys, target);
}

/**
 * 合并行是否「跟随默认」：两个 key 都不是显式指定才算。
 *
 * 只看 main-chat 会漏掉「main-chat 跟随默认、general-chat 显式指定」这种合法组合：
 * 那时实际调用的是 general-chat，恢复按钮却会因为 main-chat 跟着默认而消失。
 */
function isFollowing(row: Row): boolean {
  return row.usages.every((usage) => usage.followsDefault);
}

function connectionNameOf(credentialId: string | null | undefined): string {
  if (!credentialId) return "";
  return props.connections.find((item) => item.credentialId === credentialId)?.name ?? "";
}

interface RowTarget {
  credentialId: string;
  modelId: string;
  credentialName: string;
  usable: boolean;
  unavailableReason: string;
}

/**
 * 这一行实际会打到哪：优先后端算好的 effective（与运行时同一份规则，
 * general-chat 显式优先），没有时回退 resolved（老后端兼容）。
 */
function targetOf(row: Row): RowTarget | null {
  const effective = row.effective;
  if (effective) {
    if (!effective.ok || !effective.credentialId || !effective.modelId) return null;
    return {
      credentialId: effective.credentialId,
      modelId: effective.modelId,
      credentialName:
        connectionNameOf(effective.credentialId) || row.primary.resolved?.credentialName || "",
      usable: effective.usable,
      unavailableReason: effective.unavailableReason,
    };
  }
  const resolved = row.primary.resolved;
  if (!resolved) return null;
  return {
    credentialId: resolved.credentialId,
    modelId: resolved.modelId,
    credentialName: resolved.credentialName,
    usable: resolved.usable,
    unavailableReason: resolved.unavailableReason,
  };
}

/** 这一行当前不可用的真实原因（effective 不可用时以后端原话为准）。 */
function unavailableReasonOf(row: Row): string {
  const effective = row.effective;
  if (effective && (!effective.ok || !effective.usable)) {
    return effective.unavailableReason || "运行时没有解析到可用目标";
  }
  const target = targetOf(row);
  return target && !target.usable ? target.unavailableReason : "";
}

function resolvedText(row: Row): string {
  const target = targetOf(row);
  if (!target) return "尚未设置";
  const name = target.credentialName || target.credentialId;
  return name + " · " + target.modelId;
}

function statusText(row: Row): string {
  const target = targetOf(row);
  if (!target) return row.effective && !row.effective.ok ? "当前不可用" : "";
  return target.usable ? "当前可用" : "当前不可用";
}

function statusTone(row: Row): string {
  const target = targetOf(row);
  if (!target) return row.effective && !row.effective.ok ? "err" : "quiet";
  return target.usable ? "ok" : "err";
}

/**
 * 行的补充说明（后端 note）。
 *
 * 「沿用现有处理方式」「未接通独立调用路径」这类事实行内标记已经说过一遍，
 * 后端 note 往往就是同一句（或同一句 + 补充原因）：完全重复的不再显示，
 * 前缀重复的只留真正补充的部分，避免同一行把同一句话说两遍。
 */
function noteText(row: Row): string {
  const note = (row.note || "").trim();
  if (!note) return "";
  // 已经被提到列表顶部的那几句，行内不再重复
  if (sharedNoteSet.value.has(note)) return "";
  const badge = badgeOf(row);
  if (!badge) return note;
  const compact = (value: string) => value.replace(/[\s\u3000]+/g, "");
  if (compact(note) === compact(badge.text)) return "";
  if (!note.startsWith(badge.text)) return note;
  return note.slice(badge.text.length).replace(/^[\s：:，,。；;、\-—]+/, "").trim();
}

/**
 * 全列表共用的同一句状态说明（例如「还没有设置默认模型…」）。
 *
 * 后端会给每个"跟随默认"的用途各发一遍同一句 note：逐行重复 8 次没有信息量。
 * 这里把它识别成一条列表级状态，只在顶部说一次，行内不再重复。
 */
const sharedNotes = computed(() => {
  const counts = new Map<string, number>();
  for (const row of rows.value) {
    if (!row.assignmentEffective) continue;
    if (!isFollowing(row) || targetOf(row)) continue;
    const note = (row.note || "").trim();
    if (!note) continue;
    counts.set(note, (counts.get(note) ?? 0) + 1);
  }
  // 出现两次以上才算"列表级状态"：只出现一次的那句仍然属于它自己那一行。
  return [...counts.entries()].filter(([, times]) => times >= 2).map(([note]) => note);
});
const sharedNoteSet = computed(() => new Set(sharedNotes.value));

function badgeOf(row: Row): { text: string; tone: string } | null {
  if (row.kind === "vision") return { text: "需要能读图的模型", tone: "quiet" };
  return null;
}

/** 只读用途的一句话状态（与后端 delivery 同一口径）。 */
function readonlyBadge(row: Row): string {
  return row.delivery === "existing" ? "沿用现有处理方式" : "未接通独立调用路径";
}

/** 历史覆盖指向的目标（只用于如实展示，不说成当前实际使用）。 */
function historicalText(row: Row): string {
  const target = row.historicalTarget;
  if (!target) return "";
  const name = connectionNameOf(target.credentialId) || target.credentialId;
  return name + " · " + target.modelId;
}

function historicalUnavailable(row: Row): string {
  const target = row.historicalTarget;
  return target && !target.usable ? target.unavailableReason : "";
}

const usageError = computed(() => props.errors ?? {});
</script>

<template>
  <section class="section" aria-labelledby="usage-title">
    <header class="section__head">
      <div>
        <h3 id="usage-title" class="section__title">用途分配</h3>
      </div>
    </header>

    <p v-for="note in sharedNotes" :key="note" class="list__note">{{ note }}</p>

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
            <span v-if="resolvedText(row)">{{ resolvedText(row) }}</span>
            <span v-if="statusText(row)" class="qio-state" :class="statusTone(row)">{{ statusText(row) }}</span>
            <span v-if="unavailableReasonOf(row)" class="row__reason">
              不可用：{{ unavailableReasonOf(row) }}
            </span>
          </div>
          <p v-if="noteText(row)" class="row__note">{{ noteText(row) }}</p>
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
          :connections="connectionsFor(row)"
          :selected="row.primary.credentialId && row.primary.modelId ? { credentialId: row.primary.credentialId, modelId: row.primary.modelId } : null"
          :allow-inherit="!isFollowing(row)"
          :usage-label="row.label"
          :usage-key="row.keys[0]"
          :usage-error="usageCandidateErrors[row.keys[0]] || ''"
          @pick="pick(row, $event)"
          @cancel="expanded = null"
        />
      </div>
    </div>

    <!-- 不生效的用途：只读说明，不再画成可用选择器。 -->
    <div v-if="readonlyRows.length" class="group" data-test="usage-readonly">
      <h4 class="group__title">不单独生效的用途</h4>
      <p class="list__note">
        这些用途当前没有独立的模型调用路径：保留历史配置的可见性与清除入口，这里的模型选择不会改变真实请求。
      </p>
      <div v-for="row in readonlyRows" :key="row.keys[0]" class="row row--readonly">
        <div class="row__main">
          <div class="row__label">
            <span class="row__name">{{ row.label }}</span>
            <span class="qio-state quiet">{{ readonlyBadge(row) }}</span>
            <span v-if="row.historical" class="qio-state warn" data-test="usage-historical">
              历史配置（未生效）
            </span>
          </div>
          <div v-if="row.historical && row.historicalTarget" class="row__target mono">
            <span>{{ historicalText(row) }}</span>
            <span v-if="historicalUnavailable(row)" class="row__reason">
              不可用：{{ historicalUnavailable(row) }}
            </span>
          </div>
          <p v-if="row.note" class="row__note">{{ row.note }}</p>
          <p v-if="usageError[row.keys[0]]" class="qio-feedback err" role="alert">
            {{ usageError[row.keys[0]] }}
          </p>
          <p v-if="busyKey === row.keys[0]" class="row__note" role="status">正在保存…</p>
        </div>
        <div v-if="row.historical" class="row__actions">
          <button
            type="button"
            class="qio-btn mini quiet"
            :aria-label="'清除' + row.label + '的历史覆盖'"
            @click="emit('assign', row.keys, null)"
          >
            清除历史覆盖
          </button>
        </div>
      </div>
    </div>

    <p v-if="!groupedRows.length && !readonlyRows.length" class="muted">正在加载用途分配…</p>
  </section>
</template>

<style scoped>
.section { display: flex; flex-direction: column; gap: 8px; }
.section__head { display: flex; align-items: flex-start; gap: 12px; justify-content: space-between; }
.section__title { margin: 0; font-size: var(--fs-sm); color: var(--text-strong); }
.group { display: flex; flex-direction: column; }
.group + .group { margin-top: 10px; }
.group__title { margin: 6px 0 2px; font-size: 10.5px; color: var(--text-muted); font-weight: 600; letter-spacing: .04em; }
/* 用途行默认透明：不套盒子，行与行之间只有一条中性细线；候选面板展开时插在该行下面。 */
.row {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start;
  padding: 10px 0; border: 0; border-radius: 0; background: none;
}
.row + .row { border-top: 1px solid var(--border-subtle); }
.row__main { flex: 1; min-width: 240px; display: flex; flex-direction: column; gap: 4px; }
.row__label { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.row__name { font-size: var(--fs-xs); color: var(--text-strong); font-weight: 600; }
.row__sub { font-size: 10px; color: var(--text-muted); }
.row__target { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 10.5px; color: var(--text-secondary); }
.row__reason { flex-basis: 100%; font-size: 10px; color: var(--warning); }
.row__note { margin: 0; font-size: 10px; color: var(--text-muted); line-height: 1.5; }
.list__note { margin: 4px 0 6px; font-size: 11px; color: var(--text-muted); line-height: 1.6; }
.row__actions { display: flex; gap: 6px; flex-wrap: wrap; }
.muted { margin: 0; font-size: 10.5px; color: var(--text-muted); }
.row--readonly .row__name { color: var(--text-secondary); }
</style>
