<script setup lang="ts">
/**
 * 工具与权限：权限模式（复用既有组件）、工作区根目录、工具记录（开关 + 两个保留天数）、
 * 当前生效参数（只读，只显示已保存且真实生效值）。
 *
 * 工具记录的两个保留期不是同一件事，界面必须说清：
 * - 输出保留天数：到期只清空输出全文，参数、状态、失败原因仍在；
 * - 记录保留天数：到期删除整条工具记录（0 = 永久保留，默认）。
 */
import { computed, onMounted, ref } from "vue";
import QInput from "../../../components/ui/QInput.vue";
import QNumber from "../../../components/ui/QNumber.vue";
import PermissionModeSettings from "../../../components/security/PermissionModeSettings.vue";
import { api } from "../../../services/api";
import { useSecurityStore } from "../../../stores/security";
import { useQioSettingsSave } from "../../../composables/useQioSettingsSave";
import { effectiveFacts, patchEffectiveFacts } from "../effectiveFacts";
import { modelFacts } from "../modelFacts";
import SettingRow from "../shell/SettingRow.vue";
import SettingsNoticeLine from "../shell/SettingsNoticeLine.vue";

const save = useQioSettingsSave("tools");
const { notice } = save;
const security = useSecurityStore();

/* ---------------- 工作区根目录（失焦即时保存） ---------------- */
const workspaceRoot = ref("");
const workspaceSaved = ref("");

async function loadComputerSettings(): Promise<void> {
  try {
    const s = await api.getComputerSettings();
    workspaceSaved.value = s.root_dir || "";
    workspaceRoot.value = s.root_dir || "";
  } catch (e) {
    save.setNotice("err", `加载电脑操控设置失败：${(e as Error).message}`);
  }
}

function saveWorkspace(): void {
  const next = workspaceRoot.value.trim();
  if (next === workspaceSaved.value) return;
  const previous = workspaceSaved.value;
  void save.saveNow<{ root_dir: string }>({
    pending: "正在保存工作区根目录…",
    rollback: () => {
      workspaceRoot.value = previous;
    },
    commit: () => api.updateComputerSettings({ root_dir: next }),
    apply: (server) => {
      workspaceSaved.value = server.root_dir || "";
      workspaceRoot.value = server.root_dir || "";
    },
    success: "工作区根目录已保存",
    failure: "保存工作区根目录",
  });
}

/* ---------------- 工具记录（即时保存） ---------------- */
const recordOutputs = ref(true);
const outputRetentionDays = ref(90);
const recordRetentionDays = ref(0);
const recordsSaved = ref({ recordOutputs: true, outputRetentionDays: 90, recordRetentionDays: 0 });

function applyRecords(server: {
  record_outputs: boolean;
  output_retention_days: number;
  record_retention_days?: number;
}): void {
  const next = {
    recordOutputs: server.record_outputs,
    outputRetentionDays: server.output_retention_days,
    recordRetentionDays: server.record_retention_days ?? recordRetentionDays.value,
  };
  recordOutputs.value = next.recordOutputs;
  outputRetentionDays.value = next.outputRetentionDays;
  recordRetentionDays.value = next.recordRetentionDays;
  recordsSaved.value = next;
}

async function loadToolHistory(): Promise<void> {
  try {
    const s = await api.getToolHistorySettings();
    applyRecords({ ...s, record_retention_days: s.record_retention_days ?? 0 });
  } catch (e) {
    save.setNotice("err", `加载工具调用历史设置失败：${(e as Error).message}`);
  }
}

function toggleRecordOutputs(): void {
  const next = !recordOutputs.value;
  const previous = recordOutputs.value;
  recordOutputs.value = next;
  void save.saveNow<{ record_outputs: boolean; output_retention_days: number; record_retention_days?: number; purged?: number }>({
    pending: "正在保存工具记录设置…",
    rollback: () => {
      recordOutputs.value = previous;
    },
    commit: () => api.updateToolHistorySettings({ record_outputs: next }),
    apply: applyRecords,
    success: (server) =>
      server.purged
        ? `已保存；按保留期清理了 ${server.purged} 条旧输出（参数与失败原因仍在）`
        : "工具记录设置已保存",
    failure: "保存工具记录设置",
  });
}

function onOutputRetentionChange(value: number | null): void {
  const next = value ?? 0;
  const previous = outputRetentionDays.value;
  outputRetentionDays.value = next;
  void save.saveNow<{ record_outputs: boolean; output_retention_days: number; record_retention_days?: number; purged?: number }>({
    pending: "正在保存输出保留期…",
    rollback: () => {
      outputRetentionDays.value = previous;
    },
    commit: () => api.updateToolHistorySettings({ output_retention_days: next }),
    apply: applyRecords,
    success: (server) =>
      server.purged
        ? `输出保留期已保存；清理了 ${server.purged} 条旧输出`
        : `输出保留期已保存：${server.output_retention_days === 0 ? "永久保留" : `${server.output_retention_days} 天`}`,
    failure: "保存输出保留期",
  });
}

function onRecordRetentionChange(value: number | null): void {
  const next = value ?? 0;
  const previous = recordRetentionDays.value;
  recordRetentionDays.value = next;
  void save.saveNow<{ record_outputs: boolean; output_retention_days: number; record_retention_days?: number }>({
    pending: "正在保存记录保留期…",
    rollback: () => {
      recordRetentionDays.value = previous;
    },
    commit: () => api.updateToolHistorySettings({ record_retention_days: next }),
    apply: applyRecords,
    success: (server) =>
      `记录保留期已保存：${(server.record_retention_days ?? 0) === 0 ? "永久保留" : `${server.record_retention_days} 天`}`,
    failure: "保存记录保留期",
  });
}

/* ---------------- 当前生效参数（只读） ---------------- */
const permissionLabel = computed(() =>
  security.loaded ? security.currentLabel : security.loading ? "读取中…" : "尚未读取",
);

function fact(value: string | number | null | undefined, fallback = "读取中…"): string {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

/** 连接展示名：优先 Key 名称，其次服务商名，最后才回退到 id（不把 id 伪装成名称） */
function modelConnectionLabel(info: {
  connectionName?: string | null;
  providerName?: string | null;
  credentialId?: string | null;
}): string {
  return info.connectionName || info.providerName || info.credentialId || "";
}

function usageFactLine(usage: (typeof modelFacts.usages)[number]): string {
  // 跟随默认的用途以 resolved（后端解析后的真实目标）为准；显式分配的用自身字段
  const target =
    usage.followsDefault && usage.resolved
      ? { credentialId: usage.resolved.credentialId, modelId: usage.resolved.modelId }
      : { credentialId: usage.credentialId ?? null, modelId: usage.modelId ?? null };
  // 没有真实目标时把后端的原话写出来：视频 / 语音 / 嵌入是「沿用现有处理方式」、
  // 研究是「未接通」。写成「尚未解析」会让人以为只是还没加载完。
  if (!target.modelId) return usage.note || "尚未解析";
  const connection = modelConnectionLabel({
    connectionName: usage.connectionName,
    providerName: usage.providerName,
    credentialId: target.credentialId,
  });
  return connection ? `${target.modelId} · ${connection}` : target.modelId;
}

onMounted(() => {
  void loadComputerSettings();
  void loadToolHistory();
});
</script>

<template>
  <section class="set-section" data-settings-section="tools" aria-label="工具与权限设置">
    <!--
      这里**不再**写自己的「权限」标题与说明：PermissionModeSettings 自带标题、
      说明与三模式选项，写在外面会让同一句说明在页面上出现两次（实测截图
      settings-dark-1440x900-cat-tools.png）。规则本身仍由该组件维护，未改。
    -->
    <!-- 复用既有组件：三模式、旧值映射与「只有用户能改」的规则保持不变 -->
    <div data-settings-key="tools.permission">
      <PermissionModeSettings />
    </div>

    <header class="set-section__head">
      <h2>电脑操控</h2>
      <p class="set-section__desc">允许 QIO 读写文件、执行命令、查看进程。分级授权，高危操作会触发审批。</p>
    </header>

    <SettingRow
      title="工作区根目录"
      desc="QIO 默认可访问的目录；之外默认要审批。留空则用默认工作区（失焦即保存）"
      settings-key="tools.workspace"
    >
      <QInput
        :model-value="workspaceRoot"
        class="set-input mono"
        placeholder="留空则用默认工作区"
        mono
        aria-label="工作区根目录"
        @update:model-value="(value: string) => (workspaceRoot = value)"
        @change="saveWorkspace"
      />
    </SettingRow>

    <header class="set-section__head">
      <h2>工具记录</h2>
      <p class="set-section__desc">
        保存每次工具调用的参数与输出，供刷新或重开应用后回看（模型不会读到这些内容）。修改即时保存。
      </p>
    </header>

    <SettingRow
      title="保存输出全文"
      desc="关闭后仍保留参数、状态、失败原因与耗时，只是没有完整输出"
      settings-key="tools.records.record-outputs"
    >
      <button
        type="button"
        class="qio-switch"
        :class="{ on: recordOutputs }"
        role="switch"
        :aria-checked="recordOutputs"
        aria-label="保存工具输出全文开关"
        @click="toggleRecordOutputs"
      ></button>
      <span class="set-state mono" aria-hidden="true">{{ recordOutputs ? "开启" : "关闭" }}</span>
    </SettingRow>

    <SettingRow
      title="输出保留天数"
      desc="到期只清空输出全文，参数与失败原因继续保留；填 0 表示永久保留"
      settings-key="tools.records.output-retention"
    >
      <QNumber
        class="set-num"
        :model-value="outputRetentionDays"
        :min="0"
        :max="3650"
        mono
        label="输出保留（天）"
        unit="天"
        @update:model-value="(value: number | null) => (outputRetentionDays = value ?? 0)"
        @change="onOutputRetentionChange(outputRetentionDays)"
      />
    </SettingRow>

    <SettingRow
      title="记录保留天数"
      desc="到期删除整条工具记录（参数与输出一起）；填 0 表示永久保留，这也是默认值"
      settings-key="tools.records.record-retention"
    >
      <QNumber
        class="set-num"
        :model-value="recordRetentionDays"
        :min="0"
        :max="3650"
        mono
        label="记录保留（天）"
        unit="天"
        @update:model-value="(value: number | null) => (recordRetentionDays = value ?? 0)"
        @change="onRecordRetentionChange(recordRetentionDays)"
      />
    </SettingRow>

    <header class="set-section__head">
      <h2>当前生效参数</h2>
      <p class="set-section__desc">
        只读展示，便于排查；这里只显示<strong>已保存且真实生效</strong>的值，不显示任何未保存草稿。
      </p>
    </header>

    <dl class="set-raw" data-settings-key="tools.effective">
      <dt>权限模式</dt>
      <dd>{{ permissionLabel }}</dd>
      <dt>工作区根目录</dt>
      <dd>{{ workspaceSaved || "（默认工作区）" }}</dd>
      <dt>记忆分段</dt>
      <dd>
        {{ fact(effectiveFacts.memoryTurns) }}
        <template v-if="effectiveFacts.memoryTurns !== null">轮 · 单段 {{ fact(effectiveFacts.memoryTokens) }}</template>
      </dd>
      <dt>迭代上限</dt>
      <dd>{{ fact(effectiveFacts.maxIterations) }}</dd>
      <dt>输出预算</dt>
      <dd>{{ fact(effectiveFacts.outputTokenBudget) }}</dd>
      <dt>搜索条数</dt>
      <dd>{{ fact(effectiveFacts.searchTopK) }}</dd>
      <dt>正文字符预算</dt>
      <dd>{{ fact(effectiveFacts.searchMaxFetchChars) }}</dd>
      <dt>免密钥搜索</dt>
      <dd>
        <template v-if="effectiveFacts.searchKeyless === null">读取中…</template>
        <template v-else>
          {{ effectiveFacts.searchKeyless ? "开启（Exa / Parallel / DuckDuckGo）" : "关闭" }}
        </template>
      </dd>
      <dt>SearXNG</dt>
      <dd>{{ fact(effectiveFacts.searxngUrl, "（未配置）") }}</dd>
      <dt>后台整理</dt>
      <dd>
        <template v-if="effectiveFacts.maintenanceEnabled === null">读取中…</template>
        <template v-else>
          {{
            effectiveFacts.maintenanceEnabled
              ? `开启 · 每 ${fact(effectiveFacts.maintenanceInterval)} 小时`
              : "关闭"
          }}
        </template>
      </dd>
      <dt>工具记录</dt>
      <dd>
        {{ recordsSaved.recordOutputs ? "保存输出全文" : "不保存输出全文" }} ·
        输出保留 {{ recordsSaved.outputRetentionDays === 0 ? "永久" : recordsSaved.outputRetentionDays + " 天" }} ·
        记录保留 {{ recordsSaved.recordRetentionDays === 0 ? "永久" : recordsSaved.recordRetentionDays + " 天" }}
      </dd>
      <dt data-settings-key="tools.effective.model-default">默认模型</dt>
      <dd data-settings-key="tools.effective.model-default">
        <template v-if="modelFacts.defaultModel">
          {{ modelFacts.defaultModel.modelId }}
          <template v-if="modelConnectionLabel(modelFacts.defaultModel)">
            · {{ modelConnectionLabel(modelFacts.defaultModel) }}
          </template>
        </template>
        <template v-else-if="modelFacts.loaded">尚未设置</template>
        <template v-else>由「模型与服务」提供，尚未读取</template>
      </dd>
    </dl>

    <dl v-if="modelFacts.usages.length" class="set-raw" data-settings-key="tools.effective.usages">
      <template v-for="usage in modelFacts.usages" :key="usage.usageKey">
        <dt>{{ usage.label }}</dt>
        <dd>
          {{ usageFactLine(usage) }}
          <span v-if="usage.followsDefault && usage.modelId" class="set-hint-inline mono">（跟随默认）</span>
          <span
            v-if="usage.status === 'unsupported'"
            class="set-hint-inline mono"
            data-usage-status="unsupported"
            >（未接通）</span
          >
          <span
            v-else-if="usage.status === 'existing'"
            class="set-hint-inline mono"
            data-usage-status="existing"
            >（沿用现有处理方式）</span
          >
        </dd>
      </template>
    </dl>

    <SettingsNoticeLine :notice="notice" label="tools" />
  </section>
</template>
