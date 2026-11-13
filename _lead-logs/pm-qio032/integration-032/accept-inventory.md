# QIO 0.3.2 集成轮 · 本地定向验收入口清单（accept-inventory）

> 核实时点：2026-11-13 23:54 +08:00（Asia/Shanghai；最后复核 23:57，HEAD 未变）。集成 worktree 正在被 Lead 合并，本文件是**快照**；跑验收前先重新核对 HEAD 与文件存在性（见 `5.1`）。
> 集成 worktree：`D:\qio-dev\pm-qio032-integration`，branch `fix/pm-qio032-integration`，HEAD `e324e392974209161a7640a8af237723058f7fed`。
> 产品基线：`7445170d0f51756b2740eb7209d798540d8042a2`（2026-11-12 16:18:57，`docs(release): v0.3.2 已知限制标注本轮真机验收未执行（UNVERIFIED）`；已验证是集成 HEAD 的祖先）。
> 所有命令都在本机实际执行核对过（除注明“未运行”的）；数字为当时实测，不作为承诺。

## 0. 一屏结论

1. **9 个点名用例/脚本全部存在于集成 worktree**（`Test-Path = True`，23:54 核对）；其中 8 个是本轮分支改动/新增，`layoutPrefs.test.ts`、`settings-search.test.ts` 是基线文件（本轮被 shell 分支改动/触碰）。
2. 两个“提交脚本”**不是 CLI 脚本**，而是 `backend/tests/` 下的 pytest 文件（`scripts/`、`backend/scripts/` 下不存在同名脚本）。
3. **关键坑**：`pm_qio032_submit_material_versions.py`、`pm_qio032_submit_first_round_materials.py` 及 4 个同族文件**文件名不匹配 pytest 默认收集规则**（`test_*.py` / `*_test.py`），全量 pytest 不会收集它们；必须显式写文件路径（已在 `2.1` 给命令）。
4. 本机实测：3 个后端点名文件 **27 passed / 18.3s**；6 个前端点名文件分两批跑 **11 + 32 passed / 12.9s、12.1s**（当时集成 HEAD 未变；23:53 有一个点名测试文件被 Lead 未提交修改）。
5. 门禁现状：`python scripts/check_docs.py` **通过**（121 个里程碑，0.48s）；`npx vue-tsc --noEmit` **通过**（30.4s）；`git diff --check 7445170d..HEAD` **当前为红**（exit 2，3 处空白错误，见 `2.4`）。
6. E2E 最终台账 `summary.json` = **38/40 PASS，2 项 UNVERIFIED（②-2/②-3，不是产品缺陷）**；`run.ps1` 因 UNVERIFIED 会返回 exit 1，判读必须看 `detail.kind`（见 `3.6`）。
7. 全量规模：前端 **612 个受跟踪测试文件 / 约 5325 条用例**（全量实测历史 583 文件 5065 条 519s、589 文件 5104 条 281s，预计本机 5–9 分钟）；后端全量历史 **6904 条 / 715.4s**。**本轮只跑定向，不要跑全量。**

## 1. 用户点名用例/脚本的定位与核对

### 1.1 结论表（全部在集成 worktree 中 Test-Path=True）

| # | 点名项 | integration 绝对路径 | 归属分支 / 来源 worktree | 最近改动 commit | 实测（2026-11-13 23:48–23:52，HEAD e324e392） |
|---|---|---|---|---|---|
| 1 | `pm_qio032_material_scope_test.py` | `D:\qio-dev\pm-qio032-integration\backend\tests\pm_qio032_material_scope_test.py` | `fix/pm-qio032-interactive`（i-material 组）；也存在于 pm032-interactive、pm-qio032-i-material / i-state / i-submit | `facb8a84` 2026-11-13 `feat(interactive-material): 材料内容版本冻结接口…`（引入 `81e04c9d`） | 21 tests；三文件合计 27 passed / 18.3s |
| 2 | `pm_qio032_submit_material_versions.py` | `D:\qio-dev\pm-qio032-integration\backend\tests\pm_qio032_submit_material_versions.py` | `fix/pm-qio032-interactive`（i-submit 组）；也存在于 pm032-interactive、pm-qio032-i-material / i-submit | `690df33d` 2026-11-13 `fix(interactive-submit): 提交受理冻结材料内容版本…（#28）` | 2 tests；单跑 2 passed / 9.0s；**默认不被收集** |
| 3 | `pm_qio032_submit_first_round_materials.py` | `D:\qio-dev\pm-qio032-integration\backend\tests\pm_qio032_submit_first_round_materials.py` | `fix/pm-qio032-interactive`（i-submit 组）；也存在于 pm032-interactive、pm-qio032-i-canvas / i-material / i-state / i-submit | `0b9e01ae` 2026-11-13 `test(interactive-submit): 首轮参与材料范围用例…`（引入 `ab579d68`） | 4 tests；**默认不被收集**；文件末尾多空行（见 `2.4`） |
| 4 | `pm032-canvas-import-batch.test.ts` | `D:\qio-dev\pm-qio032-integration\frontend\src\components\interactive\__tests__\pm032-canvas-import-batch.test.ts` | `fix/pm-qio032-interactive`（i-canvas 组）；也存在于 pm032-interactive、pm-qio032-i-canvas / i-material / i-state | `3440edbf` 2026-11-13 `fix(interactive-canvas): 拖放/多文件导入批次反馈 UI（#44/#45/#46）` | 8 passed / 23.1s（单条最长 3.5s） |
| 5 | `layoutPrefs.test.ts` | `D:\qio-dev\pm-qio032-integration\frontend\src\conversation\layoutPrefs.test.ts` | 基线文件；本轮由 `fix/pm-qio032-shell` 改（#83）；五个来源分支都有，interactive/planet/settings/records 仍停在 `5a93fe32`（10-22） | `89976073` 2026-11-12 `fix(shell-layout): #83 按中央实际可用宽度解析三栏…` | 11 passed / 12.9s |
| 6 | `settings-search.test.ts` | `D:\qio-dev\pm-qio032-integration\frontend\src\views\settings\__tests__\settings-search.test.ts` | 基线文件（`53646536` 2026-10-28 引入）；集成与 pm032-shell 最近被 `c9ac7b33` 触碰，其他来源分支停在 `50eda618`（10-28） | `c9ac7b33` 2026-11-12（subject 显示为 planet 浮层修复的合并提交） | 15 passed（与另外 3 个点名文件同批 32 passed / 12.1s） |
| 7 | `cv-c-preview-50-53-state.test.ts` | `D:\qio-dev\pm-qio032-integration\frontend\src\components\conversation\__tests__\cv-c-preview-50-53-state.test.ts` | `fix/pm-qio032-shell`（shell-preview 组）；也存在于 pm032-shell、pm032-shell-preview | `c872f16a` 2026-11-12 `test(preview): 补 #52 焦点标签快捷键与 #50/#53 外壳用例` | 9 passed；23:53 该文件有 Lead 未提交修改（+10/-3），以最新版本重跑为准 |
| 8 | `d-preview-52-tab-focus.test.ts` | `D:\qio-dev\pm-qio032-integration\frontend\src\components\conversation\__tests__\d-preview-52-tab-focus.test.ts` | `fix/pm-qio032-shell`（shell-preview 组）；也存在于 pm032-shell、pm032-shell-preview | `c872f16a` 2026-11-12（同上） | 3 passed（同批） |
| 9 | `cv-c-preview-feedback.test.ts` | `D:\qio-dev\pm-qio032-integration\frontend\src\components\conversation\__tests__\cv-c-preview-feedback.test.ts` | `fix/pm-qio032-records`（records-feedback 组）；也存在于 pm032-records、pm032-records-feedback | `7a716c54` 2026-11-13 `fix(records-feedback): #57 预览成功反馈由面板承担…` | 5 passed（同批） |

### 1.2 点名项的两个重要差异

- **“submit 脚本”其实是 pytest 用例**：`pm_qio032_submit_material_versions.py`、`pm_qio032_submit_first_round_materials.py` 在 `backend/tests/`，不是可直接执行的提交脚本；用 pytest 跑，不是 `python xxx.py`（文件里也没有 `__main__` CLI 入口）。
- **默认收集规则**：`backend/pyproject.toml` 没有自定义 `python_files`，pytest 默认只收 `test_*.py` / `*_test.py`。实测 `pytest tests -k qio032 --collect-only` 只收到 `pm_qio032_material_scope_test.py`(21) + `test_pm_qio032_entity_zero_confidence.py`(3) + `test_qio032_restore_idempotency.py`(3)；`pm_qio032_submit_*.py` 6 个文件全部**不在**收集结果里。跑它们必须显式列文件名。
- 同族但未被点名的 4 个文件（同样默认不收集）：`pm_qio032_submit_authorization_facts.py`、`pm_qio032_submit_empty_semantics.py`、`pm_qio032_submit_intents_event.py`、`pm_qio032_submit_replay_lookup.py`；另有 `test_records_facts_file_read.py`（默认收集）。建议把同族一起显式跑（见 `4.2`）。

### 1.3 来源 worktree 事实核对

| worktree | 分支 | HEAD（本机 `git rev-parse HEAD`） | 与用户给定 SHA |
|---|---|---|---|
| `D:\qio-dev\qio` | `feat/window-frame-032` | `9c781e0e3830f58f76a3c6fde42df94df9c2dda0` | 主仓，非本轮基线提交（基线是 HEAD 祖先） |
| `D:\qio-dev\pm032-shell` | `fix/pm-qio032-shell` | `a8c9222b1525ed0e5e6f8874487e7748f070cafb` | 一致 |
| `D:\qio-dev\pm032-interactive` | `fix/pm-qio032-interactive` | `08d08955549d3843603dc160b4da2fdee1cb799d` | 一致 |
| `D:\qio-dev\pm032-planet` | `fix/pm-qio032-planet` | `0db3521dba6e8b38ba2d4a6017fdd1d7bd49c1fa` | 一致 |
| `D:\qio-dev\pm032-settings` | `fix/pm-qio032-settings` | `baf2a1f9ce4c2ef333e73ed68bba7f56b7afb2ab` | 一致 |
| `D:\qio-dev\pm032-records` | `fix/pm-qio032-records` | `30575cebcf942b7c0a4f137996554c962d35a8de` | 一致 |
| `D:\qio-dev\pm-qio032-integration` | `fix/pm-qio032-integration` | `e324e392974209161a7640a8af237723058f7fed` | 集成中，持续变化 |

另有本次未点名的子分支 worktree（`pm-qio032-i-canvas/i-layout/i-material/i-state/i-submit/i-tasks`、`pm032-shell-preview`、`pm032-records-feedback` 等），点名文件在这些副本里也有，但**验收以集成分支为准**，来源分支只作“来源事实”对照。

## 2. 精确命令（可直接复制）

### 2.0 公共前置

~~~powershell
$WT = "D:\qio-dev\pm-qio032-integration"
$PY = "$WT\backend\.venv\Scripts\python.exe"
& $PY --version   # Python 3.11.16；pytest 9.1.1

# 前端目录；node_modules 是 Junction -> D:\qio-dev\qio\frontend\node_modules（不要 npm ci / npm install，避免联网）
$FE = "$WT\frontend"
~~~

- venv 事实：集成 worktree 的 `backend\.venv` 是 Junction → `D:\qio-dev\qio\backend\.venv`（Python 3.11.16）。pm032-shell / pm032-interactive / pm032-settings / pm032-records 的 `.venv` 同样指向它；**pm032-planet 是独立真实 venv**（也是 3.11.16，能用）。
- 工具链：Node v24.11.1 / npm 11.6.2；vitest 4.1.10；TypeScript 5.9.3；vue-tsc 2.2.12；uv 0.12.13（可用，但本轮定向验收建议直接用 `.venv` 的 python，避免 `uv run --frozen` 触发 sync/联网）。
- Edge：`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`（存在）；Chrome：`C:\Program Files\Google\Chrome\Application\chrome.exe`（存在）；**Playwright 不存在**（旧截图脚本会失败，见 `3.4`）。

### 2.1 后端定向 pytest（点名 3 文件）

~~~powershell
cd $WT\backend

# 可选但推荐：不往仓库写 __pycache__ / .pytest_cache，缓存放到 TEMP
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPYCACHEPREFIX = "$env:TEMP\qio-pycache"
Remove-Item Env:QIO_DATA_DIR -ErrorAction SilentlyContinue

# 三个点名文件（2 个必须显式列名，否则默认不收集）
& .\.venv\Scripts\python.exe -m pytest `
  tests\pm_qio032_material_scope_test.py `
  tests\pm_qio032_submit_material_versions.py `
  tests\pm_qio032_submit_first_round_materials.py `
  -q -p no:cacheprovider
~~~

实测（HEAD e324e392）：`27 passed, 1 warning in 18.3s`（墙钟 18.3s，进程冷启动含 numpy/onnx 导入）。

环境变量说明（**不设也能跑**，`backend/tests/conftest.py` 已处理）：
- `QIO_DEV_INSECURE=1`：conftest 用 `os.environ.setdefault` 设置（测试免认证）。
- `QIO_DISABLE_DB_CHECK=1`：conftest 用 setdefault 设置（DB 身份自检关闭）。
- `QIO_DATA_DIR`：conftest 会在导入 `agent.config` 前把它 `pop` 掉（防止开发机数据目录泄漏进测试）。**不要**为了跑测试去设它；要设数据落点请在用例里显式传 `tmp_path`。
- 可选 `QIO_TEST_REAL_MIGRATIONS=1`：强制所有 db_conn 用例走真实迁移（更慢，仅对比/迁移问题排查用）。

其他常用变体（同样已核对可跑）：

~~~powershell
# 单文件（实测 2 passed / 9.0s）
& .\.venv\Scripts\python.exe -m pytest tests\pm_qio032_submit_material_versions.py -q -p no:cacheprovider

# 只收集不执行（实测 11.5s；输出各文件条数 21 / 4 / 2）
& .\.venv\Scripts\python.exe -m pytest `
  tests\pm_qio032_material_scope_test.py tests\pm_qio032_submit_material_versions.py tests\pm_qio032_submit_first_round_materials.py `
  --collect-only -q -p no:cacheprovider

# 单个 nodeid 例（把中文用例名换成实际函数名）
& .\.venv\Scripts\python.exe -m pytest "tests\pm_qio032_material_scope_test.py::test_xxx" -q
~~~

### 2.2 前端定向 vitest（6 个点名文件）

~~~powershell
cd $FE

# 1) 基线/布局 + 画布导入批次
npx --no-install vitest run src/conversation/layoutPrefs.test.ts src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts

# 2) shell/preview 三个 + settings 搜索（实测 4 files / 32 tests / 12.1s）
npx --no-install vitest run `
  src/components/conversation/__tests__/cv-c-preview-50-53-state.test.ts `
  src/components/conversation/__tests__/d-preview-52-tab-focus.test.ts `
  src/components/conversation/__tests__/cv-c-preview-feedback.test.ts `
  src/views/settings/__tests__/settings-search.test.ts
~~~

- 配置：`frontend/vitest.config.ts`（environment=jsdom，setupFiles=`./src/vitest.setup.ts`，include=`src/**/*.test.ts` + `src/**/*.spec.ts`）；不需要额外 config 参数。
- 超时：vitest 默认 testTimeout=5000ms / hookTimeout=10000ms。慢文件建议加 `--testTimeout=20000 --hookTimeout=20000`（`pm032-canvas-import-batch` 单条最长实测 3.5s，接近但不超默认值）。
- 实测：单文件 layoutPrefs 11 tests / 12.9s；canvas-import-batch 8 tests / 23.1s（vitest 自报 19.0s）；4 文件 32 tests / 12.1s。首次冷启动会慢 3–5s。
- 全量前端：`npm test`（= `vitest run`），**本轮不要跑**；规模与历史耗时见 `4.1`。

### 2.3 类型检查 `npx vue-tsc --noEmit`

~~~powershell
cd $FE
npx --no-install vue-tsc --noEmit
~~~

- 目录：`frontend/`（与 CI `.github/workflows/ci.yml` 的 frontend job 一致）。
- 实测（HEAD e324e392）：**exit 0，30.4s**；版本 TypeScript 5.9.3 / vue-tsc 2.2.12。冷热差异约 ±5s。
- `npx --no-install` 保证不联网（本地 `node_modules/.bin` 已有）。

### 2.4 空白/补丁卫生：`git diff --check`

~~~powershell
cd $WT

# 仅未暂存改动（工作区 vs index）
git diff --check

# 仅已暂存改动
git diff --cached --check

# 整条集成分支（基线..HEAD）——验收真正要看的那个
git diff --check 7445170d0f51756b2740eb7209d798540d8042a2..HEAD
~~~

实测（HEAD e324e392，2026-11-13 23:54）：
- `git diff --check` → exit 0；`git diff --cached --check` → exit 0。
- **range 形式 exit 2**，3 处空白错误（需 Lead 决定修掉或明确豁免）：
  - `_lead-logs/pm-qio032/reports/fix-pm-qio032-i-submit.md:107: new blank line at EOF.`
  - `backend/tests/pm_qio032_submit_first_round_materials.py:298: new blank line at EOF.`
  - `frontend/src/components/__tests__/TurnProcessFileReadFailure.test.ts:223: trailing whitespace.`（行内容 `+        return h("div", [ `）
- 退出码语义：0=没有空白错误；2=有空白错误（输出会列 文件:行: 原因）。
- `git diff --check` 无参数**不检查已提交内容**；本轮的验收口径应用 `7445170d..HEAD`。集成还在变，交卷前请重跑。

### 2.5 文档一致性：`python scripts/check_docs.py`

~~~powershell
cd $WT
& .\backend\.venv\Scripts\python.exe -B scripts\check_docs.py
& .\backend\.venv\Scripts\python.exe -B scripts\check_docs.py --selftest
~~~

- 实测：正式检查 **通过（121 个里程碑条目）/ 0.48s / exit 0**；自检 **6 个场景全过 / 0.21s / exit 0**。
- 纯 stdlib，任何 Python 3.11 都能跑；用 `.venv` 的 python 只为与其它命令一致。`-B` 避免写 `__pycache__`。
- 退出码：0=通过，1=存在文档漂移。

## 3. 起应用做真实界面验收（E2E）

### 3.1 一键入口（本轮既有做法，优先用）

~~~powershell
cd $WT
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1

# 单场景 / 多场景
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s4,s6

# 复用一个已迁移的数据域（冷库首次迁移实测 60s+）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1 -DataDir "$env:TEMP\qio-pm-qio032-data"
~~~

参数（来自 run.ps1 头）：`-Only`、`-ProviderPort`（默认 8807）、`-DataDir`、`-OutDir`、`-KeepRunning`。
端口：后端 **8734** / vite **5199** / fake provider **8807** / CDP **9333**。
产物目录：`_lead-logs\pm-qio032\e2e\`（`summary.json`、`console.log`、`console.raw.log`、`run.log`、`provider-log*.json`、`backend.log`、`vite.log`、`shots\*.png`）。
注意：**不要并发跑两个 run.ps1**（会互杀 8734/5199/8807，表现为卡片消失 / provider fetch failed；上一轮已发生过一次作废运行）。

### 3.2 手工最小步骤（等价于 run.ps1，便于单步调试）

~~~powershell
$WT = "D:\qio-dev\pm-qio032-integration"
$PY = "$WT\backend\.venv\Scripts\python.exe"
$OUT = "D:\qio-dev\_lead-logs\integration-032\e2e-manual"
New-Item -ItemType Directory -Force -Path $OUT, "$OUT\shots" | Out-Null

# 0) 清端口（8734/5199；provider 端口单独杀）
& $PY "$WT\scripts\e2e_down.py"
Get-NetTCPConnection -LocalPort 8807 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { taskkill /PID $_.OwningProcess /T /F }

# 1) 清代理（关键：本机 HTTP(S)_PROXY 会让 OpenAI 客户端把 127.0.0.1 也送代理，台账 0 请求）
foreach ($n in @("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy")) {
  Remove-Item -LiteralPath ("Env:" + $n) -ErrorAction SilentlyContinue }
$env:NO_PROXY = "127.0.0.1,localhost,::1"; $env:no_proxy = $env:NO_PROXY

# 2) fake provider（真 SSE + 请求台账；详细见 3.5）
$prov = Start-Process -FilePath $PY -ArgumentList @("$WT\scripts\verify-pm-qio032\fake_provider.py", "--port", "8807") `
  -WorkingDirectory "$WT\backend" -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput "$OUT\provider-server.log" -RedirectStandardError "$OUT\provider-server.log.err"
# 等就绪：Invoke-RestMethod http://127.0.0.1:8807/__health

# 3) 隔离数据域 + 起后端(8734)/前端(5199)；日志在 %TEMP%\qio-e2e-logs
$env:QIO_DATA_DIR = "$env:TEMP\qio-pm-qio032-manual-" + (Get-Date -Format "yyyyMMdd-HHmmss")
& $PY "$WT\scripts\e2e_up.py"
# 冷库迁移 60s+：e2e_up.py 自带 25s 预算会报 backend_ready=False，需自己等到 180s
# 就绪检查：Invoke-RestMethod http://127.0.0.1:8734/api/health ; Invoke-RestMethod http://127.0.0.1:5199/

# 4) 建假凭据 + 完成首次引导（干净数据域下模式切换会被「初始设置尚未完成」拦住）
$body = @{ provider="custom"; endpoint="http://127.0.0.1:8807/v1"; secret="sk-pm-qio032-fake-0001"; default_model="verify-model"; tags=@("main-loop") } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8734/api/credentials -Method Post -Body $body -ContentType application/json
Invoke-RestMethod -Uri http://127.0.0.1:8734/api/onboarding/complete -Method Post

# 5) 跑浏览器场景（系统 Edge + CDP；每个场景结束写 summary/shots）
$env:QIO_E2E_BASE = "http://127.0.0.1:5199"
$env:QIO_E2E_API = "http://127.0.0.1:8734"
$env:QIO_E2E_PROVIDER = "http://127.0.0.1:8807"
$env:QIO_E2E_OUT = $OUT
node "$WT\scripts\verify-pm-qio032\e2e.mjs" --only=s1
~~~

收尾：`& $PY "$WT\scripts\e2e_down.py"` + 杀 provider；二次确认 `Get-NetTCPConnection -LocalPort 8734,5199,8807 -State Listen` 为空。

### 3.3 如何“强制刷新”（避免看到旧前端）

- E2E harness 每次都**新起 Vite dev server + 新 Edge 临时 profile**（`mkdtemp`），页面从 `http://127.0.0.1:5199/` 全新加载；场景内需要重载时用 `page.reload()`（cdp.mjs:188）。
- 若复用已开的浏览器/IAB 手工验收：项目约定是 **带新参数强制刷新**（文档里用过 `?fresh=N`，等价写法 `?v=<时间戳>`）。例：
  `http://127.0.0.1:5199/?fresh=20261113235500` 或 `http://127.0.0.1:5199/?v=20261113235500`。
- 改了后端：uvicorn **无热加载**，必须重启（`scripts\e2e_down.py` 后 `e2e_up.py`）；改了前端源码：Vite HMR 通常够，但验收截图前建议 `page.reload()` 或新参数。

### 3.4 如何截图

- **首选（与本轮 E2E 同源）**：跑 `e2e.mjs` 时每个关键步骤自动 `page.screenshot`，落在 `_lead-logs\pm-qio032\e2e\shots\NN-<name>.png`（现有 57 张；`QIO_E2E_OUT` 可改输出根目录）。底层 API：`scripts/verify-pm-qio032/cdp.mjs` 的 `Page.screenshot(path)`。
- **临时补截（独立小工具，无需 npm 包）**：`scripts/visual_probe.mjs`（Chrome + Node 内置 WebSocket）。先保证 8734/5199 已起，然后：

~~~powershell
$steps = @'
[{"op":"navigate","url":"http://127.0.0.1:5199/?v=manual1"},
 {"op":"viewport","width":1440,"height":900},
 {"op":"wait","ms":1500},
 {"op":"screenshot","name":"manual-01"}]
'@
Set-Content -LiteralPath "D:\qio-dev\_lead-logs\integration-032\steps.json" -Value $steps -Encoding utf8
cd $WT
node scripts\visual_probe.mjs "@D:\qio-dev\_lead-logs\integration-032\steps.json"
# 截图默认落 %TEMP%\qio-visual\shots；可用 QIO_PROBE_OUT / QIO_PROBE_PORT / QIO_PROBE_PROFILE 覆盖
~~~

- **不可用/过时**：`scripts/verify-e2e-shots.mjs`、`frontend/scripts/e2e_check.mjs`、`frontend/scripts/screenshot_planet.mjs` 依赖 Playwright，本机该路径不存在（`Test-Path ...playwright = False`）；`scripts/verify-e2e.ps1` 的截图段因此也不可用。别把它们当现成截图入口。

### 3.5 fake provider 的用法与日志位置

- 本轮 E2E 用**记录请求正文**的版本：`scripts/verify-pm-qio032/fake_provider.py`（import 共享 `scripts/verify_stream_provider.py` 后子类化，只在台账上加 `_body`；正文截断 2MB；密钥只留指纹）。
- 启动：`& $PY "$WT\scripts\verify-pm-qio032\fake_provider.py" --port 8807`（工作目录 `backend` 或任意；run.ps1 用后者）。
- 控制面（同端口）：`GET /__health`、`GET /__log`（请求台账）、`POST /__script {"steps":[...]}`、`POST /__reset`、`GET /v1/models`、`POST /v1/chat/completions`。
- 日志/台账位置：
  - run.ps1 在收尾时把 `/__log` 归档为 `_lead-logs\pm-qio032\e2e\provider-log.json`（全场景最后一份，188KB）与 `provider-log-s1.json`（s1 单独复跑，82KB，含 `_body` 正文证据）。
  - provider 进程 stdout/stderr：`_lead-logs\pm-qio032\e2e\provider-server.log` / `provider-server.log.err`。
  - 通用旧版 provider：`scripts/e2e_fake_provider.py --port 8799`（也支持 `--selftest`；不带正文台账，勿用于交付正文断言）。
- 台账条目字段：`model / stream / tool_count / key 指纹 / mono / _body / body_truncated / message_roles` 等；断言“正文是否进了请求”看 `_body`。

### 3.6 上一轮 E2E 的 `summary-*.json` 判读方式

目录：`$WT\_lead-logs\pm-qio032\e2e\`。**权威文件是 `summary.json`**（最终合并台账），其余是历史/单场景复跑。

| 文件 | runId | pass/total | 性质 |
|---|---|---|---|
| `summary.json` | 20261113045732 | **38/40** | 最终合并台账（含 `mergedRuns` / `defects`）；权威 |
| `summary-full.json` | 20261112162607 | 25/29 | 11-12 历史全场景（旧，不用于本轮结论） |
| `summary-merged.json` | 20261112162607 | 31/34 | 11-12 历史合并（旧） |
| `summary-s1.json` | 20261112163820 | 12/13 | 11-12 旧 s1 中间件 |
| `summary-s4.json` | 20261112163230 | 8/8 | 11-12 旧 s4 中间件 |
| `baseline-s1\summary.json` | 20261113044822 | 14/14 | 13 日 s1 基线复跑 |
| `s1-rerun\summary.json` | 20261113050730 | 14/14 | 最终 s1（`mergedRuns` 引用） |
| `s4-rerun\summary.json` | 20261113050610 | 9/9 | 最终 s4（`mergedRuns` 引用） |

判读规则：
1. 顶层 `pass` / `total` 只是计数；**先看 `results[]` 里 `ok=false` 的条目**。
2. `detail.kind == "unverified"` = **未取证，不是产品缺陷**（本轮 ②-2/②-3 两项）。`summary.json` 的 `unverified` 数组另列 5 条范围外未验项。
3. `detail.kind` 其它值（如 `txt_file_body_delivered`、`image_delivered`）是正文/图片交付的直接证据类型。
4. `simulated` 列出 5 处“模拟故障”（CDP Fetch 延迟/中断、`/api/events/test` 注入、`Storage.removeItem` 抛错、CompositionEvent），引用结论时要带上。
5. `defects` 4 条是“已修复并复验”的产品项（#05/#80/#44/#45 + ①-6b 脚本口径修正）；`mergedRuns` 3 条说明 s1/s4 是用单独复跑替换同名断言后的合并。
6. e2e.mjs 退出码：任何一条 `ok=false`（含 UNVERIFIED）就 `process.exit(1)`（e2e.mjs:1076）；run.ps1 因此会打印 `[FAIL] E2E 有失败项`。**不要**把 run.ps1 exit 1 直接当产品失败。

~~~powershell
$e2e = "$WT\_lead-logs\pm-qio032\e2e"
$j = Get-Content "$e2e\summary.json" -Raw -Encoding utf8 | ConvertFrom-Json
"{0}/{1}" -f $j.pass, $j.total
$j.results | Where-Object { -not $_.ok } | ForEach-Object { $_.name + " :: kind=" + $_.detail.kind }
$j.unverified; $j.simulated; $j.mergedRuns; $j.defects
# 逐条看：$j.results | Format-Table name, ok, @{n='kind';e={$_.detail.kind}}
~~~

注意：`summary.json` 的 `shotsDir` 字段写的是 `D:\qio-dev\pm032-interactive\_lead-logs\...`（生成时在 shell 来源 worktree）；集成 worktree 里的同名目录是**镜像副本**，判读时以集成目录的文件为准。

### 3.7 证据入口现状（集成 worktree 内）

| 证据 | 路径 | 现状 |
|---|---|---|
| E2E 说明 | `_lead-logs\pm-qio032\e2e\README.md`（96 行） | 逐场景结论/未验项/模拟标注/harness 修正 |
| E2E 汇总 | `_lead-logs\pm-qio032\e2e\SUMMARY.md` | 38/40 表格 + 未验项 + 注意事项 |
| 最终台账 | `summary.json`（22,973 B） | 38/40；2 UNVERIFIED；4 defects；3 mergedRuns |
| 请求台账 | `provider-log.json`（188,100 B）、`provider-log-s1.json`（81,893 B） | 后者含 `_body`，用于 TXT/PNG 交付断言 |
| 控制台 | `console.log` / `console.raw.log` / `run.log` / `lead-run-s1.log` / `lead-final-run.log` | `run.log` 有完整启动/收尾事实（含 e2e_exit=1） |
| 服务日志 | `backend.log` / `vite.log` / `provider-server.log(.err)` | 进程侧证据 |
| 截图 | `shots\*.png` | 57 张（命名 `NN-<step>.png`） |
| 单场景 | `baseline-s1/`、`s1-rerun/`、`s4-rerun/`、`rerun-20261113-1307/` | 前三个有效；`rerun-20261113-1307/` 为并发事故**作废**产物 |
| 报告 | `_lead-logs\pm-qio032\reports\fix-pm-qio032-*.md`（7 份） | 各组修复报告与命令/证据引用 |

## 4. 前端全量基线规模与“受影响的入口测试文件”候选

### 4.1 全量基线规模（HEAD e324e392 实测）

- 受 git 跟踪的 vitest 文件：**612** 个；磁盘上 **615** 个（含 3 个未跟踪/生成差异）；静态统计 `it(`/`test(` 约 **5325** 条。
- 历史全量实测（同机、单进程 `vitest run`，来自 `_lead-logs\pm-qio032\final3-frontend.log` / `final-round3-frontend.log`）：
  - 583 files / 5065 tests / **519.19s**（2026-11-13 00:23，含 4 file/5 test 失败）
  - 589 files / 5104 tests / **280.96s**（2026-11-13 13:08，含 2 file/2 test 失败）
- 估计：当前 612 文件规模，本机全量 **约 5–9 分钟**（受并行 worker/CPU 负载影响）。CI 用 `npm test`（ubuntu，默认 worker 数）。
- 后端全量参照（同机历史，**不要在本轮跑**）：6791 parallel + 113 serial = **6904 tests / 715.4s**（parallel 612.6s + serial 102.9s）。

### 4.2 受影响的入口测试文件候选清单

**必跑（用户点名 9 个）**：见 `1.1` 表；命令见 `2.1` / `2.2`。

**建议同组一起跑（本轮分支 diff 里新增/修改的入口级用例）**：

- 后端（`backend/tests/`，前 6 个必须显式列名）：
  - `pm_qio032_material_scope_test.py`（21）
  - `pm_qio032_submit_material_versions.py`（2）
  - `pm_qio032_submit_first_round_materials.py`（4）
  - `pm_qio032_submit_authorization_facts.py`
  - `pm_qio032_submit_empty_semantics.py`
  - `pm_qio032_submit_intents_event.py`
  - `pm_qio032_submit_replay_lookup.py`
  - `test_pm_qio032_entity_zero_confidence.py`（3，默认收集）
  - `test_qio032_restore_idempotency.py`（3，默认收集）
  - `test_records_facts_file_read.py`
- 前端 shell / 预览 / 对话壳：
  - `frontend/src/__tests__/pmqio032-shell-acceptance.test.ts`
  - `frontend/src/components/conversation/__tests__/cv-c-preview-50-53-state.test.ts`、`d-preview-52-tab-focus.test.ts`、`d-preview-tabs-keyboard.test.ts`
  - `frontend/src/components/conversation/__tests__/cv-c-preview-feedback.test.ts`
  - `frontend/src/views/__tests__/ConversationViewShell.test.ts`、`ConversationViewLayers.test.ts`、`pm032-layout-shell.test.ts`、`settings-return-source.test.ts`
  - `frontend/src/conversation/layoutPrefs.test.ts`；`frontend/src/window-frame-contract.test.ts`；`frontend/src/design-guards.test.ts`
  - `frontend/src/components/__tests__/MessageItemPreviewFeedback.test.ts`、`MessageItemFileReadFailure.test.ts`、`TurnProcessFileReadFailure.test.ts`、`recovery-inbox-*.test.ts`、`recovery-copy-9-11-12.test.ts`、`composer-failed-send-topic-lock.test.ts`、`send-issues-cancel-swap-copy.test.ts`
  - `frontend/src/stores/__tests__/pm032-state-contract.test.ts`、`pm032-state-duplicate-submit.test.ts`、`toolFileReadFacts.test.ts`、`recovery-open-from-task.test.ts`
- 前端互动画布 / 提交 / 任务：
  - `frontend/src/components/interactive/__tests__/pm032-*.test.ts`（canvas-delete-collapse / group-ops / hint-copy / import-batch / link-labels / link-undo / toolbar-geometry / toolbar-render / visibility / layout-submit-panel / layout-submit-status / tasks-approval-recovery / tasks-intent / tasks-task-center，共 14 个）
  - `frontend/src/components/interactive/__tests__/execution-record-display.test.ts`、`ExecutionRecordPanel.test.ts`、`d-execution-record-modal.test.ts`、`d-execution-record-switch.test.ts`、`submitClusterPanels.test.ts`、`r9-file-card-material-panel.test.ts`、`BoardToolbar.test.ts`
  - `frontend/src/components/interactive/v2/__tests__/InteractiveConversation.test.ts`、`conversationLayout.test.ts`；`frontend/src/interactive/__tests__/p2int-canvas-wiring.test.ts`、`a-understanding-ownership.test.ts`
  - `frontend/src/views/__tests__/i2v2-a-intent-read-failure.test.ts`、`i2v2-a-task-entries.test.ts`
- 前端设置 / 星球 / 记录：
  - `frontend/src/views/settings/__tests__/settings-search.test.ts`、`settings-content.test.ts`、`settings-save-model.test.ts`、`settings-visual.test.ts`、`closeBehaviorSettings.test.ts`；`frontend/src/views/__tests__/SettingsView.test.ts`
  - `frontend/src/components/planet/__tests__/mp-a-*.test.ts`、`pm032-detail-hierarchy.test.ts`、`qio032-maintenance-rollback.test.ts`、`qio032-pending-sources.test.ts`、`EntityPanel.attributeZero.test.ts`、`KnowledgePanel.leaveGuard.test.ts`；`frontend/src/planet/__tests__/entityKind.test.ts`；`frontend/src/views/__tests__/PlanetView.test.ts`、`planet-chrome-clearance.test.ts`、`planet-dialog-a11y.test.ts`

### 4.3 用“分支 diff”生成完整受影响集合（推荐，别手抄）

~~~powershell
$WT = "D:\qio-dev\pm-qio032-integration"
$BASE = "7445170d0f51756b2740eb7209d798540d8042a2"

# 全部改动统计（e324e392 实测：426 项 = 256 新增 + 170 修改；其中测试文件 107 项 = 前端 97 + 后端 10）
git -C $WT diff --name-only $BASE..HEAD | Measure-Object

# 受影响前端测试文件批量跑（只跑这些文件，不是全量）
$feFiles = git -C $WT diff --name-only $BASE..HEAD |
  Where-Object { $_ -match '^frontend/.*\.(test|spec)\.ts$' } |
  ForEach-Object { $_ -replace '^frontend/', '' }
Push-Location "$WT\frontend"; npx --no-install vitest run @feFiles; Pop-Location

# 受影响后端测试文件批量跑（显式列名，绕开默认收集规则）
$beFiles = git -C $WT diff --name-only $BASE..HEAD |
  Where-Object { $_ -match '^backend/tests/.+\.py$' } |
  ForEach-Object { $_ -replace '^backend/', '' }
Push-Location "$WT\backend";
  & .\.venv\Scripts\python.exe -m pytest @beFiles -q -p no:cacheprovider;
Pop-Location
~~~

注意：diff 列表随 Lead 合并实时变化；每次跑前重新生成。上面两条是“本轮改动过的测试”，比全量小一个量级，仍属于定向范围。

## 5. 缺失项 / 风险 / 需要 Lead 决策

### 5.1 集成 worktree 会持续变化
- 核实期间观察到 HEAD 连续推进：`bb0fd72b → b2796785 → 543ee77a → e324e392`；23:53 有 1 个未提交修改（`cv-c-preview-50-53-state.test.ts`，+10/-3）；23:57 再观察时已有 5 个未提交修改（`cv-c-preview-50-53-state.test.ts`、`conversation/layoutPrefs.ts`、`stores/conversationShell.ts`、`views/settings/__tests__/settings-content.test.ts`、`settings-search.test.ts`）——跑任何验收前以 Lead 最新工作区为准。
- 出稿后 Lead 继续返修：2026-11-14 00:2x 集成 HEAD 已到 `8c2009ff`，工作树 46 项改动 / 14 个未跟踪（含 `frontend/.tmp-tsc*.log`、`frontend/.tmp-vitest*.log` 等临时日志，以及新增未跟踪用例 `backend/tests/pm_qio032_submit_version_basis.py`）。跑正式验收前请先提交/清理，使 `git status` 干净，再记录 HEAD。
- 本文件所有“现状”都以 `e324e392` 为准。重跑前先执行：
~~~powershell
$WT = "D:\qio-dev\pm-qio032-integration"
git -C $WT rev-parse HEAD; git -C $WT status --porcelain=v1
git -C $WT diff --check 7445170d0f51756b2740eb7209d798540d8042a2..HEAD
~~~

### 5.2 已知缺口/不一致（按优先级）
1. **pytest 默认收集漏 6 个 `pm_qio032_submit_*.py`**：全量/`-k qio032` 都不会跑它们。要么验收命令显式列名（本清单做法），要么 Lead 决定改文件名/加 `python_files`（改配置会影响全量口径，需评审）。
2. **`git diff --check 7445170d..HEAD` 当前红**（3 处空白，见 `2.4`）。若验收把该命令当硬门禁，需要先修掉（两处 EOF 空行、一处行尾空格）；否则需明确豁免理由。
3. **E2E 有 2 项 UNVERIFIED（②-2/②-3）**，且 ② 的读文件一路、⑤ 旧卡迁移/隐藏成员/撤销连线、原生 Tauri 拖放未验；不得在结论里写成“全通过”。
4. **`run.ps1` exit 1 是预期**（UNVERIFIED 计入 ok=false）；判读入口是 `summary.json` 的 `detail.kind`，不是 run.ps1 退出码。
5. **`summary-full.json` / `summary-merged.json` 是 11-12 的旧台账**（25/29、31/34），别当本轮证据；权威是 `summary.json`（38/40）+ `s1-rerun`/`s4-rerun`。
6. **`summary.json` 里 `shotsDir` 指向 pm032-interactive 路径**：集成目录是镜像；引用截图请写集成路径。
7. **Playwright 不存在**：`scripts/verify-e2e-shots.mjs`、`frontend/scripts/e2e_check.mjs`、`scripts/verify-e2e.ps1` 的截图段不可用；截图走 `e2e.mjs` 或 `visual_probe.mjs`。
8. **不要 `npm ci` / `npm install` / `uv sync`**：node_modules 与 .venv 都已就绪（junction），联网会失败且违反本轮约束。
9. 冷数据域首次迁移 **60s+**；`e2e_up.py` 自带 25s 预算会误报 `backend_ready=False`，run.ps1 已用 180s 二次等待兜住。
10. `pm032-planet` 的 `backend\.venv` 是独立真实目录（不是 junction），验证一致为 Python 3.11.16；如果 Lead 用该 worktree 跑定向测试，路径照常可用。

### 5.3 其他既有验收装置（需要时再用，端口互不冲突）
- 设置页全套取证：`scripts/settings-verify/run-all.ps1`（一键：起环境→视觉→交互→录屏→汇总→停环境）；单独起环境 `node scripts/settings-verify/serve.mjs up --label d`，端口前端 **5106** / 后端 **8756** / CDP **9806**，数据目录 `%TEMP%\qio-settings-verify\data\d`，截图落 `docs/verification-shots-settings/`。
- 星球页 032 验收：`node scripts/planet-032-verify/verify.mjs`（默认前端 5112 / 后端 8762；依赖 `scripts/mp-verify/.state/<label>.json` 的已起环境）。
- 这两套是“分组验收”装置，和 `verify-pm-qio032`（互动板面）端口不同，可分别跑；不要与 run.ps1 并发。

## 附录 A：环境事实核验（2026-11-13 23:42–23:54）

| 项 | 事实 |
|---|---|
| Python | `D:\qio-dev\pm-qio032-integration\backend\.venv\Scripts\python.exe` → Python 3.11.16；pytest 9.1.1 |
| venv 链接 | 集成/shell/interactive/settings/records 的 `backend\.venv` 均 Junction → `D:\qio-dev\qio\backend\.venv` |
| Node | v24.11.1（`C:\Program Files\nodejs\node.exe`，e2e_up.py 里硬编码该路径，存在）；npm 11.6.2 |
| 前端依赖 | `frontend\node_modules` Junction → `D:\qio-dev\qio\frontend\node_modules`；vitest 4.1.10 / TS 5.9.3 / vue-tsc 2.2.12 |
| uv | 0.12.13（可用；本轮建议直接用 `.venv` python） |
| 浏览器 | Edge x86 存在；Chrome 存在；Playwright 路径不存在 |
| 后端配置 | `backend/pyproject.toml`：testpaths=tests，addopts=`-q`，asyncio_mode=auto，pythonpath=src；无自定义 python_files |
| 文档检查 | `scripts/check_docs.py` 存在，支持 `--selftest`；CI docs job 也跑它 + `--selftest` + `release_gate.py --selftest` |
| E2E 脚本 | `scripts/verify-pm-qio032/{run.ps1, e2e.mjs, cdp.mjs, fake_provider.py, README.md}` 齐备 |

## 附录 B：命令速查（最小集）

~~~powershell
$WT = "D:\qio-dev\pm-qio032-integration"

# 后端点名 3 文件
cd $WT\backend; & .\.venv\Scripts\python.exe -m pytest tests\pm_qio032_material_scope_test.py tests\pm_qio032_submit_material_versions.py tests\pm_qio032_submit_first_round_materials.py -q -p no:cacheprovider

# 前端点名 6 文件（两条）
cd $WT\frontend; npx --no-install vitest run src/conversation/layoutPrefs.test.ts src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts
npx --no-install vitest run src/components/conversation/__tests__/cv-c-preview-50-53-state.test.ts src/components/conversation/__tests__/d-preview-52-tab-focus.test.ts src/components/conversation/__tests__/cv-c-preview-feedback.test.ts src/views/settings/__tests__/settings-search.test.ts

# 类型检查
cd $WT\frontend; npx --no-install vue-tsc --noEmit

# 补丁卫生 + 文档
cd $WT; git diff --check 7445170d0f51756b2740eb7209d798540d8042a2..HEAD
cd $WT; & .\backend\.venv\Scripts\python.exe -B scripts\check_docs.py

# E2E 一键（会起 8734/5199/8807，跑完自动收尾）
cd $WT; powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-pm-qio032\run.ps1 -Only s1
~~~

---

生成者：accept-inventory（验收入口清单员）。只写了 `D:\qio-dev\_lead-logs\integration-032\` 下的文件；未改任何仓库文件、未执行 git 写操作、未跑全量测试/CI、未联网调用真实模型。
## 6. 第 1 步 UI 驱动（真实界面往返与视觉证据，已写未跑）

Lead 要求先写脚本、等三个返修落地后再跑；驱动放在**仓库外**，不改产品/共享验收资产：

- `D:\qio-dev\_lead-logs\integration-032\ui-verify\ui-verify.mjs`（约 1100 行，59.7KB）：复用 `scripts/verify-pm-qio032/cdp.mjs`，自己 launch 系统 Edge（CDP 9333）；真实鼠标点击/拖动/输入/截图；所有等待都有超时与标签。
- `D:\qio-dev\_lead-logs\integration-032\ui-verify\run-ui.ps1`：校验 8734/5199/8807 就绪 → 跑 node → 默认 `e2e_down.py` + 结束 provider；`-Setup` 才调 `run.ps1 -KeepRunning`；`-Only s1,s3` 可单场景；`-SkipCleanup` 保留服务。

用法（服务已由 run.ps1 -KeepRunning 起好）：

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\qio-dev\_lead-logs\integration-032\ui-verify\run-ui.ps1
# 或自动起服务（冷库迁移 60s+，且 run.ps1 会先跑一遍自带 E2E）：
powershell -NoProfile -ExecutionPolicy Bypass -File D:\qio-dev\_lead-logs\integration-032\ui-verify\run-ui.ps1 -Setup
~~~

产物：`ui-verify\{summary.json,shots/*.png,console.log,run.log}`；summary 逐条 PASS/FAIL/UNVERIFIED，含 DOM 事实、服务端 `conversation_layout` 真值、模拟标注、console errors、集成 HEAD 与工作树状态。

覆盖：s1 #47 宽窗（拖动左右分隔线 / 收起左栏 / 两预览标签各自搜索+滚动 / 设置页还原 / 重开预览 / 整页刷新）；s2 #47 窄窗（800×600 自动收起/全窗口不写入偏好，拉宽回分栏）；s3 #46 跨页上传（`POST /api/attachments/upload` 延迟 4.5s，切设置页往返，只关联一次/无重复卡/无重新选文件；单文件 AddMenu 无批次 UI，另补单文件拖放批次「已接收」证据）；s4 搜索「单段长度」自动展开 memory.advanced；s5 搜索「还原默认布局」；s7 `[模拟]` PUT /api/settings/ui 返回 500 → 失败提示 + 重试；s6 2 主题 × 2 尺寸矩阵（对话底部/预览标题/互动底部/设置搜索截图 + 控件可达/横向溢出/重复提示探测 + 真实更新检查失败态尝试）。

自检：`node --check` 通过、PowerShell Parser 0 错误、无服务空跑能写出 summary 并 exit 1（只写 ui-verify 目录）；关键选择器已在当前工作树（HEAD 8c2009ff）逐个 grep 核对。**尚未对真实应用执行**。
