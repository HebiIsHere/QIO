<script setup lang="ts">
/**
 * 预览栏里的结构化文档阅读面：DOCX / XLSX / PPTX（只读，不是原版面渲染）。
 *
 * 诚实口径：
 *   * 这里显示的是后端解析出的**结构化正文**，不能声称与原始 Office 排版完全一致；
 *   * 表格不计算公式：公式原文与「上次保存的缓存值」分开显示；
 *   * 宽表格 / 长代码在**面板内部**滚动，页面本身不出现横向滚动。
 */
import { computed } from "vue";
import type { DocxView } from "../../../interactive/renderers/docx";
import type { PptxView } from "../../../interactive/renderers/pptx";
import type { XlsxView } from "../../../interactive/renderers/xlsx";
import {
  STRUCTURED_SCOPE_NOTE,
  highlightSegments,
  type PreviewSearchHit,
} from "../../../conversation/preview";
import { vQioScrollbar } from "../../../composables/useQioScrollbars";

const props = defineProps<{
  kind: "docx" | "xlsx" | "pptx";
  docx: DocxView | null;
  xlsx: XlsxView | null;
  pptx: PptxView | null;
  zoom: number;
  /** PPTX：当前幻灯片序号（slide.index，1 起）。 */
  page: number;
  /** XLSX：当前工作表名。 */
  sheet: string;
  query: string;
  activeHit: PreviewSearchHit | null;
}>();

const emit = defineEmits<{
  "sheet-change": [name: string];
  "page-change": [page: number];
}>();

const currentSheet = computed(() => {
  const sheets = props.xlsx?.sheets ?? [];
  if (!sheets.length) return null;
  return sheets.find((item) => item.name === props.sheet) ?? sheets[0];
});

const currentSheetIndex = computed(() => {
  const sheets = props.xlsx?.sheets ?? [];
  return currentSheet.value ? sheets.indexOf(currentSheet.value) : -1;
});

const currentSlide = computed(() => {
  const slides = props.pptx?.slides ?? [];
  if (!slides.length) return null;
  return slides.find((slide) => slide.index === props.page) ?? slides[0];
});

function isActiveBlock(index: number): boolean {
  return props.activeHit?.block === index;
}

function isActiveCell(ref: string): boolean {
  return props.activeHit?.cell === ref && props.activeHit?.sheet === currentSheet.value?.name;
}

function isActiveSlide(page: number): boolean {
  return props.activeHit?.page === page;
}

function blockTag(type: "heading" | "paragraph" | "list"): string {
  if (type === "heading") return "h4";
  return "p";
}
</script>

<template>
  <div class="pv-structured" :data-pv-kind="kind" :style="{ '--pv-scale': zoom }">
    <!-- DOCX：结构化正文（段落 / 标题 / 列表 / 表格） -->
    <template v-if="kind === 'docx'">
      <p class="pv-scope">Word 文档 · {{ STRUCTURED_SCOPE_NOTE }}</p>
      <template v-if="docx">
        <p v-if="docx.empty" class="pv-empty">这份文档没有可显示的正文段落（可能是空文档或只含图片）</p>
        <template v-for="(block, index) in docx.blocks" :key="index">
          <component
            :is="blockTag(block.type)"
            class="pv-block"
            :class="['is-' + block.type, { 'is-hit': isActiveBlock(index) }]"
            :data-block="index"
          >
            <span v-if="block.type === 'list'" class="pv-marker mono" :title="block.ordered === null ? '编号未知（文件里没有说明编号格式）' : ''">{{ block.marker }}</span>
            <component
              :is="run.bold ? 'strong' : run.italic ? 'em' : 'span'"
              v-for="(run, runIndex) in block.runs"
              :key="runIndex"
            ><template
              v-for="(segment, segmentIndex) in highlightSegments(run.text, query)"
              :key="segmentIndex"
            ><mark v-if="segment.hit" class="pv-hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></component>
          </component>
        </template>
        <div class="pv-table-pan" v-qio-scrollbar="{ policy: 'auto', axis: 'x' }">
          <table
            v-for="(table, tableIndex) in docx.tables"
            :key="'t' + tableIndex"
            class="pv-table"
            :data-table="tableIndex"
          >
            <tbody>
              <tr v-for="(row, rowIndex) in table.rows" :key="rowIndex" :data-row="rowIndex">
                <td
                  v-for="(cell, cellIndex) in row"
                  :key="cellIndex"
                  :class="{ 'is-hit': activeHit?.table === tableIndex && activeHit?.row === rowIndex }"
                ><template
                  v-for="(segment, segmentIndex) in highlightSegments(cell, query)"
                  :key="segmentIndex"
                ><mark v-if="segment.hit" class="pv-hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </template>

    <!-- XLSX：工作表切换 + 单元格地址 + 公式/缓存值分开 -->
    <template v-else-if="kind === 'xlsx'">
      <p class="pv-scope">
        表格 · {{ STRUCTURED_SCOPE_NOTE }} · 不计算公式（公式显示原文，缓存值是文件上次保存的结果）
      </p>
      <template v-if="xlsx">
        <div class="pv-sheets" role="tablist" aria-label="工作表">
          <button
            v-for="(sheet, index) in xlsx.sheets"
            :key="sheet.name"
            type="button"
            role="tab"
            class="pv-sheet-tab mono"
            :class="{ 'is-active': index === currentSheetIndex }"
            :aria-selected="index === currentSheetIndex"
            @click="emit('sheet-change', sheet.name)"
          >{{ sheet.name }}</button>
        </div>
        <p v-if="xlsx.empty || !currentSheet" class="pv-empty">这个工作簿没有可显示的内容</p>
        <template v-else>
          <p class="pv-fact">
            共 {{ xlsx.sheets.length }} 个工作表 · {{ currentSheet.rowCount }} 行 · {{ currentSheet.columnCount }} 列
          </p>
          <div class="pv-grid-pan" data-pv-grid v-qio-scrollbar="{ policy: 'auto', axis: 'both' }">
            <table class="pv-grid mono">
              <thead>
                <tr>
                  <th class="pv-grid-corner" aria-hidden="true"></th>
                  <th v-for="column in currentSheet.columns" :key="column" scope="col">{{ column }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in currentSheet.rows" :key="row.index" :data-row="row.index">
                  <th scope="row">{{ row.index }}</th>
                  <td
                    v-for="(cell, cellIndex) in row.cells"
                    :key="cellIndex"
                    :data-cell="cell ? currentSheet.name + '!' + cell.ref : undefined"
                    :class="{ 'is-hit': cell ? isActiveCell(cell.ref) : false, 'is-formula': cell?.kind === 'formula' }"
                    :title="cell?.kind === 'formula' ? cell.formulaNote : undefined"
                  ><template v-if="cell"><template
                    v-for="(segment, segmentIndex) in highlightSegments(cell.display, query)"
                    :key="segmentIndex"
                  ><mark v-if="segment.hit" class="pv-hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></template></td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </template>
    </template>

    <!-- PPTX：当前幻灯片（正文 / 备注 / 表格 / 图片清单） -->
    <template v-else>
      <p class="pv-scope">演示文稿 · {{ STRUCTURED_SCOPE_NOTE }}</p>
      <template v-if="pptx">
        <p v-if="pptx.empty" class="pv-empty">这份演示文稿没有可显示的幻灯片</p>
        <template v-else-if="currentSlide">
          <p class="pv-fact mono">
            第 {{ currentSlide.index }} / {{ pptx.slides.length }} 张幻灯片
          </p>
          <article
            class="pv-slide"
            :class="{ 'is-hit': isActiveSlide(currentSlide.index) }"
            :data-slide="currentSlide.index"
          >
            <h3 v-if="currentSlide.title" class="pv-slide-title">{{ currentSlide.title }}</h3>
            <p
              v-for="(text, index) in currentSlide.texts"
              :key="index"
              class="pv-slide-text"
            ><template
              v-for="(segment, segmentIndex) in highlightSegments(text, query)"
              :key="segmentIndex"
            ><mark v-if="segment.hit" class="pv-hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></p>
            <div
              v-for="(table, tableIndex) in currentSlide.tables"
              :key="'t' + tableIndex"
              class="pv-table-pan"
              v-qio-scrollbar="{ policy: 'auto', axis: 'x' }"
            >
              <table class="pv-table">
                <tbody>
                  <tr v-for="(row, rowIndex) in table" :key="rowIndex">
                    <td
                      v-for="(cell, cellIndex) in row"
                      :key="cellIndex"
                    ><template
                      v-for="(segment, segmentIndex) in highlightSegments(String(cell), query)"
                      :key="segmentIndex"
                    ><mark v-if="segment.hit" class="pv-hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="currentSlide.notes.length" class="pv-notes">
              <p class="pv-notes-label mono">备注</p>
              <p v-for="(note, index) in currentSlide.notes" :key="'n' + index" class="pv-note-text">{{ note }}</p>
            </div>
            <p v-if="currentSlide.images.length" class="pv-fact">
              这一页含 {{ currentSlide.images.length }} 张图片（{{ currentSlide.images.map((image) => image.size).join(" / ") }}）：图片不在结构化预览里展开
            </p>
          </article>
        </template>
      </template>
    </template>
  </div>
</template>

<style scoped>
.pv-structured {
  --pv-scale: 1;
  min-width: 0;
  font-size: calc(var(--fs-base) * var(--pv-scale));
  color: var(--text-primary);
  line-height: var(--lh-base);
}
.pv-scope,
.pv-fact {
  margin: 0 0 var(--sp-2);
  color: var(--text-muted);
  font-size: var(--fs-tech);
  overflow-wrap: anywhere;
}
.pv-empty {
  margin: var(--sp-3) 0;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.pv-block {
  margin: 0 0 var(--sp-2);
  overflow-wrap: anywhere;
}
.pv-block.is-heading {
  font-family: var(--serif);
  color: var(--text-strong);
  line-height: var(--lh-tight);
  margin-top: var(--sp-3);
}
.pv-block.is-list {
  padding-left: var(--sp-2);
}
.pv-marker {
  display: inline-block;
  min-width: 1.6em;
  color: var(--text-muted);
  font-size: var(--fs-tech);
}
.pv-block.is-hit,
.pv-slide.is-hit {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: var(--r-xs);
}
.pv-table-pan {
  max-width: 100%;
  overflow-x: auto;
  margin: var(--sp-2) 0;
}
.pv-table {
  border-collapse: collapse;
  border: 1px solid var(--border-subtle);
  font-size: calc(var(--fs-sm) * var(--pv-scale));
}
.pv-table td {
  border: 1px solid var(--border-subtle);
  padding: var(--sp-1) var(--sp-2);
  vertical-align: top;
  text-align: left;
  overflow-wrap: anywhere;
}
.pv-sheets {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
  margin-bottom: var(--sp-2);
}
.pv-sheet-tab {
  font-size: var(--fs-tech);
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pv-sheet-tab.is-active {
  color: var(--text-strong);
  border-color: var(--accent);
  background: var(--accent-soft);
}
.pv-sheet-tab:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.pv-grid-pan {
  max-width: 100%;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
}
.pv-grid {
  border-collapse: collapse;
  font-size: calc(var(--fs-tech) * var(--pv-scale));
  color: var(--text-secondary);
}
.pv-grid th,
.pv-grid td {
  border: 1px solid var(--border-subtle);
  padding: 2px var(--sp-2);
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: top;
}
.pv-grid thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-weight: normal;
}
.pv-grid tbody th {
  background: var(--bg-elevated);
  color: var(--text-muted);
  font-weight: normal;
  text-align: right;
}
.pv-grid td.is-formula {
  color: var(--link);
}
.pv-grid td.is-hit {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
  background: var(--accent-soft);
}
.pv-slide {
  min-width: 0;
  overflow-wrap: anywhere;
}
.pv-slide-title {
  margin: 0 0 var(--sp-2);
  font-family: var(--serif);
  color: var(--text-strong);
  font-size: calc(var(--fs-lg) * var(--pv-scale));
  line-height: var(--lh-tight);
}
.pv-slide-text {
  margin: 0 0 var(--sp-1);
  white-space: pre-wrap;
}
.pv-notes {
  margin-top: var(--sp-3);
  padding-left: var(--sp-3);
  border-left: 2px solid var(--border-strong);
  color: var(--text-secondary);
}
.pv-notes-label {
  margin: 0 0 var(--sp-1);
  color: var(--text-muted);
  font-size: var(--fs-tech);
}
.pv-note-text {
  margin: 0 0 var(--sp-1);
  font-size: calc(var(--fs-sm) * var(--pv-scale));
  white-space: pre-wrap;
}
.pv-hit {
  background: color-mix(in srgb, var(--accent) 26%, transparent);
  color: var(--text-strong);
  border-radius: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .pv-structured *,
  .pv-structured *::before,
  .pv-structured *::after {
    transition: none !important;
    animation: none !important;
  }
}
</style>
