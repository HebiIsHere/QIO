/**
 * F09 回归：跨实体候选 UI 必须用 `(entity_id, candidate_id)` 复合身份。
 *
 * 原缺陷：后端 `_pending_id(field, value)` 的 candidate_id 只在**单实体内**唯一。
 * 两个实体相同字段+值会撞同一个 id（核查值 `pc_553140533c`）。全局候选列表的
 * Vue key / rowNotice / busyId 只用 candidate_id，于是对实体 A 的采纳故障会同时
 * 显示在实体 B 的行上，`candidate_id === busyId` 还会让两行都显示「处理中…」。
 *
 * 这里用两个 **candidate_id 完全相同、entity_id 不同** 的候选，断言：
 * 1. 两行都能渲染，且 Vue key 是互不相同、且带各自 entity_id 的复合身份；
 * 2. 对 A 的操作只改变 A 的行内提示，B 的行内提示 / 按钮状态完全不变；
 * 3. 忙碌状态只落在被操作的那一行，B 仍可独立操作；
 * 4. 操作请求仍然用**各自实体**的 entity_id（界面身份不改变接口契约）；
 * 5. 重读后 key 随内容更新，不会把 A 的行复用成 B。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import EntityCandidates from "../EntityCandidates.vue";
import type { EntityCandidateView } from "../../../services/entityCandidatesApi";

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

/** 同一个 candidate_id，分属两个实体 —— 缺陷的触发条件。 */
const SHARED_ID = "pc_553140533c";

function candidate(entityId: string, overrides: Partial<EntityCandidateView> = {}): EntityCandidateView {
  return {
    candidate_id: SHARED_ID,
    entity_id: entityId,
    entity_name: entityId === "ec_A" ? "甲实体" : "乙实体",
    card_state: "active",
    field: "attributes.生日",
    field_label: "生日",
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

const A = candidate("ec_A");
const B = candidate("ec_B");

function listing(candidates: EntityCandidateView[], extra: Record<string, unknown> = {}) {
  return { candidates, total: candidates.length, shown: candidates.length, truncated: false, ...extra };
}

/** 从 DOM 元素读回 Vue 渲染用的 key（dev 下元素上挂着 vnode）。 */
function vnodeKey(el: Element): unknown {
  return (el as unknown as { __vnode?: { key?: unknown } }).__vnode?.key;
}

function keysOf(w: VueWrapper): string[] {
  return w.findAll(".ec-row").map((row) => String(vnodeKey(row.element)));
}

async function mountGlobal(initial: EntityCandidateView[] = [A, B]): Promise<VueWrapper> {
  mocks.listAllCandidates.mockResolvedValueOnce(listing(initial));
  const w = mount(EntityCandidates, {});
  await flushPromises();
  await w.find(".ec-head").trigger("click");
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.listAllCandidates.mockResolvedValue(listing([]));
  mocks.listEntityCandidates.mockResolvedValue({ entity: null, candidates: [], total: 0, truncated: false });
  mocks.adoptCandidate.mockResolvedValue({ ok: true, entity: null, adopted: null });
  mocks.dismissCandidate.mockResolvedValue({ ok: true, dismissed: true });
});

function rowByEntity(w: VueWrapper, name: string): DOMWrapper<Element> {
  const row = w.findAll(".ec-row").find((r) => r.find(".ec-entity").text().includes(name));
  if (!row) throw new Error(`没有找到实体 ${name} 的行`);
  return row;
}

describe("F09 跨实体候选的复合身份", () => {
  it("相同 candidate_id 的两行都能渲染，Vue key 互不相同且带各自 entity_id", async () => {
    const w = await mountGlobal();
    const rows = w.findAll(".ec-row");
    expect(rows).toHaveLength(2);

    const keys = keysOf(w);
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys[0]).toContain("ec_A");
    expect(keys[0]).toContain(SHARED_ID);
    expect(keys[1]).toContain("ec_B");
    expect(keys[1]).toContain(SHARED_ID);

    w.unmount();
  });

  it("对 A 的操作只改变 A 的提示，B 的行内提示与按钮状态不变", async () => {
    mocks.adoptCandidate.mockRejectedValue(new Error("A 的写入失败"));
    const w = await mountGlobal();

    await rowByEntity(w, "甲实体").find(".ec-adopt").trigger("click");
    await flushPromises();

    const rowA = rowByEntity(w, "甲实体");
    const rowB = rowByEntity(w, "乙实体");
    expect(rowA.find(".ec-row-notice.err").exists()).toBe(true);
    expect(rowA.text()).toContain("A 的写入失败");
    expect(rowB.find(".ec-row-notice").exists()).toBe(false);
    expect(rowB.find(".ec-adopt").text()).toBe("采纳");
    expect(rowB.find(".ec-adopt").attributes("disabled")).toBeUndefined();

    // 请求仍用候选各自实体的 entity_id
    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_A", SHARED_ID, 7);
    expect(mocks.adoptCandidate).not.toHaveBeenCalledWith("ec_B", SHARED_ID, 7);

    w.unmount();
  });

  it("忙碌状态只落在被操作的行上；B 仍可独立操作并用自己的 entity_id", async () => {
    let release!: (v: unknown) => void;
    mocks.adoptCandidate.mockImplementationOnce(
      () => new Promise((resolve) => (release = resolve)),
    );
    const w = await mountGlobal();

    await rowByEntity(w, "甲实体").find(".ec-adopt").trigger("click");
    await flushPromises();

    const rowA = rowByEntity(w, "甲实体");
    const rowB = rowByEntity(w, "乙实体");
    expect(rowA.find(".ec-adopt").text()).toBe("处理中…");
    expect(rowA.find(".ec-adopt").attributes("disabled")).toBeDefined();
    // B 完全不受影响：标签不是「处理中…」，按钮可点
    expect(rowB.find(".ec-adopt").text()).toBe("采纳");
    expect(rowB.find(".ec-adopt").attributes("disabled")).toBeUndefined();

    // B 可以独立丢弃（用的是 B 自己的 entity_id）
    await rowB.find(".ec-dismiss").trigger("click");
    await flushPromises();
    expect(mocks.dismissCandidate).toHaveBeenCalledWith("ec_B", SHARED_ID, 7);

    release({ ok: true, entity: null, adopted: null });
    await flushPromises();
    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_A", SHARED_ID, 7);

    w.unmount();
  });

  it("重读后 Vue key 随内容更新，不会把 A 的行复用成 B", async () => {
    mocks.listAllCandidates
      .mockResolvedValueOnce(listing([A, B]))
      .mockResolvedValueOnce(listing([B]));
    mocks.adoptCandidate.mockResolvedValue({ ok: true, entity: null, adopted: null });

    const w = mount(EntityCandidates, {});
    await flushPromises();
    await w.find(".ec-head").trigger("click");
    await flushPromises();
    expect(keysOf(w)).toHaveLength(2);

    await rowByEntity(w, "甲实体").find(".ec-adopt").trigger("click");
    await flushPromises();

    const rows = w.findAll(".ec-row");
    expect(rows).toHaveLength(1);
    expect(vnodeKey(rows[0]!.element)).toContain("ec_B");
    expect(rows[0]!.text()).toContain("乙实体");
    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_A", SHARED_ID, 7);

    w.unmount();
  });
});
