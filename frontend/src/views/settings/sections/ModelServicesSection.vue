<script setup lang="ts">
/**
 * 模型与服务：只做容器（外壳、标题、搜索锚点、错误边界），主体是 B 提供的
 * `components/models/ModelServicesPanel.vue`（Key 接入与管理、默认模型、用途分配、
 * 模型目录与按模型验证）。
 *
 * 为什么容器要自己捕获错误：模型面板依赖目录接口与验证状态，任何一处渲染异常都不该
 * 把整个设置页打成白屏 —— 搜索配置、常规、工具与权限都不依赖它。
 * 出错时只把这一块换成自绘的错误提示，其余分区继续可用。
 *
 * 联网搜索的配置界面由 A 放在本容器的第二块（§9 契约，内容在 A 的 SearchConfigGroup）。
 */
import { onErrorCaptured, ref } from "vue";
import ModelServicesPanel from "../../../components/models/ModelServicesPanel.vue";
import { settingsErrorText } from "../../../composables/useQioSettingsSave";
import SearchConfigGroup from "./SearchConfigGroup.vue";

const panelError = ref<string | null>(null);

onErrorCaptured((error) => {
  panelError.value = settingsErrorText(error);
  // 返回 false：错误已在这里处理，不再向上冒泡把整页打掉
  return false;
});
</script>

<template>
  <section class="set-section" data-settings-section="models" aria-label="模型与服务设置">
    <div class="set-block" data-settings-key="models.panel">
      <header class="set-section__head">
        <h2>模型与 Key</h2>
        <p class="set-section__desc">
          密钥只写不读：保存后不再显示明文，只写进本机的系统凭据库（keyring）。
          在 QIO 里停用、撤销或删除凭据都不会去厂商那边吊销 API Key。
        </p>
      </header>
      <div v-if="panelError" class="set-panel-error" role="alert" data-testid="settings-models-error">
        <div class="set-panel-error__title">模型与服务面板暂时无法显示</div>
        <div class="set-panel-error__desc">
          原因：{{ panelError }}。其余设置仍然可用；可稍后切回本分类重试。
        </div>
      </div>
      <ModelServicesPanel v-else />
    </div>

    <SearchConfigGroup />
  </section>
</template>
