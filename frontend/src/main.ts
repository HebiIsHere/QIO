import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import router from "./router";
import "./styles/tokens.css";
import "./styles/base.css";
import { getTheme, setTheme } from "./utils/theme";

// 启动即应用持久化主题（对话页状态条已移除，不再由组件兜底）
setTheme(getTheme());

createApp(App).use(createPinia()).use(router).mount("#app");
