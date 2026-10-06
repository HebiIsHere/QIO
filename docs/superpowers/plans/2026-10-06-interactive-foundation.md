# 互动模式第一阶段实施方案（2026-10-06）

## 1. 起点与分支

- 仓库：`HebiIsHere/QIO`；起点提交 `ee6bbff`（= `origin/main`，本机已 fetch 核对 `git ls-remote`）。
- 总开发分支：`feat/interactive-foundation`（工作区 `D:\qio-dev\qio-int`）。
- 子智能体分支（同一原点）：`wt/int-a-board`、`wt/int-b-persist`、`wt/int-c-approval`。
- 独立复核分支：`wt/int-verify`（从集成后的提交再开）。
- 不合并 main、不发布安装包。

## 2. 方案要点

互动模式与对话模式并列（`#/interactive`），左侧板面 + 右侧可收起辅助区（回复 / 任务）。
数据含义、可见性边界、提交语义、审批语义、接口签名全部先冻结在
`docs/interactive-mode-contract.md`，子智能体只实现各自模块的**冻结签名**，不改公共约定。

实现骨架在派发前由 Lead 提交（迁移 23、共享模型、路由聚合、前端类型 / API 客户端 /
store / 视图 / 模式入口 + 三个占位组件），因此：

1. 三个子智能体从**同一个含接口的起点**分叉，各自工作区互不重叠；
2. 任何分支都能编译、能起应用，集成只是替换各自占位实现；
3. 公共文件（`server.py`、`router.ts`、`store`、`view`）只有 Lead 写。

## 3. 子智能体分工（写作用域见契约 §5）

| 子智能体 | 范围 | 主要验收 |
| --- | --- | --- |
| A 板面 | 卡片增删改复制、选择、拖动、分组、顺序、关系链接；板内搜索、勾选 / 隐藏 / 折叠 / 书签 | 分组与顺序规则（含有序组合并）、搜索能查到未勾选注释、普通移动不产生意图 |
| B 保存恢复 | 自动保存（不调用 QIO）、状态快照、草稿、有效改动、提交前后状态、可见范围、基准与失败处理 | 未勾选注释不泄露（含链接端点 / 组名）、失败不更新基准、撤销后只体现最终状态、空 / 重复提交幂等 |
| C 回复与审批 | 虚线预览、预览调整、单项 / 批量审批、冲突与依赖、需要更新、进度与影响说明、演示入口、撤回 | 4 项批量、冲突不能同时批准、依赖需前项完成 + 再次确认、材料变化禁止批准、失败撤回保留用户改动 |
| Lead | 公共设计、模式入口、store / API 客户端 / 视图、集成、冲突解决、最终验收 | 10 个端到端场景、文档同步、`check_docs`、截图证据 |

## 4. 阶段范围

做：入口与布局、材料卡片基础操作、分组 / 顺序 / 链接 / 多选与区域选择、注释勾选 / 隐藏 /
折叠 / 搜索 / 书签、真实保存恢复与提交前后状态、虚线预览与审批全流程、演示意图七种场景。
不做：高亮 / 箭头 / 手写 / 旋转、标签 / 筛选 / 遮挡顺序 / 直接包含 / 重点标记、QIO 真实模型理解与外部执行。

## 5. 验收办法

1. 后端 `uv run --frozen pytest` 全绿（含新增用例）；前端 `npx vue-tsc --noEmit` + `npm test`。
2. 端到端：`scripts/e2e_up.py` 起后端 + 前端，用 `scripts/visual_probe.mjs` 驱动真实 Chrome
   走完 10 个场景并截图（板面、提交、预览、批量审批、恢复）。
3. 独立复核子智能体在 `wt/int-verify` 上读实现 + 复现关键场景，输出问题清单。
4. 文档：`docs/status.md` 增里程碑，`python scripts/check_docs.py` 通过。

---

## 交付记录（2026-10-06）

- **起点提交：** `ee6bbff`（= `origin/main`，用 `git ls-remote origin refs/heads/main` 核对过；本地 `main` 与远端一致）。
- **总开发分支：** `feat/interactive-foundation`（工作区 `D:\qio-dev\qio-int`），**未合并 main、未发布安装包**。
- **子分支（都从 `26fa417` 这个含接口骨架的共同起点分出）：**
  - A 板面：`wt/int-a-board` → `fbf4398`
  - B 保存与可见范围：`wt/int-b-persist` → `b49a44e`
  - C 回复与审批：`wt/int-c-approval` → `ea7f2f5`
  - 独立复核：`wt/int-verify`（只读复核，产出报告与复现用例，不加入产品分支）
- **集成顺序：** A → B → C，之后是按复核结论的修复（B 的权限边界、C 的恢复身份与批量透传）与 Lead 的文档/验收脚本。
- **最终提交：** `bf942e5`（集成分支头）。
- **规模：** 相对起点 `54 files changed, 16673 insertions(+), 1 deletion(-)`。

### 最终验收（都在 `bf942e5` 上跑）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `cd backend; uv run --frozen pytest` | `2079 passed, 9 skipped`（exit 0） |
| 前端类型 | `cd frontend; npx vue-tsc --noEmit` | 无输出（exit 0） |
| 前端全量 | `cd frontend; npm test` | `106 files / 1041 tests passed`（exit 0） |
| 文档一致性 | `python scripts/check_docs.py` | 通过（28 个里程碑条目） |
| 真实界面验收 | `node scripts/interactive-verify/ui-scenarios.mjs` | 21 项断言全过（含真实鼠标拖动、批量审批冲突、重新打开恢复） |

界面验收的截图与说明见 `scripts/interactive-verify/README.md`。

