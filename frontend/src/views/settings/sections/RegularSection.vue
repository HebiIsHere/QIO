<script setup lang="ts">
/**
 * 常规：外观（主题 / 动画 / 滚动指示条）、输出速度、设置助手入口、
 * 窗口行为（两个贴靠隐藏开关 + 还原默认布局 + 关闭主窗口行为），
 * 以及「高级选项」里的诊断开关（基线「开发者模式」）。
 *
 * 保存方式：全部即时保存 —— 这些设置要么只落在本地（主题 / 动画 / 指示条 / 贴靠隐藏 /
 * 诊断），要么一次只有一个值（输出速度 / 关闭行为）。失败时控件回滚到真实值。
 */
import { computed, ref } from "vue";
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
import {
  floatingState,
  resetFloatPositions,
  setHideEnabled,
  type DockId,
} from "../../../composables/floatingState";
import { useQioSettingsSave } from "../../../composables/useQioSettingsSave";
import SettingRow from "../shell/SettingRow.vue";
import SegmentedControl from "../shell/SegmentedControl.vue";
import AdvGroup from "../shell/AdvGroup.vue";
import SettingsNoticeLine from "../shell/SettingsNoticeLine.vue";

const ui = useUiStore();
const onboarding = useOnboardingStore();
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
/**
 * 文案必须说清边界：「始终显示」只让**允许显示且实际溢出**的区域常驻显示，
 * 不会让板面（hidden 策略）出现滚动条，也不开放任何横向滚动。
 */
const SCROLLBAR_OPTIONS: { value: ScrollbarPreference; label: string; hint: string }[] = [
  {
    value: "auto",
    label: "自动显示",
    hint: "默认：滚动、指针靠近边缘或用键盘操作该区域时才出现，停止后淡出",
  },
  {
    value: "always",
    label: "始终显示",
    hint: "只让允许显示且实际溢出的区域常驻显示；不会让板面出现滚动条，也不开放对话横向滚动",
  },
];
const scrollbarHint = computed(
  () => SCROLLBAR_OPTIONS.find((o) => o.value === scrollbarPref.value)?.hint ?? "",
);
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
function onSpeedChange(value: string): void {
  const cps = Number(value);
  if (!TYPEWRITER_SPEEDS.includes(cps as (typeof TYPEWRITER_SPEEDS)[number])) return;
  const previous = ui.typewriterCps;
  void save.saveNow<{ typewriter_cps: number }>({
    pending: `正在保存输出速度：${cps} 字/秒…`,
    optimistic: () => {
      ui.typewriterCps = cps;
    },
    rollback: () => {
      ui.typewriterCps = previous;
    },
    commit: async () => {
      await ui.setCps(cps);
      return { typewriter_cps: ui.typewriterCps };
    },
    apply: (server) => {
      ui.typewriterCps = server.typewriter_cps;
    },
    success: `输出速度已保存：${cps} 字/秒`,
    failure: "保存输出速度",
  });
}

/* ---------------- 设置助手 ---------------- */
function rerunOnboarding(): void {
  onboarding.reopen();
  save.setNotice("ok", "设置助手已重新打开");
}

/* ---------------- 窗口行为 ---------------- */
/**
 * 只有两个可拖动的浮动组件（星球入口 / 设置入口）会贴靠并自动隐藏；
 * floatingState 里的 composer 是历史占位（输入区由布局承载，不可拖动、也不会被隐藏），
 * 所以这里不提供一个会「把输入框藏起来」的开关。
 */
const WINDOW_ITEMS: { id: DockId; title: string; desc: string; key: string }[] = [
  {
    id: "planet-dock",
    title: "星球入口贴靠后自动隐藏",
    desc: "关闭＝入口常显（不是禁用）。开启后贴靠完成时淡化，悬停展开、移出再隐藏",
    key: "regular.window.planet-hide",
  },
  {
    id: "settings-float",
    title: "设置入口贴靠后自动隐藏",
    desc: "关闭＝入口常显（不是禁用）。开启后贴靠完成时淡化，悬停展开、移出再隐藏",
    key: "regular.window.settings-hide",
  },
];
function toggleWindowHide(id: DockId): void {
  const next = !floatingState[id].hideEnabled;
  setHideEnabled(id, next);
  const item = WINDOW_ITEMS.find((entry) => entry.id === id);
  save.setNotice("ok", `${item?.title ?? "入口"}：${next ? "开启" : "关闭"}`);
}
function resetWindowLayout(): void {
  resetFloatPositions();
  save.setNotice("ok", "已还原默认布局（贴靠隐藏关闭、位置回到默认）");
}

/* ---------------- 关闭主窗口行为（R9 契约 §11） ---------------- */
const CLOSE_OPTIONS = CLOSE_BEHAVIOR_OPTIONS;
function closeBehaviorLabel(value: CloseBehavior): string {
  return CLOSE_OPTIONS.find((o) => o.value === value)?.label ?? value;
}
function onCloseBehaviorChange(value: string): void {
  const next = value as CloseBehavior;
  if (ui.closeBehavior === next) return;
  const previous = ui.closeBehavior;
  void save.saveNow<void>({
    pending: `正在保存关闭行为：${closeBehaviorLabel(next)}…`,
    rollback: () => {
      // 失败时回到真实值：停留在新值会让下一次关窗行为与界面不一致
      ui.closeBehavior = previous;
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
      <p class="set-section__desc">跟随系统或自行固定；切换立即生效，不需要刷新。</p>
    </header>

    <SettingRow title="主题" desc="跟随系统，或固定暗紫晶 / 净白" settings-key="regular.theme">
      <SegmentedControl
        :options="THEME_OPTIONS"
        :model-value="themePref"
        label="主题偏好"
        @update:model-value="onThemeChange"
      />
    </SettingRow>

    <SettingRow
      title="动画"
      desc="减少动画会同时作用于页面过渡与星球镜头，切换立即生效"
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
      desc="不使用系统原生滚动条；「始终显示」只作用于允许显示且确实溢出的区域，不会让板面出现滚动条，也不开放对话横向滚动"
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
    <p class="set-hint mono" data-settings-key="regular.scrollbar.hint">{{ scrollbarHint }}</p>

    <header class="set-section__head">
      <h2>对话</h2>
      <p class="set-section__desc">助手回答的呈现方式。</p>
    </header>

    <SettingRow
      title="输出速度"
      desc="打字机逐字输出的速度：慢 25 字/秒 · 中 50 字/秒 · 快 75 字/秒"
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
      <p class="set-section__desc">
        重新走一遍设置助手：连接模型、认识你、偏好与目标。已设置项会预填，
        重复提交不会产生重复的记忆、实体卡或话题。
      </p>
    </header>

    <SettingRow
      :title="onboarding.needsSetup ? '继续设置' : '设置助手'"
      :desc="
        onboarding.needsSetup
          ? '初始设置还没完成：从这里继续（顶部的一次性提醒只出现一次，这里是随时可用的入口）'
          : '随时可以重跑，完成后回到对话页'
      "
      settings-key="regular.onboarding"
    >
      <button type="button" class="qio-btn" @click="rerunOnboarding">
        {{ onboarding.needsSetup ? "继续设置" : "重新运行设置助手" }}
      </button>
    </SettingRow>

    <header class="set-section__head">
      <h2>窗口行为</h2>
      <p class="set-section__desc">
        输入框固定在对话底部；星球入口与设置入口为可拖动浮动组件，松手自动贴靠。
        下面两个开关只控制「贴边后要不要自动隐藏」；关闭不等于禁用入口。
      </p>
    </header>

    <SettingRow
      v-for="item in WINDOW_ITEMS"
      :key="item.id"
      :title="item.title"
      :desc="item.desc"
      :settings-key="item.key"
    >
      <button
        type="button"
        class="qio-switch"
        :class="{ on: floatingState[item.id].hideEnabled }"
        role="switch"
        :aria-checked="floatingState[item.id].hideEnabled"
        :aria-label="item.title"
        @click="toggleWindowHide(item.id)"
      ></button>
      <span class="set-state mono" aria-hidden="true">
        贴边后自动隐藏：{{ floatingState[item.id].hideEnabled ? "开" : "关" }}
      </span>
    </SettingRow>

    <SettingRow
      title="布局"
      desc="将所有浮动组件还原到默认位置并关闭贴靠隐藏"
      settings-key="regular.window.reset-layout"
    >
      <button type="button" class="qio-btn" @click="resetWindowLayout">还原默认布局</button>
    </SettingRow>

    <SettingRow
      title="关闭主窗口时"
      desc="标题条关闭按钮、系统关闭（Alt+F4 / 任务栏关闭）都按这里的选择处理；最小化到托盘只是隐藏窗口，直接退出会有界收尾"
      settings-key="regular.window.close-behavior"
    >
      <div class="set-stack">
        <SegmentedControl
          :options="CLOSE_OPTIONS"
          :model-value="ui.closeBehavior"
          label="关闭主窗口行为"
          @update:model-value="onCloseBehaviorChange"
        />
        <p class="set-hint mono">
          当前：{{ closeBehaviorLabel(ui.closeBehavior) }}（{{
            CLOSE_OPTIONS.find((o) => o.value === ui.closeBehavior)?.desc
          }}）
        </p>
        <p class="set-hint">
          「最小化到托盘」只是隐藏窗口：QIO 与后端继续运行，可从系统托盘恢复；
          「直接退出」会先停止受理新工作、保存状态并有界收尾，等待中的有效审批会保留到下次启动。
        </p>
      </div>
    </SettingRow>

    <AdvGroup id="regular.advanced" label="高级选项" hint="诊断与图形信息">
      <SettingRow
        title="诊断（开发者模式）"
        desc="打开后显示 FPS / WebGL / 视角等图形诊断，以及每条消息的 token 明细。日常使用建议保持关闭"
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

    <SettingsNoticeLine :notice="regularNotice" label="regular" />
  </section>
</template>
