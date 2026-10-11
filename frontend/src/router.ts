import { createRouter, createWebHashHistory } from "vue-router";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", name: "conversation", component: () => import("./views/ConversationView.vue") },
    // 互动模式与对话模式并列：共同整理材料、关系和工作
    { path: "/interactive", name: "interactive", component: () => import("./views/InteractiveView.vue") },
    { path: "/settings", name: "settings", component: () => import("./views/SettingsView.vue") },
    { path: "/debug", name: "debug", component: () => import("./views/DebugView.vue") },
  ],
});

export default router;
