# 统一文字卡 · 持续许可 · 卡片内预览 · 多媒体对话 —— 冻结契约（2026-10-12）

本文件是 QIO 0.2.1 之后这一轮的**共享边界**。基线：`v0.2.1` = `65cdd7e3759fe44e12c6802a0cf41777bfc2e086`。
集成分支：`feat/unified-cards-media-preview-021`。

规则：**接口、事件、组件契约先冻结再并行实现**。本文件冻结的部分，任何人不得单方面修改；
确需修改时先通知主智能体，由主智能体改本文件并广播。各自模块内部实现自由。

---

## 0. 名词与总原则

| 名词 | 含义 |
| --- | --- |
| 文字卡 | 板面上唯一承载用户字符内容的卡片。内部是**块序列**（普通文字/标题/列表/引用/代码/链接），用户始终在同一个编辑区操作 |
| 整卡许可 | 文字卡的 `checked` 字段。一张卡只有一个许可，覆盖卡内文字、链接、代码 |
| 原卡片展开 | 点击卡片上的「展开预览」，**这张卡片本身**扩大阅读区域；不是弹出独立窗口、不是固定列重排 |
| 临时布局 | 展开造成的让位与尺寸变化。只影响显示，**不写入板面状态**，不产生表达/意图/任务语义 |

三条不可违反的诚实规则：

1. **未许可 = 完全不可见**。未许可文字卡的整体（含链接、代码、派生正文）绝不进入提交载荷、材料解析、模型请求。
2. **预览 ≠ 模型已读**。展开、翻页、缩放、临时让位都不触发模型调用、不产生分组、不暂停任务。
3. **状态可区分**：附件已保存 / 预览可用 / 内容已处理 / 已交付模型 / 回答已完成 —— 五件事分开报告。

---

## 1. 统一文字卡：数据契约（冻结）

### 1.1 卡片身份

* 卡片身份不变：**仍然是一个 `kind="text"` 的卡片**（id 不变）。
* 旧的 `kind="code"` / `kind="url"` 卡片在迁移后变成 `kind="text"`，**卡片的 id、位置、分组、关系、书签、草稿、任务引用全部保留**。
* `CARD_KINDS` 保留全部旧取值（历史数据与导入文件仍可读），但**新建时可添加的字符类卡片只有 `text` 一种**。
* 迁移逐卡进行：**绝不把历史上多张独立卡片合并成一张**。

### 1.2 块序列（唯一权威表示）

存放在 `card.meta.doc`：

```jsonc
{
  "schema": "qio.textcard/1",
  "blocks": [ Block, ... ]
}
```

`Block`（`text` 字段是**原样字符**，不做 trim、不做转义）：

```jsonc
{ "id": "b_ab12cd34ef56", "kind": "paragraph", "text": "普通文字" }
{ "id": "b_...", "kind": "heading",   "level": 2, "text": "标题" }      // level 1..3
{ "id": "b_...", "kind": "list",      "ordered": true, "text": "第一条" }
{ "id": "b_...", "kind": "quote",     "text": "引用" }
{ "id": "b_...", "kind": "code",      "text": "  def f():\n      return 1\n",
  "attrs": { "language": "python", "fence": "```" } }
{ "id": "b_...", "kind": "link",      "text": "QIO 仓库",
  "attrs": { "href": "https://github.com/HebiIsHere/QIO" } }
```

硬规则：

* `kind` 只能是 `paragraph | heading | list | quote | code | link`。
* **代码保真**：`code` 块的 `text` 必须逐字保存缩进、换行、行尾空白；不得补写、不得重排、不得去掉语言信息。语言信息在 `attrs.language`。
* **链接**：`attrs.href` 是地址；`text` 是可读标题（可为空，为空时界面显示地址）。`href` 非 `http/https` 时保留原值但在界面标注不可打开。
* 其他块只允许 `text`（+ `level`/`ordered`），多余字段忽略。
* 块的顺序 = 正文顺序。
* 无法识别的块（未知 kind / 结构损坏）**保留原文**为 `paragraph`，并在 `doc.issues` 里如实记录一条原因。绝不静默丢内容。

### 1.3 编辑区序列化（markdown 子集，双向稳定）

编辑区是**一个连续的编辑面**；这份文本是它的唯一输入输出格式。

| 块 | 序列化 |
| --- | --- |
| paragraph | `原文` + 空行 |
| heading(level=n) | `#`×n + 空格 + `原文` + 空行 |
| list(ordered=false) | `- ` + 原文（每行一个 `-`）+ 空行 |
| list(ordered=true) | `1. ` + 原文（每行 `1. `）+ 空行 |
| quote | `> ` + 原文 + 空行 |
| code | 围栏：第 1 行 `\`\`\`lang`（lang 为空则只有围栏），正文逐字，最后一行 `\`\`\`` + 空行 |
| link | `[标题](地址)` 单独成行 + 空行 |

解析规则（`parseDocText`）：

* 以**空行**分隔块；不以空行分隔的连续同一类型行属于同一个块（列表多条 = 一个 list 块的多行）。
* 围栏内的所有行（含空行、含以 `#` `>` `-` 开头的行）**逐字**属于 code 块，直到遇到同为 `\`\`\`` 的结束行。
* 未闭合围栏：到文本末尾为止，`doc.issues` 记「代码围栏未闭合，已按代码块读到末尾」。
* `[标题](地址)` 单独成行才识别为 link 块；出现在行中间则属于该块的正文（不解析为链接）。
* `serialize(parse(t)) === t`（对规范写法）与 `parse(serialize(doc))` 保持块序与 code/list 文本不变 —— 这两条是**测试必须锁死的性质**。

### 1.4 卡片冗余字段（兼容与派生，均可从块序列重建）

* `card.content` = 块序列的**纯文本**（照契约 §1.5 生成），用于列表/摘要/搜索与旧读取路径。
* `card.meta.docText` = 编辑区的 markdown 文本（**草稿恢复与提交载荷里也用这一份**）。
* `card.meta.doc` = 块序列（权威）。
* 新建文字卡（`new_card("text", ...)`）默认：`meta.doc = {"schema":"qio.textcard/1","blocks":[]}`，`meta.docText = ""`，`checked = False`。

### 1.5 纯文本派生（`blocksToText`）

```
heading  → "#"×level + " " + text
list     → ("- " | "1. ") + text        （每条一行）
paragraph/quote → text
code     → text                          （逐字，不改缩进换行）
link     → "标题 <地址>"  /  只有地址时 "地址"
块之间用一个 "\n" 连接
```

### 1.6 前端模块与导出（冻结签名）

`frontend/src/interactive/textCard.ts`（新增，纯函数，不依赖 vue、不碰 DOM）：

```ts
export type TextBlockKind = "paragraph" | "heading" | "list" | "quote" | "code" | "link";
export interface TextBlock { id: string; kind: TextBlockKind; text: string;
  level?: number; ordered?: boolean; attrs?: { language?: string; href?: string; fence?: string } }
export interface TextDoc { schema: "qio.textcard/1"; blocks: TextBlock[]; issues?: string[] }

export function emptyDoc(): TextDoc;
export function parseDocText(text: string): TextDoc;
export function serializeDocText(doc: TextDoc): string;
export function blocksToText(doc: TextDoc): string;
export function newBlockId(): string;
export function docFromCard(card: { content?: string; meta?: Record<string, unknown> }): TextDoc;
export function cardDocFields(doc: TextDoc): { content: string; meta: { doc: TextDoc; docText: string } };
export function blockAt(doc: TextDoc, index: number): TextBlock | undefined;
export function insertBlock(doc: TextDoc, index: number, block: TextBlock): TextDoc;
export function removeBlock(doc: TextDoc, index: number): TextDoc;
export function replaceBlock(doc: TextDoc, index: number, block: TextBlock): TextDoc;
export function moveBlock(doc: TextDoc, from: number, to: number): TextDoc;
export function wrapSelection(text: string, start: number, end: number, kind: "bold" | "italic" | "code" | "link" | "strike" | "heading2" | "quote" | "list", href?: string):
  { text: string; selection: { start: number; end: number } };
export function detectLanguage(name: string): string;   // 扩展名 → 语言 id，未知返回 ""
```

`frontend/src/interactive/board.ts` 新增（冻结签名）：

```ts
export function addTextCard(state: BoardState, opts: { x?: number; y?: number; doc?: TextDoc }): { state: BoardState; cardId: string };
export function setCardDoc(state: BoardState, cardId: string, doc: TextDoc | { docText: string }): BoardState;
```

`frontend/src/components/interactive/TextCardEditor.vue`（新增）：
```ts
props: { card: BoardCard; readonly?: boolean; minHeight?: number }
emits: { change: [{ docText: string; doc: TextDoc }] }   // 输入即发，父组件负责草稿与提交
```

### 1.7 添加菜单与识别规则

* `AddMenu.vue` 的字符类入口只剩「文字卡」一项；`file` / `image` 不变。
* 界面文案：文字卡提示「文字、链接、代码写在同一张卡片里；默认未许可」。
* `board.ts` 的 `ADDABLE_CARD_KINDS` 收敛为 `["text", "file", "image"]`（代码/网址不再出现在添加菜单）。
* `BoardCard.vue` 渲染规则：`text`（含迁移后的旧 code/url）走统一的文字卡渲染；`code`/`url` 的**旧分支保留**，仅在历史数据未迁移完成时兜底，且界面标注「旧卡片，尚未迁移」。
* 代码**文件**（`.py`/`.ts`/`.md` 等）仍然走文件材料（`kind="file"`），与卡内代码块是两件事：文件预览只读，卡内代码块可编辑。

### 1.8 后端模块与导出（冻结签名）

`backend/src/agent/interactive/text_card.py`（新增）：

```python
TEXT_DOC_SCHEMA = "qio.textcard/1"
BLOCK_KINDS = ("paragraph", "heading", "list", "quote", "code", "link")

def empty_doc() -> dict: ...
def parse_doc_text(text: str) -> dict: ...          # 与前端 parseDocText 同一套规则
def serialize_doc_text(doc: dict) -> str: ...
def blocks_to_text(doc: dict) -> str: ...
def normalize_doc(raw: Any) -> dict: ...            # 任何输入都返回合法 doc；损坏内容保留为 paragraph 并记 issues
def doc_from_card(card: dict) -> dict: ...          # 文字卡 → doc（旧 content 兜底：content 无结构时整体成为一个 paragraph/code 块）
def card_text_fields(doc: dict) -> dict: ...        # -> {"content": ..., "docText": ..., "doc": ...}
def new_block_id() -> str: ...
def new_text_card(content: str = "", **fields) -> dict: ...
```

`backend/src/agent/interactive/text_migration.py`（新增）：

```python
def migrate_card(card: dict) -> tuple[dict, bool]:
    """把一张旧 code / url 卡就地迁成文字卡。返回 (新卡, 是否发生了迁移)。
    幂等：已经是文字卡（含 meta.doc）时原样返回 (card, False)。
    不改 id / x / y / w / h / groups / links / 草稿 / 书签 / createdAt。"""
```

---

## 2. 持续许可（冻结）

### 2.1 规则

1. 文字卡只有**一个整卡许可** `checked`，覆盖卡内文字、链接、代码。
2. **许可持续有效**：提交成功/失败/重复/超时/受理未知/恢复/刷新/重启/模式切换，都不得清除 `checked`；也不得用旧回执把用户更晚的取消恢复为开启。
3. 只有用户手动取消（`checked=false`）或明确隐藏（`hidden=true`，隐藏即退出范围且按既有规则把 `checked` 置 false）才失效。
4. 可见范围与许可分别判断；隐藏/删除的卡片不进入提交范围。
5. 未许可的文字卡**整体不交付**给模型：其中的链接、代码、派生正文都不可见。旧 `code`/`url` 的「材料默认可见」通道**取消**。

### 2.2 后端改动点

* `interactive/models.py`：
  * `ANNOTATION_KINDS = ("text",)`（不变）；**`MATERIAL_KINDS` 去掉 `"code"`、`"url"`** → `("file", "image")`；
  * `visible_except_deleted()` 增加：`kind in ("code","url")` 的**遗留**卡片一律返回 `False`（迁移未完成时也不回退成可见）；
  * `CHECKABLE_KINDS` 保持 `ANNOTATION_KINDS`。
* `interactive/submission.py`：删除提交后清空勾选的行为。`checkedCleared` **恒为 `[]`**（字段保留，标注为历史字段）；不得再写 `board_store.save_board(..., reason="submitted")` 去清 `checked`。`delivery.detail` 不再出现「勾选已取消」。
* 回执/幂等重放/恢复路径：`_checked_cleared_at()` 删除或恒返回 `[]`；`replay_response()` 的 `checkedCleared` 恒 `[]`。
* 启动/读取时的数据迁移：`interactive/board_store.py` 在 `load_board` 之后对状态做一次 `migrate_state(state) -> (state, migrated)`（新增于 `text_migration.py`）：逐卡迁移旧 code/url；`migrated=True` 时用 `reason="text_card_migration"` 保存一次并 **seq+1**。幂等：已迁移过则不再写。
  * 迁移只发生在**板面状态加载/保存**这一层，不改历史快照表（历史快照按原样可读）。
* `interactive/materials.py`：`card_material(card)` 对 `kind="text"` 且 `checked` 为真时，把 `blocks_to_text(doc)` 作为 `inline` 材料交付（含链接地址与代码正文）；未许可时**根本不进入材料列表**。`kind in ("code","url")` 的遗留分支删除（迁移后再也不会出现）。
  * 代码块材料的 `label` = `"代码" + (language ? " （" + language + "）" : "")`；链接材料 = `"链接" + 标题/地址`；同一张卡的多个块**合并为一条** `inline` 材料（标注「同一张文字卡的正文」），不拆成多条以免重复。

### 2.3 前端改动点

* `interactive/board.ts` `setChecked` 语义不变；`board.ts:239` 的「hidden → checked=false」保留。
* `stores/interactive.ts`：`applyCheckedCleared` 保留函数（读 `result.checkedCleared`），但服务端恒为空 → 行为上不再清许可；**必须新增**：合并服务端快照时，`checked` 以「本地未提交的更新」优先，且**回执里的 `checkedCleared` 为空时不得把本地 `checked` 改成 false**。
* `BoardCard.vue` 文案：勾选框说明改为「许可：允许 QIO 查看这张卡（提交后仍然有效，只有你手动取消才失效）」。
* `SubmitCluster.vue` / 提交结果提示：不再出现「提交后勾选已取消」；改为「这些卡片仍保持许可」。
* `BoardSearchPanel.vue` 的 `uncheckedNotes` 只统计 `kind==="text"` 且通过迁移的卡（未迁移的 code/url 不计入「未许可」统计，而应显示为「旧卡片」）。

### 2.4 冻结字段与断言

| 字段 | 新语义 |
| --- | --- |
| `card.checked` | 整卡许可（持续有效） |
| 提交回执 `checkedCleared` | **恒 `[]`**；仍保留字段名以兼容旧前端 |
| 提交回执 `visibleRange` | 与旧一致：本次允许查看的范围 |
| 材料条目 `source` | 见 §5.3 |

---

## 3. 卡片内预览（冻结）

### 3.1 支持的格式矩阵（首版真实范围，逐条可验收）

| 类别 | 扩展名 | 预览方式 | 保真口径 |
| --- | --- | --- | --- |
| 图片 | png/jpg/jpeg/gif/webp/bmp | 原卡片内位图，等比 | 完整比例，可放大/缩小/适应 |
| PDF | pdf | **前端 pdf.js 逐页渲染** | 原页面外观（含图片/表格）；页数、当前页真实 |
| Word | docx | **后端结构化 JSON → 原卡片渲染**，按段落/标题/列表/表格 | 正文与表格内容保真；**逐页版面外观不保真**（2026-10-14 用户要求：预览界面上不再写「结构化正文预览，不是原版面」那一行） |
| Excel | xlsx/xlsm | **后端结构化 JSON → 原卡片渲染**，按工作表 + 行列 | 保留工作表名、单元格地址、公式与缓存值；不计算公式，明确标出 |
| PowerPoint | pptx | **后端结构化 JSON → 原卡片渲染**，按幻灯片 | 每页文本（含备注）、图片清单、表格；**不是原版面渲染**（同上：预览界面上不再写那一行） |
| 代码/文本 | py/ts/js/vue/java/c/cpp/h/go/rs/json/yaml/yml/md/txt/log/csv/ini/toml/sh/html/css… | 原卡片内只读渲染 | 保留缩进、行号、语言信息；只读、不执行 |
| 旧版 Office | doc/xls/ppt | **不支持内嵌预览** | 如实说明：需要用户用系统程序打开；保留打开/另存入口 |
| 其他 | 其它 | 不支持内嵌预览 | 如实说明原因 + 保留打开/下载入口 |

`INLINE_SAFE_SUFFIXES` 追加缺失的文本/代码后缀（`.py .ts .js .vue .json5 .yaml .yml .toml .ini .sh .ps1 .sql .c .h .cpp .hpp .rs .go .java .rb .php .xml① .html① .css .scss .tsx .jsx`）；`①`：`html`/`xml`/`svg` **保持不内联**（会执行脚本/外链），前端预览这些后缀时走「转换层返回文本」的路径，不让浏览器按 HTML 解析。

### 3.2 后端：转换层

新增 `backend/src/agent/services/preview.py`（冻结接口）：

```python
RECIPE_VERSION = 1                     # 转换配方版本，进缓存键
MAX_PREVIEW_BYTES = 64 * 1024 * 1024   # 单个附件参与转换的字节上限
MAX_PREVIEW_UNITS = 4000               # 一次转换产出的单元（段/行/单元/页）上限
MAX_ROWS_PER_SHEET = 500
MAX_COLS_PER_SHEET = 60

def classify_preview(name: str) -> str:
    """-> "image" | "pdf" | "docx" | "xlsx" | "pptx" | "code" | "legacy_office" | "unsupported" """

def convert(att: Attachment, path: Path, *, cancel: threading.Event | None = None) -> dict:
    """纯计算：把文件转成预览数据。**不**写库、**不**发事件。异常向上抛。"""

def preview_cache_key(attachment_id: str, version: str, kind: str) -> str: ...
def render_for(store, attachment_id: str) -> dict:
    """带缓存与版本校验的入口：返回 {"status": "ready"|"failed"|"unsupported",
    "kind": ..., "version": ..., "attachmentId": ..., "recipe": RECIPE_VERSION,
    "data": {...} | None, "reason": str, "truncated": bool, "stats": {...}}"""
```

各 `kind` 的 `data` 形状（冻结）：

```jsonc
// image
{ "kind":"image", "width": int|null, "height": int|null, "bytes": int }
// pdf（后端只给事实，渲染在前端）
{ "kind":"pdf", "pages": int|null, "title": str, "encrypted": bool }
// docx
{ "kind":"docx", "blocks":[ {"type":"heading"|"paragraph"|"list",
    "level": int|null, "ordered": bool|null, "runs":[{"text":str,"bold":bool,"italic":bool}]} ],
  "tables":[ {"rows":[[str,...],...]} ],
  "truncated": bool }
// xlsx
{ "kind":"xlsx", "sheets":[ {"name":str, "rows":[ {"index":int,
    "cells":[ {"ref":"A1","value":str|null,"formula":str|null,"cached":str|null} ]} ]} ],
  "truncated": bool }
// pptx
{ "kind":"pptx", "slides":[ {"index":int, "title":str,
    "texts":[str,...], "images":[{"name":str,"bytes":int}], "tables":[[[str,...],...]] } ],
  "truncated": bool }
// code
{ "kind":"code", "language":str, "lines":[str,...], "totalLines":int, "truncated": bool }
// legacy_office / unsupported
{ "kind":"legacy_office"|"unsupported", "reason": str }
```

实现约束（硬）：

* **不加新 Python 依赖**：docx/xlsx/pptx 继续用 `zipfile` + `xml.etree` 增量解析（沿用 `tools/attachment_tools.py` 已有实现与预算思路，抽到可直接复用的纯函数）。
* 单次转换在中止事件 / 预算耗尽时**必须**停止；转换在**工作线程**里跑（`asyncio.to_thread` 或既有 executor），不得阻塞事件循环。
* 任何异常都变成 `status="failed"` + 可读 `reason`，绝不抛出 500。
* 加密 / 损坏 / 超限 / 未支持分别给不同 `reason`。

### 3.3 后端：HTTP 接口（冻结）

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/api/attachments/{id}/content` | 可选 `Range` | 既有行为 + **真实 `Content-Length`、`Accept-Ranges: bytes`、206 分段**；`Content-Type` 按后缀（已有） |
| GET | `/api/attachments/{id}/preview` | — | `render_for` 的结果；`?refresh=1` 绕过缓存重算 |
| GET | `/api/interactive/boards/{boardId}/preview` | — | `{"state": {"positions": {cardId: {"attachmentId":str,"version":str,"position":{...},"updatedAt":str}}}}` |
| PUT | `/api/interactive/boards/{boardId}/preview/{cardId}` | `{"attachmentId":str,"version":str,"position":{...}}` | 同上单项；`attachmentId` 与当前卡不一致时 409 |
| DELETE | `/api/interactive/boards/{boardId}/preview/{cardId}` | — | `{"ok":true,"deleted":bool}` |

* `position` 形状：`{"mode":"image|pdf|docx|xlsx|pptx|code","page"?:int,"slide"?:int,"sheet"?:str,"row"?:int,"line"?:int,"zoom"?:number,"scrollTop"?:number}`（只允许这些键；未知键丢弃）。
* 阅读位置按 **attachmentId + version** 归属：版本变了（文件被改/重新关联）→ 位置回到默认，并如实返回 `version` 供前端比较。**旧版本的位置绝不写到新版本上。**

### 3.4 数据库（迁移 41/42，只追加）

```sql
-- 41：预览缓存（按附件 + 版本 + 配方）
CREATE TABLE IF NOT EXISTS attachment_previews (
  attachment_id TEXT NOT NULL,
  version TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  recipe INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  reason TEXT NOT NULL DEFAULT '',
  truncated INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL DEFAULT '{}',
  bytes INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (attachment_id, version, recipe)
);
-- 42：板面卡片的预览阅读位置
CREATE TABLE IF NOT EXISTS board_preview_state (
  board_id TEXT NOT NULL,
  card_id TEXT NOT NULL,
  attachment_id TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  position TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (board_id, card_id)
);
```
`COMPENSATION_VERSION` 升到 42（= 40 的全部语句 + 41 + 42），`SCHEMA_VERSION` 自动跟随。

### 3.5 前端：预览层（冻结）

* `frontend/src/interactive/previewPlan.ts`（新增）：纯函数，把 `(card, render状态, 环境)` 翻成
  `{ mode: "inline"|"external"|"unsupported", renderer: "image"|"pdf"|"docx"|"xlsx"|"pptx"|"code"|"none",
     reason: string, actions: PreviewAction[] }`。
* `frontend/src/components/interactive/CardInlinePreview.vue`（新增）：一张已展开卡片的预览主体。
  * 渲染器**动态 import**（`pdfjs-dist` / `docx-preview` / `xlsx` / `pptx` 解析器），未使用时不进首屏包。
  * 组件 props：`{ card: BoardCard; active: boolean; onClose: () => void }`；emits：`{ position: [position] }`。
  * 卸载 / 换附件 / 卡片收起时：中止在途转换与渲染（`AbortController` + 代次计数），释放 blob URL 与 canvas，**迟到结果丢弃**。
* `BoardPreviewLayer.vue`：从「浮层」扩为「原卡片内展开」。**收起时**卡片仍显示文件名、类型、状态与缩略图（图片才有缩略图）。
* 展开入口：卡片上的「展开预览」按钮；展开状态存 `useInteractiveStore().expandedCards: Set<string>`（**只在前端内存**，不写板面状态）。

### 3.6 临时布局（冻结）

`frontend/src/interactive/cardLayout.ts`（新增，纯函数）：

```ts
export interface LayoutInput {
  cards: { id:string; x:number; y:number; w:number; h:number }[];   // 板面状态里的正式位置
  expanded: string[];                                              // 当前展开的卡片 id
  expandedSize: { w:number; h:number };                            // 展开后的目标尺寸
  stage: { w:number; h:number };                                   // 板面可视范围
  gap?: number;                                                    // 默认 16
}
export interface LayoutResult {
  slots: Record<string, { x:number; y:number; w:number; h:number }>;  // 只给渲染用
  expanded: string[];
  displaced: string[];                                                // 被让位的卡片
}
export function planExpandedLayout(input: LayoutInput): LayoutResult;
```

硬规则：

* **不改板面状态**：`slots` 只用于渲染；`board.cards[].x/y/w/h` 永远是用户摆放的正式位置。
* 按 `expanded` 数组顺序处理（同一集合的顺序决定让位顺序）；每个展开卡在原位置原位放大到 `expandedSize`（放不下时向内收敛，不越出 stage）。
* 与展开矩形相交的**未展开**卡片按最短方向让开（先试向右 / 向下，再向左 / 向上），让开后仍相交则继续推，最多 8 轮；仍相交的记为 `displaced`（界面不隐藏它，但不再强行移动）。
* **多个展开卡之间不互相让位**（用户可以选择让两张重叠或分开，那是用户的摆放）。
* 收起后：`expanded` 里没有的卡一律回到正式位置 → 布局自然恢复；**用户展开期间手动移动过**的卡（其 `x/y` 已被用户改过）恢复后停在**用户的新位置**。
* 展开、让位、收起**都不写入板面状态、不产生表达、不触发意图/任务**。

**裁决与实现补充（2026-10-12，集成前定稿）**：

* `displaced` 的含义以正文为准：**推满上限轮数后仍与某个展开矩形相交的未展开卡片**（只列进结果、不隐藏、不再硬推）。
* 展开尺寸默认 `560×420`（`EXPANDED_PREVIEW_SIZE`），放不下时向内收敛；`stage` 无效（0 / NaN）时按**无界**处理（不越界约束退化为不生效，而不是抛错）。
* **减少动画**：`prefers-reduced-motion: reduce`（或 `data-motion="reduced"`）下，展开 / 让位 / 收起**完全不做过渡动画**，直接落到最终位置；不是「压短时长」。
* 预览主体的 `onClose` 是 prop、阅读位置是 emit `position`；回写阅读位置走预览服务的入口，**绝不写入板面状态**。
* Esc：展开状态下按 Esc **只收起预览**；没有展开的卡片时，Esc 的既有语义（中断连线 / 拖动）保持不变。

---

## 4. 多媒体对话（冻结）

### 4.1 两处入口，一套能力

| 入口 | 现状 | 本轮 |
| --- | --- | --- |
| 对话模式聊天（`ConversationView` + `Composer`） | 已支持图片真实字节交付（`image_delivered`） | 增加音频 / 视频；共用同一套「能力协商 + 交付事实」 |
| 互动模式悬浮聊天（`ChatDock.vue` → 悬浮聊天会话） | 同上 | 同上；只发送**当前消息 + 明确选定的附件**，不自动带板面/未提交改动/未许可文字卡/预览内容 |

### 4.2 能力协商（能力事实的唯一来源）

`backend/src/agent/adapters/base.py` 的适配器能力声明扩展为：

```python
supports_images: bool = False      # 已有
supports_audio: bool = False       # 新增：能否把**音频字节**作为内容块交付
supports_video: bool = False       # 新增：能否把**视频字节**作为内容块交付（绝大多数为 False）
audio_transcription: str | None = None   # 新增：派生转写通道标识（如 "openai.audio.transcriptions"），None = 没有
```

* 交付判定只看**本次真实使用的链路**（含回落后的适配器），不看接口「能接受什么」。
* 没有能力时如实降级，**绝不**静默换成用户没配置的外部服务。

### 4.3 派生处理（有 ffmpeg 才做，没有就如实说）

新增 `backend/src/agent/services/media.py`：

```python
def ffmpeg_path() -> tuple[str | None, str]:      # (路径, 来源说明)；找不到时返回 (None, 原因)
def probe_media(path: Path, *, timeout: float = 20.0) -> dict:
    """-> {"container":str,"duration":float|null,"hasVideo":bool,"hasAudio":bool,
           "width":int|null,"height":int|null,"reason":str}  找不到 ffmpeg 时 reason 说明"""
def sample_video_frames(path: Path, out_dir: Path, *, count: int = 6, max_edge: int = 768,
                        start: float = 0.0, end: float | None = None, timeout: float = 60.0) -> list[dict]:
    """-> [{"at": float, "path": Path, "bytes": int}]  按均匀时间点取帧，保留时间位置"""
def extract_audio(path: Path, out_path: Path, *, max_seconds: float = 300.0, timeout: float = 90.0) -> dict:
    """-> {"path":Path,"seconds":float,"bytes":int,"reason":str} 单声道 16k wav（转写用）"""
def cleanup_media(work_dir: Path) -> None: ...
```

边界（写进 `reason`，不许含糊）：单文件 `MAX_MEDIA_BYTES = 512 MiB`；抽样帧数上限 `MAX_SAMPLE_FRAMES = 12`；
音频转写时长上限 `MAX_TRANSCRIBE_SECONDS = 300`；样本帧与音频片段**只存在于本轮工作目录**，用后清理。

### 4.4 诚实事实（材料条目 `source` 扩展，冻结）

```
image_delivered     图片字节已交给模型
image_unsupported   链路没有视觉能力 / 取不到字节（reason 分四种，沿用既有实现）
audio_delivered     音频字节已交给模型
audio_transcribed   音频经派生转写得到文字（带 transcript / 时段 / 提供方）
audio_unsupported   没有音频能力也没有转写通道（带原因）
video_delivered     视频字节已交给模型
video_sampled       视频按时间点抽样帧交付（frames 带 at 时间位置；明确「不是逐帧」）
video_unsupported   没有视频能力也没有可用的抽样通道（带原因）
```

每条都要带：`read: bool`、`reason: str`、`range: {start: float|null, end: float|null}`（音频/视频）、
`frames: [{at: float}]`（视频抽样）、`engine: {provider: str, model: str}`（派生处理用了谁）。
**转写文本只代表实际得到的文字**，不得据此声称理解了语气、音乐、全部环境声音；抽样帧不得声称逐帧读完整段视频。

### 4.5 互动提交的图片范围（本轮）

* 只完成图片：提交时按冻结的可见范围 + 材料身份 + **版本**取图；隐藏 / 删除 / 未许可不通过任何派生结果绕过。
* 视频 / 音频**不**在互动板面提交理解范围内（界面如实说明），对话模式两处入口已支持。
* 审批仍绑定对应方案与材料版本；有效图片依据延续到后续生成/恢复阶段。

---

## 5. 事件、状态与文档

### 5.1 状态词（必须可区分，界面与文档统一用这五个词）

`附件已保存` → `预览可用` → `内容已处理` → `已交付模型` → `回答已完成`。
失败时保留真实原因与恢复入口（打开 / 下载 / 重新关联 / 重试）。

### 5.2 不改的契约

* 事件集合与 `EventType` 的守卫测试（`test_event_protocol.py`）不变；本轮**不新增**事件类型，除非某个契约必须新增 —— 若新增，必须同时改 `docs/architecture.md`、`docs/status.md`、前端 `stores/events.ts`、守卫测试。
* 迁移只追加（`test_migrations_append_only_verify.py` 会查）。
* 密钥脱敏：任何新增日志/事件必须过 `agent/trace/redact.py`。

### 5.3 文档更新（谁改谁负责）

| 文档 | 内容 |
| --- | --- |
| `docs/interactive-mode-contract.md` | 统一文字卡、持续许可（删除「提交后取消勾选」全部表述）、卡片内预览、临时布局 |
| `docs/interactive-vision-contract.md` | 多媒体四种以上事实（图片/音频/视频抽样/转写） |
| `docs/architecture.md` | 新模块（text_card / text_migration / preview / media）、新表、新接口 |
| `docs/status.md` | 本轮实现与**已知限制**（旧版 Office 不支持、无 ffmpeg 时的降级、docx/xlsx/pptx 是结构化预览不是原版面） |
| `docs/SETUP.md` | 预览与媒体的运行依赖与随包提供方式 |
| `docs/releases/v0.2.2.md`（新增） | 发布说明 |

---

## 6. 验证口径（唯一一次全量 + 唯一一次 CI）

* 开发期：各分支只跑**职责范围内的定向测试**（`uv run --frozen pytest tests/test_x.py -q` / `npx vitest run src/....test.ts`）。
* 集成后、工作树确定后：主智能体执行**唯一一次**全量本地验证（后端全量 pytest、前端 vue-tsc + 全量 vitest + build、`python scripts/check_docs.py`、eval 对比按 AGENTS.md 判定）。
* CI：**唯一一次**受控运行，记录 run id 与 SHA；失败只做定向补验。
