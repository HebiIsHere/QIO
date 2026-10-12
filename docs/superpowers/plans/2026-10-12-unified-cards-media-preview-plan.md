# QIO 0.2.1 后一轮：实现方案 · 分工 · 验证清单

**基线**：标签 `v0.2.1` = `65cdd7e3759fe44e12c6802a0cf41777bfc2e086`（远端与本地一致，见 §0）。
**集成分支**：`feat/unified-cards-media-preview-021`（本地）；契约基提交 `dacc7cb`。
**共享契约（冻结，先读）**：`docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md`

---

## 0. 基线核对（先做，且如实记录差异）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 远端标签 SHA | `git ls-remote --tags origin v0.2.1` | `65cdd7e3759fe44e12c6802a0cf41777bfc2e086` |
| 本地标签 SHA | `git rev-parse v0.2.1^{commit}` | 同上（一致，无差异） |
| 基线 CI | GitHub Actions run `38223031214`（main @ 65cdd7e） | success（作为基线见证，不计入本轮配额） |
| 工作起点 | `git worktree add --detach D:/qio-dev/q021 v0.2.1` | 干净检出，未改动历史迁移 |

> 远端标签未变化，**没有**静默改用最新 main（main 在标签之后只多了发布台账文档提交）。

---

## 1. 实现方案（按用户要求拆成四块，逐块说清「怎么做」与「怎么证明」）

### 1.1 三种字符卡片统一为一种文字卡

**做法**：卡片身份仍是 `kind="text"` 的同一张卡（id 不变），内容改成**块序列**（`card.meta.doc`：paragraph / heading / list / quote / code / link），编辑区是一个**连续编辑面**（单个 textarea 承载 markdown 子集，工具栏对选中文字或段落设置格式），链接与代码块写在同一个编辑面里。

* 为什么不是三个固定栏目：块序列里链接是 `link` 块、代码是 `code` 块，但它们与普通文字**共享同一个编辑区**（同一份文本、同一个光标、同一套撤销栈），格式动作只改文本并归还选区。
* 代码保真：`code` 块的 `text` 逐字保存缩进/换行/行尾空白，语言在 `attrs.language`。
* 前后端共用一份规则：`frontend/src/interactive/textCard.ts` 与 `backend/src/agent/interactive/text_card.py`，两侧各有契约测试锁死三条性质（逐字保真、`serialize(parse(t)) === t`、块序稳定）。
* 添加菜单与识别规则收敛：新建字符内容只有「文字卡」；代码**文件**仍走文件材料（`kind="file"`，只读预览），与卡内代码块是两件事。

**历史迁移**（逐卡、幂等、不合并）：旧 `code` 卡 → 一个 `code` 块；旧 `url` 卡 → 一个 `link` 块；旧 `text` 卡补齐 `meta.doc`。id / 位置 / 分组 / 关系 / 书签 / 折叠 / 草稿键 / 任务引用全部保留。迁移在板面状态加载层执行一次（`reason="text_card_migration"`），不改历史快照表。

**可见范围**：旧注释（`checked=false`）迁移后**仍然不可见**；旧代码/网址卡迁移后 `checked=true`（迁移前它们默认在范围内，不能用迁移把它们变可见或不可见）。

### 1.2 持续许可

**做法**：`checked` 就是文字卡的整卡许可，覆盖卡内文字、链接、代码。

* 删除「提交后自动取消勾选」的全部行为：`submission.py` 不再清 `checked`、`checkedCleared` **恒为空数组**（直连与重放两条路径）。
* 前端 `applyCheckedCleared` 在回执为空时绝不把 `checked` 改成 false；快照合并不覆盖用户更晚的手动操作。
* 未许可 = 整体不交付：遗留 `code`/`url` 一律不可见；`MATERIAL_KINDS` 去掉这两类；`materials.py` 只在许可为真时把 `blocks_to_text(doc)`（含链接地址与代码正文）作为一条 `inline` 材料交付。
* 可见范围与许可分别判断：隐藏 = 退出范围；展开/预览/移动只改工作状态。

### 1.3 卡片内真实预览（六类格式）

**做法（前端渲染 + 后端结构化转换，不要求用户装任何东西）**：

| 格式 | 方案 | 依赖 |
| --- | --- | --- |
| 图片 | 原卡片内位图，等比 + 放大/缩小/适应 | 浏览器原生 |
| PDF | 前端 pdf.js 逐页渲染（保留原页面外观） | `pdfjs-dist`（进前端包） |
| DOCX | 后端 `zipfile`+`xml.etree` → 结构化 JSON（段落/标题/列表/表格）→ 原卡片渲染 | 无新依赖 |
| XLSX | 同上 → 工作表 + 单元格地址 + 公式与缓存值 | 无新依赖 |
| PPTX | 同上 → 幻灯片 + 文本/表格/图片清单 | 无新依赖 |
| 代码/文本 | 行号 + 等宽 + 语言信息，只读 | highlight.js（已有） |
| PDF 事实 | 页数/标题/加密/可提取文本量 | `pypdf`（纯 Python，进 sidecar） |
| 旧版 .doc/.xls/.ppt | **不内嵌预览**，如实说明并保留打开/下载入口 | — |

* 转换在工作线程、可取消、按「附件 + 版本 + 配方」缓存；版本变化重新转换，旧结果绝不写到新版本。
* 异步与释放：渲染器动态 import；卡片收起/换附件/卸载时中止在途工作并释放 blob URL、canvas、pdf 文档对象；迟到结果丢弃。
* 阅读位置（页码/幻灯片/工作表/行/行号/缩放）按 `attachmentId + version` 落库，版本不符回默认。
* **预览 ≠ 模型已读**：展开、翻页、缩放、临时让位都不触发模型、不产生分组、不暂停任务。

### 1.4 临时布局（原卡片展开 + 让位，不动板面状态）

**做法**：`cardLayout.planExpandedLayout` 纯函数把 `(正式位置, 展开集合, 展开尺寸, 舞台尺寸)` 算成 `slots`（只给渲染）。

* 展开 = 这张卡原位放大；与它相交的未展开卡按最短方向让开（最多 8 轮），仍相交记 `displaced`。
* 多个展开卡之间不互相让位；收起后不在 `expanded` 的卡一律回正式位置；展开期间用户手动移动过的卡停在**用户的新位置**。
* `board.cards[].x/y/w/h` 与 `seq` 在展开前后**完全不变**，不产生 `layout_only` 表达。

### 1.5 两处聊天读多媒体

**做法**：能力协商以**本次真实链路**为准（适配器声明 `supports_images` / `supports_audio` / `supports_video` / `audio_transcription`）。

* 图片：真实字节作为多模态内容块交付（已有能力，沿用）。
* 音频：链路有原生音频能力 → 交付字节；没有但存在派生转写通道 → 抽取音轨转写，带**时段**与**提供方**；两者都没有 → 如实说明。
* 视频：链路有原生视频能力 → 交付字节；否则用 ffmpeg 按时间点**抽样帧**（带时间位置），明确「不是逐帧」；没有 ffmpeg 或没有可用链路 → 如实说明原因。
* 材料依据进入**本轮回答以及需要再次调用模型的后续步骤**；两处入口（对话模式 + 互动悬浮聊天）共用同一套会话、附件与多媒体处理。
* 悬浮聊天只发送当前消息 + 明确选定的附件。

### 1.6 互动提交的图片范围

沿用既有图片交付闸门，按提交时冻结的可见范围 + 材料身份 + **版本**取图；隐藏/删除/未许可不通过派生结果绕过；视频/音频本轮不在板面提交理解范围内（界面如实说明）。

---

## 2. 分工与文件所有权（避免两个子智能体改同一文件）

| 工作流 | 分支 | worktree | 独占范围 | 共享任务 |
| --- | --- | --- | --- | --- |
| **P1 文字卡 / 迁移 / 持续许可** | `feat/uc-01-card-core` | `D:/qio-dev/qL1` | 后端 `interactive/text_migration.py`、`materials.py`、`submission.py`、`board.py`、`board_store.py`、`models.py`；前端 `TextCardEditor.vue`、`BoardCard.vue`（编辑与勾选）、`board.ts`、`submission.ts`、`AddMenu.vue`、`BoardSearchPanel.vue`、`stores/interactive.ts`（许可相关） | task-1 |
| **P2 真实预览** | `feat/uc-02-preview-core` | `D:/qio-dev/qL2` | 后端 `services/preview.py`、`api/preview_routes.py`、迁移 41/42、`attachment_tools.py`（pptx/pdf）、`services/attachments.py`（可读性分类）；前端 `previewPlan.ts`、`CardInlinePreview.vue`、`interactive/renderers/`、`services/attachments.ts`（预览请求） | task-2 |
| **P3 多媒体聊天** | `feat/uc-03-media-chat` | `D:/qio-dev/qL3` | 后端 `services/media.py`、`adapters/*`（能力位）、`turn_orchestrator.py`（接线）、`interactive/materials.py`（媒体事实）；前端 `Composer.vue`（能力提示）、`ChatDock.vue`（附件入口）、`MessageItem.vue`（读取事实） | task-3 |
| **P4 展开与临时布局** | `feat/uc-04-layout-preview-ui` | `D:/qio-dev/qL4` | 前端 `cardLayout.ts`、`InteractiveView.vue`、`BoardCanvas.vue`（坐标来源）、`BoardCard.vue`（展开按钮）、`BoardPreviewLayer.vue`、`stores/interactive.ts`（展开集合） | task-4 |
| **主智能体（集成/共享边界）** | `feat/unified-cards-media-preview-021`（基 `...-021-base`） | `D:/qio-dev/q021` | 契约与共享内容模型、`server.py`（路由注册与常量）、`storage/schema.py` 迁移编号仲裁、版本文案、全部文档、依赖与打包、最终集成与验证 | — |

**重叠规避规则**：`BoardCard.vue` 由 P1 负责「编辑区与勾选语义」、P4 负责「展开按钮与预览挂载点」；`stores/interactive.ts` 由 P1 负责「许可/快照合并」、P4 负责「展开集合」；`materials.py` 由 P1 负责 text/code/url、P3 负责音视频。跨范围改动一律先通报主智能体。

---

## 3. 验证清单（一次性执行，不重复整套）

### 3.1 开发期（各分支只跑职责范围内的定向测试）

| 范围 | 命令 | 期望 |
| --- | --- | --- |
| 共享内容模型（基提交） | `npx vitest run src/interactive/__tests__/textCard.test.ts`；`python -m pytest tests/test_text_card.py -q` | 15 + 13 通过 |
| P1 | `pytest tests/test_text_migration.py tests/test_text_license.py tests/test_interactive_submission.py tests/test_interactive_materials.py -q` | 全绿 |
| P2 | `pytest tests/test_preview_service.py tests/test_preview_api.py -q`；`vitest run src/interactive/__tests__/previewPlan.test.ts` | 全绿 |
| P3 | `pytest tests/test_media_service.py tests/test_media_delivery.py -q` | 全绿（本机有 ffmpeg 时真跑，CI 无则跳过并说明） |
| P4 | `vitest run src/interactive/__tests__/cardLayout.test.ts` | 全绿 |

### 3.2 全量本地验证（**只执行一次**，集成后、工作树确定后）

| # | 项 | 命令 |
| --- | --- | --- |
| 1 | 后端全量 | `cd backend; uv run --frozen pytest` |
| 2 | 前端类型 | `cd frontend; npx vue-tsc --noEmit` |
| 3 | 前端测试 | `cd frontend; npm test` |
| 4 | 前端构建 | `cd frontend; npm run build` |
| 5 | 文档一致性 | `python scripts/check_docs.py` |
| 6 | 评测对比 | `cd backend; uv run --frozen python -m agent.eval.run`（按 AGENTS.md：仅在改了 runtime/预算/工具策略时需要；本轮的判定与结论单独记录） |

**失败处理**：记录精确失败项；修改后**只重跑失败项**，不重跑整套。

### 3.3 全量 CI（**只执行一次**，受控）

* 先核对现有触发规则（`.github/workflows/ci.yml`：`push` 所有分支 + `pull_request`）。
* 开发期间四个子分支**全程不 push** —— 从源头避免重复触发。
* 最终只 push 一次集成分支（或对 main 发一次 PR），记录 **run id + SHA**；失败时只补验失败 job（定向补验记录单列）。

### 3.4 验收情境（必须逐条给出证据）

统一文字卡 · 历史迁移 · 持续许可 · 六类预览 · 临时布局 · 预览边界 · 两处多媒体聊天 · 互动提交图片 · 故障与取消 · 打包与性能。

---

## 4. 已知限制（本轮如实声明，不夸大）

1. DOCX / XLSX / PPTX 的卡片内预览是**结构化正文预览**（段落/表格/单元格/幻灯片文本），**不是原版面渲染**；PDF 与图片保留原外观。
2. 旧版 `.doc` / `.xls` / `.ppt` 不支持内嵌预览（保留打开/下载入口）。
3. 音频转写 / 视频抽样依赖 ffmpeg 与所选链路的真实能力；没有可用转换程序或链路不支持时**如实降级**并说明原因。
4. 「已交付模型」不等于「模型理解正确」；抽样帧不等于逐帧读取；转写文字不代表理解了语气、音乐与全部环境声音。
