# 统一执行展示 / 真实流式 / 附件 / 耗时修复 —— 实施方案（契约版）

- 基线：origin/main `ee6bbff1841b869be8b2e17dd85dc434b496b839`
- 功能分支：`feat/unified-process-attachments-streaming`（不自动合并 main）
- 本文是**共享契约**：事件字段、标识、持久化、降级规则以本文为准。
  子智能体在自己的 worktree 上开发，只改自己名下的文件；冲突由 Lead 集成裁决。

---

## 0. 现状核对（已核实，带证据）

| 链路 | 现状 | 证据 |
| --- | --- | --- |
| 模型流式 | **完全不存在**。`BaseAdapter` 只有 `complete()`，无 stream/delta | `backend/src/agent/adapters/base.py:154` |
| 事件传输 | SSE 总线已有（replay/背压/RESYNC），但只广播整段结果 | `backend/src/agent/api/bus.py:158` |
| 过程说明 | 模型在工具参数 `_qio` 里给 kind/text/explanation，落库为 `content_type="narrative"` 的消息并广播 NARRATIVE | `core/narrative.py:48`、`services/app.py:828` |
| 工具旁白 | native 模式工具调用前的正文以 `ASSISTANT{interim:true}` 整体推送（无增量） | `core/loop.py:722-739` |
| 阶段 | **不存在阶段概念**：narrative 是平铺的逐条记录，没有 stage_id / 顺序 / 状态 | `core/narrative.py:77` |
| 耗时 | 折叠态在未展开（未拉明细）时永远显示「读取中」；总耗时只在明细里 | `TurnTimingPanel.vue:55`、`useTurnTiming.ts:40` |
| 附件 | **完全不存在**：没有附件表、接口、组件，也没有原生文件选择 | 全仓 grep 无 attachment/upload 实现 |
| 桌面壳 | Tauri v2；依赖里**没有** dialog 插件；缓存里也没有该 crate；无 target 目录 | `frontend/src-tauri/Cargo.toml:35-50` |

结论：四项都是**真开发**，不是接线。

---

## 1. 统一执行展示（每轮一个过程区域）

### 1.1 标识（系统生成，模型不能伪造）

- `turn_id`：既有。
- `stage_id`：新，`st_<turn8>_<n>`，由 QIO 生成并持久化。
- `call_id`：既有工具调用 id。
- 工具归属**只看 `stage_id`**，不靠消息相邻位置。

### 1.2 阶段协议（模型侧，可选）

模型在工具参数保留键 `_qio` 里可再给一个 `stage` 操作（白名单解析，非法一律忽略）：

```json
"_qio": {
  "kind": "announce|progress|warning|result",
  "text": "正在读取仓库结构",
  "explanation": "可选，审批说明",
  "stage": { "op": "start|next|update", "name": "读取仓库结构" }
}
```

规则（可验证）：

1. **不自动开阶段**：没有合法 `stage` 操作的 narrative 只更新「当前阶段说明」；
   当前没有阶段时，才建立一个由系统兜底的隐式阶段（name 取首条说明，截断）。
2. `op=start`：当前无阶段时开一个；已有阶段时**不新开**（等价 update）。
3. `op=next`：结束当前阶段，开一个新阶段。
4. `op=update` / 缺失 / 非法 / kind 非法 / text 为空 → 不改变阶段集合，只更新当前说明（或什么都不做）。
5. 阶段名与说明都过 `core/narrative.py` 的白名单与脱敏，长度截断（name ≤ 40，text ≤ 120）。
6. **模型不能**改状态、参数、审批权限、真实结果；阶段文案不参与任何判定。

### 1.3 事件契约

新增 `EventType.STAGE`（关键事件，进 bus 的 CRITICAL 集合）：

```jsonc
{
  "turn_id": "turn_ab12",
  "stage_id": "st_ab12_1",
  "index": 1,                    // 从 1 开始，单调
  "status": "running" | "done",  // 阶段自身的状态，不代表整轮
  "name": "读取仓库结构",
  "text": "正在读取仓库结构",     // 本次说明，可为空串
  "kind": "progress",            // 可为空串（纯阶段边界）
  "op": "start" | "update" | "next" | "end",
  "narrative_id": "msg_x" | null,// 落库消息 id，前端据此去重
  "call_id": "call_1" | null,
  "call_ids": ["call_1"],
  "created_at": "2026-10-06T08:00:00+00:00"
}
```

- `NARRATIVE` **保留**（旧数据/旧客户端兼容），主轮不再发；前端遇到它时按**旧版平铺记录**渲染，
  **不伪造阶段**。
- `TOOL_START` / `TOOL_END` 增加可选 `stage_id`（缺省 null：真实执行数据里没有阶段时前端归入「整轮」）。
- 整轮真实状态只认 `TURN_START/TURN_END/TOOL_*`，阶段文案的 status 不能结束整轮。

### 1.4 持久化

阶段不新建表：沿用 `messages`（`content_type="narrative"`），在 `raw` 里加：

```json
{ "narrative": {"kind":"progress","tool":"read_file","call_id":"call_1","silent":false},
  "stage": {"stage_id":"st_ab12_1","index":1,"name":"读取仓库结构","op":"start","status":"running"},
  "calls": [ ... 系统事实 ... ] }
```

历史加载时按 `created_at/rowid` 顺序重放 `raw.stage` → 阶段顺序、阶段内历次说明、关联工具记录全部可回看。
旧数据没有 `raw.stage` → 走 legacy 平铺渲染。

### 1.5 前端呈现规则

- 一轮 = **一个过程区域**（新组件 `TurnProcess.vue`），内部三块：
  1. 状态行（**系统事实**：受理中/运行中/等待确认/已停止/已完成 · 耗时；
     工具行如「正在读取文件 · 2 项工具运行中」）；
  2. 当前阶段（名字 + 当前说明，突出显示）；
  3. 可展开历史：之前的阶段（顺序、历次说明、关联工具记录）。
- 工具默认一行，展开看参数/结果/耗时/失败详情。
- 审批在**同一过程区域内**自动展开（ApprovalEntry 复用现有权限与执行机制），保持可见直到用户选择。
- 完成/失败/停止时**自动收起**过程区，保留简短状态 + 总耗时；正文在下方。
- 用户正在阅读历史（过程区已展开 或 已上翻）时，普通状态更新**不得**强制收起/抢滚动位置。
- 旧入口归并：原「◈ 过程」气泡、NarrativeStage 平铺、工具卡、运行中提示、耗时面板
  统一进这一个区域，**同一内容只出现一次**；`MessageItem` 不再单独渲染 interim 气泡。

---

## 2. 真实流式（边生成边显示正式回答）

### 2.1 角色生命周期（核心裁决，避免"先当答案再撤回"）

一次模型调用 = 一条流式消息，稳定标识 `delta_id`（`dl_<turn8>_<call_seq>`）。

**分类守卫（Guard）**：adapter 的正文增量先进入守卫缓冲，出缓冲的条件只有一个 —— 分类已经确定：

1. 出现**任何工具调用增量** → 这条响应是工具轮：整段（含已缓冲正文）判为 `interim`，
   归入当前阶段，之后所有增量都进过程区。
2. 守卫窗口到期（`GUARD_MS=300` 且已收到 ≥1 个正文增量）仍无工具调用增量 → 判为 `answer`：
   缓冲文字**一次性**进入正式回答区，之后增量直接进正式回答区（真流式，不重打）。
3. 流终止时仍未分类 → 按「有工具调用 / 无工具调用」定论（无工具调用即 `answer`）。

守卫是**有界的**（300ms / 首段），不是「等整段响应结束再播放」，因此不构成假流式。

**唯一允许的角色改判**：守卫放行之后才出现工具调用增量（罕见，模型先说了一段完整的话又决定调工具）。
此时该 `delta_id` 的文字从答案区**移动**到过程区：文字逐字保留、标识不变、绝不重复出现；
答案区不再显示它。除此之外任何路径都**不得**撤回或重复展示正文。

事件（沿用既有可合并语义，降低背压/RESYNC 风险）：

```jsonc
// 累计快照：content 是该 delta_id 已确认的全部文字（与既有 ASSISTANT 语义一致）
{"content":"...", "interim": false, "streaming": true, "delta_id":"dl_ab12_3", "seq": 7}
```

4. 工具调用增量（name/arguments 碎片）**只**在 adapter 内组装，攒成合法 JSON 才交给工具执行；
   永远不当作正文展示，也绝不执行未完成参数。
5. 顺序与去重：`(delta_id, seq)` 单调；前端丢弃 `seq <= 已收最大 seq`；
   bus 的合并键从 `(type, turn_id)` 细化为 `(type, turn_id, delta_id)`，避免不同 delta 互相覆盖。
6. 发布节奏：主循环按 `≥40ms 或 ≥24 字符` 合并一次，**不逐字符写盘**；只有最终文本落库。
   `TURN_END.final_content` 是权威全文，只做**校准**（替换），不追加。
7. 取消/失败/断线：保留已确认文本并给状态；重连不重播动画（同一 delta_id 的 seq 不回退）。
8. `adapters/text.py` 等不支持流式的路径：一次性 `{streaming:false}`，前端显示「该模型路径不支持实时生成」。

### 2.2 分层改造

- `adapters/base.py`：新增 `StreamDelta` 与 `supports_stream`、`stream()`（默认不支持 → 抛 `NotImplementedError`，由 loop 走整段降级，**不假装流式**）。
- `adapters/native.py`（OpenAI 兼容，当前主力）与 `adapters/anthropic.py`：真 SSE 增量。
- `adapters/text.py`（text 兼容档）：明确降级 —— 一次性 `mode:"final"`，前端显示「该模型路径不支持实时生成」。
- `core/loop.py`：`_plan` 走流式分支；合并渲染节奏（≥40ms 或 ≥24 字符合并一次），
  **不逐字符写盘**；只有最终文本落库。
- `api/bus.py`：`ASSISTANT` 保持可合并事件；新增 `STAGE` 到 CRITICAL。

---

## 3. 耗时修复

- `TURN_END.data` 增补：`duration_ms`（权威总时长）、`queue_ms`、`started_at`、`ended_at`。
  数据源：`trace/store.py` 台账（已有 `duration_ms`）。
- 前端把总耗时记在 turn 上，**不展开也能显示**「已完成 · 耗时 12.3s」。
- 展开才拉明细；只有**真的在请求明细**时才显示读取中。
- 五种状态互不混淆：未请求 / 加载中 / 成功但无分项 / 失败 / 旧记录缺字段。
- **已知总耗时不受明细加载失败影响**；缺失 ≠ 0（不伪造 0，不永久转圈）。
- 并行分项不求和冒充总耗时；整轮结束后的后台整理单独说明（沿用 `afterTurnMs`）。
- 过程区与耗时面板**只保留一个入口**（并入 TurnProcess）。

---

## 4. 附件

### 4.1 保存规则（十进制 MB）

- 阈值 `100_000_000` 字节；`size <= 100_000_000` → **保存独立副本**（"已保存副本"）；
  `size > 100_000_000` → **引用本地文件**（"引用本地文件"），只存路径 + 元数据。
- 副本目录：`<QIO_DATA_DIR>/attachments/<yyyy>/<mm>/<att_id>__<safe_name>`，临时文件 + 成功后提交。
- 引用文件：清楚写明「历史保留的是位置，不保证内容仍然存在」。

### 4.2 数据与接口

追加迁移（**只追加**，不改历史迁移）到 `storage/schema.py::MIGRATIONS`：

```sql
CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY, message_id TEXT, turn_id TEXT, topic_id TEXT,
  kind TEXT NOT NULL,                    -- copy | reference
  original_name TEXT NOT NULL, stored_path TEXT, source_path TEXT,
  size_bytes INTEGER NOT NULL, mtime REAL, sha256 TEXT,
  state TEXT NOT NULL,                   -- prepared | ready | failed | missing | changed
  error TEXT, created_at TEXT, updated_at TEXT
);
```

接口（`api/server.py`）：

- `POST /api/attachments` `{source_path, name?, size?}` → 后台复制/登记，返回 `{id, kind, display, state, size_bytes}`
- `GET /api/attachments/{id}` → 元数据 + 可用性/变化检查（`missing` / `changed`）
- `POST /api/attachments/{id}/relocate` `{source_path}` → 重新指定位置
- `DELETE /api/attachments/{id}` → 只删 QIO 管理的副本，**绝不动用户原文件**
- `POST /api/turns` 接受 `attachment_ids: []`，发送后与消息绑定

工具（`tools/attachment_tools.py` + `registry.py` 注册）：
`read_attachment(attachment_id, offset=0, limit=200)` —— 按需分段读，返回真实内容 + 读了哪一段 + 有什么限制。

### 4.3 可读类型（诚实矩阵）

| 类型 | 现状 |
| --- | --- |
| 文本/代码/日志/JSON/CSV/TSV/XML | 读（编码嗅探 + 分段） |
| HTML | 读（bs4 取文本） |
| DOCX / XLSX | 读（zipfile + xml.etree，**不加新依赖**） |
| 图片 | 只给元数据 + 大小；**无视觉能力时不声称看懂** |
| PDF / 扫描件 / 加密文档 / 二进制 | 明确「当前不可读取」，不因拿到文件名就说已读 |

### 4.4 真实路径

- 拖放：Tauri 核心 `onDragDropEvent`（**不需要新 crate**，给的是真实路径）。
- 点击选择：Tauri 命令 + Win32 `GetOpenFileNameW`（**不引入新 crate**；若 `cargo check --offline` 不可用，
  明确标注"未编译验证"，并在前端 honest 降级为「粘贴/拖入路径」）。
- 浏览器回退：`<input type=file>` 走 multipart 上传字节（服务端存副本），**不把 fakepath 当路径**。

---

## 5. 并行分工与文件归属

| 子智能体 | worktree / 分支 | 独占文件 |
| --- | --- | --- |
| A 后端运行状态/阶段/流式 | `qio-up-a` / `wt/up-a` | `backend/src/agent/{core/loop.py,core/narrative.py,core/stage.py(新),core/turn.py,adapters/*,api/events.py,api/bus.py,services/app.py,services/turn_orchestrator.py}`、`backend/tests/test_stage_*.py`、`test_streaming_*.py` |
| B 前端统一过程/流式展示/耗时 UI | `qio-up-b` / `wt/up-b` | `frontend/src/{stores/events.ts,stores/session.ts,components/MessageStream.vue,components/MessageItem.vue,components/TurnProcess.vue(新),components/NarrativeStage.vue,components/TurnTimingPanel.vue,components/MarkdownContent.vue,composables/useTurnTiming.ts,services/events.ts,services/trace.ts}` |
| C 附件链路 | `qio-up-c` / `wt/up-c` | `backend/src/agent/{storage/schema.py(只追加迁移),services/attachments.py(新),api/server.py,tools/attachment_tools.py(新),tools/registry.py}`、`frontend/src/{components/Composer.vue,components/AttachmentChip.vue(新),services/attachments.ts(新)}`、`frontend/src-tauri/**` |
| D 耗时口径 + 独立验证 | `qio-up-d` / `wt/up-d` | 新增测试与证据文件、`docs/**`（验证报告）、`scripts/verify-*`；**不改 A/B/C 的实现文件** |

- 集成与最终裁决：Lead（`qio-up`，功能分支）。
- 共享文件（`docs/status.md`、`docs/architecture.md`）由 Lead 统一收口，子智能体不写。

## 6. 验收（Lead 集成后逐条跑）

1. 统一过程：立即出现、安静（无阶段也能显示真实状态）、同阶段更新、自主转阶段、
   工具并行与晚到结果、无重复气泡、完成自动收起。
2. 审批与异常：批准/拒绝/多项待审批/可恢复错误/失败/停止/中断/重连/旧历史。
3. 流式：fake provider 分次返回正文，**provider 未结束前前端已显示非空回答**；
   参数碎片正确组装；取消/错误/重复事件/恢复/全文校准不重复。
4. 耗时：从未展开即见总耗时；加载/无数据/失败/旧记录分别正确；明细错误不抹掉总耗时。
5. 附件：<、=、> 100MB 边界；多文件/同名/失败/取消/移动/删除/变更/重定位/重启/不可读类型。
6. 性能与视觉：真实起应用 + 截图（窄窗口、长回答、代码块、历史展开、审批、附件准备）。
7. `python scripts/check_docs.py`、后端 pytest、前端 vue-tsc + vitest 全绿。
