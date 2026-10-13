/**
 * IRL-E 独立复核（第一阶段）· 对话窗口查看偏好（chatFrame.ts）+ 共享桥（chatFrameState.ts）。
 *
 * 与 Lead 用例的角度差异：
 * 1) 收敛的可见性不变量（随机 300 组，含 origin 偏移与幂等），以及「宽高小于最小值」的边界；
 * 2) 存档边界（版本字段、坏数据不污染旧记录、getItem / removeItem 抛异常、往返一致）；
 * 3) 共享桥的副本语义：发布时是否复制、返回值是否复制、有没有活引用后门。
 *
 * it.fails = 契约应当成立而当前实现不成立的最小复现，缺陷记在 _IRL-REVIEW-E-1.md。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  CHAT_FRAME_DEFAULT,
  CHAT_FRAME_MIN,
  chatFrameKey,
  chatFrameLimits,
  clearChatFrame,
  convergeChatFrame,
  defaultChatFrame,
  loadChatFrame,
  saveChatFrame,
  type ChatFrame,
} from "../chatFrame";
import {
  chatObstacleFrame,
  liveChatCollapsed,
  liveChatFrame,
  publishChatFrame,
  readChatFrame,
  resetChatFrameState,
  setChatCollapsed,
} from "../chatFrameState";

const BOARD_A = "board-a";
const BOARD_B = "board-b";

const show = (frame: ChatFrame): string =>
  "x=" + frame.x + ",y=" + frame.y + ",w=" + frame.w + ",h=" + frame.h;

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

describe("收敛：可见性不变量（300 组随机）", () => {
  it("尺寸不小于最小值的框：收敛后完整落在可见区内，且二次收敛不变", () => {
    const rand = makeRandom(7731);
    const bad: string[] = [];
    for (let i = 0; i < 300; i += 1) {
      const origin = { x: Math.round((rand() - 0.5) * 300), y: Math.round(rand() * 80) };
      const area = { w: 320 + Math.round(rand() * 1400), h: 260 + Math.round(rand() * 800) };
      const frame: ChatFrame = {
        x: Math.round((rand() - 0.5) * 3000),
        y: Math.round((rand() - 0.5) * 2000),
        w: CHAT_FRAME_MIN.w + Math.round(rand() * 1200),
        h: CHAT_FRAME_MIN.h + Math.round(rand() * 800),
      };
      const next = convergeChatFrame(frame, area, origin);
      const tag =
        show(frame) + " in " + area.w + "x" + area.h + " @" + origin.x + "," + origin.y + " -> " + show(next);
      if (next.x < origin.x || next.y < origin.y) note(bad, "跑到 origin 之外 " + tag);
      if (next.x + next.w > origin.x + area.w) note(bad, "右边溢出 " + tag);
      if (next.y + next.h > origin.y + area.h) note(bad, "下边溢出 " + tag);
      if (!Number.isFinite(next.x + next.y + next.w + next.h)) note(bad, "出现非有限数 " + tag);
      const again = convergeChatFrame(next, area, origin);
      if (show(again) !== show(next)) note(bad, "二次收敛还在动 " + tag);
    }
    expect(bad).toEqual([]);
  });
});

describe("收敛：宽 / 高小于最小值的边界（缺陷面）", () => {
  it("现状：x=990 / w=10 → 宽被抬到 300，右边界跑到 1290（可见区只有 1000）", () => {
    const next = convergeChatFrame({ x: 990, y: 0, w: 10, h: 300 }, { w: 1000, h: 800 });
    expect(next.w).toBe(CHAT_FRAME_MIN.w);
    expect(next.x + next.w).toBe(1290);
  });

  it("现状：y=795 / h=5 → 高被抬到 220，下边界跑到 1015（可见区只有 800）", () => {
    const next = convergeChatFrame({ x: 0, y: 795, w: 400, h: 5 }, { w: 1000, h: 800 });
    expect(next.h).toBe(CHAT_FRAME_MIN.h);
    expect(next.y + next.h).toBe(1015);
  });

  it("这种记录真的存得进去：saveChatFrame 只要求正尺寸", () => {
    localStorage.clear();
    expect(saveChatFrame(BOARD_A, { x: 990, y: 795, w: 10, h: 5 })).toBe(true);
    expect(loadChatFrame(BOARD_A)).toEqual({ x: 990, y: 795, w: 10, h: 5 });
  });

  it.fails("契约「恢复到屏幕内」：宽高小于最小值时也不能溢出可见区", () => {
    const next = convergeChatFrame({ x: 990, y: 795, w: 10, h: 5 }, { w: 1000, h: 800 });
    expect(next.x + next.w).toBeLessThanOrEqual(1000);
    expect(next.y + next.h).toBeLessThanOrEqual(800);
  });
});

describe("默认框与限制", () => {
  it("宽裕窗口：默认尺寸贴右下角，留出 margin 与 origin", () => {
    const frame = defaultChatFrame({ w: 1600, h: 1000 }, { x: 0, y: 44 });
    expect(frame.w).toBe(CHAT_FRAME_DEFAULT.w);
    expect(frame.h).toBe(CHAT_FRAME_DEFAULT.h);
    expect(frame.x + frame.w).toBe(1600 - 16);
    expect(frame.y).toBeGreaterThanOrEqual(44);
    expect(frame.y + frame.h).toBeLessThanOrEqual(1044);
  });

  it("窗口比默认框小：收缩到可用尺寸，仍不越界（200×150 的极端窗口）", () => {
    const frame = defaultChatFrame({ w: 200, h: 150 }, { x: 0, y: 44 }, 16);
    expect(frame.x).toBeGreaterThanOrEqual(0);
    expect(frame.y).toBeGreaterThanOrEqual(44);
    expect(frame.x + frame.w).toBeLessThanOrEqual(200);
    expect(frame.y + frame.h).toBeLessThanOrEqual(194);
  });

  it("可用区域不足最小值：上限与下限一起收敛到可用区域", () => {
    expect(chatFrameLimits({ w: 260, h: 180 })).toEqual({ minW: 260, minH: 180, maxW: 260, maxH: 180 });
  });

  it("零面积（还没量出来）：限制塌到 1，收敛会把窗口缩成 1×1（调用方必须先量到面积）", () => {
    const next = convergeChatFrame({ x: 0, y: 0, w: 400, h: 500 }, { w: 0, h: 0 });
    expect(next).toEqual({ x: 0, y: 0, w: 1, h: 1 });
  });

  it("默认框在随机窗口里都不越界（120 组）", () => {
    const rand = makeRandom(90210);
    const bad: string[] = [];
    for (let i = 0; i < 120; i += 1) {
      const origin = { x: Math.round(rand() * 200), y: Math.round(rand() * 120) };
      const area = { w: 360 + Math.round(rand() * 1800), h: 300 + Math.round(rand() * 1000) };
      const frame = defaultChatFrame(area, origin);
      const tag = show(frame) + " in " + area.w + "x" + area.h + " @" + origin.x + "," + origin.y;
      if (frame.x < origin.x || frame.y < origin.y) note(bad, "跑到 origin 之外 " + tag);
      if (frame.x + frame.w > origin.x + area.w) note(bad, "右边溢出 " + tag);
      if (frame.y + frame.h > origin.y + area.h) note(bad, "下边溢出 " + tag);
      if (frame.w < CHAT_FRAME_MIN.w || frame.h < CHAT_FRAME_MIN.h) note(bad, "小于最小值 " + tag);
    }
    expect(bad).toEqual([]);
  });
});

describe("存档格式的边界", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("往返一致：保存什么读回什么（50 组半整数）", () => {
    const rand = makeRandom(4242);
    for (let i = 0; i < 50; i += 1) {
      const frame: ChatFrame = {
        x: Math.round((rand() - 0.5) * 4000) / 2,
        y: Math.round((rand() - 0.5) * 4000) / 2,
        w: 1 + Math.round(rand() * 2000) / 2,
        h: 1 + Math.round(rand() * 2000) / 2,
      };
      expect(saveChatFrame(BOARD_A, frame)).toBe(true);
      expect(loadChatFrame(BOARD_A)).toEqual(frame);
    }
  });

  it("写进去的记录带 version 字段（迁移用），但读的时候完全不校验它", () => {
    saveChatFrame(BOARD_A, { x: 1, y: 2, w: 400, h: 500 });
    expect(localStorage.getItem(chatFrameKey(BOARD_A))).toContain('"version":1');
    localStorage.setItem(
      chatFrameKey(BOARD_A),
      JSON.stringify({ x: 1, y: 2, w: 400, h: 500, version: 999 }),
    );
    expect(loadChatFrame(BOARD_A)).toEqual({ x: 1, y: 2, w: 400, h: 500 });
  });

  it("坏数据不污染已有记录：写失败时旧值原样保留", () => {
    saveChatFrame(BOARD_A, { x: 1, y: 2, w: 400, h: 500 });
    expect(saveChatFrame(BOARD_A, { x: 9, y: 9, w: 0, h: 500 })).toBe(false);
    expect(saveChatFrame(BOARD_A, { x: 9, y: 9, w: 400, h: Number.POSITIVE_INFINITY })).toBe(false);
    expect(loadChatFrame(BOARD_A)).toEqual({ x: 1, y: 2, w: 400, h: 500 });
  });

  it("非对象载荷（数组 / null / 字符串 / 数字）一律当作没有偏好", () => {
    for (const payload of ["[1,2,3,4]", "null", '"x"', "42", "true"]) {
      localStorage.setItem(chatFrameKey(BOARD_A), payload);
      expect(loadChatFrame(BOARD_A)).toBeNull();
    }
  });

  it("getItem 抛异常时读回 null（不只是属性访问抛）", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("受限 WebView");
    });
    try {
      expect(loadChatFrame(BOARD_A)).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });

  it("removeItem 抛异常时清除不抛", () => {
    saveChatFrame(BOARD_A, { x: 1, y: 2, w: 400, h: 500 });
    const spy = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("受限 WebView");
    });
    try {
      expect(() => clearChatFrame(BOARD_A)).not.toThrow();
    } finally {
      spy.mockRestore();
    }
    expect(loadChatFrame(BOARD_A)).not.toBeNull();
  });

  it("板面身份前后空格被归一：同一板面不会分成两条记录", () => {
    saveChatFrame(" board-a ", { x: 1, y: 2, w: 400, h: 500 });
    expect(loadChatFrame("board-a")).toEqual({ x: 1, y: 2, w: 400, h: 500 });
    expect(chatFrameKey(" board-a ")).toBe(chatFrameKey("board-a"));
  });

  it("空 / 空白 / null 身份都落到 default 键（不与某个真实板面串号）", () => {
    expect(chatFrameKey("")).toBe(chatFrameKey(null));
    expect(chatFrameKey("   ")).toBe(chatFrameKey(undefined));
    expect(chatFrameKey("")).not.toBe(chatFrameKey(BOARD_A));
  });

  it("非字符串板面身份（从旧数据 / JS 里漏进来的数字 0）不会掉进 default 键", () => {
    // 类型签名只收 string | null | undefined，这里显式绕过类型，验运行时归一化。
    const numeric = 0 as unknown as string;
    expect(chatFrameKey(numeric)).not.toBe(chatFrameKey(null));
    expect(chatFrameKey(numeric)).toBe(chatFrameKey("0"));
  });

  it("清除只影响当前板面（另一半仍在）", () => {
    saveChatFrame(BOARD_A, { x: 1, y: 2, w: 400, h: 500 });
    saveChatFrame(BOARD_B, { x: 3, y: 4, w: 400, h: 500 });
    clearChatFrame(BOARD_A);
    expect(loadChatFrame(BOARD_A)).toBeNull();
    expect(loadChatFrame(BOARD_B)).toEqual({ x: 3, y: 4, w: 400, h: 500 });
  });
});

describe("共享桥：副本语义与活引用", () => {
  beforeEach(() => resetChatFrameState());

  it("发布时复制传入对象：外部继续改原对象影响不到已发布值", () => {
    const input = { x: 1, y: 2, w: 300, h: 220 };
    publishChatFrame(input);
    input.x = 50;
    expect(readChatFrame()).toEqual({ x: 1, y: 2, w: 300, h: 220 });
  });

  it("readChatFrame 返回副本（改返回值不影响源）", () => {
    publishChatFrame({ x: 1, y: 2, w: 300, h: 220 });
    const copy = readChatFrame();
    copy!.w = 999;
    expect(readChatFrame()!.w).toBe(300);
  });

  it("现状：chatObstacleFrame 返回的是活引用，下游改它等于改源（与 readChatFrame 的副本语义不一致）", () => {
    publishChatFrame({ x: 10, y: 20, w: 400, h: 500 });
    const obstacle = chatObstacleFrame();
    expect(obstacle).not.toBeNull();
    obstacle!.x = 999;
    expect(readChatFrame()!.x).toBe(999);
  });

  it.fails("chatObstacleFrame 也应当返回副本（当前返回活引用，下游 normalize 一下就会污染源）", () => {
    publishChatFrame({ x: 10, y: 20, w: 400, h: 500 });
    const obstacle = chatObstacleFrame();
    obstacle!.x = 999;
    expect(readChatFrame()!.x).toBe(10);
  });

  it("liveChatFrame 是活引用：直接写它能绕过 publish（下游约定只读）", () => {
    publishChatFrame({ x: 0, y: 0, w: 300, h: 220 });
    liveChatFrame.value!.w = 999;
    expect(readChatFrame()!.w).toBe(999);
  });

  it("publish 的收起参数有默认值：不传就把之前的收起态复位（调用方每次都要显式传）", () => {
    publishChatFrame({ x: 0, y: 0, w: 300, h: 220 }, true);
    expect(chatObstacleFrame()).toBeNull();
    publishChatFrame({ x: 0, y: 0, w: 300, h: 220 });
    expect(liveChatCollapsed.value).toBe(false);
    expect(chatObstacleFrame()).not.toBeNull();
  });

  it("单独 setChatCollapsed 就能切换避让，不需要重新 publish", () => {
    publishChatFrame({ x: 0, y: 0, w: 300, h: 220 });
    setChatCollapsed(true);
    expect(chatObstacleFrame()).toBeNull();
    setChatCollapsed(false);
    expect(chatObstacleFrame()).toEqual({ x: 0, y: 0, w: 300, h: 220 });
  });

  it("reset 同时清掉显示框与收起态", () => {
    publishChatFrame({ x: 0, y: 0, w: 300, h: 220 }, true);
    resetChatFrameState();
    expect(readChatFrame()).toBeNull();
    expect(liveChatCollapsed.value).toBe(false);
    expect(chatObstacleFrame()).toBeNull();
  });

  it("卸载发布 null 时收起态一起复位（不留「已收起」的幽灵状态）", () => {
    publishChatFrame({ x: 0, y: 0, w: 300, h: 220 }, true);
    publishChatFrame(null);
    expect(readChatFrame()).toBeNull();
    expect(liveChatCollapsed.value).toBe(false);
  });
});
