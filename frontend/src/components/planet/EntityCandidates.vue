<script setup lang="ts">
/**
 * 「待处理候选」区块（A04 客户端接线，冻结契约 §3.4）。
 *
 * 为什么需要它：自动提炼出来的新值以前会直接写进实体卡 —— 用户手动写过的内容、
 * 历史来源未知的内容都可能被悄悄盖掉；被挡住的那部分现在留在后端候选队列里，
 * 但界面从来没把它呈现出来，用户既看不到也管不了。这里给出一处**安静、默认收起**
 * 的列表，让每一条候选都能被明确「采纳」或「丢弃」。
 *
 * 三条刻意的设计：
 * 1. **默认收起、不自动弹出**：待处理候选是一份等待清单，不是警报；重复提示属于
 *    规范明令禁止的行为。数量写在收起态的标题里，用户想看才展开。
 * 2. **不假装成功**：采纳/丢弃的结果只能来自后端。失败原样显示原因；409
 *    （这条实体已被改动）就地提示并给「刷新」，绝不把冲突显示成「已采纳」。
 * 3. **归档卡片不给点不动的按钮**：`card_archived` 的候选采纳按钮禁用并说明
 *    「该实体已归档；恢复实体是另一个动作」；丢弃仍然可用（它不改实体值）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  adoptCandidate,
  dismissCandidate,
  isCandidateConflict,
  listAllCandidates,
  listEntityCandidates,
  type EntityCandidateView,
} from "../../services/entityCandidatesApi";

const props = defineProps<{
  /** 传了就只看这张卡片的候选；不传就是跨卡片的待处理清单 */
  entityId?: string | null;
}>();

const emit = defineEmits<{ changed: [entityId: string] }>();

const open = ref(false);
const loading = ref(false);
/** 读取失败原因：失败必须自己可见，不能伪装成「没有待处理候选」 */
const loadError = ref("");
const candidates = ref<EntityCandidateView[]>([]);
const total = ref(0);
const truncated = ref(false);
/**
 * 正在处理的那几条（复合身份）。
 *
 * 用**集合**而不是单个 id：全局清单里不同实体可能撞同一个 `candidate_id`，
 * 一条在飞不该把别的实体那一行也变成「处理中…」/不可点。同一条（同一复合身份）
 * 仍然只允许一个请求在飞 —— 重复点击幂等。
 */
const busyIds = ref<string[]>([]);
/** 每条自己的就地结果（失败 / 冲突），成功不在这里占位 */
const rowNotice = ref<Record<string, { tone: "err" | "conflict"; text: string }>>({});
/** 区块级结果（成功）：放在列表外，列表被清空后它仍然在 */
const blockNotice = ref("");

/**
 * 请求代次：每一次 `load()` 领一个新号。
 *
 * 提交结果前必须同时校验「仍是最新一次请求」与「发起时的实体没被换掉」——
 * 旧实体 A 的响应（成功、失败、finally 三处）都要整段丢弃，否则 A 的候选会显示在
 * B 的面板上，而操作又会按响应里的 entity_id 发出去。组件卸载后同样作废。
 */
let loadSeq = 0;
/** 组件是否已卸载：卸载后任何迟到响应都不许再碰状态。 */
let disposed = false;

/** 这次请求发起时的实体（null = 全局跨卡片清单）。 */
function currentEntityId(): string | null {
  return props.entityId ?? null;
}

/** 这份结果是否仍属于「当前实体 + 最新一次请求」。 */
function belongs(seq: number, entityId: string | null): boolean {
  return !disposed && seq === loadSeq && currentEntityId() === entityId;
}

/**
 * 一条候选的**界面身份**。
 *
 * 后端 `_pending_id(field, value)` 只在单实体内唯一：不同实体相同字段+值会撞同一个
 * `candidate_id`（例如两个实体的「生日」都给 `pc_5531…`）。所以渲染 key、逐行提示与
 * 忙碌状态都必须用 `(entity_id, candidate_id)` 复合键；单实体视图里 candidate_id 虽然
 * 唯一，用复合键同样成立（不另造分支）。
 * 这里只改**界面身份**，**不动**持久化里的历史 candidate_id。
 */
function identityOf(c: EntityCandidateView): string {
  return `${c.entity_id}\u0000${c.candidate_id}`;
}

/** 这一条（复合身份）是否正在处理。 */
function isBusy(c: EntityCandidateView): boolean {
  return busyIds.value.includes(identityOf(c));
}
function markBusy(token: string) {
  if (!busyIds.value.includes(token)) busyIds.value = [...busyIds.value, token];
}
function clearBusy(token: string) {
  busyIds.value = busyIds.value.filter((t) => t !== token);
}

const count = computed(() => total.value);
const hiddenCount = computed(() => Math.max(0, total.value - candidates.value.length));
/**
 * 收起态的标题。
 * 读失败时**不写 0**：那会把「不知道」显示成「没有」，正是规范禁止的伪装。
 */
const headTitle = computed(() =>
  loadError.value && !count.value ? "待处理候选（读取失败）" : `待处理候选（${count.value}）`,
);

const REASON_LABELS: Record<string, string> = {
  stale_revision: "这条候选来自旧版本的卡片，需要你确认一次",
  user_value_conflict: "你已经手动写过这个字段，自动结果不会覆盖它",
  user_deleted: "你删掉过这个内容，自动结果不会把它加回来",
  card_archived: "这张实体卡已归档，不再自动更新",
  legacy_unknown_source: "这个字段的旧值来源不明，按你写的值保护",
};

const BLOCKED_LABELS: Record<string, string> = {
  card_archived: "该实体已归档；恢复实体是另一个动作",
  structural_conflict: "这是一个结构性冲突（例如别名归属），不支持一键采纳",
  unsupported_kind: "这种候选还不支持一键采纳",
};

/** 卡片是否已归档（后端给了状态就认它，没给就按 blocked_reason 判）。 */
function isArchived(c: EntityCandidateView): boolean {
  if (c.blocked_reason === "card_archived") return true;
  return Boolean(c.card_state) && c.card_state !== "active";
}

/** 采纳按钮能不能点：归档卡片与非「可采纳」候选都不给点了没效果的按钮。 */
function adoptDisabled(c: EntityCandidateView): boolean {
  return !c.adoptable || isArchived(c) || blockedReasonText(c) !== "";
}

/** 不能采纳时给用户的一句话原因。 */
function blockedReasonText(c: EntityCandidateView): string {
  if (isArchived(c)) return BLOCKED_LABELS.card_archived;
  if (!c.adoptable && c.blocked_reason) {
    return BLOCKED_LABELS[c.blocked_reason] ?? `这条候选现在不能采纳（${c.blocked_reason}）`;
  }
  if (!c.adoptable) return "这条候选现在不能采纳，你可以丢弃它";
  return "";
}

function fieldText(c: EntityCandidateView): string {
  return c.field_label?.trim() || c.field;
}

function reasonText(c: EntityCandidateView): string {
  if (c.reason_label?.trim()) return c.reason_label.trim();
  if (c.reason && REASON_LABELS[c.reason]) return REASON_LABELS[c.reason];
  return c.reason?.trim() || "有一个更合适的新值";
}

/** 值的可读形态：不做假设，拿不到就说「（空）」而不是显示 [object Object]。 */
function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "（空）";
  if (Array.isArray(v)) return v.length ? v.map((x) => formatValue(x)).join("、") : "（空）";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return "（无法显示）";
    }
  }
  const text = String(v);
  return text.trim() ? text : "（空）";
}

/** 某一条的就地结果（模板里少写一层索引与断言）。 */
function noticeOf(c: EntityCandidateView): { tone: "err" | "conflict"; text: string } | null {
  return rowNotice.value[identityOf(c)] ?? null;
}

async function load() {
  const seq = ++loadSeq;
  const entityId = currentEntityId();
  loading.value = true;
  loadError.value = "";
  try {
    if (entityId) {
      const r = await listEntityCandidates(entityId);
      // 迟到响应（实体已切换 / 已有更新的请求 / 已卸载）：数据一律不写回
      if (!belongs(seq, entityId)) return;
      candidates.value = r.candidates;
      total.value = r.total;
      truncated.value = r.truncated;
    } else {
      const r = await listAllCandidates(true);
      if (!belongs(seq, entityId)) return;
      candidates.value = r.candidates;
      total.value = r.total;
      truncated.value = r.truncated;
    }
    // 刷新之后展示的是真实持久状态：上一次的就地提示不再代表现状
    rowNotice.value = {};
  } catch (e) {
    // 失败也必须属于当前实体：旧实体的错误不能写到新面板上
    if (!belongs(seq, entityId)) return;
    loadError.value = `加载待处理候选失败：${(e as Error).message}`;
  } finally {
    // 只有仍属于当前实体、且自己还是最新一次请求的那次加载才允许解锁 ——
    // 旧请求的 finally 不得把新请求（切换实体后重新发起）的 loading 关掉
    if (belongs(seq, entityId)) loading.value = false;
  }
}

function toggle() {
  open.value = !open.value;
  // 展开时如果还没有数据（或上次读失败），补一次读取
  if (open.value && (!candidates.value.length || loadError.value)) void load();
}

function setRow(c: EntityCandidateView, tone: "err" | "conflict", text: string) {
  rowNotice.value = { ...rowNotice.value, [identityOf(c)]: { tone, text } };
}

async function decide(c: EntityCandidateView, action: "adopt" | "dismiss") {
  const token = identityOf(c);
  // 同一条幂等：同一条（复合身份）只发一个决定；别的实体那一行不受影响
  if (busyIds.value.includes(token)) return;
  // 操作也绑在「发起时的实体」上：实体切换后，这次决定的结果不再属于当前面板
  const entityId = currentEntityId();
  markBusy(token);
  blockNotice.value = "";
  const next = { ...rowNotice.value };
  delete next[token];
  rowNotice.value = next;
  const what = action === "adopt" ? "采纳" : "丢弃";
  try {
    // URL / body 仍用这条候选自己所属实体的 entity_id（复合身份只用于界面区分）
    const result =
      action === "adopt"
        ? await adoptCandidate(c.entity_id, c.candidate_id, c.card_revision)
        : await dismissCandidate(c.entity_id, c.candidate_id, c.card_revision);
    // 实体已切换 / 已卸载：这次结果整段丢弃，也不去刷新新面板的清单
    if (disposed || currentEntityId() !== entityId) return;
    if (result && result.ok === false) {
      // 后端明确说没有写进去：如实说明，不刷新成「成功」
      const blocked = result.blocked_reason ?? "";
      const text = blocked
        ? (BLOCKED_LABELS[blocked] ?? `这次没有${what}（${blocked}）`)
        : `这次没有${what}，实体内容未改变`;
      setRow(c, blocked === "card_archived" ? "conflict" : "err", text);
      return;
    }
    if (action === "adopt") {
      blockNotice.value = result?.already_resolved
        ? `「${fieldText(c)}」此前已经处理过，当前值就是候选值`
        : `已采纳「${fieldText(c)}」：${formatValue(c.candidate_value)}`;
    } else {
      blockNotice.value = result?.already_resolved
        ? `「${fieldText(c)}」这条候选此前已经处理过了`
        : `已丢弃「${fieldText(c)}」的候选值（实体当前值未改变）`;
    }
    await load(); // 展示真实持久状态，不靠本地猜（load 自带代次守卫）
    if (disposed || currentEntityId() !== entityId) return;
    emit("changed", c.entity_id);
  } catch (e) {
    if (disposed || currentEntityId() !== entityId) return;
    if (isCandidateConflict(e)) {
      setRow(c, "conflict", "这条实体已被改动，请刷新后重试");
    } else {
      setRow(c, "err", `${what}没有成功：${(e as Error).message}`);
    }
  } finally {
    // 只清自己那一条忙碌标记：切换实体后可能已经在处理别的候选，别替它解锁
    if (!disposed) clearBusy(token);
  }
}

/** 冲突后的「刷新」：只重新读权威状态，不重放刚才那次决定。 */
async function refresh() {
  blockNotice.value = "";
  await load();
}

watch(
  () => props.entityId,
  () => {
    // 切换实体：旧实体的数据、提示、错误与忙碌标记都必须先清掉 ——
    // 在飞的旧请求由下面的 load() 领新代次作废，绝不允许它的迟到响应写回来。
    candidates.value = [];
    total.value = 0;
    truncated.value = false;
    rowNotice.value = {};
    blockNotice.value = "";
    loadError.value = "";
    busyIds.value = [];
    void load();
  },
);

onMounted(() => {
  void load();
});

onUnmounted(() => {
  // 卸载后到达的响应一律丢弃（先作废代次，再标记 disposed）
  disposed = true;
  loadSeq += 1;
});
</script>

<template>
  <!--
    根条件包含 loadError / blockNotice：采纳掉最后一条候选之后列表会变空，
    如果整块跟着消失，用户点完什么都看不到（静默失败）。操作结果必须留在屏幕上。
  -->
  <div v-if="count || loadError || blockNotice" class="ec-block">
    <button
      class="ec-head"
      type="button"
      :aria-expanded="open"
      :aria-label="`${headTitle}，展开可以逐条采纳或丢弃`"
      @click="toggle"
    >
      <span class="ec-mark" aria-hidden="true">✎</span>
      <span class="ec-title">{{ headTitle }}</span>
      <span class="ec-chev" :class="{ open }" aria-hidden="true">⌄</span>
    </button>

    <div v-if="open" class="ec-panel" role="region" aria-label="待处理候选">
      <p class="ec-lead">
        这些是自动提炼出来、但没有写进卡片的新值（你手动写过的内容不会被自动覆盖）。逐条决定即可。
      </p>

      <div v-if="loading" class="ec-hint">加载中…</div>

      <div v-else-if="loadError" class="ec-error" role="alert">
        <span>{{ loadError }}</span>
        <button class="qio-btn mini quiet ec-retry" type="button" @click="refresh">重试</button>
      </div>

      <template v-else>
        <ul class="ec-list">
          <li v-for="c in candidates" :key="identityOf(c)" class="ec-row qio-card qio-card--quiet">
            <div class="ec-row-head">
              <span class="ec-field">{{ fieldText(c) }}</span>
              <span v-if="!entityId" class="ec-entity">{{ c.entity_name }}</span>
              <span v-if="isArchived(c)" class="qio-state quiet ec-archived">已归档</span>
            </div>

            <div class="ec-values">
              <div class="ec-value">
                <span class="ec-value-label">当前值</span>
                <span class="ec-value-text" :title="formatValue(c.current_value)">{{ formatValue(c.current_value) }}</span>
              </div>
              <div class="ec-value">
                <span class="ec-value-label">候选值</span>
                <span class="ec-value-text candidate" :title="formatValue(c.candidate_value)">{{ formatValue(c.candidate_value) }}</span>
              </div>
            </div>

            <p class="ec-reason">{{ reasonText(c) }}</p>

            <div class="ec-actions">
              <button
                class="qio-btn mini primary ec-adopt"
                type="button"
                :disabled="adoptDisabled(c) || isBusy(c)"
                :aria-label="`采纳这条候选：${fieldText(c)}`"
                @click="decide(c, 'adopt')"
              >
                {{ isBusy(c) ? "处理中…" : "采纳" }}
              </button>
              <button
                class="qio-btn mini quiet ec-dismiss"
                type="button"
                :disabled="isBusy(c)"
                :aria-label="`丢弃这条候选：${fieldText(c)}`"
                @click="decide(c, 'dismiss')"
              >
                丢弃
              </button>
            </div>

            <!-- 归档卡片：采纳按钮禁用，并说明「恢复实体」是另一个动作 -->
            <p v-if="blockedReasonText(c)" class="ec-blocked">{{ blockedReasonText(c) }}</p>

            <!-- 409 就地提示：不是普通失败，也不翻成「已采纳」 -->
            <p
              v-if="noticeOf(c)"
              class="ec-row-notice"
              :class="noticeOf(c)?.tone"
              role="alert"
            >
              <span>{{ noticeOf(c)?.text }}</span>
              <button
                v-if="noticeOf(c)?.tone === 'conflict'"
                class="qio-btn mini quiet ec-refresh"
                type="button"
                :disabled="loading"
                @click="refresh"
              >
                刷新
              </button>
            </p>
          </li>
          <li v-if="!candidates.length" class="ec-hint">没有待处理的候选了。</li>
        </ul>

        <p v-if="truncated && hiddenCount > 0" class="ec-hint ec-more">
          还有 {{ hiddenCount }} 条未显示（后端一次只返回一部分）。
        </p>
      </template>
    </div>

    <!-- 结果放在面板外：列表被清空、面板收起时它仍然可见 -->
    <p v-if="blockNotice" class="ec-notice" role="status" aria-live="polite">{{ blockNotice }}</p>
  </div>
</template>

<style scoped>
.ec-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
  font-family: var(--sans);
}
/* 收起态：一行安静的入口（不是警报条，也不抢焦点） */
.ec-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  cursor: pointer;
  text-align: left;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.ec-head:hover { border-color: var(--border-strong); color: var(--text-strong); }
.ec-head:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.ec-mark {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--accent-soft); color: var(--accent);
  font-size: var(--fs-xs);
}
.ec-title { flex: 1; }
.ec-chev { color: var(--text-muted); transition: transform var(--dur-fast) var(--ease); }
.ec-chev.open { transform: rotate(180deg); }

.ec-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-surface);
}
.ec-lead { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.ec-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.ec-row { display: flex; flex-direction: column; gap: 6px; padding: var(--sp-3); border-radius: var(--r-md); }
.ec-row-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ec-field { font-size: var(--fs-sm); color: var(--text-strong); }
.ec-entity { font-size: var(--fs-xs); color: var(--text-muted); }
.ec-archived { flex: none; }
.ec-values { display: flex; flex-direction: column; gap: 3px; }
.ec-value { display: flex; gap: 8px; align-items: baseline; font-size: var(--fs-sm); }
.ec-value-label { flex: none; width: 44px; font-size: var(--fs-xs); color: var(--text-secondary); }
/* 长值截断（省略号可见），DOM 里保留全文，title 与读屏软件都能拿到 */
.ec-value-text {
  flex: 1;
  min-width: 0;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ec-value-text.candidate { color: var(--link); }
.ec-reason { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.ec-actions { display: flex; gap: var(--sp-2); }
.ec-blocked { margin: 0; font-size: var(--fs-xs); color: var(--warning); line-height: 1.6; }
.ec-row-notice {
  margin: 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: var(--fs-xs); line-height: 1.6;
}
.ec-row-notice.err { color: var(--danger); }
.ec-row-notice.conflict { color: var(--warning); }
.ec-hint { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.ec-more { color: var(--text-secondary); }
.ec-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: var(--r-sm); font-size: var(--fs-xs);
  border: 1px solid var(--border-danger); background: var(--danger-soft); color: var(--danger);
}
.ec-notice { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: var(--fs-xs); border-radius: var(--r-sm); }
</style>
