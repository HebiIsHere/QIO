import { describe, expect, it } from "vitest";
import {
  CENTER_MIN,
  LEFT_DEFAULT,
  LEFT_MAX,
  LEFT_MIN,
  LEFT_RAIL_WIDTH,
  RIGHT_DEFAULT,
  RIGHT_MAX,
  RIGHT_MIN,
  SPLITTER_WIDTH,
  clampLeftWidth,
  clampRightWidth,
  cloneLayoutPrefs,
  defaultLayoutPrefs,
  layoutPrefsFingerprint,
  resolveConversationLayout,
  restoreDefaultLayout,
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

  it("#47 restoreDefaultLayout 只重置布局字段：标签/顺序/当前标签/位置/任务页签原样保留", () => {
    const original = prefs({
      leftWidth: 380,
      leftCollapsed: true,
      rightWidth: 700,
      rightOpen: true,
      previewFull: true,
      taskTab: "history",
      previewTabs: [
        { ...tab(), id: "attachment:a1", position: { scrollTop: 120, search: "预算" } },
        {
          ...tab(),
          id: "attachment:a2",
          target: { ...tab().target, id: "attachment:a2", title: "报告.pdf" },
          position: { page: 3 },
        },
      ],
      activePreviewTabId: "attachment:a1",
      updatedAt: "2026-11-12T00:00:00.000Z",
    });
    const snapshotBefore = JSON.parse(JSON.stringify(original));
    const next = restoreDefaultLayout(original);

    // 只改这四个用户可调字段（+ 版本回默认）
    expect(next.leftWidth).toBe(LEFT_DEFAULT);
    expect(next.leftCollapsed).toBe(false);
    expect(next.rightWidth).toBe(RIGHT_DEFAULT);
    expect(next.rightOpen).toBe(false);
    expect(next.previewFull).toBe(false);
    expect(next.version).toBe(1);
    // 不在还原范围：任务页签 / 标签及顺序 / 当前标签 / 阅读位置 / 写入时间
    expect(next.taskTab).toBe("history");
    expect(next.previewTabs.map((item) => item.id)).toEqual(["attachment:a1", "attachment:a2"]);
    expect(next.activePreviewTabId).toBe("attachment:a1");
    expect(next.previewTabs[0].position).toEqual({ scrollTop: 120, search: "预算" });
    expect(next.previewTabs[1].position).toEqual({ page: 3 });
    expect(next.updatedAt).toBe("2026-11-12T00:00:00.000Z");
    // 纯函数：不就地改输入
    expect(original).toEqual(snapshotBefore);
    expect(next).not.toBe(original);
  });

  it("#47 cloneLayoutPrefs 深拷贝标签与位置（发出时的快照不被后续修改影响）", () => {
    const original = prefs({
      previewTabs: [{ ...tab(), position: { scrollTop: 10 } }],
      activePreviewTabId: "attachment:a1",
    });
    const snapshot = cloneLayoutPrefs(original);
    original.previewTabs[0].position.scrollTop = 999;
    original.previewTabs[0].target.title = "改名后";
    expect(snapshot.previewTabs[0].position.scrollTop).toBe(10);
    expect(snapshot.previewTabs[0].target.title).toBe("方案.md");
    expect(snapshot).not.toBe(original);
  });

  it("#47 指纹忽略 updatedAt，但布局/标签/当前标签/阅读位置任一变化都会变（迟到回执保护依据）", () => {
    const base = prefs({
      previewTabs: [
        { ...tab(), id: "attachment:a1", position: { scrollTop: 120, search: "预算" } },
        { ...tab(), id: "attachment:a2", position: { page: 3 } },
      ],
      activePreviewTabId: "attachment:a1",
      rightOpen: true,
    });
    expect(layoutPrefsFingerprint({ ...base, updatedAt: "2026-11-12T00:00:00.000Z" })).toBe(
      layoutPrefsFingerprint(base),
    );

    expect(layoutPrefsFingerprint({ ...base, leftWidth: 300 })).not.toBe(layoutPrefsFingerprint(base));
    expect(layoutPrefsFingerprint({ ...base, rightOpen: false })).not.toBe(layoutPrefsFingerprint(base));
    expect(layoutPrefsFingerprint({ ...base, previewFull: true })).not.toBe(layoutPrefsFingerprint(base));
    expect(layoutPrefsFingerprint({ ...base, previewTabs: [...base.previewTabs].reverse() })).not.toBe(
      layoutPrefsFingerprint(base),
    );
    expect(layoutPrefsFingerprint({ ...base, activePreviewTabId: "attachment:a2" })).not.toBe(
      layoutPrefsFingerprint(base),
    );
    const moved = cloneLayoutPrefs(base);
    moved.previewTabs[0].position.scrollTop = 121;
    expect(layoutPrefsFingerprint(moved)).not.toBe(layoutPrefsFingerprint(base));
    const searched = cloneLayoutPrefs(base);
    searched.previewTabs[0].position.search = "新搜索";
    expect(layoutPrefsFingerprint(searched)).not.toBe(layoutPrefsFingerprint(base));
  });

  it("#47 还原后的默认偏好仍按窄窗规则推导临时收起/临时全窗口，不写回手动偏好", () => {
    const restored = restoreDefaultLayout(
      prefs({
        leftCollapsed: true,
        rightOpen: true,
        previewFull: true,
        previewTabs: [{ ...tab(), position: { scrollTop: 120 } }],
        activePreviewTabId: "attachment:a1",
      }),
    );
    // 默认 rightOpen=false：预览收起，但标签/当前标签仍在
    const wideCollapsed = resolveConversationLayout(1440, restored);
    expect(wideCollapsed.leftVisible).toBe(true);
    expect(wideCollapsed.rightVisible).toBe(false);
    expect(restored.previewTabs.map((item) => item.id)).toEqual(["attachment:a1"]);
    expect(restored.activePreviewTabId).toBe("attachment:a1");

    // 用户重新打开预览（只改开合，不改手动全窗口）：窄窗放不下分栏 → 临时全窗口
    const reopened = { ...restored, rightOpen: true };
    const narrow = resolveConversationLayout(680, reopened);
    expect(narrow.leftVisible).toBe(false); // 临时收起（推导），不是用户手动收起
    expect(reopened.leftCollapsed).toBe(false);
    expect(narrow.previewFull).toBe(true); // 临时全窗口（推导）
    expect(reopened.previewFull).toBe(false);

    // 宽窗：分栏，原标签与位置仍在
    const split = resolveConversationLayout(1440, reopened);
    expect(split.leftVisible).toBe(true);
    expect(split.rightVisible).toBe(true);
    expect(split.previewFull).toBe(false);
    expect(reopened.previewTabs[0].position.scrollTop).toBe(120);
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
