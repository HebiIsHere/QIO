from __future__ import annotations

import os

import pytest
from pathlib import Path

# 测试环境显式选择「开发豁免」：绝大多数测试关心的是业务行为，不是认证。
# 认证本身由 tests/test_api_auth.py 专门覆盖（那里显式传入会话令牌，
# 并且断言无令牌 401 / 恶意 origin 403）。
os.environ.setdefault("QIO_DEV_INSECURE", "1")
# 数据库身份自检默认关闭：它的基线存在用户级位置（注册表），测试不该碰。
# 需要测自检的用例（tests/test_db_identity.py）自己打开，并把基线指向 tmp_path。
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture(autouse=True)
def no_real_model_calls(request, monkeypatch):
    """把「验证模型」换成假的，默认成功。

    保存凭据现在会自动做一次真实验证；项目规则明确要求测试不得依赖真实 Key 或
    联网（AGENTS.md）。这里兜住默认路径，具体用例想控制结果时用 `fake_verify`
    （可抛错、可返回失败分类、可断言「只打了一次、只打了这个地址」）。
    标了 `@pytest.mark.real_verify` 的用例例外：它们直接测验证逻辑本身
    （网络仍然由 fake 客户端顶替）。
    """
    if request.node.get_closest_marker("real_verify"):
        yield
        return
    from agent.services import verify as verify_service

    async def _ok(**kwargs):  # noqa: ANN003
        return verify_service.VerifyResult(True, mode="native", detail="fake provider: ok")

    monkeypatch.setattr(verify_service, "verify_model", _ok)
    monkeypatch.setattr(verify_service, "list_models", lambda **kwargs: _empty_models())
    yield


async def _empty_models() -> list[str]:
    return []


@pytest.fixture()
def fake_verify(monkeypatch):
    """可编程的验证桩：`fake_verify.set(result)` / `fake_verify.set_error(exc)`。"""
    from agent.services import verify as verify_service

    calls: list[dict] = []

    class _Stub:
        result = verify_service.VerifyResult(True, mode="native", detail="fake provider: ok")
        error: BaseException | None = None

        def set(self, result) -> None:  # noqa: ANN001
            self.result = result

        def set_error(self, exc: BaseException) -> None:
            self.error = exc

        @property
        def calls(self) -> list[dict]:
            return calls

    stub = _Stub()

    async def _verify(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        if stub.error is not None:
            raise stub.error
        return stub.result

    monkeypatch.setattr(verify_service, "verify_model", _verify)
    return stub


@pytest.fixture()
def db_conn(tmp_path: Path):
    conn = connect(tmp_path / "test.db")
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")
