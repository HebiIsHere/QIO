# scripts/irm-int-verify —— 整合轮 E 的独立验证入口

这里的脚本只做**定向、可复现**的验证，不跑全量（额度红线见 docs/integration-contract-20261011.md §5）。
全部脚本用 worktree 里已接好的虚拟环境执行：

    backend/.venv/Scripts/python.exe scripts/irm-int-verify/<script>.py [参数]

## verify_migrations.py —— 独立迁移验证

自己建库、自己升级、自己制造缺口，不看任何别人的自述。六个部分：

| 部分 | 场景 | 要求 |
| --- | --- | --- |
| A | 空库从零迁移 | 版本到顶、REQUIRED_OBJECTS 齐全、真实模型对象与列存在、版本行不重复 |
| B | 真实应用库升级（默认 \`%APPDATA%\qio\app.db\`） | 版本推进到顶、对象齐全、**既有表行数不变**、新增表都来自未应用的迁移语句 |
| C | 缺对象补偿 | 删表 / 删索引 / 删列后按对象检出 → 重放补偿迁移补齐 → 可重复触发 |
| D | 重复启动 | 连续三次启动版本稳定、版本行不重复 |
| E | 合成存量库（停在 36、带数据） | 升级后草稿 / 板面 / 提交 / 意图正文一行不变，新列为默认值 |
| I | 静态不变式 | SCHEMA_VERSION / 补偿编号 / 补偿 = 上一条完整迁移 + 本轮新增 / 真实模型对象首现于 37 / REQUIRED_OBJECTS 每项都有补偿语句 |

    backend/.venv/Scripts/python.exe scripts/irm-int-verify/verify_migrations.py \
        --json scripts/irm-int-verify/out/verify-migrations-<sha>.json

退出码 0 = 全过。\`--real-db\` 可换库，\`--keep-tmp\` 保留临时库用于人工检查。

## check_conflict_symbols.py —— 9 个共同修改文件的行为符号级核对

对「两条来源分支都改过的共同文件」，逐侧取「该侧引入、merge base 里没有」的标识符与字符串字面量，
再在整合版本里核对是否还在（给首次出现行号）。

    backend/.venv/Scripts/python.exe scripts/irm-int-verify/check_conflict_symbols.py \
        --base 8fa8452 --rel 4890870 --irm 7158152 --int HEAD \
        --json scripts/irm-int-verify/out/symbol-check-<sha>.json

**边界（诚实声明）**：这是名字级核对，只证明「这条线引入的符号没有整条消失」，
不证明语义没有变化；语义结论必须另有阅读或实测证据（见 docs/integration-review-20261011.md）。

## out/ —— 证据

每次运行写一份 JSON（含 worktree、branch、HEAD、逐项 check 与结果）。
文件名带被检查的 SHA/版本，便于台账对齐。
