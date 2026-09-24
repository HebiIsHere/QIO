# 工具失败与静默失败加固（规格）

**日期：** 2026-09-24
**范围：** 后端工具执行链（工作区 / 开发工作区 / 审批 / 抓取 / 主循环 / 轨迹落库）与前端工具卡失败文案。

## 背景（来自本机安装实例的真实数据）

对 `%APPDATA%\qio\app.db`（189 次工具调用、71 次失败）逐条核对后，失败集中在五类问题上，
其中三类会让「失败没有可读原因」，一类会让整轮以**空回答**结束。

| 现象 | 证据 | 根因 |
| --- | --- | --- |
| `fs_list "."` / `fs_find` 报 `[WinError 3] 系统找不到指定的路径`（11 次） | 路径指向 `%APPDATA%\qio\workspace`，该目录不存在 | `Settings.ensure_dirs()` 不创建默认工作区根目录（`services/app.py::_computer_root` 的默认值） |
| 重启后 `dev_*` 全报「找不到工作区：ws_…」（5 次） | 磁盘上 `dev-workspaces/ws_dedd9eeb2ffb/` 里文件都在 | `DevWorkspace._tasks` 只存在内存，构造时不扫盘回填 |
| `run_shell` 报「超时（45000 毫秒）」（2 次） | 耗时字段 45025ms；用户实际回应节奏约 100s | 审批等待发生在 `run()` 内，被 `registry._default_execute` 的 45s 工具超时截断；审批服务给用户 300s |
| `find/list/info 未获批准，未执行`（6 次） | 耗时字段 300011–300042ms | 超时、拒绝、取消三种结局被写成同一句话 |
| `该页面需要登录或验证`（14 次） | 实测 `gi.yatta.moe/chs` HTTP 200，命中的是页面里邀请 AI agent 读 `/api/AGENTS` 的说明文字 | `BAD_HTML_FLAGS` 里的裸词 `verify` 误命中；真实原因是页面正文由脚本生成（页面自己写着 "doesn't work properly without JavaScript enabled"） |
| `ConnectError: `（22 次，冒号后为空） | `api.hakush.in` 反复 4 次 | `registry._default_execute` 直接拼 `f"{type(exc).__name__}: {exc}"`，底层异常无文本时只剩半句 |
| 两次「再试试？」得到**空白回答** | `messages` 里两条 `content=''` 的 assistant 消息，trace `status=done` | 护栏终止时旧构建只写日志不发事件，`final_content` 为 `None`，`persist` 写 `content=result.final_content or ""` |
| 失败原因不在轨迹表里 | `tool_calls` 里 71 条失败记录 `result` 全为空 | `_record_tool_call` 只落 `result`（输出正文），从不落 `error` |

## 需求（本规格要交付的行为）

1. **默认工作区根目录必须存在。** 应用启动后，`computer.root_dir` 为空时其默认根目录即已存在；
   用户在设置里新填的根目录在保存时同样会被创建（创建失败不阻塞，由工具如实报错）。
2. **开发工作区跨进程存活。** 进程重启后，磁盘上已有的 `ws_*` 工作区必须能被 `dev_*` 工具认出；
   `request.md` 的内容（去掉文件头）作为该任务的 `request`。
3. **审批等待不受工具超时截断。** 需要审批的工具，其工具级超时必须把审批窗口算进去；
   执行本身仍受原有执行超时约束。
4. **审批的三种结局分开表述。** 超时 / 用户拒绝 / 本轮取消分别给出不同原因，且都保留「未获批准」这个既有语义。
5. **抓取失败的原因必须可核。**
   - 连接层失败要写出目标地址与底层原因；底层无文本时给出可辨认的替代说明。
   - 「页面需要 JavaScript」单独成一种原因，不再报成登录/验证问题。
   - 登录/验证的判定只认明确短语（不再用裸词 `verify`）。
6. **回答不能是空白。** 护栏终止、预算耗尽后用户选择停止、以及任何「循环结束但没有任何最终文本」的情况，
   都必须给出一句说明当前状态与原因的文本；用户主动取消的轮次保持现状（界面已有「已停止」状态行）。
7. **失败原因落到轨迹表。** `tool_calls` 必须记录失败原因（新增 `error` 列，迁移只追加）。
8. **前端工具卡的失败结论要能读到真正的原因。** 短原因原样显示，长度上限放宽到 120 字符，
   不再把 60–120 字符之间的原因隐藏成「这次执行没有成功，展开可看原因」。

## 非目标

- 不改变审批的授权语义（单次使用、过期、摘要校验、拒绝/超时/取消）。
- 不把工具卡写进历史消息（本轮不做，属既有边界）。
- 不为外部站点做可访问性兜底（站点下架、反爬、需要浏览器渲染，仍然会失败）。
- 不改模型如何选工具（`pwd` 之类选错程序的问题不在本轮范围）。

## 契约

```python
# agent/services/computer.py
class ComputerSandbox:
    def ensure_root(self) -> Path: ...
    """建工作区根目录（幂等、只建这一层）；建不出来只记日志，由工具如实报错。"""

# agent/tools/dev_workspace.py
class DevWorkspace:
    def _restore(self) -> None: ...
    """把 root_dir 下已有的 ws_<12 位十六进制> 目录登记回内存字典。"""

# agent/tools/approval.py
def refusal_reason(decision: str) -> str: ...
"""'timeout' | 'rejected' | 'cancelled' | 其他 → 用户可读的中文原因。"""

# agent/tools/cmd_tools.py
class RunProgramTool: timeout_ms = 45_000 + APPROVAL_SLACK_MS
class RunCmdTool:     timeout_ms = 45_000 + APPROVAL_SLACK_MS
# APPROVAL_SLACK_MS = int(DEFAULT_TIMEOUT_SECONDS * 1000)（审批窗口）

# agent/storage/schema.py（迁移 19，只追加）
ALTER TABLE tool_calls ADD COLUMN error TEXT
```

## 验收方式

- 后端：`cd backend; uv run --frozen pytest`（必须全绿）。
- 评测：`uv run --frozen python -m agent.eval.run`（本轮改了工具策略与主循环，需对比基线）。
- 前端：`cd frontend; npx vue-tsc --noEmit` + `npm test`。
- 文档一致性：`python scripts/check_docs.py`。
