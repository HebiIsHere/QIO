import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import CredentialCard from "../CredentialCard.vue";
import type { CredentialMeta } from "../../../services/api";

const base: CredentialMeta = {
  key_id: "key_demo",
  version: 2,
  tags: ["chat", "code"],
  endpoint: "https://api.openai.com/v1",
  default_model: "gpt-4o-mini",
  budget: 40,
  budget_used: 12,
  status: "active",
  enabled: true,
  note: null,
};

describe("CredentialCard", () => {
  it("渲染名称与类别标签", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, note: "GPT-5 Studio" } } });
    expect(w.find(".name").text()).toBe("GPT-5 Studio");
    const badges = w.findAll(".qio-badge").map((b) => b.text());
    expect(badges).toContain("chat");
    expect(badges).toContain("code");
  });
  it("active+enabled 显示 ✓ 已启用（ok），停用显示 已停用（paused），revoked 显示 ✕ 已撤销（err）", () => {
    const ok = mount(CredentialCard, { props: { credential: { ...base } } });
    expect(ok.find(".status").text()).toContain("✓");
    expect(ok.find(".status").classes()).toContain("ok");
    const paused = mount(CredentialCard, { props: { credential: { ...base, enabled: false } } });
    expect(paused.find(".status").text()).toBe("已停用");
    expect(paused.find(".status").classes()).toContain("paused");
    const err = mount(CredentialCard, { props: { credential: { ...base, status: "revoked" } } });
    expect(err.find(".status").text()).toContain("✕");
    expect(err.find(".status").classes()).toContain("err");
  });
  it("掩码密钥行只读显示（不包含明文）", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    expect(w.find(".key").text()).toContain("••••");
    expect(w.find(".key").text()).not.toContain("sk-");
  });
  it("预算进度条填充宽度与警告态（>=80% 变 warning）", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, budget: 40, budget_used: 12 } } });
    expect(w.find(".fill").attributes("style")).toContain("30%");
    expect(w.find(".fill").classes()).not.toContain("warn");
    const warn = mount(CredentialCard, { props: { credential: { ...base, budget: 20, budget_used: 18 } } });
    expect(warn.find(".fill").classes()).toContain("warn");
    expect(warn.find(".meta").text()).toContain("¥ 18 / 20");
  });
  it("无预算时显示 预算 ∞ 且不渲染进度条", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, budget: null } } });
    expect(w.find(".meta").text()).toContain("预算 ∞");
    expect(w.find(".budget").exists()).toBe(false);
  });
  /**
   * 「撤销」以前在后端有、界面没有入口 —— 「已撤销」筛选与状态都在，
   * 用户却做不到（第三阶段 spec 第 75~79 条要求消除这种能力悬空）。
   */
  it("撤销密钥有入口，且已撤销的凭据不再显示撤销按钮", async () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    const btn = w.find(".btn-revoke");
    expect(btn.exists()).toBe(true);
    expect(btn.text()).toContain("撤销");
    await btn.trigger("click");
    expect(w.emitted("revoke")).toBeTruthy();

    const revoked = mount(CredentialCard, {
      props: { credential: { ...base, status: "revoked" } },
    });
    expect(revoked.find(".btn-revoke").exists()).toBe(false);
  });

  it("测试/编辑/换钥/启停/删除按钮发出对应事件", async () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    // 第四阶段：管理动作收进「管理」区，先展开再用（阅读态不铺按钮）
    await w.find(".btn-manage").trigger("click");
    await w.find(".btn-test").trigger("click");
    await w.find(".btn-meta").trigger("click");
    await w.find(".btn-rotate").trigger("click");
    await w.find(".btn-toggle").trigger("click");
    await w.find(".btn-danger").trigger("click");
    expect(w.emitted("test")).toHaveLength(1);
    expect(w.emitted("edit-meta")).toHaveLength(1);
    expect(w.emitted("rotate")).toHaveLength(1);
    expect(w.emitted("toggle-enabled")).toHaveLength(1);
    expect(w.emitted("remove")).toHaveLength(1);
  });
});

/**
 * 第四阶段：凭据卡也是「一条凭据」，先读后管。
 *
 * 以前一张卡上常驻 7 个按钮（测试/详情/编辑/换钥/停用/撤销/删除），
 * 读一行凭据要在按钮堆里找；现在默认只给结论与一个「管理」入口。
 */
describe("CredentialCard 阅读态与按需管理（第四阶段）", () => {
  it("默认阅读态：管理动作收起，且不可聚焦（inert）", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    const manage = w.find(".manage");
    expect(manage.exists()).toBe(true);
    expect(manage.classes()).not.toContain("open");
    // inert：收起时里面的按钮不能通过 Tab 被聚焦，否则键盘用户会掉进看不见的按钮里
    expect(manage.attributes("inert")).toBeDefined();
  });

  it("阅读态给的是结论：名称 / 状态徽章 / 掩码 / 预算，而不是一堆控件", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, note: "生产主钥" } } });
    expect(w.find(".name").text()).toBe("生产主钥");
    expect(w.find(".status").classes()).toContain("qio-state");
    expect(w.find(".key").text()).toContain("••••");
    expect(w.find(".meta").text()).toContain("预算");
  });

  it("「管理」入口在阅读态就可见（不是 hover 才出现，触屏与键盘都够得到）", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    expect(w.find(".btn-manage").exists()).toBe(true);
    expect(w.find(".btn-manage").text()).toContain("管理");
  });

  it("点「管理」展开动作并可再次收起", async () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    await w.find(".btn-manage").trigger("click");
    expect(w.find(".manage").classes()).toContain("open");
    expect(w.find(".manage").attributes("inert")).toBeUndefined();
    await w.find(".btn-manage").trigger("click");
    expect(w.find(".manage").classes()).not.toContain("open");
  });
});
