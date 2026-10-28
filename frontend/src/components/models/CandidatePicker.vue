<script setup lang="ts">
/**
 * 模型候选列表：默认模型与用途分配共用同一套候选呈现。
 *
 * 三条硬要求（验收会查）：
 * 1. 不可用候选**不静默过滤**：照样列出来并写清原因（停用 / 撤销 / 过期 / 预算耗尽 /
 *    验证未通过 / 能力不满足），否则用户会以为「这把 Key 没有这个模型」；
 * 2. 每个候选显示的是「它自己」的状态，不是一个总的绿点：Key 状态、模型验证状态分开；
 * 3. 同名模型跨 Key 必须能区分：分组标题是连接（名称 + 服务商 + 端点点位）。
 */
import { computed } from "vue";
import type { CatalogModel, ModelConnection } from "../../services/modelCatalog";
import QIcon from "../ui/QIcon.vue";

const props = defineProps<{
  connections: ModelConnection[];
  /** 当前选中的目标（用于打勾），空表示跟随默认 */
  selected?: { credentialId: string; modelId: string } | null;
  /** 是否提供「恢复跟随默认」入口 */
  allowInherit?: boolean;
  /** 这张列表对应的用途（用于能力检查的说明文案；不改变后端判定） */
  usageLabel?: string;
}>();
const emit = defineEmits<{
  pick: [target: { credentialId: string; modelId: string } | null];
  cancel: [];
}>();

interface Group {
  connection: ModelConnection;
  models: CatalogModel[];
  usableCount: number;
}

const groups = computed<Group[]>(() =>
  props.connections.map((connection) => ({
    connection,
    models: connection.models,
    usableCount: connection.models.filter((m) => m.usable).length,
  })),
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
    <p class="picker__hint">
      候选按「连接 → 模型」分组；同一个模型名可能来自不同 Key，请按连接名确认发送目标。
    </p>
    <div v-if="!hasAnyModel" class="picker__empty">
      还没有任何已记录的模型。先在「Key 接入与管理」里添加连接并获取模型。
    </div>
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
        <li v-for="model in group.models" :key="model.modelId">
          <button
            type="button"
            class="model qio-row"
            :class="{ 'is-selected': isSelected(group.connection, model) }"
            :disabled="!model.usable"
            :aria-describedby="model.usable ? undefined : 'reason-' + group.connection.credentialId + '-' + model.modelId"
            @click="emit('pick', { credentialId: group.connection.credentialId, modelId: model.modelId })"
          >
            <QIcon v-if="isSelected(group.connection, model)" name="check" />
            <span class="model__name mono">{{ model.displayName || model.modelId }}</span>
            <span v-if="model.displayName && model.displayName !== model.modelId" class="model__id mono">{{ model.modelId }}</span>
            <span class="qio-state" :class="verifyTone(model)">{{ verifyText(model) }}</span>
            <span v-if="model.source === 'manual'" class="qio-tag">手动</span>
            <span v-if="model.capabilities.length" class="qio-tag">能力 {{ model.capabilities.join("/") }}</span>
            <span
              v-if="!model.usable"
              :id="'reason-' + group.connection.credentialId + '-' + model.modelId"
              class="model__reason"
            >
              {{ model.unavailableReason || "当前不可用" }}
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
.picker {
  margin-top: 8px;
  padding: 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.picker__hint { margin: 0; font-size: 10.5px; color: var(--text-muted); line-height: 1.5; }
.picker__empty { font-size: var(--fs-xs); color: var(--text-muted); }
.group { display: flex; flex-direction: column; gap: 6px; }
.group__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.group__name { font-size: var(--fs-xs); color: var(--text-strong); font-weight: 600; }
.group__meta { font-size: 10px; color: var(--text-muted); }
.group__count { font-size: 10px; color: var(--text-muted); margin-left: auto; }
.group__none { margin: 0; font-size: 10.5px; color: var(--text-muted); }
.models { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.model {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.model:hover:not(:disabled) { background: var(--layer-hover); border-color: var(--border-subtle); }
.model:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -1px; }
.model.is-selected { background: var(--layer-selected); border-color: var(--accent); }
.model:disabled { cursor: not-allowed; opacity: 1; }
.model__name { font-size: var(--fs-xs); color: var(--text-strong); }
.model__id { font-size: 10px; color: var(--text-muted); }
.model__reason { flex-basis: 100%; font-size: 10px; color: var(--warning); }
.picker__actions { display: flex; gap: 8px; justify-content: flex-end; }
.qio-state.ok { color: var(--success); background: var(--success-soft); border-color: var(--success-soft); }
.qio-state.warn { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-soft); }
.qio-state.err { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-soft); }
.qio-state.quiet { color: var(--text-muted); border-color: var(--border-subtle); }
</style>
