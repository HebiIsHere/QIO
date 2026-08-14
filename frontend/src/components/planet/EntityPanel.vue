<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type EntityAttribute, type EntityCard } from "../../services/api";
import QInput from "../ui/QInput.vue";

const props = defineProps<{ openCardId?: string }>();

const cards = ref<EntityCard[]>([]);
const q = ref("");
const loading = ref(false);
const opened = ref<EntityCard | null>(null);
const toast = ref("");
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

function showToast(text: string) {
  toast.value = text;
  window.setTimeout(() => (toast.value = ""), 2200);
}

async function load() {
  loading.value = true;
  try {
    const r = await api.listEntities();
    cards.value = r.entities;
  } catch (e) {
    console.error("[entity] load failed:", e);
    showToast("加载实体失败");
  } finally {
    loading.value = false;
  }
}

function openCard(card: EntityCard) {
  opened.value = card;
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
  try {
    const r = await api.reviseEntity(opened.value.id, {
      summary: draft.value.summary.trim(),
      kind: draft.value.kind.trim() || undefined,
      aliases: draft.value.aliases,
      attributes: draft.value.attributes,
    });
    if (r.entity) openCard(r.entity);
    showToast("已保存");
    await load();
  } catch (e) {
    console.error(e);
    showToast("保存失败");
  }
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
  try {
    const r = await api.addEntityRelation(opened.value.id, { type, target });
    if (r.entity) openCard(r.entity);
    draft.value.relType = "";
    draft.value.relTarget = "";
    showToast("已添加关系");
    await load();
  } catch (e) {
    console.error(e);
    showToast("添加关系失败");
  }
}

async function removeRel(type: string, target: string) {
  if (!opened.value) return;
  try {
    const r = await api.removeEntityRelation(opened.value.id, { type, target });
    if (r.entity) openCard(r.entity);
    showToast("已删除关系");
    await load();
  } catch (e) {
    console.error(e);
    showToast("删除关系失败");
  }
}

async function revokeCard() {
  if (!opened.value) return;
  if (!window.confirm(`归档实体「${opened.value.name}」？`)) return;
  try {
    await api.revokeEntity(opened.value.id);
    opened.value = null;
    showToast("已归档");
    await load();
  } catch (e) {
    console.error(e);
    showToast("归档失败");
  }
}

watch(
  () => props.openCardId,
  (id) => {
    if (!id) return;
    const card = cards.value.find((c) => c.id === id);
    if (card) openCard(card);
  },
);

onMounted(async () => {
  await load();
  if (props.openCardId) {
    const card = cards.value.find((c) => c.id === props.openCardId);
    if (card) openCard(card);
  }
});
</script>

<template>
  <div class="epanel">
    <template v-if="!opened">
      <QInput v-model="q" placeholder="搜索实体（名称/别名）…" />
      <div v-if="loading" class="hint">加载中…</div>
      <ul v-else class="e-list">
        <li v-for="c in filtered" :key="c.id" class="e-item" @click="openCard(c)">
          <div class="e-name">{{ c.name }}</div>
          <div class="e-meta mono">
            <span v-if="c.kind" class="qio-badge">{{ c.kind }}</span>
            {{ c.attributes.length }} 属性 · {{ c.relations.length }} 关系
          </div>
        </li>
        <li v-if="!filtered.length && !loading" class="hint">无实体卡。</li>
      </ul>
    </template>

    <template v-else>
      <div class="e-detail">
        <div class="e-detail-head">
          <button class="qio-btn mini" @click="opened = null">← 返回</button>
          <span class="e-title serif">{{ opened.name }}</span>
          <button class="qio-btn mini danger e-revoke" @click="revokeCard">归档</button>
        </div>

        <label class="field"><span class="label">摘要</span>
          <textarea v-model="draft.summary" class="qio-input e-summary"></textarea>
        </label>
        <label class="field"><span class="label">类型</span>
          <input v-model="draft.kind" class="qio-input" />
        </label>

        <div class="field"><span class="label">别名</span>
          <div class="chips">
            <span v-for="a in draft.aliases" :key="a" class="chip">{{ a }} <button type="button" class="chip-x" @click="removeAlias(a)">✕</button></span>
          </div>
          <div class="row">
            <input v-model="draft.aliasInput" class="qio-input grow" placeholder="添加别名" @keyup.enter="addAlias" />
            <button class="qio-btn mini" @click="addAlias">添加</button>
          </div>
        </div>

        <div class="field"><span class="label">属性（键=值 · 置信度）</span>
          <div class="attr-table">
            <div v-for="a in draft.attributes" :key="a.key" class="attr-row">
              <span class="grow">{{ a.key }} = {{ a.value }}</span>
              <span class="mono conf">{{ a.confidence.toFixed(2) }}</span>
              <button class="qio-btn mini" @click="removeAttr(a.key)">✕</button>
            </div>
            <div class="attr-row add">
              <input v-model="draft.attrKey" class="qio-input e-attr-key" placeholder="键" />
              <input v-model="draft.attrValue" class="qio-input e-attr-value" placeholder="值" />
              <input v-model="draft.attrConf" class="qio-input e-attr-conf" type="number" min="0" max="1" step="0.05" />
              <button class="qio-btn mini e-attr-add" @click="addAttr">+</button>
            </div>
          </div>
        </div>

        <div class="field"><span class="label">关系（类型 → 目标）</span>
          <div class="rel-list">
            <div v-for="r in opened.relations" :key="r.type + r.target" class="rel-row">
              <span class="grow">{{ r.type }} → {{ r.target }}</span>
              <button class="qio-btn mini e-rel-del" @click="removeRel(r.type, r.target)">✕</button>
            </div>
            <div class="rel-row add">
              <input v-model="draft.relType" class="qio-input e-rel-type" placeholder="类型" />
              <input v-model="draft.relTarget" class="qio-input e-rel-target" placeholder="目标实体" list="entity-names" />
              <datalist id="entity-names">
                <option v-for="c in cards" :key="c.id" :value="c.name"></option>
              </datalist>
              <button class="qio-btn mini e-rel-add" @click="addRel">+</button>
            </div>
          </div>
        </div>

        <div class="row e-actions">
          <button class="qio-btn mini primary e-save" @click="saveCard">保存</button>
        </div>
      </div>
    </template>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>

<style scoped>
.epanel { display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px; }
.e-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.e-item { border: 1px solid var(--border-subtle); border-radius: 10px; padding: 10px; cursor: pointer; background: var(--bg-inset); }
.e-item:hover { border-color: var(--accent); }
.e-name { font-size: 14px; color: var(--text-strong); }
.e-meta { font-size: 11px; color: var(--text-muted); margin-top: 3px; display: flex; gap: 6px; align-items: center; }
.e-detail { display: flex; flex-direction: column; gap: 10px; }
.e-detail-head { display: flex; align-items: center; gap: 8px; }
.e-title { flex: 1; font-size: 16px; color: var(--text-strong); }
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
.toast { position: fixed; top: 20px; right: 20px; z-index: 60; padding: 10px 16px; border-radius: 12px; font-size: 12.5px; background: var(--bg-surface); border: 1px solid var(--border-strong); color: var(--text-primary); }
</style>
