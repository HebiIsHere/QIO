"""开发工作区里的多文件项目：子目录可读写，越界与保留名一律拒绝。

回归的缺口：文件工具只接受**单层文件名**（`_SAFE_NAME`），模型写不出
`pkg/util.py` 这样的项目结构，也写不出带 `__init__.py` 的包 —— 稍微像样的
工具都做不了。
"""

from __future__ import annotations

import pytest

from agent.tools.dev_workspace import DevWorkspace


def test_subdirectory_files_are_written_read_and_listed(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("做一个带工具的包")

    ws.write_file(task.id, "pkg/__init__.py", "")
    ws.write_file(task.id, "pkg/util.py", "def double(x):\n    return x * 2\n")
    ws.write_file(task.id, "tool.py", "from pkg.util import double\n")

    assert ws.read_file(task.id, "pkg/util.py") == "def double(x):\n    return x * 2\n"
    files = ws.list_files(task.id)
    assert "pkg/util.py" in files
    assert "pkg/__init__.py" in files
    assert "tool.py" in files


def test_listing_is_stable_and_uses_forward_slashes(tmp_path):
    """枚举顺序稳定、分隔符统一用 `/`：模型与摘要都依赖它。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.write_file(task.id, "b/two.py", "2")
    ws.write_file(task.id, "a/one.py", "1")

    assert ws.list_files(task.id) == [
        "a/one.py",
        "b/two.py",
        "request.md",
        "tool.json",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "pkg/../../escape.py",
        "pkg/../escape.py",
        "/abs.py",
        "C:/windows.py",
        "c:\\windows.py",
        "..\\escape.py",
        "",
        "pkg/",
        "./pkg/x.py",
        "pkg//x.py",
        "pkg/x.py/",
    ],
)
def test_paths_outside_the_workspace_are_rejected(tmp_path, name):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")

    with pytest.raises(ValueError):
        ws.write_file(task.id, name, "boom")

    # 真的什么都没写出来
    assert not (tmp_path / "escape.py").exists()
    assert ws.list_files(task.id) == ["request.md", "tool.json"]


@pytest.mark.parametrize("name", ["state.json", "pkg/state.json", "request.md", "pkg/request.md"])
def test_reserved_names_are_protected_at_any_depth(tmp_path, name):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")

    with pytest.raises(ValueError):
        ws.write_file(task.id, name, "{}")


def test_content_digest_covers_subdirectory_files(tmp_path):
    """证据绑定的是整个项目：改一个子模块里的字符，旧测试结论就失效。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.write_file(task.id, "pkg/util.py", "VALUE = 1")
    before = ws.content_digest(task.id)

    ws.write_file(task.id, "pkg/util.py", "VALUE = 2")

    assert ws.content_digest(task.id) != before


def test_deep_paths_are_rejected(tmp_path):
    """目录层级有限：一个失控的路径不该让工作区变成任意深的结构。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    deep = "/".join(f"d{i}" for i in range(12)) + "/x.py"

    with pytest.raises(ValueError):
        ws.write_file(task.id, deep, "x")
