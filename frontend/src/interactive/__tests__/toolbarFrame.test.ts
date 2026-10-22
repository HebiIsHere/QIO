/**
 * 关系编辑器布局 / 工具栏净空桥的纯函数测试（任务 C）。
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  DEFAULT_TOOLBAR_CLEARANCE,
  LINK_EDITOR_GAP,
  linkEditorLayout,
  publishToolbarClearance,
  readToolbarClearance,
  resetToolbarClearance,
} from "../toolbarFrame";

afterEach(() => {
  resetToolbarClearance();
});

describe("关系编辑器布局：打开时永远停在工具栏上沿之上", () => {
  it("bottom = 实测工具栏净空 + 间距", () => {
    const layout = linkEditorLayout({ clearance: 96, viewportWidth: 1400 });
    expect(layout.bottom).toBe(96 + LINK_EDITOR_GAP);
    expect(layout.narrow).toBe(false);
    expect(layout.left).toBe(16);
    expect(layout.right).toBeNull();
    expect(layout.width).toBe("420px");
  });

  it("没量到工具栏时用兜底净空（不压工具栏）", () => {
    const layout = linkEditorLayout({ clearance: null, viewportWidth: 1400 });
    expect(layout.bottom).toBe(DEFAULT_TOOLBAR_CLEARANCE + LINK_EDITOR_GAP);
  });

  it("窄窗口（<= 800px）：贴底全宽，但仍让出工具栏带", () => {
    const layout = linkEditorLayout({ clearance: 60, viewportWidth: 640 });
    expect(layout.narrow).toBe(true);
    expect(layout.left).toBe(8);
    expect(layout.right).toBe(8);
    expect(layout.width).toBe("auto");
    expect(layout.bottom).toBe(60 + LINK_EDITOR_GAP);
  });

  it("宽窗口但视口较窄时宽度跟着收敛（200..420）", () => {
    expect(linkEditorLayout({ clearance: 72, viewportWidth: 900 }).width).toBe("420px");
    expect(linkEditorLayout({ clearance: 72, viewportWidth: 820 }).width).toBe("420px");
    expect(linkEditorLayout({ clearance: 72, viewportWidth: 820, gap: 0 }).width).toBe("420px");
  });
});

describe("工具栏净空桥：发布 / 读取 / 兜底", () => {
  it("未测量时读兜底值；发布后读到实测值（响应式 ref 被读）", () => {
    expect(readToolbarClearance()).toBe(DEFAULT_TOOLBAR_CLEARANCE);
    publishToolbarClearance(123.6);
    expect(readToolbarClearance()).toBe(124);
  });

  it("非法 / 负数 / null 回到兜底值", () => {
    publishToolbarClearance(80);
    expect(readToolbarClearance()).toBe(80);
    publishToolbarClearance(Number.NaN);
    expect(readToolbarClearance()).toBe(DEFAULT_TOOLBAR_CLEARANCE);
    publishToolbarClearance(-5);
    expect(readToolbarClearance()).toBe(DEFAULT_TOOLBAR_CLEARANCE);
    publishToolbarClearance(null);
    expect(readToolbarClearance()).toBe(DEFAULT_TOOLBAR_CLEARANCE);
  });
});
