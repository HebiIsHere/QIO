/**
 * 对话窗口查看偏好用例（互动模式契约 2026-10-13 · IRL §2 / 任务书 §4.8）。
 *
 * 契约：
 * 1. 按**板面身份**绑定：换板面互不覆盖；
 * 2. 恢复时按当前可用空间收敛，绝不把窗口恢复到屏幕外（含负坐标、超大尺寸）；
 * 3. 存储不可用（隐私模式 / 受限 WebView）时静默降级，不抛错；
 * 4. 坏数据（非 JSON / 缺字段 / 非数字 / 非正尺寸）当作没有偏好，而不是崩溃。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CHAT_FRAME_MIN,
  chatFrameKey,
  chatFrameLimits,
  clearChatFrame,
  convergeChatFrame,
  defaultChatFrame,
  loadChatFrame,
  saveChatFrame,
} from "../chatFrame";

const BOARD_A = "board-a";
const BOARD_B = "board-b";

describe("chatFrame：板面身份绑定", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("键里带板面身份，默认键兜住空身份", () => {
    expect(chatFrameKey(BOARD_A)).not.toBe(chatFrameKey(BOARD_B));
    expect(chatFrameKey("")).toBe(chatFrameKey(null));
  });

  it("两个板面各存各的，互不覆盖", () => {
    saveChatFrame(BOARD_A, { x: 10, y: 20, w: 400, h: 500 });
    saveChatFrame(BOARD_B, { x: 100, y: 200, w: 600, h: 700 });
    expect(loadChatFrame(BOARD_A)).toEqual({ x: 10, y: 20, w: 400, h: 500 });
    expect(loadChatFrame(BOARD_B)).toEqual({ x: 100, y: 200, w: 600, h: 700 });
  });

  it("没存过 → null", () => {
    expect(loadChatFrame(BOARD_A)).toBeNull();
  });

  it("清除只影响当前板面", () => {
    saveChatFrame(BOARD_A, { x: 1, y: 2, w: 400, h: 500 });
    saveChatFrame(BOARD_B, { x: 3, y: 4, w: 400, h: 500 });
    clearChatFrame(BOARD_A);
    expect(loadChatFrame(BOARD_A)).toBeNull();
    expect(loadChatFrame(BOARD_B)).not.toBeNull();
  });
});

describe("chatFrame：坏数据与存储不可用", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("非 JSON / 缺字段 / 非正尺寸一律当作没有偏好", () => {
    localStorage.setItem(chatFrameKey(BOARD_A), "{不是 JSON");
    expect(loadChatFrame(BOARD_A)).toBeNull();
    localStorage.setItem(chatFrameKey(BOARD_A), JSON.stringify({ x: 1, y: 2 }));
    expect(loadChatFrame(BOARD_A)).toBeNull();
    localStorage.setItem(chatFrameKey(BOARD_A), JSON.stringify({ x: 1, y: 2, w: 0, h: 500 }));
    expect(loadChatFrame(BOARD_A)).toBeNull();
    localStorage.setItem(chatFrameKey(BOARD_A), JSON.stringify({ x: 1, y: 2, w: "400", h: 500 }));
    expect(loadChatFrame(BOARD_A)).toBeNull();
  });

  it("存储访问抛异常时读返回 null、写返回 false，都不抛", () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("隐私模式");
      },
    });
    try {
      expect(loadChatFrame(BOARD_A)).toBeNull();
      expect(saveChatFrame(BOARD_A, { x: 0, y: 0, w: 400, h: 500 })).toBe(false);
      expect(() => clearChatFrame(BOARD_A)).not.toThrow();
    } finally {
      if (descriptor) Object.defineProperty(globalThis, "localStorage", descriptor);
    }
  });

  it("setItem 抛配额错误时返回 false，不抛", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    try {
      expect(saveChatFrame(BOARD_A, { x: 0, y: 0, w: 400, h: 500 })).toBe(false);
    } finally {
      spy.mockRestore();
    }
  });
});

describe("chatFrame：恢复到可用空间", () => {
  it("最小值保证消息阅读区（不是一行）", () => {
    expect(CHAT_FRAME_MIN.h).toBeGreaterThanOrEqual(200);
    expect(CHAT_FRAME_MIN.w).toBeGreaterThanOrEqual(280);
  });

  it("保存的超大框收敛到当前可用区域", () => {
    const next = convergeChatFrame({ x: 0, y: 0, w: 3000, h: 3000 }, { w: 900, h: 700 });
    expect(next.w).toBeLessThanOrEqual(900);
    expect(next.h).toBeLessThanOrEqual(700);
  });

  it("保存的位置在屏幕外时挪回可见区", () => {
    const next = convergeChatFrame({ x: 9999, y: 9999, w: 400, h: 500 }, { w: 1000, h: 800 });
    expect(next.x).toBeLessThanOrEqual(1000 - next.w);
    expect(next.y).toBeLessThanOrEqual(800 - next.h);
    expect(next.x).toBeGreaterThanOrEqual(0);
  });

  it("可用区域比最小值还小时，最小值向内收敛（不强行超过屏幕宽度）", () => {
    const limits = chatFrameLimits({ w: 260, h: 180 });
    expect(limits.maxW).toBe(260);
    expect(limits.minW).toBe(260);
    const next = convergeChatFrame({ x: 0, y: 0, w: 800, h: 600 }, { w: 260, h: 180 });
    expect(next.w).toBe(260);
    expect(next.h).toBe(180);
  });

  it("origin（0.2.3 自绘标题栏之下）不会被拉回 0", () => {
    const next = convergeChatFrame({ x: 0, y: 0, w: 400, h: 500 }, { w: 1000, h: 700 }, { x: 0, y: 44 });
    expect(next.y).toBeGreaterThanOrEqual(44);
  });

  it("默认框落在可见区域内且不小于最小值", () => {
    const frame = defaultChatFrame({ w: 1440, h: 900 }, { x: 0, y: 40 });
    expect(frame.x).toBeGreaterThanOrEqual(0);
    expect(frame.y).toBeGreaterThanOrEqual(40);
    expect(frame.x + frame.w).toBeLessThanOrEqual(1440);
    expect(frame.y + frame.h).toBeLessThanOrEqual(940);
    expect(frame.w).toBeGreaterThanOrEqual(CHAT_FRAME_MIN.w);
  });
});
