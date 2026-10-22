"""确定性用户指令授权提取（契约 §2.2，模块 agent/security/intent.py）。

规则**保守、可测试**：只有同一句话里同时出现「明确对象」+「明确动作」才生成 grant。
不确定就不授权，走审批。这个模块只接受用户原话（由 turn 运行时传入），模型/工具
没有任何调用入口 —— 模型不能自我授权。

覆盖的典型句式（与契约一致）：

* 「把 D:\\work\\a.docx 第 3 段改成 X 并保存」 -> write(D:\\work\\a.docx)
* 「修复 D:\\proj 里的这个项目」 -> write+create(D:\\proj 子树) + execute(项目目录)
* 「删除 D:\\work\\a.tmp」 -> delete(D:\\work\\a.tmp)

刻意**不**生成的情况：

* 只提到路径、没有动作（「D:\\work\\a.docx 是什么？」）；
* 只提到动作、没有明确对象（「帮我修复一下」）；
* 从「修改项目」扩张出删除（删除授权只来自明确的删除动作 + 明确对象）；
* 相对路径 / 模糊目录（只认绝对路径，避免凭猜测的工作目录扩权）；
* URL 只在外发动作 + 明确 host 同时出现时授权。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.security.instructions import (
    Grant,
    OPERATION_CREATE,
    OPERATION_DELETE,
    OPERATION_EXECUTE,
    OPERATION_NETWORK_EGRESS,
    OPERATION_WRITE,
    Scope,
)

# 句子边界：授权只在同一句话内成立（「删除」不会跨句影响另一句的路径）。
_SENTENCE_SPLIT = re.compile(r"[。；;！？\n]")

# 明确动作词（保守：只列有「改变对象」语义的动词）。
_WRITE_VERBS = (
    "修改", "编辑", "改写", "改成", "改为", "改一下", "改好", "改到", "保存",
    "写入", "写回", "写进", "更新", "替换", "修复", "重构", "重写", "补全",
    "整理", "格式化", "润色", "排版", "生成", "创建", "新建", "追加", "插入",
    "补上", "加上",
)
_DELETE_VERBS = ("删除", "删掉", "删了", "移除", "清理掉", "清空", "清除")
_EXECUTE_VERBS = ("运行", "执行", "跑一下", "跑一遍", "测试", "构建", "编译", "启动", "调试")
_REPAIR_VERBS = ("修复", "重构", "改进", "完善", "调试", "优化", "补全", "整理")
_EGRESS_VERBS = ("发送", "发给", "上传", "传给", "提交给", "同步给", "外发", "分享给")

# 目录/项目语义：出现这些词时把对象视作目录子树，允许 write 前缀 + execute。
_DIRECTORY_HINTS = (
    "目录", "文件夹", "项目", "工程", "仓库", "代码库", "子目录", "整个项目",
    "这个项目", "里的", "下面的", "repo", "folder", "directory",
)

# 路径 token 的终止字符：空白、引号、shell 元字符与中英文标点。
_PATH_STOP = r"""\s"'“”‘’<>|*?，。；：！？、（）【】「」『』《》,;!?(){}"""
_WIN_ABS = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/](?:[^" + _PATH_STOP + r"]+))")
_UNC = re.compile(r"(\\\\[^\\/\s]+\\[^\\/\s]+(?:\\[^" + _PATH_STOP + r"]*)*)")
# POSIX 绝对路径：**必须把前导 "/" 一起捕获**。旧写法把 "/" 放在捕获组外，
# 于是 _looks_absolute_path("tmp/x") 判 False —— 在 Linux 上任何 "/tmp/..." 都提取不到，
# 明确指令授权在非 Windows 平台整体失效（CI py3.11/py3.12 实证）。
_POSIX_ABS = re.compile(r"(?<![\w:./\\])(/(?:[^" + _PATH_STOP + r"]+))")
_QUOTED = re.compile(r"""[“"']([^"'“”‘’]{2,300})[”"']""")
_URL = re.compile(r"""https?://([^/\s"'“”]+)""", re.IGNORECASE)

#: 尾随标点（不在路径里，提取时剥掉）。
_TRAILING = "，。；：！？、.,;:!?）)】」》>"


def _clean_path(token: str) -> str:
    text = str(token or "").strip()
    while text and text[-1] in _TRAILING:
        text = text[:-1]
    return text


def _looks_absolute_path(token: str) -> bool:
    text = token.strip()
    if len(text) < 3:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    if text.startswith("\\\\"):
        return True
    if text.startswith("/"):
        return True
    return False


def _extract_paths(sentence: str) -> list[tuple[int, str]]:
    """按出现顺序返回 (位置, 路径) —— 只认绝对路径与带引号的绝对路径。"""
    found: list[tuple[int, str]] = []
    for match in _WIN_ABS.finditer(sentence):
        path = _clean_path(match.group(1))
        if _looks_absolute_path(path):
            found.append((match.start(), path))
    for match in _UNC.finditer(sentence):
        path = _clean_path(match.group(1))
        if _looks_absolute_path(path):
            found.append((match.start(), path))
    for match in _POSIX_ABS.finditer(sentence):
        path = _clean_path(match.group(1))
        if _looks_absolute_path(path):
            found.append((match.start(), path))
    for match in _QUOTED.finditer(sentence):
        candidate = _clean_path(match.group(1))
        if _looks_absolute_path(candidate):
            found.append((match.start(), candidate))
    # 去重 + 排序（同一路径可能同时命中多种正则）。
    seen: set[str] = set()
    unique: list[tuple[int, str]] = []
    for position, path in sorted(found, key=lambda item: item[0]):
        # 去重键：Windows 路径大小写不敏感；POSIX 路径大小写敏感（/tmp/A 与 /tmp/a 是两个对象）。
        key = re.sub(r"[\\/]+$", "", path)
        key = key.lower() if re.match(r"^[A-Za-z]:[\\/]", key) or key.startswith("\\\\") else key
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((position, path))
    return unique


def _acts(text: str, verbs: tuple[str, ...]) -> bool:
    return any(verb in text for verb in verbs)


def _is_directory_like(path: str, sentence: str, position: int, verb: str = "") -> bool:
    """这个路径是否应被当成目录子树（项目/目录/已存在的目录/无扩展名+修复动作）。"""
    trailing = path.endswith(("\\", "/"))
    text = path.rstrip("\\/")
    try:
        if Path(text).is_dir():
            return True
    except OSError:  # pragma: no cover - 非法路径
        pass
    if trailing:
        return True
    # 只看路径前后一小段，避免整句里「项目」影响到另一个明确文件。
    window_start = max(0, position - 40)
    window_end = min(len(sentence), position + len(path) + 40)
    window = sentence[window_start:window_end]
    if any(hint in window for hint in _DIRECTORY_HINTS):
        return True
    # 修复类动作 + 无文件扩展名 -> 按项目目录处理（契约里的「修复 D:\\proj」）。
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    if verb in _REPAIR_VERBS and "." not in name:
        return True
    return False


def _make_grant(
    *,
    instruction_id: str,
    created_at: str,
    operations: set[str],
    scopes: list[Scope],
    purpose: str,
) -> Grant | None:
    if not operations or not scopes:
        return None
    return Grant(
        grant_id="",
        instruction_id=instruction_id,
        operations=frozenset(operations),
        objects=scopes,
        purpose=purpose[:200],
        source="user_explicit",
        created_at=created_at,
    )


def extract_grants(text: str, *, instruction_id: str, created_at: str) -> list[Grant]:
    """从用户原话确定性提取 grant；不确定就不授权。"""
    source = str(text or "")
    if not source.strip():
        return []
    grants: list[Grant] = []
    index = 0
    for sentence in _SENTENCE_SPLIT.split(source):
        sentence = sentence.strip()
        if not sentence:
            continue
        paths = _extract_paths(sentence)
        write_act = _acts(sentence, _WRITE_VERBS)
        delete_act = _acts(sentence, _DELETE_VERBS)
        execute_act = _acts(sentence, _EXECUTE_VERBS)
        repair_act = _acts(sentence, _REPAIR_VERBS)
        for position, path in paths:
            purpose = sentence[:200]
            directory = _is_directory_like(
                path,
                sentence,
                position,
                verb="修复" if repair_act else ("删除" if delete_act else ""),
            )
            scopes = [Scope("path_prefix", path) if directory else Scope("path", path)]
            operations: set[str] = set()
            # 写入/创建：明确 write 动作，或项目目录 + 修复类动作。
            if write_act or (directory and repair_act):
                operations.add(OPERATION_WRITE)
                if directory:
                    operations.add(OPERATION_CREATE)
            # 删除：只来自明确删除动作；目录/项目不因「修改项目」获得删除授权。
            if delete_act and not directory:
                operations.add(OPERATION_DELETE)
            # 执行：目录/项目 + 执行或修复动作（限定该目录，不产生自由 shell 权限）。
            if directory and (execute_act or repair_act):
                operations.add(OPERATION_EXECUTE)
            if not operations:
                continue
            index += 1
            grant = _make_grant(
                instruction_id=instruction_id,
                created_at=created_at,
                operations=operations,
                scopes=scopes,
                purpose=purpose,
            )
            if grant is not None:
                grant.grant_id = "grant_" + instruction_id + "_" + str(index)
                grants.append(grant)
        # 外发：明确外发动作 + 明确服务（URL host）才构成 grant。
        if _acts(sentence, _EGRESS_VERBS):
            for match in _URL.finditer(sentence):
                host = match.group(1).strip().lower()
                if not host:
                    continue
                index += 1
                grant = _make_grant(
                    instruction_id=instruction_id,
                    created_at=created_at,
                    operations={OPERATION_NETWORK_EGRESS},
                    scopes=[Scope("url_host", host)],
                    purpose=sentence[:200],
                )
                if grant is not None:
                    grant.grant_id = "grant_" + instruction_id + "_" + str(index)
                    grants.append(grant)
    return grants