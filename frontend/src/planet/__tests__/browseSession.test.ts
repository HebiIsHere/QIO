/**
 * 星球浏览会话：展示窗口的进出规则。
 *
 * 第二阶段的产品约束（见 spec 第 6~13 / 46~52 条）：
 * - 同屏话题数量有明确上限，但总话题数不受限（队列可以一直补）；
 * - 前进一次只换掉一个槽位，其它话题保持原位，不能整屏跳位；
 * - 短距离反向浏览要能把刚离开的话题放回原来的槽位；
 * - 节奏慢：一个话题至少被看见一段时间才可回收，被选中的话题在查看期间锁定。
 */
import { describe, expect, it } from "vitest";

import { PlanetBrowseSession, VISIBLE_CAPACITY, type BrowseTopic, type SwapContext } from "../browseSession";

function topic(n: number): BrowseTopic {
  return {
    topic_id: `t${n}`,
    title: `话题 ${n}`,
    fragment_count: n,
    last_activity: null,
  };
}

function topics(count: number): BrowseTopic[] {
  return Array.from({ length: count }, (_, i) => topic(i));
}

function openSession(count = 40, capacity = 8): PlanetBrowseSession {
  const session = new PlanetBrowseSession({ capacity });
  session.setSequence(topics(count));
  session.fill(0);
  return session;
}

/** 每次都给一个「背面」位置：用序号造一批互不相同的确定性方向 */
function placer(offset = 0) {
  let n = offset;
  return (occupied: [number, number, number][]) => {
    n += 1;
    return [-0.4 - (n % 5) * 0.05, 0.2, -0.8] as [number, number, number];
  };
}

const ctx = (backSlots: number[], place?: SwapContext["place"]): SwapContext => ({ backSlots, place });

describe("PlanetBrowseSession", () => {
  it("同屏话题数永远不超过容量", () => {
    const session = openSession(40, 8);

    expect(session.windowSlots()).toHaveLength(8);
    expect(session.windowSlots().filter(Boolean)).toHaveLength(8);

    for (let i = 0; i < 30; i++) {
      session.takeSwap(1, ctx([i % 8]), i * 1000);
    }

    expect(session.windowSlots()).toHaveLength(8);
    expect(session.windowSlots().filter(Boolean).length).toBeLessThanOrEqual(8);
  });

  it("默认容量与后端可见容量一致", () => {
    expect(new PlanetBrowseSession().windowSlots()).toHaveLength(VISIBLE_CAPACITY);
  });

  it("前进一次只换掉一个槽位，其余话题保持原位", () => {
    const session = openSession(40, 8);
    const before = session.windowSlots().map((t) => t?.topic_id);

    const swap = session.takeSwap(1, ctx([3]), 10_000);

    expect(swap).not.toBeNull();
    expect(swap!.slot).toBe(3);
    const after = session.windowSlots().map((t) => t?.topic_id);
    for (let i = 0; i < 8; i++) {
      if (i === 3) continue;
      expect(after[i]).toBe(before[i]);
    }
    expect(after[3]).not.toBe(before[3]);
  });

  it("短距离反向能把刚离开的话题放回原来的槽位", () => {
    const session = openSession(40, 8);
    const before = session.windowSlots().map((t) => t?.topic_id);
    session.takeSwap(1, ctx([5]), 10_000);
    const afterForward = session.windowSlots().map((t) => t?.topic_id);
    expect(afterForward[5]).not.toBe(before[5]);

    const back = session.takeSwap(-1, ctx([]), 10_500);

    expect(back!.slot).toBe(5);
    expect(session.windowSlots().map((t) => t?.topic_id)).toEqual(before);
  });

  it("还没有可还原记录时，反向旋转同样带来新话题（世界在两侧延伸）", () => {
    const session = openSession(40, 8);
    const before = session.windowSlots().map((t) => t?.topic_id);

    const swap = session.takeSwap(-1, ctx([1]), 10_000);

    expect(swap).not.toBeNull();
    const after = session.windowSlots().map((t) => t?.topic_id);
    expect(after).not.toEqual(before);
    expect(after.filter(Boolean)).toHaveLength(8);
    // 不能把当前窗口里已经有的话题再塞一遍
    expect(new Set(after).size).toBe(8);
  });

  it("持续往同一方向旋转：每一步都真的换进新话题（不能一进一退原地打转）", () => {
    const session = openSession(40, 8);
    const snapshots: string[] = [];

    for (let i = 0; i < 6; i++) {
      session.takeSwap(-1, ctx([i % 8]), 10_000 + i * 2_000);
      snapshots.push(session.windowSlots().map((t) => t?.topic_id).join(","));
    }

    expect(new Set(snapshots).size).toBe(snapshots.length);
  });

  it("新进入的话题使用 place 给出的背面位置，其它成员的位置一概不动", () => {
    const session = openSession(40, 8);
    const dirsBefore = session.windowMembers().map((m) => m?.dir);
    const place = placer();
    const backDir = [-0.4 - 0.05, 0.2, -0.8] as [number, number, number];

    const swap = session.takeSwap(1, ctx([4], place), 10_000);

    expect(swap?.slot).toBe(4);
    expect(session.windowMembers()[4]?.dir).toEqual(backDir);
    for (let i = 0; i < 8; i++) {
      if (i === 4) continue;
      expect(session.windowMembers()[i]?.dir).toEqual(dirsBefore[i]);
    }
  });

  it("place 拿到的是「除了要离开的那个点之外」已占用的位置", () => {
    const session = openSession(40, 8);
    let seen: number[] = [];
    const place = (occupied: [number, number, number][]) => {
      seen = occupied.map((d) => d[1]);
      return [0, 0.1, -1] as [number, number, number];
    };

    session.takeSwap(1, ctx([3], place), 10_000);

    expect(seen).toHaveLength(7);
    expect(seen).not.toContain(session.windowMembers()[3]?.dir[1]);
  });

  it("反向还原时话题连同它的位置一起回到原槽位", () => {
    const session = openSession(40, 8);
    const original = session.windowMembers()[2]!;

    session.takeSwap(1, ctx([2], placer()), 10_000);
    expect(session.windowMembers()[2]?.dir).not.toEqual(original.dir);

    session.takeSwap(-1, ctx([]), 10_500);

    expect(session.windowMembers()[2]?.topic.topic_id).toBe(original.topic.topic_id);
    expect(session.windowMembers()[2]?.dir).toEqual(original.dir);
  });

  it("被选中的话题在用户查看期间不会被回收", () => {
    const session = openSession(40, 8);
    const selected = session.windowSlots()[2]!;
    session.lock(selected.topic_id);

    for (let i = 0; i < 10; i++) {
      session.takeSwap(1, ctx([2]), 20_000 + i * 5_000);
    }

    expect(session.windowSlots()[2]?.topic_id).toBe(selected.topic_id);
  });

  it("取消选中后该槽位重新进入正常流动", () => {
    const session = openSession(40, 8);
    const selected = session.windowSlots()[2]!;
    session.lock(selected.topic_id);
    session.takeSwap(1, ctx([2]), 20_000);
    expect(session.windowSlots()[2]?.topic_id).toBe(selected.topic_id);

    session.lock(null);
    const swap = session.takeSwap(1, ctx([2]), 40_000);

    expect(swap?.slot).toBe(2);
    expect(session.windowSlots()[2]?.topic_id).not.toBe(selected.topic_id);
  });

  it("一个话题至少要展示 minLifetimeMs 才可能被回收", () => {
    const session = new PlanetBrowseSession({ capacity: 4, minLifetimeMs: 1200 });
    session.setSequence(topics(20));
    session.fill(0);

    expect(session.takeSwap(1, ctx([0]), 300)).toBeNull();
    expect(session.takeSwap(1, ctx([0]), 1300)).not.toBeNull();
  });

  it("冷却窗口内的话题不会立刻从另一侧重新出现", () => {
    const session = new PlanetBrowseSession({ capacity: 4, cooldownSize: 8, minLifetimeMs: 0 });
    session.setSequence(topics(30));
    session.fill(0);
    const justShown = new Set(session.windowSlots().map((t) => t!.topic_id));

    const seen: string[] = [];
    for (let i = 0; i < 8; i++) {
      const swap = session.takeSwap(1, ctx([0]), 5_000 + i * 1_000);
      if (swap?.topic) seen.push(swap.topic.topic_id);
    }

    expect(seen).toHaveLength(8);
    expect(new Set(seen).size).toBe(8);
    for (const id of seen) expect(justShown.has(id)).toBe(false);
  });

  it("补充新一页后可以继续取出没展示过的话题", () => {
    const session = new PlanetBrowseSession({ capacity: 4, minLifetimeMs: 0 });
    session.setSequence(topics(4));
    session.fill(0);
    expect(session.needsMore()).toBe(true);

    session.appendSequence(topics(10).slice(4));

    const swap = session.takeSwap(1, ctx([0]), 9_000);
    expect(swap?.topic?.topic_id).toBeDefined();
  });

  it("搜索命中的话题可以被注入窗口且不挤掉已锁定的话题", () => {
    const session = openSession(40, 4);
    const locked = session.windowSlots()[0]!;
    session.lock(locked.topic_id);
    const found = topic(999);

    const slot = session.pinTopic(found);

    expect(slot).toBeGreaterThanOrEqual(0);
    expect(session.windowSlots()[slot]?.topic_id).toBe(found.topic_id);
    expect(session.windowSlots()[0]?.topic_id).toBe(locked.topic_id);
  });

  it("已经展示过的话题不需要再次插入", () => {
    const session = openSession(40, 4);
    const existing = session.windowSlots()[1]!;

    const slot = session.pinTopic(existing);

    expect(slot).toBe(1);
    expect(session.windowSlots().filter(Boolean)).toHaveLength(4);
  });
});
