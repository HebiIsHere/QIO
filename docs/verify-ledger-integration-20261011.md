# 定向验证台账：可靠性 × 真实模型整合轮（2026-10-11）

> 本文件由子智能体 D 维护（所有权见 `docs/integration-contract-20261011.md` §4）。
> 它记录**这一轮**每一步验证的命令、范围、SHA、结果、CI run ID/attempt 与额度使用情况。
> 整合总体记录见 `docs/integration-round-20261011.md`（§5 台账骨架与本文件对应）；
> 独立审查见 `docs/integration-review-20261011.md`。

## 0. 额度预算（全程共用，只减不增）

| 额度 | 允许次数 | 已用 | 说明 |
| --- | --- | --- | --- |
| 唯一一轮本地全量 | 1 | 0（待 Lead 在冻结候选 SHA 上执行） | 见 §3 |
| 唯一一次全量 CI | 1 | 0（待 Lead 触发） | 见 §3 |
| 定向验证（本地 / 定向派发） | 不限，但每次只跑需要的项 | 见 §4 | 失败后只重跑失败项，不重跑已通过集合 |

**禁止**：无筛选 pytest、全前端 vitest、全仓评测（`uv run python -m agent.eval.run`）、完整 CI、
`npm ci` / `uv sync` 全量重装。**允许**：定向 pytest（`-k` / 具体文件 / node id）、
`npx vue-tsc --noEmit`、定向 vitest（指定文件）、`python scripts/check_docs.py`。

> 判据：定向命令必须带**显式选择器**（文件、node id、`-k`、`-t`）。没有选择器的命令一律按
> 「全量」计额度，不允许在开发期执行。

## 1. 基线与候选 SHA

| 角色 | 分支 | SHA |
| --- | --- | --- |
| 整合基线（本轮起点） | `integrate/reliability-interactive-real-model-20261011` | `c6a03b17423ccda606f72e1e81e3a610fd8e1d43` |
| 冻结契约提交 | 同上 | `0ed432fee35fa5d9b9cea1c37370ce644d77a12a` |
| 整合轮记录提交 | 同上 | `1f36e10` |
| **冻结候选（本地全量 + 全量 CI 的对象）** | 同上 | 待 Lead 冻结后填写（§3） |
| 本入口所在分支 | `wt/irm-int-d-ci` | 见 §7 最终 SHA |

## 2. 触发与去重安排（为什么不会重复跑）

`ci.yml` 的 `push: branches: ["**"]` + `pull_request` 双触发是其它分支的正常门禁，**保持原样**。
本轮的重复来自「同一个提交既有 push 又有 PR」：两次都会拉起完整矩阵。处理方式是**在调度期按提交去重**：

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{ github.sha }}
  cancel-in-progress: true
```

* 以 `github.sha`（提交）为键 → 同一个提交的 push 运行与 PR 运行进同一组，重复的那次在**入队时**
  就被替换掉，不会出现两套完整矩阵同时在跑；
* 用 SHA 而不是 ref 作键 → 连续两次 push 是两个不同提交，互不干扰，**其它分支的正常门禁不受影响**；
* 没有用 `[skip ci]`、没有关掉任何检查、没有把 job 整条 `if` 掉（`verify_workflow_contract.py`
  对这三件事都有断言）。

**证据**：每个 run 的 `ci-dedup-preflight` job 打印 `event / ref / commit / run attempt / run id / 去重键`。
台账记录时对照「同一提交在 Actions 列表里剩几个 run」即可核对去重是否生效。

> 诚实说明：`cancel-in-progress: true` 兜住的是「同一提交第二次入队」这一种竞态；
> 去重本身由 concurrency group（调度期）完成，不是「跑完再取消」。
> 该 preflight job **不做检查、不设门槛**，它只提供身份证据 —— 不要把它当成「去重已生效」的证明。

## 3. 一次性额度（由 Lead 执行，本文件记录结果）

### 3.1 唯一一轮本地全量

| 项 | 内容 |
| --- | --- |
| 对象 SHA | 待填（§1 冻结候选） |
| 后端 | `cd backend; uv run --frozen pytest -q`（口径与 CI 一致，含 `-m "not requires_docker"` 的差异需注明） |
| 前端 | `cd frontend; npx vue-tsc --noEmit` + `npm test` + `npm run build` |
| 文档 | `python scripts/check_docs.py` |
| Rust | `cd frontend/src-tauri; cargo check` + `cargo test`（需 sidecar / 模型目录占位） |
| 评测 | 按 `AGENTS.md` 要求的评测集合 |
| 发布闸门 | `python scripts/release_gate.py --selftest` |
| 结果 | 待填 |

### 3.2 唯一一次全量 CI

| 项 | 内容 |
| --- | --- |
| workflow | `.github/workflows/ci.yml`（全量矩阵） |
| 对象 SHA | 待填（与 §3.1 同一候选） |
| CI run ID / attempt | 待填（`gh run list --commit <sha> --workflow ci.yml`） |
| 各 job 结论 | 待填（docs / backend py3.11 / backend py3.12 / backend-windows / frontend / rust / rust-windows / frozen-worker / install-e2e） |
| 额度 | 用掉 1/1 —— 之后任何失败**不得**再触发全量，只能走 §4 的定向入口 |

## 4. 定向验证记录

每次定向验证一行。**失败修复后只重跑失败项**（node id / 文件 / `-k` / `-t`），不重跑已通过集合。

| # | 阶段 | 范围（命令） | SHA | 结果 | CI run ID / attempt | 额度 |
| --- | --- | --- | --- | --- | --- | --- |
| — | 开发期定向（D） | `python -m pytest -q scripts/targeted-verify/test_verify_sha.py` | 见 §7 | 17 passed | 本机 | 定向 |
| — | 开发期定向（D） | `python scripts/targeted-verify/verify_workflow_contract.py` | 见 §7 | 60 条断言通过 | 本机 | 定向 |
| — | 开发期定向（D） | `python scripts/targeted-verify/verify_workflow_contract.py --selftest` | 见 §7 | 7 条元断言通过（合成文本能红） | 本机 | 定向 |
| — | 开发期定向（D） | `cd backend; uv run --frozen pytest -q --durations=10 tests/test_sandbox_worker.py` | 见 §7 | 25 passed | 本机 | 定向（单文件） |
| — | 开发期定向（D） | `cd frontend; npx vitest run src/__tests__/sr-e-sendConfirm.spec.ts` | 见 §7 | 8 passed（1 文件） | 本机 | 定向（单文件） |
| — | 开发期定向（D） | `python scripts/check_docs.py` | 见 §7 | 通过（47 个里程碑条目） | 本机 | 定向 |
| 待填 | 失败项修复后定向重跑 | 使用 §5 的派发命令 | 待填 | 待填 | 待填 | 定向 |

### 4.1 失败项定向重跑记录（模板）

| 失败项 | 原结论 | 失败用例 / node id | 定向重跑命令 | 新 SHA | 新结果 | CI run ID / attempt |
| --- | --- | --- | --- | --- | --- | --- |
| 待填 | 待填 | 待填 | 见 §5 | 待填 | 待填 | 待填 |

> 注意：**不要**用旧 CI 的 "Re-run failed jobs"。那个按钮重跑的是 run 绑定的**旧提交**，
> 修完的提交再点重跑，跑的仍是老代码。改用 §5 的定向入口：它显式 checkout 输入的 ref/sha，
> 并在 checkout 之后断言「实际工作区提交 == 输入 SHA」。

## 5. 定向验证入口用法（`targeted-verify.yml`）

只由 `workflow_dispatch` 触发（推送 / PR 都不会自己跑），所以不会和 `ci.yml` 的门禁重复。

```bash
# 单个 CI 项（例：只跑 py3.11 后端 + 两个失败文件）
gh workflow run targeted-verify.yml \
  -f ref=<分支或 tag> \
  -f sha=<完整 40 位提交 SHA> \
  -f targets=backend-py311 \
  -f pytest_node_ids="tests/test_interactive_submission.py tests/test_interactive_intents.py"

# 只重跑失败用例（node id 精确到函数）
gh workflow run targeted-verify.yml -f ref=<ref> -f sha=<sha> \
  -f targets=backend-py312 \
  -f pytest_node_ids="tests/test_interactive_submission.py::test_client_request_id_replay"

# 只重跑失败用例（-k 表达式）
gh workflow run targeted-verify.yml -f ref=<ref> -f sha=<sha> \
  -f targets=windows -f pytest_k="client_request_id or acceptance_lookup"

# 前端：类型检查 + 指定 spec + 构建（原 CI 里 Tests 失败时 Build 不会执行，这里补上）
gh workflow run targeted-verify.yml -f ref=<ref> -f sha=<sha> \
  -f targets=frontend -f vitest_files="src/__tests__/sr-e-sendConfirm.spec.ts" \
  -f vitest_name="受理"

# 原 CI 因失败未执行的构建 / 检查，按单项补齐
gh workflow run targeted-verify.yml -f ref=<ref> -f sha=<sha> -f targets=rust         # cargo check
gh workflow run targeted-verify.yml -f ref=<ref> -f sha=<sha> -f targets=rust-windows # cargo test
gh workflow run targeted-verify.yml -f ref=<ref> -f sha=<sha> -f targets=frozen-worker
gh workflow run targeted-verify.yml -f ref=<ref> -f sha=<sha> -f targets=docs
```

| targets 取值 | 跑什么 |
| --- | --- |
| `docs` | `check_docs.py` + `--selftest`、`release_gate.py --selftest`、workflow 契约断言（含 `--selftest`）、`verify_sha.py` 单测 |
| `backend-py311` / `backend-py312` | 该 Python 版本：`uv sync --frozen --extra dev` → 源码 worker 冒烟 → worker 协议用例 → **定向** pytest |
| `windows` | windows-latest 同一套（`-m "not requires_docker"`） |
| `frontend` | `npm ci` → `npx vue-tsc --noEmit` → **定向** vitest → `npm run build` |
| `rust` | `cargo check`（含 Tauri 系统依赖与 sidecar/模型占位） |
| `rust-windows` | `cargo test` |
| `frozen-worker` | 真 sidecar 打包 + `tool_worker.py` 在产物里的断言 + 冻结协议冒烟 |
| `all` | 上面全部（等价于 `ci.yml` 全量矩阵，谨慎使用） |

### 5.1 这道入口的三条硬约束

1. **必须显式指定 SHA，且必须对得上**：`verify_sha.py` 打印 requested / actual(git HEAD) /
   GITHUB_SHA(event) / ref 解析结果；不一致就以非 0 退出并写明差异。缩写 SHA 默认拒绝
   （缩写证明不了跑的是哪个提交）。每个 job 自己 checkout、自己核验（`verify-checkout` 复合 action）。
2. **不许无筛选整套重跑**：`backend` / `windows` / `frontend` 三个 job 在既没有 node id / `-k`
   也没有 spec 文件 / `-t` 时**直接失败**，提示补筛选参数。
3. **skipped 不得当 passed**：`summary` job 用 `toJSON(needs)` 逐项打印 `ran / SKIPPED / failure`，
   并显式声明「SKIPPED 不是通过」；没有任何检查项真的跑过时以非 0 退出。

### 5.2 SHA 证据长什么样（台账抄这里）

`verify-sha` job 打印并上传 `verify-sha-preflight.json`（artifact `verify-sha-preflight`）：

```json
{
  "label": "preflight",
  "requestedSha": "<输入 SHA>",
  "actualSha": "<git rev-parse HEAD>",
  "actualSource": "git rev-parse HEAD",
  "expectedRef": "<输入 ref>",
  "refSha": "<ref 解析到的提交>",
  "gitHead": "<同上>",
  "githubSha": "<事件提交，PR 上是 merge commit>",
  "passed": true
}
```

> 为什么 `actualSha` 不取 `GITHUB_SHA`：它是**事件提交**，PR 上是 merge commit，
> 与 `actions/checkout` 真正落到工作区的提交可以不同。拿它当「实际 SHA」会假绿。

## 6. 本入口自己的验证证据（本轮实测，命令与输出摘要）

| 命令 | 真实输出摘要 |
| --- | --- |
| `python -m pytest -q scripts/targeted-verify/test_verify_sha.py` | `17 passed` |
| `python scripts/targeted-verify/verify_workflow_contract.py` | `-> 60 assertions passed`（解析 ci.yml / targeted-verify.yml / verify-checkout action 后逐条断言） |
| `python scripts/targeted-verify/verify_workflow_contract.py --selftest` | `-> selftest: 7 checks, 0 failed`（把 concurrency 键、push 范围、sha 输入、SHA 核验、筛选参数逐条破坏后断言变红） |
| `cd backend; uv run --frozen pytest -q --durations=10 tests/test_sandbox_worker.py` | `25 passed`，`slowest 10 durations` 正常输出（证明 `--durations=15` 口径可用） |
| `cd frontend; npx vitest run src/__tests__/sr-e-sendConfirm.spec.ts` | `Test Files 1 passed (1) / Tests 8 passed (8)` |
| `python scripts/check_docs.py` | `文档一致性检查通过（47 个里程碑条目）` |
| YAML 结构检查（每个 job 的 step 必须恰好有 `uses` 或 `run`） | `PROBLEMS: none` |

**未覆盖**：以上全部是本机命令，**不能**替代 CI。`targeted-verify.yml` 的 GitHub 端行为
（`workflow_dispatch` 输入映射、`fromJSON` 矩阵、composite action 解析、artifact 上传）
在派发之前**没有真实运行过**；第一次派发必须按 §5.1 三条约束核对输出，不得假定它已经正确。

## 7. 本分支改动与最终 SHA

| 项 | 内容 |
| --- | --- |
| 分支 | `wt/irm-int-d-ci`（起点 `c6a03b1`） |
| 最终 SHA | 待填（提交后回填） |
| 改动文件 | `.github/workflows/ci.yml`、`.github/workflows/targeted-verify.yml`、`.github/actions/verify-checkout/action.yml`、`scripts/targeted-verify/verify_sha.py`、`scripts/targeted-verify/test_verify_sha.py`、`scripts/targeted-verify/verify_workflow_contract.py`、`docs/verify-ledger-integration-20261011.md` |

## 8. 未覆盖项与不确定项（诚实清单）

1. **GitHub 端未实跑**：本入口与去重配置都没有在真实 Actions 上跑过（全量 CI 只有一次，由 Lead 触发）。
   所有 yaml 级断言都只是**静态解析**的结论。第一次派发后必须按 §5.1 核对。
2. **去重的可观测性依赖外部核对**：concurrency 在调度期生效，仓库内没有任何脚本能证明「同一提交只跑了一次」；
   证据是 Actions run 列表 + `ci-dedup-preflight` 打印的身份行。
3. **`install-e2e` 未收进定向入口**：它要构建 NSIS 安装包（45 分钟级、依赖合成模型与 runtime 构建），
   塞进「定向」入口既慢又容易与额度红线冲突。它仍然只在 `ci.yml` 全量里跑；
   如果全量 CI 在这一项失败，只能整体重跑该项对应的全量任务 —— 这是**已知的能力缺口**，不含糊其辞。
4. **`frontend` 单项里的 `npm ci`**：类型检查与 vitest 都要求依赖树干净，所以这一步保留了 `npm ci`。
   本机一律用 junction 接好的 `node_modules`，不重装。
5. **`vitest -t` 的值经环境变量传递**：含空格/正则字符时语义由 vitest 解释；本机 PowerShell 下
   `-t a:b` 里的冒号会被解析成参数名，需要 `--%` 或改用 `--testNamePattern`（本文件 §6 的实测命令不含 `-t`）。
6. **`ref` 输入默认 `main`**：默认值只是省事；真正把关的是 SHA 核验，ref 与 SHA 不一致时直接失败。
7. **原 CI 的 `install-e2e` / `frozen-worker` 里那几条「只有 runner 上才成立」的断言**
   （注册表、onefile 解包、无系统 Python）在本机无法复现，本台账不声称它们被验证过。
