from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 0. 测试进程内的线程上限 —— 必须最早设置（在任何 numpy / onnxruntime /
#    tokenizers 导入之前）。xdist 下每个 worker 是独立进程，多个 worker 同时加载
#    数值库时，默认每进程开满物理核线程会让整机线程数严重超订，既拖慢总耗时也让
#    时序敏感用例抖动。这里只影响测试进程，不改生产行为（生产不导入本文件）。
#    setdefault：机器上有特殊需要时可用同名环境变量显式覆盖。
# ---------------------------------------------------------------------------
for _thread_env_var in (
    'OMP_NUM_THREADS',
    'OPENBLAS_NUM_THREADS',
    'MKL_NUM_THREADS',
    'NUMEXPR_NUM_THREADS',
    'VECLIB_MAXIMUM_THREADS',
):
    os.environ.setdefault(_thread_env_var, '1')

# 测试环境显式选择「开发豁免」：绝大多数测试关心的是业务行为，不是认证。
# 认证本身由 tests/test_api_auth.py 专门覆盖（那里显式传入会话令牌，
# 并且断言无令牌 401 / 恶意 origin 403）。
os.environ.setdefault('QIO_DEV_INSECURE', '1')
# 数据库身份自检默认关闭：它的基线存在用户级位置（注册表），测试不该碰。
# 需要测自检的用例（tests/test_db_identity.py）自己打开，并把基线指向 tmp_path。
os.environ.setdefault('QIO_DISABLE_DB_CHECK', '1')

# 开发机上的 QIO_DATA_DIR 不得泄漏进测试。它是「整个应用的数据落点覆盖」，
# 而且 agent.config.Settings.__post_init__ 是无条件覆盖 —— 连显式传进来的
# Settings(data_dir=tmp_path) 也会被它改掉。开发机设了 QIO_DATA_DIR 时，
# 用例会真的写到用户数据目录（实测：PermissionError 以及跨用例互相污染）。
# 测试必须自己声明落点：这里直接摘掉它，需要它的用例（如
# tests/test_entrypoint_split.py）自己构造子进程环境显式传。
os.environ.pop('QIO_DATA_DIR', None)

from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

# ---------------------------------------------------------------------------
# 1. 标记（marker）约定
# ---------------------------------------------------------------------------
# real_migrations：该用例必须走真实迁移（逐次对独立空库 apply_migrations），
#   不能用 session/worker 级模板库克隆。用于迁移 / 升级 / 旧版本兼容 / 结构缺失 /
#   损坏自愈 / DB 初始化 / 持久性类用例（判据见 docs/SETUP.md 测试章节）。
REAL_MIGRATIONS_MARKER = 'real_migrations'
# serial：该用例依赖真实进程清理 / 共享系统资源 / 严格时序 / 固定端口，
#   不能与并行组的其它用例同时跑（入口脚本第二阶段 -m serial -p no:xdist）。
SERIAL_MARKER = 'serial'

# 显式串行清单（文件级）。这些文件里的用例要么启动真实进程树并断言清理语义，
# 要么使用固定端口 / 严格时序阈值 / 共享系统资源；放进并行组会互相抢资源、
# 或让阈值在负载下抖动。只标记真正需要独占的用例，不要用它掩盖失败。
# 契约 §5 要求「标记 serial 并写清原因」；条目必须保持可核对（见
# tests/test_test_infra_partition.py）。
SERIAL_MODULES: dict[str, str] = {
    # 固定端口 8731 / 8732 起真实 uvicorn；断言事件循环停顿阈值（严格时序）；
    # 还会加载仓库里的真实 ONNX 模型（与并行 worker 抢 CPU 会让阈值失真）。
    'test_interactive_during_heavy_work.py': '固定端口 + 真 uvicorn + 严格事件循环停顿阈值 + 真实 ONNX 模型',
    # Windows 原生 taskkill /T /F /PID 的进程树清理，以及同名诱饵进程不得被误杀。
    'test_sr_b_process_cleanup.py': '真实 Windows taskkill 进程树清理 + 同名诱饵存活断言',
    # 真实 sleep 子/孙进程 + tasklist 取证 + 固定 1s 宽窗口（进程清理/严格时序）。
    'test_sr_verify_process.py': '真实进程树超时/取消收割 + tasklist 取证 + 固定宽窗口',
    # 真实 Job Object / 句柄与进程树清理断言（进程清理）。
    'test_tool_isolation.py': '真实 Job Object / 句柄关闭的进程树清理断言',
    # POSIX 进程组/会话清理语义（Linux 上会按进程组收割；Windows 跳过）。
    'test_main_closeout_process_group_scope.py': 'POSIX 进程组清理语义（进程组/会话共享）',
    # 真实 worker 子进程的超时/取消进程树清理协议。
    'test_sandbox_worker.py': '真实 worker 子进程的超时/取消进程树清理协议',
    # 容器/受限子进程路径的进程与输出保护（进程边界桩 + 真实进程树清理）。
    'test_sandbox_executor.py': '容器/受限子进程路径的进程与输出保护（真实子进程）',
    # 严格时序：慢读取注入 0.3s 后断言事件循环停摆 < 0.25s（并行负载下会抖动）。
    'test_acc_a_f21_scheduling.py': '严格时序：事件循环停摆阈值（并行负载下抖动）',
}

# 显式串行清单（用例级）：同一个文件里只有个别用例需要独占的，写 nodeid 前缀。
SERIAL_NODEIDS: dict[str, str] = {
    'test_container_images.py::test_real_docker_lists_and_removes_only_orphan_images': (
        'requires_docker：真实 docker 镜像构建/删除，共享 docker 守护进程资源'
    ),
    'test_dev_workflow_integration.py::test_full_dev_workflow_registers_tool': (
        'requires_docker：真实容器执行，共享 docker 守护进程资源'
    ),
    'test_dev_tools.py::test_dev_run_tests_pass_and_fail': (
        'requires_docker：真实容器执行，共享 docker 守护进程资源'
    ),
}


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    '''把显式串行清单落成 pytest.mark.serial（不改各测试文件本身）。'''
    del config
    for item in items:
        module_name = Path(str(item.fspath)).name
        reason = SERIAL_MODULES.get(module_name)
        if reason is None:
            for prefix, node_reason in SERIAL_NODEIDS.items():
                location, _, rest = item.nodeid.partition('::')
                normalized = Path(location).name + '::' + rest
                if normalized == prefix or normalized.startswith(prefix + '['):
                    reason = node_reason
                    break
        if reason is not None and item.get_closest_marker(SERIAL_MARKER) is None:
            item.add_marker(pytest.mark.serial(reason=reason))


# ---------------------------------------------------------------------------
# 2. 模板数据库：每个 xdist worker 一次（session 级），用当前代码与真实迁移生成
#    空白模板；每个用例仍拿到独立 tmp_path 数据库，用 sqlite3 backup API 复制。
#
#    不变量（刻意保留）：
#    * 模板生成走真实 agent.storage.db.connect + apply_migrations，与生产同路径；
#      迁移/升级/旧版本/损坏自愈等用例由 real_migrations 标记逐次真实迁移
#      （见 db_conn fixture），不依赖模板。
#    * 模板只读（RO 连接）；每次运行重新生成，放 pytest 的 worker 专属 basetemp，
#      不进仓库，也不跨运行复用（避免拿到过期结构）。
#    * 克隆用 sqlite3 backup API（不是文件复制）：模板 WAL 已 checkpoint，
#      backup 取一致快照，不依赖未 checkpoint 的 -wal 文件。
#    * 克隆出独立文件后再用生产 connect() 建连接：foreign_keys=ON / WAL /
#      busy_timeout=5000 与生产完全一致；两个克隆互不影响。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DbTemplate:
    '''session/worker 级空白模板库。'''

    path: Path
    source: sqlite3.Connection  # 只读模板连接，仅用于 backup()
    schema_version: int
    built_ms: float


def _open_template_readonly(path: Path) -> sqlite3.Connection:
    uri = 'file:' + path.as_posix() + '?mode=ro'
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        # 个别平台/SQLite 版本对「WAL 库只读打开」要求 -shm 存在；模板文件在本次
        # 运行内不会再被写入，immutable=1 明确告诉 SQLite 可以免锁只读。
        return sqlite3.connect('file:' + path.as_posix() + '?mode=ro&immutable=1', uri=True)


@pytest.fixture(scope='session')
def db_template(tmp_path_factory: pytest.TempPathFactory) -> DbTemplate:
    '''用当前代码 + 真实迁移生成 worker 专属空白模板（每个 worker 一次）。'''
    import time

    # tmp_path_factory 在 xdist 下是 worker 专属 basetemp（pytest-<n>），不与他人共享。
    template_dir = tmp_path_factory.mktemp('qio-db-template')
    template_path = template_dir / 'template.db'
    started = time.perf_counter()
    writer = connect(template_path)
    try:
        apply_migrations(writer)
        # 把 WAL 内容并回主库：backup API 本就能取一致快照，这里先 checkpoint
        # 让模板文件自洽（即使将来有工具直接读模板文件也不会看到半份 WAL）。
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        version_row = writer.execute('SELECT MAX(version) FROM schema_version').fetchone()
    finally:
        writer.close()
    built_ms = (time.perf_counter() - started) * 1000.0
    source = _open_template_readonly(template_path)
    template = DbTemplate(
        path=template_path,
        source=source,
        schema_version=int(version_row[0]) if version_row is not None else -1,
        built_ms=built_ms,
    )
    yield template
    source.close()


def clone_template_db(template: DbTemplate, destination: Path) -> sqlite3.Connection:
    '''从只读模板用 sqlite3 backup API 克隆出独立数据库连接。

    返回的连接用生产 connect() 打开：foreign_keys / WAL / busy_timeout 与生产一致。
    '''
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(destination))
    try:
        # 目标库是测试用的一次性副本：backup 期间不需要每次页写都 fsync。
        # 复制完成后由 connect() 打开，之后的写入仍按生产的默认同步级别进行。
        raw.execute('PRAGMA synchronous=OFF')
        template.source.backup(raw)
    finally:
        raw.close()
    return connect(destination)


def build_real_migration_connection(target: Path) -> sqlite3.Connection:
    '''真实迁移路径：对独立空库逐次 apply_migrations（迁移/升级/自愈类用例）。

    单独抽成函数是为了让 tests/test_test_infra_*.py 能用 spy 计数验证
    「real_migrations 用例确实调用了迁移」，而不是看模板是否长得对。
    '''
    conn = connect(target)
    apply_migrations(conn)
    return conn


def _wants_real_migrations(request: pytest.FixtureRequest) -> bool:
    '''real_migrations 标记 + QIO_TEST_REAL_MIGRATIONS 环境变量都可强制真实迁移。'''
    forced = os.environ.get('QIO_TEST_REAL_MIGRATIONS', '').strip().lower()
    if forced in ('1', 'true', 'yes', 'on'):
        return True
    return request.node.get_closest_marker(REAL_MIGRATIONS_MARKER) is not None


# 基础设施自测用：记录每个 db_conn 实际走的路径（模板克隆 / 真实迁移）。
# 只在测试进程内存在，不影响生产，也不参与任何断言以外的逻辑。
DB_CONN_EVENTS: list[dict[str, object]] = []


@pytest.fixture()
def db_conn_events() -> list[dict[str, object]]:
    '''给 tests/test_test_infra_*.py 观察 db_conn 实际模式的事件列表。'''
    return DB_CONN_EVENTS


@pytest.fixture()
def db_conn(tmp_path: Path, request: pytest.FixtureRequest, db_template: DbTemplate):
    target = tmp_path / 'test.db'
    if _wants_real_migrations(request):
        # 迁移/升级/旧版本/结构缺失/自愈/初始化/持久性用例：逐次对独立空库
        # 真实重放迁移，不看模板。改名/改造这条分支要同步 real_migrations 清单。
        conn = build_real_migration_connection(target)
        mode = 'real_migrations'
    else:
        conn = clone_template_db(db_template, target)
        mode = 'template_clone'
    DB_CONN_EVENTS.append(
        {
            'nodeid': request.node.nodeid,
            'mode': mode,
            'path': str(target),
            'template': str(db_template.path),
        }
    )
    yield conn
    # 阶段四 CI 崩溃收口：heavy 线程里可能仍有正在执行的任务（装配/预测提交会读写
    # 同一个共享 SQLite 连接）。关闭连接前先等工作线程真正结束，否则 CPython sqlite3
    # C 层会在「连接已关闭、线程仍使用」时崩溃（Windows access violation / Linux SIGSEGV）。
    try:
        from agent.services.heavy import wait_for_heavy_work

        asyncio.run(wait_for_heavy_work(timeout=30.0))
    except Exception:  # noqa: BLE001 - 收尾等待失败也不能吞掉测试结果，但绝不盲目关连接
        import logging

        logging.getLogger(__name__).warning('heavy work drain before db close failed', exc_info=True)
    conn.close()


# 下面保留原有 fixtures（no_real_model_calls / fake_verify / settings）不变。
@pytest.fixture(autouse=True)
def no_real_model_calls(request, monkeypatch):
    '''把「验证模型」换成假的，默认成功。

    保存凭据现在会自动做一次真实验证；项目规则明确要求测试不得依赖真实 Key 或
    联网（AGENTS.md）。这里兜住默认路径，具体用例想控制结果时用 fake_verify
    （可抛错、可返回失败分类、可断言「只打了一次、只打了这个地址」）。
    标了 @pytest.mark.real_verify 的用例例外：它们直接测验证逻辑本身
    （网络仍然由 fake 客户端顶替）。
    '''
    if request.node.get_closest_marker('real_verify'):
        yield
        return
    from agent.services import verify as verify_service

    async def _ok(**kwargs):  # noqa: ANN003
        return verify_service.VerifyResult(True, mode='native', detail='fake provider: ok')

    monkeypatch.setattr(verify_service, 'verify_model', _ok)
    monkeypatch.setattr(verify_service, 'list_models', lambda **kwargs: _empty_models())
    yield


async def _empty_models() -> list[str]:
    return []


@pytest.fixture()
def fake_verify(monkeypatch):
    '''可编程的验证桩：fake_verify.set(result) / fake_verify.set_error(exc)。'''
    from agent.services import verify as verify_service

    calls: list[dict] = []

    class _Stub:
        result = verify_service.VerifyResult(True, mode='native', detail='fake provider: ok')
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

    monkeypatch.setattr(verify_service, 'verify_model', _verify)
    return stub


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / 'data')

# ---------------------------------------------------------------------------
# 3. 旧测试兼容：话题申请是本轮新增的交互，旧测试里的脚本模型可能在真实轮里
#    直接调用 create_topic / switch_topic（它们当时期望工具立即执行）。
#    这些测试不关心「等待用户选择」这一步，因此默认把 topic_* 申请视为用户同意，
#    保持它们原有的「工具照常执行」语义；明确验证申请交互的文件用
#    @pytest.mark.topic_application 关闭这个兜底，走真实审批等待。
#
#    只兜底 bridge 的两种 kind（topic_switch / topic_create）；工具内部的
#    相似冲突二次审批（kind=create_topic）仍保持真实人工审批语义。
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _legacy_tests_auto_approve_topic_applications(request, monkeypatch):
    if request.node.get_closest_marker('topic_application') is not None:
        yield
        return

    from agent.tools import approval as approval_module

    original = approval_module.ApprovalService.request

    async def _request(self, kind, payload, *, turn_id=None, session_id=None):  # noqa: ANN001
        if str(kind or '') in ('topic_switch', 'topic_create'):
            return approval_module.ApprovalResult(
                approval_id='appr_test_auto_topic',
                decision='approved',
            )
        return await original(
            self, kind, payload, turn_id=turn_id, session_id=session_id
        )

    monkeypatch.setattr(approval_module.ApprovalService, 'request', _request)
    yield
