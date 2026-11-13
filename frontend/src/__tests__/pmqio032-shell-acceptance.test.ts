/**
 * Lead 独立验收（pm-qio032-shell 审计修复组）。
 *
 * 写法：先在基线 7445170d 上运行 → 期望红（独立复现审计问题）；
 * 四分支集成到父分支后再跑 → 期望绿。只覆盖能脱离真实后端、可客观断言的跨分支语义；
 * 真机/真实浏览器项见 _reports/ 的验收报告。
 *
 * 覆盖：#39 失效设置残留、#14/#15 文案、#66 分类标题、#67 Esc 焦点/输入法、#52 标签快捷键焦点归属。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getUISettings: vi.fn(),
  saveConversationLayout: vi.fn(),
}));

vi.mock("../services/api", async () => {
  const { settingsApiModule } = await import("../views/settings/__tests__/settingsApiMock");
  const mod = settingsApiModule();
  return {
    ...mod,
    api: {
      ...mod.api,
      getUISettings: mocks.getUISettings,
      saveConversationLayout: mocks.saveConversationLayout,
    },
  };
});

vi.mock("../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("../views/settings/__tests__/settingsApiMock");
  return modelCatalogModule();
});

import PreviewPanel from "../components/conversation/PreviewPanel.vue";
import SettingsSearchPanel from "../views/settings/shell/SettingsSearchPanel.vue";
import { SETTINGS_SEARCH_INDEX, searchSettings } from "../views/settings/searchIndex";
import { useConversationShellStore } from "../stores/conversationShell";
import { useConversationPreviewStore } from "../stores/conversationPreview";
import type { PreviewTabState } from "../conversation/previewTypes";
import { flushFrames, makePinia, mountSettings } from "../views/settings/__tests__/settingsTestUtils";

function codeTab(index: number): PreviewTabState {
  const id = "replycode:lead:" + index + ":0";
  return {
    id,
    target: {
      id,
      kind: "code",
      title: "验收文件 " + index,
      source: "reply_code",
      messageId: "lead" + index,
      blockIndex: 0,
    },
    position: {},
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
  document.body.innerHTML = "";
  mocks.getUISettings.mockReset();
  mocks.getUISettings.mockResolvedValue({ typewriter_cps: 50, close_behavior: "ask", conversation_layout: null });
  mocks.saveConversationLayout.mockReset();
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({ conversation_layout: prefs }));
});

afterEach(() => {
  try { useConversationPreviewStore().resetForTests(); } catch { /* 未初始化 */ }
  try { useConversationShellStore().dispose(); } catch { /* 未初始化 */ }
  localStorage.clear();
  document.body.innerHTML = "";
});

describe("#39 已失效的贴边隐藏设置不得残留（设置/搜索索引）", () => {
  it("两个隐藏开关不再出现在搜索索引与搜索结果", () => {
    for (const key of ["regular.window.planet-hide", "regular.window.settings-hide"]) {
      expect(SETTINGS_SEARCH_INDEX.some((entry) => entry.key === key), key).toBe(false);
      expect(searchSettings("贴靠后自动隐藏").some((entry) => entry.key === key), key).toBe(false);
    }
  });
});

describe("#14/#15 设置搜索结果说明", () => {
  it("免密钥搜索说明 = 使用免密钥搜索服务", () => {
    const hit = SETTINGS_SEARCH_INDEX.find((entry) => entry.key === "models.search.keyless");
    expect(hit?.description ?? "").toBe("使用免密钥搜索服务");
  });

  it("单段长度说明 = 单段长度上限（按估算字符数计）", () => {
    const hit = SETTINGS_SEARCH_INDEX.find((entry) => entry.key === "memory.fragment.max-tokens");
    expect(hit?.description ?? "").toBe("单段长度上限（按估算字符数计）");
  });
});

describe("#66 搜索用分类标题区分结果、行内不重复分类", () => {
  it("有分组标题，且结果行不再显示分类标签", () => {
    const w = mount(SettingsSearchPanel, { props: { query: "记忆", open: true } });
    expect(w.findAll(".set-search__group").length).toBeGreaterThan(0);
    expect(w.findAll(".set-search__hit-cat")).toHaveLength(0);
    w.unmount();
  });
});

describe("#67 设置搜索 Esc：焦点交还入口；输入法组字优先", () => {
  it("组字中的 Esc 不关闭搜索；正常 Esc 关闭并聚焦搜索入口", async () => {
    makePinia();
    const w = await mountSettings();
    const entry = w.find('[data-testid="settings-search-entry"]');
    expect(entry.exists()).toBe(true);
    await entry.trigger("click");
    await nextTick();
    const panel = w.find('[data-testid="settings-search-panel"]');
    const input = w.find('[data-testid="settings-search-input"]');
    expect(panel.isVisible()).toBe(true);
    await input.setValue("记忆");
    await nextTick();

    // 中文输入法组字中按 Esc：只取消候选词，不关闭搜索面板
    const composing = new KeyboardEvent("keydown", {
      key: "Escape",
      isComposing: true,
      bubbles: true,
      cancelable: true,
    });
    input.element.dispatchEvent(composing);
    await nextTick();
    expect(panel.isVisible()).toBe(true);

    await input.trigger("keydown", { key: "Escape" });
    await flushFrames();
    expect(panel.isVisible()).toBe(false);
    expect(document.activeElement).toBe(entry.element);
    w.unmount();
  });
});

describe("#52 预览标签快捷键按实际焦点所指标签", () => {
  it("乙选中、焦点在关闭甲时，Delete 不得误关乙", async () => {
    const shell = useConversationShellStore();
    const preview = useConversationPreviewStore();
    const tabs = [codeTab(1), codeTab(2)];
    shell.prefs.previewTabs = tabs;
    shell.prefs.activePreviewTabId = tabs[1].id; // 乙选中
    shell.prefs.rightOpen = true;
    for (const tab of tabs) {
      preview.documents[tab.id] = { text: "alpha\nbeta" };
      preview.runtime[tab.id] = {
        phase: "ready",
        envelope: null,
        problem: null,
        version: "",
        notice: "",
        updateError: "",
        updateFailures: 0,
        loadedAt: 1,
      };
    }
    const w = mount(PreviewPanel, { attachTo: document.body });
    await flushPromises();
    const closes = w.findAll(".pv-tab-close");
    expect(closes).toHaveLength(2);
    (closes[0].element as HTMLElement).focus(); // 焦点在关闭甲
    const event = new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true });
    (closes[0].element as HTMLElement).dispatchEvent(event);
    await flushPromises();
    const ids = shell.prefs.previewTabs.map((tab) => tab.id);
    // 乙（选中的那个）不能被误关；操作应落在实际焦点所指的甲上
    expect(ids).toContain(tabs[1].id);
    expect(ids).not.toContain(tabs[0].id);
    w.unmount();
  });
});
describe("#53 切文件前立即记录当前标签最新搜索词", () => {
  it("先改搜索词再立刻切走，切回后仍是新词（不等防抖）", async () => {
    const shell = useConversationShellStore();
    const preview = useConversationPreviewStore();
    const textTab = (index: number): PreviewTabState => {
      const id = "taskoutput:lead:" + index;
      return {
        id,
        target: { id, kind: "text", title: "验收文本 " + index, source: "task_output", taskId: "lead" + index },
        position: {},
      };
    };
    const tabs = [textTab(1), textTab(2)];
    shell.prefs.previewTabs = tabs;
    shell.prefs.activePreviewTabId = tabs[0].id;
    shell.prefs.rightOpen = true;
    for (const tab of tabs) {
      preview.documents[tab.id] = { text: "alpha\nbeta\ngamma" };
      preview.runtime[tab.id] = {
        phase: "ready",
        envelope: null,
        problem: null,
        version: "",
        notice: "",
        updateError: "",
        updateFailures: 0,
        loadedAt: 1,
      };
    }
    const w = mount(PreviewPanel, { attachTo: document.body });
    await flushPromises();
    const input = w.find(".pv-search-input");
    expect(input.exists()).toBe(true);
    await input.setValue("beta");
    // 不等 200ms 防抖，立刻切到乙（组件会随 key 重建）
    await w.findAll(".pv-tab-main")[1].trigger("click");
    await flushPromises();
    await new Promise((resolve) => setTimeout(resolve, 260));
    await w.findAll(".pv-tab-main")[0].trigger("click");
    await flushPromises();
    expect((w.find(".pv-search-input").element as HTMLInputElement).value).toBe("beta");
    w.unmount();
  });
});
