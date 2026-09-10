import { createRouter, createWebHashHistory } from "vue-router";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", name: "conversation", component: () => import("./views/ConversationView.vue") },
    { path: "/settings", name: "settings", component: () => import("./views/SettingsView.vue") },
    { path: "/debug", name: "debug", component: () => import("./views/DebugView.vue") },
  ],
});

export default router;
