<script setup lang="ts">
/**
 * 模型候选列表：默认模型与用途分配共用同一套候选呈现。
 *
 * 三条硬要求（验收会查）：
 * 1. 不可用候选**不静默过滤**：照样列出来并写清原因（停用 / 撤销 / 过期 / 预算耗尽 /
 *    验证未通过 / 能力不满足），否则用户会以为「这把 Key 没有这个模型」；
 * 2. 每个候选显示的是「它自己」的状态，不是一个总的绿点：Key 状态、模型验证状态分开；
 * 3. 同名模型跨 Key 必须能区分：分组标题是连接（名称 + 服务商 + 端点点位）。
 *
 * 用途维度（发布第二轮 C）：候选按**当前用途**给结论，且由服务端算好，这里只呈现：
 * - 能力已声明 → 可选；
 * - 明确缺少所需能力 → 禁止选择 + 原因；
 * - 能力未知（模型没声明 capabilities）→ **仍可选**，标注「能力未确认」；
 * - 连接本身不可用 → 禁止选择 + 既有原因文案。
 * 「一般可用」与「适用于当前用途」是两条不同事实：同一个模型可能普通对话可用、
 * 看图不适用，两条都要显示出来。
 */
import { computed } from "vue";
import type { CatalogModel, ModelConnection } from "../../services/modelCatalog";

const props = defineProps<{
  connections: ModelConnection[];
  /** 当前选中的目标（用于打勾），空表示跟随默认 */
  selected?: { credentialId: string; modelId: string } | null;
  /** 是否提供「恢复跟随默认」入口 */
  allowInherit?: boolean;
  /** 这张列表对应的用途（用于能力检查的说明文案；不改变后端判定） */
  usageLabel?: string;
  /** 当前用途 key（服务端按它算 usage_usable / capability_state；没有数据时不猜） */
  usageKey?: string;
  /** 读取用途候选判定失败时的如实说明（不阻塞选择，但要说清楚） */
  usageError?: string;
}>();
const emit = defineEmits<{
  pick: [target: { credentialId: string; modelId: string } | null];
  cancel: [];
}>();

interface ModelView {
  model: CatalogModel;
  /** 这个模型一般可不可用（与用途无关） */
  generalUsable: boolean;
  /** 服务端是否给了用途维度结论 */
  usageScoped: boolean;
  /** 现在到底能不能选 */
  selectable: boolean;
  /** 不能选的原因（一般不可用原因优先，其次用途不适用原因） */
  reason: string;
  /** 用途维度的一句话结论（仅当一般可用且有结论时显示） */
  usageBadge: { text: string; tone: string } | null;
}

interface Group {
  connection: ModelConnection;
  models: ModelView[];
  usableCount: number;
}

function viewOf(model: CatalogModel): ModelView {
  const usageScoped =
    typeof model.usageUsable === "boolean" && Boolean(model.usageKey || props.usageKey);
  const generalUsable = model.usable;
  const usageUsable = usageScoped ? Boolean(model.usageUsable) : generalUsable;
  const selectable = generalUsable && usageUsable;
  let reason = "";
  if (!generalUsable) {
    // 连接 / 模型层面的既有原因（第一轮口径），不与用途能力混在一起。
    reason = model.unavailableReason || "当前不可用";
  } else if (usageScoped && !usageUsable) {
    reason = model.usageReason || "该模型不适用于当前用途";
  }
  let usageBadge: { text: string; tone: string } | null = null;
  if (generalUsable && usageScoped) {
    if (model.capabilityState === "supported") {
      usageBadge = { text: "该用途能力已声明", tone: "ok" };
    } else if (model.capabilityState === "missing") {
      usageBadge = { text: "不适用于该用途：缺少所需能力", tone: "err" };
    } else if (model.capabilityState === "unknown") {
      usageBadge = { text: "能力未确认", tone: "quiet" };
    }
  }
  return { model, generalUsable, usageScoped, selectable, reason, usageBadge };
}

const groups = computed<Group[]>(() =>
  props.connections.map((connection) => {
    const models = connection.models.map(viewOf);
    return {
      connection,
      models,
      usableCount: models.filter((item) => item.selectable).length,
    };
  }),
);

const hasAnyModel = computed(() => props.connections.some((c) => c.models.length > 0));

function isSelected(connection: ModelConnection, model: CatalogModel): boolean {
  return (
    props.selected?.credentialId === connection.credentialId &&
    props.selected?.modelId === model.modelId
  );
}

function verifyText(model: CatalogModel): string {
  switch (model.verifyState) {
    case "verified":
      return "已验证";
    case "failed":
      return model.verifyError ? "验证未通过" : "验证未通过";
    case "legacy":
      return "旧配置结论";
    default:
      return "尚未验证";
  }
}

function verifyTone(model: CatalogModel): string {
  if (model.verifyState === "verified") return "ok";
  if (model.verifyState === "failed") return "err";
  if (model.verifyState === "legacy") return "quiet";
  return "quiet";
}

function connectionState(connection: ModelConnection): { text: string; tone: string } {
  if (connection.status === "revoked") return { text: "已撤销", tone: "err" };
  if (connection.status === "expired") return { text: "已过期", tone: "err" };
  if (!connection.enabled) return { text: "已停用", tone: "warn" };
  return { text: "已启用", tone: "ok" };
}
</script>

<template>
  <div class="picker" role="group" :aria-label="usageLabel ? usageLabel + '的候选模型' : '候选模型'">
    <div v-if="!hasAnyModel" class="picker__empty">
      还没有任何已记录的模型。先在「Key 接入与管理」里添加连接并获取模型。
    </div>
    <p v-if="usageError" class="picker__note" role="status">{{ usageError }}</p>
    <section v-for="group in groups" :key="group.connection.credentialId" class="group">
      <header class="group__head">
        <span class="group__name">{{ group.connection.name }}</span>
        <span class="group__meta mono">{{ group.connection.providerName || "自定义服务" }} · {{ group.connection.endpoint || "—" }}</span>
        <span class="qio-state" :class="connectionState(group.connection).tone">{{ connectionState(group.connection).text }}</span>
        <span class="group__count">{{ group.usableCount }} / {{ group.models.length }} 个可用</span>
      </header>
      <p v-if="group.models.length === 0" class="group__none">
        这条连接还没有模型记录：到「Key 接入与管理 → 模型」里点「获取模型」。
      </p>
      <ul v-else class="models">
        <li v-for="view in group.models" :key="view.model.modelId">
          <button
            type="button"
            class="model qio-row"
            :class="{ 'is-selected': isSelected(group.connection, view.model) }"
            :disabled="!view.selectable"
            :aria-describedby="view.selectable ? undefined : 'reason-' + group.connection.credentialId + '-' + view.model.modelId"
            @click="emit('pick', { credentialId: group.connection.credentialId, modelId: view.model.modelId })"
          >
            <span class="model__name mono">{{ view.model.displayName || view.model.modelId }}</span>
            <span v-if="view.model.displayName && view.model.displayName !== view.model.modelId" class="model__id mono">{{ view.model.modelId }}</span>
            <span class="qio-state" :class="verifyTone(view.model)">{{ verifyText(view.model) }}</span>
            <span v-if="view.model.source === 'manual'" class="qio-tag">手动</span>
            <span v-if="view.model.capabilities.length" class="qio-tag">能力 {{ view.model.capabilities.join("/") }}</span>
            <!-- 两条不同事实：一般可用性（与用途无关）与是否适用于当前用途。 -->
            <span v-if="view.usageScoped && view.generalUsable" class="qio-state ok">一般可用</span>
            <span v-if="view.usageBadge" class="qio-state" :class="view.usageBadge.tone">{{ view.usageBadge.text }}</span>
            <span
              v-if="!view.selectable"
              :id="'reason-' + group.connection.credentialId + '-' + view.model.modelId"
              class="model__reason"
            >
              {{ view.reason }}
            </span>
          </button>
        </li>
      </ul>
    </section>
    <div class="picker__actions">
      <button v-if="allowInherit" type="button" class="qio-btn mini" @click="emit('pick', null)">
        恢复跟随默认
      </button>
      <button type="button" class="qio-btn mini quiet" @click="emit('cancel')">收起</button>
    </div>
  </div>
</template>

<style scoped>
/* 候选是一层"展开的浮层"：轻玻璃 + 1px 中性边界 + 层级阴影，保持 14px 圆角；
   候选行本身透明，选中 = 品牌色模型名 + 4px 圆点（不画外框、不铺选中底色块）。 */
.picker {
  margin-top: 10px;
  padding: 12px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--glass-bg);
  box-shadow: var(--shadow-2);
  backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.picker__empty { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.picker__note { margin: 0; font-size: 10.5px; color: var(--text-muted); }
.group { display: flex; flex-direction: column; gap: 4px; }
.group__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.group__name { font-size: var(--fs-xs); color: var(--text-strong); font-weight: 600; }
.group__meta { font-size: 10px; color: var(--text-muted); min-width: 0; overflow-wrap: anywhere; }
.group__count { font-size: 10px; color: var(--text-muted); margin-left: auto; }
.group__none { margin: 0; font-size: 10.5px; color: var(--text-muted); }
.models { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.model {
  position: relative;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 8px 8px 14px;
  border: 0;
  border-radius: 0;
  background: none;
  color: var(--text-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.model::before {
  content: ""; position: absolute; left: 3px; top: 50%; width: 4px; height: 4px; margin-top: -2px;
  border-radius: 50%; background: var(--accent);
  opacity: 0; transform: scale(.6);
  transition: opacity var(--mo-1-state) var(--ease-1), transform var(--mo-1-state) var(--ease-1);
}
.model:hover:not(:disabled) { background: var(--layer-hover); border-color: transparent; }
.model:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
.model.is-selected { background: none; border-color: transparent; }
.model.is-selected::before { opacity: 1; transform: scale(1); }
.model.is-selected .model__name { color: var(--link); }
.model:disabled { cursor: not-allowed; opacity: 1; }
.model__name { font-size: var(--fs-xs); color: var(--text-strong); overflow-wrap: anywhere; }
.model__id { font-size: 10px; color: var(--text-muted); overflow-wrap: anywhere; }
.model__reason { flex-basis: 100%; font-size: 10px; color: var(--warning); }
.picker__actions { display: flex; gap: 8px; justify-content: flex-end; }
</style>
