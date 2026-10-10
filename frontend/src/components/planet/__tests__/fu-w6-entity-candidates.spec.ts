/**
 * W6 · A04 界面接线：「待处理候选」子组件（契约 §3.4）。
 *
 * 这里用受控替身覆盖界面必须成立的行为（不依赖真实后端）：
 * 1. 默认收起，展开后每条显示 当前值 / 候选值 / 一句话原因 / 采纳 / 丢弃；
 * 2. 采纳与丢弃的结果只能来自后端：失败原样显示原因，不假装成功；
 * 3. 409（实体已被改动）就地提示 + 刷新按钮，且刷新后展示真实持久状态；
 * 4. 归档卡片：采纳按钮禁用 + 说明「恢复实体是另一个动作」；
 * 5. 重复点击幂等：在飞时不会再发第二个决定。
 *
 * 标注：候选列表与决定结果都由替身返回，**真实后端尚未接线**，
 * 「真库里有一条候选、点采纳后卡片真的变了」这一端到端结论未验证。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
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

function candidate(overrides: Partial<EntityCandidateView> = {}): EntityCandidateView {
  return {
    candidate_id: "cand_1",
    entity_id: "ec_1",
    entity_name: "王翠华",
    card_state: "active",
    field: "attributes.生日",
    field_label: "生日",
    kind: "attribute",
    current_value: "",
    candidate_value: "1961-03-02",
    reason: "user_value_conflict",
    reason_label: "你已经手动写过这个字段，自动结果不会覆盖它",
    created_at: "2026-10-10T02:00:00+00:00",
    card_revision: 7,
    adoptable: true,
    blocked_reason: "",
    ...overrides,
  };
}

function listing(candidates: EntityCandidateView[], extra: Record<string, unknown> = {}) {
  return { candidates, total: candidates.length, shown: candidates.length, truncated: false, ...extra };
}

async function mountBlock(props: { entityId?: string } = {}) {
  const w = mount(EntityCandidates, { props });
  await flushPromises();
  return w;
}

/** 展开（默认收起是契约要求，所以每个用例都要先点一次标题）。 */
async function expand(w: Awaited<ReturnType<typeof mountBlock>>) {
  await w.find(".ec-head").trigger("click");
  await flushPromises();
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.listAllCandidates.mockResolvedValue(listing([candidate()]));
  mocks.listEntityCandidates.mockResolvedValue({
    entity: null,
    candidates: [candidate()],
    total: 1,
    truncated: false,
  });
  mocks.adoptCandidate.mockResolvedValue({ ok: true, entity: null, adopted: null });
  mocks.dismissCandidate.mockResolvedValue({ ok: true, dismissed: true });
});

describe("默认收起与展开内容", () => {
  it("默认收起：只显示「待处理候选（n）」，不发第二个请求", async () => {
    mocks.listAllCandidates.mockResolvedValue(listing([candidate(), candidate({ candidate_id: "cand_2" })]));
    const w = await mountBlock();

    expect(w.find(".ec-title").text()).toBe("待处理候选（2）");
    expect(w.find(".ec-head").attributes("aria-expanded")).toBe("false");
    expect(w.find(".ec-panel").exists()).toBe(false);
    expect(mocks.listAllCandidates).toHaveBeenCalledTimes(1);
    w.unmount();
  });

  it("展开后每条显示 当前值 / 候选值 / 一句话原因 / 采纳 / 丢弃", async () => {
    const w = await mountBlock();
    await expand(w);

    const row = w.find(".ec-row");
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("生日");
    expect(row.text()).toContain("当前值");
    expect(row.text()).toContain("（空）");
    expect(row.text()).toContain("候选值");
    expect(row.text()).toContain("1961-03-02");
    expect(row.text()).toContain("你已经手动写过这个字段");
    expect(row.find(".ec-adopt").exists()).toBe(true);
    expect(row.find(".ec-dismiss").exists()).toBe(true);
    // 跨卡片清单还要能让用户看出是哪张卡
    expect(row.find(".ec-entity").text()).toContain("王翠华");
    w.unmount();
  });

  it("单卡片模式只取这张卡的候选，并带上 expected_revision", async () => {
    const w = await mountBlock({ entityId: "ec_1" });
    await expand(w);

    expect(mocks.listEntityCandidates).toHaveBeenCalledWith("ec_1");
    expect(mocks.listAllCandidates).not.toHaveBeenCalled();
    expect(w.find(".ec-entity").exists()).toBe(false);

    await w.find(".ec-adopt").trigger("click");
    await flushPromises();
    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_1", "cand_1", 7);
    w.unmount();
  });
});

describe("采纳与丢弃：结果只能来自后端", () => {
  it("采纳成功：重新读一次列表并说清结果，同时通知父组件刷新卡片", async () => {
    mocks.listAllCandidates
      .mockResolvedValueOnce(listing([candidate()]))
      .mockResolvedValueOnce(listing([]));
    mocks.adoptCandidate.mockResolvedValue({
      ok: true,
      entity: null,
      adopted: { field: "attributes.生日", value: "1961-03-02" },
    });

    const w = await mountBlock();
    await expand(w);
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_1", "cand_1", 7);
    // 刷新后展示真实持久状态（候选已经不在清单里）
    expect(mocks.listAllCandidates).toHaveBeenCalledTimes(2);
    expect(w.findAll(".ec-row")).toHaveLength(0); // 候选已经被解决，清单里不再有它
    expect(w.find(".ec-notice").text()).toContain("已采纳");
    expect(w.find(".ec-notice").text()).toContain("1961-03-02");
    expect(w.emitted("changed")?.[0]).toEqual(["ec_1"]);
    w.unmount();
  });

  it("丢弃：不改实体值的语义要写清楚，且不发采纳请求", async () => {
    const w = await mountBlock();
    await expand(w);
    await w.find(".ec-dismiss").trigger("click");
    await flushPromises();

    expect(mocks.dismissCandidate).toHaveBeenCalledWith("ec_1", "cand_1", 7);
    expect(mocks.adoptCandidate).not.toHaveBeenCalled();
    expect(w.find(".ec-notice").text()).toContain("已丢弃");
    expect(w.find(".ec-notice").text()).toContain("当前值未改变");
    w.unmount();
  });

  it("后端说没写进去（ok:false）：不假装成功、候选留在原地", async () => {
    mocks.adoptCandidate.mockResolvedValue({ ok: false, entity: null, adopted: null, blocked_reason: "card_archived" });

    const w = await mountBlock();
    await expand(w);
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    expect(w.find(".ec-notice").exists()).toBe(false);
    expect(w.find(".ec-row-notice").text()).toContain("该实体已归档");
    expect(w.findAll(".ec-row")).toHaveLength(1); // 候选没有被移掉：列表里还剩那一条
    w.unmount();
  });

  it("普通失败：就地显示原因，不显示成功", async () => {
    mocks.adoptCandidate.mockRejectedValue(new Error("POST /api/... -> 500: boom"));

    const w = await mountBlock();
    await expand(w);
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    expect(w.find(".ec-notice").exists()).toBe(false);
    expect(w.find(".ec-row-notice.err").text()).toContain("采纳没有成功");
    expect(w.find(".ec-row-notice.err").text()).toContain("boom");
    // 失败后不刷新成「已解决」：候选还在
    expect(mocks.listAllCandidates).toHaveBeenCalledTimes(1);
    w.unmount();
  });
});

describe("409 冲突：就地提示 + 刷新，不翻成成功", () => {
  it("冲突时就地提示「这条实体已被改动，请刷新后重试」并给刷新按钮", async () => {
    mocks.adoptCandidate.mockRejectedValue(
      new EntityCandidateConflictError("/api/entities/ec_1/candidates/cand_1/adopt", "stale_revision", 9, "stale_revision"),
    );

    const w = await mountBlock();
    await expand(w);
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    const notice = w.find(".ec-row-notice.conflict");
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("这条实体已被改动，请刷新后重试");
    expect(notice.find(".ec-refresh").exists()).toBe(true);
    expect(w.find(".ec-notice").exists()).toBe(false);
    w.unmount();
  });

  it("点刷新只重新读权威状态（不重放那次决定），并撤下过期的冲突提示", async () => {
    mocks.adoptCandidate.mockRejectedValue(
      new EntityCandidateConflictError("/api/x/adopt", "stale_revision", 9, "stale_revision"),
    );
    mocks.listAllCandidates
      .mockResolvedValueOnce(listing([candidate()]))
      .mockResolvedValueOnce(listing([candidate({ card_revision: 9, current_value: "1961-03-02" })]));

    const w = await mountBlock();
    await expand(w);
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();
    expect(w.find(".ec-row-notice.conflict").exists()).toBe(true);

    await w.find(".ec-refresh").trigger("click");
    await flushPromises();

    expect(mocks.adoptCandidate).toHaveBeenCalledTimes(1); // 没有重放
    expect(mocks.listAllCandidates).toHaveBeenCalledTimes(2);
    expect(w.find(".ec-row-notice").exists()).toBe(false);
    // 刷新后展示的是真实持久状态：新的当前值与新的 revision
    expect(w.find(".ec-row").text()).toContain("1961-03-02");
    w.unmount();
  });
});

describe("归档卡片：不给点了没效果的按钮", () => {
  it("采纳按钮禁用 + 说明「该实体已归档；恢复实体是另一个动作」；丢弃仍可用", async () => {
    mocks.listAllCandidates.mockResolvedValue(
      listing([candidate({ card_state: "revoked", adoptable: false, blocked_reason: "card_archived" })]),
    );

    const w = await mountBlock();
    await expand(w);

    const adopt = w.find(".ec-adopt");
    expect(adopt.attributes("disabled")).toBeDefined();
    expect(w.find(".ec-blocked").text()).toContain("该实体已归档");
    expect(w.find(".ec-blocked").text()).toContain("恢复实体是另一个动作");
    // 丢弃不改实体值，是另一个安全动作，保持可用
    expect(w.find(".ec-dismiss").attributes("disabled")).toBeUndefined();

    await w.find(".ec-dismiss").trigger("click");
    await flushPromises();
    expect(mocks.dismissCandidate).toHaveBeenCalledWith("ec_1", "cand_1", 7);
    w.unmount();
  });

  it("不支持一键采纳的候选（adoptable=false）：按钮禁用并给出原因", async () => {
    mocks.listAllCandidates.mockResolvedValue(
      listing([candidate({ adoptable: false, blocked_reason: "structural_conflict" })]),
    );
    const w = await mountBlock();
    await expand(w);

    expect(w.find(".ec-adopt").attributes("disabled")).toBeDefined();
    expect(w.find(".ec-blocked").text()).toContain("结构性冲突");
    w.unmount();
  });
});

describe("重复点击与读取失败", () => {
  it("决定在飞时重复点击不会再发一次（幂等由条件校验兜底）", async () => {
    let release!: (v: unknown) => void;
    mocks.adoptCandidate.mockImplementationOnce(
      () => new Promise((resolve) => (release = resolve)),
    );

    const w = await mountBlock();
    await expand(w);
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();

    await w.find(".ec-dismiss").trigger("click");
    await w.find(".ec-adopt").trigger("click");
    await flushPromises();
    expect(mocks.adoptCandidate).toHaveBeenCalledTimes(1);
    expect(mocks.dismissCandidate).not.toHaveBeenCalled();

    release({ ok: true, entity: null, adopted: null });
    await flushPromises();
    expect(w.find(".ec-notice").text()).toContain("已采纳");
    w.unmount();
  });

  it("读失败：标题如实写「读取失败」而不是 0，展开可见原因并能重试", async () => {
    mocks.listAllCandidates
      .mockRejectedValueOnce(new Error("GET /api/entities/candidates -> 500"))
      .mockResolvedValueOnce(listing([candidate()]));

    const w = await mountBlock();
    expect(w.find(".ec-title").text()).toContain("读取失败");

    await w.find(".ec-head").trigger("click");
    await flushPromises();
    // 展开即重试：第二次成功 → 显示真实候选
    expect(mocks.listAllCandidates).toHaveBeenCalledTimes(2);
    expect(w.find(".ec-row").text()).toContain("生日");
    w.unmount();
  });

  it("读失败且重试仍失败：面板里给可读原因与重试按钮", async () => {
    mocks.listAllCandidates.mockRejectedValue(new Error("GET /api/entities/candidates -> 500"));

    const w = await mountBlock();
    await expand(w);

    expect(w.find(".ec-error").text()).toContain("加载待处理候选失败");
    expect(w.find(".ec-error").text()).toContain("500");
    expect(w.find(".ec-retry").exists()).toBe(true);
    w.unmount();
  });

  it("清单被截断时如实说明还有多少条未显示", async () => {
    mocks.listAllCandidates.mockResolvedValue(
      listing([candidate()], { total: 7, shown: 1, truncated: true }),
    );
    const w = await mountBlock();
    await expand(w);
    expect(w.find(".ec-title").text()).toBe("待处理候选（7）");
    expect(w.find(".ec-more").text()).toContain("还有 6 条未显示");
    w.unmount();
  });
});
