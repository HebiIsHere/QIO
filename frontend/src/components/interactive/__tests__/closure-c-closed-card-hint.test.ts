/**
 * 关闭态卡片：本机副本删除失败 / 清除未同步 / 草稿保存失败必须**看得见、能处理**（本轮 C）。
 *
 * 触发：A 的组件级探针发现，用户在关闭态点「用服务器上的」而本机副本删除失败时，
 * 失败原因与重试入口只在**编辑态**渲染 —— 必须先点「编辑」才看得到，等于不可达。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardCard as BoardCardType } from "../../../interactive/types";

const CARD = {
  id: "c1",
  kind: "text",
  x: 0,
  y: 0,
  w: 280,
  h: 180,
  content: "正文还在卡片里",
  meta: {},
  deleted: false,
  folded: false,
  hidden: false,
  checked: true,
  bookmarked: false,
  createdAt: "2026-10-10T00:00:00.000Z",
  updatedAt: "2026-10-10T00:00:00.000Z",
} as unknown as BoardCardType;

const PROPS = {
  card: CARD,
  selected: false,
  highlight: false,
  dragging: false,
  x: 0,
  y: 0,
  groupName: null,
  groups: [],
  toolbarLeft: 0,
  toolbarTop: 0,
  multi: false,
  connecting: false,
};

beforeEach(() => {
  setActivePinia(createPinia());
  useInteractiveStore().board = board.emptyState("b1");
});

describe("关闭态也能看到并处理本机/草稿异常", () => {
  it("本机副本没删掉：就地显示提示与重试，正文仍在（不是替换正文）", () => {
    const store = useInteractiveStore();
    vi.spyOn(store, "draftLocalRemovalErrorFor").mockReturnValue("本机存储写入失败");
    const w = mount(BoardCard, { props: PROPS });
    expect(w.find('[data-im="card-draft-hint"]').exists()).toBe(true);
    expect(w.text()).toContain("这份本机副本没能删掉");
    expect(w.text()).toContain("正文还在卡片里");
    // 重试入口可达（CardDraftHint 自己那一路重试）
    expect(w.find('[data-im="card-draft-local-removal-retry"]').exists()).toBe(true);
  });

  it("一切都正常时关闭态不占位（不长期挂一行提示）", () => {
    const w = mount(BoardCard, { props: PROPS });
    expect(w.find('[data-im="card-draft-hint"]').exists()).toBe(false);
  });
});
