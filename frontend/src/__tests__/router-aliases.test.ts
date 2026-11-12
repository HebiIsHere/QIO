/**
 * 路由兼容：对话页真实 path 是 "/"，但旧链接/手输深链会是 "#/conversation"。
 * 没有别名时 vue-router 会落到「无匹配路由」：App 没有视图、WindowChrome 的
 * hasGlobalBar=false，于是顶部只剩一个 QIO 字标（用户实际遇到）。
 * 未知深链同样回对话页：单窗口桌面应用里不存在可停留的 404 空白页。
 */
import { describe, expect, it } from "vitest";
import router from "../router";

/**
 * 冷启动时 router.push 会懒加载 ConversationView/InteractiveView（大模块，首次 transform 可达数秒）；
 * 本文件在孤立运行/全量套件高负载时都可能超过默认 5s。这里给单个用例放宽到 20s：
 * 断言的是路由结果，不是加载耗时（超时过紧会让真实回归被误报成 flake）。
 */
const ROUTE_PUSH_TIMEOUT_MS = 20000;

describe("路由深链兼容", () => {
  it("旧的 #/conversation 回到对话页（name=conversation，path=/）", { timeout: ROUTE_PUSH_TIMEOUT_MS }, async () => {
    await router.push("/conversation");
    expect(router.currentRoute.value.name).toBe("conversation");
    expect(router.currentRoute.value.path).toBe("/");
    expect(router.currentRoute.value.fullPath).toBe("/");
  });

  it("未知/过期深链回到对话页，而不是空白页 + 只剩 QIO", { timeout: ROUTE_PUSH_TIMEOUT_MS }, async () => {
    await router.push("/this-route-does-not-exist");
    expect(router.currentRoute.value.name).toBe("conversation");
    expect(router.currentRoute.value.path).toBe("/");
  });

  it("互动页与设置页仍是独立路由（不被 catch-all 吃掉）", { timeout: ROUTE_PUSH_TIMEOUT_MS }, async () => {
    await router.push("/interactive");
    expect(router.currentRoute.value.name).toBe("interactive");
    await router.push("/settings");
    expect(router.currentRoute.value.name).toBe("settings");
  });
});
