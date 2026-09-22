import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import router from "./router";
import "./styles/tokens.css";
import "./styles/base.css";
// 中文衬线声部：本地打包 Noto Serif SC（SIL OFL 1.1，@fontsource 分发，
// unicode-range 分片按需加载，离线可用；不引入网络字体）
import "@fontsource/noto-serif-sc/400.css";
import "@fontsource/noto-serif-sc/600.css";
import { getThemePreference, resolveTheme, applyTheme, watchSystemTheme } from "./utils/theme";
import { getMotionPreference, resolveMotion, applyMotion, watchSystemMotion } from "./utils/motion";
import { useUpdaterStore } from "./stores/updater";

// 启动即应用持久化主题偏好（system 时按系统解析，并监听系统切换）
applyTheme(resolveTheme(getThemePreference()));
watchSystemTheme();
// 动画偏好同理：落成 html[data-motion]，CSS 与星球脚本动画读同一份结果
applyMotion(resolveMotion(getMotionPreference()));
watchSystemMotion();

const pinia = createPinia();
createApp(App).use(pinia).use(router).mount("#app");

// 启动后的静默更新检查（10s 首检 + 24h 周期；设置里可关）。
// 只在 Tauri 壳里生效，浏览器开发预览不会请求更新源。
useUpdaterStore(pinia).startAutoCheck();
