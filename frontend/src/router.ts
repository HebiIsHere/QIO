import { createRouter, createWebHashHistory } from "vue-router";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", name: "conversation", component: () => import("./views/ConversationView.vue") },
    // 兼容旧链接 / 手输的深链：对话页真正的 path 是 "/"。没有这条时 `#/conversation`
    // 会落到「无匹配路由」→ 页面空、全局栏只剩 QIO 字标（用户反馈过）。
    { path: "/conversation", redirect: "/" },
    // 互动模式与对话模式并列：共同整理材料、关系和工作
    { path: "/interactive", name: "interactive", component: () => import("./views/InteractiveView.vue") },
    { path: "/settings", name: "settings", component: () => import("./views/SettingsView.vue") },
    { path: "/debug", name: "debug", component: () => import("./views/DebugView.vue") },
    // 任何未知/过期深链都回到对话页：单窗口桌面应用里不存在「可停留的 404」，
    // 落在一个没有视图、没有全局栏的空白页对用户没有任何意义。
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

export default router;
