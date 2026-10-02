# P4 计划 —— sidecar 所有权（A）与安装版自带 Python 运行时（B）

基线：`main@71b992c`（含 0.1.12 版本号承接）。工作分支：`wt/p4-sidecar-runtime`。
取证与先验事实见 `docs/e2e-install-2026-10-02.md`、`docs/status.md`、`docs/release-qualification.md`。

## 0. 先验事实（已核对，不要再重新论证）

| 事实 | 证据位置 |
| --- | --- |
| 卸载钩子按**映像名**收 sidecar：`CheckIfAppIsRunning "qio-backend.exe"` | `frontend/src-tauri/nsis/installer-hooks.nsh` |
| 该宏来自模板 `utils.nsh`，内部走 `nsis_tauri_utils::FindProcessCurrentUser/KillProcessCurrentUser`，**只认 exe 名** | `frontend/src-tauri/target/release/nsis/x64/installer.nsi:756-760` |
| 模板对主程序 `qio.exe` 用的就是同名宏（本阶段**不改**这条） | 同上 :636 / :760 |
| 运行中的 `qio-backend.exe` 删不掉/覆盖不了，只有 rename 能成功 | `scripts/verify_backend_process_model.py --case 6` |
| onefile 是 launcher + child 两层；job 生效时关 job 句柄即收整棵树 | `frontend/src-tauri/src/main.rs`（`backend_job`、run 回调） |
| 冻结后端不能用 `sys.executable` 当解释器 | `backend/src/agent/tools/tool_envs.py::_resolve_base_python` |
| `_prepare` 用 `<base> -m venv <dir>` 建环境，pip 来自 `ensurepip`（离线） | 同上 :1189-1200、:1305-1347 |
| 本机有 tauri 自带的 makensis：`%LOCALAPPDATA%\tauri\NSIS\makensis.exe` | 实测 |
| 本机已有可用 CPython 3.11（uv 管理）：`%APPDATA%\uv\python\cpython-3.11.15-windows-x86_64-none` | 实测；裁剪到 37.1MB 后 `-m venv` 正常、venv 内 pip 24.0 可用 |

## 1. A —— 卸载只收「本安装实例自己的」sidecar

### 1.1 所有权模型（唯一事实源 = lease 文件）

安装实例在**启动**时由外壳写一份 lease（同一个安装目录 = 同一个实例）：

```
<INSTALL_DIR>\sidecar.lease.json
{
  "schema": 1,
  "install_dir": "<安装目录绝对路径>",
  "shell":  { "pid": 1234, "created_filetime": "133400000000000000" },
  "backend":{ "pid": 5678, "created_filetime": "133400000012345678",
              "exe": "<INSTALL_DIR>\\qio-backend.exe" },
  "started_at": "2026-10-03T02:00:00Z"
}
```

- `created_filetime` = Windows 进程创建时间（100ns 精度，UTC FILETIME，十进制字符串）。**PID + 创建时间 + 映像路径**三者同时匹配才认这个进程 = 抗 PID 复用。
- 写盘方式：临时文件 + 原子替换；内容不含任何密钥/令牌。
- 外壳退出（任何路径）时删除 lease。
- 开发态（debug 构建）不写 lease：开发实例不能成为安装实例的清理目标。

### 1.2 卸载时的判定（`qio-uninstall-helper.exe`）

帮助程序（与主程序同源的**独立 exe**，随安装包放进安装目录）被卸载器调用：
```
$INSTDIR\qio-uninstall-helper.exe --close-installation --install-dir "$INSTDIR" --timeout-ms 8000
```
1. 读 lease；文件不存在 / 不是合法 JSON / schema 不符 / `install_dir` 与传入路径不一致 → **不动任何进程**，退出码 `3`。
2. 取 `backend.pid` 的创建时间与映像路径；对不上（进程已退出、PID 被复用、路径不同）→ **不动**，退出码 `3`。
3. 对得上 → 先向 `shell.pid` 发 `WM_CLOSE`（外壳自己的退出路径会关 job → 收整棵树）→ 等 `--timeout-ms`。
4. 超时 → `taskkill /F /T /PID <backend.pid>`，再 `taskkill /F /PID <shell.pid>`（**只按 pid**）。
5. 成功 → 删除 lease，退出码 `0`；确认不了就 `3`（**宁可不杀**）。
6. `--json` 输出一行诊断（pid/判定/human-readable reason），不含敏感信息。

### 1.3 NSIS 钩子

`NSIS_HOOK_PREUNINSTALL`：先调帮助程序，再保留模板原有的主程序检查。**删除**按映像名杀 sidecar 的写法。
性能与健壮性：帮助程序缺失/启动失败 → 跳过（不报错、不退化为按名字杀）。

### 1.4 必须证明的
- 两份真实安装并存时，卸载 A **不动** B 的任何进程，B 端口仍响应（先失败后通过）。
- 安装目录里的替身 `qio-backend.exe`（无 lease / 无主程序）时，卸载器**不**杀它，而是如实报告"无法确认归属"。
- PID 复用反证：伪造 lease 指向一个已存在的无关进程 → 帮助程序必须拒绝。

## 2. B —— 安装包自带 Python 运行时

### 2.1 产物
`frontend/src-tauri/resources/python-runtime/`（**不入库**，构建期生成；`resources/` 已在 .gitignore）：
`python.exe` + `Lib/`（含 `venv`、`ensurepip/_bundled`）+ `DLLs/` + `python3*.dll` + `vcruntime*`。
经裁剪（去 `tcl`/`include`/`libs`/`Lib/test`/`idlelib`/`tkinter`/`__pycache__`/`site-packages/pip`）后约 37 MB；
**不裁剪** `Lib/venv` 与 `Lib/ensurepip/_bundled`（`-m venv` 的离线 pip 就来自这里）。

版本：与**冻结后端的 Python major.minor 一致**（`_prepare` 会拒绝不匹配的环境）。
构建期用 `uv python install <ver>` 取官方 python-build-standalone 作为来源（可重复、不依赖本机 Python）。

### 2.2 解析顺序（`tool_envs.py::_resolve_base_python`）
`QIO_PYTHON`（显式，不匹配就明确失败）
→ **`QIO_BUNDLED_PYTHON_DIR`**（外壳在冻结态传入；目录内有可探到的匹配解释器就用它）
→ 冻结态：`py -0p` → `PATH`
→ 非冻结态：`sys.executable`
说明文案必须写清"这次用的是哪个"（自带运行时 / 机器上的 / 显式的）。

### 2.3 外壳与打包
- 外壳启动时解析 `resource_dir()/python-runtime`，存在就把解释器路径通过 `QIO_BUNDLED_PYTHON_DIR` 交给后端。
- `tauri.conf.json` 的 `bundle.resources` 增加 `resources/python-runtime` → `python-runtime`。
- `scripts/build_runtime.ps1`：取来源 → 裁剪 → **自检**（`python -c "import sys"` + `-m venv` + venv 内 `pip --version`）→ 失败即失败。
- `scripts/build_installer.ps1` / `build_sidecar.ps1` 依赖 `scripts/python-version.txt`（默认 `3.11`）统一版本来源。
- 构建闸门：安装包里缺运行时 → 构建/闸门报红（与"缺模型"同一口径）。

### 2.4 必须证明的
- 新 VM 语义下（无系统 Python、PATH 里也没有）：安装版能建出依赖环境并跑通一个声明第三方依赖的工具。
- 有 `QIO_PYTHON` 时仍以显式指定为准；显式指定版本不匹配时明确失败（行为不变）。
- 不带运行时也能给出原有那条"可行动的明确失败"（不假装可用）。

## 3. 明确不做（与任务书一致）
cross-topic retrieval / memory ranking / Topic 主策略 / Fragment / Low Integrity 默认策略 /
网络隔离 / AppContainer / Job Object 主设计 / updater signing / release signing credential /
TraceStore / migration framework / Planet / UI 大改 / worker protocol。
模板对 `qio.exe` 的按名字检查也**不动**（超出本轮；如与本轮改动冲突再单独记账）。

## 4. 验收口径
- 后端：`uv run --frozen python -m pytest`（受影响用例 + 全量）。
- Rust：`cargo test`（主程序与帮助程序的两个二进制）。
- 安装：`scripts/install_e2e.py`（本机 + CI 的 install-e2e 任务）。
- 多安装并存：新增 stage（两份安装 + 双后端 + 先失败后通过的断言）。
- 自带运行时：`scripts/install_dep_e2e.py` 的"无系统 Python"分支。
- 文档：`docs/status.md`、`docs/release-qualification.md`、`docs/e2e-install-2026-10-02.md`、`README.md`、`docs/releases/v0.1.12.md`、`docs/SETUP.md` 必须与实现一致。
