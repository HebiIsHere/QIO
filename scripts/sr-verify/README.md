# sr-verify：QIO 契约 6 独立受控反例与验收（Agent F）

对应冻结契约 `_sr-contracts-20261009.md` 契约 6（2026-10-09）。本目录只包含
**验证与证据**，不改任何生产实现。

## 结构

* `probes.py` —— 反例探针本体：每个 `probe_*` 在临时环境里真实复现一类问题，
  返回可 JSON 化的证据（布尔/线程号/相对时间戳/无敏感语义的截断文本，
  **不含任何密钥原文**），并把证据写进 `output/<label>.json`。
* `run_all.py` —— 一键验收运行器：逐组执行反例，输出中文 PASS/FAIL/SKIP 结论。
  退出码：全部 PASS → 0；存在 FAIL → 1（前后对照的机器可读信号）。
* `output/` —— 证据目录（每次运行覆盖同名文件，可重复运行）。

## 运行

后端四组（权限 / 进程 / 打码 / 响应性）：

    cd backend
    uv run --frozen --extra dev python ../scripts/sr-verify/run_all.py

或按测试粒度（pytest 断言的是**修复后的期望**）：

    cd backend
    uv run --frozen --extra dev pytest tests/test_sr_verify_permissions.py tests/test_sr_verify_process.py tests/test_sr_verify_redaction.py tests/test_sr_verify_responsiveness.py

前端（状态组 = 契约 5 的两个时序反例，需 node_modules；worktree 没有时先
整目录复制主检出 qio/frontend/node_modules）：

    cd frontend && npm test -- --run src/__tests__/sr-f-turn-lifecycle.spec.ts

## 判定语义

* **PASS** —— 当前行为已满足修复后的契约期望（Lead 集成后应在集成 worktree 全 PASS）。
* **FAIL** —— 反例成立。**未修复基线上的 FAIL 是预期结果**，它就是修复需求的证据；
  pytest 把同一条证据显示为红色失败，运行器把细节落成 JSON。
* **SKIP** —— 环境缺件（例如本机没有 git），不计入失败，但也**不能作数**。

## 五组反例对照

| 组 | 反例（基线实际行为） | 修复后期望 |
| --- | --- | --- |
| 权限 1a/1c | `git tag <名>`（创建）在 default 与 plan 模式都 verdict=auto，零审批真的建出 tag | 非 auto；拒绝后 repo 零变化 |
| 权限 1b | FsReadTool 拒绝的工作区外 `.env`，`run_program cat` verdict=auto，真实 cat 可读出内容 | 非 auto（approve/deny）；内容不泄漏；根内普通文件 cat 保持 auto |
| 进程 2a | `git show <坏引用>` 退出码非 0 却 ok=True | ok=False + 「退出码 N」语义，输出保留 |
| 进程 2b | 工具超时/取消只放弃等待：子+孙进程仍存活 | 本次启动的进程树退出；同名诱饵进程存活；Cancel 原样传播 |
| 打码 | bus 不打码 event.data：SSE 字节 / 重连重放 / api_key 字段都含注册密钥原文 | 先打码再序列化；日志路径（已覆盖）不回退 |
| 响应性 | 同步 `route()` 在事件循环上冻结心跳；无 `route_async`；fs_find 扫描冻结循环 | `route_async` + 有界执行器：心跳窗口内推进，线程 id ≠ 主线程 |
| 状态（前端） | 见 `frontend/src/__tests__/sr-f-turn-lifecycle.spec.ts` | 契约 5 反例 A/B 的两条时序 |

## 纪律

* 证据与断言一律**布尔取证**：密钥原文绝不进入证据文件、断言消息或 pytest 报告。
* 反例只依据可观察行为（verdict、ok、字节、时间、进程存活、状态字段）。
* 子进程/进程树收尾：只杀探针自己记录到的 pid（Windows `taskkill /T /F /PID`）。
* 密钥登记表是进程级全局状态：探针在 finally 里清空它，避免污染同进程的其它测试。

## 注记（给 Lead）

* 本 worktree 的 uv 环境：`uv run --frozen pytest` 会命中系统 Python 3.10 的
  pytest（项目 venv 默认不装 dev 依赖），导致 import keyring 失败的假红。
  可靠命令是 `uv run --frozen --extra dev python -m pytest ...`（见 README 运行段）。
* 测试命名按全体纪律使用 `tests/test_sr_verify_*.py`（契约冻结文本第 12 行）。
