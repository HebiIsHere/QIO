# 互动模式本轮修复 · 合并材料（fix/interactive-markdown-layout-continuity）

> 本轮**不合并 main、不发布、不打包**。本文只准备合并材料。
> 由 Lead 维护；数据以实际命令输出为准，未验证项如实标注。

## 1. 基线与分支

| 项目 | 值 |
| --- | --- |
| 仓库 | https://github.com/HebiIsHere/QIO |
| 基线（开始前 fetch 后的真实 origin/main） | `29c68b7a017de4d5897122cf1e9714bf4bbd2cae`（2026-10-15 13:52 `fix(interactive): 封面卡片的时间条与渐变光晕贴到卡片最下沿`） |
| 基线核对 | fetch 后 origin/main 与本任务书给定 SHA 一致；本地 `main` 落后（未使用）；工作树 `D:\qio-dev\qio` 上另有人未提交的改动，**全程未触碰** |
| 集成分支 | `fix/interactive-markdown-layout-continuity` |
| 集成 worktree | `D:\qio-dev\qio-iml` |
| 最终提交 | `4d1591a2`（见 §3 提交序列） |
| 子分支（各独立 worktree，已全部合并） | `wt/iml-a-layout`、`wt/iml-b-markdown`、`wt/iml-c-continuity`、`wt/iml-d-verify`（验收，未改被测源码） |

## 2. 契约与文档

- `_IML-COMMON.md`：开工前冻结的共享契约（F1–F8 + 文件归属表 + 运行命令）。
- `docs/interactive-mode-contract.md` **§17**：本轮冻结语义，明确取代 §1.4 的 reply 行、
  §15.1 / §15.2 的临时槽位口径、§15.6 相关已知限制、§14.5 的操作形状（改为 §17.4）。
- `docs/architecture.md` §15：模块表补 `geometry.ts`、改写 `cardLayout.ts` 职责、关键边界第 3/4 条。
- `docs/status.md`：新增 P66（第三十八轮）。

## 3. 提交序列（集成分支）

```
bd2ad4dc docs(iml): freeze shared contract (F1-F8, file ownership)
dae5220d fix(iml-b): 结果卡正文改用 Markdown 渲染（F3 / B1-B7）
a5d0e264 docs(contract): §17 冻结本轮语义
60602d00 Merge branch 'wt/iml-a-layout'          # F1/F2 几何权威化 + 统一落点/尺寸约束
fa2b637c Merge branch 'wt/iml-c-continuity'      # F4/F5/F6/F7/F8
c4208a8d docs(architecture): 卡片几何权威化与统一几何约束
6435de10 Merge branch 'wt/iml-a-layout'          # BoardCanvas 发布 scale（接通 F6）
dcf44140 docs(status): P66
4d1591a2 fix(interactive): 修 D1 边把手被连接点抢命中 + D2 新建卡压住既有卡（task-6）
```

## 4. 各目标的实现结果

| # | 目标 | 结果 | 关键落点 |
| --- | --- | --- | --- |
| 1 | 结果卡真正解析并显示 Markdown | **完成** | `BoardCard.vue`（reply → `MarkdownContent`）、`MarkdownContent.vue`（任务列表等）、「复制原文」 |
| 2 | 放大 / 平移 / 展开阅读 / 浮层变化不再造成自动移位、跳动、新重叠 | **完成** | `cardLayout.ts` 恒等映射、`BoardCanvas.vue` 去临时槽位、`BoardPreviewLayer.vue` |
| 3 | QIO 新建 / 移动卡片有可靠落点与尺寸保护 | **完成** | `backend/src/agent/interactive/placement.py`、`geometry.ts` |
| 4 | QIO 获得获准范围内的几何信息并可提出相对布局 | **完成** | `build_context.geometry`、`place{relativeTo,side,gap}`、冻结 view |
| 5 | 用户许可后的结果卡可作为后续提交依据 | **完成** | `models.CHECKABLE_KINDS`、`visible_except_deleted`、`result_added/result_updated` |
| 6 | 同批新对象可被后续已批准操作引用 | **完成** | `card:`/`group:` 符号、`understanding._validate_operation` 先收集声明、`build_plan` 稳定分配 |
| 7 | 用户要求实际排列时排序改变卡片位置 | **完成** | `order_group.arrange = row \| column` 真实写 `x/y`；无 `arrange` 只改逻辑顺序且文案明说 |

## 5. 验证记录

### 5.1 Lead（集成后，各只跑一次全量）

- 后端全量：`cd backend; uv run --frozen --no-sync pytest -q` → **全绿（无失败项）**。
  第一次运行时与前端全量套件**并发**，出现 1 例 `tests/test_acc_e_f16_clone_rollback.py::test_cancel_during_second_clone_rolls_back_first_clone`
  失败（取消竞态，残留 `.part`）；随后**单独重跑全量**无失败，该用例单独运行亦稳定通过 →
  判定为并发负载下的计时敏感用例，与本轮 diff 无关（本轮只改 `agent/interactive/*` 与
  `api/interactive_store.py`）。如实记录，未改该用例。
- 前端全量：`cd frontend; npx vitest run` → **全绿**。
- `npx vue-tsc --noEmit` → exit 0。
- `python scripts/check_docs.py` → `文档一致性检查通过`；`--selftest` → 6/6 符合预期。
- 修复 D1/D2 后**只重跑受影响面**：`npx vitest run src/interactive src/components/interactive` → 全绿。

### 5.2 CI

| 运行 | 提交 | 结果 | 链接 |
| --- | --- | --- | --- |
| 38704972644 | `dcf44140` | **success**（10/10 作业绿：backend py3.11 / py3.12 / windows-latest、frontend、install e2e、rust ×2、frozen worker、docs consistency、ci dedup preflight） | https://github.com/HebiIsHere/QIO/actions/runs/38704972644 |
| 38710760976 | `4d1591a2`（D1/D2 修复后） | **success**（10/10 作业绿） | https://github.com/HebiIsHere/QIO/actions/runs/38710760976 |

CI 触发策略：`ci.yml` 的 `push: branches: ["**"]` 对同一提交去重（`concurrency: ci-<workflow>-<sha>`），
本轮没有为规避次数改过 CI。第二次运行由修复提交自动触发（代码已变，属正常门禁）。

### 5.3 独立验收（D，真实浏览器 headless + 离线假厂商 API 闭环）

被测 `dcf44140`：

| 侧 | 基线 `bd2ad4dc` | 集成后 `dcf44140` |
| --- | --- | --- |
| 真实浏览器 | PASS 17 / FAIL 7 / 未验证 5 | **PASS 22 / FAIL 2 / 未验证 5** |
| API 闭环（假厂商） | PASS 15 / FAIL 6 / 未验证 2 | **PASS 21 / FAIL 0 / 未验证 2** |

必转项（FAIL→PASS）：C1.1（reply 卡 Markdown 全结构、无横向滚动）、C1.3（复制原文与 `card.content` 逐字相等）、
C4（展开不再推走邻卡）、C5.5（拉伸在邻卡边界 gap=16 停住、邻卡不动）、F6（`delivery.view` 非 null、scale 与界面一致）、
API 侧 C7.1（许可结果卡进入后续上下文）、F5.1、F7.1（`(0,0)` → `60,254`）、F7.2（`place` 精确且**预览 == 执行**）、
F7.3（1×1 → 180×96）、C10.arrange（真实写坐标）；C8 闭环与 C9.dup（重复批准不重复创建）保持 PASS。

发现并已修复的 2 项（`4d1591a2`，已复验 **PASS 21 / FAIL 0 / 未验证 0**）：
- **D1**：n/e/s/w 边把手正中被 `connect-point` 抢命中 → 抓边正中会开始拉线。修法：连接点命中区移到边热区内侧、
  各让开热区厚度（10px），四边中点归尺寸把手，连接点拖线仍可用（视觉上圆点向卡内挪 10px，按"所见即可点"取舍）。
- **D2**：`+添加` 新建卡在可见区域挤满时压住既有卡。修法：`planNewCardSlot` 两级搜索（可见区域 → 向右/下各延伸
  一整张卡 + gap），找不到才退化并由 AddMenu 如实提示；`findFreeSlot` 的既有契约未改。

D1/D2 复验证据：`D:\qio-dev\_iml-evidence\after-fix\RECHECK-REPORT.md`（逐项结论 + 量化证据）、
`acceptance-recheck-after-fix.json`、`shots/R-*.png`、脚本 `iml-recheck.mjs`。
- D1 复验：四边中点分别命中 `rz-hit-n/e/s/w`（不再是 `connect-point`）；点圆点仍能拉线并在服务端新增正式关系
  （`l_a–l_b`）；4 个圆点在卡内各内移 10px、自带可命中区域、亮暗主题均可见。
- D2 复验：常规场景新卡落 `(2336,0)` 与全部既有卡零重叠；25 张铺满 0..1564×0..1064 的挤满场景落 `(1580,0)`，
  零重叠且在可见区之外，且**未**触发「板面已无零重叠空位」的病态退化提示（走的是正常二级搜索）。
- D 的两处自我更正（已接受）：C5.2（八向拉伸基线本就可用）与 C5.3（增长上限基线本就可达 1600×1200）在阶段 1
是夹具伪影（把 8 次拉伸累积在同一张卡 / 鼠标无法移出屏幕），用相同脚本重跑冻结基线后确认。

### 5.4 未验证（如实，不得推断为已通过）

- C6.4 批量新建卡（浏览器未覆盖）。
- C9：未批准依赖、半途暂停继续、取消、失败撤回、重启恢复（只覆盖了重复批准幂等）。
- C10.order「只改逻辑顺序」的用户可见文案（API 未断言、浏览器未单独读）。
- 桌面壳（Tauri）内复验；浏览器验收为**无头 Chromium**（受"不在桌面开窗"约束）。
- 矮卡（约 170px 高）的**上边连接点圆点**经 D1 修法内移 10px 后，会落在卡头文字行附近（仍可点、仍可拖线，纯观感）。
- 可见区域确实放不下新卡时，新卡会落到视野外的空白处（这是「视野不是落点搜索范围上限」的预期结果），
  本轮不新增「定位新卡」提示，属下一轮取舍。
- 字体：`@fontsource/noto-serif-sc` 在 junction + Vite fs 白名单下 403，截图中文字体回落系统字体 ——
  **环境限制**，不影响结构判定，字体观感不作为验收依据。

### 5.5 证据位置

- 独立验收：`D:\qio-dev\_iml-evidence\after\PHASE2-REPORT.md`（逐条 before/after）、
  `acceptance-after.json`、`acceptance-api-after.json`、`shots\`、启动日志；
  基线对照 `...\baseline\`；污染轮留档 `...\baseline-contaminated-midrun\`。
  脚本：`D:\qio-dev\qio-iml-d\_iml-evidence\scripts\{iml-acceptance.mjs,iml-api-probe.mjs,iml-app.ps1}`。
- Lead 全量日志：`D:\qio-dev\_iml-evidence\full-frontend.txt`、`full-backend-alone.txt`、`full-backend.txt`。

## 6. 用户实际会看到的变化

1. **结果卡真的会排版了**：标题、列表、任务清单、引用、行内代码、代码块、表格、链接都按 Markdown 显示；
   工具栏多一个「复制原文」，复制到的是完整 Markdown 原文。
2. **卡片不再自己动**：缩放画布、平移、改窗口大小、开关聊天窗口 / 工具栏 / 审批浮层，卡片的位置与尺寸都不变；
   点「展开预览」只是把这张卡的内容切成阅读态，不会把它放大、也不会把旁边的卡推开。
3. **卡片可以比屏幕大**：可以大于视野、可以有一部分在屏外，靠平移缩放查看；只有点「定位／完整查看」时视野才一次性居中。
4. **拉伸更符合直觉**：拉东/南边不动左上角，拉西/北边保持对边；拉到邻卡边界会停住并给出简短原因，不会把别的卡挤走。
5. **新建卡不再叠在一起**：QIO 新建卡不再默认落在 `(0,0)`、也不会出现 1×1；用户点「+添加」时若可见区域挤满，
   会放到视野外的空白处（平移即可看到）而不是压在已有卡上。
6. **结果卡可以继续参与协作**：结果卡多了一个「允许 QIO 查看」的整卡许可（默认关闭，需要用户主动打开）；
   打开后下一次提交会把这张结果的正文与来源交给 QIO，于是可以说「根据上次这张结论继续分析」。
7. **同一批的新对象可以被后续操作引用**：例如"生成两张结果卡再连线""生成后分组再排序"能在一次审批里完成。
8. **说"排成一列 / 从左到右"会真的动位置**：只改逻辑顺序时界面会明说是逻辑顺序，不再把序号变化说成"已经重排"。

## 7. 已知限制

- 结果卡正文在前端**没有**展示层长度上限，因此没有前端截断标记；后端上下文对结果卡正文有上限并带
  `textTruncated` 显式标记（前端不截断，所以不会把围栏 / 列表 / 表格截成半段）。
- 用户拖动落点仍走 `frontend/src/interactive/board.ts` 的既有下落策略（为保留"拖到卡片上自动成组"的显式分组语义）；
  `geometry.ts` 覆盖拉伸与新建落点，QIO 侧在服务端复用同口径。
- 本轮不扩展通用工具调用、不开放删除 / 隐藏 / 改用户已有卡片正文 / 替用户改许可；不纳入其他 P2 外观功能。
- 连接点圆点较改动前向卡内挪了 10px（见 D1 修法），如评审要求贴边可再调整。

## 8. 合并建议

- 建议 **squash 或按 §3 的顺序保留提交**（三个子分支的提交已按 A → C → A 的顺序合并，直接合并集成分支即可）。
- 合并前请确认：CI（`4d1591a2` 的 run）`success`、D 的 D1/D2 复验结论、以及 `docs/status.md` 顶部"最后核对"日期。
- 本文写于合并前；最终执行结果：已合并 main（fast-forward）、打包 0.2.9，并已发布 GitHub Release `v0.2.9`。
