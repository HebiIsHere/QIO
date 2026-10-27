# C 组核查笔记（记忆星球页改版）

## 1. 「迁移 47 / 维护表」到底会不会被产品路径建出来 —— 核对依据

结论：**(a) 真实产品路径（后端启动跑 migrations）会建出维护表。**
`scripts/mp-verify/README.md` 早先那句「基线上还没接进 schema.py」是错的，已改正。

依据（只读基线 worktree `D:\\qio-dev\\qio-mp-base`，提交 d8cdcc41）：

| 事实 | 依据（文件:行号） |
| --- | --- |
| 迁移 47 的 DDL 定义 | `backend/src/agent/storage/migration_p2_b.py:25`（`MIGRATION = (47, [...])`；第 29/46/69 行分别是 `maintenance_batches` / `maintenance_items` / `maintenance_operations`） |
| 迁移 47 被登记进主迁移表 | `backend/src/agent/storage/schema.py:1465`（import `_P2_B_MIGRATION`）与 `schema.py:1468`（`MIGRATIONS.append(_P2_B_MIGRATION)`） |
| 补偿迁移 48 重放 46+47 的 DDL | `backend/src/agent/storage/schema.py:1503-1511`（`list(_P2_A_MIGRATION[1]) + list(_P2_B_MIGRATION[1])`） |
| 补偿版本号 | `schema.py:1514`（`COMPENSATION_VERSION = 48`） |
| 三张表是「必需对象」，缺了会触发补偿 | `backend/src/agent/storage/migrate.py:119-127`（`REQUIRED_OBJECTS['tables']` 含 `maintenance_batches` / `maintenance_items` / `maintenance_operations`）；补偿执行见 `migrate.py:433-445` 的 `_run_compensation` |
| 当前版本号 | `schema.py:1798-1799`（`SCHEMA_VERSION = COMPENSATION_VERSION = 72`） |

实测印证：用 `scripts/mp-verify/seed.py` 在隔离数据目录灌数据时，`ensure_maintenance_tables()` 报告
`{present: true, applied: false}` —— 表**本来就存在**（`apply_migrations()` 已建好），
只需要插数据，不需要补 DDL。

结论修正：这不是基线缺陷，是我最初读错了文件（只 grep 了 `schema.py` 里的字面 `47`，
而 47 是通过 `MIGRATIONS.append(_P2_B_MIGRATION)` 间接登记的）。B 的「整理记录」有真实数据可读。

## 2. 与本次改动无关的发现

（暂无；发现问题只记录、不顺手改。）
## 3. 本机 headless WebGL 的帧间隔是双模态（影响所有动画结论的判读）

本机（C 环境）与 A 环境的 headless Edge + SwiftShader 都呈现**双模态帧间隔**：约 7ms 的正常帧
与 100–180ms 的长帧交替出现（探针里可见 216ms、380ms 的长帧；见 `frames/*.json` 的 `longFrames`）。
后果：逐帧采样里单个 `|Δmp|` 可能很大，但那是**长帧**造成的，不是动画跳变。

因此本组的动画质量结论一律按下面三条给，不用原始 `|Δmp|` 下结论：
1. **几何不变式**：球心 x（画布中心）与选中标签 x 的起止值、终点是否精确落位（`--mp`=0/1）、过冲量；
2. **归一化步进**：`normStep = Δmp / (dt / 动画时长)`，基准取令牌的 `--mo-3-expand|collapse`（520/420ms），
   不是采样窗口长度；`normStep > 1.6` 才标记；被标记的帧再取**邻域三帧聚合速率**复核；
3. **长帧单列**：`dt > 60ms` 的帧单独列出，凡是标记帧紧邻长帧且长帧内 mp 几乎没动（采样器滞后一帧），
   就判为采样相位差而非真实瞬移。

## 4. 环境用到的 Python 说明

C 的验收后端用的是 `D:\qio-dev\qio-mp-int\backend\.venv\Scripts\python.exe`（通过
`serve.mjs --python` 显式传入），**PYTHONPATH 指向被执行 worktree 自己的 `backend/src`**
（`serve.mjs` 里写死 `PYTHONPATH: <worktree>/backend/src`，并设 `QIO_DATA_DIR`/`QIO_ALLOWED_ORIGINS`）。
Lead 在 12:55 修正了那个 venv 的 editable `.pth`（原本指向基线 `quf2-lead/backend/src`）并给后端补了
`PYTHONPATH=<int>/backend/src`。C 的**后端接口相关**核查只有 `seed.py` 灌数据一处（迁移/表结构读取），
12:55 之后重跑过（`maintenance_tables: {present: true, applied: false}`），**星球/前端视觉结论不依赖该 venv**。
