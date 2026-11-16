# Task 1（shell-lock）：桌面全机单实例 + 更新重启交接 + install_e2e_multi

- 工作树：`D:\qio-dev\q46-r9`（分支 fix/qio032-46-r9，基线 645e5a1f）
- 红基线：`D:\qio-dev\_q46-r9-baseline`（产品代码零改动；仅临时复制测试文件/Cargo.toml，跑完已 git checkout 还原并删除复制件）
- 未提交 git（按约定由 Lead 统一提交）

## 1. 文件清单

新增：
- `frontend/src-tauri/src/single_instance.rs` —— Global 命名互斥（NULL DACL）+ 同会话激活事件 + 更新交接事件 + QIO_INSTANCE_PROBE 探针 + 纯逻辑单测
- `frontend/src-tauri/src/tests_single_instance.rs` —— 跨进程契约测试（CARGO_BIN_EXE_qio 真二进制）

修改：
- `frontend/src-tauri/Cargo.toml` —— windows-sys 加 `Win32_Security` feature（只加 feature，**Cargo.lock 未变**，已用 git status 核对）；新增 `[[test]] single_instance_contract`
- `frontend/src-tauri/src/main.rs` —— main() 最前面取锁并分流（任何共享写操作之前）；主实例 Guard 存静态；窗口 init 后启动激活监听线程；`qio_arm_relaunch` 命令；`show_main_window` 改 pub(crate) 复用；note_and_prune_instances 改诊断措辞；停/恢复后端不释放进程级锁的注释
- `frontend/src/services/updater.ts` —— `relaunch()` 先 `invoke("qio_arm_relaunch")` 再 `plugin-process.relaunch()`；登记失败只 warn、不阻断重启
- `frontend/src/services/__tests__/updaterFlow.test.ts` —— 新增「先 arm 再 relaunch」顺序用例（真实 service 实现 + mock 插件边界）与「登记失败不阻断」用例
- `scripts/install_e2e_multi.py` —— 默认用例改为全机单实例语义（见 §4）

## 2. 实现要点（含 review 修正）

- 锁名只与产品/协议版本有关：`Global\QIO.SingleInstance.v1`；激活/交接事件为 `Local\QIO.SingleInstance.Activate.v1` / `...Handoff.v1`。与安装路径、Windows 账号、登录会话、提权、QIO 版本无关。
- 创建 mutex/event 都用显式 **NULL DACL** 的 SECURITY_ATTRIBUTES（Everyone 完全访问）→ 跨账号/跨提权仍能打开参与判定；`CreateMutexW` 返回 `ERROR_ACCESS_DENIED` 也按「已有实例」处理；`CreateMutexW` 建不出来时 **fail-closed** 按重复启动处理，绝不静默放行第二套应用。
- 取锁用 `WaitForSingleObject(mutex, 0)`（WAIT_OBJECT_0/WAIT_ABANDONED 均视为取得；ABANDONED = 上一个实例崩溃，可直接重开）；Guard 存进 `OnceLock` 持有到进程结束，正常退出/崩溃都由内核释放。
- **Review 修正（Lead 意见 1）**：删除了「创建激活事件后立刻 ResetEvent」的竞态写法 —— 新建事件对象初始就是未置位（内核对象在最后一个句柄关闭时销毁，不会跨进程残留），Reset 反而会吞掉同会话第二进程在 CreateEventW 与 Reset 之间到达的 SetEvent。
  handoff 事件只在 `CreateEventW` 的 GetLastError == ERROR_ALREADY_EXISTS（极短窗口内打开到旧进程正在退出的旧对象）时才 ResetEvent；时序依据：handoff 只由**当前主实例**的 qio_arm_relaunch 置位，而此刻本进程已持有全机互斥（没有别的主实例），不存在另一个置位者并发。
- 同会话重复启动：置位激活事件（事件未创建时重试 ~2s）→ exit 0；主实例监听线程 `WaitForSingleObject → ResetEvent → run_on_main_thread(show_main_window)`，复用托盘恢复路径（show + unminimize + focus + 窗口层状态同步），覆盖正常/最小化/托盘隐藏。
- 异会话/账号：可关闭 MessageBox「QIO 已在此电脑运行」+ 下一步说明后 exit 0；`QIO_STARTUP_ERROR_DIALOG=0` 时跳过模态框（仍 exit 0，供无人值守脚本）。
- 更新重启交接：前端 relaunch 前置位 handoff；新进程 acquire 发现锁被占且 handoff 置位 → 轮询等待锁释放（最多 30s）→ 重新 create_lock 接任；超时按重复启动处理。
- 更新期间停/恢复后端**不释放锁**（进程级；已补注释）。`note_and_prune_instances` 保留为诊断，措辞改为「锁已保证单机唯一活动实例」。

## 3. 红/绿证据（命令与结果）

证据目录：`_lead-logs/qio032-46/r9/{red,green}/`

### 3.1 前端 relaunch 交接（红在基线，绿在产品）

红基线（把改过的测试文件复制到 _q46-r9-baseline，跑完 git checkout 还原）：
```
cd D:\qio-dev\_q46-r9-baseline\frontend
npx vitest run src/services/__tests__/updaterFlow.test.ts
→ Tests  1 failed | 11 passed (12)
  × relaunch() 先 invoke qio_arm_relaunch，再调用 plugin-process 的 relaunch
  AssertionError: expected [ 'process.relaunch' ] to deeply equal [ 'qio_arm_relaunch', 'process.relaunch' ]
```
日志：`red/red-updater-relaunch-handoff-baseline.log`

产品绿：
```
cd D:\qio-dev\q46-r9\frontend
npx vitest run src/services/__tests__/updaterFlow.test.ts
→ Tests  12 passed (12)
```
日志：`green/green-updater-relaunch-handoff.log`

定向回归（updater service + store + 定时器）：
```
npx vitest run src/services/__tests__/updaterFlow.test.ts src/stores/__tests__/updater.test.ts src/stores/__tests__/updaterTimers.test.ts
→ Test Files  3 passed (3) / Tests  38 passed (38)
```
日志：`green/green-updater-store-regression.log`

### 3.2 Rust 跨进程契约测试 `--test single_instance_contract`

红基线（复制 test 文件 + Cargo.toml 到 _q46-r9-baseline；跑完 Cargo.toml git checkout、test 文件删除）：
```
cd D:\qio-dev\_q46-r9-baseline\frontend\src-tauri
$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'
cargo test --test single_instance_contract
→ test result: FAILED. 0 passed; 3 failed  (62.14s)
  三个用例都卡在「60s 内没有输出 JSON 行」：基线 qio.exe 没有 QIO_INSTANCE_PROBE 探针、也没有单实例闸门 → 第二个进程会作为第二套应用继续启动（正是要修的缺陷）
```
日志：`red/red-single-instance-contract-baseline.log`

产品绿：
```
cd D:\qio-dev\q46-r9\frontend\src-tauri
$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'
cargo test --test single_instance_contract
→ running 3 tests
  test first_process_becomes_primary ... ok
  test second_same_session_process_is_duplicate_and_activates_first ... ok
  test relaunch_handoff_waits_for_old_instance_then_becomes_primary ... ok
  test result: ok. 3 passed; 0 failed  (6.00s)
```
日志：`green/green-single-instance-contract.log`

对应用例（Lead 意见 2 已覆盖）：① 首个进程 primary；② 第二同会话进程 duplicate(sameSession=true) 且首个进程观察到 activated；③ 旧进程 arm handoff 后延迟 2.5s 退出 → 新进程等待（断言 waited ≥ 1s）后成为 primary。

### 3.3 cargo check 与既有单测

```
cd D:\qio-dev\q46-r9\frontend\src-tauri
$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'
cargo check                    → Finished dev profile in 4.29s（0 error / 0 warning）
cargo test --bin qio           → test result: ok. 54 passed; 0 failed（含 single_instance 3 条纯逻辑单测 + 既有 backend_job/window_frame/model-sync/updater_net 单测）
```
日志：`green/green-cargo-check.log`、`green/green-qio-bin-unit-tests.log`

未跑全 workspace / 未跑 `--test uninstall_helper_cli`（未受影响）；未触发 CI。

## 4. `scripts/install_e2e_multi.py` 默认用例改动

默认口径（`--expect-legacy-name-kill` 之外）从「两套安装同时活动」改为全机单实例语义：

1. 装 A/B 两份（原有 C-010..C-016 保留；C-012 逐文件 sha256 证明装 B 不改 A）。
2. **先启动 B**（活动实例）：lease 四件套 + backend.pid 归属校验（C-020-B/C-021-B）、冻结 B 进程身份 + lease 指纹（C-030）、等窗口可见（C-032）。
3. **再启动 A（第二份安装）**：断言被单实例锁拦截 ——
   - C-060：A 退出码 0（60s 内；未拦截则 C-060b FAIL 并按 pid 收掉，避免后续验错对象）；
   - C-061：A 没写 sidecar lease / 会话令牌 / 数据目录 logs（没有启动第二套应用）；
   - C-062：A 安装目录按路径枚举无任何进程（没有第二套 shell/backend）；
   - C-063：同一时刻只有一套窗口（持有主窗口的 pid 只能是 B；A 无窗口进程）；
   - C-064：B 的进程身份（pid+创建时间+路径）、lease 指纹、/api/health 均未受影响。
4. C-031/C-048 的「ping 改名 qio.exe」诱饵保留：证明卸载 A 时模板不会按映像名连坐杀掉 B（(b) 未修的显式证据）。
5. **卸载非活动 A**：C-041（A 壳进程确认不在）、C-042（A 无 lease）、C-043（A 目录清空）；C-044（B 的进程身份逐条一致）、C-045（B 的 lease 字节指纹未变且仍合法）、C-047（B 的 sidecar pid 未重启）。
6. **卸载活动 B**：C-051（B 的壳与 sidecar 按既有进程归属关闭并退出）、C-052（lease 消失）、C-053（目录清空）。
7. 保留原有辅助/收尾：只按 PID/路径收进程（绝不 `/IM`）、`wait_uninstall_settled`、`health_check` 按 pid 反查端口并核对回答者 exe、沙箱兜底口径（`run_sandbox_fallback`，合成 lease 只证判定逻辑）不变。
8. `--rename-b-shell` 帮助文本已更正：单实例锁按产品名而非映像名，改名**不会**绕过锁，只用于隔离模板按映像名杀 qio.exe 的遗留行为。

验证（本机可做的部分）：
```
python -m py_compile scripts\install_e2e_multi.py   → exit 0
python scripts\install_e2e_multi.py --help          → 正常输出新口径/参数说明
python scripts\install_e2e_multi.py --installer <不存在> --work-dir <临时> → 走到 C-001 记 FAIL 并 exit 3（plumbing 正常，无异常栈）
```
**真机安装版跑（需要真实安装包 + 真外壳 + WebView2）由 Lead 执行 → UNVERIFIED（见 §6）。**

## 5. 更新交接路径结论：还会撞锁吗？

- 正常更新重启（前端 `restart()` → service `relaunch()`）：先置位 handoff → 再请求 plugin-process relaunch。新进程若在旧进程退完前启动，会看到锁被占 + handoff 置位 → 等待旧进程退出释放锁（轮询 CreateMutex，最多 30s）→ 接任主实例。**不会**被当成重复启动。
- 交接竞态窗口：handoff 置位与 plugin relaunch 之间若旧进程崩溃/退出，新进程直接创建锁成为 primary（handoff 无对象时 `handoff_armed()=false`），同样不会卡。
- 残留风险（低）：用户点「立即重启」但 plugin relaunch 失败/被取消时，handoff 事件保持置位且旧实例继续运行 → **之后**一次普通重复启动会先等最多 30s 才走同会话激活（表现为慢，不会启动第二套应用）。当前没有 disarm 命令；如 Lead 认为需要，可加 `qio_disarm_relaunch`（未加是为避免扩大范围）。
- 另一条与锁无关的既有边界：更新安装器替换文件期间若旧进程仍活着，安装器可能报占用 —— 这由既有 `qio_prepare_for_update` 结束后端/进程树负责，本轮未改其职责。

## 6. UNVERIFIED（本机无法制造 / 需要 Lead 真机验收）

1. **跨 Windows 账号/登录会话互斥**：本机只有单会话，`ERROR_ACCESS_DENIED` 路径与「异会话弹提示后 exit 0」路径未实测（代码路径存在，NULL DACL 设计上覆盖）。
2. **真机安装版默认用例**（`install_e2e_multi.py`：B 活动 → A 被拦 → 卸载非活动 A 不动 B → 卸载活动 B）：需要安装包 + 真外壳 + WebView2。
3. **托盘隐藏 / 最小化状态下第二启动唤出**：契约测试只验证激活事件送达（activated=true），真实窗口 show+unminimize+focus 的可见效果需真机（Lead 的 win_instance_accept.py S1-S9 覆盖）。
4. **更新成功后重启经真 IPC 的端到端交接**：本轮只验证「service 先 arm 再 relaunch」顺序 + 契约测试的 handoff 等待语义；真安装器/plugin-process 的端到端需 Lead 用 CDP 真 IPC（S9）。
5. **快速同时启动（双击两次）**：契约测试是顺序启动（第二进程启动时第一已持有锁）；真正的毫秒级并发（两个进程同时 CreateMutex）由内核原子性保证，但本机未做压测。
6. 单实例锁在 release/安装版的真实表现（debug 契约测试之外）：需 Lead 构建 release + 安装版跑 S1-S9。

## 7. 需要 Lead 参与/确认

- 真机跑 `install_e2e_multi.py` 默认口径与 `win_instance_accept.py` S1-S9；release 构建产物替换到 win-install-a/b。
- 确认是否有其它正在运行的 QIO（脚本 C-002 会在安装前发现按名字运行的 qio.exe 并 FAIL；Lead 当前有一个 win-install-a\qio.exe 在跑，跑脚本前需先关掉）。
- `docs/status.md` 回填（Lead 负责）：单实例为当前行为；删除「允许多个实例运行」表述。
- 是否要加 `qio_disarm_relaunch` 处理「更新重启失败后 handoff 残留导致下一次启动慢 30s」的低风险场景（默认未加）。

## 8. 关键命令复跑清单

```powershell
# Rust
cd D:\qio-dev\q46-r9\frontend\src-tauri
$env:CARGO_TARGET_DIR='D:\qio-dev\qio\frontend\src-tauri\target'
cargo check
cargo test --test single_instance_contract
cargo test --bin qio
# 前端
cd D:\qio-dev\q46-r9\frontend
npx vitest run src/services/__tests__/updaterFlow.test.ts src/stores/__tests__/updater.test.ts src/stores/__tests__/updaterTimers.test.ts
# 脚本
cd D:\qio-dev\q46-r9
python -m py_compile scripts\install_e2e_multi.py
python scripts\install_e2e_multi.py --help
```
