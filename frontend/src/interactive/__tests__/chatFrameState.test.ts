/**
 * 对话窗口显示框共享桥用例（互动模式契约 2026-10-13 · IRL §2 / §7.4）。
 *
 * 锁定的行为：审批托盘 / 展开卡片等下游图层读到的是**同一份当前显示框**，
 * 收起时不再当作避让障碍，卸载后不留幽灵矩形。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { watch } from "vue";
import {
  chatObstacleFrame,
  liveChatCollapsed,
  liveChatFrame,
  publishChatFrame,
  readChatFrame,
  resetChatFrameState,
  setChatCollapsed,
} from "../chatFrameState";

describe("chatFrameState：显示框共享桥", () => {
  beforeEach(() => resetChatFrameState());

  it("未发布时为 null，下游回落到默认几何", () => {
    expect(readChatFrame()).toBeNull();
    expect(chatObstacleFrame()).toBeNull();
  });

  it("发布后能读到同一份框，且是副本（下游改不到源）", () => {
    publishChatFrame({ x: 10, y: 20, w: 400, h: 500 });
    const first = readChatFrame();
    expect(first).toEqual({ x: 10, y: 20, w: 400, h: 500 });
    first!.x = 999;
    expect(readChatFrame()?.x).toBe(10);
  });

  it("收起时不再作为避让障碍，但仍然记录显示框", () => {
    publishChatFrame({ x: 10, y: 20, w: 400, h: 500 }, true);
    expect(chatObstacleFrame()).toBeNull();
    expect(readChatFrame()).not.toBeNull();
    expect(liveChatCollapsed.value).toBe(true);
    setChatCollapsed(false);
    expect(chatObstacleFrame()).toEqual({ x: 10, y: 20, w: 400, h: 500 });
  });

  it("卸载（发布 null）后清掉幽灵矩形", () => {
    publishChatFrame({ x: 10, y: 20, w: 400, h: 500 });
    publishChatFrame(null);
    expect(readChatFrame()).toBeNull();
    expect(liveChatFrame.value).toBeNull();
  });

  it("发布是响应式的（下游 computed 能跟着变）", () => {
    const seen: (number | null)[] = [];
    const stop = watch(
      liveChatFrame,
      (value) => seen.push(value ? value.w : null),
      { flush: "sync" },
    );
    publishChatFrame({ x: 0, y: 0, w: 320, h: 400 });
    publishChatFrame({ x: 0, y: 0, w: 640, h: 400 });
    stop();
    expect(seen).toEqual([320, 640]);
  });
});
