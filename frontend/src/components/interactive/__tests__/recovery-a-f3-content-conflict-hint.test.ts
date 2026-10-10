/**
 * [recovery-A / F3] CardDraftHint 的「正文冲突」入口（真实组件 DOM，store 用可控替身）。
 *
 * F3：本页候选正文与服务器上**同一张卡片**的正文都有改动时，必须由用户决定保留哪一份，
 * 不许静默覆盖。本文件验证：
 * - 冲突存在时渲染入口（关闭态也要可见可点，由 BoardCard 的守卫保证）；
 * - 两个按钮分别调用 store.resolveBoardContentConflict(cardId, "local" | "server")；
 * - 与既有「草稿冲突」块可以并存，各自说清、互不冒充；
 * - 没有冲突 / 接口未提供时不渲染（不长期占位），沿用既有冲突块样式与文案层级。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import CardDraftHint from "../CardDraftHint.vue";

const stub = vi.hoisted(() => ({
  draftState: { status: "idle", error: null as string | null },
  localState: { ok: true, error: null as string | null },
  removalState: { status: "idle", error: null as string | null },
  conflict: null as { local: string; server: string } | null,
  contentConflict: null as { local: string; server: string } | null,
  localRemovalError: null as string | null,
  retryDraftSave: vi.fn(async () => undefined),
  resolveDraftConflict: vi.fn(),
  resolveBoardContentConflict: vi.fn(),
}));

vi.mock("../../../stores/interactive", () => ({
  useInteractiveStore: () => ({
    lastDraftKey: "card:c1",
    draftStateFor: () => stub.draftState,
    draftLocalStateFor: () => stub.localState,
    draftRemovalStateFor: () => stub.removalState,
    draftConflictFor: () => stub.conflict,
    draftLocalRemovalErrorFor: () => stub.localRemovalError,
    boardContentConflictFor: () => stub.contentConflict,
    retryDraftSave: stub.retryDraftSave,
    resolveDraftConflict: stub.resolveDraftConflict,
    resolveBoardContentConflict: stub.resolveBoardContentConflict,
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
  stub.contentConflict = null;
  stub.localRemovalError = null;
  stub.retryDraftSave.mockClear();
  stub.resolveDraftConflict.mockClear();
  stub.resolveBoardContentConflict.mockClear();
});

describe("F3 正文冲突入口", () => {
  it("冲突存在时渲染：说清两份都保留、必须由用户选择", () => {
    stub.contentConflict = { local: "本页改过的正文", server: "服务器上的正文" };
    const wrapper = mountHint();
    const text = wrapper.text();

    expect(wrapper.find('[data-im="card-content-conflict"]').exists()).toBe(true);
    expect(text).toMatch(/都有改动/);
    expect(text).toMatch(/两份都留着/);
    expect(text).toMatch(/先选一份继续/);
    // 不许出现「已保存 / 已清除」这类结论性字样（冲突还没解决）
    expect(text).not.toMatch(/已保存|已清除/);
  });

  it("两个按钮分别调用 store.resolveBoardContentConflict(cardId, 选择)", async () => {
    stub.contentConflict = { local: "本页改过的正文", server: "服务器上的正文" };
    const wrapper = mountHint();

    await wrapper.find('[data-im="card-content-keep-local"]').trigger("click");
    expect(stub.resolveBoardContentConflict).toHaveBeenLastCalledWith("c1", "local");
    // 这是**另一类**冲突：不许走草稿冲突的选择入口
    expect(stub.resolveDraftConflict).not.toHaveBeenCalled();

    await wrapper.find('[data-im="card-content-keep-server"]').trigger("click");
    expect(stub.resolveBoardContentConflict).toHaveBeenLastCalledWith("c1", "server");
  });

  it("与既有草稿冲突块并存：两块各自可操作，互不冒充", async () => {
    stub.conflict = { local: "本机草稿", server: "服务器草稿" };
    stub.contentConflict = { local: "本页正文", server: "服务器正文" };
    const wrapper = mountHint();

    expect(wrapper.find('[data-im="card-draft-conflict"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-content-conflict"]').exists()).toBe(true);

    await wrapper.find('[data-im="card-content-keep-server"]').trigger("click");
    expect(stub.resolveBoardContentConflict).toHaveBeenCalledWith("c1", "server");
    expect(stub.resolveDraftConflict).not.toHaveBeenCalled();

    await wrapper.find('[data-im="card-draft-keep-local"]').trigger("click");
    expect(stub.resolveDraftConflict).toHaveBeenCalledWith("c1", "local");
  });

  it("没有冲突时不渲染这一块（也保留一切正常时不渲染的既有行为）", () => {
    expect(mountHint().find('[data-im="card-content-conflict"]').exists()).toBe(false);
    expect(mountHint().find('[data-im="card-draft-hint"]').exists()).toBe(false);
  });
});
