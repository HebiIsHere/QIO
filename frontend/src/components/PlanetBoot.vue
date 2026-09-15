<script setup lang="ts">
/**
 * 星球页的懒加载占位：three.js 不进首屏 chunk，点击入口后到页面挂载之间有一小段
 * 代码下载时间。这段时间必须立刻有反馈（入口已响应、正在打开），而不是空白。
 */
</script>

<template>
  <div class="planet-boot" role="status">
    <span class="boot-text">正在打开星球…</span>
  </div>
</template>

<style scoped>
.planet-boot {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-base);
  color: var(--text-secondary);
  font-size: 13px;
  /* 与星球页同一套铺底时长：先盖住聊天，再交出内容 */
  animation: planet-boot-in var(--dur-planet-backdrop) var(--ease-out) both;
}
@keyframes planet-boot-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>
