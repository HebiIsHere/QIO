"""verify_sha.py 的单测：判定逻辑与真实 CLI 都要覆盖。

跑法（本机 / CI 都只跑这一个文件，不跑整套）：

    python -m pytest -q scripts/targeted-verify/test_verify_sha.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import verify_sha  # noqa: E402  （同目录脚本，测试时显式加入 sys.path）

SCRIPT = HERE / "verify_sha.py"

GOOD = "a" * 40
OTHER = "b" * 40


# ---------------------------------------------------------------- 纯判定

def test_identical_full_sha_passes():
    passed, lines = verify_sha.check_sha(GOOD, GOOD, label="ut")
    assert passed is True
    assert any("PASS SHA" in line for line in lines)
    assert any(GOOD in line for line in lines)


def test_uppercase_and_whitespace_are_normalized():
    passed, _ = verify_sha.check_sha("  " + GOOD.upper() + "  ", GOOD, label="ut")
    assert passed is True


def test_mismatch_fails_and_reports_both_sides():
    passed, lines = verify_sha.check_sha(GOOD, OTHER, label="ut")
    assert passed is False
    joined = "\n".join(lines)
    assert GOOD in joined and OTHER in joined
    assert "FAIL SHA 不一致" in joined


def test_short_requested_sha_is_rejected_by_default():
    passed, lines = verify_sha.check_sha(GOOD[:7], GOOD, label="ut")
    assert passed is False
    assert any("缩写" in line for line in lines)


def test_short_requested_sha_allowed_when_prefix_matches():
    passed, _ = verify_sha.check_sha(GOOD[:12], GOOD, label="ut", allow_short=True)
    assert passed is True


def test_short_requested_sha_allowed_still_fails_on_wrong_prefix():
    passed, _ = verify_sha.check_sha(OTHER[:12], GOOD, label="ut", allow_short=True)
    assert passed is False


def test_invalid_requested_sha_fails():
    passed, lines = verify_sha.check_sha("not-a-sha", GOOD, label="ut")
    assert passed is False
    assert any("FAIL 输入 sha" in line for line in lines)


def test_missing_actual_sha_fails():
    passed, lines = verify_sha.check_sha(GOOD, "", label="ut")
    assert passed is False
    assert any("拿不到实际 checkout 的完整 SHA" in line for line in lines)


def test_ref_resolution_mismatch_fails():
    passed, lines = verify_sha.check_sha(
        GOOD, GOOD, expected_ref="refs/heads/x", ref_sha=OTHER, label="ut"
    )
    assert passed is False
    assert any("解析到" in line for line in lines)


def test_ref_resolution_match_passes_and_prints_ref():
    passed, lines = verify_sha.check_sha(
        GOOD, GOOD, expected_ref="refs/heads/x", ref_sha=GOOD, label="ut"
    )
    assert passed is True
    assert any("refs/heads/x" in line for line in lines)


def test_classify_buckets():
    assert verify_sha.classify(GOOD) == "full"
    assert verify_sha.classify(GOOD[:8]) == "short"
    assert verify_sha.classify("zz") == "invalid"
    assert verify_sha.classify("") == "invalid"


# ---------------------------------------------------------------- CLI

def _run(args, cwd, env_extra=None):
    env = dict(os.environ)
    env.pop("GITHUB_SHA", None)
    env.pop("GITHUB_REF", None)
    env.pop("GITHUB_EVENT_NAME", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        check=False,
    )


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_cli_uses_git_head_and_writes_json_evidence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)

    out = tmp_path / "evidence" / "verify-sha.json"
    result = _run(
        ["--requested", head, "--repo", str(repo), "--label", "ut-cli", "--json-out", str(out)],
        cwd=repo,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS SHA" in result.stdout
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["actualSha"] == head
    assert payload["actualSource"] == "git rev-parse HEAD"
    assert payload["gitHead"] == head
    assert payload["githubSha"] is None


def test_cli_head_is_authority_even_when_github_sha_differs(tmp_path):
    """GITHUB_SHA 是事件提交（PR 上是 merge commit），不是 checkout 到工作区的提交。

    这里显式把两者做成不同并断言：默认仍然通过，且证据里同时记录两个 SHA ——
    避免「拿 GITHUB_SHA 当实际 SHA」的假绿。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    env = {"GITHUB_SHA": GOOD, "GITHUB_REF": "refs/pull/7/merge", "GITHUB_EVENT_NAME": "pull_request"}

    ok = _run(["--requested", head, "--repo", str(repo), "--label", "ut-cli"], cwd=repo, env_extra=env)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "GITHUB_SHA" in ok.stdout and GOOD in ok.stdout

    bad = _run(
        ["--requested", head, "--repo", str(repo), "--assert-github-sha", "--label", "ut-cli"],
        cwd=repo,
        env_extra=env,
    )
    assert bad.returncode == 1
    assert "FAIL GITHUB_SHA" in bad.stdout


def test_cli_assert_github_sha_passes_when_equal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    result = _run(
        ["--requested", head, "--repo", str(repo), "--assert-github-sha", "--label", "ut-cli"],
        cwd=repo,
        env_extra={"GITHUB_SHA": head},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_ignores_actual_unless_actual_from_arg(tmp_path):
    """--actual 只在显式 --actual-from arg 时生效，默认一律以 git HEAD 为准。

    这条是防「传错 actual 造成假绿」：不显式声明来源时，脚本不会用调用方给的值。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    assert head != GOOD

    ignored = _run(
        ["--requested", head, "--actual", GOOD, "--repo", str(repo), "--label", "ut-cli"],
        cwd=repo,
    )
    assert ignored.returncode == 0, ignored.stdout + ignored.stderr
    assert "git rev-parse HEAD" in ignored.stdout

    used = _run(
        [
            "--requested", GOOD,
            "--actual", GOOD,
            "--actual-from", "arg",
            "--repo", str(repo),
            "--label", "ut-cli",
        ],
        cwd=repo,
    )
    assert used.returncode == 1
    assert "与仓库 HEAD" in used.stdout


def test_cli_expected_ref_mismatch_fails(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "-m", "main"], check=True)

    ok = _run(
        ["--requested", head, "--repo", str(repo), "--expected-ref", "main", "--label", "ut-cli"],
        cwd=repo,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "ref     resolved" in ok.stdout

    bad = _run(
        ["--requested", head, "--repo", str(repo), "--expected-ref", "nope", "--label", "ut-cli"],
        cwd=repo,
    )
    assert bad.returncode == 1
    assert "无法取得 git 证据" in bad.stdout


def test_cli_short_sha_fails_by_default_and_evidence_records_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    out = tmp_path / "evidence.json"
    result = _run(
        [
            "--requested", head[:8],
            "--repo", str(repo),
            "--label", "ut-cli",
            "--json-out", str(out),
        ],
        cwd=repo,
    )
    assert result.returncode == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["requestedSha"] == head[:8]
