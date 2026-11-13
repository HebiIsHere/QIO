/**
 * #47 · 还原默认布局：只重置布局字段、保留标签与阅读位置、竞态保护、失败可重试。
 *
 * 这里测的是「常规 → 窗口行为 → 还原默认布局」这个真实入口：
 * 按钮走 RegularSection → conversationShell.resetLayout() → 既有 /api/settings/ui 保存通道。
 * 模型与 API 全部是 fake，不联网。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { flushPromises } from "@vue/test-utils";
import type { Pinia } from "pinia";
import { resetFloatPositions } from "../../../composables/floatingState";
import type { ConversationLayoutPrefs } from "../../../conversation/contracts";
import type { PreviewTabState } from "../../../conversation/previewTypes";
import {
  LEFT_DEFAULT,
  RIGHT_DEFAULT,
  defaultLayoutPrefs,
  sanitizeLayoutPrefs,
} from "../../../conversation/layoutPrefs";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import { makePinia, mountSettings, sectionById, setViewportWidth } from "./settingsTestUtils";

vi.mock("../../../services/api", async () => {
  const { settingsApiModule } = await import("./settingsApiMock");
  return settingsApiModule();
});

vi.mock("../../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("./settingsApiMock");
  return modelCatalogModule();
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  resetFloatPositions();
  document.body.innerHTML = "";
  setViewportWidth(1280);
});

afterEach(() => {
  // 防抖计时器可能还挂着：清掉，避免跨用例写请求/计数串味。
  useConversationShellStore().dispose();
});

function textTab(
  id: string,
  title: string,
  position: PreviewTabState["position"] = {},
): PreviewTabState {
  return {
    id,
    target: {
      id,
      kind: "text",
      title,
      source: "attachment",
      attachmentId: id.replace(/^attachment:/, ""),
    },
    position,
  };
}

/** 用户已经调过布局的状态（服务端布局已读取；标签顺序为 a、b，当前标签是 a）。 */
function userAdjustedShell(): { shell: ReturnType<typeof useConversationShellStore>; pinia: Pinia } {
  const pinia = makePinia();
  const shell = useConversationShellStore();
  shell.prefs = {
    ...defaultLayoutPrefs(),
    leftWidth: 380,
    leftCollapsed: true,
    rightWidth: 700,
    rightOpen: true,
    previewFull: true,
    taskTab: "history",
    previewTabs: [
      textTab("attachment:a", "方案.md", { scrollTop: 120, search: "预算" }),
      textTab("attachment:b", "报告.pdf", { page: 3, zoom: 1.25 }),
    ],
    activePreviewTabId: "attachment:a",
  };
  // 真实流程里 ConversationView 已经读过服务端布局；这里直接标记，避免用例中再被
  // 默认 mock 的无布局读取覆盖（启动即还原的场景由专门用例覆盖）。
  shell.loaded = true;
  shell.layoutLoaded = true;
  return { shell, pinia };
}

function resetButton(w: Awaited<ReturnType<typeof mountSettings>>) {
  return sectionById(w, "regular").find('[data-settings-key="regular.window.reset-layout"] button');
}

/** 让 saveNow / 串行保存链的微任务全部结算（不推进 400ms 防抖计时器）。 */
async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await flushPromises();
}

async function mountAndClickReset(pinia: Pinia) {
  const w = await mountSettings(pinia);
  await resetButton(w).trigger("click");
  await settle();
  return w;
}

describe("#47 还原默认布局：只改布局字段", () => {
  it("栏宽/左栏收起/预览开合/手动全窗口回到默认；标签顺序、当前标签、搜索词与位置、任务页签保留", async () => {
    const { api } = await import("../../../services/api");
    const { shell, pinia } = userAdjustedShell();
    const tabsBefore = JSON.parse(JSON.stringify(shell.prefs.previewTabs));
    const w = await mountAndClickReset(pinia);
    const regular = sectionById(w, "regular");

    // 真实保存通道只写一次，且写出的正是「还原后的布局 + 原标签」
    expect(api.saveConversationLayout).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(api.saveConversationLayout).mock.calls[0][0] as ConversationLayoutPrefs;
    expect(payload.leftWidth).toBe(LEFT_DEFAULT);
    expect(payload.leftCollapsed).toBe(false);
    expect(payload.rightWidth).toBe(RIGHT_DEFAULT);
    expect(payload.rightOpen).toBe(false);
    expect(payload.previewFull).toBe(false);
    expect(payload.taskTab).toBe("history");
    expect(payload.previewTabs.map((tab) => tab.id)).toEqual(["attachment:a", "attachment:b"]);
    expect(payload.activePreviewTabId).toBe("attachment:a");
    expect(typeof payload.updatedAt).toBe("string");

    // 内存状态：布局字段变了，其它一律没动
    expect(shell.prefs.leftWidth).toBe(LEFT_DEFAULT);
    expect(shell.prefs.leftCollapsed).toBe(false);
    expect(shell.prefs.rightWidth).toBe(RIGHT_DEFAULT);
    expect(shell.prefs.rightOpen).toBe(false);
    expect(shell.prefs.previewFull).toBe(false);
    expect(shell.prefs.previewTabs).toEqual(tabsBefore);
    expect(shell.prefs.activePreviewTabId).toBe("attachment:a");
    expect(shell.prefs.taskTab).toBe("history");

    // 保存成功后只给一次短反馈（不是点一下就先说已还原）
    const ok = regular.find(".set-notice.ok");
    expect(ok.exists()).toBe(true);
    expect(ok.text()).toContain("已还原默认布局");
    expect(ok.text()).toContain("预览标签与阅读位置保留");
    expect(regular.find(".set-notice.err").exists()).toBe(false);
    expect(shell.saveError).toBe("");
    w.unmount();
  });

  it("默认布局收起预览；重新打开回到原标签与原位置（刷新后按保存值恢复）", async () => {
    const { api } = await import("../../../services/api");
    const { shell, pinia } = userAdjustedShell();
    const w = await mountAndClickReset(pinia);
    const payload = vi.mocked(api.saveConversationLayout).mock.calls[0][0] as ConversationLayoutPrefs;

    expect(shell.prefs.rightOpen).toBe(false);
    expect(shell.rightVisible).toBe(false);

    // 刷新/重启后读回：收起预览但标签、当前标签、搜索词与阅读位置都在
    const reloaded = sanitizeLayoutPrefs(JSON.parse(JSON.stringify(payload)));
    expect(reloaded.rightOpen).toBe(false);
    expect(reloaded.activePreviewTabId).toBe("attachment:a");
    expect(reloaded.previewTabs.map((tab) => tab.id)).toEqual(["attachment:a", "attachment:b"]);
    expect(reloaded.previewTabs[0].position).toEqual({ scrollTop: 120, search: "预算" });
    expect(reloaded.previewTabs[1].position).toEqual({ page: 3, zoom: 1.25 });

    // 重新打开预览栏：回到原标签，阅读位置（搜索词/滚动）原样可取
    shell.setRightOpen(true);
    const preview = useConversationPreviewStore();
    expect(shell.rightVisible).toBe(true);
    expect(preview.activeTab?.id).toBe("attachment:a");
    // positionOf 本身是「按标签取位置」的函数（Pinia getter 返回函数）
    expect(preview.positionOf("attachment:a")).toEqual({ scrollTop: 120, search: "预算" });

    // 宽窗回到分栏（手动全窗口偏好已被还原，不会继续全窗口）
    shell.viewportWidth = 1440;
    expect(shell.effectivePreviewFull).toBe(false);
    expect(shell.splitRightVisible).toBe(true);
    w.unmount();
  });

  it("启动直接进设置（尚未读取布局）时先读服务端真实标签再保存，不清空标签与阅读位置", async () => {
    const { api } = await import("../../../services/api");
    const pinia = makePinia();
    const shell = useConversationShellStore();
    shell.prefs = defaultLayoutPrefs();
    shell.loaded = false;
    shell.layoutLoaded = false;
    vi.mocked(api.getUISettings).mockResolvedValueOnce({
      typewriter_cps: 50,
      close_behavior: "ask",
      conversation_layout: {
        leftWidth: 380,
        leftCollapsed: true,
        rightWidth: 700,
        rightOpen: true,
        previewFull: true,
        previewTabs: [textTab("attachment:a", "方案.md", { scrollTop: 120, search: "预算" })],
        activePreviewTabId: "attachment:a",
      },
    } as never);

    const w = await mountAndClickReset(pinia);

    expect(api.getUISettings).toHaveBeenCalledTimes(1);
    expect(shell.layoutLoaded).toBe(true);
    const payload = vi.mocked(api.saveConversationLayout).mock.calls[0][0] as ConversationLayoutPrefs;
    expect(payload.leftWidth).toBe(LEFT_DEFAULT);
    expect(payload.rightOpen).toBe(false);
    expect(payload.previewTabs.map((tab) => tab.id)).toEqual(["attachment:a"]);
    expect(payload.previewTabs[0].position).toEqual({ scrollTop: 120, search: "预算" });
    expect(payload.activePreviewTabId).toBe("attachment:a");
    w.unmount();
  });
});

describe("#47 竞态：迟到旧回执不得覆盖还原结果或更新的标签/位置", () => {
  it("拖动分隔线后立刻还原：旧保存回执迟到，不覆盖还原后的布局", async () => {
    const { api } = await import("../../../services/api");
    const { shell, pinia } = userAdjustedShell();
    const stale = JSON.parse(JSON.stringify(shell.prefs)) as ConversationLayoutPrefs;
    let releaseOldSave!: (value: { ok: boolean; conversation_layout: unknown }) => void;
    const oldSave = new Promise<{ ok: boolean; conversation_layout: unknown }>((resolve) => {
      releaseOldSave = resolve;
    });
    vi.mocked(api.saveConversationLayout).mockImplementationOnce((layout) => {
      // 旧请求写出的是「拖动后的布局」
      expect(layout.leftWidth).toBe(380);
      return oldSave;
    });

    // 进设置前：拖动分隔线触发的保存已经在途
    const oldFlush = shell.flush();
    await nextTick();
    expect(api.saveConversationLayout).toHaveBeenCalledTimes(1);

    // 进设置点「还原默认布局」：还原的写入排在旧写入之后
    const w = await mountSettings(pinia);
    await resetButton(w).trigger("click");
    await flushPromises();
    expect(api.saveConversationLayout).toHaveBeenCalledTimes(1);

    // 旧回执现在才回来（内容正是旧布局）
    releaseOldSave({ ok: true, conversation_layout: stale });
    await oldFlush;
    await settle();

    // 迟到旧回执被忽略：还原结果仍在，且新写入一定在旧写入之后
    expect(api.saveConversationLayout).toHaveBeenCalledTimes(2);
    const newest = vi.mocked(api.saveConversationLayout).mock.calls[1][0] as ConversationLayoutPrefs;
    expect(newest.leftWidth).toBe(LEFT_DEFAULT);
    expect(newest.leftCollapsed).toBe(false);
    expect(newest.rightWidth).toBe(RIGHT_DEFAULT);
    expect(newest.rightOpen).toBe(false);
    expect(newest.previewFull).toBe(false);
    expect(shell.prefs.leftWidth).toBe(LEFT_DEFAULT);
    expect(shell.prefs.leftCollapsed).toBe(false);
    expect(shell.prefs.rightWidth).toBe(RIGHT_DEFAULT);
    expect(shell.prefs.rightOpen).toBe(false);
    expect(shell.prefs.previewFull).toBe(false);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["attachment:a", "attachment:b"]);
    expect(shell.saveError).toBe("");
    w.unmount();
  });

  it("还原保存回执迟到：回到对话后新开的标签与新阅读位置不被旧回执覆盖", async () => {
    const { api } = await import("../../../services/api");
    const { shell, pinia } = userAdjustedShell();
    let releaseResetSave!: (value: { ok: boolean; conversation_layout: unknown }) => void;
    const resetSave = new Promise<{ ok: boolean; conversation_layout: unknown }>((resolve) => {
      releaseResetSave = resolve;
    });
    let sent: ConversationLayoutPrefs | null = null;
    vi.mocked(api.saveConversationLayout).mockImplementationOnce((layout) => {
      sent = JSON.parse(JSON.stringify(layout)) as ConversationLayoutPrefs;
      return resetSave;
    });

    const w = await mountSettings(pinia);
    await resetButton(w).trigger("click");
    await flushPromises();
    // 还原请求已带着「旧标签 [a, b] + 默认布局」发出，但回执还没回来
    expect(sent).not.toBeNull();
    expect(sent!.previewTabs.map((tab) => tab.id)).toEqual(["attachment:a", "attachment:b"]);

    // 回到对话后：又开了一个标签、改了搜索词与阅读位置（此时旧回执仍未返回）
    shell.setPreviewTabs([textTab("attachment:c", "新文件.md")], "attachment:c");
    shell.prefs.previewTabs.find((tab) => tab.id === "attachment:a")!.position = {
      scrollTop: 999,
      search: "新搜索",
    };
    shell.scheduleSave();

    // 迟到的旧回执（只有 a/b，且是还原时的位置）
    releaseResetSave({ ok: true, conversation_layout: sent });
    await settle();

    // 新标签、新当前标签、新阅读位置都没有被旧数据盖回去
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual([
      "attachment:a",
      "attachment:b",
      "attachment:c",
    ]);
    expect(shell.prefs.activePreviewTabId).toBe("attachment:c");
    expect(shell.prefs.previewTabs[0].position).toEqual({ scrollTop: 999, search: "新搜索" });
    expect(shell.prefs.leftWidth).toBe(LEFT_DEFAULT);
    expect(shell.prefs.rightOpen).toBe(false);
    expect(shell.saveError).toBe("");

    // 新状态随后自己落盘（不依赖旧回执）：最后一次写入包含新标签与新位置
    await shell.flush();
    const calls = vi.mocked(api.saveConversationLayout).mock.calls;
    const newest = calls[calls.length - 1][0] as ConversationLayoutPrefs;
    expect(newest.previewTabs.map((tab) => tab.id)).toEqual([
      "attachment:a",
      "attachment:b",
      "attachment:c",
    ]);
    expect(newest.activePreviewTabId).toBe("attachment:c");
    expect(newest.previewTabs.find((tab) => tab.id === "attachment:a")!.position.search).toBe("新搜索");
    w.unmount();
  });
});

describe("#47 失败与窄窗", () => {
  it("保存失败：明确显示失败（含技术详情）并可重试；重试成功后才给成功短反馈", async () => {
    const { api } = await import("../../../services/api");
    const { shell, pinia } = userAdjustedShell();
    vi.mocked(api.saveConversationLayout).mockRejectedValueOnce(new Error("network down"));
    const w = await mountSettings(pinia);
    const regular = sectionById(w, "regular");

    await resetButton(w).trigger("click");
    await settle();

    const err = regular.find(".set-notice.err");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("还原默认布局失败");
    expect(regular.find('[data-settings-action="retry"]').exists()).toBe(true);
    expect(regular.find("details").text()).toContain("network down");
    expect(regular.find(".set-notice.ok").exists()).toBe(false);
    // 失败不伪造成功：错误留在 store（可重试），内存里的还原结果保留
    expect(shell.saveError).toContain("布局偏好保存失败");
    expect(shell.saveError).toContain("network down");
    expect(shell.prefs.leftWidth).toBe(LEFT_DEFAULT);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["attachment:a", "attachment:b"]);

    // 重试同一次还原：成功之后才显示成功反馈
    await regular.find('[data-settings-action="retry"]').trigger("click");
    await settle();

    expect(regular.find(".set-notice.err").exists()).toBe(false);
    expect(regular.find(".set-notice.ok").text()).toContain("已还原默认布局");
    expect(shell.saveError).toBe("");
    expect(api.saveConversationLayout).toHaveBeenCalledTimes(2);
    const newest = vi.mocked(api.saveConversationLayout).mock.calls[1][0] as ConversationLayoutPrefs;
    expect(newest.leftWidth).toBe(LEFT_DEFAULT);
    expect(newest.previewTabs.map((tab) => tab.id)).toEqual(["attachment:a", "attachment:b"]);
    w.unmount();
  });

  it("窄窗临时收起/临时全窗口由布局推导，不写成手动偏好；拉宽后自动恢复", async () => {
    const { api } = await import("../../../services/api");
    const { shell, pinia } = userAdjustedShell();
    const w = await mountAndClickReset(pinia);

    // 还原后手动偏好是默认值（没有手动收起/没有手动全窗口）
    expect(shell.prefs.leftCollapsed).toBe(false);
    expect(shell.prefs.previewFull).toBe(false);

    // 窄窗：左栏临时收起、预览临时全窗口 —— 都只是推导结果
    shell.viewportWidth = 680;
    expect(shell.leftVisible).toBe(false);
    expect(shell.autoCollapsedLeft).toBe(true);
    expect(shell.prefs.leftCollapsed).toBe(false);
    shell.setRightOpen(true);
    expect(shell.rightVisible).toBe(true);
    expect(shell.effectivePreviewFull).toBe(true);
    expect(shell.prefs.previewFull).toBe(false);

    // 落盘的仍是默认手动偏好（临时状态没被写回）
    await shell.flush();
    const calls = vi.mocked(api.saveConversationLayout).mock.calls;
    const newest = calls[calls.length - 1][0] as ConversationLayoutPrefs;
    expect(newest.leftCollapsed).toBe(false);
    expect(newest.previewFull).toBe(false);
    expect(newest.rightOpen).toBe(true);

    // 拉宽：不靠用户再点一次，自动回到三栏 + 分栏预览
    shell.viewportWidth = 1440;
    expect(shell.leftVisible).toBe(true);
    expect(shell.effectivePreviewFull).toBe(false);
    expect(shell.splitRightVisible).toBe(true);
    expect(shell.prefs.leftCollapsed).toBe(false);
    expect(shell.prefs.previewFull).toBe(false);
    w.unmount();
  });
});
