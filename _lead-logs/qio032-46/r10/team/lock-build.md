# Task 1（lock-build）：正式构建固定产品锁名 / 测试后缀与探针隔离

- 工作树：`D:\qio-dev\q46-r9`（分支 fix/qio032-46-r9）
- 红基线：`D:\qio-dev\_q46-r10-baseline`（detached，产品代码零改动）
- 起点/远端 tip 核对：`git rev-parse HEAD` = `70e475c2d4393f6eb6a1a28491078b7a6f32cf4b`；`git ls-remote origin refs/heads/fix/qio032-46-r9` 同为 `70e475c2`（未改分支基线）
- 未 git add/commit/push；未改 docs/status.md；未跑全量、未触发 CI；模型/后端零联网（仅 Rust 单测 + 本机探针 qio.exe）

## 1. 文件清单与落点

修改：
- `frontend/src-tauri/src/single_instance.rs`（核心，+124 行）
  - `PROBE_ENV` 常量（L55）：`QIO_INSTANCE_PROBE` 收敛为一处常量（`run_probe` 也改用它）。
  - `test_build_env_allowed()`（L90）：**唯一 gating 判据** `cfg!(debug_assertions)`。
  - `resolve_suffix(allowed: bool)`（L99）：显式 allowed 参数的可单测后缀解析；
    `allowed=false` 直接返回 `String::new()`（**完全不读 env**）；`allowed=true` 沿用既有 sanitize 规则。
  - `suffix()`（L112）：唯一一处 `resolve_suffix(test_build_env_allowed())` 接入，
    `lock_name()/activate_event_name()/handoff_event_name()` 全部经它 → 正式构建恒为
    `Global\QIO.SingleInstance.v1` / `Local\...Activate.v1` / `Local\...Handoff.v1`。
  - `probe_requested()`（L136）→ `probe_requested_when(test_build_env_allowed())`（L143）：
    正式构建 `allowed=false` 时恒 false（即使 env 有值也不读）；测试构建语义不变。
  - `run_probe` 的 mode 读取改用 `PROBE_ENV`（行为不变，仅常量收敛）。
  - 新增 4 条纯逻辑单测（L743 起）：`formal_build_objects_are_fixed_product_names`、
    `formal_build_ignores_suffix_env_even_when_set`、`probe_only_requested_in_test_build`、
    `test_build_gate_is_debug_assertions`；env 用例用进程级 `ENV_LOCK` 串行（cargo test 默认多线程）。
- `frontend/src-tauri/src/main.rs`（仅注释，L2235-2237）：说明探针只在测试构建生效、release 恒 false。
- `frontend/src-tauri/src/tests_single_instance.rs`（仅文档注释）：契约测试是 debug 构建；
  release/安装版按设计恒忽略两个测试 env。

新增：
- `_lead-logs/qio032-46/r10/verify/release_env_alarmy.py`：给 Lead 的 release exe env 过敏验收脚本（见 §4）。

**未改**：`Cargo.toml`（无需新 feature/依赖）、`Cargo.lock`、任何前端代码、任何行为语义
（`acquire`/`signal_activation`/`try_wait_for_relaunch_handoff`/同会话激活/异会话提示/更新交接逻辑全部未动）。

## 2. gating 依据与语义

- 依据：`cfg!(debug_assertions)`。`cargo test` 通过 `CARGO_BIN_EXE_qio` 拉起的是 **debug** 构建
  （debug_assertions=true）；`tauri build` / NSIS 安装版是 **release** 构建（false）。
- 用 `cfg!` 而非 `#[cfg]`：两条分支都参与类型检查，避免 release 下把符号 cfg 掉、
  留下「env 被读却没人用」的隐患；`cargo check --release` 已验证该分支零 warning。
- 正式构建（`!debug_assertions`）：
  - 三个对象名固定产品名，与安装路径/账号/会话/提权/版本无关；
  - `QIO_INSTANCE_PROBE_SUFFIX` / `QIO_INSTANCE_PROBE` 一律不生效 ——
    不会因继承测试 env 而「提前走探针打印 JSON 后退出」或「按另一后缀自成隔离实例」。
- 测试构建（debug）：后缀隔离与探针语义**完全保留**，跨进程契约测试继续可用。

## 3. TDD 红 → 绿（命令与结果）

证据目录：`_lead-logs/qio032-46/r10/{red,green}/`（编译使用 r9 约定的共享热 target：
`$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'`；不设也能跑，只是首次全量编译更慢）

### 3.1 红（先写测试，实现前）

(a) 产品 worktree（只加测试、未加实现）：
```powershell
cd D:\qio-dev\q46-r9\frontend\src-tauri
$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'
cargo test --bin qio
```
→ `error: could not compile 'qio' (bin "qio" test) due to 16 previous errors`；16×E0425
（`cannot find function resolve_suffix / test_build_env_allowed / probe_requested_when`，
`cannot find value PROBE_ENV`）。日志：`red/red-r10-task1-worktree-tests-first.log`

(b) **红基线** `D:\qio-dev\_q46-r10-baseline`（按约定采集；产品实现保持基线）：
把 r10 新增的单测模块追加到基线 `single_instance.rs`（基线无这些函数/常量），跑同一命令，
跑完 `git -C D:\qio-dev\_q46-r10-baseline checkout -- frontend/src-tauri/src/single_instance.rs` 还原。
```
cargo_exit=101
error[E0425]: cannot find function 'resolve_suffix' in this scope        （×7）
error[E0425]: cannot find function 'test_build_env_allowed' in this scope（×4）
error[E0425]: cannot find function 'probe_requested_when' in this scope  （×3）
error[E0425]: cannot find value 'PROBE_ENV' in this scope                （×2）
error: could not compile 'qio' (bin "qio" test) due to 16 previous errors
```
日志：`red/red-r10-task1-baseline.log`
说明：基线缺被 gitignore 的 build.rs 产物（`binaries/qio-backend-x86_64-pc-windows-msvc.exe`、
`resources/models`、`resources/python-runtime`、`resources/qio-uninstall-helper.exe`），
本次用 junction/copy 临时补齐（构建产物，非产品代码），跑完已删除。
**基线跟踪文件零改动**：还原后 `git -C _q46-r10-baseline status --short` 为空（其它 M 文件是他人工件，非本任务所改）。

### 3.2 绿（实现后，定向）

```powershell
cd D:\qio-dev\q46-r9\frontend\src-tauri
$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'
cargo test --bin qio
#   running 58 tests ... test result: ok. 58 passed; 0 failed（含 4 条新单测，其余 54 条为既有）
cargo test --test single_instance_contract
#   running 3 tests ... test result: ok. 3 passed; 0 failed; finished in 4.93s
cargo check
#   Finished 'dev' profile ... in 3.89s（exit 0，无 warning）
cargo check --release
#   Finished 'release' profile [optimized] ... in 57.89s（exit 0，无 warning；验证 !debug_assertions 分支）
```
日志：
- `green/green-r10-task1-qio-bin-unit-tests.log`
- `green/green-r10-task1-single-instance-contract.log`
- `green/green-r10-task1-cargo-check.log`
- `green/green-r10-task1-cargo-check-release.log`

单测证明的语义（debug 测试构建内）：
1. `resolve_suffix(false)` 恒 `""`；三个对象名 = 三个产品常量（即使 env 已设置）。
2. env 设为 `inherited-a` 时：`resolve_suffix(false)==""`（正式语义），
   而 `resolve_suffix(true)==".inherited-a"`、`suffix()==".inherited-a"`（测试构建隔离仍可用）。
3. env `QIO_INSTANCE_PROBE=hold` 时：`probe_requested_when(false)==false`；
   `probe_requested()==probe_requested_when(cfg!(debug_assertions))`（唯一 cfg! 接入等价性）。
4. `test_build_env_allowed()==true`：固定 gating 依据为 debug_assertions（若有人 `cargo test --release`，
   前提不成立会尽早失败，而不是悄悄让所有用例共用产品锁名）。
契约测试 3 条（primary / duplicate+激活 / 更新交接等待后接任）继续全绿 → 测试构建语义零回退。

## 4. release exe 验收脚本（Lead 用）

文件：`_lead-logs/qio032-46/r10/verify/release_env_alarmy.py`（复用 `r9/verify/win_instance_accept.py` 的
进程/窗口/lease/后端辅助函数；断言只来自 OS 事实 + 退出码，不以日志文案判定）

用法（--exe 指向完整安装/暂存目录里的 release qio.exe，同目录需有 qio-backend.exe）：
```powershell
python D:\qio-dev\q46-r9\_lead-logs\qio032-46\r10\verify\release_env_alarmy.py `
  --exe "D:\qio-dev\_win-r10\install-u\qio.exe" `
  --work "D:\qio-dev\_win-r10\env-alarmy"
```
断言：
- **R1** 首个 release qio.exe 带 `QIO_INSTANCE_PROBE=hold` + `QIO_INSTANCE_PROBE_SUFFIX=inherited-a`：
  必须按正式实例启动 —— 进程存活、写出本进程 lease（`shell.pid` 匹配）、主窗口可见、后端已监听；
  若正式构建仍认探针，进程会打印 `{"role":"primary"}` 后 exit 0 → 脚本 FAIL 并附输出尾部（可直接定位）。
- **R1b** 同会话再启动一个带 `QIO_INSTANCE_PROBE_SUFFIX=inherited-b`（+ `QIO_INSTANCE_PROBE=hold`）的进程：
  必须 exit 0、仅唤醒已有窗口 —— 窗口数 1、权威 lease 数 1 且属首进程、后端 pid 集合与启动前一致。
- 输出 `<work>/results.json`，有 FAIL 时退出码 1；结束按安装目录收进程并清理 lease。
- 另：Lead 既有的 `r10/verify/update_accept.py` §R1/R1b 是同一场景的安装器版本（可二选一/互相印证）。

**为什么必须 Lead 用 release exe 验收**：`cfg!` 是编译期常量，单测只能在 debug 测试构建里
通过显式 allowed 参数证明正式构建语义；真实二进制在 `debug_assertions=false` 下
「不认 env、照常写 lease/建窗口/拉后端、第二次启动被同一产品锁拦截」只能由 release 构建实跑证明。
（`cargo check --release` 已证明该分支可干净编译，但不执行运行时行为。）

## 5. 风险 / UNVERIFIED

1. **release exe 实跑未做**（明确由 Lead 验收）：脚本已就绪，但本任务未构建/运行 release 安装版。
2. 跨 Windows 账号/登录会话的异会话提示路径沿用 r9 口径，本机单会话无法实测（UNVERIFIED，与 r9 一致）。
3. gating 依据绑定 `debug_assertions`：若未来有人在 `[profile.release]` 打开
   `debug-assertions = true`，正式构建会退回旧行为；`test_build_gate_is_debug_assertions`
   会在 debug 测试构建里继续通过（无法发现 release profile 配置），需要靠发布构建验收兜底。
4. 单测里修改进程级 env 用 `ENV_LOCK` 互斥串行；不影响契约测试（独立子进程）。
5. 本任务未新增契约用例（计划明确「已有」）：同会话重复启动 + 后缀隔离仍由既有 3 条契约测试覆盖。
6. 未跑全量 workspace / `uninstall_helper_cli`（未受影响）。

## 6. 复跑命令清单

```powershell
$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'   # 可选：r9 约定的共享热缓存
cd D:\qio-dev\q46-r9\frontend\src-tauri
cargo test --bin qio
cargo test --test single_instance_contract
cargo check
cargo check --release
python -m py_compile D:\qio-dev\q46-r9\_lead-logs\qio032-46\r10\verify\release_env_alarmy.py
```
