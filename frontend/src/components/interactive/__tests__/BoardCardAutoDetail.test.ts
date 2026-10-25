import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-24T00:00:00.000Z";

function textCard(w: number, h: number): BoardCardModel {
  return {
    id: "card_detail",
    kind: "text",
    content: "第一段正文。\n\n第二段正文，用于验证详细状态。",
    meta: {},
    x: 40,
    y: 40,
    w,
    h,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

describe("BoardCard：尺寸驱动的详细状态", () => {
  let wrapper: VueWrapper | null = null;
  beforeEach(() => {
    setActivePinia(createPinia());
    const store = useInteractiveStore();
    store.board = emptyState("board_default");
    store.board.cards = [textCard(420, 280)];
  });
  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
  });

  function mountAt(card: BoardCardModel): VueWrapper {
    wrapper = mount(BoardCard, {
      props: {
        card,
        selected: false,
        highlight: false,
        dragging: false,
        x: card.x,
        y: card.y,
        groupName: null,
        groups: [],
        toolbarLeft: 0,
        toolbarTop: 0,
        multi: false,
        connecting: false,
      },
      attachTo: document.body,
    });
    return wrapper;
  }

  it("宽度和高度达到阈值时直接进入详细状态，且没有展开预览按钮", async () => {
    const card = textCard(420, 280);
    const view = mountAt(card);
    await view.vm.$nextTick();
    expect(view.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
    expect(view.find('[data-im="card-preview-host"]').exists()).toBe(true);
    expect(view.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
  });

  it("已详细时缩到缓冲区仍保持详细，低于退出阈值才回到简洁", async () => {
    const card = textCard(420, 280);
    const view = mountAt(card);
    await view.vm.$nextTick();
    await view.setProps({ card: { ...card, w: 380, h: 250 } });
    await view.vm.$nextTick();
    expect(view.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");

    await view.setProps({ card: { ...card, w: 350, h: 250 } });
    await view.vm.$nextTick();
    expect(view.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(view.find('[data-im="card-preview-host"]').exists()).toBe(false);
  });

  it("小卡片仍是简洁状态，不被自动展开", async () => {
    const view = mountAt(textCard(300, 180));
    await view.vm.$nextTick();
    expect(view.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(view.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
  });
});
