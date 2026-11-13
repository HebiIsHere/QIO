<script setup lang="ts">
/**
 * 联网搜索配置（「模型与服务」容器的第二块）。
 *
 * 三条硬语义（契约 §9，界面不能只靠文案暗示）：
 * 1. **免密钥通道不是总开关**：关掉它不清除博查 Key、不清空 SearXNG 地址，
 *    也不改后台回退顺序；它只是「允许用免费通道」这一条。
 * 2. **博查 Key 留空 ≠ 清除**：已配置时留空保存不会发送该字段；清除必须显式操作（QConfirm）。
 * 3. **「已配置」不等于「连接检查通过」**：状态分「未配置 / 已配置（未检查）/ 检查通过 /
 *    检查失败（原因）」四种；只有真的做过连接检查且通过，才显示「检查通过」。
 *    这条语义由**通道行自己的状态**表达（data-channel-check-state），不靠常驻的判据说明；
 *    只有旧后端没有状态接口时才解释一次。
 *
 * 保存方式：数字参数与 SearXNG 地址属于「搜索配置组」，一起保存（有未保存标记）；
 * 免密钥开关与博查 Key 的替换/清除是显式动作，各自独立。
 */
import { computed, onMounted, reactive, ref } from "vue";
import QConfirm from "../../../components/ui/QConfirm.vue";
import QIcon from "../../../components/ui/QIcon.vue";
import QInput from "../../../components/ui/QInput.vue";
import QNumber from "../../../components/ui/QNumber.vue";
import { api, type SearchChannelStatus, type SearchSettings } from "../../../services/api";
import {
  useGroupDraft,
  useQioSettingsSave,
  settingsErrorText,
} from "../../../composables/useQioSettingsSave";
import { patchEffectiveFacts } from "../effectiveFacts";
import SettingRow from "../shell/SettingRow.vue";
import AdvGroup from "../shell/AdvGroup.vue";
import GroupSaveBar from "../shell/GroupSaveBar.vue";

const save = useQioSettingsSave("models");
const { notice } = save;

/* ---------------- 基础状态（来自 /api/settings/search） ---------------- */
const keyless = ref(true);
/** 免密钥开关的真实保存值：失败回滚 / 最终校准读它，不用点击瞬间捕获的界面值。 */
const keylessSaved = ref(true);
const bochaConfigured = ref(false);
/** 用户对免密钥开关 / 博查 Key 做过几次显式动作：迟到的初始读取不得覆盖（F14） */
let keylessTouched = 0;
let bochaTouched = 0;
/* 博查 Key 的输入只活在组件内存里：不进草稿、不进索引、不落 localStorage。 */
const bochaEditing = ref(false);
const bochaKeyDraft = ref("");
const pendingClear = ref(false);

/* ---------------- 通道检查状态（可选端点） ---------------- */
type ChannelId = "keyless" | "bocha" | "searxng";
interface ChannelCheck {
  checked: boolean;
  ok: boolean | null;
  reason: string;
}
const checks = reactive<Record<ChannelId, ChannelCheck>>({
  keyless: { checked: false, ok: null, reason: "" },
  bocha: { checked: false, ok: null, reason: "" },
  searxng: { checked: false, ok: null, reason: "" },
});
/** null = 未知；false = 该版本后端没有状态/检查端点（界面降级） */
const statusAvailable = ref<boolean | null>(null);
const checking = ref(false);
const checkedAt = ref("");

function applyChannels(channels: SearchChannelStatus[] | undefined): void {
  if (!channels?.length) return;
  for (const item of channels) {
    const id = item.channel as ChannelId;
    if (id !== "keyless" && id !== "bocha" && id !== "searxng") continue;
    checks[id] = {
      checked: Boolean(item.checked),
      ok: item.checked ? Boolean(item.ok) : null,
      reason: item.reason || item.detail || "",
    };
  }
}

async function loadStatus(): Promise<void> {
  try {
    const status = await api.getSearchStatus();
    statusAvailable.value = true;
    applyChannels(status.channels);
    checkedAt.value = status.checked_at ?? "";
  } catch {
    // 旧后端 / 尚未集成：降级为只显示「已配置 / 未配置」，不据此判断通道不可用
    statusAvailable.value = false;
  }
}

/**
 * 通道状态分两段，各说一件事（#7 / #23）：
 * - 配置状态：未配置 / 已配置 / 已填写地址；免密钥通道只写「已开启 / 已关闭」；
 * - 检查状态：尚未检查 / 检查通过 / 检查失败（原因）——只有用户**主动点过检查**才存在，
 *   「检查通过」也只能来自真实成功的这一次检查。
 */
const channelViews = computed(() => {
  function configText(configured: boolean, text: string): string {
    return configured ? text : "未配置";
  }
  /** 检查状态是**状态**，不只是文案：模板把它写成 data-channel-check-state，测试按状态断言。 */
  function checkState(check: ChannelCheck): "unchecked" | "ok" | "failed" {
    if (!check.checked) return "unchecked";
    return check.ok ? "ok" : "failed";
  }
  function checkText(check: ChannelCheck): string {
    if (!check.checked) return "，尚未检查";
    return check.ok ? "，检查通过" : `，检查失败${check.reason ? `：${check.reason}` : ""}`;
  }
  function checkTone(check: ChannelCheck): string {
    if (!check.checked) return "warn";
    return check.ok ? "ok" : "err";
  }
  return [
    {
      id: "keyless" as ChannelId,
      name: "免密钥通道",
      configured: keyless.value,
      configText: keyless.value ? "已开启" : "已关闭",
      // 文案 7：配置状态只写「已开启 / 已关闭」；检查状态是**另一段**（「尚未检查 / 检查通过」），
      // 不混进配置状态里，也不伪装成「已验证」。开关本身保留。
      checkText: keyless.value ? checkText(checks.keyless) : "",
      checkState: keyless.value ? checkState(checks.keyless) : "off",
      tone: !keyless.value ? "off" : checks.keyless.checked ? (checks.keyless.ok ? "ok" : "err") : "warn",
      checkTone: checkTone(checks.keyless),
    },
    {
      id: "bocha" as ChannelId,
      name: "博查 API Key",
      configured: bochaConfigured.value,
      configText: configText(bochaConfigured.value, "已配置"),
      checkText: bochaConfigured.value ? checkText(checks.bocha) : "",
      checkState: bochaConfigured.value ? checkState(checks.bocha) : "off",
      tone: !bochaConfigured.value ? "off" : checks.bocha.checked ? (checks.bocha.ok ? "ok" : "err") : "warn",
      checkTone: checkTone(checks.bocha),
    },
    {
      id: "searxng" as ChannelId,
      name: "自建 SearXNG",
      configured: Boolean(search.values.searxngUrl.trim()),
      configText: configText(Boolean(search.values.searxngUrl.trim()), "已填写地址"),
      checkText: search.values.searxngUrl.trim() ? checkText(checks.searxng) : "",
      checkState: search.values.searxngUrl.trim() ? checkState(checks.searxng) : "off",
      tone: !search.values.searxngUrl.trim()
        ? "off"
        : checks.searxng.checked
          ? checks.searxng.ok
            ? "ok"
            : "err"
          : "warn",
      checkTone: checkTone(checks.searxng),
    },
  ];
});

/**
 * 配置变了，旧的检查结论就不再属于这份配置：回到「尚未检查」，
 * 避免把上一份地址/Key 的「检查通过」继续显示成当前配置的结论。
 */
function resetCheck(id: ChannelId): void {
  checks[id] = { checked: false, ok: null, reason: "" };
}

const hasCheckedAnyChannel = computed(() =>
  (["keyless", "bocha", "searxng"] as ChannelId[]).some((id) => checks[id].checked),
);

async function runConnectionCheck(): Promise<void> {
  if (checking.value) return;
  checking.value = true;
  try {
    const result = await api.checkSearch();
    statusAvailable.value = true;
    applyChannels(result.channels);
    if (result.channels?.length) {
      const failed = result.channels.filter((c) => c.checked && c.ok === false);
      save.setNotice(
        failed.length ? "warn" : "ok",
        failed.length
          ? `连接检查完成：${failed.length} 个通道未通过（原因见上方状态）`
          : "连接检查完成：全部通道通过",
      );
      return;
    }
    if (result.ok === false) {
      save.setNotice("warn", "连接检查未通过", undefined, {
        detail: result.message || result.error || "通道不可用",
        retry: () => {
          void runConnectionCheck();
        },
      });
      return;
    }
    save.setNotice("info", result.message || "连接检查已完成（结果见各通道状态）");
  } catch (e) {
    const reason = settingsErrorText(e);
    if (/404|not found|未实现/i.test(reason)) {
      statusAvailable.value = false;
      save.setNotice("info", "当前版本暂不提供连接检查；仍可查看「已配置 / 未配置」状态");
    } else {
      // 这是「这一次检查」的失败：配置原样保留，重试只重放检查（不重写配置）。
      // 主界面只说这一次检查的结果；原始原因在按需展开的技术详情里。
      save.setNotice("err", "这次检查失败", undefined, {
        detail: reason,
        retry: () => {
          void runConnectionCheck();
        },
      });
    }
  } finally {
    checking.value = false;
  }
}

/* ---------------- 搜索配置组（按组保存） ---------------- */
/** 服务端返回值里的附加状态（属于同一响应，但不参与草稿基线比较） */
const saveFlags: { current: { bocha: boolean; keyless: boolean } | null } = { current: null };
const search = useGroupDraft<{ topK: number; maxFetchChars: number; searxngUrl: string }>({
  initial: { topK: 5, maxFetchChars: 15000, searxngUrl: "" },
  validate: (values) => {
    if (!Number.isInteger(values.topK) || values.topK < 1 || values.topK > 20) {
      return "默认返回条数需在 1-20 之间";
    }
    if (
      !Number.isInteger(values.maxFetchChars) ||
      values.maxFetchChars < 1000 ||
      values.maxFetchChars > 40000
    ) {
      return "读正文字符预算需在 1000-40000 之间";
    }
    return null;
  },
  commit: async (values) => {
    const body: Record<string, unknown> = {
      top_k_default: values.topK,
      max_fetch_chars: values.maxFetchChars,
      searxng_url: values.searxngUrl.trim(),
    };
    // 只有用户显式进入替换态并填了新 Key 才发送该字段；留空永远不等于清除
    if (bochaEditing.value && bochaKeyDraft.value.trim()) {
      body.bocha_api_key = bochaKeyDraft.value.trim();
    }
    const result = await api.updateSearchSettings(body);
    saveFlags.current = {
      bocha: result.bocha_has_key,
      keyless: result.keyless_fallback ?? keyless.value,
    };
    return {
      topK: result.top_k_default,
      maxFetchChars: result.max_fetch_chars,
      searxngUrl: result.searxng_url,
    };
  },
  failure: "保存搜索配置",
});

const manualError = ref<string | null>(null);
const groupError = computed(() => manualError.value ?? search.error.value);
const groupDirty = computed(
  () => search.dirty.value || (bochaEditing.value && bochaKeyDraft.value.trim().length > 0),
);

async function onSaveGroup(): Promise<void> {
  manualError.value = null;
  if (bochaEditing.value && !bochaKeyDraft.value.trim()) {
    manualError.value = "请输入新的博查 API Key，或取消替换";
    return;
  }
  const previousUrl = search.baseline.value.searxngUrl.trim();
  const replacingBocha = bochaEditing.value && bochaKeyDraft.value.trim().length > 0;
  saveFlags.current = null;
  const ok = await search.save();
  if (!ok) return;
  const truth = search.baseline.value;
  // 保存成功后，被改动的那份配置之前的检查结论不再有效：回到「尚未检查」。
  if (truth.searxngUrl.trim() !== previousUrl) resetCheck("searxng");
  if (replacingBocha) resetCheck("bocha");
  // 赋值发生在 commit 回调里：控制流分析看不到，所以这里显式取一次快照
  const flags = saveFlags.current as { bocha: boolean; keyless: boolean } | null;
  if (flags) {
    bochaConfigured.value = flags.bocha;
    keyless.value = flags.keyless;
  }
  patchEffectiveFacts({
    searchTopK: truth.topK,
    searchMaxFetchChars: truth.maxFetchChars,
    searxngUrl: truth.searxngUrl,
    searchKeyless: keyless.value,
  });
  bochaEditing.value = false;
  bochaKeyDraft.value = "";
  save.setNotice("ok", "搜索配置已保存");
}

function onCancelGroup(): void {
  search.cancel();
  bochaEditing.value = false;
  bochaKeyDraft.value = "";
  manualError.value = null;
  save.setNotice("info", "已放弃搜索配置里未保存的修改");
}

/* ---------------- 免密钥通道（即时保存，不是总开关） ---------------- */
function toggleKeyless(): void {
  const next = !keyless.value;
  keyless.value = next;
  keylessTouched += 1;
  // 开关变了，上一次针对旧开关状态的检查结论不再有效。
  resetCheck("keyless");
  void save.saveNow<SearchSettings>({
    // F08：免密钥开关与搜索配置组是同一分区的不同设置，互不作废在途保存。
    key: "models.search.keyless",
    pending: "正在保存免密钥通道开关…",
    // 回滚到真实保存值；最终校准也用 apply 同步控件与生效摘要。
    rollback: () => {
      keyless.value = keylessSaved.value;
      patchEffectiveFacts({ searchKeyless: keyless.value });
    },
    commit: () => api.updateSearchSettings({ keyless_fallback: next }),
    trackSaved: (server) => {
      keylessSaved.value = server.keyless_fallback ?? keylessSaved.value;
    },
    apply: (server) => {
      keyless.value = server.keyless_fallback ?? keyless.value;
      bochaConfigured.value = server.bocha_has_key;
      patchEffectiveFacts({ searchKeyless: keyless.value });
    },
    success: (server) =>
      server.keyless_fallback ?? next
        ? "已开启免密钥联网搜索（Exa / Parallel / DuckDuckGo）；其它搜索通道与回退顺序不受影响"
        : "已关闭免密钥联网搜索（只用博查 / 自建 SearXNG / 必应 / 百度）；博查 Key 与 SearXNG 地址都保留",
    failure: "保存免密钥通道开关",
  });
}

/* ---------------- 博查 Key（替换 / 清除都是显式动作） ---------------- */
function startReplace(): void {
  bochaTouched += 1;
  bochaEditing.value = true;
  bochaKeyDraft.value = "";
  manualError.value = null;
}
function cancelReplace(): void {
  bochaEditing.value = false;
  bochaKeyDraft.value = "";
  manualError.value = null;
}
function askClearBocha(): void {
  pendingClear.value = true;
}
async function clearBochaKey(): Promise<void> {
  pendingClear.value = false;
  bochaTouched += 1;
  try {
    const result = await api.updateSearchSettings({ bocha_api_key: "" });
    bochaConfigured.value = result.bocha_has_key;
    bochaEditing.value = false;
    bochaKeyDraft.value = "";
    resetCheck("bocha");
    save.setNotice("warn", "博查 API Key 已清除；免密钥通道与 SearXNG 不受影响");
  } catch (e) {
    save.setNotice("err", "清除博查 API Key 失败", undefined, {
      detail: settingsErrorText(e),
      retry: () => {
        void clearBochaKey();
      },
    });
  }
}

/* ---------------- 加载 ---------------- */
async function load(): Promise<void> {
  const revisionAtStart = search.revision.value;
  const savedAtStart = search.savedRevision.value;
  const keylessAtStart = keylessTouched;
  const bochaAtStart = bochaTouched;
  try {
    const s = await api.getSearchSettings();
    const truth = {
      topK: s.top_k_default,
      maxFetchChars: s.max_fetch_chars,
      searxngUrl: s.searxng_url,
    };
    // F14：迟到的初始读取不能覆盖用户在等待期间的编辑/保存值。
    // 已有更新的保存结果 / 保存还在途 → 整体跳过（不覆盖保存值，也不用旧值当取消基线）。
    if (search.savedRevision.value !== savedAtStart) {
      // 已有更新的保存成功：旧加载值无关紧要，整体跳过。
    } else if (search.saving.value || search.revision.value !== revisionAtStart) {
      // 用户已经在编辑 / 保存还在途：保留输入与在途保存，但取消必须回到服务端实际保存值
      // （返修 4）；保存期间返回时也先记下真实基线，保存失败仍能取消回它（返修 2）。
      // adoptBaseline 不清错误、不作废在途保存（返修 3）。
      search.adoptBaseline(truth);
    } else {
      search.reset(truth);
      patchEffectiveFacts({
        searchTopK: s.top_k_default,
        searchMaxFetchChars: s.max_fetch_chars,
        searxngUrl: s.searxng_url,
      });
    }
    const keylessSuperseded = keylessTouched !== keylessAtStart;
    const bochaSuperseded = bochaTouched !== bochaAtStart;
    if (!bochaSuperseded) bochaConfigured.value = s.bocha_has_key;
    if (!keylessSuperseded) {
      keylessSaved.value = s.keyless_fallback ?? true;
      keyless.value = keylessSaved.value;
      patchEffectiveFacts({ searchKeyless: keyless.value });
    }
  } catch (e) {
    save.setNotice("err", `加载联网搜索设置失败：${settingsErrorText(e)}`);
  }
  await loadStatus();
}

onMounted(() => {
  void load();
});
</script>

<template>
  <section class="set-block" data-settings-key="models.search" aria-label="联网搜索设置">
    <QConfirm
      :open="pendingClear"
      variant="layer"
      tone="danger"
      title="清除已保存的博查 API Key？"
      detail="清除后联网搜索会回退到其它已配置通道（免密钥通道 / 自建 SearXNG）。需要时可以重新填写。"
      confirm-text="清除"
      cancel-text="取消"
      title-id="settings-search-clear-title"
      @confirm="clearBochaKey"
      @cancel="pendingClear = false"
    />

    <header class="set-section__head">
      <h2>联网搜索</h2>
    </header>

    <div class="set-channels" data-settings-key="models.search.status">
      <div
        v-for="channel in channelViews"
        :key="channel.id"
        class="set-channel"
        :data-search-channel="channel.id"
        :data-channel-state="channel.tone"
      >
        <span class="set-channel__dot" aria-hidden="true"></span>
        <span class="set-channel__name">{{ channel.name }}</span>
        <span class="set-channel__state" :class="channel.tone" data-channel-config>{{ channel.configText }}</span>
        <span
          v-if="channel.checkText"
          class="set-channel__state"
          :class="channel.checkTone"
          :data-channel-check-state="channel.checkState"
          data-channel-check
        >{{ channel.checkText }}</span>
      </div>
      <!-- 只有旧后端真的没有状态接口时才解释一次；常态不再常驻判据说明。 -->
      <p v-if="statusAvailable === false" class="set-hint" data-testid="settings-search-status-unavailable">
        当前版本暂不提供通道状态接口，这里显示的是配置状态（已配置 / 未配置）。
      </p>
      <div class="set-channel-actions">
        <button
          type="button"
          class="qio-btn"
          data-testid="settings-search-check"
          :disabled="checking"
          :aria-busy="checking || undefined"
          @click="runConnectionCheck"
        >
          {{ checking ? "检查中…" : hasCheckedAnyChannel ? "重新检查" : "检查连接" }}
        </button>
        <span v-if="checkedAt" class="set-state mono" aria-hidden="true">上次检查：{{ checkedAt }}</span>
      </div>
    </div>

    <SettingRow
      title="免密钥联网搜索"
      desc="关闭后只是不再使用免费通道，博查 Key 与 SearXNG 地址都保留"
      settings-key="models.search.keyless"
    >
      <button
        type="button"
        class="qio-switch"
        :class="{ on: keyless }"
        role="switch"
        :aria-checked="keyless"
        aria-label="免密钥联网搜索"
        @click="toggleKeyless"
      ></button>
      <span class="set-state mono" aria-hidden="true">{{ keyless ? "开启" : "关闭" }}</span>
    </SettingRow>

    <SettingRow
      title="博查 API Key"
      settings-key="models.search.bocha"
    >
      <template v-if="bochaEditing">
        <input
          v-model="bochaKeyDraft"
          class="qio-input mono set-input"
          type="password"
          placeholder="粘贴新的博查 API Key"
          autocomplete="off"
          aria-label="新的博查 API Key"
          data-testid="settings-search-bocha-input"
        />
        <span class="set-hint-inline" data-testid="settings-search-bocha-hint">
          留空保存不会改动已保存的 Key；要移除请点「清除」。
        </span>
        <button type="button" class="qio-btn" @click="cancelReplace">取消替换</button>
      </template>
      <template v-else>
        <span class="set-state mono">{{ bochaConfigured ? "已配置" : "未配置" }}</span>
        <button type="button" class="qio-btn" @click="startReplace">替换</button>
        <button
          v-if="bochaConfigured"
          type="button"
          class="qio-btn danger"
          data-testid="settings-search-bocha-clear"
          @click="askClearBocha"
        >
          清除
        </button>
      </template>
    </SettingRow>

    <AdvGroup id="models.search.advanced" label="高级选项" hint="SearXNG 地址、返回条数、正文预算">
      <SettingRow
        title="自建 SearXNG 地址"
        desc="可选；需要有可达的自建或自备实例。清除请直接清空地址并保存"
        settings-key="models.search.advanced.searxng"
      >
        <QInput
          :model-value="search.values.searxngUrl"
          class="set-input mono"
          type="url"
          mono
          placeholder="https://your-searxng.example"
          aria-label="自建 SearXNG 地址"
          @update:model-value="(value: string) => (search.values.searxngUrl = value)"
        />
      </SettingRow>

      <SettingRow
        title="默认返回条数"
        desc="每次搜索返回的结果数（1-20）"
        settings-key="models.search.advanced.top-k"
      >
        <QNumber
          class="set-num"
          :model-value="search.values.topK"
          :min="1"
          :max="20"
          required
          mono
          label="返回条数"
          @update:model-value="(value: number | null) => (search.values.topK = value ?? 0)"
        />
      </SettingRow>

      <SettingRow
        title="读正文字符预算"
        desc="抓取网页正文的字符上限（1000-40000）"
        settings-key="models.search.advanced.max-fetch-chars"
      >
        <QNumber
          class="set-num"
          :model-value="search.values.maxFetchChars"
          :min="1000"
          :max="40000"
          required
          mono
          label="字符预算"
          @update:model-value="(value: number | null) => (search.values.maxFetchChars = value ?? 0)"
        />
      </SettingRow>
    </AdvGroup>

    <GroupSaveBar
      group="models.search"
      :dirty="groupDirty"
      :saving="search.saving.value"
      :error="groupError"
      saved-hint="搜索配置已保存"
      @save="onSaveGroup"
      @cancel="onCancelGroup"
    />

    <div v-if="notice" :data-settings-notice="'models'">
      <p
        class="set-notice"
        :class="notice.kind"
        :role="notice.kind === 'err' ? 'alert' : 'status'"
      >
        {{ notice.text }}
      </p>
      <div class="set-channel-actions">
        <button
          v-if="notice.retry"
          type="button"
          class="qio-btn mini"
          data-settings-action="retry"
          @click="notice.retry()"
        >
          重试
        </button>
        <button
          type="button"
          class="qio-btn mini quiet"
          aria-label="关闭这项提示"
          @click="save.dismissNotice()"
        >
          关闭
        </button>
      </div>
      <details v-if="notice.detail">
        <summary>技术详情</summary>
        <p class="set-notice info mono">{{ notice.detail }}</p>
      </details>
    </div>
  </section>
</template>