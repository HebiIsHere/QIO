/**
 * IRL-E 独立复核 · 对话窗口「当前显示框」共享桥（chatFrameState.ts）。
 *
 * 2026-10-26（合并后清理 · C2）：本文件原来的前半部分守的是 chatFrame.ts 的
 * 「按板面身份持久化对话窗口位置 / 尺寸」（load/save/clear/default/converge/limits）。
 * 旧 ChatDock 删除后这条持久化链路已无生产者（v2 互动页的对话区不再是可拖动、可保存的浮窗），
 * 相关实现与用例一并删除；这里只保留**仍然有意义**的共享桥语义：
 * 发布时复制、读取返回副本、活引用是显式只读约定、收起/清空不留幽灵矩形。
 * 新版生产者是 v2/InteractiveConversation（C01），消费者是 BoardCanvas 的 chatObstacleFrame()。
 */
import { beforeEach, describe, expect, it } from "vitest";
import {
  chatObstacleFrame,
  liveChatCollapsed,
  liveChatFrame,
  publishChatFrame,
  readChatFrame,
  resetChatFrameState,
  setChatCollapsed,
} from "../chatFrameState";

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

  it("修复后（F4）：chatObstacleFrame 返回副本，下游就地改不会污染源", () => {
    publishChatFrame({ x: 10, y: 20, w: 400, h: 500 });
    const obstacle = chatObstacleFrame();
    expect(obstacle).toEqual({ x: 10, y: 20, w: 400, h: 500 });
    obstacle!.x = 999;
    obstacle!.w = 1;
    expect(readChatFrame()).toEqual({ x: 10, y: 20, w: 400, h: 500 });
  });

  it("修复后（F4）：两次 chatObstacleFrame 拿到两个独立对象", () => {
    publishChatFrame({ x: 10, y: 20, w: 400, h: 500 });
    const first = chatObstacleFrame();
    const second = chatObstacleFrame();
    expect(first).not.toBe(second);
    expect(first).toEqual(second);
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
