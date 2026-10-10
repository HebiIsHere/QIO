/**
 * F06 回归：**切换实体后必须丢弃迟到的候选响应**。
 *
 * 原缺陷：`load()` 在 `await` 之后无条件写回结果。实体 A 的请求挂起时切到 B，
 * B 先返回、A 后返回 —— B 的面板会显示 A 的候选，而操作又会按响应里的
 * `entity_id` 发出去（对 B 的界面点「采纳」，实际改的是 A）。
 *
 * 这里用受控替身把请求按「谁先返回」排好序，断言：
 * 1. A→B，B 先返回 A 后返回：面板只显示 B，操作只发往 B；
 * 2. 快速连续切换 A→B→C：只认最后一次（C）；
 * 3. 失败也迟到：A 的错误不能写到 B 的面板上；
 * 4. 刷新（冲突后重读）中切换实体：A 的刷新结果整段丢弃；
 * 5. 组件卸载后迟到：数据 / 错误都不写回，也不报错；
 * 6. 旧请求的 finally 不得把新请求的 loading 提前关掉。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import EntityCandidates from "../EntityCandidates.vue";
import {
  EntityCandidateConflictError,
  type EntityCandidateView,
} from "../../../services/entityCandidatesApi";

const mocks = vi.hoisted(() => ({
  listAllCandidates: vi.fn(),
  listEntityCandidates: vi.fn(),
  adoptCandidate: vi.fn(),
  dismissCandidate: vi.fn(),
}));

vi.mock("../../../services/entityCandidatesApi", async () => {
  const actual = await vi.importActual<typeof import("../../../services/entityCandidatesApi")>(
    "../../../services/entityCandidatesApi",
  );
  return {
    ...actual,
    listAllCandidates: mocks.listAllCandidates,
    listEntityCandidates: mocks.listEntityCandidates,
    adoptCandidate: mocks.adoptCandidate,
    dismissCandidate: mocks.dismissCandidate,
  };
});

function candidate(
  entityId: string,
  overrides: Partial<EntityCandidateView> = {},
): EntityCandidateView {
  return {
    candidate_id: `cand_${entityId}`,
    entity_id: entityId,
    entity_name: `实体 ${entityId}`,
    card_state: "active",
    field: "attributes.生日",
    field_label: `${entityId} 的生日`,
    kind: "attribute",
    current_value: "",
    candidate_value: `${entityId} 的候选值`,
    reason: "user_value_conflict",
    reason_label: "你已经手动写过这个字段",
    created_at: "2026-10-10T02:00:00+00:00",
    card_revision: 7,
    adoptable: true,
    blocked_reason: "",
    ...overrides,
  };
}

function entityListing(candidates: EntityCandidateView[], extra: Record<string, unknown> = {}) {
  return { entity: null, candidates, total: candidates.length, truncated: false, ...extra };
}

/** 手工控制返回时机：验证「谁先返回」不改变归宿。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.listAllCandidates.mockResolvedValue({
    candidates: [],
    total: 0,
    shown: 0,
    truncated: false,
  });
  mocks.listEntityCandidates.mockResolvedValue(entityListing([]));
  mocks.adoptCandidate.mockResolvedValue({ ok: true, entity: null, adopted: null });
  mocks.dismissCandidate.mockResolvedValue({ ok: true, dismissed: true });
});

async function expand(w: VueWrapper) {
  await w.find(".ec-head").trigger("click");
  await flushPromises();
}

describe("F06 切换实体：迟到响应必须丢弃", () => {
  it("A→B，B 先返回 A 后返回：面板只显示 B，操作只发往 B", async () => {
    const a = deferred<ReturnType<typeof entityListing>>();
    const b = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates
      .mockImplementationOnce(() => a.promise)
      .mockImplementationOnce(() => b.promise);

    const w = mount(EntityCandidates, { props: { entityId: "A" } });
    await flushPromises();
    expect(mocks.listEntityCandidates).toHaveBeenNthCalledWith(1, "A");

    await w.setProps({ entityId: "B" });
    await flushPromises();
    expect(mocks.listEntityCandidates).toHaveBeenNthCalledWith(2, "B");

    // B 先回来
    b.resolve(entityListing([candidate("B")]));
    await flushPromises();
    await expand(w);
    expect(w.find(".ec-title").text()).toContain("1");
    expect(w.find(".ec-row").text()).toContain("B 的生日");

    // A 的响应迟到：整段丢弃，B 的面板不准出现 A 的任何东西
    a.resolve(entityListing([candidate("A")]));
    await flushPromises();
    expect(w.find(".ec-title").text()).toContain("1");
    expect(w.findAll(".ec-row")).toHaveLength(1);
    expect(w.text()).toContain("B 的生日");
    expect(w.text()).not.toContain("A 的生日");

    // 操作只能发往当前面板的实体（B）
    mocks.listEntityCandidates.mockResolvedValue(entityListing([]));
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();
    expect(mocks.adoptCandidate).toHaveBeenCalledWith("B", "cand_B", 7);
    expect(mocks.adoptCandidate).not.toHaveBeenCalledWith("A", "cand_A", 7);

    w.unmount();
  });

  it("快速连续切换 A→B→C：只有最后一次（C）生效，乱序返回也不改归宿", async () => {
    const a = deferred<ReturnType<typeof entityListing>>();
    const b = deferred<ReturnType<typeof entityListing>>();
    const c = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates
      .mockImplementationOnce(() => a.promise)
      .mockImplementationOnce(() => b.promise)
      .mockImplementationOnce(() => c.promise);

    const w = mount(EntityCandidates, { props: { entityId: "A" } });
    await flushPromises();
    await w.setProps({ entityId: "B" });
    await flushPromises();
    await w.setProps({ entityId: "C" });
    await flushPromises();
    expect(mocks.listEntityCandidates).toHaveBeenCalledTimes(3);

    // 乱序返回：C 最先，然后 B，最后 A
    c.resolve(entityListing([candidate("C")]));
    await flushPromises();
    await expand(w);
    b.resolve(entityListing([candidate("B")]));
    await flushPromises();
    a.resolve(entityListing([candidate("A")]));
    await flushPromises();

    expect(w.findAll(".ec-row")).toHaveLength(1);
    expect(w.text()).toContain("C 的生日");
    expect(w.text()).not.toContain("B 的生日");
    expect(w.text()).not.toContain("A 的生日");

    w.unmount();
  });

  it("失败也迟到：A 的错误不能写到 B 的面板上", async () => {
    const a = deferred<ReturnType<typeof entityListing>>();
    const b = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates
      .mockImplementationOnce(() => a.promise)
      .mockImplementationOnce(() => b.promise);

    const w = mount(EntityCandidates, { props: { entityId: "A" } });
    await flushPromises();
    await w.setProps({ entityId: "B" });
    await flushPromises();

    b.resolve(entityListing([candidate("B")]));
    await flushPromises();
    a.reject(new Error("A 的网络炸了"));
    await flushPromises();

    await expand(w);
    expect(w.find(".ec-error").exists()).toBe(false);
    expect(w.text()).not.toContain("A 的网络炸了");
    expect(w.text()).toContain("B 的生日");

    w.unmount();
  });

  it("刷新（409 后重读）中切换实体：A 的刷新结果整段丢弃", async () => {
    const initial = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates.mockImplementationOnce(() => initial.promise);

    const w = mount(EntityCandidates, { props: { entityId: "A" } });
    await flushPromises();
    initial.resolve(entityListing([candidate("A")]));
    await flushPromises();
    await expand(w);

    mocks.adoptCandidate.mockRejectedValue(
      new EntityCandidateConflictError("/api/x/adopt", "stale_revision", 9, "stale_revision"),
    );
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();
    expect(w.find(".ec-row-notice.conflict").exists()).toBe(true);

    // 点「刷新」开始重读 A；重读还没回来就切到 B
    const refreshA = deferred<ReturnType<typeof entityListing>>();
    const loadB = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates
      .mockImplementationOnce(() => refreshA.promise)
      .mockImplementationOnce(() => loadB.promise);
    await w.find(".ec-refresh").trigger("click");
    await flushPromises();

    await w.setProps({ entityId: "B" });
    await flushPromises();
    loadB.resolve(entityListing([candidate("B")]));
    await flushPromises();

    // A 的刷新最后才回来：不能覆盖 B，也不能把过期的冲突提示搬过来
    refreshA.resolve(entityListing([candidate("A")]));
    await flushPromises();

    expect(w.findAll(".ec-row")).toHaveLength(1);
    expect(w.text()).toContain("B 的生日");
    expect(w.text()).not.toContain("A 的生日");
    expect(w.find(".ec-row-notice").exists()).toBe(false);

    w.unmount();
  });

  it("组件卸载后迟到：不写回数据，也不写回错误", async () => {
    const a = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates.mockImplementationOnce(() => a.promise);

    const w = mount(EntityCandidates, { props: { entityId: "A" } });
    await flushPromises();
    type Internals = { candidates: EntityCandidateView[]; loadError: string };
    const vm = w.vm as unknown as Internals;

    w.unmount();
    a.resolve(entityListing([candidate("A")]));
    await flushPromises();

    expect(vm.candidates).toEqual([]);
    expect(vm.loadError).toBe("");

    // 失败同样不许写回
    const b = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates.mockImplementationOnce(() => b.promise);
    const w2 = mount(EntityCandidates, { props: { entityId: "A" } });
    await flushPromises();
    const vm2 = w2.vm as unknown as Internals;
    w2.unmount();
    b.reject(new Error("卸载后的失败"));
    await flushPromises();
    expect(vm2.candidates).toEqual([]);
    expect(vm2.loadError).toBe("");
  });

  it("旧请求的 finally 不得把新请求的 loading 提前关掉", async () => {
    const a = deferred<ReturnType<typeof entityListing>>();
    const b = deferred<ReturnType<typeof entityListing>>();
    mocks.listEntityCandidates
      .mockImplementationOnce(() => a.promise)
      .mockImplementationOnce(() => b.promise);

    const w = mount(EntityCandidates, { props: { entityId: "A" } });
    await flushPromises();
    type Internals = { loading: boolean };
    const vm = w.vm as unknown as Internals;

    await w.setProps({ entityId: "B" });
    await flushPromises();
    expect(vm.loading).toBe(true);

    // 旧实体 A 的响应回来：它没资格关掉 B 那次请求的 loading
    a.resolve(entityListing([candidate("A")]));
    await flushPromises();
    expect(vm.loading).toBe(true);

    b.resolve(entityListing([candidate("B")]));
    await flushPromises();
    expect(vm.loading).toBe(false);

    w.unmount();
  });
});
