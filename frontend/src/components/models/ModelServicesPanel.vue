<script setup lang="ts">
/**
 * 占位文件，集成时由 B 的版本整体替换。
 *
 * 真正的面板（Key 接入与管理、默认模型、用途分配、模型目录与按模型验证）由
 * B 在 `frontend/src/components/models/ModelServicesPanel.vue` 提供；
 * A 只负责「模型与服务」容器（标题、搜索锚点、错误边界）与联网搜索配置。
 * 这里放最小占位是为了让 A 的分支能独立通过类型检查与导航/搜索测试。
 */
</script>

<template>
  <p class="models-panel-placeholder" data-testid="models-panel-placeholder">
    模型与服务面板占位：集成时由 B 的 ModelServicesPanel 整体替换。
  </p>
</template>
