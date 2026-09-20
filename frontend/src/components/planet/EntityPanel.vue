<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type EntityAttribute, type EntityCard } from "../../services/api";
import QInput from "../ui/QInput.vue";
import QConfirm from "../ui/QConfirm.vue";
import { useActionFeedback } from "../../composables/useActionFeedback";

const props = defineProps<{ openCardId?: string; openByNodeId?: string }>();

const cards = ref<EntityCard[]>([]);
const q = ref("");
const loading = ref(false);
/** 读取失败原因：失败必须自己可见，不能显示成「没有实体」 */
const loadError = ref("");
const opened = ref<EntityCard | null>(null);
/**
 * 阅读态 / 编辑态。
 * 第四阶段：实体卡默认是「读一遍这张卡」，不是「一堆输入框」。
 * 点「修正」才就地进入编辑；取消则丢弃草稿（重新从 cards 里取一次权威数据）。
 */
const editing = ref(false);
/** 归档确认（就地展开，不用浏览器原生 confirm） */
const confirmRevoke = ref(false);
/** 修改实体卡 / 关系 / 归档的反馈：留在卡片内，不再用全局 Toast */
const feedback = useActionFeedback();
const draft = ref({
  summary: "",
  kind: "",
  aliases: [] as string[],
  aliasInput: "",
  attributes: [] as EntityAttribute[],
  attrKey: "",
  attrValue: "",
  attrConf: "0.8",
  relType: "",
  relTarget: "",
});

const filtered = computed(() => {
  const query = q.value.trim().toLowerCase();
  return cards.value.filter((c) => {
    if (!query) return true;
    return c.name.toLowerCase().includes(query) || c.aliases.some((a) => a.toLowerCase().includes(query));
  });
});

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const r = await api.listEntities();
    cards.value = r.entities;
  } catch (e) {
    console.error("[entity] load failed:", e);
    loadError.value = `加载实体失败：${(e as Error).message}`;
  } finally {
    loading.value = false;
  }
}

function openCard(card: EntityCard) {
  opened.value = card;
  editing.value = false;
  confirmRevoke.value = false;
  draft.value = {
    summary: card.summary,
    kind: card.kind ?? "",
    aliases: [...card.aliases],
    aliasInput: "",
    attributes: card.attributes.map((a) => ({ ...a })),
    attrKey: "",
    attrValue: "",
    attrConf: "0.8",
    relType: "",
    relTarget: "",
  };
}

async function saveCard() {
  if (!opened.value) return;
  await feedback.run(
    "save",
    async () => {
      const r = await api.reviseEntity(opened.value!.id, {
        summary: draft.value.summary.trim(),
        kind: draft.value.kind.trim(),
        aliases: draft.value.aliases,
        attributes: draft.value.attributes,
      });
      if (r.entity) openCard(r.entity);
      await load();
    },
    { okText: "已保存", failText: "保存没有成功，卡片内容未改变" },
  );
}

/** 「保存」按钮：保存成功才退出编辑态（失败要留在原地让用户改） */
async function saveFromButton() {
  await saveCard();
  if (feedback.stateOf("save") !== "failed") editing.value = false;
}

/** 取消编辑：丢弃草稿，回到阅读态（重新取一次列表里的权威数据） */
function cancelEdit() {
  if (!opened.value) return;
  const fresh = cards.value.find((c) => c.id === opened.value!.id);
  openCard(fresh ?? opened.value);
}

function addAlias() {
  const a = draft.value.aliasInput.trim();
  if (a && !draft.value.aliases.includes(a)) draft.value.aliases.push(a);
  draft.value.aliasInput = "";
}

function removeAlias(a: string) {
  draft.value.aliases = draft.value.aliases.filter((x) => x !== a);
}

async function addAttr() {
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
  await saveCard();
}

function removeAttr(key: string) {
  draft.value.attributes = draft.value.attributes.filter((a) => a.key !== key);
}

async function addRel() {
  if (!opened.value) return;
  const type = draft.value.relType.trim();
  const target = draft.value.relTarget.trim();
  if (!type || !target) return;
  await feedback.run(
    "rel",
    async () => {
      const r = await api.addEntityRelation(opened.value!.id, { type, target });
      if (r.entity) openCard(r.entity);
      draft.value.relType = "";
      draft.value.relTarget = "";
      await load();
    },
    { okText: "已添加关系", failText: "添加关系没有成功" },
  );
}

async function removeRel(type: string, target: string) {
  if (!opened.value) return;
  await feedback.run(
    "rel",
    async () => {
      const r = await api.removeEntityRelation(opened.value!.id, { type, target });
      if (r.entity) openCard(r.entity);
      await load();
    },
    { okText: "已删除关系", failText: "删除关系没有成功" },
  );
}

async function revokeCard() {
  if (!opened.value) return;
  confirmRevoke.value = false;
  await feedback.run(
    "revoke",
    async () => {
      await api.revokeEntity(opened.value!.id);
      opened.value = null;
      await load();
    },
    { okText: "已归档", failText: "归档没有完成" },
  );
}

watch(
  () => props.openCardId,
  (id) => {
    if (!id) return;
    const card = cards.value.find((c) => c.id === id);
    if (card) openCard(card);
  },
);

watch(
  () => props.openByNodeId,
  (id) => {
    if (!id) return;
    const card = cards.value.find((c) => c.node_id === id);
    if (card) openCard(card);
  },
);

onMounted(async () => {
  await load();
  if (props.openCardId) {
    const card = cards.value.find((c) => c.id === props.openCardId);
    if (card) openCard(card);
  }
  if (props.openByNodeId) {
    const card = cards.value.find((c) => c.node_id === props.openByNodeId);
    if (card) openCard(card);
  }
});
</script>

<template>
  <div class="epanel">
    <template v-if="!opened">
      <QInput v-model="q" placeholder="搜索实体（名称/别名）…" />
      <div v-if="loading" class="hint">加载中…</div>
      <!-- 失败与「没有数据」必须区分：失败给原因和重试，不伪装成空集合 -->
      <div v-else-if="loadError" class="e-load-error" role="alert">
        <span>{{ loadError }}</span>
        <button class="qio-btn mini" type="button" @click="load">重试</button>
      </div>
      <ul v-else class="e-list">
        <li
          v-for="c in filtered"
          :key="c.id"
          class="e-item qio-card qio-card--quiet qio-row"
          role="button"
          tabindex="0"
          @click="openCard(c)"
          @keydown.enter.prevent="openCard(c)"
          @keydown.space.prevent="openCard(c)"
        >
          <div class="e-item-text">
            <div class="e-name">{{ c.name }}</div>
            <div class="e-meta mono">
              {{ c.attributes.length }} 属性 · {{ c.relations.length }} 关系
            </div>
          </div>
          <span v-if="c.kind" class="qio-tag e-kind">{{ c.kind }}</span>
        </li>
        <li v-if="!filtered.length" class="hint e-empty">
          <template v-if="cards.length">没有匹配「{{ q.trim() }}」的实体卡。</template>
          <template v-else>暂无实体卡。</template>
          <button v-if="cards.length" class="link e-clear-search" type="button" @click="q = ''">清除搜索</button>
        </li>
      </ul>
    </template>

    <template v-else>
      <div class="e-detail qio-card">
        <div class="e-detail-head">
          <button class="qio-btn mini quiet" type="button" @click="opened = null">← 返回</button>
          <span class="e-title serif">{{ opened.name }}</span>
          <span v-if="opened.state" class="qio-state" :class="opened.state === 'active' ? 'ok' : 'quiet'">
            {{ opened.state === "active" ? "生效中" : "已归档" }}
          </span>
          <span class="spacer"></span>
          <!-- 阅读态只在明处放一个动作；修正与归档是次要的，点开才进入 -->
          <button v-if="!editing" class="qio-btn mini e-edit-open" type="button" @click="editing = true">修正</button>
          <button v-if="!editing" class="qio-btn mini quiet e-revoke" type="button" @click="confirmRevoke = true">归档</button>
        </div>

        <!-- 阅读态：摘要与字段组是主体，没有输入框 -->
        <template v-if="!editing">
          <p v-if="opened.summary" class="e-read-summary">{{ opened.summary }}</p>
          <p v-else class="hint">暂无摘要。</p>
          <div class="field">
            <span class="label">别名</span>
            <div class="chips">
              <span v-for="a in opened.aliases" :key="a" class="qio-tag">{{ a }}</span>
              <span v-if="!opened.aliases.length" class="hint">无</span>
            </div>
          </div>
          <div class="field">
            <span class="label">属性</span>
            <div class="attr-table">
              <div v-for="a in opened.attributes" :key="a.key" class="attr-row">
                <span class="grow">{{ a.key }} = {{ a.value }}</span>
                <span class="mono conf">{{ a.confidence.toFixed(2) }}</span>
              </div>
              <p v-if="!opened.attributes.length" class="hint">暂无属性。</p>
            </div>
          </div>
          <div class="field">
            <span class="label">关系</span>
            <div class="rel-list">
              <div v-for="r in opened.relations" :key="r.type + r.target" class="rel-row">
                <span class="grow">{{ r.type }} → {{ r.target }}</span>
              </div>
              <p v-if="!opened.relations.length" class="hint">暂无关系。</p>
            </div>
          </div>
        </template>

        <!-- 编辑态：就地进入，保存/取消都在同一处 -->
        <template v-else>
          <label class="field"><span class="label">摘要</span>
            <textarea v-model="draft.summary" class="qio-inline-edit e-summary"></textarea>
          </label>
          <label class="field"><span class="label">类型</span>
            <input v-model="draft.kind" class="qio-inline-edit e-kind" />
          </label>

          <div class="field"><span class="label">别名</span>
            <div class="chips">
              <span v-for="a in draft.aliases" :key="a" class="qio-tag">{{ a }} <button type="button" class="chip-x" @click="removeAlias(a)">✕</button></span>
            </div>
            <div class="row">
              <input v-model="draft.aliasInput" class="qio-inline-edit grow" placeholder="添加别名" @keyup.enter="addAlias" />
              <button class="qio-btn mini" type="button" @click="addAlias">添加</button>
            </div>
          </div>

          <div class="field"><span class="label">属性（键=值 · 置信度）</span>
            <div class="attr-table">
              <div v-for="a in draft.attributes" :key="a.key" class="attr-row">
                <span class="grow">{{ a.key }} = {{ a.value }}</span>
                <span class="mono conf">{{ a.confidence.toFixed(2) }}</span>
                <button class="qio-btn mini quiet" type="button" @click="removeAttr(a.key)">✕</button>
              </div>
              <div class="attr-row add">
                <input v-model="draft.attrKey" class="qio-inline-edit e-attr-key" placeholder="键" />
                <input v-model="draft.attrValue" class="qio-inline-edit e-attr-value" placeholder="值" />
                <input v-model="draft.attrConf" class="qio-inline-edit e-attr-conf" type="number" min="0" max="1" step="0.05" />
                <button class="qio-btn mini e-attr-add" type="button" @click="addAttr">+</button>
              </div>
            </div>
          </div>

          <div class="field"><span class="label">关系（类型 → 目标）</span>
            <div class="rel-list">
              <div v-for="r in opened.relations" :key="r.type + r.target" class="rel-row">
                <span class="grow">{{ r.type }} → {{ r.target }}</span>
                <button
                  class="qio-btn mini quiet e-rel-del"
                  type="button"
                  :disabled="feedback.stateOf('rel') === 'busy'"
                  @click="removeRel(r.type, r.target)"
                >✕</button>
              </div>
              <div class="rel-row add">
                <input v-model="draft.relType" class="qio-inline-edit e-rel-type" placeholder="类型" />
                <input v-model="draft.relTarget" class="qio-inline-edit e-rel-target" placeholder="目标实体" list="entity-names" />
                <datalist id="entity-names">
                  <option v-for="c in cards" :key="c.id" :value="c.name"></option>
                </datalist>
                <button
                  class="qio-btn mini e-rel-add"
                  type="button"
                  :disabled="feedback.stateOf('rel') === 'busy'"
                  @click="addRel"
                >+</button>
              </div>
            </div>
            <p v-if="feedback.stateOf('rel') === 'failed'" class="e-feedback err" role="alert">
              {{ feedback.errorOf("rel") }}
            </p>
            <span v-else-if="feedback.stateOf('rel') === 'ok'" class="e-feedback ok" role="status">
              {{ feedback.okTextOf("rel") }}
            </span>
          </div>

          <div class="row e-actions">
            <span v-if="feedback.stateOf('save') === 'ok'" class="e-feedback ok" role="status">
              {{ feedback.okTextOf("save") }}
            </span>
            <button class="qio-btn mini quiet e-cancel" type="button" @click="cancelEdit">取消</button>
            <button
              class="qio-btn mini primary e-save"
              type="button"
              :disabled="feedback.stateOf('save') === 'busy'"
              @click="saveFromButton"
            >
              {{ feedback.stateOf("save") === "busy" ? "保存中…" : "保存" }}
            </button>
          </div>
        </template>

        <!-- 归档：就地确认，说明影响（实体卡归档后不再参与回答） -->
        <QConfirm
          v-if="confirmRevoke"
          :open="true"
          variant="inline"
          tone="danger"
          title="归档这张实体卡？"
          :detail="`「${opened.name}」归档后不再参与回答与检索，别名与属性都会保留。`"
          confirm-text="归档"
          @confirm="revokeCard"
          @cancel="confirmRevoke = false"
        />
        <p v-if="feedback.stateOf('save') === 'failed'" class="e-feedback err" role="alert">
          {{ feedback.errorOf("save") }}
        </p>
        <p v-if="feedback.stateOf('revoke') === 'failed'" class="e-feedback err" role="alert">
          {{ feedback.errorOf("revoke") }}
        </p>
      </div>
    </template>

  </div>
</template>

<style scoped>
.epanel { display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px; flex: 1 1 auto; min-height: 0; overflow-y: auto; }
.e-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
/* 列表项用一个可聚焦的行：hover / 焦点 / 点击反馈来自 `.qio-row` 与 `.qio-card` */
.e-item { padding: var(--sp-3); border-radius: var(--r-lg); }
.e-item-text { flex: 1; min-width: 0; }
.e-name { font-size: 14.5px; color: var(--text-strong); }
.e-meta { font-size: 11px; color: var(--text-muted); margin-top: 3px; display: flex; gap: 6px; align-items: center; }
.e-detail { display: flex; flex-direction: column; gap: 10px; padding: var(--sp-4); }
.e-detail-head { display: flex; align-items: center; gap: 8px; }
.e-detail-head .spacer { flex: 1; }
.e-title { flex: 1; font-size: 16px; color: var(--text-strong); }
/* 阅读态：摘要是主体，字段组整齐收敛（不再是满屏输入框） */
.e-read-summary { margin: 0; font-size: 13.5px; line-height: 1.7; color: var(--text-primary); }
.field { display: flex; flex-direction: column; gap: 4px; }
.label { font-size: 11px; color: var(--text-secondary); letter-spacing: 0.03em; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.chip { display: inline-flex; align-items: center; gap: 4px; background: var(--bg-accent-subtle); border-radius: 20px; padding: 3px 10px; font-size: 12px; }
.chip-x { background: none; border: none; color: var(--text-muted); cursor: pointer; padding: 0; }
.attr-table, .rel-list { display: flex; flex-direction: column; gap: 5px; }
.attr-row, .rel-row { display: flex; align-items: center; gap: 6px; }
.attr-row.add, .rel-row.add { margin-top: 2px; }
.grow { flex: 1; }
.conf { color: var(--text-muted); width: 44px; text-align: right; }
.row { display: flex; align-items: center; gap: 8px; }
.e-actions { justify-content: flex-end; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; border-radius: 8px; }
.qio-btn.mini.danger { color: var(--danger); border-color: var(--border-danger); }
.hint { font-size: 12px; color: var(--text-muted); }
.e-empty { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.e-load-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: 8px; font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
/* 操作反馈留在卡片内：成功短暂、失败保留 */
.e-feedback { font-size: 11.5px; }
.e-feedback.ok { color: var(--success); }
.e-feedback.err { margin: 4px 0 0; color: var(--danger); }
</style>
