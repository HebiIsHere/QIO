/**
 * W6 · A04 界面接线（父组件）：`components/planet/EntityPanel.vue`。
 *
 * 契约要求「实际界面完成一次操作」，所以这里挂的是**真实组件**，
 * 只把两个数据来源换成受控替身：
 * - `services/api`（实体卡读写）；
 * - `services/entityCandidatesApi`（候选列表与决定）。
 *
 * 验证的行为：
 * 1. 列表页就能看到「待处理候选（n）」（默认收起），展开后点「采纳」会真的调用
 *    `adoptCandidate(entity_id, candidate_id, card_revision)`；
 * 2. 采纳之后父组件重新取权威卡数据，阅读态显示的是**刷新后的真实值**；
 * 3. 详情页的候选只针对这张卡（`listEntityCandidates(entityId)`）；
 * 4. 编辑态不显示候选区块（草稿写入与采纳是两套写入，混在一起会互相盖掉）。
 *
 * 标注：真实后端尚未接线，端到端（真库 + 真候选）未验证。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import EntityPanel from "../EntityPanel.vue";
import type { EntityCard } from "../../../services/api";
import type { EntityCandidateView } from "../../../services/entityCandidatesApi";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listEntities: vi.fn(),
    getEntity: vi.fn(),
    reviseEntity: vi.fn(),
    revokeEntity: vi.fn(),
    addEntityRelation: vi.fn(),
    removeEntityRelation: vi.fn(),
  },
  listAllCandidates: vi.fn(),
  listEntityCandidates: vi.fn(),
  adoptCandidate: vi.fn(),
  dismissCandidate: vi.fn(),
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

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

function card(overrides: Partial<EntityCard> = {}): EntityCard {
  return {
    id: "ec_1",
    node_id: "n1",
    name: "王翠华",
    aliases: ["妈"],
    kind: "家人",
    summary: "我妈妈",
    attributes: [],
    relations: [],
    state: "active",
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

function candidate(overrides: Partial<EntityCandidateView> = {}): EntityCandidateView {
  return {
    candidate_id: "cand_1",
    entity_id: "ec_1",
    entity_name: "王翠华",
    card_state: "active",
    field: "summary",
    field_label: "摘要",
    kind: "summary",
    current_value: "我妈妈",
    candidate_value: "我妈，退休教师",
    reason: "stale_revision",
    reason_label: "这条候选来自旧版本的卡片，需要你确认一次",
    created_at: "2026-10-10T02:00:00+00:00",
    card_revision: 7,
    adoptable: true,
    blocked_reason: "",
    ...overrides,
  };
}

function listing(candidates: EntityCandidateView[]) {
  return { candidates, total: candidates.length, shown: candidates.length, truncated: false };
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [card()] });
  mocks.apiMock.reviseEntity.mockResolvedValue({ ok: true, entity: card() });
  mocks.apiMock.revokeEntity.mockResolvedValue({ ok: true });
  mocks.listAllCandidates.mockResolvedValue(listing([]));
  mocks.listEntityCandidates.mockResolvedValue({
    entity: card(),
    candidates: [],
    total: 0,
    truncated: false,
  });
  mocks.adoptCandidate.mockResolvedValue({ ok: true, entity: card(), adopted: null });
  mocks.dismissCandidate.mockResolvedValue({ ok: true, dismissed: true });
});

describe("列表页：实际点击一次「采纳」", () => {
  it("展开候选区块 → 采纳 → 调用带 revision 的接口并重新取权威卡数据", async () => {
    mocks.listAllCandidates
      .mockResolvedValueOnce(listing([candidate()]))
      .mockResolvedValueOnce(listing([]));

    const w = mount(EntityPanel);
    await flushPromises();
    // 候选区块本身要等实体列表先渲染出来才挂载，再清一轮微任务
    await flushPromises();

    // 默认收起：只看得见数量
    expect(w.find(".ec-title").text()).toBe("待处理候选（1）");
    expect(w.find(".ec-panel").exists()).toBe(false);

    await w.find(".ec-head").trigger("click");
    await flushPromises();
    expect(w.find(".ec-row").text()).toContain("摘要");
    expect(w.find(".ec-row").text()).toContain("我妈，退休教师");

    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_1", "cand_1", 7);
    // 采纳后重新读候选清单，并让父组件重新取一次实体卡（不靠本地猜）
    expect(mocks.listAllCandidates).toHaveBeenCalledTimes(2);
    expect(mocks.apiMock.listEntities).toHaveBeenCalledTimes(2);
    expect(w.find(".ec-notice").text()).toContain("已采纳");
    w.unmount();
  });
});

describe("详情页：候选只针对这张卡，采纳后阅读态显示真实值", () => {
  it("打开卡片 → 区块改用单卡接口；采纳后阅读态刷新为新值", async () => {
    mocks.apiMock.listEntities
      .mockResolvedValueOnce({ entities: [card()] })
      .mockResolvedValueOnce({ entities: [card({ summary: "我妈，退休教师" })] });
    mocks.listEntityCandidates
      .mockResolvedValueOnce({ entity: card(), candidates: [candidate()], total: 1, truncated: false })
      .mockResolvedValueOnce({ entity: card({ summary: "我妈，退休教师" }), candidates: [], total: 0, truncated: false });

    const w = mount(EntityPanel);
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();

    expect(w.find(".e-read-summary").text()).toBe("我妈妈");
    expect(mocks.listEntityCandidates).toHaveBeenCalledWith("ec_1");
    // 候选区块的标题在卡片关闭时读的是跨卡清单，打开这张卡之后是它自己的候选
    expect(w.find(".ec-title").text()).toBe("待处理候选（1）");

    await w.find(".ec-head").trigger("click");
    await flushPromises();
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_1", "cand_1", 7);
    expect(mocks.apiMock.listEntities).toHaveBeenCalledTimes(2);
    // 刷新后展示的是真实持久状态
    expect(w.find(".e-read-summary").text()).toBe("我妈，退休教师");
    expect(w.find(".e-detail").exists()).toBe(true);
    w.unmount();
  });

  it("编辑态不显示候选区块（草稿与采纳是两套写入）", async () => {
    mocks.listEntityCandidates.mockResolvedValue({
      entity: card(),
      candidates: [candidate()],
      total: 1,
      truncated: false,
    });

    const w = mount(EntityPanel);
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    expect(w.find(".ec-block").exists()).toBe(true);

    await w.find(".e-edit-open").trigger("click");
    await flushPromises();
    expect(w.find(".ec-block").exists()).toBe(false);

    await w.find(".e-cancel").trigger("click");
    await flushPromises();
    expect(w.find(".ec-block").exists()).toBe(true);
    w.unmount();
  });

  it("采纳失败（409 冲突）：详情页就地提示，值不假装变过", async () => {
    const { EntityCandidateConflictError } = await vi.importActual<
      typeof import("../../../services/entityCandidatesApi")
    >("../../../services/entityCandidatesApi");
    mocks.listEntityCandidates.mockResolvedValue({
      entity: card(),
      candidates: [candidate()],
      total: 1,
      truncated: false,
    });
    mocks.adoptCandidate.mockRejectedValue(
      new EntityCandidateConflictError("/api/entities/ec_1/candidates/cand_1/adopt", "stale_revision", 9, "stale_revision"),
    );

    const w = mount(EntityPanel);
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    await w.find(".ec-head").trigger("click");
    await flushPromises();
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    expect(w.find(".ec-row-notice.conflict").text()).toContain("这条实体已被改动，请刷新后重试");
    // 卡片内容没有被本地改写，父组件也没有重新拉数据（什么都没发生）
    expect(w.find(".e-read-summary").text()).toBe("我妈妈");
    expect(mocks.apiMock.listEntities).toHaveBeenCalledTimes(1);
    w.unmount();
  });
});
