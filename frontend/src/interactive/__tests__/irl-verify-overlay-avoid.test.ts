/**
 * IRL-E 独立复核 · widestFreeBand（对话窗口 ↔ 审批面板的让开几何）对抗式用例。
 *
 * 与 Lead 的 overlayAvoid.test.ts 换角度：不逐个复述同样样例，而是
 * 1) 障碍重叠 / 包含 / 相同；gap 大于带宽；带为空 / 反转；障碍贴带边与带外；
 * 2) vertical 相交判定的成对边界（刚好贴住 vs 进入 1px）；
 * 3) 属性式（120 组随机）：返回值在带内、与所有相交障碍的扩展区间不相交，
 *    且宽度不劣于「1px 网格独立参考」量出的最宽自由段。
 */
import { describe, expect, it } from "vitest";
import { widestFreeBand, type Band, type RectLike } from "../overlayAvoid";

function makeRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function note(list: string[], message: string): void {
  if (list.length < 6) list.push(message);
}

const rect = (x: number, w: number, y = 400, h = 400): RectLike => ({ x, y, w, h });
const width = (band: Band | null): number => (band ? band.right - band.left : 0);
const show = (band: Band | null): string => (band ? "[" + band.left + "," + band.right + "]" : "null");

/** 独立参考：障碍扩展区间裁剪到带内，用 1px 网格数最长的自由点连续段（点数 ≈ 宽度 + 1）。 */
function referenceFreePoints(
  full: Band,
  obstacles: readonly RectLike[],
  gap: number,
  vertical?: { top: number; bottom: number },
): number {
  const lo = Math.min(full.left, full.right);
  const hi = Math.max(full.left, full.right);
  const blocked: Band[] = [];
  for (const item of obstacles) {
    if (vertical && !(item.y < vertical.bottom && item.y + item.h > vertical.top)) continue;
    const left = item.x - gap;
    const right = item.x + item.w + gap;
    if (right <= lo || left >= hi) continue;
    blocked.push({ left: Math.max(left, lo), right: Math.min(right, hi) });
  }
  let best = 0;
  let run = 0;
  for (let x = Math.ceil(lo); x <= Math.floor(hi); x += 1) {
    const inside = blocked.some((item) => x > item.left && x < item.right);
    if (inside) {
      run = 0;
    } else {
      run += 1;
      if (run > best) best = run;
    }
  }
  return best;
}

describe("widestFreeBand：重叠 / 包含 / 相同的障碍", () => {
  const FULL: Band = { left: 0, right: 600 };

  it("一个障碍完全包含另一个：仍然取障碍右侧的空隙", () => {
    const band = widestFreeBand(FULL, [rect(100, 200), rect(150, 100)], 0);
    expect(band).toEqual({ left: 300, right: 600 });
  });

  it("两个障碍部分重叠：合并之后取右侧空隙", () => {
    const band = widestFreeBand(FULL, [rect(100, 100), rect(180, 120)], 0);
    expect(band).toEqual({ left: 300, right: 600 });
  });

  it("两个障碍完全相同：不会因为重复而把光标推错位置", () => {
    const band = widestFreeBand(FULL, [rect(100, 100), rect(100, 100)], 0);
    expect(band).toEqual({ left: 200, right: 600 });
  });

  it("乱序传入与正序传入结果一致（内部会排序）", () => {
    const shuffled = widestFreeBand(FULL, [rect(400, 60), rect(100, 50), rect(250, 40)], 8);
    const sorted = widestFreeBand(FULL, [rect(100, 50), rect(250, 40), rect(400, 60)], 8);
    expect(shuffled).toEqual(sorted);
  });

  it("等宽时取更左的那条（确定性）", () => {
    const band = widestFreeBand(FULL, [rect(295, 10)], 0);
    expect(width(band)).toBe(295);
    expect(band!.left).toBe(0);
  });
});

describe("widestFreeBand：gap 与带本身退化", () => {
  it("gap 大于带宽：扩展后的障碍把整条带吃光 → null", () => {
    expect(widestFreeBand({ left: 0, right: 100 }, [rect(40, 20, 400, 200)], 100)).toBeNull();
  });

  it("只剩下 1px 缝也算有位置（gap 49 让障碍扩展后从 x=1 开始）", () => {
    const band = widestFreeBand({ left: 0, right: 100 }, [rect(50, 10, 400, 200)], 49);
    expect(band).toEqual({ left: 0, right: 1 });
    expect(width(band)).toBe(1);
  });

  it("修复后（F-G2）：带为空（left === right）返回 null，而不是一条零宽带", () => {
    expect(widestFreeBand({ left: 100, right: 100 }, [], 12)).toBeNull();
    expect(widestFreeBand({ left: 100, right: 100 }, [rect(0, 50)], 12)).toBeNull();
  });

  it("修复后（F-G2，另一组输入）：反向带（left > right）无论有没有障碍都返回 null", () => {
    expect(widestFreeBand({ left: 100, right: 0 }, [], 0)).toBeNull();
    expect(widestFreeBand({ left: 500, right: 200 }, [rect(300, 100)], 8)).toBeNull();
    expect(widestFreeBand({ left: 100, right: 99.5 }, [], 0)).toBeNull();
  });

  it("回归（修复不能误伤正常带）：非空带无障碍时返回整条带，带内障碍照常让开", () => {
    expect(widestFreeBand({ left: 16, right: 1424 }, [], 12)).toEqual({ left: 16, right: 1424 });
    // 障碍在 [40,50)：左空 40、右空 50 → 取更宽的右侧
    expect(widestFreeBand({ left: 0, right: 100 }, [rect(40, 10)], 0)).toEqual({ left: 50, right: 100 });
  });
});

describe("widestFreeBand：障碍相对带的位置", () => {
  const FULL: Band = { left: 0, right: 100 };

  it("障碍整体在带左侧：不吃掉任何空间", () => {
    expect(widestFreeBand(FULL, [rect(-200, 150)], 0)).toEqual(FULL);
  });

  it("障碍整体在带右侧：不吃掉任何空间", () => {
    expect(widestFreeBand(FULL, [rect(150, 50)], 0)).toEqual(FULL);
  });

  it("障碍右边界正好等于带左边界：不吃掉任何空间", () => {
    expect(widestFreeBand(FULL, [rect(-50, 50)], 0)).toEqual(FULL);
  });

  it("障碍左边界正好等于带右边界：不吃掉任何空间", () => {
    expect(widestFreeBand(FULL, [rect(100, 50)], 0)).toEqual(FULL);
  });

  it("障碍擦进带 1px：那条 1px 被吃掉，剩余 99", () => {
    expect(widestFreeBand(FULL, [rect(-50, 51)], 0)).toEqual({ left: 1, right: 100 });
  });
});

describe("widestFreeBand：vertical 相交判定的成对边界", () => {
  const FULL: Band = { left: 0, right: 100 };
  const VERTICAL = { top: 100, bottom: 200 };

  it("障碍底边正好等于区间上沿：不算相交 → 整条带可用", () => {
    expect(widestFreeBand(FULL, [rect(40, 20, 0, 100)], 0, VERTICAL)).toEqual(FULL);
  });

  it("障碍底边进入区间 1px：算相交 → 让开（等宽两侧取更左）", () => {
    expect(widestFreeBand(FULL, [rect(40, 20, 0, 101)], 0, VERTICAL)).toEqual({ left: 0, right: 40 });
  });

  it("障碍上沿正好等于区间下沿：不算相交 → 整条带可用", () => {
    expect(widestFreeBand(FULL, [rect(40, 20, 200, 50)], 0, VERTICAL)).toEqual(FULL);
  });

  it("障碍上沿进入区间 1px：算相交 → 让开（等宽两侧取更左）", () => {
    expect(widestFreeBand(FULL, [rect(40, 20, 199, 50)], 0, VERTICAL)).toEqual({ left: 0, right: 40 });
  });
});

describe("widestFreeBand：属性式（120 组随机 + 独立网格参考）", () => {
  it("返回值在带内、不与相交障碍的扩展区间相交，且宽度不劣于独立参考 - 2px", () => {
    const rand = makeRandom(20261013);
    const bad: string[] = [];
    for (let i = 0; i < 120; i += 1) {
      const lo = Math.round((rand() - 0.3) * 300);
      const full: Band = { left: lo, right: lo + 120 + Math.round(rand() * 480) };
      const gap = Math.round(rand() * 40);
      const useVertical = rand() < 0.5;
      const vertical = useVertical
        ? { top: Math.round(rand() * 400), bottom: 0 }
        : undefined;
      if (vertical) vertical.bottom = vertical.top + 40 + Math.round(rand() * 300);
      const obstacles: RectLike[] = [];
      const count = 1 + Math.round(rand() * 5);
      for (let j = 0; j < count; j += 1) {
        obstacles.push({
          x: full.left - 60 + Math.round(rand() * (full.right - full.left + 120)),
          y: Math.round(rand() * 500),
          w: Math.round(rand() * 200),
          h: Math.round(rand() * 300),
        });
      }
      const band = widestFreeBand(full, obstacles, gap, vertical);
      const tag = show(full) + " gap=" + gap + " -> " + show(band);
      const reference = referenceFreePoints(full, obstacles, gap, vertical);
      if (band) {
        if (band.left < full.left - 1e-9 || band.right > full.right + 1e-9) note(bad, "超出带范围 " + tag);
        for (const item of obstacles) {
          if (vertical && !(item.y < vertical.bottom && item.y + item.h > vertical.top)) continue;
          const left = item.x - gap;
          const right = item.x + item.w + gap;
          if (right <= full.left || left >= full.right) continue;
          if (band.right > left + 1e-9 && band.left < right - 1e-9) note(bad, "与障碍区间相交 " + tag);
        }
        if (band.right - band.left < reference - 2) note(bad, "比独立参考窄（参考点数 " + reference + "） " + tag);
      } else if (reference > 2) {
        note(bad, "参考里还有 " + reference + " 个自由点却判成没位置 " + tag);
      }
    }
    expect(bad).toEqual([]);
  });
});
