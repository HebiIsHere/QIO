/**
 * 全局异常来源接入（B）。
 *
 * 只收「整体 / 持续」的异常，全部从**已有权威状态**派生（不复制业务状态、不轮询）：
 * 1. SSE 连接断开（stores/events connected + error）；
 * 2. 无可用凭据 / 凭据暂停 / 失效（stores/events credentialNotice）；
 * 3. 模型能力不可用（stores/events modelMode / fallbackNotice）；
 * 4. 启动/重连恢复失败（stores/session resyncState + lastError）。
 *
 * 恢复时只移除对应那一项（部分恢复 ≠ 全部恢复）；全部恢复后入口由 store 自动收起。
 * 处理动作一律指向真实位置：设置页真实分区 / 真实重试入口，不造假按钮。
 */
import { watch, type WatchStopHandle } from "vue";
import { useEventStore } from "../../stores/events";
import { useGlobalIssuesStore, type GlobalIssueAction } from "../../stores/globalIssues";
import { useSessionStore } from "../../stores/session";

export const GLOBAL_ISSUE_IDS = {
  sse: "sse_disconnected",
  noCredentials: "no_credentials",
  credentialPaused: "credential_paused",
  credentialRevoked: "credential_revoked",
  credentialState: "credential_state",
  modelCapability: "model_capability_fallback",
  modelUnavailable: "model_unavailable",
  restore: "restore_failed",
} as const;

const SETTINGS_CRED: GlobalIssueAction[] = [
  { label: "打开凭据设置", kind: "settings", section: "cred" },
];
const SETTINGS_MODEL: GlobalIssueAction[] = [
  { label: "打开模型设置", kind: "settings", section: "model" },
];

/** 凭据提示 → 全局问题（同一时刻只保留一条凭据类问题；按真实文案分型）。 */
export function classifyCredentialIssue(text: string): {
  id: string;
  title: string;
  severity: "error" | "warning";
  kind: "no_credentials" | "credential_paused" | "credential_revoked" | "credential_state";
} | null {
  const raw = String(text ?? "").trim();
  if (!raw) return null;
  if (raw.includes("没有可用")) {
    return { id: GLOBAL_ISSUE_IDS.noCredentials, title: "没有可用的模型凭据", severity: "error", kind: "no_credentials" };
  }
  if (raw.includes("暂停")) {
    return { id: GLOBAL_ISSUE_IDS.credentialPaused, title: "有凭据已暂停，依赖它的能力暂不可用", severity: "warning", kind: "credential_paused" };
  }
  if (raw.includes("失效") || raw.includes("撤销") || raw.includes("删除")) {
    return { id: GLOBAL_ISSUE_IDS.credentialRevoked, title: "有凭据已失效，依赖它的能力暂不可用", severity: "error", kind: "credential_revoked" };
  }
  return { id: GLOBAL_ISSUE_IDS.credentialState, title: "凭据状态需要处理", severity: "warning", kind: "credential_state" };
}

export interface GlobalIssueSourcesHandle {
  /** 卸载时必须调用：停止全部监听。 */
  stop: () => void;
}

export function useGlobalIssueSources(): GlobalIssueSourcesHandle {
  const events = useEventStore();
  const session = useSessionStore();
  const issues = useGlobalIssuesStore();
  const stops: WatchStopHandle[] = [];

  // 1) SSE 断线：连上即恢复；断开时保留后端给定的原因原文。
  stops.push(
    watch(
      () => [events.connected, events.error] as const,
      ([connected, error]) => {
        if (connected) {
          issues.resolve(GLOBAL_ISSUE_IDS.sse);
          return;
        }
        const reason = String(error ?? "").trim();
        if (!reason) return;
        issues.upsert({
          id: GLOBAL_ISSUE_IDS.sse,
          kind: "sse_disconnected",
          title: "与后端的实时连接已断开",
          reason,
          actions: [{ label: "重新连接", kind: "retry-connection" }],
          diagnostics: [
            "界面显示的是上次读取到的状态；连接恢复后会自动重新对齐。",
            "原因：" + reason,
          ],
          severity: "error",
          at: Date.now(),
        });
      },
      { immediate: true },
    ),
  );

  // 2) 凭据：同一时刻只保留一条凭据类问题；恢复正常即移除全部凭据类问题。
  stops.push(
    watch(
      () => events.credentialNotice,
      (text) => {
        const classified = classifyCredentialIssue(String(text ?? ""));
        for (const id of [
          GLOBAL_ISSUE_IDS.noCredentials,
          GLOBAL_ISSUE_IDS.credentialPaused,
          GLOBAL_ISSUE_IDS.credentialRevoked,
          GLOBAL_ISSUE_IDS.credentialState,
        ]) {
          if (!classified || classified.id !== id) issues.resolve(id);
        }
        if (!classified) return;
        issues.upsert({
          id: classified.id,
          kind: classified.kind,
          title: classified.title,
          reason: String(text ?? "").trim(),
          actions: SETTINGS_CRED,
          diagnostics: [
            "凭据内容不会在这里显示；只能在本机设置页查看与修改。",
            String(text ?? "").trim(),
          ],
          severity: classified.severity,
          at: Date.now(),
        });
      },
      { immediate: true },
    ),
  );

  // 3) 模型能力：后端明确说 unsupported 才报；恢复 native/text 后移除。
  stops.push(
    watch(
      () => [events.modelMode, events.fallbackNotice] as const,
      ([mode, fallback]) => {
        if (mode === "unsupported") {
          issues.upsert({
            id: GLOBAL_ISSUE_IDS.modelCapability,
            kind: "model_capability_fallback",
            title: "当前模型能力受限",
            reason:
              String(fallback ?? "").trim() ||
              "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）",
            actions: SETTINGS_MODEL,
            diagnostics: ["后端能力事件（CAPABILITY）报告当前适配模式为 unsupported。"],
            severity: "warning",
            at: Date.now(),
          });
          return;
        }
        issues.resolve(GLOBAL_ISSUE_IDS.modelCapability);
      },
      { immediate: true },
    ),
  );

  // 4) 恢复失败（启动 / 重连 / RESYNC）：失败期间界面状态可能不是最新，必须说出来并可重试。
  stops.push(
    watch(
      () => [session.resyncState, session.lastError] as const,
      ([state, lastError]) => {
        if (state !== "failed") {
          issues.resolve(GLOBAL_ISSUE_IDS.restore);
          return;
        }
        const reason = String(lastError ?? "").trim() || "状态同步失败";
        issues.upsert({
          id: GLOBAL_ISSUE_IDS.restore,
          kind: "restore_failed",
          title: "界面状态可能不是最新的",
          reason,
          actions: [{ label: "重新同步", kind: "retry-restore" }],
          diagnostics: [reason],
          severity: "error",
          at: Date.now(),
        });
      },
      { immediate: true },
    ),
  );

  return {
    stop() {
      for (const stop of stops) stop();
      stops.length = 0;
    },
  };
}
