<!--
  页面中央的影响确认框（结构由 D 建立；本轮「显示与入口」由 C 调整：文案层级、辅助说明、
  不出现开发术语；不改任何 store 行为）。

  契约 §1.6 / §8.1：
  - 用户要改**执行中任务**依赖的材料时，在**改动生效前**先用页面中央的确认框说明
    受影响的对象、任务与后果；不用顶部横幅承担这一步；
  - 确认 = 改动生效 + 相关任务暂停并保留进度；取消 = 改动不生效、任务继续；
    其他独立任务不受影响；
  - 不先改数据再询问：板面还没保存（store.saveNow 在预判命中时直接返回，pendingImpact 只是待决定）；
  - 这不是新的任务审批：已经批准过的任务不会被重新审批，只会被暂停并保留进度；
  - 键盘：打开时聚焦框内第一个按钮，Tab / Shift+Tab 限制在框内，Escape = 取消，
    关闭后焦点回到触发点；
  - 失败 / 取消后的部分撤回若还有需要决定的部分，用同一个框说明剩余改动与具体影响。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();

const dialog = ref<HTMLElement | null>(null);
const firstAction = ref<HTMLButtonElement | null>(null);
const busy = ref(false);
const notice = ref<string | null>(null);
const error = ref<string | null>(null);

/** 打开前焦点在哪：关闭后要还回去 */
let lastFocused: HTMLElement | null = null;

const affected = computed(() => store.pendingImpact?.affected ?? []);

/**
 * 服务端/主流程补充的说明（可选字段 note，可能为 undefined）。
 * 它是对「为什么又让你确认一次」的**辅助说明**，层级低于主决定：
 * 主决定永远是「确认 / 取消」两个按钮，note 只解释情况，不改变按钮含义。
 */
const impactNote = computed(() => String((store.pendingImpact as unknown as { note?: string } | null)?.note ?? "").trim());

/**
 * 撤回留下的「等待你决定」：同一批（正在执行 / 已暂停）任务失败或取消后，
 * 若还有需要决定的部分，用同一个框说明剩余改动与具体影响。
 */
const pendingReverts = computed(() =>
  store.intents.filter(
    (intent) =>
      (intent.revert?.pendingDecision?.length ?? 0) > 0 &&
      ["running", "paused", "failed", "cancelled"].includes(intent.status),
  ),
);

const open = computed(() => Boolean(store.pendingImpact) || pendingReverts.value.length > 0);
const mode = computed<"impact" | "revert">(() => (store.pendingImpact ? "impact" : "revert"));

const title = computed(() =>
  mode.value === "impact"
    ? "这次改动还没有生效：它会影响正在执行的任务"
    : "还有改动没有撤回：需要你决定剩下的部分",
);

function recallFocus(): void {
  const target = lastFocused;
  lastFocused = null;
  if (target && typeof target.focus === "function" && document.contains(target)) {
    target.focus();
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (!open.value) return;
  if (event.key === "Escape") {
    event.preventDefault();
    if (mode.value === "impact") cancel();
    else dismiss();
    return;
  }
  if (event.key !== "Tab") return;
  const root = dialog.value;
  if (!root) return;
  const focusables = [...root.querySelectorAll<HTMLElement>("button:not([disabled])")];
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  // 焦点限制在框内：到边界就回卷，绝不跑到页面其它地方
  if (event.shiftKey && (active === first || !root.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !root.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

watch(
  open,
  async (isOpen, wasOpen) => {
    if (typeof document === "undefined") return;
    if (isOpen && !wasOpen) {
      const active = document.activeElement as HTMLElement | null;
      lastFocused = active && active !== document.body ? active : null;
      notice.value = null;
      error.value = null;
      await nextTick();
      firstAction.value?.focus();
    } else if (!isOpen && wasOpen) {
      recallFocus();
    }
  },
  { immediate: true },
);

watch(mode, () => {
  notice.value = null;
  error.value = null;
});

if (typeof window !== "undefined") {
  window.addEventListener("keydown", onKeydown, true);
}
onBeforeUnmount(() => {
  if (typeof window !== "undefined") window.removeEventListener("keydown", onKeydown, true);
});

/** 确认：改动生效，相关任务暂停并保留进度（不是重新审批任务） */
async function confirm(): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    await store.confirmImpact();
    notice.value =
      "已确认：改动已经保存生效；受影响的任务已暂停并保留进度。" +
      "它们不会自动继续，需要你在任务浮层里确认后按当前材料继续；其他独立任务不受影响。";
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

/** 取消：改动不生效，任务继续（板面回到已保存状态） */
function cancel(): void {
  store.cancelImpact();
  notice.value = "已取消：这次改动没有生效，任务继续；其他独立任务不受影响。";
}

/** 只结束「等待你决定」的提示：不做任何板面改动 */
function dismiss(): void {
  store.dismissMaterialPaused();
  notice.value = "已结束这条提示：板面没有任何自动改动，你后来的修改都保留。";
}
</script>

<template>
  <div v-if="open" class="backdrop qio-confirm-scrim" data-im="impact-scrim" data-im-scrim="impact">
    <div
      ref="dialog"
      class="dialog qio-confirm qio-confirm--layer"
      data-im="impact-dialog"
      :data-impact-mode="mode"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="im-impact-title"
      aria-describedby="im-impact-body"
      tabindex="-1"
      @keydown.stop
      @keyup.stop
      @wheel.stop
    >
      <h2 id="im-impact-title" class="title">{{ title }}</h2>

      <div id="im-impact-body" class="body">
        <template v-if="mode === 'impact'">
          <!-- 主句说「发生了什么」，下一行说「两个选择各自意味着什么」：标题不再重复 -->
          <p class="lead">
            下面这些任务正在执行，而这次改动动了它们依赖的材料。<strong>改动还没有生效。</strong>
          </p>
          <p class="lead-sub">
            选择「继续」才会保存并让它们暂停（进度保留）；选择「取消」则这次改动不生效，任务继续。
            其他独立任务不受影响；这不是对任务的重新审批。
          </p>
          <p v-if="impactNote" class="note-aux" data-im="impact-note">{{ impactNote }}</p>
          <ul class="list">
            <li v-for="item in affected" :key="item.intentId" class="item" data-im="impact-item">
              <p class="task">任务：{{ item.title }}</p>
              <p v-if="item.materials.length" class="materials mono">
                受影响材料：{{ item.materials.join("、") }}
              </p>
              <p class="consequence">后果：{{ item.consequence }}</p>
            </li>
          </ul>
        </template>

        <template v-else>
          <p class="lead">
            这些任务失败或取消后，已经撤回了不受影响的部分；下面这些改动会影响其他工作，所以停在这里等你决定。
          </p>
          <p class="lead-sub">选择「继续」会按上面的说明继续处理；选择「取消」保持现状，不做任何改动。</p>
          <ul class="list">
            <li v-for="intent in pendingReverts" :key="intent.id" class="item" data-im="impact-item">
              <p class="task">任务：{{ intent.title }}</p>
              <p class="consequence">原因：{{ intent.revert?.reasonText || "未说明" }}</p>
              <ul class="pending">
                <!-- 不再显示内部决定项 id：它是开发标识，对用户没有意义（本轮验收点 4） -->
                <li v-for="item in intent.revert?.pendingDecision ?? []" :key="item.id">
                  {{ item.reason }}
                  <span class="impact">影响：{{ item.impact }}</span>
                </li>
              </ul>
            </li>
          </ul>
        </template>
      </div>

      <p v-if="notice" class="notice" role="status" data-im="impact-notice">{{ notice }}</p>
      <p v-if="error" class="error" role="alert" data-im="impact-error">操作失败：{{ error }}</p>

      <div class="actions qio-confirm__actions">
        <!-- 打开时聚焦这个按钮：默认动作是「不改动」 -->
        <button
          ref="firstAction"
          class="btn qio-btn quiet"
          type="button"
          data-im="impact-cancel"
          :disabled="busy"
          @click="mode === 'impact' ? cancel() : dismiss()"
        >
          {{ mode === "impact" ? "取消：改动不生效，任务继续" : "取消：保持现状，不做改动" }}
        </button>
        <button
          class="btn qio-btn primary"
          type="button"
          data-im="impact-continue"
          :disabled="busy"
          @click="mode === 'impact' ? confirm() : dismiss()"
        >
          {{
            mode === "impact"
              ? "继续：改动生效，相关任务暂停并保留进度"
              : "继续：按上面的说明处理剩余的改动"
          }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 遮罩与层级走既有原语（.qio-confirm-scrim 用令牌 --bg-overlay，不再自带 rgba 兜底） */
.backdrop {
  z-index: var(--im-z-dialog, 60);
}
.dialog {
  width: min(560px, calc(100vw - var(--sp-6)));
  max-height: min(80vh, 640px);
  overflow: auto;
  overscroll-behavior: contain;
  padding: var(--sp-5);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  color: var(--text-primary);
  box-shadow: var(--elev-overlay, var(--shadow-3));
}
.dialog:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.title {
  margin: 0 0 var(--sp-3);
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.body { display: flex; flex-direction: column; gap: var(--sp-2); }
/* 说明文字是用户做决定要读的东西：用正文级字号，不缩成元信息级 */
.lead {
  margin: 0;
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  color: var(--text-secondary);
}
/* 辅助说明层：比主句低一档（字号小一档、颜色更安静），用户可以先读主句做决定 */
.lead-sub {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-muted);
}
/* 需要重新核对时的补充说明：同一层级里的「注意」，用左边线而不是整块变色，避免抢走主决定 */
.note-aux {
  margin: 0;
  padding: var(--sp-1) var(--sp-3);
  border-left: 2px solid var(--warning);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-secondary);
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.item {
  border-left: 2px solid var(--danger);
  padding-left: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.task { margin: 0; font-size: var(--fs-base); color: var(--text-strong); }
.materials { margin: 0; font-size: var(--fs-sm); color: var(--text-secondary); }
.consequence {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-secondary);
}
.pending {
  margin: 0;
  padding-left: var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  line-height: var(--lh-base);
}
.impact { color: var(--warning); }
.notice,
.error {
  margin: var(--sp-3) 0 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
}
.notice { color: var(--text-secondary); }
.error { color: var(--danger); }
.actions {
  margin-top: var(--sp-4);
  display: flex;
  gap: var(--sp-3);
  flex-wrap: wrap;
  justify-content: flex-end;
}
/*
  按钮复用全局原语 .qio-btn（圆角 / 边框 / 语义色 / 悬停 / 禁用都来自同一套语言），
  这里只把高度放开：两个按钮的文案是一句完整的话，写死 34px 会把第二行裁掉。
*/
.btn {
  height: auto;
  min-height: 34px;
  padding: var(--sp-2) var(--sp-4);
  line-height: var(--lh-tight);
  text-align: left;
}
.btn.quiet { border-color: transparent; background: none; color: var(--text-secondary); }
.btn.quiet:hover:enabled { background: var(--layer-hover); color: var(--text-strong); }
.btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
</style>
