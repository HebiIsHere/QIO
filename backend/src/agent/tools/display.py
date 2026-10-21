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
    "run_program": "运行程序",
    "run_shell": "执行 shell 命令",
    "sys_info": "系统信息",
    "proc_list": "进程列表",
    "proc_kill": "结束进程",
    # 工具开发
    "create_tool": "创建工具",
    "dev_list_files": "开发：列出文件",
    "dev_list_tasks": "开发：列出任务",
    "dev_read_file": "开发：读取文件",
    "dev_write_file": "开发：写入文件",
    "dev_run_tests": "开发：运行测试",
    "dev_submit_tool": "开发：提交工具",
    "declare_completion": "开发：核对完成结论",
    # 子任务
    "await_task": "等待子任务",
    "read_task_result": "读取子任务结果",
    # 纠正 / 记忆写入
    "correct_entity": "修正实体",
    "correct_knowledge": "修正知识",
    "propose_knowledge": "提交知识",
    "create_entity": "创建实体",
    # 阶段三/四：互动板面与凭据目录
    "board_inspect": "查看互动板面",
    "board_read_cards": "读取板面卡片",
    "board_propose_intents": "提出板面意图",
    "board_update_intent": "更新板面意图",
    "board_wait_for_input": "等待补充信息",
    "board_create_card": "创建成果卡片",
    "board_update_card": "修改卡片",
    "board_move_card": "移动卡片",
    "board_group_cards": "分组卡片",
    "board_order_group": "调整组内顺序",
    "board_add_link": "添加卡片关系",
    "board_finish_intent": "结束板面意图",
    "list_available_credentials": "可用凭据目录",
}


def tool_label(name: str) -> str:
    """工具的中文展示名；未登记的工具回落为原始名（诚实优先）。"""
    return TOOL_LABELS.get(name, name)
