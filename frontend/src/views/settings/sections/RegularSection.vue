<script setup lang="ts">
/**
 * 常规：外观（主题 / 动画 / 滚动指示条）、输出速度、设置助手入口、
 * 窗口行为（还原默认布局 + 关闭主窗口行为），以及「高级选项」里的诊断开关（基线「开发者模式」）。
 *
 * 保存方式：全部即时保存 —— 这些设置要么只落在本地（主题 / 动画 / 指示条 / 诊断），
 * 要么一次只有一个值（输出速度 / 关闭行为 / 还原布局）。失败时控件回滚到真实值。
 *
 * #39（定稿 A）：星球入口固定在对话输入区旁、设置入口固定在全局顶栏，两个入口都不再是
 * 可拖动浮动组件，所以「贴靠后自动隐藏」两项设置已经失效，统一移除；对应的搜索索引也不再有。
 *
 * #47：但「还原默认布局」重新恢复 —— 它现在还原的是**对话页布局偏好**（左右栏宽、左栏收起、
 * 右侧预览开合与手动全窗口），由 stores/conversationShell 的既有持久化通道保存；
 * 预览标签及顺序、当前标签、各标签搜索词与阅读位置、聊天内容、草稿与当前话题都不动。
 * 窗口太窄导致的临时收起 / 临时全窗口仍由布局逻辑推导，不是这里能改的保存值。
 */
import { ref, watch } from "vue";
import QSelect from "../../../components/ui/QSelect.vue";
import { useUiStore, TYPEWRITER_SPEEDS } from "../../../stores/ui";
import { CLOSE_BEHAVIOR_OPTIONS, type CloseBehavior } from "../../../services/quitGuard";
import { useOnboardingStore } from "../../../stores/onboarding";
import {
  getThemePreference,
  setThemePreference,
  type ThemePreference,
} from "../../../utils/theme";
import { getMotionPreference, setMotionPreference, type MotionPreference } from "../../../utils/motion";
import {
  getScrollbarPreference,
  setScrollbarPreference,
  type ScrollbarPreference,
} from "../../../utils/scrollIndicator";
import { useQioSettingsSave } from "../../../composables/useQioSettingsSave";
import { useConversationShellStore } from "../../../stores/conversationShell";
import SettingRow from "../shell/SettingRow.vue";
import SegmentedControl from "../shell/SegmentedControl.vue";
import AdvGroup from "../shell/AdvGroup.vue";
import SettingsNoticeLine from "../shell/SettingsNoticeLine.vue";

const ui = useUiStore();
const onboarding = useOnboardingStore();
const conversationShell = useConversationShellStore();
const save = useQioSettingsSave("regular");
const { notice: regularNotice } = save;

/* ---------------- 主题 ---------------- */
const themePref = ref<ThemePreference>(getThemePreference());
const THEME_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "系统" },
  { value: "dark", label: "暗紫晶" },
  { value: "light", label: "净白" },
];
function onThemeChange(value: string): void {
  const next = value as ThemePreference;
  if (next === themePref.value) return;
  themePref.value = next;
  setThemePreference(next);
  save.setNotice("ok", `主题已切换：${THEME_OPTIONS.find((o) => o.value === next)?.label ?? next}`);
}

/* ---------------- 动画 ---------------- */
const motionPref = ref<MotionPreference>(getMotionPreference());
const MOTION_OPTIONS: { value: MotionPreference; label: string }[] = [
  { value: "system", label: "跟随系统" },
  { value: "standard", label: "标准" },
  { value: "reduced", label: "减少动画" },
];
function onMotionChange(value: string): void {
  const next = value as MotionPreference;
  if (next === motionPref.value) return;
  motionPref.value = next;
  const mode = setMotionPreference(next);
  save.setNotice(
    "ok",
    `动画已切换：${MOTION_OPTIONS.find((o) => o.value === next)?.label ?? next}（当前生效：${mode === "reduced" ? "减少动画" : "标准"}）`,
  );
}

/* ---------------- 滚动指示条 ---------------- */
const scrollbarPref = ref<ScrollbarPreference>(getScrollbarPreference());
const SCROLLBAR_OPTIONS: { value: ScrollbarPreference; label: string }[] = [
  { value: "auto", label: "自动显示" },
  { value: "always", label: "始终显示" },
];
function onScrollbarChange(value: string): void {
  const next = value as ScrollbarPreference;
  if (next === scrollbarPref.value) return;
  scrollbarPref.value = next;
  setScrollbarPreference(next);
  save.setNotice(
    "ok",
    "滚动指示条已切换：" + (SCROLLBAR_OPTIONS.find((o) => o.value === next)?.label ?? next),
  );
}

/* ---------------- 输出速度 ---------------- */
const speedOptions = TYPEWRITER_SPEEDS.map((cps) => ({
  value: String(cps),
  label: cps === 25 ? "慢（25 字/秒）" : cps === 50 ? "中（50 字/秒）" : "快（75 字/秒）",
}));
/**
 * 「实际已保存值」：只能由已确认的初始值（加载成功）或本次接口成功返回更新，
 * 不能从正在变化的界面状态读取；所有请求都失败时回滚到它（返修五）。
 */
const speedSaved = ref(ui.typewriterCpsSaved);
watch(
  () => ui.typewriterCpsSaved,
  (saved) => {
    speedSaved.value = saved;
  },
);
function onSpeedChange(value: string): void {
  const cps = Number(value);
  if (!TYPEWRITER_SPEEDS.includes(cps as (typeof TYPEWRITER_SPEEDS)[number])) return;
  void save.saveNow<number>({
    key: "regular.speed",
    pending: `正在保存输出速度：${cps} 字/秒…`,
    // 用户最新选择先显示；后续排队执行较早请求时不能把它改回较早选择。
    optimistic: () => {
      ui.typewriterCps = cps;
    },
    // 等待期间保持最新选择；最新失败时由共享逻辑恢复最后实际保存成功的值。
    rollback: () => {
      ui.typewriterCps = speedSaved.value;
    },
    // 返回本次接口的真实结果；不在这里写界面（排队执行较早请求时不回写旧选择）。
    commit: () => ui.saveCps(cps),
    trackSaved: (saved) => {
      speedSaved.value = saved;
    },
    apply: (saved) => {
      ui.typewriterCps = saved;
    },
    success: (saved) => `输出速度已保存：${saved} 字/秒`,
    failure: "保存输出速度",
  });
}

/* ---------------- 设置助手 ---------------- */
function rerunOnboarding(): void {
  onboarding.reopen();
  save.setNotice("ok", "设置助手已重新打开");
}

/* ---------------- 窗口行为：还原默认布局（#47） ---------------- */
/**
 * 只还原「用户可调的布局偏好」：左右栏宽、左栏收起、右侧预览开合、手动全窗口。
 * 预览标签及顺序、当前标签、各标签搜索词与阅读位置、任务页签、聊天内容/草稿/当前话题都不动；
 * 窄窗下的临时收起/临时全窗口由布局逻辑按实际宽度推导，不写进偏好。
 *
 * 成功反馈在**保存成功之后**给出（不是点一下就显示「已还原」）；失败由共享保存模型给出
 * 明确错误 + 「重试」按钮，重试重放同一次还原（含保存）。
 */
function resetConversationLayout(): void {
  void save.saveNow<boolean>({
    key: "regular.window.reset-layout",
    pending: "正在还原默认布局…",
    commit: async () => {
      const ok = await conversationShell.resetLayout();
      // 保存失败要落到本行的失败提示（含重试），不能显示成已还原。
      if (!ok) throw new Error(conversationShell.saveError || "布局偏好保存失败");
      return true;
    },
    success: "已还原默认布局（预览标签与阅读位置保留）",
    failure: "还原默认布局",
  });
}

/* ---------------- 关闭主窗口行为（R9 契约 §11） ---------------- */
const CLOSE_OPTIONS = CLOSE_BEHAVIOR_OPTIONS;
function closeBehaviorLabel(value: CloseBehavior): string {
  return CLOSE_OPTIONS.find((o) => o.value === value)?.label ?? value;
}
function onCloseBehaviorChange(value: string): void {
  const next = value as CloseBehavior;
  if (ui.closeBehavior === next) return;
  void save.saveNow<void>({
    key: "regular.close-behavior",
    // 关闭行为的顺序与真实生效值由 ui store 自己维护（写序号 + closeBehaviorEffective），
    // 上层不再串行排队，保持既有语义。
    serial: false,
    pending: `正在保存关闭行为：${closeBehaviorLabel(next)}…`,
    rollback: () => {
      // 失败时回到**真实生效值**（store 里最近一次确认生效的那个），不是页面自己记的上一帧：
      // 连续切换时上一帧可能只是尚未保存成功的乐观值，拿它回滚会和实际关窗行为不一致（F05）。
      ui.closeBehavior = ui.closeBehaviorEffective;
    },
    commit: () => ui.setCloseBehavior(next),
    success: () => `关闭主窗口时：${closeBehaviorLabel(ui.closeBehavior)}（已保存，立即生效）`,
    failure: "保存关闭行为",
  });
}

/* ---------------- 高级选项：诊断（基线「开发者模式」） ---------------- */
function toggleDiagnostics(): void {
  ui.setDeveloperMode(!ui.developerMode);
  save.setNotice(
    "ok",
    ui.developerMode ? "开发者模式已开启：显示图形诊断与 token 明细" : "开发者模式已关闭",
  );
}
</script>

<template>
  <section class="set-section" data-settings-section="regular" aria-label="常规设置">
    <header class="set-section__head">
      <h2>外观</h2>
    </header>

    <SettingRow title="主题" settings-key="regular.theme">
      <SegmentedControl
        :options="THEME_OPTIONS"
        :model-value="themePref"
        label="主题偏好"
        @update:model-value="onThemeChange"
      />
    </SettingRow>

    <SettingRow
      title="动画"
      settings-key="regular.motion"
    >
      <SegmentedControl
        :options="MOTION_OPTIONS"
        :model-value="motionPref"
        label="动画偏好"
        @update:model-value="onMotionChange"
      />
    </SettingRow>

    <SettingRow
      title="滚动指示条"
      settings-key="regular.scrollbar"
    >
      <div class="set-stack">
        <SegmentedControl
          :options="SCROLLBAR_OPTIONS"
          :model-value="scrollbarPref"
          label="滚动指示条偏好"
          @update:model-value="onScrollbarChange"
        />
      </div>
    </SettingRow>
    <header class="set-section__head">
      <h2>对话</h2>
    </header>

    <SettingRow
      title="输出速度"
      settings-key="regular.speed"
    >
      <QSelect
        :options="speedOptions"
        :model-value="String(ui.typewriterCps)"
        @update:model-value="onSpeedChange"
      />
    </SettingRow>

    <header class="set-section__head">
      <h2>引导</h2>
    </header>

    <SettingRow
      :title="onboarding.needsSetup ? '继续设置' : '设置助手'"
      settings-key="regular.onboarding"
    >
      <button type="button" class="qio-btn" @click="rerunOnboarding">
        {{ onboarding.needsSetup ? "继续设置" : "重新运行设置助手" }}
      </button>
    </SettingRow>

    <header class="set-section__head">
      <h2>窗口行为</h2>
    </header>

    <SettingRow
      title="布局"
      settings-key="regular.window.reset-layout"
      desc="还原左右栏宽、左栏收起、预览开合与全窗口；预览标签、搜索词和阅读位置保留"
    >
      <button type="button" class="qio-btn" @click="resetConversationLayout">
        还原默认布局
      </button>
    </SettingRow>

    <SettingRow
      title="关闭主窗口时"
      settings-key="regular.window.close-behavior"
    >
      <div class="set-stack">
        <SegmentedControl
          :options="CLOSE_OPTIONS"
          :model-value="ui.closeBehavior"
          label="关闭主窗口行为"
          @update:model-value="onCloseBehaviorChange"
        />
        <p class="set-hint">
          「最小化到托盘」只是隐藏窗口：QIO 与后端继续运行。
          「直接退出」后，仍有效的待审批事项会保留到下次启动。
        </p>
      </div>
    </SettingRow>

    <AdvGroup id="regular.advanced" label="高级选项" hint="诊断与图形信息">
      <SettingRow
        title="诊断（开发者模式）"
        settings-key="regular.advanced.diagnostics"
      >
        <button
          type="button"
          class="qio-switch"
          :class="{ on: ui.developerMode }"
          role="switch"
          :aria-checked="ui.developerMode"
          aria-label="开发者模式"
          @click="toggleDiagnostics"
        ></button>
        <span class="set-state mono" aria-hidden="true">{{ ui.developerMode ? "开启" : "关闭" }}</span>
      </SettingRow>
    </AdvGroup>

    <SettingsNoticeLine
      :notice="regularNotice"
      label="regular"
      @dismiss="save.dismissNotice()"
    />
  </section>
</template>