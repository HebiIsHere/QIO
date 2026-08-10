import { describe, expect, it } from "vitest";
import { computeAvoidance, type Avoidance } from "../dockAvoidance";

const VP = { width: 1024, height: 800 };
const ZERO: Avoidance = { top: 0, right: 0, bottom: 0, left: 0 };

describe("computeAvoidance 消息流避让", () => {
  it("贴底：底部让出 height+12", () => {
    expect(
      computeAvoidance({ dockedTo: "bottom", x: 230, y: 706, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, bottom: 102 });
  });
  it("贴右：右侧让出 width+12", () => {
    expect(
      computeAvoidance({ dockedTo: "right", x: 460, y: 350, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, right: 572 });
  });
  it("贴左：左侧让出 width+12", () => {
    expect(
      computeAvoidance({ dockedTo: "left", x: 4, y: 350, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, left: 572 });
  });
  it("贴顶：顶部让出 height+12", () => {
    expect(
      computeAvoidance({ dockedTo: "top", x: 230, y: 4, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, top: 102 });
  });
  it("未贴靠且贴近底部：按最近边估算为底部避让", () => {
    expect(
      computeAvoidance({ dockedTo: null, x: 230, y: 710, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, bottom: 102 });
  });
  it("未贴靠且贴近右侧：按最近边估算为右侧避让", () => {
    expect(
      computeAvoidance({ dockedTo: null, x: 900, y: 350, width: 120, height: 90 }, VP),
    ).toEqual({ ...ZERO, right: 132 });
  });
  it("尺寸未初始化（width/height 为 0）：不避让", () => {
    expect(
      computeAvoidance({ dockedTo: "bottom", x: 0, y: 0, width: 0, height: 0 }, VP),
    ).toEqual(ZERO);
  });
});
