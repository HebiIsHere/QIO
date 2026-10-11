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

## probes/e-submit-read-once.probe.test.ts —— 提交成功路径「只读一次」探针

这是**我自己的**反例断言（不依赖 A 写的用例）：用调用计数证明成功提交之后的读取与批次记录只发生一次。

    # 1) 复制进前端源码树（运行完必须删掉，不提交）
    cd frontend
    New-Item -ItemType Directory -Force -Path src\__irm_int_e_probe__ | Out-Null
    Copy-Item ..\scripts\irm-int-verify\probes\e-submit-read-once.probe.test.ts src\__irm_int_e_probe__\ -Force
    # 2) 定向运行
    npm test -- src/__irm_int_e_probe__/e-submit-read-once.probe.test.ts
    # 3) 清理
    Remove-Item -Recurse -Force src\__irm_int_e_probe__

断言口径：

* 提交成功后 `fetchBoardState` / `fetchIntents` / `recordIntentBatch` 各只 +1；
* `fetchVisibleRange` 期望 +2：其中一次来自保存成功路径里那句 fire-and-forget 的
  `refreshVisibleRange()`（与提交后刷新无关），另一次才是提交后刷新；
* 第二条用例复刻 F10：读取失败时提交事实不变，只标「页面刷新失败」。

基线对照（c6a03b1）：**2 failed**，实测计数 `{ board: 2, intents: 2, batches: 2 }`（期望各 1），
第二条 `submitStatus=unknown`。原始输出见 `out/probe-read-once-baseline-c6a03b1.txt`。

## verify_submission_identity.py —— 稳定提交身份链路（契约 §1，第二段新增验收）

自己建库、自己起应用、自己注入 fake provider、自己数数据库行与模型调用次数，覆盖：

| 场景 | 断言 |
| --- | --- |
| S0 迁移 39 对象 | `board_submissions.client_request_id` 与唯一索引 `idx_board_submissions_client_request` 存在 |
| S1 同身份重试 | 200、返回原 submissionId / understandingId、`replayed=true`、库里仍只有一行、理解请求只有一个、**模型调用计数不变** |
| S2 同身份不同 note | 409 `request_identity_conflict`、不落新提交、不再调用模型 |
| S3 同身份不同 baseStateVersion | 同上 |
| S4 精确查证 | 两个身份各自查到自己的事实 |
| S5「最近一条」诱饵 | 从未用过的身份必须 404 且 `acceptance=null` |
| S6「时间接近」诱饵 | 紧接着提交之后查未用身份仍 404；旧身份仍返回旧事实 |
| S7 数据库事实 | 三个身份三条提交、无同 (board, 身份) 重复行 |

    backend/.venv/Scripts/python.exe scripts/irm-int-verify/verify_submission_identity.py \
        --json scripts/irm-int-verify/out/verify-identity-<sha>.json

**注意基准版本的口径**：新身份的提交用**当前** seq；同身份重试必须用**记录里的原 seq**
（重试时板面已经前进）。按契约 §1.2，身份命中要排在 stale_state 判定**之前**——
否则原 seq 会被判成「版本过期」。S1.5 就是这条红线的断言。

基线对照（c6a03b1，B 的迁移 39 还没到位时）：**15/33 通过**，失败项全部指向缺口
（列与索引不存在、重试被判 stale_state、查证端点不存在）。

## 取文件时的编码坑（Lead 复跑时踩到，记下来省下一次）

本机 harness 走的是 Windows PowerShell 5.1：裸重定向 `git show <rev>:<path> > file.py`
会写成 **UTF-16LE**，Python 直接跑会报语法/解码错误（不是脚本的问题）。取文件请用：

    git checkout <sha> -- <path>          # 推荐：git 自己按原样落盘
    git show <rev>:<path> | Out-File -Encoding utf8 <file>   # 备选（UTF-8）

本目录下的脚本与 JSON 证据都是用 Python 自己写的文件（UTF-8 无 BOM），可直接执行。

## out/ —— 证据

每次运行写一份 JSON（含 worktree、branch、HEAD、逐项 check 与结果）。
文件名带被检查的 SHA/版本，便于台账对齐。
