import { describe, expect, it } from "vitest";
import {
  CENTER_MIN,
  LEFT_MAX,
  LEFT_MIN,
  RIGHT_MAX,
  RIGHT_MIN,
  SPLITTER_WIDTH,
  clampLeftWidth,
  clampRightWidth,
  defaultLayoutPrefs,
  sanitizeLayoutPrefs,
  shouldAutoCollapseLeft,
  shouldUseFullPreview,
} from "./layoutPrefs";

describe("布局夹取", () => {
  it("左栏宽度夹在可读范围内", () => {
    expect(clampLeftWidth(10)).toBe(LEFT_MIN);
    expect(clampLeftWidth(9999)).toBe(LEFT_MAX);
    expect(clampLeftWidth(301.6)).toBe(302);
  });

  it("右栏宽度不超过窗口允许的上限", () => {
    expect(clampRightWidth(200, 1440, 292)).toBe(RIGHT_MIN);
    // 1440 窗口、左栏 292：右栏上限同时受「中央最小可读宽度」约束
    expect(clampRightWidth(9999, 1440, 292)).toBe(
      Math.min(RIGHT_MAX, 1440 - 292 - SPLITTER_WIDTH * 2 - CENTER_MIN),
    );
    // 窗口足够宽时才允许到最大宽度
    expect(clampRightWidth(9999, 2560, 292)).toBe(RIGHT_MAX);
    // 1024 窗口且左栏已临时收起（48px）：右栏上限会被压低到刚好保住中央最小宽度
    const narrowed = clampRightWidth(9999, 1024, 48);
    expect(narrowed).toBeGreaterThanOrEqual(RIGHT_MIN);
    expect(1024 - 48 - SPLITTER_WIDTH * 2 - narrowed).toBeGreaterThanOrEqual(CENTER_MIN);
  });
});

describe("窄窗口规则", () => {
  it("1180px 以下才临时收起左栏", () => {
    expect(shouldAutoCollapseLeft(1440)).toBe(false);
    expect(shouldAutoCollapseLeft(1180)).toBe(false);
    expect(shouldAutoCollapseLeft(1024)).toBe(true);
  });

  it("三栏放不下时预览改为全窗口；放得下时不改", () => {
    expect(shouldUseFullPreview(1440, false, 420)).toBe(false);
    // 1024：收起左栏后中央仍 ≥ 最小可读宽度，三栏并排成立
    expect(shouldUseFullPreview(1024, true, 420)).toBe(false);
    // 900：收起左栏仍放不下「中央最小宽度 + 右栏」，改为全窗口
    expect(shouldUseFullPreview(900, true, 420)).toBe(true);
    expect(shouldUseFullPreview(800, true, 420)).toBe(true);
  });
});

describe("偏好的容错恢复", () => {
  it("缺失/损坏输入回落到默认值", () => {
    const defaults = defaultLayoutPrefs();
    expect(sanitizeLayoutPrefs(null)).toEqual(defaults);
    expect(sanitizeLayoutPrefs("bad")).toEqual(defaults);
  });

  it("越界数值被夹取，坏标签被丢弃", () => {
    const prefs = sanitizeLayoutPrefs({
      leftWidth: 9999,
      rightWidth: -5,
      taskTab: "unknown-tab",
      previewTabs: [{ id: "attachment:a@1", target: { id: "attachment:a@1", kind: "pdf", title: "a.pdf", source: "attachment" }, position: { page: 3 } }, { id: "", target: {} }],
      activePreviewTabId: "missing",
    });
    expect(prefs.leftWidth).toBe(LEFT_MAX);
    expect(prefs.rightWidth).toBe(RIGHT_MIN);
    expect(prefs.taskTab).toBe("current");
    expect(prefs.previewTabs).toHaveLength(1);
    expect(prefs.previewTabs[0].position.page).toBe(3);
    expect(prefs.activePreviewTabId).toBe("attachment:a@1");
  });
});
