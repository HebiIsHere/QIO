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

// 启动即应用持久化主题偏好（system 时按系统解析，并监听系统切换）
applyTheme(resolveTheme(getThemePreference()));
watchSystemTheme();

createApp(App).use(createPinia()).use(router).mount("#app");
