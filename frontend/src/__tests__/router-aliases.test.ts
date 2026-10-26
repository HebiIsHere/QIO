/**
 * 路由兼容：对话页真实 path 是 "/"，但旧链接/手输深链会是 "#/conversation"。
 * 没有别名时 vue-router 会落到「无匹配路由」：App 没有视图、WindowChrome 的
 * hasGlobalBar=false，于是顶部只剩一个 QIO 字标（用户实际遇到）。
 * 未知深链同样回对话页：单窗口桌面应用里不存在可停留的 404 空白页。
 */
import { describe, expect, it } from "vitest";
import router from "../router";

describe("路由深链兼容", () => {
  it("旧的 #/conversation 回到对话页（name=conversation，path=/）", async () => {
    await router.push("/conversation");
    expect(router.currentRoute.value.name).toBe("conversation");
    expect(router.currentRoute.value.path).toBe("/");
    expect(router.currentRoute.value.fullPath).toBe("/");
  });

  it("未知/过期深链回到对话页，而不是空白页 + 只剩 QIO", async () => {
    await router.push("/this-route-does-not-exist");
    expect(router.currentRoute.value.name).toBe("conversation");
    expect(router.currentRoute.value.path).toBe("/");
  });

  it("互动页与设置页仍是独立路由（不被 catch-all 吃掉）", async () => {
    await router.push("/interactive");
    expect(router.currentRoute.value.name).toBe("interactive");
    await router.push("/settings");
    expect(router.currentRoute.value.name).toBe("settings");
  });
});
