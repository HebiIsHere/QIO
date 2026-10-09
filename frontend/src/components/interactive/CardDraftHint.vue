<!--
  卡片草稿的保存提示（子智能体 A 负责，渲染位置由 BoardCard.vue 决定）。

  契约：docs/interactive-mode-contract.md §9.5 / §10.4 / §10.5 / §11.2 / §11.3。

  为什么单独做成组件：草稿保存失败必须**贴近正在编辑的那张卡**说清楚，
  而不是在板面顶部挂一条全局提示（那会与「已保存 / 已提交」两个主状态打架，
  也离用户正在编辑的对象太远）。组件只读 stores/interactive.ts 的状态，
  重试只重写草稿，不建卡、不提交板面、不调用 QIO。

  这里要分开说清三件事（§11.3 要求「区分服务器结果与本机结果」）：
  - 服务器草稿保存结果（draftStates）；
  - **本机恢复记录**的写入结果（draftLocalStates）——服务器已经成功时，
    绝不把它显示成服务器保存失败；只有服务器还没保存成功时，它才真的影响恢复能力；
  - 清除（§11.2）：服务器确认之前说「正在清除 / 清除没同步成功」，不说「已清除」；
  - **本机副本真的没删掉**（条目 13）：明确选择或清空草稿之后本机那份没删掉时，
    如实说明「重开后可能又出现」并给重试；版本守卫有意保留（不算失败）与「本来就没有」都不显示。

  用法（BoardCard.vue 的编辑区里渲染任意一处即可）：
    <CardDraftHint :card-id="card.id" />        // 键按 cardDraftKey(card.id) 推导
    <CardDraftHint draft-key="card:xxx" />      // 已经有键时直接传
    // 关闭态（编辑器没打开）也要渲染本组件：调用点用「确有未决冲突」守卫（§12.1）
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { cardDraftKey, cardIdFromDraftKey } from "../../interactive/drafts";

const props = defineProps<{
  /** 卡片 id：内部按契约的 cardDraftKey(cardId) 组成草稿键 */
  cardId?: string;
  /** 已算好的草稿键（与 cardId 二选一，优先用它） */
  draftKey?: string;
}>();

const store = useInteractiveStore();
const retrying = ref(false);

const key = computed(() => {
  if (props.draftKey) return props.draftKey;
  if (props.cardId) return cardDraftKey(props.cardId);
  // 没给具体对象时退回「最近编辑的那一条」：至少提示不会指向别的卡片
  return store.lastDraftKey;
});

/** 卡片 id：没有 cardId 时从草稿键里取（两种键都认） */
const cardId = computed(() => props.cardId ?? cardIdFromDraftKey(key.value) ?? key.value);
const state = computed(() => store.draftStateFor(key.value));
const local = computed(() => store.draftLocalStateFor(key.value));
const removal = computed(() => store.draftRemovalStateFor(key.value));
const conflict = computed(() => store.draftConflictFor(cardId.value));

/**
 * 本机副本「真的没删掉」的原因（条目 13）：store 的 draftLocalRemovalErrorFor。
 *
 * 只有 storage-failure（真的删失败）才非 null；版本守卫有意保留与「本来就没有记录」都是 null，
 * 界面不许把它们显示成删除失败。
 */
const localRemovalError = computed(() => store.draftLocalRemovalErrorFor(cardId.value) || null);
/** 别的分支已经给了重试入口（同一次重试会一并补做本机删除），不重复放按钮 */
const hasOtherRetry = computed(
  () =>
    removal.value.status === "error" ||
    Boolean(conflict.value) ||
    state.value.status === "error" ||
    localAtRisk.value,
);

/**
 * 本机恢复副本失败**且服务器还没保存成功**时才提示：
 * 这时「关掉页面还能不能恢复」真的受影响，必须如实说明并提供重试（§11.3）。
 * 服务器已经保存成功时，恢复能力由服务器提供（重新打开会重新拉取草稿），
 * 这时不能显示成服务器保存失败，也不需要制造一条吓人的失败提示。
 */
const localAtRisk = computed(() => !local.value.ok && state.value.status !== "saved");

/** idle 且本机没出问题时什么都不显示（不该在板面上长期占位） */
const visible = computed(
  () =>
    Boolean(key.value) &&
    (Boolean(conflict.value) ||
      removal.value.status !== "idle" ||
      state.value.status !== "idle" ||
      localAtRisk.value ||
      Boolean(localRemovalError.value)),
);
const isError = computed(
  () =>
    state.value.status === "error" ||
    removal.value.status === "error" ||
    localAtRisk.value ||
    Boolean(localRemovalError.value) ||
    Boolean(conflict.value),
);

async function retry() {
  if (retrying.value) return;
  retrying.value = true;
  try {
    // 只重写草稿：不建卡、不提交、不调 QIO（本机那一路失败过的也会一起补上）
    await store.retryDraftSave(key.value);
  } finally {
    retrying.value = false;
  }
}

function choose(choice: "local" | "server") {
  store.resolveDraftConflict(cardId.value, choice);
}
</script>

<template>
  <p
    v-if="visible"
    class="card-draft-hint"
    :class="{ err: isError }"
    :role="isError ? 'alert' : 'status'"
    :aria-live="isError ? 'assertive' : 'polite'"
    :title="state.error ?? removal.error ?? local.error ?? ''"
    data-im="card-draft-hint"
  >
    <!-- 清除还没同步成功：说清「哪件事没成」，不说「已清除」 -->
    <template v-if="removal.status === 'error'">
      <span class="msg" data-im="card-draft-removal-error">
        这份草稿的清除还没同步成功：{{ removal.error || "原因未知" }}（这份旧草稿刷新后可能重新出现）
      </span>
      <button class="retry" type="button" data-im="card-draft-retry" :disabled="retrying" @click="retry">
        {{ retrying ? "重试中…" : "重试清除" }}
      </button>
    </template>

    <!-- 无法判定新旧：两份都还在，选一份继续（不静默丢弃、不自动覆盖） -->
    <template v-else-if="conflict">
      <span class="msg" data-im="card-draft-conflict">
        本机与服务器上各有一份不同的草稿，两份都留着，先选一份继续编辑
      </span>
      <button class="retry" type="button" data-im="card-draft-keep-local" @click="choose('local')">
        用本机的
      </button>
      <button class="retry" type="button" data-im="card-draft-keep-server" @click="choose('server')">
        用服务器上的
      </button>
    </template>

    <!-- 服务器保存失败（本机那一路若也失败，如实一起说明） -->
    <template v-else-if="state.status === 'error'">
      <span class="msg" data-im="card-draft-error">
        草稿未保存：{{ state.error || "原因未知" }}
      </span>
      <!--
        本机副本写成功时才能说「关掉重开还在」（§11.3：不许把「内容还在当前页面」说成
        「关闭后一定能恢复」）；写失败时只说明限制，不承诺。
      -->
      <span v-if="localAtRisk" class="msg" data-im="card-draft-local-error">
        本机也没能留下恢复副本：{{ local.error || "原因未知" }}（现在关闭页面就恢复不到这次未完成的输入）
      </span>
      <span v-else class="msg" data-im="card-draft-error-local-kept">
        完整内容已保留在本机，关闭后重开仍能继续编辑
      </span>
      <button class="retry" type="button" data-im="card-draft-retry" :disabled="retrying" @click="retry">
        {{ retrying ? "重试中…" : "重试保存" }}
      </button>
    </template>

    <!-- 清除正在等服务器确认 -->
    <template v-else-if="removal.status === 'pending'">
      <span class="msg mono">正在清除这份草稿…</span>
    </template>

    <!-- 服务器还没保存成功、本机恢复副本也没写进去：如实说明恢复能力 -->
    <template v-else-if="localAtRisk">
      <span class="msg" data-im="card-draft-local-error">
        草稿还没保存成功；本机也没能留下恢复副本：{{ local.error || "原因未知" }}（现在关闭页面就恢复不到这次未完成的输入）
      </span>
      <button class="retry" type="button" data-im="card-draft-retry" :disabled="retrying" @click="retry">
        {{ retrying ? "重试中…" : "重试保护" }}
      </button>
    </template>

    <span v-else-if="state.status === 'saving'" class="msg mono">草稿保存中…</span>
    <span v-else-if="state.status === 'saved'" class="msg mono">草稿已保存</span>
    <span v-else class="msg mono">草稿待保存</span>

    <!-- 本机副本真的没删掉（条目 13）：说清后果并给重试；与「清除没同步」是两件事，各自说清 -->
    <template v-if="localRemovalError">
      <span class="msg" data-im="card-draft-local-removal-error">
        这份本机副本没能删掉，重开后可能又出现：{{ localRemovalError }}
      </span>
      <button
        v-if="!hasOtherRetry"
        class="retry"
        type="button"
        data-im="card-draft-local-removal-retry"
        :disabled="retrying"
        @click="retry"
      >
        {{ retrying ? "重试中…" : "重试" }}
      </button>
    </template>
  </p>
</template>

<style scoped>
/* 一行轻量提示：贴着编辑对象，不铺底、不加阴影（比卡片本身更轻） */
.card-draft-hint {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  margin: var(--sp-1) 0 0;
  font-family: var(--sans);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  color: var(--text-muted);
}
.card-draft-hint.err {
  color: var(--danger);
}
.msg {
  min-width: 0;
  /* 原因可能很长（后端原话）：换行显示，不截断成看不懂的省略号 */
  overflow-wrap: anywhere;
}
.retry {
  flex: none;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--link);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: 1px var(--sp-2);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.retry:hover:not(:disabled) {
  border-color: var(--link);
}
.retry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.retry:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
