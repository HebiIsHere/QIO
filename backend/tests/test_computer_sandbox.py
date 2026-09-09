from __future__ import annotations

from agent.services.computer import ComputerSandbox, CommandRisk


def _make(root="C:/work", mode="default") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: root, permission_mode=lambda: mode)


def test_classify_low_commands():
    s = _make()
    assert s.classify_command("git status") == CommandRisk.LOW
    assert s.classify_command("ls -la") == CommandRisk.LOW
    assert s.classify_command("pwd") == CommandRisk.LOW


def test_classify_danger_commands():
    s = _make()
    # 破坏性/提权命令归为 DANGER（比 HIGH 更严重）
    assert s.classify_command("rm -rf /tmp/x") == CommandRisk.DANGER
    assert s.classify_command("sudo reboot") == CommandRisk.DANGER
    assert s.classify_command("dd if=/dev/zero of=/dev/sda") == CommandRisk.DANGER


def test_classify_high_commands():
    s = _make()
    # 状态改变但未匹配白名单：fail-closed → HIGH（走审批）
    assert s.classify_command("pip install requests") == CommandRisk.HIGH


def test_classify_fail_closed_unknown():
    s = _make()
    # 未匹配白名单的命令应视为 HIGH（fail-closed），而非放行
    assert s.classify_command("python weird_script.py") == CommandRisk.HIGH


def test_check_path_within_root_auto():
    s = _make()
    assert s.check_path("C:/work/a.txt") == "auto"


def test_check_path_redline_deny():
    s = _make()
    assert s.check_path("C:/work/.env") == "deny"
    assert s.check_path("C:/work/.git/config") == "deny"


def test_check_path_outside_root_approve():
    s = _make()
    assert s.check_path("C:/other/file.txt") == "approve"


def test_check_path_traversal_safe():
    s = _make()
    # ../ 穿越不应被误判为根内自动（resolve 后落在根外 → approve，需审批）
    assert s.check_path("C:/work/../../etc/passwd") == "approve"


def test_mode_default():
    assert _make(mode="default").mode == "default"


def test_read_verdict_default():
    s = _make()
    assert s.read_verdict("C:/work/a.txt") == "auto"
    assert s.read_verdict("C:/other/a.txt") == "approve"
    assert s.read_verdict("C:/work/.env") == "deny"


def test_write_verdict_default_and_accept_edits():
    # default：根内写也需审批；accept-edits：根内写自动放行
    assert _make(mode="default").write_verdict("C:/work/a.txt") == "approve"
    assert _make(mode="accept-edits").write_verdict("C:/work/a.txt") == "auto"
    # plan：只读，拒绝写
    assert _make(mode="plan").write_verdict("C:/work/a.txt") == "deny"
    # 红线在任何模式都拒绝
    assert _make(mode="accept-edits").write_verdict("C:/work/.env") == "deny"


def test_command_verdict():
    s = _make()
    verdict, risk = s.command_verdict("git status")
    assert verdict == "auto" and risk == CommandRisk.LOW
    verdict, risk = s.command_verdict("pip install requests")
    assert verdict == "approve" and risk == CommandRisk.HIGH
    # plan：低危放行，高危拒绝
    plan = _make(mode="plan")
    assert plan.command_verdict("git status")[0] == "auto"
    assert plan.command_verdict("pip install requests")[0] == "deny"
    # bypass：全放行
    assert _make(mode="bypass").command_verdict("rm -rf /x")[0] == "auto"
