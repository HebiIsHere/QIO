"""工具的中文展示名。

界面里出现的工具名一律用中文（用户可见文案统一中文）；原始工具名
（`web_search` 这类）只放在 presentation.tool 里，供开发者模式 / 悬停查看，
不再直接当作界面标题。

新增内置工具时在这里补一条；未登记的（例如 Agent 自己创建的工具）回落到原始名。
"""

from __future__ import annotations

TOOL_LABELS: dict[str, str] = {
    # 运行时核心
    "echo": "回声测试",
    "now": "当前时间",
    "memory_search": "检索记忆",
    "continue_from_fragment": "接续历史片段",
    "switch_topic": "切换话题",
    "create_topic": "创建话题",
    # 联网
    "web_search": "网络搜索",
    "web_fetch": "抓取网页",
    # 电脑操控
    "fs_read": "读取文件",
    "fs_write": "写入文件",
    "fs_patch": "修改文件",
    "fs_list": "列出目录",
    "fs_find": "查找文件",
    "fs_info": "文件信息",
    "run_cmd": "执行命令",
    "sys_info": "系统信息",
    "proc_list": "进程列表",
    "proc_kill": "结束进程",
    # 工具开发
    "create_tool": "创建工具",
    "dev_list_files": "开发：列出文件",
    "dev_read_file": "开发：读取文件",
    "dev_write_file": "开发：写入文件",
    "dev_run_tests": "开发：运行测试",
    "dev_submit_tool": "开发：提交工具",
    # 子任务
    "await_task": "等待子任务",
    "read_task_result": "读取子任务结果",
    # 纠正
    "correct_entity": "修正实体",
    "correct_knowledge": "修正知识",
}


def tool_label(name: str) -> str:
    """工具的中文展示名；未登记的工具回落为原始名（诚实优先）。"""
    return TOOL_LABELS.get(name, name)
