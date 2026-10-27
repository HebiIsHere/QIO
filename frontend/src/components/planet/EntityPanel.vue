<script setup lang="ts">
/**
 * 实体面板（B · 管理流程）。
 *
 * 信息架构：列表 → 点条目 → 详情 → 返回列表（列表用 v-show 保活，返回恢复滚动位置）。
 *
 * 草稿化编辑（本次改版的关键要求）：
 * - 点「修正」进入**明确的草稿态**；改摘要/类型/别名/属性/**关系增删**都只改本地草稿；
 * - 只点「保存」才提交，且是**一次**提交：字段与关系增删进同一个请求、后端同一事务；
 * - 取消丢弃本次修改；保存失败保留草稿与可读错误；
 * - 编辑期间卡被别处改过（409 / revision 变化）→ 提示冲突、保留用户输入、不静默覆盖；
 * - 返回列表时若有未保存修改，先走确认层，不悄悄丢掉。
 *
 * 基线能力全部保留：摘要/类型/别名/属性（含置信度）/关系、身份与创建依据、证据数、
 * 归档、这张卡的更新候选（采纳/丢弃），以及关系目标实体建议。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { getActivePinia } from "pinia";
import {
  api,
  conflictCurrentVersion,
  isVersionConflict,
  type EntityAttribute,
  type EntityCard,
  type EntityRelation,
} from "../../services/api";
import QInput from "../ui/QInput.vue";
import QConfirm from "../ui/QConfirm.vue";
import QIcon from "../ui/QIcon.vue";
import EntityCandidates from "./EntityCandidates.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { useActionFeedback } from "../../composables/useActionFeedback";
import { usePlanetPendingStore } from "../../stores/planetPending";

const props = withDefaults(
  defineProps<{
    openCardId?: string;
    openByNodeId?: string;
    focusId?: string | null;
  }>(),
  { openCardId: "", openByNodeId: "", focusId: null },
);
const emit = defineEmits<{ changed: [] }>();

const cards = ref<EntityCard[]>([]);
const q = ref("");
const loading = ref(false);
/** 读取失败原因：失败必须自己可见，不能显示成「没有实体」 */
const loadError = ref("");
const feedback = useActionFeedback();
/**
 * 待处理角标 store：A 负责渲染，B 负责在写入成功后刷新。
 * 单测/画廊环境没有 pinia 时跳过（与 QConfirm / QModal 的处理一致），不改变业务行为。
 */
const pendingStore = getActivePinia() ? usePlanetPendingStore() : null;
function refreshPendingCount() {
  void pendingStore?.refresh();
}
/** 归档确认（就地展开，不用浏览器原生 confirm） */
const confirmRevoke = ref(false);

const notice = ref("");
let noticeTimer: ReturnType<typeof setTimeout> | null = null;
function announce(text: string) {
  notice.value = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.value = "";
  }, 4200);
}

// -- 列表 ↔ 详情 -------------------------------------------------------------

type PanelView = "list" | "detail";
const view = ref<PanelView>("list");
const activeId = ref<string | null>(null);
const rootRef = ref<HTMLElement | null>(null);
let listScrollTop = 0;

const activeCard = computed<EntityCard | null>(
  () => cards.value.find((c) => c.id === activeId.value) ?? null,
);

interface DraftRelation {
  type: string;
  target: string;
}
interface DraftAttr {
  key: string;
  value: string;
  confidence: number;
}

/** 草稿：全部字段都在本地，保存前不写任何后端状态。 */
const draft = ref({
  summary: "",
  kind: "",
  aliases: [] as string[],
  attributes: [] as DraftAttr[],
  relations: [] as DraftRelation[],
  aliasInput: "",
  attrKey: "",
  attrValue: "",
  attrConf: "0.8",
  relType: "",
  relTarget: "",
});
const editing = ref(false);
const saving = ref(false);
const saveError = ref("");
const conflict = ref<{ message: string; remoteSummary: string; remoteRevision: number | null } | null>(
  null,
);
const pendingLeave = ref(false);
/** 开始编辑时看到的服务端版本（CAS 基准 + 「有没有改过」的比较基准）。 */
const baseline = ref<{
  summary: string;
  kind: string;
  aliases: string[];
  attributes: DraftAttr[];
  relations: DraftRelation[];
  revision: number | null;
}>({ summary: "", kind: "", aliases: [], attributes: [], relations: [], revision: null });

const relSuggestOpen = ref(false);
const relSuggestions = computed(() => {
  const query = draft.value.relTarget.trim().toLowerCase();
  if (!query) return [];
  return cards.value
    .map((c) => c.name)
    .filter((name) => name.toLowerCase().includes(query))
    .slice(0, 6);
});
function pickRelTarget(name: string) {
  draft.value.relTarget = name;
  relSuggestOpen.value = false;
}
function onRelTargetBlur() {
  // 延迟收起：允许 mousedown 先完成点选
  setTimeout(() => {
    relSuggestOpen.value = false;
  }, 120);
}

const filtered = computed(() => {
  const query = q.value.trim().toLowerCase();
  return cards.value.filter((c) => {
    if (!query) return true;
    return c.name.toLowerCase().includes(query) || c.aliases.some((a) => a.toLowerCase().includes(query));
  });
});

function sameRelations(a: DraftRelation[], b: DraftRelation[]): boolean {
  if (a.length !== b.length) return false;
  const key = (r: DraftRelation) => r.type + "\u0000" + r.target;
  const left = a.map(key).sort();
  const right = b.map(key).sort();
  return left.every((v, i) => v === right[i]);
}

function sameAttributes(a: DraftAttr[], b: DraftAttr[]): boolean {
  if (a.length !== b.length) return false;
  const key = (x: DraftAttr) => x.key + "\u0000" + x.value + "\u0000" + String(x.confidence);
  const left = a.map(key).sort();
  const right = b.map(key).sort();
  return left.every((v, i) => v === right[i]);
}

const dirty = computed(() => {
  if (!editing.value) return false;
  const base = baseline.value;
  return (
    draft.value.summary.trim() !== base.summary.trim() ||
    draft.value.kind.trim() !== base.kind.trim() ||
    draft.value.aliases.join("\u0000") !== base.aliases.join("\u0000") ||
    !sameAttributes(draft.value.attributes, base.attributes) ||
    !sameRelations(draft.value.relations, base.relations)
  );
});

function resetDraftState() {
  editing.value = false;
  saveError.value = "";
  conflict.value = null;
  pendingLeave.value = false;
  relSuggestOpen.value = false;
}

function scrollRootTo(top: number) {
  const root = rootRef.value;
  if (!root) return;
  root.scrollTop = Math.max(0, top);
}

function openCard(card: EntityCard) {
  if (view.value === "detail" && activeId.value === card.id) return;
  if (view.value === "list") listScrollTop = rootRef.value?.scrollTop ?? 0;
  resetDraftState();
  confirmRevoke.value = false;
  activeId.value = card.id;
  view.value = "detail";
  void nextTick(() => scrollRootTo(0));
}

function leaveDetail() {
  resetDraftState();
  confirmRevoke.value = false;
  view.value = "list";
  activeId.value = null;
  const top = listScrollTop;
  void nextTick(() => scrollRootTo(top));
}

function requestBack() {
  if (dirty.value) {
    pendingLeave.value = true;
    return;
  }
  leaveDetail();
}

function discardAndLeave() {
  pendingLeave.value = false;
  leaveDetail();
}

// -- 读取 -------------------------------------------------------------------

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const r = await api.listEntities();
    cards.value = r.entities;
    if (
      view.value === "detail" &&
      activeId.value &&
      !cards.value.some((c) => c.id === activeId.value)
    ) {
      activeId.value = null;
    }
  } catch (e) {
    console.error("[entity] load failed:", e);
    loadError.value = "加载实体失败：" + (e as Error).message;
  } finally {
    loading.value = false;
  }
}

async function afterMutation() {
  await load();
  emit("changed");
  refreshPendingCount();
}

// -- 草稿进入 / 退出 ---------------------------------------------------------

function startEdit(card: EntityCard) {
  draft.value = {
    summary: card.summary,
    kind: card.kind ?? "",
    aliases: [...card.aliases],
    attributes: card.attributes.map((a) => ({
      key: a.key,
      value: a.value,
      confidence: a.confidence,
    })),
    relations: card.relations.map((r) => ({ type: r.type, target: r.target })),
    aliasInput: "",
    attrKey: "",
    attrValue: "",
    attrConf: "0.8",
    relType: "",
    relTarget: "",
  };
  baseline.value = {
    summary: card.summary,
    kind: card.kind ?? "",
    aliases: [...card.aliases],
    attributes: card.attributes.map((a) => ({
      key: a.key,
      value: a.value,
      confidence: a.confidence,
    })),
    relations: card.relations.map((r) => ({ type: r.type, target: r.target })),
    revision: typeof card.revision === "number" ? card.revision : null,
  };
  editing.value = true;
  saveError.value = "";
  conflict.value = null;
}

function cancelEdit() {
  editing.value = false;
  saveError.value = "";
  conflict.value = null;
}

// -- 草稿内的本地编辑（不写后端） --------------------------------------------

function addAlias() {
  const a = draft.value.aliasInput.trim();
  if (a && !draft.value.aliases.includes(a)) draft.value.aliases.push(a);
  draft.value.aliasInput = "";
}

function removeAlias(a: string) {
  draft.value.aliases = draft.value.aliases.filter((x) => x !== a);
}

function addAttr() {
  const key = draft.value.attrKey.trim();
  const value = draft.value.attrValue.trim();
  if (!key || !value) return;
  const conf = Math.max(0, Math.min(1, Number(draft.value.attrConf) || 0.8));
  const hit = draft.value.attributes.find((a) => a.key === key);
  if (hit) {
    hit.value = value;
    hit.confidence = conf;
  } else {
    draft.value.attributes.push({ key, value, confidence: conf });
  }
  draft.value.attrKey = "";
  draft.value.attrValue = "";
}

function removeAttr(key: string) {
  draft.value.attributes = draft.value.attributes.filter((a) => a.key !== key);
}

function addRel() {
  const type = draft.value.relType.trim();
  const target = draft.value.relTarget.trim();
  if (!type || !target) return;
  const exists = draft.value.relations.some((r) => r.type === type && r.target === target);
  if (!exists) draft.value.relations.push({ type, target });
  draft.value.relType = "";
  draft.value.relTarget = "";
}

function removeRel(type: string, target: string) {
  draft.value.relations = draft.value.relations.filter(
    (r) => !(r.type === type && r.target === target),
  );
}

// -- 保存（一次提交，字段 + 关系增删） ---------------------------------------

function relationsAdded(): DraftRelation[] {
  return draft.value.relations.filter(
    (r) => !baseline.value.relations.some((b) => b.type === r.type && b.target === r.target),
  );
}
function relationsRemoved(): DraftRelation[] {
  return baseline.value.relations.filter(
    (r) => !draft.value.relations.some((d) => d.type === r.type && d.target === r.target),
  );
}

async function saveDraft() {
  const card = activeCard.value;
  if (!card || saving.value) return;
  saving.value = true;
  saveError.value = "";
  conflict.value = null;
  const add = relationsAdded();
  const remove = relationsRemoved();
  try {
    const payload: Parameters<typeof api.reviseEntity>[1] = {
      summary: draft.value.summary.trim(),
      kind: draft.value.kind.trim(),
      aliases: [...draft.value.aliases],
      attributes: draft.value.attributes.map((a) => ({
        key: a.key,
        value: a.value,
        confidence: a.confidence,
      })),
      expected_revision: baseline.value.revision,
    };
    if (add.length || remove.length) {
      payload.relations = {
        add: add.map((r) => ({ type: r.type, target: r.target })),
        remove: remove.map((r) => ({ type: r.type, target: r.target })),
      };
    }
    await api.reviseEntity(card.id, payload);
    editing.value = false;
    await afterMutation();
    announce("已保存（字段与关系一次提交）。");
  } catch (e) {
    if (isVersionConflict(e)) {
      await onConflict(e);
    } else {
      saveError.value = "保存没有成功：" + (e as Error).message + "。草稿还在，可以重试。";
    }
  } finally {
    saving.value = false;
  }
}

/** 409：这张卡在编辑期间被改动过（可能被自动提炼或另一个入口写了）。 */
async function onConflict(error: unknown) {
  const remoteRevision = conflictCurrentVersion(error);
  await load();
  const fresh = activeCard.value;
  baseline.value = {
    summary: fresh?.summary ?? baseline.value.summary,
    kind: fresh?.kind ?? baseline.value.kind,
    aliases: fresh ? [...fresh.aliases] : [...baseline.value.aliases],
    attributes: fresh
      ? fresh.attributes.map((a) => ({ key: a.key, value: a.value, confidence: a.confidence }))
      : [...baseline.value.attributes],
    relations: fresh
      ? fresh.relations.map((r) => ({ type: r.type, target: r.target }))
      : [...baseline.value.relations],
    revision: typeof fresh?.revision === "number" ? fresh.revision : remoteRevision,
  };
  conflict.value = {
    message: fresh
      ? "这张卡在你编辑期间被改动过。你的修改还在，没有被覆盖；下面是服务端当前的摘要，确认后再决定是否再次保存（再次保存会以最新版本为基础提交）。"
      : "这张卡已经不在当前列表里了。你的修改还在，可以复制后返回列表。",
    remoteSummary: fresh?.summary ?? "",
    remoteRevision: typeof fresh?.revision === "number" ? fresh.revision : remoteRevision,
  };
}

/** 编辑期间权威数据变了（同一面板里重新读到了新版本）：提示冲突，不覆盖草稿。 */
watch(
  () => activeCard.value,
  (next) => {
    if (!editing.value || !next) return;
    const revision = typeof next.revision === "number" ? next.revision : null;
    if (
      revision !== null &&
      baseline.value.revision !== null &&
      revision !== baseline.value.revision
    ) {
      conflict.value = {
        message:
          "这张卡在服务端已经有了新的版本。你的修改还在，没有被覆盖；可以查看服务端当前摘要后再决定。",
        remoteSummary: next.summary,
        remoteRevision: revision,
      };
    }
  },
  { deep: false },
);

// -- 归档 / 候选 -------------------------------------------------------------

async function revokeCard() {
  const card = activeCard.value;
  if (!card) return;
  confirmRevoke.value = false;
  await feedback.run("revoke", async () => {
    await api.revokeEntity(card.id);
    await afterMutation();
    leaveDetail();
  }, { okText: "已归档", failText: "归档没有完成" });
}

/**
 * 候选被采纳 / 丢弃之后：重新取一次权威数据（不靠本地猜），详情里的 activeCard 是
 * 从 cards 计算出来的，所以阅读态会立刻显示真实的持久值；列表页不因此跳进详情。
 */
async function onCandidatesChanged(_entityId: string) {
  await load();
  emit("changed");
  refreshPendingCount();
}

// -- 外部定位（A 的搜索结果 / 记忆页跳转） ------------------------------------

function openByProps() {
  if (props.openCardId) {
    const card = cards.value.find((c) => c.id === props.openCardId);
    if (card) {
      openCard(card);
      return;
    }
  }
  if (props.openByNodeId) {
    const card = cards.value.find((c) => c.node_id === props.openByNodeId);
    if (card) {
      openCard(card);
      return;
    }
  }
  if (props.focusId) {
    const card = cards.value.find((c) => c.id === props.focusId);
    if (card) openCard(card);
  }
}

watch(
  () => [props.openCardId, props.openByNodeId, props.focusId] as const,
  () => openByProps(),
);
watch(
  () => cards.value.length,
  () => openByProps(),
);

async function bootstrap() {
  await load();
  openByProps();
  refreshPendingCount();
}

void bootstrap();

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
});
</script>

<template>
  <div
    ref="rootRef"
    class="epanel"
    v-qio-scrollbar
    data-qio-fit
    data-scroll="entity-list"
  >
    <!-- 列表：v-show 保活（返回时搜索与滚动位置都在） -->
    <div v-show="view === 'list'" class="e-list-view" data-e-list>
      <QInput v-model="q" placeholder="搜索实体（名称/别名）…" data-e-search />
      <div v-if="loading && !cards.length" class="hint">加载中…</div>
      <!-- 失败与「没有数据」必须区分：失败给原因和重试，不伪装成空集合 -->
      <div v-else-if="loadError" class="e-load-error" role="alert">
        <span>{{ loadError }}</span>
        <button class="qio-btn mini" type="button" @click="load">重试</button>
      </div>
      <ul v-else class="e-list">
        <li
          v-for="c in filtered"
          :key="c.id"
          class="e-item qio-card qio-card--quiet"
          :data-e-id="c.id"
          role="button"
          tabindex="0"
          @click="openCard(c)"
          @keydown.enter.prevent="openCard(c)"
          @keydown.space.prevent="openCard(c)"
        >
          <!-- 名称一行（长名省略 + 悬停全名），类别标签贴右；元数据另起一行左对齐 -->
          <div class="e-item-head">
            <span class="e-name">{{ c.name }}</span>
            <span class="e-item-spacer"></span>
            <span v-if="c.kind" class="qio-tag e-kind">{{ c.kind }}</span>
          </div>
          <div class="e-item-meta mono">{{ c.attributes.length }} 属性 · {{ c.relations.length }} 关系</div>
        </li>
        <li v-if="!filtered.length" class="hint e-empty">
          <template v-if="cards.length">没有匹配「{{ q.trim() }}」的实体卡。</template>
          <template v-else>暂无实体卡。</template>
          <button v-if="cards.length" class="link e-clear-search" type="button" @click="q = ''">清除搜索</button>
        </li>
      </ul>
      <!--
        跨卡片的待处理候选（A04，基线能力保留）：默认收起，安静地挂在列表这一层，
        让「有东西等我决定」在打开某张卡之前就看得见。「待处理」入口里也有同一份清单
        （PendingItems），两处读的是同一个真实接口，不各自造数据。
      -->
      <EntityCandidates @changed="onCandidatesChanged" />
    </div>

    <!-- 详情 -->
    <div v-show="view === 'detail'" class="e-detail-view e-detail" data-e-detail>
      <div class="e-detail-head">
        <button class="qio-btn mini quiet e-back" type="button" data-e-back @click="requestBack">
          <QIcon name="chevron-left" :size="12" /> 返回
        </button>
        <span class="e-title serif">{{ activeCard?.name ?? "实体" }}</span>
        <span
          v-if="activeCard && activeCard.state"
          class="qio-state"
          :class="activeCard.state === 'active' ? 'ok' : 'quiet'"
        >{{ activeCard.state === "active" ? "生效中" : "已归档" }}</span>
        <span class="spacer"></span>
        <button
          v-if="activeCard && !editing"
          class="qio-btn mini e-edit-open"
          type="button"
          data-e-edit
          @click="startEdit(activeCard)"
        >修正</button>
        <button
          v-if="activeCard && !editing"
          class="qio-btn mini quiet e-revoke"
          type="button"
          @click="confirmRevoke = true"
        >归档</button>
      </div>

      <p v-if="notice" class="e-notice" role="status">{{ notice }}</p>

      <div v-if="!activeCard" class="e-missing">
        <p class="hint">这张实体卡已经不在当前列表里（可能已被归档）。</p>
        <button class="qio-btn mini primary" type="button" @click="leaveDetail">返回列表</button>
      </div>

      <template v-else>
        <!-- 未保存修改确认层（返回列表前；不悄悄丢） -->
        <div v-if="pendingLeave" class="e-guard" data-unsaved-guard role="alertdialog" aria-label="未保存的修改">
          <p class="e-guard-title">还有未保存的修改</p>
          <p class="e-guard-detail">返回列表会丢弃这次修改；已保存的内容不受影响。</p>
          <div class="row e-guard-actions">
            <button class="qio-btn mini" type="button" data-unsaved-keep @click="pendingLeave = false">继续编辑</button>
            <button class="qio-btn mini danger-solid" type="button" data-unsaved-discard @click="discardAndLeave">
              放弃修改并返回
            </button>
          </div>
        </div>

        <!-- 冲突：不覆盖草稿 -->
        <div v-if="conflict" class="e-conflict" role="alert">
          <p class="e-conflict-text">{{ conflict.message }}</p>
          <div v-if="conflict.remoteSummary" class="e-conflict-remote">
            <span class="e-label">服务端当前摘要（v{{ conflict.remoteRevision ?? "?" }}）</span>
            <p class="e-read-summary">{{ conflict.remoteSummary }}</p>
          </div>
          <div class="row">
            <button class="qio-btn mini quiet" type="button" @click="load">重新读取</button>
            <button class="qio-btn mini quiet" type="button" @click="conflict = null">知道了</button>
          </div>
        </div>

        <!-- 阅读态：摘要与字段组是主体，没有输入框 -->
        <div v-if="!editing" class="e-card qio-card">
          <p v-if="activeCard.summary" class="e-read-summary">{{ activeCard.summary }}</p>
          <p v-else class="hint">暂无摘要。</p>
          <div v-if="activeCard.kind" class="field e-kind-field">
            <span class="label">类别</span>
            <p class="e-read-kind">{{ activeCard.kind }}</p>
          </div>
          <!-- Phase 1 C：身份与创建依据要可读（判断「这张卡是不是同一个对象」的依据） -->
          <div v-if="activeCard.identity" class="field e-identity-field">
            <span class="label">身份</span>
            <p class="e-read-identity">{{ activeCard.identity }}</p>
          </div>
          <div v-if="activeCard.creation_reason" class="field e-creation-field">
            <span class="label">创建依据</span>
            <p class="e-read-creation">{{ activeCard.creation_reason }}</p>
          </div>
          <p v-if="activeCard.evidence_ids && activeCard.evidence_ids.length" class="hint e-evidence-count">
            依据 {{ activeCard.evidence_ids.length }} 条
          </p>
          <div class="field">
            <span class="label">别名</span>
            <div class="chips">
              <span v-for="a in activeCard.aliases" :key="a" class="qio-tag">{{ a }}</span>
              <span v-if="!activeCard.aliases.length" class="hint">无</span>
            </div>
          </div>
          <div class="field">
            <span class="label">属性</span>
            <div class="attr-table">
              <div v-for="a in activeCard.attributes" :key="a.key" class="attr-row">
                <span class="grow">{{ a.key }} = {{ a.value }}</span>
                <span class="mono conf">{{ a.confidence.toFixed(2) }}</span>
              </div>
              <p v-if="!activeCard.attributes.length" class="hint">暂无属性。</p>
            </div>
          </div>
          <div class="field">
            <span class="label">关系</span>
            <div class="rel-list">
              <div v-for="r in activeCard.relations" :key="r.type + r.target" class="rel-row">
                <span class="grow">{{ r.type }} → {{ r.target }}</span>
              </div>
              <p v-if="!activeCard.relations.length" class="hint">暂无关系。</p>
            </div>
          </div>
          <dl class="e-detail-meta">
            <div class="e-meta-row">
              <dt>修订</dt>
              <dd class="mono">v{{ activeCard.revision ?? 0 }}</dd>
            </div>
            <div class="e-meta-row">
              <dt>时间</dt>
              <dd class="mono">创建 {{ activeCard.created_at || "未记录" }} · 更新 {{ activeCard.updated_at || "未记录" }}</dd>
            </div>
            <div class="e-meta-row">
              <dt>节点</dt>
              <dd class="mono">{{ activeCard.node_id || "未关联节点" }}</dd>
            </div>
          </dl>
        </div>

        <!-- 草稿态：字段与关系全部在本地，保存才提交 -->
        <div v-else class="e-card qio-card e-draft" data-e-draft>
          <div class="e-draft-banner" role="status">
            草稿状态：下面的修改还没有提交，点「保存」才会一次写入（保留取消）。
          </div>
          <label class="field"><span class="label">摘要</span>
            <textarea v-model="draft.summary" class="qio-inline-edit e-summary"></textarea>
          </label>
          <label class="field"><span class="label">类别</span>
            <input v-model="draft.kind" class="qio-inline-edit e-kind" />
          </label>

          <div class="field"><span class="label">别名</span>
            <div class="chips">
              <span v-for="a in draft.aliases" :key="a" class="qio-tag e-alias">
                {{ a }}
                <button
                  type="button"
                  class="chip-x"
                  :aria-label="'移除别名：' + a"
                  @click="removeAlias(a)"
                >
                  <QIcon name="close" :size="10" />
                </button>
              </span>
            </div>
            <div class="row">
              <input
                v-model="draft.aliasInput"
                class="qio-inline-edit grow"
                placeholder="添加别名"
                @keyup.enter="addAlias"
              />
              <button class="qio-btn mini" type="button" @click="addAlias">添加</button>
            </div>
          </div>

          <div class="field"><span class="label">属性（键=值 · 置信度）</span>
            <div class="attr-table">
              <div v-for="a in draft.attributes" :key="a.key" class="attr-row e-attr-edit">
                <span class="e-attr-key-text">{{ a.key }}</span>
                <input v-model="a.value" class="qio-inline-edit grow" :aria-label="'属性值：' + a.key" />
                <input
                  v-model.number="a.confidence"
                  class="qio-inline-edit e-attr-conf"
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  :aria-label="'置信度：' + a.key"
                />
                <button
                  class="qio-btn mini quiet e-attr-del"
                  type="button"
                  :aria-label="'删除属性：' + a.key"
                  @click="removeAttr(a.key)"
                >
                  <QIcon name="close" :size="11" />
                </button>
              </div>
              <div class="attr-row add">
                <input v-model="draft.attrKey" class="qio-inline-edit e-attr-key" placeholder="键" />
                <input v-model="draft.attrValue" class="qio-inline-edit e-attr-value" placeholder="值" />
                <input
                  v-model="draft.attrConf"
                  class="qio-inline-edit e-attr-conf"
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  aria-label="新属性的置信度"
                />
                <button class="qio-btn mini e-attr-add" type="button" @click="addAttr">+</button>
              </div>
            </div>
          </div>

          <div class="field"><span class="label">关系（类型 → 目标）</span>
            <div class="rel-list">
              <div v-for="r in draft.relations" :key="r.type + r.target" class="rel-row">
                <span class="grow">{{ r.type }} → {{ r.target }}</span>
                <button
                  class="qio-btn mini quiet e-rel-del"
                  type="button"
                  :aria-label="'删除关系：' + r.type + ' → ' + r.target"
                  @click="removeRel(r.type, r.target)"
                >
                  <QIcon name="close" :size="11" />
                </button>
              </div>
              <div class="rel-row add">
                <input v-model="draft.relType" class="qio-inline-edit e-rel-type" placeholder="类型" />
                <input
                  v-model="draft.relTarget"
                  class="qio-inline-edit e-rel-target"
                  placeholder="目标实体"
                  :aria-expanded="relSuggestions.length > 0"
                  @focus="relSuggestOpen = true"
                  @blur="onRelTargetBlur"
                />
                <!-- 目标实体建议：QIO 自绘（不用原生 datalist 的默认外观），点选就地填入 -->
                <ul v-if="relSuggestOpen && relSuggestions.length" class="e-rel-suggest" role="listbox" aria-label="目标实体建议">
                  <li v-for="name in relSuggestions" :key="name">
                    <button
                      type="button"
                      class="e-rel-suggest-item"
                      role="option"
                      @mousedown.prevent="pickRelTarget(name)"
                    >{{ name }}</button>
                  </li>
                </ul>
                <button class="qio-btn mini e-rel-add" type="button" @click="addRel">
                  <QIcon name="plus" :size="11" />
                </button>
              </div>
            </div>
          </div>

          <div class="row e-actions">
            <button class="qio-btn mini quiet e-cancel" type="button" data-e-cancel @click="cancelEdit">取消</button>
            <button
              class="qio-btn mini primary e-save"
              type="button"
              data-e-save
              :disabled="saving"
              :aria-busy="saving ? 'true' : undefined"
              @click="saveDraft"
            >
              {{ saving ? "保存中…" : "保存" }}
            </button>
          </div>
          <p v-if="saveError" class="e-feedback err" role="alert">{{ saveError }}</p>
        </div>

        <!-- 更新候选：只在这张卡的阅读态出现（编辑期不显示，两套写入不混在一起） -->
        <EntityCandidates
          v-if="!editing && activeCard.id"
          :entity-id="activeCard.id"
          @changed="onCandidatesChanged"
        />

        <!-- 归档：就地确认，说明影响（实体卡归档后不再参与回答） -->
        <QConfirm
          v-if="confirmRevoke && activeCard"
          :open="true"
          variant="inline"
          tone="danger"
          title="归档这张实体卡？"
          :detail="'「' + activeCard.name + '」归档后不再参与回答与检索，别名与属性都会保留。'"
          confirm-text="归档"
          @confirm="revokeCard"
          @cancel="confirmRevoke = false"
        />
        <p v-if="feedback.stateOf('revoke') === 'failed'" class="e-feedback err" role="alert">
          {{ feedback.errorOf("revoke") }}
        </p>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 面板正文层：只有这一层纵向滚动（列表与详情在同一个阅读流里切换）。 */
.epanel {
  display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px;
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: clip; overscroll-behavior: contain;
}
.e-list-view { display: flex; flex-direction: column; gap: 8px; }
.e-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
/* 列表卡是列方向：显式 stretch，避免全局行式类把内容水平居中/缩窄。 */
.e-item {
  display: flex; flex-direction: column; align-items: stretch; justify-content: flex-start;
  gap: 3px; width: 100%; padding: var(--sp-3); border-radius: var(--qio-ui-radius);
  cursor: pointer; text-align: left;
  transition: background var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1);
}
.e-item:hover { background: var(--layer-hover); }
.e-item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.e-item-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.e-item-spacer { flex: 1; min-width: 0; }
.e-name {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 14.5px; color: var(--text-strong);
}
/* 列表卡的元数据行（与详情里的 .e-detail-meta 分开命名：曾经同名导致详情列的
   flex-direction:column + align-items:center 把列表文字水平居中） */
.e-item-meta { font-size: 11px; color: var(--text-muted); display: flex; gap: 6px; align-items: center; text-align: left; }
.e-detail-view { display: flex; flex-direction: column; gap: 10px; }
.e-detail-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.e-detail-head .spacer { flex: 1; }
.e-title { font-size: 16px; color: var(--text-strong); }
.e-card { display: flex; flex-direction: column; gap: 10px; padding: var(--sp-4); }
.e-read-summary { margin: 0; font-size: 13.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.e-read-kind, .e-read-identity, .e-read-creation { margin: 0; font-size: 13px; line-height: 1.65; color: var(--text-primary); overflow-wrap: anywhere; }
.field { display: flex; flex-direction: column; gap: 4px; }
.label, .e-label { font-size: 11px; color: var(--text-secondary); letter-spacing: 0.03em; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.chip-x {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; padding: 0; border: none; border-radius: var(--qio-ui-radius);
  background: none; color: var(--text-muted); cursor: pointer;
}
.chip-x:hover { color: var(--text-strong); background: var(--layer-hover); }
.attr-table, .rel-list { display: flex; flex-direction: column; gap: 5px; }
.attr-row, .rel-row { display: flex; align-items: center; gap: 6px; position: relative; }
.attr-row.add, .rel-row.add { margin-top: 2px; flex-wrap: wrap; }
.grow { flex: 1; min-width: 0; }
.conf { color: var(--text-muted); width: 44px; text-align: right; }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.e-actions { justify-content: flex-end; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; display: inline-flex; align-items: center; gap: 4px; }
.hint { font-size: 12px; color: var(--text-muted); }
.e-empty { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.e-load-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: var(--qio-ui-radius); font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
.e-feedback { font-size: 11.5px; }
.e-feedback.err { margin: 4px 0 0; color: var(--danger); }
.e-notice { margin: 0; font-size: 12px; color: var(--text-secondary); }
.e-missing { display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.e-attr-key-text { font-size: 12.5px; color: var(--text-secondary); min-width: 56px; }
.e-attr-conf { width: 72px; flex: none; }
.e-rel-type { width: 96px; flex: none; }
/* 草稿是明确的一层：一条横幅说明「还没提交」，字段整组是输入控件 */
.e-draft-banner {
  padding: 8px 10px; border: 1px dashed var(--accent); border-radius: var(--qio-ui-radius);
  background: var(--bg-inset); color: var(--text-secondary); font-size: 12px; line-height: 1.6;
}
.e-detail-meta { display: flex; flex-direction: column; align-items: stretch; gap: 6px; margin: 0; padding: 10px 0 0; border-top: 1px solid var(--border-subtle); }
.e-detail-meta-row { display: flex; gap: 10px; align-items: flex-start; }
.e-detail-meta-row dt { flex: none; width: 48px; font-size: 11px; color: var(--text-secondary); }
.e-detail-meta-row dd { margin: 0; min-width: 0; font-size: 12px; color: var(--text-muted); overflow-wrap: anywhere; }
.e-detail-meta-row dd.mono { font-family: var(--mono); font-size: 11.5px; }
.e-conflict {
  display: flex; flex-direction: column; gap: 6px; padding: 10px;
  border: 1px solid var(--warning); border-radius: var(--qio-ui-radius);
  background: var(--warning-soft); color: var(--warning);
}
.e-conflict-text { margin: 0; font-size: 12.5px; line-height: 1.65; overflow-wrap: anywhere; }
.e-conflict-remote { display: flex; flex-direction: column; gap: 4px; padding: 8px; border-radius: var(--qio-ui-radius); background: var(--bg-inset); }
.e-conflict-remote .e-read-summary { color: var(--text-primary); font-size: 13px; }
.e-guard {
  display: flex; flex-direction: column; gap: 6px; padding: 12px;
  border: 1px solid var(--border-strong); border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.e-guard-title { margin: 0; font-family: var(--serif); font-size: 14px; color: var(--text-strong); }
.e-guard-detail { margin: 0; font-size: 12.5px; line-height: 1.65; color: var(--text-secondary); }
.e-guard-actions { justify-content: flex-end; }
/* 目标实体建议：就地浮层（替代原生 datalist） */
.e-rel-suggest {
  position: absolute; left: 0; right: 88px; top: calc(100% + 4px); z-index: 5;
  list-style: none; margin: 0; padding: 4px;
  border: 1px solid var(--border-strong); border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated); box-shadow: var(--elev-floating);
  max-height: 220px; overflow-y: auto;
}
.e-rel-suggest-item {
  width: 100%; text-align: left; padding: 5px 8px; border: none; border-radius: var(--qio-ui-radius);
  background: none; color: var(--text-primary); font: inherit; font-size: 12.5px; cursor: pointer;
}
.e-rel-suggest-item:hover { background: var(--accent-soft); color: var(--text-strong); }
</style>
