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
  type UsageAssignment,
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

/** 共用同一条调用路径的两个日常入口（合并成一行显示 / 保存）。 */
const MERGED_CHAT_KEYS = ["main-chat", "general-chat"];

/** 只读区展示用的用途状态（沿用后端 delivery / followsDefault 事实，不新增判断）。 */
function factStatus(usage: UsageAssignment): "unsupported" | "existing" | "follow" | "explicit" {
  if (usage.delivery === "unsupported") return "unsupported";
  if (usage.delivery === "existing") return "existing";
  return usage.followsDefault ? "follow" : "explicit";
}

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
  /**
   * 合并行的实际生效目标：优先本行 effective，其次兄弟行的（两者由后端同一份
   * resolve_shared_target 算出，真实后端两个 key 都会带上）。
   */
  const effectiveOf = (usage: UsageAssignment) => {
    if (usage.effective) return usage.effective;
    const siblingKey =
      usage.usageKey === "main-chat"
        ? "general-chat"
        : usage.usageKey === "general-chat"
          ? "main-chat"
          : "";
    if (!siblingKey) return null;
    const sibling = current.usages.find((item) => item.usageKey === siblingKey);
    return sibling?.effective ?? null;
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
        // 合并行（main-chat / general-chat）以**后端算好的 effective** 为准：
        // 它与真实运行规则是同一份（general-chat 显式优先），不能再拿 main-chat
        // 自己的 resolved 冒充「当前实际使用」——两份配置不一致时那会显示一个、
        // 实际调用另一个。effective 不可用时如实说不可用，不显示旧的 resolved 目标。
        if (MERGED_CHAT_KEYS.includes(usage.usageKey)) {
          const merged = effectiveOf(usage);
          if (merged) {
            if (!merged.ok || !merged.usable) {
              return {
                usageKey: usage.usageKey,
                label: usage.label,
                followsDefault: false,
                credentialId: null,
                modelId: null,
                connectionName: null,
                providerName: null,
                resolved: null,
                note:
                  "实际生效目标不可用：" +
                  (merged.unavailableReason || "运行时没有解析到可用目标"),
                status: factStatus(usage),
              };
            }
            const effectiveConnection = connectionOf(merged.credentialId);
            return {
              usageKey: usage.usageKey,
              label: usage.label,
              // 只有真正落在默认模型 / 政策回退时才算「跟随默认」。
              followsDefault: merged.source === "default" || merged.source === "policy",
              credentialId: merged.credentialId,
              modelId: merged.modelId,
              connectionName: effectiveConnection?.name || null,
              providerName: effectiveConnection?.providerName || null,
              resolved:
                merged.credentialId && merged.modelId
                  ? { credentialId: merged.credentialId, modelId: merged.modelId }
                  : null,
              note: usage.note,
              status: factStatus(usage),
            };
          }
        }
        // 其它用途 / 老后端：跟随默认的以「后端解析后的真实目标」为准，显式分配直接用分配值。
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
          // 只读区要能区分「分配会生效」「沿用现有处理方式」「当前未接通」，
          // 否则视频/语音/嵌入会显示成「尚未解析」，看起来像还没加载完。
          note: usage.note,
          status: factStatus(usage),
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
    // 一次请求整体写：合并行（main-chat + general-chat）**不能**分成两次保存 ——
    // 第二次失败会留下半个配置；后端在一个 SQLite transaction 里全写或全回滚。
    await modelCatalogApi.setUsages({
      assignments: keys.map((usageKey) => ({
        usageKey,
        credentialId: target?.credentialId ?? null,
        modelId: target?.modelId ?? null,
      })),
    });
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
  <!-- data-testid 是给设置页壳与验收脚本用的稳定锚点：外壳的搜索定位与视觉核查
       需要知道「模型面板已经真的挂上了」，而不是靠标题文案猜。 -->
  <div class="panel" data-testid="models-panel">
    <!-- 分区标题与安全说明由外层 ModelServicesSection 提供，这里不再重复一遍标题/说明。 -->
    <div class="panel__bar">
      <button type="button" class="qio-btn mini quiet" :disabled="loading" :aria-busy="loading" @click="reload">
        {{ loading ? "读取中…" : "重新读取" }}
      </button>
    </div>

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
/* 面板不再套第二层"面"，也不重复分区标题；三块之间只留一条中性细线。 */
.panel { display: flex; flex-direction: column; gap: 14px; }
.panel__bar { display: flex; justify-content: flex-end; margin-bottom: -8px; }
.divider { border: none; border-top: 1px solid var(--border-subtle); margin: 4px 0; }
.loading { font-size: var(--fs-xs); color: var(--text-muted); }
.qio-feedback.err { color: var(--danger); }
</style>
