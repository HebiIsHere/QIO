import { createRouter, createWebHashHistory } from "vue-router";

/* ------------------------------------------------------------------
 * 设置页的「进入来源」（#48）
 *
 * 设置是全局顶栏（WindowChrome）里的入口，对话页与互动页都能进；从互动页进入时
 * 返回必须回**原板面**而不是对话页。直达设置（深链 / 启动即 #/settings）没有来源，
 * 默认返回对话页。
 *
 * 记录点放在路由守卫里：任何入口（全局栏、权限模式文字、任务/通知跳转）都经过它，
 * 不需要在每个入口各写一遍（漏一个就会回错页面）。
 * ------------------------------------------------------------------ */
const SETTINGS_SOURCE_PATH_MAX = 2048;
/** 进入设置页前的完整路径；null = 没有可回的原页面（直达设置）→ 默认对话页。 */
let settingsEntryPath: string | null = null;

/**
 * 记录进入设置页的来源。
 *
 * 只有互动页有「需要回去的原板面」；对话页、调试页、未知深链、启动直达都按默认
 * （返回对话）处理 —— 不在离开设置时把用户送到一个意外页面。
 */
export function recordSettingsEntrySource(
  from: { name?: unknown; fullPath?: string } | null | undefined,
): void {
  const name = typeof from?.name === "string" ? from.name : "";
  const fullPath = typeof from?.fullPath === "string" ? from.fullPath : "";
  settingsEntryPath =
    name === "interactive" && fullPath.startsWith("/interactive")
      ? fullPath.slice(0, SETTINGS_SOURCE_PATH_MAX)
      : null;
}

/** 设置页返回的目标路径：有互动来源回原板面（含查询参数），否则回对话页 "/"。 */
export function settingsReturnPath(): string {
  return settingsEntryPath ?? "/";
}

/** 测试隔离用：清掉记录，回到「直达设置」的默认状态。 */
export function resetSettingsEntrySource(): void {
  settingsEntryPath = null;
}

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

/**
 * 进入设置页时记录来源（#48）：任何入口都走同一条守卫，避免漏接线。
 * 离开设置不清除：返回目标以「进入那一刻」为准，设置内部导航/搜索不改变它。
 */
router.beforeEach((to, from) => {
  if (to.name === "settings") recordSettingsEntrySource(from);
  return true;
});

export default router;
