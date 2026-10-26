/**
 * 在没有 App.vue 的场景（ui-catalog 画廊、独立演示页、需要真实浮层的单测）
 * 注册唯一的 QTipLayer。
 *
 * 产品里由 Lead 在 App.vue 挂一次即可；这里会先检查
 * `[data-qio-tip-layer]` 是否已存在，存在就什么都不做，不会出现第二层。
 */
import { createApp, type App } from "vue";
import QTipLayer from "./QTipLayer.vue";
import { TIP_LAYER_ATTRIBUTE } from "../../composables/useQioTip";

let layerApp: App | null = null;
let layerHost: HTMLElement | null = null;

export function ensureQioTipLayer(): void {
  if (typeof document === "undefined") return;
  if (document.querySelector(`[${TIP_LAYER_ATTRIBUTE}]`)) return;
  layerHost = document.createElement("div");
  layerHost.setAttribute("data-qio-tip-host", "");
  document.body.appendChild(layerHost);
  layerApp = createApp(QTipLayer);
  layerApp.mount(layerHost);
}

export function unmountQioTipLayer(): void {
  layerApp?.unmount();
  layerApp = null;
  layerHost?.remove();
  layerHost = null;
}
