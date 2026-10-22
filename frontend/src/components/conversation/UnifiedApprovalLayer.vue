<script setup lang="ts">
/**
 * 统一审批层（D 负责完整实现；这里先复用既有审批入口/弹窗，保证基线不丢能力）。
 *
 * 完整版：所有单次申请用同一固定位置弹窗；按类型分派真实处理接口（工具/权限 vs 知识）；
 * 队列一次一项、可切换、新申请不替换正在编辑的；关闭只隐藏；暂缓不重复自动弹；
 * 知识保存提供「确认保存 / 修改后保存 / 不保存」，修改后保存必须原子生效。
 */
import ApprovalEntry from "../ApprovalEntry.vue";
import ApprovalModal from "../ApprovalModal.vue";
</script>

<template>
  <div class="unified-approval-layer">
    <ApprovalEntry />
    <ApprovalModal />
  </div>
</template>
