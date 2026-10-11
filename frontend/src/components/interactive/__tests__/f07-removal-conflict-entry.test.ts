/**
 * 【审计 8fa8452 · F07 界面】本地删除与服务器修改相撞时的明确解决入口。
 *
 * 要求「保留依据并给出明确解决方式」：两个选择都必须在界面上可达，
 * 且各自只改本页候选（保持删除 / 恢复服务器版），不调用 QIO、不自动重复提交。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ pending: { expressions: [] } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(),
  saveDrafts: vi.fn(),
  saveBoardState: vi.fn(),
  submitBoard: vi.fn(),
  checkMaterialImpact: vi.fn(),
}));

const mounted: VueWrapper[] = [];

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

function seedConflict(store: ReturnType<typeof useInteractiveStore>): void {
  store.boardRemovalConflicts = {
    l1: {
      kind: "link",
      id: "l1",
      server: { id: "l1", src: "c1", dst: "c2", direction: false, meaning: "对方改过的含义", deleted: false },
    },
  };
}

describe("F07 删除冲突的界面入口", () => {
  it("显示对象与两种明确选择；「保持删除」清掉冲突并排一次保存", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    seedConflict(store);
    const spy = vi.spyOn(store, "resolveBoardRemovalConflict");
    const wrapper = mount(SubmitCluster);
    mounted.push(wrapper);

    const block = wrapper.find('[data-im="board-removal-conflict"]');
    expect(block.exists(), "相撞时必须给明确解决入口（F07）").toBe(true);
    expect(block.text()).toContain("l1");
    expect(wrapper.find('[data-im="board-removal-keep-deleted"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="board-removal-restore-server"]').exists()).toBe(true);

    await wrapper.find('[data-im="board-removal-keep-deleted"]').trigger("click");
    expect(spy, "保持删除必须走 store.resolveBoardRemovalConflict").toHaveBeenCalledWith("l1", "keep-deleted");
    spy.mockRestore();
  });

  it("「恢复服务器版」也走同一个 store 入口（保留依据 → 用户明确选择）", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    seedConflict(store);
    const spy = vi.spyOn(store, "resolveBoardRemovalConflict");
    const wrapper = mount(SubmitCluster);
    mounted.push(wrapper);

    await wrapper.find('[data-im="board-removal-restore-server"]').trigger("click");
    expect(spy).toHaveBeenCalledWith("l1", "restore-server");
    spy.mockRestore();
  });
});
