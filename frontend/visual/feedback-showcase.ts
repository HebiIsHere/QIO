// QIO 统一改造 · 反馈场景展示页（只服务视觉验收；产品构建不包含）
// 用法：http://127.0.0.1:5199/visual/feedback-showcase.html?scene=batch-restore&theme=dark
import { createApp, defineComponent, h, ref } from "vue";
import { createPinia } from "pinia";
import "../src/styles/tokens.css";
import "../src/styles/base.css";
import { applyTheme, getThemePreference, resolveTheme } from "../src/utils/theme";
import { applyMotion, getMotionPreference, resolveMotion } from "../src/utils/motion";
import MaintenanceRestoreDialog from "../src/components/planet/MaintenanceRestoreDialog.vue";
import BootStatus from "../src/components/BootStatus.vue";
import { ensureQioTipLayer } from "../src/components/ui/mountQioTipLayer";

const params = new URLSearchParams(location.search);
const scene = params.get("scene") || "batch-restore";
const theme = params.get("theme");
if (theme === "dark" || theme === "light") {
  localStorage.setItem("qio-theme", theme);
}
applyTheme(resolveTheme(getThemePreference()));
applyMotion(resolveMotion(getMotionPreference()));
document.documentElement.dataset.showcase = scene;

const ITEMS = [
  {
    key: "k1",
    label: "可恢复",
    tone: "ok",
    title: "修正「默认话题」中的一条偏好",
    reason: "当前内容与批次修改前的版本一致，可以安全恢复。",
    before: "回答尽量简洁。",
    after: "回答尽量简洁，但关键结论要给出依据。",
  },
  {
    key: "k2",
    label: "可恢复（你后来修改过同一字段）",
    tone: "warn",
    title: "合并两条关于发布流程的知识",
    reason: "恢复会保留你后来的修改，不覆盖较新的内容。",
    before: "发布前先跑测试。",
    after: "发布前先跑测试，并把结果写进验收记录。",
  },
  {
    key: "k3",
    label: "不可安全恢复",
    tone: "quiet",
    title: "修正一条已被再次更新的知识",
    reason: "这条知识在本次维护之后又被修改过；为避免覆盖你后来的内容，这一项不会恢复。",
    before: "周五发布。",
    after: "周五下午发布。",
  },
  {
    key: "k4",
    label: "已恢复过",
    tone: "info",
    title: "整理「关于你」的长期偏好",
    reason: "这一项已经恢复过，本次不会重复执行。",
    before: "",
    after: "",
  },
];

function batchRestore() {
  const open = ref(true);
  return () =>
    h("div", { class: "page" }, [
      h("h1", "整批撤回记忆整理 · 中央确认弹窗"),
      h("p", "展示真实组件：摘要、受影响条目、可恢复性原因、修改前后内容、恢复保护约束与确认/取消。"),
      h("p", { class: "note" }, "组件自身是中央弹层；截图取整屏。"),
      h(MaintenanceRestoreDialog, {
        open: open.value,
        modalId: "showcase-batch-restore",
        targetLabel: "2026-10-26 夜间记忆整理批次",
        summary: "共 4 项：2 项可恢复、1 项不可安全恢复、1 项已恢复过",
        items: ITEMS,
        warnings: [
          "恢复不会删除历史，也不会覆盖你之后对同一内容的修改。",
          "不可安全恢复的项会被跳过，并在维护记录里说明原因。",
        ],
        onCancel: () => (open.value = false),
        onConfirm: () => (open.value = false),
      }),
    ]);
}

function boot(slowOrFailed: "slow" | "failed") {
  return () =>
    h("div", { class: "page", style: "height:100vh;justify-content:center;" }, [
      h("h1", slowOrFailed === "slow" ? "启动状态 · 已等待 12 秒" : "启动状态 · 真实失败"),
      h("p", "展示真实 BootStatus：>10 秒才显示等待秒数；技术细节默认折叠；失败给真实原因和重试。"),
      h(BootStatus, {
        state: slowOrFailed === "failed" ? "failed" : "starting",
        waitedSeconds: slowOrFailed === "slow" ? 12 : 3,
        detail: slowOrFailed === "failed" ? "后端没有应答（最后一条错误：Failed to fetch）" : "",
        onRetry: () => undefined,
      }),
    ]);
}

const app = createApp(
  defineComponent({
    setup() {
      if (scene === "boot-slow") return boot("slow");
      if (scene === "boot-failed") return boot("failed");
      return batchRestore();
    },
  }),
);
app.use(createPinia());
ensureQioTipLayer();
app.mount("#scene");
