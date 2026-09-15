/**
 * 旋转 → 话题流（spec 第 5 / 8 / 49~50 条）。
 *
 * 用户视觉上是在转一个星球，但产品逻辑上「旋转也是在推动话题流」。
 * 节奏必须慢：拖动量对应展示变化，一次最多前进一格，不能变成快速刷新。
 */
import { describe, expect, it } from "vitest";

import { BrowseFlowDriver } from "../browseFlow";

describe("BrowseFlowDriver", () => {
  it("拖动量不足一步时不流动", () => {
    const driver = new BrowseFlowDriver({ stepRad: 0.4 });

    expect(driver.feed(0.1, 0, true)).toBe(0);
    expect(driver.feed(0.1, 100, true)).toBe(0);
    expect(driver.feed(0.1, 200, true)).toBe(0);
  });

  it("累计到一步时流动一格，方向跟随旋转方向", () => {
    const driver = new BrowseFlowDriver({ stepRad: 0.4 });

    expect(driver.feed(0.2, 0, true)).toBe(0);
    expect(driver.feed(0.2, 100, true)).toBe(1);
    expect(driver.feed(-0.4, 200, true)).toBe(-1);
  });

  it("一次喂入最多走一格（快速甩动不会连跳）", () => {
    const driver = new BrowseFlowDriver({ stepRad: 0.4 });

    expect(driver.feed(5, 0, true)).toBe(1);
    expect(driver.feed(0, 100, true)).toBe(0);
  });

  it("程序性移动（补间）既不流动也不留残量", () => {
    const driver = new BrowseFlowDriver({ stepRad: 0.4 });

    expect(driver.feed(1.2, 0, false)).toBe(0);
    expect(driver.feed(0, 100, true)).toBe(0);
    expect(driver.feed(0.2, 200, true)).toBe(0);
    expect(driver.feed(0.2, 300, true)).toBe(1);
  });

  it("reset 清空累计的拖动量", () => {
    const driver = new BrowseFlowDriver({ stepRad: 0.4 });
    driver.feed(0.3, 0, true);

    driver.reset();

    expect(driver.feed(0.3, 100, true)).toBe(0);
  });
});
