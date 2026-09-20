"""把一次具体的工具调用翻译成「普通用户能看懂的审批内容」。

第一阶段解决了审批**安全**（绑定 turn/session、单次使用、过期、摘要、能力指纹），
第三阶段要解决审批**可理解**：普通用户看到审批时应该能回答五个问题
（spec 第 67 条）——

1. QIO 想做什么？      → ``description``
2. 为什么需要？        → ``explanation``（拿不到真实原因时留空，不编造）
3. 会访问什么？        → ``access``（具体路径 / 命令 / 网址，人话）
4. 会造成什么影响？    → ``capabilities``（沿用 ``ToolExecutionPolicy.describe()``
   的约定串，前端据此给出「会修改文件 / 会执行命令」这类标签 + 副作用）
5. 一次性还是长期？    → ``scope``（``once`` / ``long_term``）

``detail`` 是给「查看详情」折叠区用的技术明细（实际命令、工作目录、完整路径），
默认不显示。这里**不**输出 capability fingerprint / policy hash / sandbox
profile 这类内部术语，它们属于开发者信息。

诚实原则：没有把握的结论不写。例如 ``run_shell`` 只声明「启动进程：是」和
破坏性副作用，不去猜测那条命令具体会写哪些文件。
"""

from __future__ import annotations

from typing import Any

# 需要用户比普通审批更明确地看待的动作（spec 第 70 条）：执行自由 shell、
# 结束进程。措辞仍然克制，不用夸张警告。
CAREFUL_TOOLS = {"run_shell", "proc_kill"}

# 会改变「长期能力/长期状态」的动作：工具注册（以后一直可用）。
LONG_TERM_TOOLS = {"create_tool", "dev_submit_tool"}

_MAX_LISTED = 10


def _as_paths(arguments: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("path", "paths", "files"):
        raw = arguments.get(key)
        if isinstance(raw, str) and raw.strip():
            out.append(raw.strip())
        elif isinstance(raw, (list, tuple)):
            out.extend(str(x).strip() for x in raw if str(x).strip())
    return out


def _truncate(items: list[str], limit: int = _MAX_LISTED) -> list[str]:
    if len(items) <= limit:
        return items
    return [*items[:limit], f"…（共 {len(items)} 项）"]


def _caps(
    *,
    network: bool = False,
    read: bool = False,
    write: bool = False,
    process: bool = False,
    side_effect: str = "pure",
    credential: str | None = None,
) -> list[str]:
    """``ToolExecutionPolicy.describe()`` 同款约定串（前端风险标签复用）。"""
    return [
        "联网：是" if network else "联网：否",
        "读取文件：是" if read else "读取文件：否",
        "写入文件：是" if write else "写入文件：否",
        "启动进程：是" if process else "启动进程：否",
        f"使用凭据：{credential}" if credential else "使用凭据：无",
        f"副作用：{side_effect}",
    ]


def describe_tool_call(tool_name: str, arguments: dict[str, Any] | None = None) -> dict:
    """返回审批界面需要的用户可见字段（见模块 docstring）。"""
    args = dict(arguments or {})
    name = str(tool_name or "").strip()
    paths = _as_paths(args)
    scope = "long_term" if name in LONG_TERM_TOOLS else "once"
    risk = "careful" if name in CAREFUL_TOOLS else "normal"
    result: dict[str, Any] = {
        "description": "",
        "explanation": "",
        "access": [],
        "capabilities": [],
        "detail": "",
        "scope": scope,
        "risk": risk,
    }

    if name in {"fs_read", "fs_list", "fs_info"}:
        what = {"fs_read": "读取文件内容", "fs_list": "列出目录内容", "fs_info": "查看文件信息"}[name]
        result["description"] = (
            f"想{what}（{len(paths)} 个路径）" if len(paths) > 1 else f"想{what}"
        )
        result["access"] = _truncate([f"读取：{p}" for p in paths])
        result["capabilities"] = _caps(read=True, side_effect="read")
        result["detail"] = "\n".join(paths)
    elif name == "fs_find":
        query = str(args.get("query") or "").strip()
        result["description"] = "想在当前工作区里查找文件"
        result["access"] = [f"查找条件：{query}"] if query else []
        result["capabilities"] = _caps(read=True, side_effect="read")
        result["detail"] = query
    elif name in {"fs_write", "fs_patch"}:
        count = max(1, len(paths))
        result["description"] = (
            f"想修改当前项目中的 {count} 个文件" if count > 1 else "想修改当前项目中的一个文件"
        )
        result["access"] = _truncate([f"写入：{p}" for p in paths])
        result["capabilities"] = _caps(write=True, side_effect="write")
        result["detail"] = "\n".join(paths)
    elif name == "run_shell":
        cmd = str(args.get("cmd") or "").strip()
        # 自定义 shell 命令的实际影响取决于命令本身：只声明可确证的「会启动进程」。
        result["description"] = "想运行一条 shell 命令"
        result["access"] = ["启动一个系统命令进程", "命令内容见下方详情"]
        result["capabilities"] = _caps(process=True, side_effect="destructive")
        result["detail"] = f"实际命令：{cmd}"
    elif name == "run_program":
        program = str(args.get("program") or "").strip()
        argv = args.get("argv")
        argv_text = " ".join(str(x) for x in argv) if isinstance(argv, (list, tuple)) else ""
        result["description"] = "想运行一个程序"
        result["access"] = [f"运行程序：{program}"] if program else []
        result["capabilities"] = _caps(process=True, side_effect="read")
        result["detail"] = f"程序：{program}\n参数：{argv_text}".strip()
    elif name == "proc_kill":
        pid = str(args.get("pid") or "").strip()
        result["description"] = f"想结束进程 {pid}" if pid else "想结束一个进程"
        result["access"] = [f"结束进程：{pid}"] if pid else ["结束一个进程"]
        result["capabilities"] = _caps(process=True, side_effect="destructive")
        result["detail"] = f"进程号：{pid}"
    elif name in {"proc_list", "sys_info"}:
        result["description"] = "想查看这台电脑的进程信息" if name == "proc_list" else "想读取系统信息"
        result["access"] = ["只读取本机信息"]
        result["capabilities"] = _caps(read=True, side_effect="read")
    elif name == "web_search":
        query = str(args.get("query") or "").strip()
        result["description"] = "想联网搜索"
        result["access"] = [f"检索内容：{query}"] if query else ["访问网络"]
        result["capabilities"] = _caps(network=True, side_effect="read")
        result["detail"] = query
    elif name == "web_fetch":
        url = str(args.get("url") or "").strip()
        result["description"] = "想抓取一个网页"
        result["access"] = [f"访问网址：{url}"] if url else ["访问网络"]
        result["capabilities"] = _caps(network=True, side_effect="read")
        result["detail"] = url
    elif name == "create_tool":
        request = str(args.get("request") or "").strip()
        result["description"] = "想创建一个新工具（先在独立工作区里起草）"
        result["access"] = ["在工具开发工作区里读写文件"]
        result["capabilities"] = _caps(write=True, side_effect="write")
        result["detail"] = request
    elif name == "dev_submit_tool":
        result["description"] = "想把这个新工具长期注册下来"
        result["access"] = ["注册后它会一直可用（除非你撤销）"]
        result["capabilities"] = _caps(write=True, side_effect="write")
        result["detail"] = str(args.get("definition") or "")[:400]
    elif name in {"dev_write_file", "dev_read_file", "dev_list_files", "dev_run_tests"}:
        target = str(args.get("name") or "").strip()
        labels = {
            "dev_write_file": "想在这个工具的工作区里写文件",
            "dev_read_file": "想读取这个工具的工作区里的文件",
            "dev_list_files": "想查看这个工具的工作区里有哪些文件",
            "dev_run_tests": "想在受限环境里运行这个工具的测试",
        }
        result["description"] = labels[name]
        result["access"] = [f"文件：{target}"] if target else ["工具开发工作区"]
        result["capabilities"] = _caps(
            process=name == "dev_run_tests",
            write=name == "dev_write_file",
            read=name != "dev_write_file",
            side_effect="write" if name == "dev_write_file" else "read",
        )
    elif name in {"correct_knowledge", "correct_entity"}:
        result["description"] = "想修正一条长期保存的信息"
        result["access"] = ["修改记忆/知识里的内容"]
        result["capabilities"] = _caps(write=True, side_effect="write")
    elif name == "memory_search":
        result["description"] = "想检索过去对话里的记忆"
        result["access"] = ["只读取记忆，不修改"]
        result["capabilities"] = _caps(read=True, side_effect="read")
    elif name in {"await_task", "read_task_result"}:
        result["description"] = "想读取一个独立任务的结果"
        result["access"] = ["只读取任务结果"]
        result["capabilities"] = _caps(read=True, side_effect="read")
    else:
        # 未登记的工具：只说「它想执行什么」，不编造行为与影响。
        from agent.tools.display import tool_label

        result["description"] = f"想执行「{tool_label(name)}」"
        result["access"] = []
        result["capabilities"] = []

    return result


# 文件 / 命令 / 进程类工具的审批走的是 `kind="computer"`（sandbox 判定为需要确认），
# 载荷里原本只有 `action` 这种半技术字段。这里把它映射回同一套行为化描述，
# 让「文件 / 命令 / 进程」三类审批和工具执行审批说同样的普通话。
_COMPUTER_ACTION_TO_TOOL: dict[str, str] = {
    "read": "fs_read",
    "write": "fs_write",
    "patch": "fs_patch",
    "list": "fs_list",
    "find": "fs_find",
    "info": "fs_info",
    "run_program": "run_program",
    "run_shell": "run_shell",
    "proc_kill": "proc_kill",
}


def describe_computer_action(payload: dict[str, Any] | None = None) -> dict:
    """`kind="computer"` 审批的行为化描述（文件 / 命令 / 进程）。

    入参是原有载荷（`action` + 具体参数），返回与 `describe_tool_call` 同构的
    用户可见字段，调用方把两者合并即可 —— 原始字段一个都不删，
    开发者详情里仍然能看到机器可读的 `action` / `risk`。
    """
    raw = dict(payload or {})
    action = str(raw.get("action") or "").strip()
    tool_name = _COMPUTER_ACTION_TO_TOOL.get(action, "")
    if not tool_name:
        # 未登记的动作：只说「需要确认」，不编造行为
        return {
            "description": "这项操作需要你的确认",
            "explanation": "",
            "access": [],
            "capabilities": [],
            "detail": "",
            "scope": "once",
        }
    # `find` 的审批载荷里带的是搜索根目录（path），不是查询词；两者都不编造：
    # 有 query 才当查询，否则按路径描述。
    args: dict[str, Any] = {}
    if tool_name in {"fs_read", "fs_write", "fs_patch", "fs_list", "fs_info"}:
        args["path"] = raw.get("path")
    elif tool_name == "run_program":
        args["program"] = raw.get("program")
        args["argv"] = raw.get("args")
    elif tool_name == "run_shell":
        args["cmd"] = raw.get("cmd")
    elif tool_name == "proc_kill":
        args["pid"] = raw.get("pid")
    described = describe_tool_call(tool_name, args)
    if tool_name == "fs_find":
        root = str(raw.get("path") or "").strip()
        described["description"] = "想在工作区外查找文件"
        described["access"] = [f"搜索范围：{root}"] if root else []
        described["detail"] = root
    # 命令类：sandbox 已经判定的风险等级是事实，保留在详情里
    risk = str(raw.get("risk") or "").strip()
    if risk:
        described["detail"] = f"{described['detail']}\n沙箱判定：{risk}".strip()
    # `risk` 这个名字在这条链路上已经被 sandbox 判定占用（danger / caution / safe），
    # 是机器可读事实，不能被「界面用的人话风险等级」覆盖（第一阶段的边界测试会挡住）。
    # 前端判断高风险本来就用 capabilities 里的副作用与行为标签，不需要这个字段。
    described.pop("risk", None)
    return described
