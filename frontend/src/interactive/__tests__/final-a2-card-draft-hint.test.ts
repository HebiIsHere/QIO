/**
 * [final-A2] 条目 09 / 13 的界面说明必须准确（契约 §11.3 / M7）。
 *
 * 组件/DOM 层（挂载真实 CardDraftHint.vue，仅把 store 换成可控替身）：
 * - 服务器拒绝保存（例如草稿超限）而**本机副本写成功**时：显示真实原因，并如实说明
 *   完整内容已在本机、关掉重开还能继续编辑；提供重试入口。
 * - 本机副本也写失败时：只能说清恢复能力受限，**不许**承诺「关掉重开一定能恢复」。
 * - 清除还没同步成功时：说「还没同步成功」，不说「已清除」，并给重试。
 * - 一切正常时不渲染（不长期占位）。
 * - 文案不出现开发用语。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import CardDraftHint from "../../components/interactive/CardDraftHint.vue";

const stub = vi.hoisted(() => ({
  draftState: { status: "idle", error: null as string | null },
  localState: { ok: true, error: null as string | null },
  removalState: { status: "idle", error: null as string | null },
  conflict: null as { local: string; server: string } | null,
  /** 本机副本「真的没删掉」时的可显示原因（Lead 接线的 store 访问器；null 表示没有这种情况） */
  localRemovalError: null as string | null,
  retryDraftSave: vi.fn(async () => undefined),
  resolveDraftConflict: vi.fn(),
}));

vi.mock("../../stores/interactive", () => ({
  useInteractiveStore: () => ({
    lastDraftKey: "card:c1",
    draftStateFor: () => stub.draftState,
    draftLocalStateFor: () => stub.localState,
    draftRemovalStateFor: () => stub.removalState,
    draftConflictFor: () => stub.conflict,
    draftLocalRemovalErrorFor: () => stub.localRemovalError,
    retryDraftSave: stub.retryDraftSave,
    resolveDraftConflict: stub.resolveDraftConflict,
  }),
}));

function mountHint() {
  return mount(CardDraftHint, { props: { cardId: "c1" } });
}

beforeEach(() => {
  stub.draftState = { status: "idle", error: null };
  stub.localState = { ok: true, error: null };
  stub.removalState = { status: "idle", error: null };
  stub.conflict = null;
  stub.localRemovalError = null;
  stub.retryDraftSave.mockClear();
  stub.resolveDraftConflict.mockClear();
});

describe("[09] 服务器拒绝保存、本机副本完整", () => {
  it("显示真实原因，并如实说明完整内容在本机、重开还能继续编辑；重试入口可用", async () => {
    stub.draftState = { status: "error", error: "草稿超过 20000 字上限，服务器没有保存这一版" };

    const wrapper = mountHint();
    const text = wrapper.text();

    expect(text).toContain("草稿超过 20000 字上限");
    expect(wrapper.find('[data-im="card-draft-error"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-error-local-kept"]').exists()).toBe(true);
    expect(text).toMatch(/完整内容已保留在本机/);
    // 与既有约定一致：这一支说明未保存时必须避免出现「已保存」字样
    expect(text).not.toContain("已保存");
    expect(text).toMatch(/重开|关闭后/);
    // 说实话：这里本机副本写成功了，可以说明恢复能力
    expect(text).not.toMatch(/恢复不到|没有留下恢复副本/);

    await wrapper.find('[data-im="card-draft-retry"]').trigger("click");
    expect(stub.retryDraftSave).toHaveBeenCalledWith("card:c1");
  });

  it("本机副本也写失败时：只说恢复能力受限，不许承诺关掉重开一定能恢复", () => {
    stub.draftState = { status: "error", error: "服务器暂时不可用" };
    stub.localState = { ok: false, error: "本机存储已满，草稿没有保存成功" };

    const text = mountHint().text();

    expect(text).toContain("服务器暂时不可用");
    expect(text).toMatch(/本机也没能留下恢复副本/);
    expect(text).toMatch(/现在关闭页面就恢复不到/);
    expect(text, "本机都没保住，却在界面上承诺关掉重开能恢复").not.toMatch(/完整内容已保留在本机/);
  });
});

describe("[13] 清除同步失败的说明", () => {
  it("说「清除还没同步成功」并给重试，不说「已清除」", async () => {
    stub.removalState = { status: "error", error: "网络不可用" };

    const wrapper = mountHint();
    const text = wrapper.text();

    expect(wrapper.find('[data-im="card-draft-removal-error"]').exists()).toBe(true);
    expect(text).toMatch(/清除还没同步成功/);
    expect(text).toContain("网络不可用");
    expect(text).not.toMatch(/已清除这份|已经清除完成/);

    await wrapper.find('[data-im="card-draft-retry"]').trigger("click");
    expect(stub.retryDraftSave).toHaveBeenCalled();
  });
});

describe("[13] 本机副本真的没删掉时的说明", () => {
  it("非 null：说清「本机副本没能删掉、重开后可能又出现」+ 真实原因 + 重试入口", async () => {
    stub.localRemovalError = "本机存储已满，这条记录没有删掉（清理一些空间后可以重试）";

    const wrapper = mountHint();
    const text = wrapper.text();

    expect(wrapper.find('[data-im="card-draft-local-removal-error"]').exists()).toBe(true);
    expect(text).toMatch(/本机副本没能删掉/);
    expect(text).toMatch(/重开后可能又出现/);
    expect(text).toContain("本机存储已满");
    // 清除同步失败说的是另一件事，不许互相冒充
    expect(wrapper.find('[data-im="card-draft-removal-error"]').exists()).toBe(false);

    const retry = wrapper.find('[data-im="card-draft-local-removal-retry"]');
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    expect(stub.retryDraftSave).toHaveBeenCalledWith("card:c1");
  });

  it("null：什么都不显示（版本守卫有意保留 / 本来就没有记录都不算失败）", () => {
    const wrapper = mountHint();
    expect(wrapper.find('[data-im="card-draft-hint"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="card-draft-local-removal-error"]').exists()).toBe(false);
  });

  it("与「清除还没同步成功」同时出现：两条事实都说清，但只留一个重试入口", () => {
    stub.localRemovalError = "本机存储已满，这条记录没有删掉";
    stub.removalState = { status: "error", error: "网络不可用" };

    const wrapper = mountHint();

    expect(wrapper.find('[data-im="card-draft-local-removal-error"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-removal-error"]').exists()).toBe(true);
    // 同一次重试会一并补做本机删除与网络清除：不重复放按钮
    expect(wrapper.find('[data-im="card-draft-local-removal-retry"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="card-draft-retry"]').exists()).toBe(true);
  });
});

describe("文案与占位", () => {
  it("没有待说明的事就不渲染", () => {
    expect(mountHint().find('[data-im="card-draft-hint"]').exists()).toBe(false);
  });

  it("界面不出现开发用语", () => {
    stub.draftState = { status: "error", error: "服务器暂时不可用" };
    stub.localState = { ok: false, error: "本机存储已满" };
    const text = mountHint().text();
    expect(text).not.toMatch(/adapter|DOM|状态机|求差|payload|schema/);
  });
});
