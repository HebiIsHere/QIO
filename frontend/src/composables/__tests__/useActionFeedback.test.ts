/**
 * 反馈状态机：进行中 / 成功（短暂）/ 失败（保留且可重试）。
 * 这是「不得静默失败」的最小保证（spec 第 71~74、105、112 条）。
 */
import { describe, expect, it, vi } from "vitest";
import { useActionFeedback } from "../useActionFeedback";

describe("useActionFeedback", () => {
  it("成功：busy → ok → 自动回到 idle", async () => {
    vi.useFakeTimers();
    const fb = useActionFeedback();
    let sawBusy = false;
    const promise = fb.run("k1", async () => {
      sawBusy = fb.stateOf("k1") === "busy";
    });
    await promise;
    expect(sawBusy).toBe(true);
    expect(fb.stateOf("k1")).toBe("ok");
    expect(fb.okTextOf("k1")).toBe("已保存");
    vi.advanceTimersByTime(2000);
    expect(fb.stateOf("k1")).toBe("idle");
    vi.useRealTimers();
  });

  it("失败：保留 failed 与人话原因，直到下次尝试", async () => {
    const fb = useActionFeedback();
    const ok = await fb.run(
      "k2",
      async () => {
        throw new Error("offline");
      },
      { failText: "保存失败，内容未改变" },
    );
    expect(ok).toBe(false);
    expect(fb.stateOf("k2")).toBe("failed");
    expect(fb.errorOf("k2")).toContain("保存失败，内容未改变");
    expect(fb.errorOf("k2")).toContain("可以重试");

    // 重试成功后不再保留失败态
    const ok2 = await fb.run("k2", async () => undefined, { okText: "已保存" });
    expect(ok2).toBe(true);
    expect(fb.stateOf("k2")).toBe("ok");
    expect(fb.errorOf("k2")).toBe("");
  });

  it("进行中重复点击不会重复提交", async () => {
    const fb = useActionFeedback();
    let calls = 0;
    // 用对象持有 resolve：直接声明 `let release: (() => void) | null` 会被
    // 类型收窄成 never（赋值发生在回调里，控制流分析看不到）
    const gate: { release?: () => void } = {};
    const first = fb.run("k3", async () => {
      calls += 1;
      await new Promise<void>((r) => {
        gate.release = r;
      });
    });
    const second = await fb.run("k3", async () => {
      calls += 1;
    });
    expect(second).toBe(false);
    expect(calls).toBe(1);
    gate.release?.();
    await first;
    expect(calls).toBe(1);
  });

  it("不同 key 的状态互不影响（反馈留在各自的卡片上）", async () => {
    const fb = useActionFeedback();
    await fb.run("a", async () => {
      throw new Error("boom");
    });
    await fb.run("b", async () => undefined);
    expect(fb.stateOf("a")).toBe("failed");
    expect(fb.stateOf("b")).toBe("ok");
  });
});
