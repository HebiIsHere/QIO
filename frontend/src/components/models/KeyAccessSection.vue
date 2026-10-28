<script setup lang="ts">
/**
 * Key 接入与管理：连接列表 + 筛选 + 添加 / 编辑 / 启停 / 换 Key / 验证 / 撤销 / 删除。
 *
 * 删除规则（诚实执行）：
 * - 仍被**已保存用途**引用 → 不删，弹窗列出引用并让用户先改用途（服务端也会兜底 409）；
 * - 未保存草稿里的引用同样算引用：父级把正在提交中的目标传进来，一起检查；
 * - 撤销与删除是两件事：撤销只在 QIO 内作废并保留记录与审计，两者都不会声称已吊销厂商后台 Key。
 */
import { computed, ref } from "vue";
import ConnectionCard from "./ConnectionCard.vue";
import KeyForm from "./KeyForm.vue";
import QConfirm from "../ui/QConfirm.vue";
import QModal from "../ui/QModal.vue";
import {
  api,
  type CredentialMeta,
  type ProviderPreset,
  type VerifyReport,
} from "../../services/api";
import { readableError } from "../../services/credentials";
import {
  modelCatalogApi,
  type CatalogResponse,
  type ModelConnection,
  type UsageAssignment,
} from "../../services/modelCatalog";

const props = defineProps<{
  connections: ModelConnection[];
  credentials: CredentialMeta[];
  catalog: CatalogResponse | null;
  providers: ProviderPreset[];
  /** 未保存草稿里被引用的连接（删前也要先处理） */
  draftRefs?: string[];
}>();
const emit = defineEmits<{ changed: [] }>();

type Filter = "all" | "enabled" | "disabled" | "revoked" | "expired";
const filters: { id: Filter; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "enabled", label: "已启用" },
  { id: "disabled", label: "已停用" },
  { id: "revoked", label: "已撤销" },
  { id: "expired", label: "已过期" },
];
const filter = ref<Filter>("all");

const filtered = computed(() =>
  props.connections.filter((connection) => {
    switch (filter.value) {
      case "enabled":
        return connection.status === "active" && connection.enabled;
      case "disabled":
        return connection.status === "active" && !connection.enabled;
      case "revoked":
        return connection.status === "revoked";
      case "expired":
        return connection.status === "expired";
      default:
        return true;
    }
  }),
);

const counts = computed(() => {
  const result: Record<Filter, number> = { all: props.connections.length, enabled: 0, disabled: 0, revoked: 0, expired: 0 };
  for (const connection of props.connections) {
    if (connection.status === "revoked") result.revoked += 1;
    else if (connection.status === "expired") result.expired += 1;
    else if (connection.enabled) result.enabled += 1;
    else result.disabled += 1;
  }
  return result;
});

const byId = computed(() => {
  const map: Record<string, CredentialMeta> = {};
  for (const meta of props.credentials) map[meta.key_id] = meta;
  return map;
});

const usages = computed<UsageAssignment[]>(() => props.catalog?.usages ?? []);

// ── 弹窗状态 ────────────────────────────────────────────────────────────────
const formOpen = ref(false);
const formMode = ref<"create" | "edit" | "rotate">("create");
const formInitial = ref<CredentialMeta | null>(null);

const notice = ref<{ tone: "ok" | "warn" | "err"; text: string } | null>(null);
const busyId = ref<string | null>(null);

interface ConfirmState {
  kind: "revoke" | "delete" | "blocked";
  credentialId: string;
  title: string;
  detail: string;
}
const confirmState = ref<ConfirmState | null>(null);

function openCreate() {
  formMode.value = "create";
  formInitial.value = null;
  formOpen.value = true;
}

function openEdit(connection: ModelConnection) {
  formMode.value = "edit";
  formInitial.value = byId.value[connection.credentialId] ?? null;
  formOpen.value = true;
}

function openRotate(connection: ModelConnection) {
  formMode.value = "rotate";
  formInitial.value = byId.value[connection.credentialId] ?? null;
  formOpen.value = true;
}

async function onSaved(credentialId: string) {
  formOpen.value = false;
  notice.value = { tone: "ok", text: "连接已保存。正在同步模型目录…" };
  // 新建/改配置后立即按新配置获取一次模型：模型属于这次保存的地址与 Key。
  try {
    await modelCatalogApi.discover({ credentialId, refresh: true });
  } catch (exc) {
    notice.value = { tone: "warn", text: "连接已保存，但获取模型失败：" + readableError(exc) };
  }
  emit("changed");
}

function verifyNotice(report: VerifyReport | null | undefined) {
  if (!report) return;
  if (report.ok) notice.value = { tone: "ok", text: "验证通过：" + report.message };
  else notice.value = { tone: "warn", text: report.message };
}

async function toggle(connection: ModelConnection) {
  busyId.value = connection.credentialId;
  try {
    await api.setCredentialEnabled(connection.credentialId, !connection.enabled);
    notice.value = { tone: "ok", text: connection.enabled ? "已停用这条连接" : "已启用这条连接" };
    emit("changed");
  } catch (exc) {
    notice.value = { tone: "err", text: readableError(exc) };
  } finally {
    busyId.value = null;
  }
}

async function verifyKey(connection: ModelConnection) {
  busyId.value = connection.credentialId;
  try {
    const reply = await api.verifyCredential(connection.credentialId);
    verifyNotice(reply.verify);
    emit("changed");
  } catch (exc) {
    notice.value = { tone: "err", text: readableError(exc) };
  } finally {
    busyId.value = null;
  }
}

async function discover(connection: ModelConnection, refresh: boolean) {
  busyId.value = connection.credentialId;
  try {
    const result = await modelCatalogApi.discover({
      credentialId: connection.credentialId,
      refresh,
    });
    if (result.error) {
      notice.value = { tone: "warn", text: "获取模型失败：" + result.error };
    } else if (result.models.length) {
      const base =
        "已获取 " + result.total + " 个模型（来源：" + (result.source === "cache" ? "上次结果" : "服务商") + "）";
      // 公共目录（例如 OpenRouter）不能称为「这把 Key 全部可用的模型」：把说明一并显示出来。
      notice.value = {
        tone: result.publicCatalog ? "warn" : "ok",
        text: result.publicCatalog && result.note ? base + "。" + result.note : base,
      };
    } else {
      notice.value = { tone: "warn", text: "这个地址没有返回可用的模型列表" };
    }
    emit("changed");
  } catch (exc) {
    notice.value = { tone: "err", text: readableError(exc) };
  } finally {
    busyId.value = null;
  }
}

async function verifyModel(connection: ModelConnection, modelId: string) {
  busyId.value = connection.credentialId;
  try {
    const result = await modelCatalogApi.verify({
      credentialId: connection.credentialId,
      modelId,
    });
    notice.value = {
      tone: result.ok ? "ok" : "warn",
      text: "模型 " + modelId + "：" + result.message,
    };
    emit("changed");
  } catch (exc) {
    notice.value = { tone: "err", text: readableError(exc) };
  } finally {
    busyId.value = null;
  }
}

function requestRevoke(connection: ModelConnection) {
  confirmState.value = {
    kind: "revoke",
    credentialId: connection.credentialId,
    title: "撤销这条连接的密钥？",
    detail:
      "撤销会移除本机保存的密钥，并保留这条记录与审计历史。它不会去厂商后台吊销这把 API Key；" +
      "撤销后引用它的用途会如实显示不可用，不会自动换别的模型。",
  };
}

function referencesOf(credentialId: string): string[] {
  const labels = usages.value
    .filter((usage) => usage.credentialId === credentialId && usage.modelId)
    .map((usage) => usage.label);
  if ((props.draftRefs ?? []).includes(credentialId)) {
    labels.push("尚未保存的用途改动");
  }
  return Array.from(new Set(labels));
}

function requestRemove(connection: ModelConnection) {
  const refs = referencesOf(connection.credentialId);
  if (refs.length) {
    confirmState.value = {
      kind: "blocked",
      credentialId: connection.credentialId,
      title: "这条连接还被用途引用，不能删除",
      detail: "请先把这些用途改成别的模型（或恢复跟随默认）再删除：" + refs.join("、"),
    };
    return;
  }
  confirmState.value = {
    kind: "delete",
    credentialId: connection.credentialId,
    title: "删除这条连接？",
    detail:
      "删除后这条连接的元数据、模型记录与审计一起移除，无法恢复；已发出的调用记录不受影响。" +
      "这不会去厂商后台吊销 API Key。",
  };
}

async function confirmAction() {
  const state = confirmState.value;
  confirmState.value = null;
  if (!state || state.kind === "blocked") return;
  busyId.value = state.credentialId;
  try {
    if (state.kind === "revoke") {
      await api.revokeCredential(state.credentialId);
      notice.value = { tone: "ok", text: "已在本机撤销这条连接（未吊销厂商后台 Key）" };
    } else {
      await api.deleteCredential(state.credentialId);
      notice.value = { tone: "ok", text: "连接已删除" };
    }
    emit("changed");
  } catch (exc) {
    notice.value = { tone: "err", text: readableError(exc) };
  } finally {
    busyId.value = null;
  }
}
</script>

<template>
  <section class="key-access" aria-labelledby="key-access-title">
    <header class="head">
      <div>
        <h3 id="key-access-title" class="head__title">Key 接入与管理</h3>
      </div>
      <button type="button" class="qio-btn primary mini" @click="openCreate">添加 Key</button>
    </header>

    <div class="filters" role="group" aria-label="筛选连接">
      <button
        v-for="item in filters"
        :key="item.id"
        type="button"
        class="filter"
        :class="{ 'is-on': filter === item.id }"
        :aria-pressed="filter === item.id"
        @click="filter = item.id"
      >
        {{ item.label }}
        <span class="filter__count">{{ counts[item.id] }}</span>
      </button>
    </div>

    <p v-if="notice" class="qio-feedback" :class="notice.tone" role="status">{{ notice.text }}</p>
    <p v-if="!filtered.length" class="empty">
      {{ filter === "all" ? "还没有连接。先点「添加 Key」接入一把 API Key。" : "这个筛选下没有连接。" }}
    </p>

    <ConnectionCard
      v-for="connection in filtered"
      :key="connection.credentialId"
      :connection="connection"
      :credential="byId[connection.credentialId] ?? null"
      :usages="usages"
      :discovery="catalog?.discovery[connection.credentialId] ?? null"
      :default-model="catalog?.default ?? null"
      :busy="busyId === connection.credentialId"
      @edit="openEdit(connection)"
      @rotate="openRotate(connection)"
      @verify-key="verifyKey(connection)"
      @toggle="toggle(connection)"
      @revoke="requestRevoke(connection)"
      @remove="requestRemove(connection)"
      @discover="(refresh: boolean) => discover(connection, refresh)"
      @verify-model="(modelId: string) => verifyModel(connection, modelId)"
    />

    <QModal
      :open="formOpen"
      :title="formMode === 'edit' ? '编辑连接' : formMode === 'rotate' ? '更换 API Key' : '添加 Key'"
      modal-id="qio-model-key-form"
      @close="formOpen = false"
    >
      <KeyForm
        v-if="formOpen"
        :providers="providers"
        :mode="formMode"
        :initial="formInitial"
        @saved="onSaved"
        @cancel="formOpen = false"
      />
    </QModal>

    <QConfirm
      :open="!!confirmState"
      :title="confirmState?.title ?? ''"
      :detail="confirmState?.detail ?? ''"
      :confirm-text="confirmState?.kind === 'revoke' ? '撤销' : confirmState?.kind === 'delete' ? '删除' : '知道了'"
      :tone="confirmState?.kind === 'delete' || confirmState?.kind === 'revoke' ? 'danger' : 'normal'"
      variant="layer"
      @confirm="confirmAction"
      @cancel="confirmState = null"
    />
  </section>
</template>

<style scoped>
.key-access { display: flex; flex-direction: column; gap: 2px; }
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 2px 0 6px; }
.head__title { margin: 0; font-size: var(--fs-sm); color: var(--text-strong); }
/* 筛选：透明文字行，选中 = 品牌色文字 + 4px 圆点；不画灰色胶囊、不画玫红外框。 */
.filters { display: flex; gap: 2px; flex-wrap: wrap; padding: 0 0 6px; }
.filter {
  position: relative;
  display: inline-flex; align-items: center; gap: 6px;
  border: 0; background: none; border-radius: 0;
  color: var(--text-secondary); font: inherit; font-size: 11.5px;
  padding: 4px 10px; cursor: pointer;
  transition: color var(--mo-1-state) var(--ease-1);
}
.filter::before {
  content: ""; position: absolute; left: 0; top: 50%; width: 4px; height: 4px; margin-top: -2px;
  border-radius: 50%; background: var(--accent);
  opacity: 0; transform: scale(.6);
  transition: opacity var(--mo-1-state) var(--ease-1), transform var(--mo-1-state) var(--ease-1);
}
.filter:hover { color: var(--text-strong); }
.filter:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: 4px; }
.filter.is-on { color: var(--link); font-weight: 600; }
.filter.is-on::before { opacity: 1; transform: scale(1); }
.filter__count { font-size: 10px; color: var(--text-muted); font-weight: 400; }
.empty { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.qio-feedback.ok { color: var(--success); }
.qio-feedback.warn { color: var(--warning); }
.qio-feedback.err { color: var(--danger); }
</style>
