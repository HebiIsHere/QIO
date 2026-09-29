# 工具 worker 模式（复用随包资源 + 独立工具子进程）实施记录

**Goal:** 让冻结产物在没有系统 Python、没有 Docker 的 Windows 上也能执行生成工具；执行发生在独立子进程里，后端主进程不执行生成代码。

**Tech Stack:** Python 3.12 / pytest / PyInstaller（`scripts/build_sidecar.ps1`）。

## 1. 架构

```
后端服务（qio-backend.exe，默认模式）
   │  结构化 JSON 请求 → 子进程 stdin
   ▼
工具 worker 子进程
   ├─ 正式：qio-backend.exe --tool-worker
   └─ 开发：python src/agent/tool_worker.py
   │  一行 JSON 结果 → 子进程 stdout
   ▼
后端服务（继续运行；工具失败/超时/取消都不影响它）
```

三个不可退让的约束：

1. **分流发生在加载之前**：`agent/main.py` 顶层只 import 标准库；`main()` 先看 argv，命中 `--tool-worker` 就直接走 worker 并退出。worker 模式不启动服务、不建数据库、不跑迁移、不取凭据、不做维护任务。
2. **开发与正式共用协议**：同一份 `agent/tool_worker.py`（只用标准库），同一套 stdin/stdout 协议；差别只是由谁把它跑起来。
3. **不宣称隔离**：独立子进程隔离的是崩溃、超时与资源占用；能访问什么由 `tools/policy.py` 的能力分级与用户审批决定。worker 本身不做权限判断，也不注入凭据或数据库访问。

## 2. 协议

请求（stdin，一个 JSON 对象）：

```json
{"code": "def run(**kwargs): ...", "arguments": {"a": 1}}
```

结果（stdout，恰好一行 JSON）：

```json
{"ok": true, "value": {}, "stdout": "", "stderr": "", "error": null, "error_type": null}
```

- 工具自己的 `print` 与异常栈被捕获进 `stdout` / `stderr` 字段 —— 结果与日志分离，结果通道始终只有一行 JSON。
- 请求不是合法 JSON → 退出码 2，原因写 stderr。
- 返回值不是 JSON 对象、或无法序列化 → `ok: false` + 明确原因。

## 3. 取消、超时与进程清理

- 超时：`asyncio.wait_for` 到点后调用 `_kill_process_tree`。
- 取消：`CancelledError` 同样先 `_kill_process_tree`，再把取消语义向上传递（不伪装成失败）。
- 清理范围：Windows `taskkill /PID <pid> /T /F`（从这个 PID 往下遍历子树）；POSIX 让子进程自成进程组后 `killpg`。
- **明确禁止**：按可执行文件名称批量结束进程（会误伤同名进程）。`tests/test_sandbox_worker.py` 断言命令行是 `/PID` 形态。

## 4. 依赖

- 默认复用随包已经验证过的依赖：正式运行与测试用的是同一份解释器与同一份依赖。
- 额外依赖的项目级隔离（QIO 管理的专用 Python 环境）是后续阶段工作，**本阶段未实现**；用到第三方库的工具会以 `missing_dependency` 明确失败，不自动安装，也不改动后端或用户全局环境。
- `QIO_TOOL_PYTHON` 只作为开发/诊断的逃生口（显式指定解释器），正式版不需要设置。

## 5. 改动文件

- `backend/src/agent/main.py`（入口分流、重依赖改为函数内导入）
- `backend/src/agent/tool_worker.py`（新增：worker 与协议）
- `backend/src/agent/tools/executor_env.py`（解析执行命令）
- `backend/src/agent/tools/sandbox.py`（走 worker 协议、进程树清理）
- `scripts/build_sidecar.ps1`（`--hidden-import agent.tool_worker`）
- 测试：`test_tool_worker.py`、`test_executor_env.py`、`test_entrypoint_split.py`、`test_sandbox_worker.py`

## 6. 验证结果

已验证：

- 后端全量 `uv run --frozen pytest` 通过。
- 真实冻结产物：按打包脚本参数用 PyInstaller 打出 `qio-backend.exe`；
  `--tool-worker` 在剔除 PATH 里所有真实 Python 后返回正确结果、退出码 0、且不创建数据目录与 `app.db`；
  同一 exe 正常启动仍创建 `app.db`、HTTP 应答正常、可按 PID 停止。

未验证（NOT RUN，不算通过发布验收）：

- `tauri build` 产出的完整安装包的人工安装验收。
- Docker 执行器与新 worker 协议的合并（Docker 分支仍走容器内 `python -c` 旧协议，未实测）。
- 额外依赖的项目级隔离环境。
- 「测试使用隔离数据（临时库/临时目录/模拟服务）」的全面覆盖。
