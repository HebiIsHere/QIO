/**
 * 功能栏按「实际可用宽度」分档的守卫（2026-10-15 布局重构）。
 *
 * 背景：旧实现用视口媒体查询决定行列关系，窗口宽但容器被挤窄时不生效 ——
 * 提交区的长说明被压成极窄竖列、功能栏被撑高。分档必须只看功能栏自己的可用宽度。
 * 这里钉住三件事：两档参考宽度（约 764px / 约 1178px）落点正确、滞回不抖、量不到不误判。
 */
import { describe, expect, it } from "vitest";
import {
  RAIL_TOOLBAR_FULL_MIN,
  RAIL_TOOLBAR_HYSTERESIS,
  RAIL_TOOLBAR_ICONS_MIN,
  TOOLBAR_BAND_HYSTERESIS,
  TOOLBAR_MID_MIN,
  TOOLBAR_WIDE_MIN,
  railToolbarMode,
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

describe("railToolbarMode：操作行按自有宽度三档降级（2026-11-17 互动底部操作区）", () => {
  /*
    实测依据（系统 Edge headless 1000×600 + 临时 lab 页挂真实组件，真实 tokens/CSS/字体，
    读 getBoundingClientRect；lab 页测完删除，产品代码零改动）：
    - full 内容宽 303.75px（最长提交态「用同一身份重试」326px）；
    - icons 内容宽 269.25px（最长态 291.5px）；more 内容宽 161.25px；
    - 父组件槽宽（InteractiveConversation）= max(96, floor(面板宽 / 2 - 62))：
      面板 860/776/696 → 槽 368/326/286，对应 1000×600 / 800×600 / 720×600。
    所以 full 下界取 344（= 最大槽宽 368 - 滞回 24，保证 1000×600 能进 full、且缩窄后
    回到最大宽度能恢复 full），icons 下界取 300（最长态 291.5px 刚好放得下）。
    证据：_lead-logs/interactive-action/toolbar-calibration/rail-calibration.json + 三档截图。
  */
  it("父组件实机落点：368 → full；326 → icons；286 → more", () => {
    expect(RAIL_TOOLBAR_FULL_MIN).toBe(344);
    expect(RAIL_TOOLBAR_ICONS_MIN).toBe(300);
    expect(RAIL_TOOLBAR_HYSTERESIS).toBe(24);
    expect(railToolbarMode({ available: 368, previous: "full" })).toBe("full");
    expect(railToolbarMode({ available: 326, previous: "full" })).toBe("icons");
    expect(railToolbarMode({ available: 286, previous: "icons" })).toBe("more");
  });

  it("阈值两侧：344 仍是 full、343 降 icons；300 仍是 icons、299 变 more", () => {
    expect(railToolbarMode({ available: RAIL_TOOLBAR_FULL_MIN, previous: "full" })).toBe("full");
    expect(railToolbarMode({ available: RAIL_TOOLBAR_FULL_MIN - 1, previous: "full" })).toBe("icons");
    expect(railToolbarMode({ available: RAIL_TOOLBAR_ICONS_MIN, previous: "icons" })).toBe("icons");
    expect(railToolbarMode({ available: RAIL_TOOLBAR_ICONS_MIN - 1, previous: "icons" })).toBe("more");
  });

  it("降级顺序固定：先省文字（icons），再收工具（more）；宽度跳变可直接到底", () => {
    expect(railToolbarMode({ available: 340, previous: "full" })).toBe("icons");
    expect(railToolbarMode({ available: 290, previous: "icons" })).toBe("more");
    expect(railToolbarMode({ available: 240, previous: "full" })).toBe("more");
  });

  it("升档带 24px 滞回：icons 回到 368 才恢复 full，more 回到 324 才回 icons", () => {
    // 344 是 full 下界，但 icons → full 还差滞回：367 仍 icons
    expect(railToolbarMode({ available: 367, previous: "icons" })).toBe("icons");
    expect(railToolbarMode({ available: RAIL_TOOLBAR_FULL_MIN + RAIL_TOOLBAR_HYSTERESIS, previous: "icons" })).toBe("full");
    expect(railToolbarMode({ available: RAIL_TOOLBAR_ICONS_MIN + RAIL_TOOLBAR_HYSTERESIS - 1, previous: "more" })).toBe("more");
    expect(railToolbarMode({ available: RAIL_TOOLBAR_ICONS_MIN + RAIL_TOOLBAR_HYSTERESIS, previous: "more" })).toBe("icons");
    // full ↔ icons 边界附近抖动（幅度 12 < 24）：第一次降档后一直停在 icons
    let mode = railToolbarMode({ available: 500, previous: "full" });
    const seen = new Set<string>();
    for (const width of [340, 352, 340, 352, 340, 352]) {
      mode = railToolbarMode({ available: width, previous: mode });
      seen.add(mode);
    }
    expect([...seen]).toEqual(["icons"]);
  });

  it("量不到（0 / NaN / 负，jsdom 或首帧）时保持上一档，初始 full", () => {
    expect(railToolbarMode({ available: 0, previous: "icons" })).toBe("icons");
    expect(railToolbarMode({ available: Number.NaN, previous: "more" })).toBe("more");
    expect(railToolbarMode({ available: -10, previous: "more" })).toBe("more");
    expect(railToolbarMode({ available: 0 })).toBe("full");
  });

  it("一路缩到窄再放大回去，档位序列稳定（最大槽宽 368 下能恢复 full）", () => {
    const widths = [900, 344, 343, 300, 299, 240];
    let mode = railToolbarMode({ available: 900, previous: "full" });
    const shrunk: string[] = [];
    for (const width of widths) {
      mode = railToolbarMode({ available: width, previous: mode });
      shrunk.push(mode);
    }
    expect(shrunk).toEqual(["full", "full", "icons", "icons", "more", "more"]);
    const grown: string[] = [];
    for (const width of [...widths].reverse()) {
      mode = railToolbarMode({ available: width, previous: mode });
      grown.push(mode);
    }
    // 回宽带滞回：240/299/300 仍是 more，343/344 先回 icons，900 才回 full
    expect(grown).toEqual(["more", "more", "more", "icons", "icons", "full"]);
  });
});
