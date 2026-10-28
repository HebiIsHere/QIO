<script setup lang="ts">
/**
 * 「模型与服务」设置分区的内容面板（A 的壳只挂这一个组件）。
 *
 * 三块：Key 接入与管理 / 默认模型 / 用途分配。数据来自两个接口：
 * - GET /api/models/catalog：连接 → 模型 → 用途 → 默认模型（只读，不联网）；
 * - GET /api/credentials：连接级元数据（用量账本、版本、审计），与目录按 credential_id 合并。
 *
 * 保存模型（_STG-COMMON.md §5）：
 * - Key 表单在弹窗里显式提交；默认模型 / 用途分配是「选完即保存」的小组操作，
 *   每组独立反馈，失败保留原值并把原因写在分区里；迟到的旧响应不覆盖新状态
 *   （每次写带自增 seq，只有 seq 仍是最新才落 UI）。
 */
import { onMounted, ref } from "vue";
import KeyAccessSection from "./KeyAccessSection.vue";
import DefaultModelSection from "./DefaultModelSection.vue";
import UsageAssignmentSection from "./UsageAssignmentSection.vue";
import {
  api,
  type CredentialMeta,
  type ProviderPreset,
} from "../../services/api";
import { readableError } from "../../services/credentials";
import {
  modelCatalogApi,
  type CatalogResponse,
  type ModelConnection,
} from "../../services/modelCatalog";
// A 的「工具与权限 → 当前生效参数」只读区的数据入口（跨分区共享的真实值，不是草稿）。
import { setModelFacts } from "../../views/settings/modelFacts";

const loading = ref(true);
const loadError = ref("");
const catalog = ref<CatalogResponse | null>(null);
const credentials = ref<CredentialMeta[]>([]);
const providers = ref<ProviderPreset[]>([]);

const defaultBusy = ref(false);
const defaultError = ref("");
const usageBusy = ref<string | null>(null);
const usageErrors = ref<Record<string, string>>({});
/** 正在提交的用途改动指向哪些连接：删连接前也要把这些「未完成的引用」算进去 */
const pendingRefs = ref<string[]>([]);

// 写操作的序号：只有最后一次请求的结果能改 UI（避免迟到响应覆盖新状态）。
let catalogSeq = 0;
let defaultSeq = 0;
const usageSeq = new Map<string, number>();

const connections = ref<ModelConnection[]>([]);

/**
 * 把「已保存并真实生效」的默认模型与各用途实际使用的模型/Key 交给 A 的只读区。
 *
 * 只在读取/保存**成功**后调用：失败时保留上一次已知的真实值（不清空、不写草稿），
 * 失败原因由本面板分区自己显示。写权限也在这里收口 —— A 不直连模型目录 API。
 */
function publishModelFacts() {
  const current = catalog.value;
  if (!current) return;
  const connectionOf = (credentialId: string | null | undefined) => {
    if (!credentialId) return null;
    return current.connections.find((item) => item.credentialId === credentialId) ?? null;
  };
  try {
    setModelFacts({
      defaultModel: current.default
        ? {
            credentialId: current.default.credentialId,
            modelId: current.default.modelId,
            connectionName:
              connectionOf(current.default.credentialId)?.name ?? current.default.credentialId,
            providerName: connectionOf(current.default.credentialId)?.providerName ?? null,
          }
        : null,
      usages: current.usages.map((usage) => {
        // 跟随默认的用途以「后端解析后的真实目标」为准；显式分配的直接用分配值。
        const targetId = usage.resolved?.credentialId ?? usage.credentialId ?? null;
        const connection = connectionOf(targetId);
        return {
          usageKey: usage.usageKey,
          label: usage.label,
          followsDefault: usage.followsDefault,
          credentialId: targetId,
          modelId: usage.resolved?.modelId ?? usage.modelId ?? null,
          connectionName:
            usage.resolved?.credentialName || connection?.name || null,
          providerName: connection?.providerName ?? null,
          resolved: usage.resolved
            ? {
                credentialId: usage.resolved.credentialId,
                modelId: usage.resolved.modelId,
              }
            : null,
        };
      }),
    });
  } catch {
    // A 的只读区同步失败不能影响模型面板本身：这里刻意吞掉，不下发错误、不清空。
  }
}

async function reload() {
  const seq = ++catalogSeq;
  loading.value = true;
  loadError.value = "";
  try {
    const [catalogReply, credentialReply, providerReply] = await Promise.all([
      modelCatalogApi.getCatalog(),
      api.listCredentials(),
      api.listProviders(),
    ]);
    if (seq !== catalogSeq) return;
    catalog.value = catalogReply;
    connections.value = catalogReply.connections;
    credentials.value = credentialReply.credentials;
    providers.value = providerReply.providers;
    // 目录加载成功：只读区拿到一份真实值（不含任何草稿）。
    publishModelFacts();
  } catch (exc) {
    if (seq !== catalogSeq) return;
    loadError.value = "读取模型与服务设置失败：" + readableError(exc);
  } finally {
    if (seq === catalogSeq) loading.value = false;
  }
}

onMounted(() => {
  void reload();
});

async function saveDefault(target: { credentialId: string; modelId: string }) {
  const seq = ++defaultSeq;
  defaultBusy.value = true;
  defaultError.value = "";
  try {
    await modelCatalogApi.setDefault(target);
    if (seq === defaultSeq) {
      await reload();
      // 保存成功之后再写一次：只读区显示的是刚保存并重新读回的真实值，不是点击时的草稿。
      if (!loadError.value) publishModelFacts();
    }
  } catch (exc) {
    if (seq === defaultSeq) defaultError.value = readableError(exc);
  } finally {
    if (seq === defaultSeq) defaultBusy.value = false;
  }
}

async function assignUsage(
  keys: string[],
  target: { credentialId: string; modelId: string } | null,
) {
  const primary = keys[0];
  const seq = (usageSeq.get(primary) ?? 0) + 1;
  usageSeq.set(primary, seq);
  delete usageErrors.value[primary];
  usageBusy.value = primary;
  const referenced = target?.credentialId;
  if (referenced && !pendingRefs.value.includes(referenced)) {
    pendingRefs.value = [...pendingRefs.value, referenced];
  }
  try {
    for (const usageKey of keys) {
      await modelCatalogApi.setUsage({
        usageKey,
        credentialId: target?.credentialId ?? null,
        modelId: target?.modelId ?? null,
      });
    }
    if (usageSeq.get(primary) === seq) {
      await reload();
      // 同上：用途分配保存成功后同步一次真实值（失败时不写，保留上次已知值）。
      if (!loadError.value) publishModelFacts();
    }
  } catch (exc) {
    if (usageSeq.get(primary) === seq) {
      usageErrors.value = { ...usageErrors.value, [primary]: readableError(exc) };
    }
  } finally {
    if (referenced) pendingRefs.value = pendingRefs.value.filter((id) => id !== referenced);
    if (usageSeq.get(primary) === seq) usageBusy.value = null;
  }
}
</script>

<template>
  <div class="panel">
    <header class="panel__head">
      <div>
        <h2 class="panel__title">模型与服务</h2>
        <p class="panel__desc">
          一把 Key 一条连接，一条连接下可以有多个模型；日常对话用默认模型，其余用途可按需单独指定。
        </p>
      </div>
      <button type="button" class="qio-btn mini" :disabled="loading" :aria-busy="loading" @click="reload">
        {{ loading ? "读取中…" : "重新读取" }}
      </button>
    </header>

    <p v-if="loadError" class="qio-feedback err" role="alert">{{ loadError }}</p>

    <div v-if="loading && !catalog" class="loading" role="status">正在读取模型与服务设置…</div>

    <template v-else>
      <KeyAccessSection
        :connections="connections"
        :credentials="credentials"
        :catalog="catalog"
        :providers="providers"
        :draft-refs="pendingRefs"
        @changed="reload"
      />
      <hr class="divider" />
      <DefaultModelSection
        :connections="connections"
        :catalog="catalog"
        :busy="defaultBusy"
        :error="defaultError"
        @save="saveDefault"
      />
      <hr class="divider" />
      <UsageAssignmentSection
        :connections="connections"
        :catalog="catalog"
        :busy-key="usageBusy"
        :errors="usageErrors"
        @assign="assignUsage"
      />
    </template>
  </div>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; gap: 14px; }
.panel__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.panel__title { margin: 0; font-size: var(--fs-md); color: var(--text-strong); }
.panel__desc { margin: 2px 0 0; font-size: 10.5px; color: var(--text-muted); line-height: 1.5; }
.divider { border: none; border-top: 1px solid var(--border-subtle); margin: 2px 0; }
.loading { font-size: var(--fs-xs); color: var(--text-muted); }
.qio-feedback.err { color: var(--danger); }
</style>
