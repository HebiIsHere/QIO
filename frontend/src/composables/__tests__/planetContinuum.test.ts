/**
 * Planet 连续体的「离场收尾」（2026-09-22）。
 *
 * 现场：星球开着的时候离开对话页（例如去设置页），`phase` 停在 ready，
 * 入口球的 `handedOff` 永远是 true —— `.dock.handed { opacity: 0; pointer-events: none }`，
 * 球看不见也点不到，而且没有任何路径能把它救回来（用户反馈的「星球无法点动」）。
 */
import { describe, expect, it } from "vitest";
import {
  abandonContinuum,
  advanceTo,
  beginOpen,
  isCurrent,
  planetContinuum,
} from "../planetContinuum";

describe("planetContinuum 离场收尾", () => {
  it("abandonContinuum：阶段回到 idle，入口交回 2D 压缩态", () => {
    const epoch = beginOpen();
    advanceTo(epoch, "ready");
    planetContinuum.ballLive = true;

    abandonContinuum();

    expect(planetContinuum.phase).toBe("idle");
    expect(planetContinuum.ballLive).toBe(false);
    expect(planetContinuum.origin).toBeNull();
    expect(planetContinuum.target).toBeNull();
  });

  it("离场后，被打断的那次转场的续行不能再写阶段（否则 phase 又会卡在非 idle）", () => {
    const epoch = beginOpen();
    advanceTo(epoch, "ready");
    abandonContinuum();

    // 收尾续行（例如收起动画在后台继续跑）拿着旧代号推进阶段
    expect(isCurrent(epoch)).toBe(false);
    expect(advanceTo(epoch, "returning")).toBe(false);
    expect(planetContinuum.phase).toBe("idle");
  });
});
