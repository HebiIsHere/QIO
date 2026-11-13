/**
 * 全局异常来源：SSE 断线 / 凭据 / 模型能力 / 恢复失败；根因去重与部分恢复。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../../../stores/events";
import { useSessionStore } from "../../../stores/session";
import { useGlobalIssuesStore } from "../../../stores/globalIssues";
import { useGlobalIssueSources } from "../useGlobalIssueSources";

async function tick(): Promise<void> {
  await nextTick();
  await nextTick();
}

function setup() {
  setActivePinia(createPinia());
  const events = useEventStore();
  const session = useSessionStore();
  const issues = useGlobalIssuesStore();
  const handle = useGlobalIssueSources();
  return { events, session, issues, handle };
}

describe("全局异常来源：一个入口、根因去重、部分恢复", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("SSE 断线上报、重连后移除", async () => {
    const { events, issues, handle } = setup();
    events.connected = false;
    events.error = "SSE 连接断开，请确认后端已启动";
    await tick();
    expect(issues.count).toBe(1);
    expect(issues.issues[0].id).toBe("sse_disconnected");
    expect(issues.issues[0].actions[0].kind).toBe("retry-connection");

    events.connected = true;
    events.error = null;
    await tick();
    expect(issues.count).toBe(0);
    handle.stop();
  });

  it("凭据：同一时刻只保留一条；恢复后全部移除", async () => {
    const { events, issues, handle } = setup();
    events.credentialNotice =
      "当前没有可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key";
    await tick();
    expect(issues.count).toBe(1);
    expect(issues.issues[0].id).toBe("no_credentials");
    expect(issues.issues[0].actions[0].section).toBe("cred");

    // 换成「暂停」：凭据类问题只保留一条，不重复计数
    events.credentialNotice = "有一项凭据已暂停：依赖它的能力暂时不可用（可在「设置 → 凭据」恢复）";
    await tick();
    expect(issues.count).toBe(1);
    expect(issues.issues[0].id).toBe("credential_paused");

    events.credentialNotice = null;
    await tick();
    expect(issues.count).toBe(0);
    handle.stop();
  });

  it("凭据暂停：只保留暂停状态与「打开凭据设置」，标题/正文/诊断不重复且不显示原始文本", async () => {
    const { events, issues, handle } = setup();
    events.credentialNotice =
      "有一项凭据已暂停：依赖它的能力暂时不可用（可在「设置 → 凭据」恢复）";
    await tick();

    const item = issues.issues[0];
    expect(item.id).toBe("credential_paused");
    expect(item.title).toContain("暂停");
    // 标题已经说清状态：正文不再复述，诊断只放额外信息（不重复、不显示凭据内容）。
    expect(item.reason).toBe("");
    expect(item.actions.map((action) => action.label)).toEqual(["打开凭据设置"]);
    expect(item.diagnostics).toEqual([
      "凭据内容不会在这里显示；只能在本机设置页查看与修改。",
    ]);
    const visible = [item.title, item.reason, ...item.diagnostics].join("\n");
    expect(visible.split("暂停").length - 1, "「暂停」只在标题出现一次").toBe(1);
    expect(visible).not.toContain("可在「设置 → 凭据」恢复");
    expect(visible).not.toContain("依赖它的能力暂时不可用（可在");
    handle.stop();
  });

  it("模型能力受限：报告一次，恢复正常后移除", async () => {
    const { events, issues, handle } = setup();
    events.modelMode = "unsupported";
    events.fallbackNotice = "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）";
    await tick();
    expect(issues.count).toBe(1);
    expect(issues.issues[0].id).toBe("model_capability_fallback");
    expect(issues.issues[0].actions[0].section).toBe("model");

    events.modelMode = "native";
    await tick();
    expect(issues.count).toBe(0);
    handle.stop();
  });

  it("恢复失败：失败期间报告，重新同步成功后移除", async () => {
    const { session, issues, handle } = setup();
    session.resyncState = "failed";
    session.lastError = "状态同步失败，界面显示的状态可能不是最新的：boom";
    await tick();
    expect(issues.count).toBe(1);
    expect(issues.issues[0].id).toBe("restore_failed");
    expect(issues.issues[0].actions[0].kind).toBe("retry-restore");

    session.resyncState = "normal";
    await tick();
    expect(issues.count).toBe(0);
    handle.stop();
  });

  it("部分恢复只移除对应一项（两个独立问题不会互相清除）", async () => {
    const { events, issues, handle } = setup();
    events.connected = false;
    events.error = "SSE 连接断开";
    events.credentialNotice = "当前没有可用的模型凭据";
    await tick();
    expect(issues.count).toBe(2);

    events.connected = true;
    events.error = null;
    await tick();
    expect(issues.count).toBe(1);
    expect(issues.issues[0].id).toBe("no_credentials");

    const panelOpenBefore = issues.panelOpen;
    issues.openPanel();
    expect(issues.panelOpen).toBe(true);
    events.credentialNotice = null;
    await tick();
    expect(issues.count).toBe(0);
    // 全部恢复：入口与面板一起收起
    expect(issues.panelOpen).toBe(false);
    void panelOpenBefore;
    handle.stop();
  });
});

describe("不进全局入口的事实：单条回复失败 / 用户停止", () => {
  it("会话最后一轮失败或用户停止（resyncState 正常）不产生全局问题", async () => {
    const { session, issues, handle } = setup();
    session.resyncState = "normal";
    session.lastError = "本轮执行失败：模型调用被拒绝（401）";
    session.lastTurnOutcome = { turnId: "turn_1", status: "failed" };
    await tick();
    expect(issues.count).toBe(0);

    session.lastTurnOutcome = { turnId: "turn_2", status: "cancelled" };
    await tick();
    expect(issues.count).toBe(0);
    handle.stop();
  });
});
