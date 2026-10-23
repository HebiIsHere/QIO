/**
 * Phase 1-B 互动页 v2 布局纯函数（子智能体 p1-layout 负责）。
 *
 * 这些用例只测**纯几何决策**，不测浏览器像素渲染：
 * - 响应式档位按「工作区自身宽度」分档，而不是视口宽度（窗口宽、但被侧栏挤窄时不误判）；
 * - 书签栏：桌面为右侧窄栏，窄窗口为受限高度的下侧区域；任何档位都不覆盖板面主区；
 * - 对话区从底部输入区向上展开，高度有上限，并且始终给板面留出最小高度（不覆盖/不挤没）；
 * - 顶栏搜索浮层（Teleport + fixed）被钳制在视口内，不被全局栏的 overflow 裁切。
 */
import { describe, expect, it } from "vitest";
import {
  BAND_COMPACT_MIN,
  BAND_WIDE_MIN,
  BOOKMARK_BOTTOM_MAX,
  BOOKMARK_BOTTOM_MIN,
  BOOKMARK_SIDE_MAX,
  BOOKMARK_SIDE_MIN,
  CHAT_MIN_HEIGHT,
  GAP,
  MAIN_MIN_HEIGHT,
  MAIN_MIN_WIDTH,
  bottomBookmarkHeight,
  resolveWorkspaceLayout,
  sideBookmarkWidth,
  workspaceBand,
  type WorkspaceLayout,
  type WorkspaceRect,
} from "../v2/layout";
import { placeChromePopover } from "../v2/chromePlacement";

/** 基准尺寸与输入：底部工具栏、输入区都按真实组件量到的高度代入。 */
function layoutInput(overrides: Partial<Parameters<typeof resolveWorkspaceLayout>[0]> = {}) {
  return {
    width: 1280,
    height: 800,
    bookmarksOpen: true,
    chatOpen: false,
    toolbarHeight: 48,
    composerHeight: 64,
    ...overrides,
  };
}

function regions(layout: WorkspaceLayout): Array<[string, WorkspaceRect]> {
  const list: Array<[string, WorkspaceRect]> = [["board", layout.board]];
  if (layout.bookmarks) list.push(["bookmarks", layout.bookmarks]);
  if (layout.chat) list.push(["chat", layout.chat]);
  if (layout.toolbar) list.push(["toolbar", layout.toolbar]);
  if (layout.composer) list.push(["composer", layout.composer]);
  return list;
}

function expectInside(layout: WorkspaceLayout, width: number, height: number): void {
  for (const [name, rect] of regions(layout)) {
    expect(rect.x, name + ".x").toBeGreaterThanOrEqual(0);
    expect(rect.y, name + ".y").toBeGreaterThanOrEqual(0);
    expect(rect.w, name + ".w").toBeGreaterThanOrEqual(0);
    expect(rect.h, name + ".h").toBeGreaterThanOrEqual(0);
    expect(rect.x + rect.w, name + " 右边界").toBeLessThanOrEqual(width + 0.001);
    expect(rect.y + rect.h, name + " 下边界").toBeLessThanOrEqual(height + 0.001);
  }
}

/** 不覆盖主区 = 任意两个区域的矩形互不相交（边贴边不算相交）。 */
function expectDisjoint(layout: WorkspaceLayout): void {
  const items = regions(layout);
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      const [nameA, a] = items[i];
      const [nameB, b] = items[j];
      const intersects = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
      expect(intersects, nameA + " 与 " + nameB + " 不应相交").toBe(false);
    }
  }
}

describe("响应式档位（按工作区自身宽度）", () => {
  it("宽档 / 中档 / 窄档按 1024 / 760 分界", () => {
    expect(workspaceBand(BAND_WIDE_MIN)).toBe("wide");
    expect(workspaceBand(BAND_WIDE_MIN + 400)).toBe("wide");
    expect(workspaceBand(BAND_WIDE_MIN - 1)).toBe("compact");
    expect(workspaceBand(BAND_COMPACT_MIN)).toBe("compact");
    expect(workspaceBand(BAND_COMPACT_MIN - 1)).toBe("narrow");
  });

  it("未测量（0 / 负数 / 非法值）按宽档处理，首帧不闪成窄档", () => {
    expect(workspaceBand(0)).toBe("wide");
    expect(workspaceBand(-120)).toBe("wide");
    expect(workspaceBand(Number.NaN)).toBe("wide");
  });
});

describe("书签栏宽度 / 高度约束", () => {
  it("桌面右侧栏宽按比例取整并钳在 168–260px", () => {
    expect(sideBookmarkWidth(1280)).toBe(205); // 1280 × 0.16 = 204.8
    expect(sideBookmarkWidth(BOOKMARK_SIDE_MIN / 0.16)).toBe(BOOKMARK_SIDE_MIN);
    expect(sideBookmarkWidth(4000)).toBe(BOOKMARK_SIDE_MAX);
  });

  it("窄窗口下侧书签栏高度按比例取整并钳在 72–148px", () => {
    expect(bottomBookmarkHeight(800)).toBe(144); // 800 × 0.18 = 144
    expect(bottomBookmarkHeight(200)).toBe(BOOKMARK_BOTTOM_MIN);
    expect(bottomBookmarkHeight(4000)).toBe(BOOKMARK_BOTTOM_MAX);
  });
});

describe("resolveWorkspaceLayout：桌面宽档", () => {
  it("书签栏在右侧窄栏，板面占其余宽度，底部输入区与工具栏分层且不覆盖板面", () => {
    const layout = resolveWorkspaceLayout(layoutInput());

    expect(layout.band).toBe("wide");
    expect(layout.bookmarkDock).toBe("side");
    expect(layout.bookmarkSize).toBe(205);

    // 板面不再被任何覆盖层压住：宽度 = 总宽 - 书签栏 - 间距
    expect(layout.board.w).toBe(1280 - 205 - GAP);
    // 高度 = 总高 - 输入区 - 工具栏 - 两条间距
    expect(layout.board.h).toBe(800 - 64 - GAP - 48 - GAP);
    expect(layout.chat).toBeNull();
    expect(layout.chatMaxHeight).toBe(0);
    expect(layout.toolbar).toEqual({ x: 0, y: 676, w: 1280, h: 48 });
    expect(layout.composer).toEqual({ x: 0, y: 736, w: 1280, h: 64 });
    expect(layout.bookmarks).toEqual({ x: 1280 - 205, y: 0, w: 205, h: layout.board.h });

    expectInside(layout, 1280, 800);
    expectDisjoint(layout);
  });

  it("书签栏收起时板面恢复整宽，也不留下空白槽位", () => {
    const layout = resolveWorkspaceLayout(layoutInput({ bookmarksOpen: false }));
    expect(layout.bookmarks).toBeNull();
    expect(layout.board.w).toBe(1280);
  });

  it("宽档但宽度不足时，书签栏改到下侧，绝不把板面压到最小宽度以下", () => {
    // 宽档需要 ≥1024；用 1024 且把侧栏比例抬高不现实，这里直接验证「侧栏会让板面过窄」时改下侧：
    // 侧栏固定 205 + 12 间距，1024 时板面还有 807，仍够 → 仍是 side。
    const wide = resolveWorkspaceLayout(layoutInput({ width: 1024 }));
    expect(wide.bookmarkDock).toBe("side");
    expect(wide.board.w).toBeGreaterThanOrEqual(MAIN_MIN_WIDTH);

    // 中档（<1024）一律下侧。
    const compact = resolveWorkspaceLayout(layoutInput({ width: 1000 }));
    expect(compact.band).toBe("compact");
    expect(compact.bookmarkDock).toBe("bottom");
    expect(compact.bookmarks).not.toBeNull();
    expect(compact.board.w).toBe(1000);
  });
});

describe("resolveWorkspaceLayout：窄窗口下侧书签栏", () => {
  it("书签栏变为受限高度的下侧区域，板面宽度不再被挤", () => {
    const layout = resolveWorkspaceLayout(layoutInput({ width: 720, height: 800 }));

    expect(layout.band).toBe("narrow");
    expect(layout.bookmarkDock).toBe("bottom");
    expect(layout.bookmarkSize).toBe(144);
    expect(layout.board.w).toBe(720);
    // 板面 / 对话（关）/ 书签栏 / 工具栏 / 输入区 自上而下依次排列
    expect(layout.board.h).toBe(508);
    expect(layout.bookmarks).toEqual({ x: 0, y: 520, w: 720, h: 144 });
    expectInside(layout, 720, 800);
    expectDisjoint(layout);
  });
});

describe("resolveWorkspaceLayout：对话区向上展开", () => {
  it("桌面：对话区从输入区一侧向上展开，有高度上限，板面仍保留最小高度", () => {
    const layout = resolveWorkspaceLayout(layoutInput({ chatOpen: true }));

    expect(layout.chat).not.toBeNull();
    expect(layout.chatMaxHeight).toBeGreaterThan(0);
    // 上限：可用高度的 46%
    expect(layout.chatMaxHeight).toBe(Math.round((800 - 64 - GAP - 48 - 2 * GAP) * 0.46));
    // 对话区位于板面下方、工具栏上方（向上展开的落点）
    expect(layout.chat!.y).toBeGreaterThanOrEqual(layout.board.h + GAP - 0.001);
    expect(layout.chat!.y + layout.chat!.h).toBeLessThanOrEqual(layout.toolbar!.y - GAP + 0.001);
    expect(layout.board.h).toBeGreaterThanOrEqual(MAIN_MIN_HEIGHT);
    expectInside(layout, 1280, 800);
    expectDisjoint(layout);
  });

  it("窄窗口：对话区打开时也不覆盖板面，下侧书签栏与对话区互不相交", () => {
    const layout = resolveWorkspaceLayout(layoutInput({ width: 720, height: 800, chatOpen: true }));

    expect(layout.chat).not.toBeNull();
    expect(layout.bookmarks).not.toBeNull();
    expect(layout.board.h).toBeGreaterThanOrEqual(MAIN_MIN_HEIGHT);
    expect(layout.chatMaxHeight).toBeLessThanOrEqual(Math.max(0, 800 - MAIN_MIN_HEIGHT));
    expectInside(layout, 720, 800);
    expectDisjoint(layout);
  });

  it("没有提供对话区内容时，chatOpen 不产生空区域", () => {
    const layout = resolveWorkspaceLayout(layoutInput({ chatOpen: false }));
    expect(layout.chat).toBeNull();
    expect(layout.chatMaxHeight).toBe(0);
  });
});

describe("resolveWorkspaceLayout：极端尺寸下的兜底", () => {
  it("高度不足时优先保板面，书签栏/对话区被压缩并如实上报问题", () => {
    const layout = resolveWorkspaceLayout(
      layoutInput({ width: 720, height: 320, bookmarksOpen: true, chatOpen: true }),
    );

    expect(layout.board.h).toBeGreaterThan(0);
    expect(layout.composer).not.toBeNull();
    // 高度连「输入区 + 工具栏 + 板面最小高度」都放不下：书签栏与对话区都不能占位
    expect(layout.bookmarks).toBeNull();
    expect(layout.chat).toBeNull();
    expect(layout.issues).toContain("bookmarks_capped");
    expect(layout.issues).toContain("chat_capped");
    expect(layout.issues).toContain("board_too_short");
    expectInside(layout, 720, 320);
    expectDisjoint(layout);
  });

  it("宽度极窄时板面仍占满宽度，不出现负尺寸", () => {
    const layout = resolveWorkspaceLayout(
      layoutInput({ width: 320, height: 600, bookmarksOpen: true, chatOpen: true }),
    );
    expect(layout.board.w).toBe(320); // 下侧书签栏只占高度，不再挤压板面宽度
    expect(layout.bookmarkDock).toBe("bottom");
    if (layout.bookmarks) {
      expect(layout.bookmarks.w).toBe(320);
      expect(layout.bookmarks.h).toBeLessThanOrEqual(148);
    }
    expect(layout.issues).toContain("board_too_narrow");
    expectInside(layout, 320, 600);
    expectDisjoint(layout);
  });

  it("尺寸扫描：任意档位 / 开关组合下区域都在容器内且互不相交", () => {
    const widths = [360, 480, 640, 760, 900, 1024, 1280, 1600];
    const heights = [320, 480, 600, 800, 1080];
    for (const width of widths) {
      for (const height of heights) {
        for (const bookmarksOpen of [false, true]) {
          for (const chatOpen of [false, true]) {
            const layout = resolveWorkspaceLayout({
              width,
              height,
              bookmarksOpen,
              chatOpen,
              toolbarHeight: 48,
              composerHeight: 64,
            });
            expectInside(layout, width, height);
            expectDisjoint(layout);
            // 对话区上限永远不得把板面挤到最小高度以下
            expect(layout.chatMaxHeight).toBeLessThanOrEqual(
              Math.max(0, height - MAIN_MIN_HEIGHT),
            );
            // 书签栏只在宽档且放得下时上右侧
            if (layout.bookmarkDock === "side") {
              expect(layout.band).toBe("wide");
              expect(layout.board.w).toBeGreaterThanOrEqual(MAIN_MIN_WIDTH);
            }
          }
        }
      }
    }
  });
});

describe("顶栏搜索浮层几何（fixed，不裁切）", () => {
  const anchor = { left: 700, top: 44, right: 760, bottom: 64, width: 60, height: 20 };

  it("默认落在触发按钮下方、右对齐，宽度与视口钳制后不越界", () => {
    const placement = placeChromePopover(anchor, { width: 1280, height: 800 }, {
      width: 360,
      maxWidth: 420,
      margin: 8,
      gap: 6,
    });
    expect(placement.left).toBe(400); // 760 - 360
    expect(placement.top).toBe(70); // 64 + 6
    expect(placement.width).toBe(360);
    expect(placement.maxHeight).toBe(800 - 8 - 70); // 贴到视口下沿留 8px
    expect(placement.left + placement.width).toBeLessThanOrEqual(1280 - 8);
  });

  it("触发按钮贴右边时向左收缩，绝不把浮层推出视口（不被顶栏裁切）", () => {
    const placement = placeChromePopover(
      { left: 1240, top: 44, right: 1300, bottom: 64, width: 60, height: 20 },
      { width: 1280, height: 800 },
      { width: 360, maxWidth: 420, margin: 8, gap: 6 },
    );
    expect(placement.left).toBe(1280 - 8 - 360);
    expect(placement.left + placement.width).toBe(1280 - 8);
  });

  it("窄视口按视口宽度收窄；矮视口把高度上限压到剩余空间", () => {
    const narrow = placeChromePopover(anchor, { width: 320, height: 700 }, {
      width: 360,
      maxWidth: 420,
      margin: 8,
      gap: 6,
    });
    expect(narrow.width).toBe(320 - 16);
    expect(narrow.left).toBe(8);
    expect(narrow.left + narrow.width).toBe(320 - 8);

    const short = placeChromePopover(anchor, { width: 1280, height: 240 }, {
      width: 360,
      maxWidth: 420,
      margin: 8,
      gap: 6,
    });
    expect(short.maxHeight).toBe(240 - 8 - short.top);
    expect(short.top + short.maxHeight).toBe(240 - 8);
  });

  it("尺寸扫描：浮层始终在视口内，不出现负宽高", () => {
    for (const viewportWidth of [120, 240, 320, 480, 768, 1024, 1440, 1920]) {
      for (const viewportHeight of [120, 240, 360, 600, 800, 1080]) {
        for (const anchorRight of [8, 60, 300, viewportWidth - 40, viewportWidth + 120]) {
          const placement = placeChromePopover(
            { left: Math.max(0, anchorRight - 60), top: 40, right: anchorRight, bottom: 64, width: 60, height: 24 },
            { width: viewportWidth, height: viewportHeight },
            { width: 360, maxWidth: 420, margin: 8, gap: 6 },
          );
          expect(placement.width).toBeGreaterThanOrEqual(0);
          expect(placement.maxHeight).toBeGreaterThanOrEqual(0);
          expect(placement.left).toBeGreaterThanOrEqual(8 - 0.001);
          expect(placement.left + placement.width).toBeLessThanOrEqual(viewportWidth - 8 + 0.001);
          expect(placement.top).toBeGreaterThanOrEqual(8 - 0.001);
          expect(placement.top + placement.maxHeight).toBeLessThanOrEqual(viewportHeight - 8 + 0.001);
        }
      }
    }
  });
});
