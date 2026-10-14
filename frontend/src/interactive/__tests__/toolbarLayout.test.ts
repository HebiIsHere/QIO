/**
 * 功能栏按「实际可用宽度」分档的守卫（2026-10-15 布局重构）。
 *
 * 背景：旧实现用视口媒体查询决定行列关系，窗口宽但容器被挤窄时不生效 ——
 * 提交区的长说明被压成极窄竖列、功能栏被撑高。分档必须只看功能栏自己的可用宽度。
 * 这里钉住三件事：两档参考宽度（约 764px / 约 1178px）落点正确、滞回不抖、量不到不误判。
 */
import { describe, expect, it } from "vitest";
import {
  TOOLBAR_BAND_HYSTERESIS,
  TOOLBAR_MID_MIN,
  TOOLBAR_WIDE_MIN,
  toolbarBand,
} from "../toolbarLayout";

describe("toolbarBand：按可用宽度分档", () => {
  it("参考宽度 1178px 落 wide，764px 落 mid（正是用户截图里的两档）", () => {
    expect(toolbarBand({ available: 1178, previous: "wide" })).toBe("wide");
    expect(toolbarBand({ available: 1178, previous: "mid" })).toBe("wide");
    expect(toolbarBand({ available: 764, previous: "wide" })).toBe("mid");
  });

  it("阈值两侧：wide 下界之下立即降档，mid 下界之下变 narrow", () => {
    expect(toolbarBand({ available: TOOLBAR_WIDE_MIN, previous: "wide" })).toBe("wide");
    expect(toolbarBand({ available: TOOLBAR_WIDE_MIN - 1, previous: "wide" })).toBe("mid");
    expect(toolbarBand({ available: TOOLBAR_MID_MIN, previous: "mid" })).toBe("mid");
    expect(toolbarBand({ available: TOOLBAR_MID_MIN - 1, previous: "mid" })).toBe("narrow");
  });

  it("升档带滞回：阈值附近来回改动不会跳档", () => {
    const justAbove = TOOLBAR_WIDE_MIN + Math.floor(TOOLBAR_BAND_HYSTERESIS / 2);
    expect(toolbarBand({ available: justAbove, previous: "mid" })).toBe("mid");
    expect(toolbarBand({ available: TOOLBAR_WIDE_MIN + TOOLBAR_BAND_HYSTERESIS, previous: "mid" })).toBe("wide");
    expect(toolbarBand({ available: TOOLBAR_MID_MIN + 1, previous: "narrow" })).toBe("narrow");
    expect(toolbarBand({ available: TOOLBAR_MID_MIN + TOOLBAR_BAND_HYSTERESIS, previous: "narrow" })).toBe("mid");
    /*
      抖动幅度小于滞回余量时不换档：
      wide 的降档线是 900、回档线是 940；在 880~910 之间来回（幅度 30 < 40）只会在
      第一次降到 mid，之后一直停在 mid —— 不会每帧换一次档（窗口连续缩放时不跳动）。
    */
    let band = toolbarBand({ available: 1000, previous: "wide" });
    expect(band).toBe("wide");
    const seen = new Set<string>();
    for (const width of [880, 910, 880, 910, 880, 910]) {
      band = toolbarBand({ available: width, previous: band as never });
      seen.add(band);
    }
    expect([...seen]).toEqual(["mid"]);
  });

  it("量不到（0 / NaN，例如 jsdom 或首帧）时保持上一档，绝不判成最窄", () => {
    expect(toolbarBand({ available: 0, previous: "wide" })).toBe("wide");
    expect(toolbarBand({ available: Number.NaN, previous: "mid" })).toBe("mid");
    expect(toolbarBand({ available: -10, previous: "narrow" })).toBe("narrow");
    expect(toolbarBand({ available: 0 })).toBe("wide");
  });

  it("从最宽一路缩到最窄，再放大回去，得到的档位序列稳定", () => {
    const widths = [1200, 1000, 900, 899, 700, 600, 560, 559, 480, 400];
    const shrunk: string[] = [];
    let band = toolbarBand({ available: widths[0], previous: "wide" });
    for (const width of widths) {
      band = toolbarBand({ available: width, previous: band as never });
      shrunk.push(band);
    }
    expect(shrunk).toEqual(["wide", "wide", "wide", "mid", "mid", "mid", "mid", "narrow", "narrow", "narrow"]);
    const grown: string[] = [];
    for (const width of [...widths].reverse()) {
      band = toolbarBand({ available: width, previous: band as never });
      grown.push(band);
    }
    expect(grown[grown.length - 1]).toBe("wide");
  });
});
