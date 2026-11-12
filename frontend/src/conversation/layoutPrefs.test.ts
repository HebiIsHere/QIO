import { describe, expect, it } from "vitest";
import {
  CENTER_MIN,
  LEFT_MAX,
  LEFT_MIN,
  LEFT_RAIL_WIDTH,
  RIGHT_MAX,
  RIGHT_MIN,
  SPLITTER_WIDTH,
  clampLeftWidth,
  clampRightWidth,
  defaultLayoutPrefs,
  resolveConversationLayout,
  sanitizeLayoutPrefs,
} from "./layoutPrefs";
import type { ConversationLayoutPrefs } from "./contracts";

function prefs(over: Partial<ConversationLayoutPrefs> = {}): ConversationLayoutPrefs {
  return { ...defaultLayoutPrefs(), ...over };
}

function tab() {
  return {
    id: "attachment:a1",
    target: {
      id: "attachment:a1",
      kind: "text" as const,
      title: "方案.md",
      source: "attachment" as const,
      attachmentId: "a1",
    },
    position: { scrollTop: 120 },
  };
}

/** 与模板一致：左栏可见时占「宽度 + 一个分隔条」；收起时占「收起栏 + 12px 槽位」。 */
function occupied(layout: ReturnType<typeof resolveConversationLayout>): number {
  const leftPart = layout.leftVisible
    ? layout.leftWidth + SPLITTER_WIDTH
    : LEFT_RAIL_WIDTH + SPLITTER_WIDTH;
  const rightPart = layout.rightVisible ? layout.rightWidth + SPLITTER_WIDTH : 0;
  return leftPart + layout.centerWidth + rightPart;
}

describe("#83 按中央实际可用宽度解析三栏（不再用固定断点/硬编码 1440）", () => {
  it("并排时中央宽度永不低于可读/可输入下限，且三块刚好填满实际视口", () => {
    const combos = [
      prefs({ leftWidth: LEFT_MAX, rightWidth: RIGHT_MAX, rightOpen: true, previewTabs: [tab()] }),
      prefs({ leftWidth: 292, rightWidth: 420, rightOpen: true, previewTabs: [tab()] }),
      prefs({ leftWidth: LEFT_MAX, rightWidth: RIGHT_MAX, rightOpen: false, previewTabs: [] }),
      prefs({ leftCollapsed: true, rightWidth: 420, rightOpen: true, previewTabs: [tab()] }),
    ];
    for (const width of [700, 800, 872, 900, 1024, 1056, 1180, 1200, 1440, 1920]) {
      for (const p of combos) {
        const layout = resolveConversationLayout(width, p);
        const label = width + "px / " + JSON.stringify(p);
        expect(layout.centerWidth, label).toBeGreaterThanOrEqual(CENTER_MIN);
        expect(occupied(layout), label).toBe(width);
      }
    }
  });

  it("宽窗恢复用户手动栏宽：左右栏按保存值排布", () => {
    const layout = resolveConversationLayout(
      1920,
      prefs({ leftWidth: 400, rightWidth: 700, rightOpen: true, previewTabs: [tab()] }),
    );
    expect(layout.leftVisible).toBe(true);
    expect(layout.leftWidth).toBe(400);
    expect(layout.rightVisible).toBe(true);
    expect(layout.rightWidth).toBe(700);
    expect(layout.previewFull).toBe(false);
    expect(layout.centerWidth).toBe(1920 - 400 - 700 - SPLITTER_WIDTH * 2);
  });

  it("1200 宽窗：先收右栏、再收左栏到下限，中央仍保住可输入宽度", () => {
    const layout = resolveConversationLayout(
      1200,
      prefs({ leftWidth: 420, rightWidth: 760, rightOpen: true, previewTabs: [tab()] }),
    );
    expect(layout.leftVisible, "左栏不应在还能放下时整栏消失").toBe(true);
    expect(layout.rightWidth).toBe(RIGHT_MIN);
    expect(layout.leftWidth).toBe(1200 - RIGHT_MIN - CENTER_MIN - SPLITTER_WIDTH * 2);
    expect(layout.leftWidth).toBeGreaterThanOrEqual(LEFT_MIN);
    expect(layout.centerWidth).toBe(CENTER_MIN);
  });

  it("左栏收起的前后两侧中央宽度连续：不靠 1180 固定断点，也不出现整块跳变", () => {
    const withPreview = prefs({ leftWidth: 420, rightWidth: 760, rightOpen: true, previewTabs: [tab()] });
    // 1056：左栏刚好还能留到下限；1055：左栏让位、右栏立即补位 —— 两侧中央都是 460。
    for (const width of [1040, 1050, 1055, 1056, 1060]) {
      const layout = resolveConversationLayout(width, withPreview);
      expect(layout.centerWidth, width + "px 中央宽度").toBe(CENTER_MIN);
    }
    expect(resolveConversationLayout(1056, withPreview).leftVisible).toBe(true);
    expect(resolveConversationLayout(1055, withPreview).leftVisible).toBe(false);
  });

  it("没有预览时：1000px 仍能三栏并排（旧 1180 断点不再提前收起左栏）", () => {
    const layout = resolveConversationLayout(1000, prefs({ leftWidth: 292 }));
    expect(layout.leftVisible).toBe(true);
    expect(layout.leftWidth).toBe(292);
    expect(layout.centerWidth).toBe(1000 - 292 - SPLITTER_WIDTH);
    // 只有中央真的放不下左栏下限（< 704）才临时收起
    expect(resolveConversationLayout(704, prefs({ leftWidth: 292 })).leftVisible).toBe(true);
    expect(resolveConversationLayout(700, prefs({ leftWidth: 292 })).leftVisible).toBe(false);
  });

  it("用户手动收起仍有效：窗口变宽也不自动展开", () => {
    const layout = resolveConversationLayout(1920, prefs({ leftCollapsed: true }));
    expect(layout.leftVisible).toBe(false);
    expect(layout.leftWidth).toBe(LEFT_RAIL_WIDTH);
    expect(layout.centerWidth).toBe(1920 - LEFT_RAIL_WIDTH - SPLITTER_WIDTH);
  });

  it("分栏放不下右栏下限时改为全窗口预览；背景中央仍然可用（不被压成 0）", () => {
    const layout = resolveConversationLayout(
      800,
      prefs({ leftWidth: 292, rightWidth: 420, rightOpen: true, previewTabs: [tab()] }),
    );
    expect(layout.previewFull).toBe(true);
    expect(layout.rightVisible).toBe(false);
    expect(layout.centerWidth).toBe(800 - LEFT_RAIL_WIDTH - SPLITTER_WIDTH);
    expect(layout.centerWidth).toBeGreaterThanOrEqual(CENTER_MIN);
  });

  it("预览收起但仍有标签时不算可见，不占中央宽度", () => {
    const layout = resolveConversationLayout(
      1440,
      prefs({ rightOpen: false, rightWidth: 700, previewTabs: [tab()] }),
    );
    expect(layout.rightVisible).toBe(false);
    expect(layout.previewFull).toBe(false);
    expect(layout.centerWidth).toBe(1440 - 292 - SPLITTER_WIDTH);
  });
});

describe("#47 布局偏好容错：读取不再用硬编码 1440 改写用户栏宽", () => {
  it("1920 屏保存的 700px 右栏原样保留；窄窗口只在排布时收缩，不改写偏好", () => {
    const stored = sanitizeLayoutPrefs({ leftWidth: 292, rightWidth: 700, rightOpen: true });
    expect(stored.rightWidth).toBe(700);
    expect(stored.rightWidth).toBeLessThanOrEqual(RIGHT_MAX);
    const withTabs = sanitizeLayoutPrefs({
      leftWidth: 292,
      rightWidth: 700,
      rightOpen: true,
      previewTabs: [tab()],
    });
    expect(withTabs.rightWidth, "不再用 1440 视口夹到 664").toBe(700);
    // 宽窗按 700 排布
    const wide = resolveConversationLayout(1920, withTabs);
    expect(wide.rightWidth).toBe(700);
    // 窄窗只是显示时收缩，保存值不变
    const narrow = resolveConversationLayout(1200, withTabs);
    expect(narrow.rightWidth).toBeLessThan(700);
    expect(withTabs.rightWidth).toBe(700);
  });

  it("栏宽夹取在用户可调范围内；给了实际视口才做视口约束", () => {
    expect(clampLeftWidth(10)).toBe(LEFT_MIN);
    expect(clampLeftWidth(9999)).toBe(LEFT_MAX);
    expect(clampLeftWidth(301.6)).toBe(302);
    expect(clampRightWidth(200)).toBe(RIGHT_MIN);
    expect(clampRightWidth(9999)).toBe(RIGHT_MAX);
    expect(clampRightWidth(9999, 1440, 292)).toBe(
      Math.min(RIGHT_MAX, 1440 - 292 - SPLITTER_WIDTH * 2 - CENTER_MIN),
    );
  });

  it("损坏/缺失的偏好仍回落默认值，坏标签丢弃", () => {
    const defaults = defaultLayoutPrefs();
    expect(sanitizeLayoutPrefs(null)).toEqual(defaults);
    expect(sanitizeLayoutPrefs("bad")).toEqual(defaults);
    const prefsValue = sanitizeLayoutPrefs({
      leftWidth: 9999,
      rightWidth: -5,
      taskTab: "unknown-tab",
      previewTabs: [
        {
          id: "attachment:a@1",
          target: { id: "attachment:a@1", kind: "pdf", title: "a.pdf", source: "attachment" },
          position: { page: 3 },
        },
        { id: "", target: {} },
      ],
      activePreviewTabId: "missing",
    });
    expect(prefsValue.leftWidth).toBe(LEFT_MAX);
    expect(prefsValue.rightWidth).toBe(RIGHT_MIN);
    expect(prefsValue.taskTab).toBe("current");
    expect(prefsValue.previewTabs).toHaveLength(1);
    expect(prefsValue.previewTabs[0].position.page).toBe(3);
    expect(prefsValue.activePreviewTabId).toBe("attachment:a@1");
  });
});
