# 第三阶段工程遗留（F1–F4）：结论与证据

> Agent F · task-20。这里只写**跑出来的数字与结论**；用户可见的进度/发布口径由 Lead 收口到
> `docs/status.md`，本文件是专题记录（复现命令都写在每节里）。

## 总览

| 项 | 结论 |
| --- | --- |
| F1 容器镜像清理 | 新增清单/引用/候选/显式清理（三道闸，被引用绝不删）；真实 docker 路径由 ubuntu CI 的
  `requires_docker` 用例覆盖 |
| F2 TraceStore 写放大 | **负结论：不重构**（四档规模单次成本 3.2 → 6.1 ms，只涨 1.9x；裸写基线同样量级） |
| F3 大库迁移 | 迁移本身很快且数据无损（中档 34 MB 库 40 ms / 内存峰值 2 KB）；**但发现并修复了
  「失败不回滚 + 崩溃后不可启动」的可靠性缺陷** |
| F4 CI 警告 | 四个 action 最小升级到首个 node24 版本；CI 全绿（见汇报） |

## F1 容器镜像

### 现状（改动前）

`tools/tool_envs.py` 会按指纹构建 `qio-tool-env:<py>-<fingerprint>`（`container_image_for`），
复用靠 `docker image inspect` 命中。但**没有任何地方能列出本机有哪些 QIO 镜像、谁在用、哪些
可以清**；`tools/` 下也没有 `container_envs.py`（容器逻辑都在 `tool_envs.py`）。专用 **Python
环境**那边早就有 inventory/引用/候选/显式清理（`inventory` / `cleanup_candidates` / `remove` /
CLI），容器镜像缺这一层。

### 做了什么（纯追加，没有改已有行为）

| 能力 | 入口 |
| --- | --- |
| 清单 | `ToolEnvManager.container_inventory()`（`parse_docker_images()` 解析 `docker image ls --format "{{json .}}"`） |
| 引用判定 | `references_from_definitions()` 的引用表 → 每个环境映射到镜像 tag |
| 最后使用 | 锁定记录里的 `last_used_at`（`mark_used` 更新）与 `container.built_at`（构建时间） |
| 清理候选 | `container_cleanup_candidates()`（默认不含来路不明的） |
| 显式清理 | `remove_container_image()` / `container_cleanup()`；CLI `images` / `rm-image` / `cleanup-images` |

三道闸（顺序即优先级）：

1. **只认 `qio-tool-env:` 前缀**：别人的镜像、`python:3.11-slim` 这类基础镜像一律不碰；
2. **被已注册工具引用的镜像绝不删**：tag 就是环境指纹，删了那组依赖就没有可用镜像 ——
   没有任何 override 能绕过（这条是任务书的硬约束，测试里用定点变异验证过它真的会挡住）。
   引用判定不依赖环境记录：注册工具声明的依赖集合 → 应然 tag 会**直接算进清单**并标 protected，
   所以锁定清单被删掉、或镜像在旧版本里建的而记录没留下时，仍然拦得住；
3. 必须显式确认；另外**拿不到引用表时保守拒绝**（「看起来没人用」可能只是「不知道谁在用」），
   CLI 要显式写 `--assume-unreferenced` 才放行。

### 验证

* 本机**没有 Docker 守护进程**（`docker` CLI 在、daemon 不在）。所以：
  * 纯逻辑（解析、大小换算、候选、保护规则、拒绝路径）用 15 条普通用例覆盖，docker 命令走
    **替身 runner**：连「拒绝时一个 `rm` 都不发」都断言；
  * 真实命令行路径由 1 条 `@pytest.mark.requires_docker` 用例覆盖：`FROM scratch` 造两个本地
    镜像（不联网）→ 真 `docker image ls` 清单 → 真删孤儿镜像 → 被引用的那个断言 `docker image
    inspect` 仍然成功。windows CI 任务按标记过滤，**ubuntu CI 任务照常跑**（沿用
    `test_tool_container_env.py` 的既有约定：CI 上守护进程不可用会直接 fail，不静默跳过）。
* 三条安全用例做了**定点变异**验证（把保护分支/保守拒绝/候选过滤分别拆掉 → 对应用例变红）。

### 限制（诚实）

* 本机无法执行真实 `docker image ls / rm`，结论以 ubuntu CI 为准；
* 「最后使用」用的是环境/锁定记录里的时间（工具走宿主解释器路径时更新），镜像本身没有
  独立的 last-used 记录 —— docker 也不提供；拿不到时显示「未知」，不猜。

## F2 TraceStore 写放大：负结论（不重构）

复现：`cd backend; $env:QIO_DATA_DIR=''; uv run --frozen python ../scripts/bench_trace_store.py`

四档规模（每档 3 次取中位数；每次从空 trace 开始；「裸写」= 同连接、同样 autocommit、单行
`UPDATE` 改一个值 —— 用来量出「机器自己的写盘成本」）：

| 规模 | 事件数 | 总耗时 | 单次成本 | 裸写基线 | 相对裸写 |
| --- | --- | --- | --- | --- | --- |
| short | 5 | 0.0158 s | 3.166 ms | 0.0181 s | 1.1x |
| medium | 45 | 0.2840 s | 6.310 ms | 0.1782 s | 1.0x |
| long | 150 | 0.8039 s | 5.359 ms | 0.7010 s | 1.3x |
| heavy | 600 | 3.6489 s | 6.081 ms | 2.1157 s | 1.7x |

读法：

* 单次成本从 3.166 ms（5 事件）涨到 6.081 ms（600 事件）= **1.9x**，而事件数涨了 120x ——
  没有出现「随记录数非线性暴涨」的情况；
* 与**裸写基线**同量级（1.0–1.7x）：开销的大头是 SQLite 每条语句自身的提交/写盘，不是
  「读整列 JSON → 改 → 写回」这一形状；
* 真实上限参考：主循环默认 `default_iterations = 128`（native），一次「继续」再加 32 ——
  150 事件这一档已经是很重的回合（多花约 0.10 s，相对裸写基线）。

**结论：不动存储结构。** 只有当一轮的 trace 事件数常态化超过数百条时才值得回头做追加表
（那时要把「整轮读出的语义完全一致」写成对拍测试）。

## F3 大库迁移

复现：`cd backend; $env:QIO_DATA_DIR=''; uv run --frozen python ../scripts/verify_migration_large_db.py --scale small|medium|large`

脚本用**真实迁移列表**把库建到 23 版，再灌 synthetic 数据（话题/片段/消息/trace/工具历史），
然后跑真实 `apply_migrations`。**不使用任何用户真实数据库**。

修复后的三档实测（每档都是全新的 23 版 synthetic 库）：

| 规模 | 行数（trace / 消息 / 工具记录） | 库大小 | 迁移耗时 | 内存峰值 | 数据无损 | 重启幂等 | 失败回滚 | 崩溃窗口 | VERDICT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| small | 200 / 1 000 / 200 | 0.7 MB | 0.1044 s | 3 532 B | 是 | 是 | 版本不推进、无半截 DDL | 自愈 ok | ok |
| medium | 20 000 / 100 000 / 5 000 | 34 MB | 0.0718 s | 3 532 B | 是 | 是 | 同上 | 自愈 ok | ok |
| large | 100 000 / 500 000 / 20 000 | 169 MB | 0.0699 s | 3 532 B | 是 | 是 | 同上 | 自愈 ok | ok |

读法：迁移耗时与库大小**无关**（169 MB 的库 70 ms，0.7 MB 的库 104 ms，差异在噪声里），
内存峰值只有几 KB —— `ALTER TABLE ADD COLUMN` 是元数据级操作，SQLite 不重写表；库大小前后
一致（169 MB → 169 MB）也印证了这一点。脚本的灌数用一个显式事务包起来（autocommit 下逐行
提交会让大档变成几十万次 fsync，几分钟起步），所以三档总共只要几十秒。

结构检查（三档一致）：`turn_traces.phases` 列存在、旧行默认 `{}`；`turn_journal` 表与
`idx_turn_journal_status` / `idx_turn_journal_created` 建好；行数与关键字段（`turn_id`/`status`/
`duration_ms`/`final_preview`/消息正文）前后一致。

### 发现的缺陷：失败不回滚 + 崩溃后不可启动（已修）

同一脚本里的「失败回滚」与「崩溃窗口」两段实测（修复前）：

```
"partial_ddl_left_behind": true,
"crash_window_restart": "raises: duplicate column name: phases"
```

根因：连接是 autocommit（`storage/db.py` 的 `isolation_level = None`），`with conn:` 在这种
连接上**不会隐式开事务**，迁移的每条语句各自落盘。于是「语句 1 生效、语句 2 失败」会留下
半截 schema 而版本号不推进；migration 24 是单条 `ALTER TABLE ... ADD COLUMN phases`，一旦
ALTER 生效、版本行没写（崩溃/断电），下次启动重放就永远 `duplicate column name: phases`。

修复（Lead 授权，`agent/storage/migrate.py`）：

1. 每条迁移的「全部语句 + 写 schema_version」包进一个真实事务（`agent.storage.db.transaction()`
   = 显式 BEGIN/COMMIT），并把「为什么 `with conn:` 在 autocommit 下是空操作」写进模块 docstring；
2. **窄口径自愈**（选 (a) 而不是「只报可行动错误」）：只有「对象已经存在」
   （`duplicate column name` / `already exists`）当作「这条已应用过」并记 warning，其它错误照旧抛 ——
   存量库里已经处于半截状态的安装能自己起来，而真正的迁移错误不会被吞掉。

修复后的实测（三档一致）：`failure_raised: true`（错误是 `no such table: no_such_table_here`）、
`version_after_failure` 仍是 25、`partial_ddl_left_behind: false`、`already_applied_is_tolerated: true`（自愈
边界：只有 duplicate column / already exists 才被吞）、`crash_window_restart: "ok"`（半截库能起来）。

修复前后（`backend/tests/test_migration_atomicity.py`，4 条）：

* 修复前：3 条红 —— 半截 DDL 留下（`assert 'atomicity_probe' not in [...]` 失败）、半截库启动抛
  `duplicate column name: phases`、`_already_applied` 不存在；
* 修复后：4 条全绿（含常规 23 → 25 升级 + 数据不丢 + 重启幂等）。

## F4 CI 警告

观测到的警告：`actions/checkout@v4`、`actions/setup-python@v5`、`actions/setup-node@v4`、
`astral-sh/setup-uv@v5` 都跑在 Node.js 20 上、被强制迁移到 Node.js 24（Node 20 已弃用）；
另有 `ubuntu-latest` 标签迁移提示（信息性，无需动作）。

最小升级（逐个查过目标版本的 `action.yml` 的 `runs.using`）：

| action | 原 | 新 | 依据 |
| --- | --- | --- | --- |
| actions/checkout | v4 | **v5** | v5 起 `using: node24` |
| actions/setup-python | v5 | **v6** | v6 起 `using: node24` |
| actions/setup-node | v4 | **v5** | v5 起 `using: node24` |
| astral-sh/setup-uv | v5 | **v7** | v6 仍是 `node20`，v7 才是 `node24` |

升级后的验证：CI run **37030691212 = completed / success，8/8 全绿**，其后的提交
（37031735492 docs、37032498417 F1 加固）同样 8/8 全绿（backend py3.11 / py3.12 /
windows-latest、frontend、rust ×2、frozen worker、docs）。那一跑同时也跑了 F1 的真 docker 用例
（ubuntu 上守护进程可用，Windows 任务按标记过滤）。

没动的东西：`dtolnay/rust-toolchain@stable`、`Swatinem/rust-cache@v2`（无此警告）；
frontend 的 `node-version: "20"` 是**项目运行时**而非 action 运行时，本轮不动。
setup-uv v7 的 inputs 与在用的一致（python-version / enable-cache / cache-dependency-glob）。

## CR1（Lead 追加）：进程模型与 Job Object 时序进 CI 门槛

`frozen worker (windows)` 任务里加了两步，紧跟既有冒烟之后，用同一构建产物
`frontend/src-tauri/binaries/qio-backend-x86_64-pc-windows-msvc.exe`（路径来自 `scripts/build_sidecar.ps1`，
与既有冒烟步骤一致）：

* `python scripts/verify_backend_process_model.py --exe <exe> --case 2 --json <runner_temp>`
* `python scripts/verify_backend_process_model.py --exe <exe> --case job --json <runner_temp>`
* 两步后面各串一个 `python scripts/check_backend_process_model.py --report <json> --case 2|job`。

**为什么需要检查器**：`verify_backend_process_model.py`（Agent A）只采集数据 —— `main()` 跑完
`return 0`，只有 exe 缺失/非 Windows 才返回 2。直接拿它当门槛等于纸面能力：进程模型退化了它照样绿。
`scripts/check_backend_process_model.py` 读它的 JSON 断言四条形状（同 exe 的两个进程、只杀 launcher 会
留下占端口的孤儿、立即 assign 收住 child 且关句柄不留孤儿、延迟 assign 会逃逸），非 0 退出即步骤红，
并把每条未通过的检查发成 `::error` 注解。自带 `--selftest`（9 个场景：好报告过、每种退化必须红）。

两步都设 `PYTHONIOENCODING: utf-8`：Windows runner 的 Python 标准输出默认 cp1252，脚本里的中文
输出会撞 `UnicodeEncodeError`（第一次上线就是这么红的）；没有 `continue-on-error`。

验证：run **37035687098 = 8/8 全绿**（frozen worker 2m42s）。CI 实测：case 2 `ready=True in 10.234s`、
两个同 exe 进程（ppid 7392 → 8304）、`结束 launcher 后 child 仍在=[8304] 端口仍开=True`；
case job 立即 assign `child_in_job=True` + 关句柄后「无相关进程 + 端口仍开=False」，延迟 assign
`child 在 job 里=False` + `端口仍开=True`；嵌套 job 成立；壳被强杀残留 0。

`scripts/verify_backend_process_model.py` 逐字节取自 Agent A 的 `wt/p3-a`（`git diff wt/p3-a -- <path>` 为空），
合并时以 A 的版本为准。
## CR2（Lead 追加，Agent B 路由）：安装包真机 E2E 进 CI

新增任务 `install-e2e (windows-latest)`（14 步，`timeout-minutes: 45`）：

```
npm ci → setup-uv + uv sync --frozen --extra dev → build_sidecar.ps1（真 sidecar）
→ 合成内置模型（model.onnx + 与它相符的 model_manifest.json）
→ npm run tauri build -- --bundles nsis --config <绝对路径的无 BOM JSON，关掉 createUpdaterArtifacts>
→ scripts/install_e2e.py --stages decoy,install      # /S 静默安装 + 注册表断言
→ 把一个替身 qio-backend.exe（ping -t）放进安装目录并启动
→ scripts/install_e2e.py --stages uninstall --uninstall-with-running-backend
```

**为什么必须放在 CI**：本机（含 Lead 那台）子进程是受限令牌 —— 安装器的 `WriteRegStr`、卸载器的
`DeleteRegKey` 都 ACCESS_DENIED 且静默失败，只能记 NOT VERIFIED。runner 能写注册表。
没有 `continue-on-error`：`install_e2e.py` 有任何 FAIL 就 `return 1`，即这一步红。

### CI 上的实测（run 37042120092，install e2e 任务）

安装阶段：**6 PASS / 0 WARN / 0 FAIL**

```
A-009 诱饵注册表已生效
A-010 诱饵实验：静默安装不调用既有安装的卸载器 :: 诱饵卸载器没有被执行（exit=0）；安装目录存在=True
A-020 安装目录内容 :: 5 个文件（78.2 MB）
A-021 安装目录关键文件 :: qio.exe / qio-backend.exe / uninstall.exe /
      models/bge-small-zh-v1.5/model.onnx / models/bge-small-zh-v1.5/model_manifest.json
A-023 安装信息写入注册表 :: InstallLocation=D:\a\_temp\qio-install-e2e\install
      DisplayVersion=0.1.10  Software\qio\QIO=<install dir>
```

卸载阶段（sidecar 仍在跑）：**9 PASS / 0 WARN / 0 FAIL**

```
A-087 卸载前故意让 sidecar 继续运行 :: 运行中的 sidecar pid=[5048]
A-090 卸载后安装目录被清空 :: exit=0 残留：[]
A-091 卸载保留用户数据（未勾选删除数据） :: 数据目录 [user-state-marker.txt] -> 同
A-092 卸载后卸载注册表项被移除 :: REMOVED
A-093 卸载清掉安装信息（安装位置 / Installer Language） :: 键内剩余值：[DbBaseline]
A-094 卸载保留用户状态（DbBaseline 不被顺手删掉） :: 合成 DbBaseline 卸载后仍在
A-095 卸载器自己收掉了还在跑的 sidecar :: 卸载后按路径枚举：无残留进程
```

### 三点如实说明（不含糊）

1. 内置模型是**合成**的（CI 没有 90MB 模型的来源）：这一步验的是安装/卸载**机制**与注册表边界，
   不是真实模型产物（真实模型由 release gate 与 B 的本机 E2E 覆盖）；
2. 「sidecar 在跑」用的是**替身进程**（`ping.exe` 拷成 `qio-backend.exe`，从安装目录启动）：真 onefile
   的 launcher/child 形状由 frozen worker 任务的进程模型门槛覆盖；这一步验的是钩子
   （`CheckIfAppIsRunning "qio-backend.exe"`）与卸载器行为；
3. 没跑 `preflight` / `restore` 阶段：那是「备份并还原本机既有安装」的步骤（reg export/import `D:\QIO`），
   CI 上没有既有安装。工作目录全部在 `$RUNNER_TEMP`。

### 迭代记录（三次真实反馈）

1. 红：`build_sidecar.ps1` 在 `backend\.venv` 里没有 PyInstaller，回退到
   `uv run --frozen --with pyinstaller` —— 新任务忘了装 uv（frozen worker 任务有 setup-uv）。补上；
2. 顺手把 `--config` 改成绝对路径 + 无 BOM UTF-8（与 `build_installer.ps1` 验证过的写法一致）；
3. 红：`--stages install` 只做「检查安装目录 + 注册表断言」，**真正的 /S 安装发生在 decoy 阶段**
   （`step_decoy` 里的 `install_cmdline`）→ `A-020 安装目录生成 :: ... 不存在`，退出码 5。
   改成 `--stages decoy,install`；两个步骤的失败诊断也改成 GitHub 注解（`::error title=A-xxx ...`），
   这样失败不用登录下载日志就能定位。
## 本机环境陷阱（本轮踩到、已同步给 Lead）

1. 工作树的 venv 需要用 `uv sync --frozen --extra dev` 才有 pytest；否则 `uv run --frozen pytest`
   会**回退到系统 Python 3.10**（那里没有 keyring 等依赖），测试结果不可信。跑测试统一用
   `uv run --frozen python -m pytest`。
2. 任何用到临时目录的工具都要先把 `TEMP`/`TMP` 指到工作树里可写的目录。
3. `%TEMP%` 与 C: 盘都曾被写满：一次大库验证直接以 `sqlite3.OperationalError: database or disk
   is full` 收场（这是环境问题，不是迁移缺陷）。