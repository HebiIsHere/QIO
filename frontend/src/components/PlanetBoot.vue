<script setup lang="ts">
/**
 * 星球页的懒加载占位：three.js 不进首屏 chunk，点击入口后到页面挂载之间有一小段
 * 代码下载时间。这段时间必须立刻有反馈（入口已响应、正在打开），而不是空白。
 *
 * 第四阶段：它不能是一整块不透明底色（那会让「星球从入口长大」变成「先黑屏再出现」）。
 * 现在只铺一层半透明底 + 一行状态文字，入口小球仍然在它上面可见，
 * 视觉上等于「这个对象已经在展开的路上」。
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
  align-items: flex-end;
  justify-content: center;
  padding-bottom: 18vh;
  /* 半透明铺底：让入口小球与对话页仍然透出来，开局不闪一块纯色 */
  background: var(--bg-overlay);
  backdrop-filter: blur(3px) saturate(0.94);
  -webkit-backdrop-filter: blur(3px) saturate(0.94);
  color: var(--text-secondary);
  font-size: 13px;
  pointer-events: none;
  /* 与星球页同一套铺底时长：先轻退，再交出内容 */
  animation: planet-boot-in var(--dur-planet-backdrop) var(--ease-1) both;
}
@keyframes planet-boot-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>
