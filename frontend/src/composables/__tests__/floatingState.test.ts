import { describe, expect, it, beforeEach } from "vitest";
import {
  floatingState,
  loadPositions,
  resetFloatPositions,
  savePositions,
  setHideEnabled,
  FLOAT_STORAGE_KEY,
  type DockId,
} from "../floatingState";

beforeEach(() => {
  localStorage.clear();
  resetFloatPositions();
});

describe("floatingState 共享状态", () => {
  it("初始：三个组件均未贴靠、未隐藏、不允许隐藏", () => {
    expect(floatingState.composer.docked).toBe(false);
    expect(floatingState["planet-dock"].docked).toBe(false);
    expect(floatingState["settings-float"].docked).toBe(false);
    expect(floatingState.composer.hidden).toBe(false);
    expect(floatingState.composer.hideEnabled).toBe(false);
  });

  it("setHideEnabled 写入 hideEnabled 并持久化；关闭时清 hidden", () => {
    floatingState.composer.width = 200; // 已初始化组件才参与持久化
    floatingState.composer.hidden = true;
    setHideEnabled("composer", true);
    expect(floatingState.composer.hideEnabled).toBe(true);
    expect(floatingState.composer.hidden).toBe(true);

    setHideEnabled("composer", false);
    expect(floatingState.composer.hideEnabled).toBe(false);
    expect(floatingState.composer.hidden).toBe(false);

    const raw = JSON.parse(localStorage.getItem(FLOAT_STORAGE_KEY) ?? "{}");
    expect(raw.composer.hideEnabled).toBe(false);
  });

  it("savePositions / loadPositions 往返一致", () => {
    floatingState.composer.width = 200; // 已初始化组件才参与持久化
    floatingState.composer.x = 120;
    floatingState.composer.y = 340;
    floatingState.composer.docked = true;
    floatingState.composer.dockedTo = "bottom";
    savePositions();

    const loaded = loadPositions();
    expect(loaded.composer).toEqual({ x: 120, y: 340, docked: true, dockedTo: "bottom", hideEnabled: false });
  });

  it("loadPositions 对损坏数据静默兜底为空表", () => {
    localStorage.setItem(FLOAT_STORAGE_KEY, "{not json");
    expect(loadPositions()).toEqual({});
    localStorage.setItem(FLOAT_STORAGE_KEY, JSON.stringify({ composer: { x: "bad", y: null } }));
    expect(loadPositions()).toEqual({});
  });

  it("resetFloatPositions 清空状态与 localStorage", () => {
    floatingState["settings-float"].x = 976;
    floatingState["settings-float"].docked = true;
    floatingState["settings-float"].hideEnabled = true;
    savePositions();
    resetFloatPositions();
    for (const id of ["composer", "planet-dock", "settings-float"] as DockId[]) {
      expect(floatingState[id].x).toBe(0);
      expect(floatingState[id].y).toBe(0);
      expect(floatingState[id].docked).toBe(false);
      expect(floatingState[id].dockedTo).toBeNull();
      expect(floatingState[id].hidden).toBe(false);
      expect(floatingState[id].hideEnabled).toBe(false);
    }
    expect(localStorage.getItem(FLOAT_STORAGE_KEY)).toBeNull();
  });
});

