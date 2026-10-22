<script setup lang="ts">
/**
 * 右侧只读文件预览（C 负责完整实现；这里先保留空态与基本开关，保证基线可编译）。
 *
 * 完整版要求：多标签（身份不是文件名）、各标签阅读位置、图片/PDF/DOCX/XLSX/PPTX/
 * 文本/代码/Markdown 只读阅读、搜索/复制/缩放/翻页/外部打开、全窗口预览与返回、
 * 与中央区并排可拖宽、文件写入结束后自动更新且不抢当前标签、资源释放。
 */
import { computed } from "vue";
import { useConversationShellStore } from "../../stores/conversationShell";

defineProps<{ full?: boolean }>();
const emit = defineEmits<{ close: []; toggleFull: [] }>();
const shell = useConversationShellStore();
const active = computed(
  () => shell.prefs.previewTabs.find((tab) => tab.id === shell.prefs.activePreviewTabId) ?? null,
);
</script>

<template>
  <aside class="preview-panel" :class="{ full }" aria-label="文件预览">
    <div class="preview-top">
      <span class="preview-label">文件预览 <span class="mono">只读</span></span>
      <div class="preview-actions">
        <button class="qio-btn quiet mini" type="button" @click="emit('toggleFull')">
          {{ full ? "返回对话" : "全窗口" }}
        </button>
        <button class="qio-btn quiet mini" type="button" aria-label="收起预览栏" @click="emit('close')">
          收起
        </button>
      </div>
    </div>
    <div v-if="shell.prefs.previewTabs.length" class="preview-tabs" role="tablist" aria-label="已打开的文件">
      <button
        v-for="tab in shell.prefs.previewTabs"
        :key="tab.id"
        type="button"
        role="tab"
        class="preview-tab"
        :aria-selected="tab.id === shell.prefs.activePreviewTabId"
        @click="shell.setActivePreviewTab(tab.id)"
      >
        {{ tab.target.title }}
      </button>
    </div>
    <div class="preview-body">
      <p v-if="!active" class="empty">打开文件后在这里查看</p>
      <p v-else class="empty">正在准备预览（C 正在实现渲染器）</p>
    </div>
  </aside>
</template>

<style scoped>
.preview-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-surface);
  border-left: 1px solid var(--border-subtle);
}
.preview-panel.full {
  position: absolute;
  inset: 0;
  z-index: 30;
  border-left: 0;
}
.preview-top {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
}
.preview-label {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.preview-label .mono { font-size: 10px; color: var(--text-muted); margin-left: 4px; }
.preview-actions { display: flex; gap: 4px; }
.preview-tabs {
  flex: none;
  display: flex;
  gap: 2px;
  overflow-x: auto;
  padding: 0 10px;
  border-bottom: 1px solid var(--border-subtle);
}
.preview-tab {
  flex: none;
  max-width: 180px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  padding: 7px 6px;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.preview-tab[aria-selected="true"] {
  color: var(--text-strong);
  border-bottom-color: var(--accent);
}
.preview-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px;
}
.empty {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
</style>
