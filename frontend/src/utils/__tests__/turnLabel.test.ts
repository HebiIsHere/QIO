import { describe, expect, it } from "vitest";
import { turnLabel } from "../turnLabel";

describe("turnLabel", () => {
  it("普通轮次用「第 NN 轮」", () => {
    expect(turnLabel(1, false)).toBe("第 01 轮");
    expect(turnLabel(12, false)).toBe("第 12 轮");
  });

  it("正在运行的那一轮显示「本轮」", () => {
    expect(turnLabel(3, true)).toBe("本轮");
  });

  it("不再出现 TURN / NOW 这类英文标签", () => {
    for (const label of [turnLabel(1, false), turnLabel(1, true)]) {
      expect(label).not.toMatch(/TURN|NOW/);
    }
  });
});
