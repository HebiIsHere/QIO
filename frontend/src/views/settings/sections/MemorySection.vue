<script setup lang="ts">
/**
 * 记忆与整理：记忆分段（按组保存）、后台整理（即时）、立即整理（QConfirm），
 * 「高级选项」里的单轮迭代上限与输出 token 预算（即时）。
 *
 * 为什么记忆分段改成按组保存：轮数与单段长度是同一个决策（一段能装多少），
 * 分开即时保存会让「刚改的轮数」在长度保存失败时停在一个半生效的状态。
 * 草稿保存在本组件里（v-show 常驻），切分类 / 搜索定位 / 宽窄切换都不会丢；
 * 离开设置页即随组件卸载丢弃（§5：不自动保存、不弹询问）。
 */
import { onMounted, ref } from "vue";
import QNumber from "../../../components/ui/QNumber.vue";
import QSelect from "../../../components/ui/QSelect.vue";
import QConfirm from "../../../components/ui/QConfirm.vue";
import { api } from "../../../services/api";
import {
  useGroupDraft,
  useQioSettingsSave,
} from "../../../composables/useQioSettingsSave";
import { patchEffectiveFacts } from "../effectiveFacts";
import SettingRow from "../shell/SettingRow.vue";
import AdvGroup from "../shell/AdvGroup.vue";
import GroupSaveBar from "../shell/GroupSaveBar.vue";
import SettingsNoticeLine from "../shell/SettingsNoticeLine.vue";

const save = useQioSettingsSave("memory");
const { notice } = save;

/* ---------------- 记忆分段（按组保存） ---------------- */
const TIER_PRESETS = ["5", "10", "15"];
const tierOptions = [
  { value: "5", label: "短（5 轮）" },
  { value: "10", label: "标准（10 轮）" },
  { value: "15", label: "长（15 轮）" },
  { value: "custom", label: "自定义" },
];

const fragment = useGroupDraft<{ tier: string; customTurns: number; maxTokens: number }>({
  initial: { tier: "10", customTurns: 10, maxTokens: 4096 },
  validate: (values) => {
    if (values.tier === "custom") {
      if (!Number.isInteger(values.customTurns) || values.customTurns < 1 || values.customTurns > 30) {
        return "自定义轮数需在 1-30 之间";
      }
    }
    if (
      !Number.isInteger(values.maxTokens) ||
      values.maxTokens < 2000 ||
      values.maxTokens > 200000
    ) {
      return "单段长度需在 2000-200000 之间";
    }
    return null;
  },
  commit: async (values) => {
    const turns = values.tier === "custom" ? values.customTurns : Number(values.tier);
    const result = await api.updateMemorySettings({
      fragment_max_turns: turns,
      fragment_max_tokens: values.maxTokens,
    });
    return {
      tier: TIER_PRESETS.includes(String(result.fragment_max_turns))
        ? String(result.fragment_max_turns)
        : "custom",
      customTurns: result.fragment_max_turns,
      maxTokens: result.fragment_max_tokens,
    };
  },
  failure: "保存记忆分段",
});

function tierLabel(tier: string, customTurns: number): string {
  if (tier !== "custom") {
    return `${tier} 轮`;
  }
  return `${customTurns} 轮（自定义）`;
}

async function onSaveFragment(): Promise<void> {
  const ok = await fragment.save();
  if (!ok) return;
  patchEffectiveFacts({
    memoryTurns: fragment.values.tier === "custom" ? fragment.values.customTurns : Number(fragment.values.tier),
    memoryTokens: fragment.values.maxTokens,
  });
  save.setNotice(
    "ok",
    `记忆分段已保存：${tierLabel(fragment.values.tier, fragment.values.customTurns)} · 单段 ${fragment.values.maxTokens}`,
  );
}

function onCancelFragment(): void {
  fragment.cancel();
  save.setNotice("info", "已放弃记忆分段里未保存的修改");
}

async function loadMemorySettings(): Promise<void> {
  const revisionAtStart = fragment.revision.value;
  const savedAtStart = fragment.savedRevision.value;
  try {
    const s = await api.getMemorySettings();
    // F14：这是**初始读取**，不是权威覆盖。已有更新的保存结果 / 保存还在途时，
    // 迟到的读取不得覆盖较新的保存值与生效摘要。
    if (fragment.saving.value || fragment.savedRevision.value !== savedAtStart) return;
    const savedTier = TIER_PRESETS.includes(String(s.fragment_max_turns))
      ? String(s.fragment_max_turns)
      : "custom";
    const values = {
      tier: savedTier,
      customTurns: s.fragment_max_turns,
      maxTokens: s.fragment_max_tokens,
    };
    if (fragment.revision.value !== revisionAtStart) {
      // 用户已经在编辑：保留输入，但取消必须回到服务端实际保存值（返修 4），
      // 不能回到加载前的占位默认值；生效摘要也不拿旧加载值覆盖。
      fragment.adoptBaseline(values);
      return;
    }
    fragment.reset(values);
    patchEffectiveFacts({ memoryTurns: s.fragment_max_turns, memoryTokens: s.fragment_max_tokens });
  } catch (e) {
    save.setNotice("err", `加载记忆分段失败：${(e as Error).message}`);
  }
}

/* ---------------- 后台整理（即时保存） ---------------- */
const maintenanceEnabled = ref(true);
const maintenanceInterval = ref(24);
/** 最近一次已保存（或刚加载）的后台整理真值：失败回滚与取消都用它，不用用户刚敲的值（返修 1） */
const maintenanceSaved = ref({ enabled: true, interval: 24 });
/** 用户对后台整理做过几次修改/保存：迟到的初始读取按这个计数判断是否已被更新的选择取代（F14） */
let maintenanceTouched = 0;

async function loadMaintenanceSettings(): Promise<void> {
  const touchedAtStart = maintenanceTouched;
  try {
    const s = await api.getMaintenanceSettings();
    if (maintenanceTouched !== touchedAtStart) return;
    maintenanceSaved.value = { enabled: s.enabled, interval: s.interval_hours };
    maintenanceEnabled.value = s.enabled;
    maintenanceInterval.value = s.interval_hours;
    patchEffectiveFacts({
      maintenanceEnabled: s.enabled,
      maintenanceInterval: s.interval_hours,
    });
  } catch (e) {
    save.setNotice("err", `加载后台整理设置失败：${(e as Error).message}`);
  }
}

function toggleMaintenance(): void {
  const next = !maintenanceEnabled.value;
  const previous = maintenanceSaved.value.enabled;
  maintenanceTouched += 1;
  void save.saveNow<{ enabled: boolean; interval_hours: number }>({
    // F08：同一分区的不同设置各有身份，互不作废在途保存。
    key: "memory.maintenance.enabled",
    pending: "正在保存后台整理开关…",
    optimistic: () => {
      maintenanceEnabled.value = next;
    },
    rollback: () => {
      maintenanceEnabled.value = previous;
    },
    commit: () => api.updateMaintenanceSettings({ enabled: next }),
    // F08：这次保存只拥有「开关」这一个设置；响应里附带的其它字段可能来自更早的请求，
    // 不能拿它覆盖另一个设置在途/已成功的选择。
    apply: (server) => {
      maintenanceSaved.value = {
        enabled: server.enabled,
        interval: maintenanceSaved.value.interval,
      };
      maintenanceEnabled.value = server.enabled;
      patchEffectiveFacts({ maintenanceEnabled: server.enabled });
    },
    success: (server) =>
      `后台整理已保存：${server.enabled ? "开启" : "关闭"}（每 ${server.interval_hours} 小时）`,
    failure: "保存后台整理开关",
  });
}

function onMaintenanceIntervalInput(value: number | null): void {
  maintenanceTouched += 1;
  maintenanceInterval.value = value ?? 0;
}

function onMaintenanceIntervalChange(value: number | null): void {
  const next = value ?? 0;
  // 回滚到最近一次真实保存值，而不是用户刚输入的数字（返修 1）。
  const previous = maintenanceSaved.value.interval;
  maintenanceInterval.value = next;
  maintenanceTouched += 1;
  void save.saveNow<{ enabled: boolean; interval_hours: number }>({
    key: "memory.maintenance.interval",
    pending: "正在保存整理间隔…",
    rollback: () => {
      maintenanceInterval.value = previous;
    },
    commit: () => api.updateMaintenanceSettings({ interval_hours: next }),
    apply: (server) => {
      maintenanceSaved.value = {
        enabled: maintenanceSaved.value.enabled,
        interval: server.interval_hours,
      };
      maintenanceInterval.value = server.interval_hours;
      patchEffectiveFacts({ maintenanceInterval: server.interval_hours });
    },
    success: (server) => `整理间隔已保存：每 ${server.interval_hours} 小时`,
    failure: "保存整理间隔",
  });
}

/** 立即整理是真实后台动作：先确认（说明影响与不可撤销边界），再执行。 */
const pendingRun = ref(false);
function askRunMaintenance(): void {
  pendingRun.value = true;
}
async function runMaintenanceNow(): Promise<void> {
  pendingRun.value = false;
  try {
    const r = await api.runMaintenance();
    save.setNotice(
      r.started ? "ok" : "info",
      r.started ? "后台整理已启动（后台执行）" : "后台整理未启动",
    );
  } catch (e) {
    save.setNotice("err", `启动后台整理失败：${(e as Error).message}`);
  }
}

/* ---------------- 高级选项：对话深度（即时保存） ---------------- */
const loopMaxIterations = ref(128);
const loopTokenBudget = ref(51200);
/** 最近一次已保存（或刚加载）的对话深度真值：失败回滚用它（返修 1） */
const loopSaved = ref({ maxIterations: 128, outputTokenBudget: 51200 });
/** 用户对对话深度做过几次修改/保存：同上，迟到的初始读取不得覆盖（F14） */
let loopTouched = 0;

async function loadLoopSettings(): Promise<void> {
  const touchedAtStart = loopTouched;
  try {
    const s = await api.getLoopSettings();
    if (loopTouched !== touchedAtStart) return;
    loopSaved.value = { maxIterations: s.max_iterations, outputTokenBudget: s.output_token_budget };
    loopMaxIterations.value = s.max_iterations;
    loopTokenBudget.value = s.output_token_budget;
    patchEffectiveFacts({ maxIterations: s.max_iterations, outputTokenBudget: s.output_token_budget });
  } catch (e) {
    save.setNotice("err", `加载对话深度失败：${(e as Error).message}`);
  }
}

function applyLoop(server: { max_iterations: number; output_token_budget: number }): void {
  loopSaved.value = {
    maxIterations: server.max_iterations,
    outputTokenBudget: server.output_token_budget,
  };
  loopMaxIterations.value = server.max_iterations;
  loopTokenBudget.value = server.output_token_budget;
  patchEffectiveFacts({
    maxIterations: server.max_iterations,
    outputTokenBudget: server.output_token_budget,
  });
}

function saveLoop(): void {
  const iterations = loopMaxIterations.value;
  const budget = loopTokenBudget.value;
  if (!Number.isInteger(iterations) || iterations < 1 || iterations > 1000) {
    save.setNotice("err", "迭代上限需在 1-1000 之间");
    return;
  }
  if (!Number.isInteger(budget) || budget < 0) {
    save.setNotice("err", "输出预算需为不小于 0 的整数");
    return;
  }
  loopTouched += 1;
  const previous = { ...loopSaved.value };
  void save.saveNow<{ max_iterations: number; output_token_budget: number }>({
    key: "memory.loop",
    pending: "正在保存对话深度…",
    // 失败回到真实已保存值，而不是用户刚输入的数字（返修 1）。
    rollback: () => {
      loopMaxIterations.value = previous.maxIterations;
      loopTokenBudget.value = previous.outputTokenBudget;
    },
    commit: () => api.updateLoopSettings({ max_iterations: iterations, output_token_budget: budget }),
    apply: applyLoop,
    success: "对话深度已保存",
    failure: "保存对话深度",
  });
}

function onIterationsInput(value: number | null): void {
  // 实际输入就建立保护：不等失焦/保存，迟到初始读取也不能覆盖未提交输入（返修 3）。
  loopTouched += 1;
  loopMaxIterations.value = value ?? 0;
}
function onBudgetInput(value: number | null): void {
  loopTouched += 1;
  loopTokenBudget.value = value ?? 0;
}

onMounted(() => {
  void loadMemorySettings();
  void loadMaintenanceSettings();
  void loadLoopSettings();
});
</script>

<template>
  <section class="set-section" data-settings-section="memory" aria-label="记忆与整理设置">
    <QConfirm
      :open="pendingRun"
      variant="layer"
      tone="normal"
      title="立即整理记忆与知识？"
      detail="会在后台启动一次离线整理：合并记忆分段、提炼知识候选与实体关系。整理过程不阻塞对话，但会占用本机计算；已经开始的任务不能中途撤销。"
      confirm-text="开始整理"
      cancel-text="取消"
      title-id="settings-maintenance-title"
      @confirm="runMaintenanceNow"
      @cancel="pendingRun = false"
    />

    <header class="set-section__head">
      <h2>记忆分段</h2>
    </header>

    <SettingRow
      title="记忆封块分档"
      settings-key="memory.fragment.tier"
    >
      <QSelect
        :options="tierOptions"
        :model-value="fragment.values.tier"
        @update:model-value="(value: string) => (fragment.values.tier = value)"
      />
    </SettingRow>

    <SettingRow
      v-if="fragment.values.tier === 'custom'"
      title="自定义轮数"
      desc="单段最多的对话轮数（用户 + 助手算一轮，1-30）"
      settings-key="memory.fragment.custom-turns"
    >
      <QNumber
        class="set-num"
        :model-value="fragment.values.customTurns"
        :min="1"
        :max="30"
        mono
        label="自定义轮数"
        @update:model-value="(value: number | null) => (fragment.values.customTurns = value ?? 0)"
      />
    </SettingRow>

    <SettingRow
      title="单段长度目标"
      desc="内容长度的兜底上限（2000-200000，按估算字符数计）"
      settings-key="memory.fragment.max-tokens"
    >
      <QNumber
        class="set-num"
        :model-value="fragment.values.maxTokens"
        :min="2000"
        :max="200000"
        :step="1000"
        mono
        label="单段长度目标"
        @update:model-value="(value: number | null) => (fragment.values.maxTokens = value ?? 0)"
      />
    </SettingRow>

    <GroupSaveBar
      group="memory.fragment"
      :dirty="fragment.dirty.value"
      :saving="fragment.saving.value"
      :error="fragment.error.value"
      saved-hint="记忆分段已保存"
      @save="onSaveFragment"
      @cancel="onCancelFragment"
    />

    <header class="set-section__head">
      <h2>后台整理</h2>
    </header>

    <SettingRow
      title="启用后台整理"
      settings-key="memory.maintenance.enabled"
    >
      <button
        type="button"
        class="qio-switch"
        :class="{ on: maintenanceEnabled }"
        role="switch"
        :aria-checked="maintenanceEnabled"
        aria-label="后台整理开关"
        @click="toggleMaintenance"
      ></button>
      <span class="set-state mono" aria-hidden="true">{{ maintenanceEnabled ? "开启" : "关闭" }}</span>
    </SettingRow>

    <SettingRow
      title="整理间隔"
      desc="每次后台整理的间隔（1-720 小时）"
      settings-key="memory.maintenance.interval"
    >
      <QNumber
        class="set-num"
        :model-value="maintenanceInterval"
        :min="1"
        :max="720"
        mono
        label="整理间隔（小时）"
        unit="小时"
        @update:model-value="onMaintenanceIntervalInput"
        @change="onMaintenanceIntervalChange(maintenanceInterval)"
      />
    </SettingRow>

    <SettingRow
      title="立即整理"
      settings-key="memory.maintenance.run"
    >
      <button type="button" class="qio-btn" @click="askRunMaintenance">立即整理</button>
    </SettingRow>

    <AdvGroup id="memory.advanced" label="高级选项" hint="单轮迭代上限与输出 token 预算">
      <SettingRow
        title="单轮迭代上限"
        desc="每轮任务最多重新决策的次数（1-1000）"
        settings-key="memory.advanced.max-iterations"
      >
        <QNumber
          class="set-num"
          :model-value="loopMaxIterations"
          :min="1"
          :max="1000"
          mono
          label="迭代上限"
          @update:model-value="onIterationsInput"
          @change="saveLoop"
        />
      </SettingRow>

      <SettingRow
        title="输出 token 预算"
        desc="单轮模型输出累计上限（0 表示不限）"
        settings-key="memory.advanced.output-budget"
      >
        <QNumber
          class="set-num"
          :model-value="loopTokenBudget"
          :min="0"
          :max="500000"
          mono
          label="输出预算"
          @update:model-value="onBudgetInput"
          @change="saveLoop"
        />
      </SettingRow>
    </AdvGroup>

    <SettingsNoticeLine :notice="notice" label="memory" />
  </section>
</template>