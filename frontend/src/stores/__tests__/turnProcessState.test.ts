/**
 * 过程区展开状态的持久化（会话内 + 跨刷新）。
 *
 * 虚拟列表只渲染可视项、条目会被卸载：组件内的 ref 会丢，所以状态必须放 store，
 * 键里必须带 turn_id / stage_id，跨刷新也不能串。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const STORAGE_KEY = "qio.turnProcess.expanded";

beforeEach(() => {
  localStorage.clear();
  vi.resetModules();
});

describe("过程区展开状态", () => {
  it("键含话题 / 轮 / 阶段：跨轮、跨阶段不串", async () => {
    const mod = await import("../turnProcess");
    const a = mod.processKey("topic_1", "turn_1");
    const b = mod.processKey("topic_1", "turn_2");
    const c = mod.processKey("topic_1", "turn_1", "st_1");
    expect(new Set([a, b, c]).size).toBe(3);
    expect(a).toContain("turn_1");
    expect(c).toContain("st_1");
  });

  it("用户手动开合：记 manual 并写进 localStorage", async () => {
    const mod = await import("../turnProcess");
    const key = mod.processKey("topic_1", "turn_1");
    mod.toggleProcess(key, true);
    expect(mod.isProcessExpanded(key)).toBe(true);
    expect(mod.isProcessManual(key)).toBe(true);
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") as Record<string, unknown>;
    expect(raw[key]).toEqual({ open: true, manual: true });
  });

  it("自动展开 / 收起只改 open 与 manual（是否覆盖用户选择由过程区按 manual 判断）", async () => {
    const mod = await import("../turnProcess");
    const key = mod.processKey("topic_1", "turn_1");
    mod.toggleProcess(key, true);
    mod.setProcessExpanded(key, false);
    expect(mod.isProcessExpanded(key)).toBe(false);
    expect(mod.isProcessManual(key)).toBe(false);
  });

  it("跨刷新：localStorage 里的记录会被读回来（键里带 turn_id / stage_id）", async () => {
    const key = "topic_1|turn_9|st_9";
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ [key]: { open: true, manual: true } }));
    const mod = await import("../turnProcess");
    expect(mod.isProcessExpanded(key)).toBe(true);
    expect(mod.isProcessManual(key)).toBe(true);
    // 没有记录过的键保持默认（不编造展开状态）
    expect(mod.isProcessExpanded("topic_1|turn_9|-")).toBe(false);
  });
});
