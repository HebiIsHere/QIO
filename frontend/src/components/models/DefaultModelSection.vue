<script setup lang="ts">
/**
 * 默认模型：日常对话（主对话 / 通用对话 / 写代码 / 子任务这类跟随默认的用途）用
 * 哪条连接的哪个模型。
 *
 * 与「默认连接」（凭据层的 is_default）不是一回事，所以这里只选择「连接 + 模型」。
 * 候选里不可用的模型照样列出并写明原因（不静默过滤、不伪装可用）。
 */
import { computed, ref } from "vue";
import CandidatePicker from "./CandidatePicker.vue";
import type { CatalogResponse, ModelConnection } from "../../services/modelCatalog";

const props = defineProps<{
  connections: ModelConnection[];
  catalog: CatalogResponse | null;
  busy?: boolean;
  error?: string;
}>();
const emit = defineEmits<{
  save: [target: { credentialId: string; modelId: string }];
  /** #65：失败后重试「刚才那次选择」（父级保留原目标） */
  retry: [];
}>();

const picking = ref(false);

const current = computed(() => props.catalog?.default ?? null);
const currentConnection = computed(() =>
  current.value
    ? props.connections.find((c) => c.credentialId === current.value?.credentialId) ?? null
    : null,
);
const currentModel = computed(() =>
  currentConnection.value && current.value
    ? currentConnection.value.models.find((m) => m.modelId === current.value?.modelId) ?? null
    : null,
);
const currentUsable = computed(() => currentModel.value?.usable ?? false);
const currentReason = computed(() => currentModel.value?.unavailableReason ?? "");

const mainChat = computed(
  () => props.catalog?.usages.find((u) => u.usageKey === "main-chat") ?? null,
);

/**
 * 当前对话实际在用的目标：优先后端算好的 effective（与运行同一份规则），
 * 老后端退回 resolved。没有默认模型时也能如实说明「现在用的是哪一条」。
 */
const actualChat = computed(() => {
  const usage = mainChat.value;
  if (!usage) return null;
  const effective = usage.effective;
  if (effective) {
    if (!effective.ok || !effective.credentialId || !effective.modelId) {
      return { text: "", reason: effective.unavailableReason || "运行时没有解析到可用目标" };
    }
    const name = props.connections.find((c) => c.credentialId === effective.credentialId)?.name;
    return {
      text: (name || effective.credentialId) + " · " + effective.modelId,
      reason: effective.usable ? "" : effective.unavailableReason,
    };
  }
  const resolved = usage.resolved;
  if (!resolved) return null;
  return {
    text: (resolved.credentialName || resolved.credentialId) + " · " + resolved.modelId,
    reason: resolved.usable ? "" : resolved.unavailableReason,
  };
});

function pick(target: { credentialId: string; modelId: string } | null) {
  picking.value = false;
  if (target) emit("save", target);
}
</script>

<template>
  <section class="section" aria-labelledby="default-model-title">
    <header class="section__head">
      <div>
        <h3 id="default-model-title" class="section__title">默认模型</h3>
      </div>
      <button type="button" class="qio-btn mini" :aria-expanded="picking" @click="picking = !picking">
        {{ current ? "更换默认模型" : "选择默认模型" }}
      </button>
    </header>

    <div v-if="current" class="current">
      <span class="current__name">{{ currentConnection?.name || current.credentialId }}</span>
      <span class="current__model mono">{{ current.modelId }}</span>
      <span class="qio-state" :class="currentUsable ? 'ok' : 'err'">
        {{ currentUsable ? "当前可用" : "当前不可用" }}
      </span>
      <span v-if="currentModel" class="qio-state" :class="currentModel.verifyState === 'verified' ? 'ok' : 'quiet'">
        {{ currentModel.verifyState === "verified" ? "模型已验证" : "这个模型尚未验证" }}
      </span>
      <span v-if="!currentUsable && currentReason" class="current__reason">{{ currentReason }}</span>
    </div>
    <p v-else class="muted">
      还没有设置默认模型。日常对话目前沿用现有可用配置；要固定下来，请在下面选择默认模型。
    </p>
    <p v-if="actualChat?.text" class="muted mono">
      现在实际在用：{{ actualChat.text }}
      <span v-if="actualChat.reason">（不可用：{{ actualChat.reason }}）</span>
    </p>

    <p v-if="error" class="qio-feedback err" role="alert">{{ error }}</p>
    <button
      v-if="error"
      type="button"
      class="qio-btn mini quiet"
      data-testid="default-model-retry"
      @click="emit('retry')"
    >
      重试保存
    </button>

    <CandidatePicker
      v-if="picking"
      :connections="connections"
      :selected="current"
      :usage-label="'默认模型'"
      @pick="pick"
      @cancel="picking = false"
    />
    <p v-if="busy" class="muted" role="status">正在保存默认模型…</p>
  </section>
</template>

<style scoped>
.section { display: flex; flex-direction: column; gap: 8px; }
.section__head { display: flex; align-items: flex-start; gap: 12px; justify-content: space-between; }
.section__title { margin: 0; font-size: var(--fs-sm); color: var(--text-strong); }
/* 当前值是一行，不再包一层灰底盒子：用一条中性细线把"结论"和下面的说明分开。 */
.current {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 10px 0 2px; border: 0; border-top: 1px solid var(--border-subtle);
  border-radius: 0; background: none;
}
.current__name { font-size: var(--fs-xs); color: var(--text-strong); font-weight: 600; }
.current__model { font-size: var(--fs-xs); color: var(--text-secondary); }
.current__reason { flex-basis: 100%; font-size: 10px; color: var(--warning); }
.muted { margin: 0; font-size: 10.5px; color: var(--text-muted); line-height: 1.5; }
</style>