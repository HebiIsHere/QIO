# 对话页 v2 · 并行开发共享契约（2026-10-22）

> 状态：**进行中**，由 Lead 维护。所有子分支以此文件为接口与验收口径的唯一事实源。
> 基线：origin/main = v0.2.10 = `249a63443c72b3dad011b5446ee9920f4dadf0fd`（开工时重新 fetch 确认无更新）。
> 主开发分支：`cv/main`（worktree `D:\qio-dev\qio-cv-main`）。

## 0. 与其他并行任务（R9）的边界

- 工作区里另有一条 **R9** 线（worktree `qio-r9-*`，分支 `r9/main`），正在改「统一权限模式 / 源文件引用 / 桌面退出」。
  它们的冻结契约在 `docs/r9-contracts.md`（在 r9 分支上，不在本基线里）。
- **本任务不从 r9 分支拉代码、不修改 `qio-r9-*` 与 `qio` 目录、不复制它们的权限引擎/窗口关闭实现。**
- 本任务对共用能力只做**最小、可合并**的接触，并遵守：
  - 审批：本页只做**统一呈现与类型分派**；工具审批继续走 `POST /api/approvals/{id}/respond`，
    知识审批继续走知识自己的接口。若 R9 合并后替换了审批存储，替换点集中在
    `frontend/src/stores/unifiedApprovals.ts` 与 `frontend/src/components/approval/UnifiedApprovalModal.vue` 两处。
  - 窗口按钮/关闭：沿用现有 `WindowChrome.vue` 的 Tauri 调用，**不改关闭语义**（R9 的托盘/真正退出会替换这一层）。
  - 附件/预览：沿用现有附件身份与 `/api/attachments/{id}/preview`；不新建第二套文件身份体系。
    文件卡与预览只读，不写回源文件。
- 如果某处必须新增共用字段/表，按「只追加、可合并」原则做，并在报告里单列「与 R9 的集成点」。

## 1. 分支与 worktree

| 角色 | 分支 | worktree | 负责 |
| --- | --- | --- | --- |
| Lead | `cv/main` | `D:\qio-dev\qio-cv-main` | 骨架、公共契约、store、后端任务模型、设置持久化、集成、全量验证、文档 |
| A | `cv/a-shell` | `D:\qio-dev\qio-cv-a` | 外层布局、顶部全局栏（含窗口按钮与设置入口）、左侧任务栏 |
| B | `cv/b-chat` | `D:\qio-dev\qio-cv-b` | 中央消息流、主题栏、输入区、上方五个图标入口、发送/停止、排队 |
| C | `cv/c-preview` | `D:\qio-dev\qio-cv-c` | 右侧多标签只读预览、全窗口阅读、阅读位置、文件更新 |
| D | `cv/d-approval` | `D:\qio-dev\qio-cv-d` | 统一审批弹窗、知识保存（含修改后保存）、记忆问题提醒、相关后端接口 |

规则：
1. 每个 agent 只在自己的 worktree 里提交；**不 push、不建 PR、不触发 CI**；不合并其他分支。
2. 不修改不属于自己的文件；需要共用文件改动时，在最终报告里写「请求 Lead 修改」。
3. 每次提交信息用中文，格式 `feat(cv-x): …` / `test(cv-x): …`。
4. 开发期只跑**定向**测试（见 §9），不要跑全量 `pytest` / `npm test` / 构建矩阵。
5. 完成后把分支停在工作区（不清理 worktree），并给 Lead 发一条自包含消息：分支、HEAD SHA、改了哪些文件、定向测试命令与结果、未完成/未验证项。

## 2. 文件归属（互斥）

Lead 拥有（别人不要改）：
- `frontend/src/views/ConversationView.vue`、`frontend/src/App.vue`
- `frontend/src/conversation/contracts.ts`、`frontend/src/conversation/layoutPrefs.ts`
- `frontend/src/stores/conversationShell.ts`
- `frontend/src/services/api.ts`、`frontend/src/services/taskFeedApi.ts`
- `frontend/src/styles/tokens.css`
- 后端：`api/server.py`、`storage/schema.py`、`storage/migrate.py`、`services/app.py`、`services/task_feed.py`、
  `api/task_feed_routes.py`、`tools/task_manager.py`、`services/maintenance.py`、`services/background.py`
- `docs/status.md`、`docs/architecture.md`、`docs/conversation-page-contract-20261022.md`

A 拥有：
- `frontend/src/components/WindowChrome.vue`（顶部全局栏：品牌、设置入口、窗口按钮排布；**不改变按钮的 Tauri 语义**）
- `frontend/src/components/conversation/TaskRail.vue` 及同目录 A 自建子组件
- `frontend/src/stores/conversationTasks.ts`、A 自建 `frontend/src/services/conversationTasks.ts`
- A 的定向测试文件

B 拥有：
- `frontend/src/components/conversation/ChatColumn.vue`、`ComposerDock.vue` 及同目录 B 自建子组件
- `frontend/src/components/Composer.vue`（仅对话页使用；交互页有自己的输入）
- `frontend/src/components/MessageStream.vue`、`MessageItem.vue` 的必要改动（只做本页集成必需的最小改动）
- B 的定向测试文件

C 拥有：
- `frontend/src/components/conversation/PreviewPanel.vue` 及同目录 C 自建子组件
- `frontend/src/conversation/preview.ts`（预览标签/身份/位置纯逻辑）、`frontend/src/stores/conversationPreview.ts`
- 复用 `frontend/src/interactive/renderers/*`、`frontend/src/interactive/attachmentPreview.ts`；若需修改，改为向后兼容的增量
- C 的定向测试文件

D 拥有：
- `frontend/src/components/approval/UnifiedApprovalModal.vue` 及同目录子组件
- `frontend/src/stores/unifiedApprovals.ts`
- `frontend/src/components/KnowledgeCandidateCard.vue`、`frontend/src/components/ApprovalModal.vue`、`ApprovalEntry.vue`（若需要改）
- 后端：`agent/api/knowledge_approval_routes.py`、`agent/services/knowledge_approval.py`（新文件）
- D 的定向测试文件

> `ConversationView.vue` 只由 Lead 改；A/B/C/D 通过「自己组件」接进去，不要动它。
> 需要在 `ConversationView.vue` 里接线的地方，写进报告，由 Lead 集成。

## 3. 目标布局（与用户原图一致，颜色仍用现有 tokens）

```
┌────────────────────────────────────────────────────────────────────┐
│ 顶部全局栏（窗口层）：QIO 品牌 ……… [设置]  [–][□][×]（仅桌面壳） │  ← 无话题标题
├─────────────┬────────────────────────────────────┬───────────────┤
│ 左：任务栏   │ 中央：话题栏 + 消息流 + 五入口+输入区 │ 右：只读预览    │
│ 当前/历史    │ 话题标题在中央顶部                    │ 多标签/全窗口  │
│ 可拖宽/收起  │ 「回到最新消息」在消息区底部居中       │ 可拖宽/收起     │
└─────────────┴────────────────────────────────────┴───────────────┘
                                  右下角：记忆星球入口（现有组件，不改样式/功能）
```

- 三栏在 1440×900 下必须都可见；≤1180px 先**临时**自动收起左栏；仍不足时右栏改为**全窗口预览**（带返回）。
- 临时收起/全窗口不改写用户保存的偏好；窗口变宽后恢复。预览原本关闭时变宽也不自动打开。
- 任何聊天区域**不允许横向滚动**：长网址/代码/表格/文件名必须 `overflow-wrap:anywhere` 或容器内滚动。
- 视觉：轻盈玻璃感（用 `--glass-*` 令牌）、柔和低干扰背景、清楚边界/圆角/层级；动效使用既有 `--mo-*`。
- 字体三声部不变：话题/标题衬线、正文无衬线、数据/时间等宽。中文不得出现方框。
- 组件内一律 `var(--*)`，禁止硬编码色值；支持亮/暗/减少动画。

## 4. 共享数据结构

TypeScript 唯一来源：`frontend/src/conversation/contracts.ts`（Lead 维护，勿复制修改）。

### 4.1 任务（左侧任务栏）

```ts
type TaskSource = "subagent" | "derived" | "dev_task" | "maintenance" | "tool" | "background" | "recovery";
type TaskStatus =
  | "queued" | "running" | "waiting_approval" | "waiting_user"
  | "completed" | "partial" | "failed" | "cancelled" | "budget_exhausted" | "unknown";
interface TaskFeedItem {
  id: string;                 // 稳定身份 "source:sourceId"
  source: TaskSource; sourceId: string;
  title: string;
  status: TaskStatus;         // 展示映射；rawStatus 原样保留
  rawStatus: string;          // 后端原始终态/状态，不得被展示层改写
  origin: "topic" | "global" | "unknown";
  topicId: string | null; topicTitle: string | null;
  executor: string | null;    // 子智能体 / 工具名 / 维护
  summary: string;
  queuedAt: string | null; startedAt: string | null; finishedAt: string | null;
  durationMs: number | null;
  requiresAttention: boolean; acknowledged: boolean;
  output: { truncated: boolean; preview: string; hint: string; readMore: boolean };
  actions: TaskAction[];      // 只有后端真实支持的动作才出现
}
```

- **禁止**把未知状态默认显示成「运行中」；`unknown` 显示「状态无法确认」。
- **禁止**把 partial 显示成 completed；`budget_exhausted` / `cancelled` 单独成状态。
- 排队用 `queuedAt`；不要伪造 `startedAt`。
- 当前/历史两个视图分开取数；滚动与展开状态分开保存（展开集合在 A 的 store 内，不写进服务端偏好）。
- 同一工作去重：以后端 `id` 为唯一键，前端再按 `source+sourceId` 兜底，不允许出现两行同一工作。
- 成功项短暂显示「完成」后进入历史；用户正在展开看的任务不得自动消失。失败需处理项置顶，`知道了` 后进历史。
- 增量事件只用于刷新；断线/重连/后端重启后以 `GET /api/tasks` 权威快照为准。快照里 `running` 必须来自仍然存活的实例，
  历史任务不得被重新标成 running。
- 长输出：默认只给预览；`output.truncated=true` 显示「仅显示部分」+「查看更多」（调用详情接口，不得拿预览冒充全文）。

### 4.2 布局偏好（服务端持久化）

`ConversationLayoutPrefs`（见 contracts.ts）：`leftWidth/leftCollapsed/rightWidth/rightOpen/taskTab/previewFull/previewTabs/activePreviewTabId`。
- 通过 `PUT /api/settings/ui` 的 `conversation_layout` 字段保存（JSON 字符串，≤64KB）；GET 读回。
- 只存轻量偏好与阅读位置；**不存正文、原始输出、密钥**。
- 保存失败：保留当前内存状态，在界面上给可重试提示，不静默丢。
- 服务端业务状态（任务状态、审批、消息）永远以服务端为准，不从布局偏好恢复。

### 4.3 预览标签身份

```ts
type PreviewKind = "image"|"pdf"|"docx"|"xlsx"|"pptx"|"text"|"code"|"markdown"|"audio"|"video"|"unsupported";
interface PreviewTarget {
  id: string;                    // 标签唯一身份，规则见下
  kind: PreviewKind;
  title: string;
  source: "attachment" | "reply_code" | "task_output";
  attachmentId?: string; version?: string;
  messageId?: string; blockIndex?: number;
  taskId?: string; topicId?: string; filePath?: string; mime?: string; size?: number;
}
```
身份规则（**不能只用文件名**）：
- 附件：`attachment:<attachmentId>@<version>`（version 取附件身份/内容版本；没有 version 时用 `attachment:<id>`）
- 回复代码块：`replycode:<messageId>:<blockIndex>`
- 任务成果文件：`task:<taskId>:<attachmentId 或规范路径>`
同一身份重复打开 → 激活已有标签；同名不同身份 → 两个标签。
位置状态（每标签）：`scrollTop / page / sheet / zoom / search / line`，只存轻量数字与短串。

### 4.4 统一审批

- 同一时刻只有一套有效提交按钮：对话页不再同时显示内联审批按钮与弹窗按钮。
- 队列一次展示一项，可切换查看；新申请不替换正在阅读/编辑的申请；关闭/稍后只隐藏，不批准不拒绝；已暂缓的同一申请不因重渲染/重连/轮询自动弹。
- 类型分派必须按**结构化字段**（kind/source），不看标题文字：
  - 工具/权限/凭据类 → 现有审批响应接口；
  - 知识保存类 → 知识自己的接口；「修改后保存」必须让用户确认后的内容成为生效版本（新增原子接口，禁止先批准原文再改）。
- 自动弹出规则：主智能体新增权限、后台工作、删除/覆盖等重点确认 → 自动；主对话知识保存 → 不自动，只更新入口数量；
  暂缓/已在弹窗/用户正在填表时不叠加、不替换。
- 记忆整理问题不是审批：不放进审批弹窗；只在星球入口显示真实待回答数量，点击进入现有星球功能定位。

## 5. 后端接口（Lead 提供，A/B/D 只调用）

- `GET /api/tasks?view=current|history&cursor=&limit=` → `TaskFeedPage`（权威快照 + 分页 + 计数）
- `GET /api/tasks/{id}` → 详情（长输出按需；被清理时如实说明）
- `POST /api/tasks/{id}/ack` → 标记「知道了」（失败/完成项转历史；不删结果）
- `POST /api/tasks/{id}/cancel` → 仅在后端真实支持时调用；返回「请求已提交」不等于执行单元已停止
- `GET/PUT /api/settings/ui` → `conversation_layout`
- 审批/知识/预览沿用现有接口；新增知识原子保存接口由 D 提供并在契约里登记。
- 前端取数封装：`frontend/src/services/taskFeedApi.ts`；A 不要自己 fetch。

## 6. 五个输入区上方图标入口（B）

有事项才显示、默认收起、图标 + 数量/状态、每次最多展开一个、再点收起：
1. 发送异常与草稿恢复（含投递不明、保存失败、被替换草稿；不自动重发、不创建重复消息；恢复不覆盖当前内容）
2. 从历史继续（说明下一条从哪继续，可取消；收起不取消已选位置）
3. 话题切换建议（保留用户选择，不自动切换）
4. 已提交的排队消息（数量；展开可检查/取消；分别处理「服务端已接受」与「投递不明」）
5. 待审批申请（去重后的有效数量；点击打开统一弹窗并收起其他四项）
- 审批入口打开时收起其余四项；左栏任务详情与右栏标签**不受**互斥展开规则限制。
- 收起不等于处理完成/忽略/删除。

## 7. 迁移入口映射（不得重复放提交按钮）

- 用户消息 / 草稿恢复 → 图标入口 1
- 任务恢复 / 后台人工处理事项 → 左栏对应任务项
- 全局存储/迁移/连接异常 → 中央顶部状态区（话题栏下方，不在消息流里造全局横幅）
- 历史读取失败 ≠ 没有历史：必须能重试，且不清空已加载内容
- 按身份去重；同一事项不要在多处出现提交按钮

## 8. 右下角记忆星球

- 保持现有 `PlanetDock` 组件与行为（用户 2026-10-22 明确：仅固定在设计右下角位置，**样式与功能不改变**）。
  不要改它的拖动/样式/功能；不要把它挪进布局网格。
- 记忆整理问题只显示简短提醒与真实数量；加载失败不能显示成「没有问题」。
- 点击进入现有星球功能并定位待回答问题（D 负责把提醒接到现有 PlanetView 的定位入口；不重做星球布局）。

## 9. 验证纪律

- 每个 agent 只跑自己的定向测试：`cd frontend; npx vitest run <你的 spec 文件...>`；需要时再 `npx vue-tsc --noEmit`。
- 后端定向：`cd backend; uv run --frozen pytest <相关测试文件>`（首次会建 venv，等它跑完）。
- 不跑全量 `pytest` / `npm test` / `npm run build` / CI；全量由 Lead 在集成后统一跑一次。
- 断言重点：状态映射与去重、审批类型分派与单次消费、修改后知识版本、发送/停止/排队/投递不明、预览身份与版本竞争、窄窗口规则。
- 不写只断言静态文案/CSS 类名/重复实现逻辑的低价值测试。
- 所有失败/未验证项如实写报告，不得用「应该可以」代替。

## 10. 交付报告模板（每个 agent）

```
分支：cv/x-…   HEAD：<sha>
改动文件：<按文件列一句话>
功能实现：<按验收项>
定向验证：<命令 + 结果数字>
未完成/未验证：<如实>
与 R9 / 共用能力的集成点：<如有>
请求 Lead 做的接线：<如有>
```
