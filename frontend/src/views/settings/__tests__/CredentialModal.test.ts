import { describe, expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import CredentialModal from "../CredentialModal.vue";

describe("CredentialModal 用途标签", () => {
  it("meta 模式：初始 tags 回填，勾选后提交发出对应 tags", async () => {
    const w = mount(CredentialModal, {
      props: { mode: "meta", initial: { tags: ["main-loop"] } },
    });
    await flushPromises();

    // 初始「主对话」应处于选中态
    const main = w.findAll(".tag-chip").find((c) => c.text().includes("主对话"));
    expect(main).toBeTruthy();
    expect(main!.classes()).toContain("on");

    // 点选「看图/视觉」
    const vision = w.findAll(".tag-chip").find((c) => c.text().includes("看图"));
    await vision!.trigger("click");

    await w.find(".btn-submit").trigger("click");
    const payload = (w.emitted("save") as unknown[])[0] as [Record<string, unknown>];
    expect(payload[0].tags).toEqual(expect.arrayContaining(["main-loop", "vision"]));
  });

  it("未选任何用途时提交被拦截", async () => {
    const w = mount(CredentialModal, { props: { mode: "meta", initial: { tags: [] } } });
    await flushPromises();
    await w.find(".btn-submit").trigger("click");
    expect(w.find(".msg.err").text()).toContain("用途");
  });
});
