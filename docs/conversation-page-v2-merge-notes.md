# 对话页 v2（对话工作台）· 合并说明与交付记录（Lead）

> 状态：**待合并**（实现、集成与本地验收完成；完整 CI 结果与最终 SHA 见文末「最终记录」）。
> 契约：`docs/conversation-page-contract-20261022.md`；架构：`docs/architecture.md` §20；
> 进度与限制：`docs/status.md` P73。

## 1. 基线与分支

| 项 | 值 |
| --- | --- |
| 基线 / 产品版本 | 远端 `origin/main` = `249a63443c72b3dad011b5446ee9920f4dadf0fd` / v0.2.10（开工时重新 fetch 确认未前进） |
| 主开发分支 | `cv/main`（worktree `D:\qio-dev\qio-cv-main`） |
| 并行子分支 | `cv/a-shell`（A，HEAD `62c7e768`）、`cv/b-chat`（B，HEAD `d489b311`）、`cv/c-preview`（C，HEAD `280f6687`）、`cv/d-approval`（D，HEAD `f6f72f51`） |
| 集成方式 | 四个子分支分别 `merge --no-ff` 进入 `cv/main`：`c882da43`（B）、`6b424567`（A）、`16bfcbad`（C）、`ff3594f3`（D） |
| 最终 SHA | 见「最终记录」 |
| CI 运行号 | 见「最终记录」 |

- 各 agent 使用独立 worktree 与独立分支；共用文件（`ConversationView.vue` 归 A；
  `contracts.ts` / `layoutPrefs.ts` / `conversationShell.ts` / `api.ts` / `App.vue` / 后端
  `server.py` 等 shared 文件归 Lead）按契约分配，未出现两套公共状态。
- 本任务**未**改动 `qio`、`qio-r9-*` 或其他并行工作目录；R9（统一授权 / 源文件引用 /
  桌面退出）保持并行，边界见契约 §0 与 P73 §二。

## 2. 集成内容（接口与持久化）

- **迁移 55**（只追加）：`task_feed`（任务栏持久化展示记录）与 `task_ack`（「知道了」状态）；
  `SCHEMA_VERSION = COMPENSATION_VERSION = 55`，号段避开 R9 冻结的 52–54；
  `REQUIRED_OBJECTS` 同步并在补偿迁移中带建表语句。
- **后端接口**：
  - `GET /api/tasks?view=current|history&cursor=&limit=`（权威快照 + 分页 + 计数）；
  - `GET /api/tasks/{id}`（详情/长输出按需）、`POST /api/tasks/{id}/ack`、
    `POST /api/tasks/{id}/cancel`（仅真实支持单独取消的来源）；
  - `GET/PUT /api/settings/ui` 增加 `conversation_layout`（只存轻量布局与阅读位置）；
  - `POST /api/knowledge/{id}/approval`（知识保存原子接口：确认 / 修改后保存 / 不保存）。
- **后端服务**：`agent/services/task_feed.py`（来源聚合与诚实状态映射）、
  `agent/api/task_feed_routes.py`、`agent/services/knowledge_approval.py`、
  `agent/api/knowledge_approval_routes.py`；`TaskManager` 增加只读展示钩子（不改变执行语义）。
- **前端**：`conversation/contracts.ts`、`layoutPrefs.ts`、`previewTypes.ts` 为共享形状；
  `stores/conversationShell.ts`（布局偏好）、`stores/conversationTasks.ts`（任务栏）、
  `stores/conversationPreview.ts`（预览运行时）、`stores/unifiedApprovals.ts`（统一审批队列）、
  `stores/memoryQuestions.ts`（记忆问题数量）；组件在 `components/conversation/` 与
  `components/approval/`、`components/planet/MemoryPendingPanel.vue`（仅原子保存一处）。
- **共用能力最小调整**：`App.vue` 在对话路由不再渲染旧顶部审批/任务入口；
  `TurnProcess.vue` 内联审批在统一弹窗挂载时让位；`PlanetDock.vue` 仅把默认位置放到右下角
  （用户原图位置）并让浮动避让新增的预览面板；`MessageStream.vue` 在输入区改为正常流布局后
  不再补「悬浮输入区」底部缓冲；`interactive/renderers/pdf.ts` 只新增向后兼容的逐页文字读取。

## 3. 验证结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 前端类型 | `cd frontend; npx vue-tsc --noEmit` | 通过（0 错误） |
| 前端构建 | `cd frontend; npm run build` | 通过（生产构建成功） |
| 前端全量用例 | `cd frontend; npx vitest run` | 首轮全量：大部分用例通过；失败项全部是仍指向旧界面位置的断言（发送恢复/被让位草稿/设计守卫/不完整结局提示等）。按 v2 位置更新受影响文件后定向复跑通过（行为断言未放宽）。 |
| 后端任务栏 / 知识审批定向 | `cd backend; uv run --frozen pytest tests/test_cv_task_feed.py tests/test_knowledge_approval_api.py tests/test_settings_ui_api.py` | 通过 |
| 后端全量 | `cd backend; uv run --frozen pytest` | 4610 通过 / 51 跳过 / 1 失败；失败项是迁移升级路径用例里「首次补列迁移」的自适应逻辑（补偿迁移复述补列语句后，用例取 max 号导致回退断言失配），已改为取首次引入迁移；该文件定向复跑通过。 |
| 离线评估 | `cd backend; uv run --frozen python -c "...run_all()..."` | sha16 = `d1a8ff768a8afad6`，与 P72 记录的有效基线一致；topic/retrieval 未退化。 |
| 真实界面 | Chrome 真实渲染 + 隔离数据域 | 见下方「真实界面检查」与截图目录 |
| 文档一致性 | `python scripts/check_docs.py` | 通过 |
| 真实界面检查 | 实际运行后端 + 前端（隔离数据域），Chrome 真实渲染截图 | 1440×900 / 1024×768 / 800×600 / 480×600，亮色/暗色/减少动画；左右栏拖动与收起、窄窗口全窗口预览、五入口互斥展开、审批自动弹出与暂缓、任务多项展开、历史阅读与回到最新、长网址/代码/宽表格不横向溢出、多标签预览与全窗口、对话与设置往返、记忆问题定位；截图见 `docs/conversation-ui-screenshots-v2/` |
| 完整 CI | 推送最终分支后运行 | 见「最终记录」 |

- 模型调用：本任务没有改变主对话提示词体系，没有新增真实模型调用；所有后端测试继续使用
  fake/mock provider，无需真实密钥。
- 未修改模型注入提示词（`backend/src/agent/prompts.py` 一行未改）。

## 4. 合并步骤（供审查）

```powershell
git switch main
git merge --no-ff cv/main
# 按 AGENTS.md 重新运行后端/前端/文档检查；如 R9 已先合并，按契约 §0 的替换点处理冲突：
#   - 审批存储：stores/unifiedApprovals.ts + components/approval/UnifiedApprovalModal.vue
#   - 桌面退出/托盘：WindowChrome.vue 的布局代码不需要改，只保留 R9 的关闭实现
#   - 源文件引用：预览侧 conversation/preview.ts 的 target 构造与 conversationPreview.ts 的版本应用
# 未经明确要求：不合并 main、不打包、不发布。
```

## 5. 已知限制

- 桌面壳（Tauri 窗口拖动 / 最小化最大化关闭 / 托盘 / 真正退出）未在本轮真实安装包验证；
  界面检查在浏览器完成，窗口按钮按既有桌面外壳接线保留、不画假按钮。
- 回复代码块与任务输出正文只在内存：刷新后标签保留，但需从原消息/任务详情重新打开正文。
- PDF 搜索只提取前若干页文字；PDF 不提供正文复制；Office 预览为结构化正文/表格，不是原版面。
- 任务栏「工具执行」只收录真实调用记录中值得展示的执行；未触发的维护与计划中的工作不进入列表。
- 历史任务在后端重启后按最后已知终态展示，不重新标成运行中；实例归属无法确认时如实说明。

## 6. 最终记录

（最终 SHA / CI 运行号 / 全量结果在推送后回填。）
