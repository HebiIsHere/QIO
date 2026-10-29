/**
 * 全局问题的处理动作执行器（B）。
 *
 * 原则：动作必须指向**真实位置** —— 设置页真实分区、真实路由、真实重试入口；
 * 不伪造「已修复」，也不把入口做成装饰按钮。执行前先收起面板（用户已经做了选择）。
 */
import router from "../../router";
import { useEventStore } from "../../stores/events";
import { useGlobalIssuesStore, type GlobalIssueAction } from "../../stores/globalIssues";
import { restoreRuntimeState } from "../../stores/restore";
import { useUiStore } from "../../stores/ui";

export async function runGlobalIssueAction(action: GlobalIssueAction): Promise<void> {
  if (!action) return;
  const issues = useGlobalIssuesStore();
  if (action.kind === "settings") {
    try {
      const ui = useUiStore();
      // 设置页真实分区 id（appearance/chat/model/tools/data/cred/advanced/about）
      if (action.section) ui.settingsSection = action.section;
    } catch {
      // ui store 未初始化（单测）：仍然尝试跳转
    }
    issues.closePanel();
    try {
      await router.push({ name: "settings" });
    } catch {
      // 没有安装 router（组件单测）：动作本身没有副作用，不抛给界面
    }
    return;
  }
  if (action.kind === "route") {
    issues.closePanel();
    try {
      await router.push({ name: action.route || "debug" });
    } catch {
      // 同上
    }
    return;
  }
  if (action.kind === "retry-connection") {
    issues.closePanel();
    try {
      useEventStore().connect();
    } catch {
      // 事件流重建失败会由 events.error 继续显示；这里不假装已连上
    }
    return;
  }
  if (action.kind === "retry-restore") {
    issues.closePanel();
    try {
      await restoreRuntimeState("retry");
    } catch {
      // 恢复失败会落回 session.resyncState=failed，由来源监听重新上报
    }
  }
}
