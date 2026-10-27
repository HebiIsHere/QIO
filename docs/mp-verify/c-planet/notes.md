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
