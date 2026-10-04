"""WS3 §3：慢推理期间交互请求仍可响应；取消后过时结果不提交。

两类内容：

* **受控回归**（默认跑）：用「慢嵌入 / 慢召回替身」把推理放慢到几百毫秒，
  测量事件循环的最大停顿、取消请求的响应延迟，以及取消后结果是否被提交。
  这些用例在修复前会红（同步推理直接占住事件循环），修复后绿。
* **真实测量**（`QIO_BENCH=1` 时跑，命令见 `_lead-logs/bench-heavy-work.md`）：
  用仓库里的真实 ONNX 模型 + 真实数据库 + 真实 uvicorn，测模型加载 / 话题预测 /
  记忆检索 / 索引更新 / 后台整理各自的耗时，以及期间健康探测、取消请求、事件流
  的响应情况。

不做：不改检索权重、话题阈值、记忆语义；不开多个主 turn；不碰工具事件归属与 FIFO。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

# ---------------------------------------------------------------------------
# 替身：把「推理」放慢到可测量，同时记录「结果有没有被提交」
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """一轮对话只需要一个能返回文本的适配器（模型调用不是本文件要测的东西）。"""

    mode = "native"
    model = "fake-model"

    async def complete(self, messages, tools, **kwargs):
        return Completion(message=ChatMessage(role="assistant", content="收到"))


class _SlowEmbedding:
    """受控的慢嵌入替身：等价于「每次 ONNX 推理耗时 delay 秒」。

    同时记录**提交点**：`save_topic_vectors` / `save_topic_vector` 是新路径的
    写回入口，`update_topic_vector` 是旧路径（重新推理 + 写回）的入口 —— 两条
    路径谁被调用、什么时候被调用，就是「过时结果有没有提交」的证据。
    """

    name = "onnx"
    supports_incremental = True
    model_identity = "onnx:slow-test:fp32:fake"
    dims = 8

    def __init__(self, *, delay: float = 0.2) -> None:
        self.delay = delay
        self.calls = 0
        self.saved: list[list[str]] = []
        self.on_call = None  # 可选钩子：第一次嵌入时回调（测试用来制造「期间发生变化」）
        self._topic_vectors: dict[str, np.ndarray] = {}

    # -- 接口 -------------------------------------------------------------

    def available(self) -> bool:
        return True

    def embed_texts(self, texts):
        self.calls += 1
        if self.on_call is not None:
            hook, self.on_call = self.on_call, None
            hook()
        time.sleep(self.delay)  # 同步阻塞：这就是「慢推理」
        return np.stack([self._vector(t) for t in texts]).astype(np.float32)

    def topic_vectors(self, topic_ids):
        return {tid: v for tid, v in self._topic_vectors.items() if tid in topic_ids}

    def topic_vector(self, topic_id):
        return self._topic_vectors.get(topic_id)

    def save_topic_vectors(self, topic_ids, vectors):
        self.saved.append(list(topic_ids))
        for tid, vec in zip(topic_ids, vectors):
            self._topic_vectors[tid] = vec

    def save_topic_vector(self, topic_id, vector):
        self.saved.append([topic_id])
        self._topic_vectors[topic_id] = vector

    def update_topic_vector(self, topic_id, text):
        # 旧路径（predict 内部自己重新推理并写回）：记录 + 写回
        self.saved.append([topic_id])
        self._topic_vectors[topic_id] = self._vector(text)

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        seed = sum(ord(c) for c in text) % 97
        vec = np.random.default_rng(seed).random(_SlowEmbedding.dims).astype(np.float32)
        return vec / np.linalg.norm(vec)


class _SlowRecall:
    """慢召回后端：等价于「一次向量检索耗时 delay 秒」（只读、无提交）。"""

    name = "onnx-slow"
    supports_incremental = True

    def __init__(self, *, delay: float = 0.2) -> None:
        self.delay = delay
        self.searches = 0

    def available(self) -> bool:
        return True

    def index(self, docs) -> None:
        return None

    def upsert(self, doc) -> None:
        return None

    def remove(self, doc_id) -> None:
        return None

    def search(self, query, top_k):
        self.searches += 1
        time.sleep(self.delay)
        return []

    def entity_card_search(self, query, top_k=3):
        time.sleep(self.delay)
        return []


# ---------------------------------------------------------------------------
# 夹具与测量工具
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


@pytest.fixture()
def topic(ctx: AppContext) -> str:
    return ctx.topics.nodes.create_topic("测试话题").id


async def _measure_loop_lag(work, *, tick: float = 0.005):
    """跑 `work()`，期间测事件循环的**最大停顿**（毫秒）。

    停顿 = 两次 5ms 心跳之间实际过去的时间。事件循环被同步重活占住时，心跳
    排不上队，停顿就等于那段重活的时长 —— 这正是健康探测 / 取消请求在真实
    请求里要等的时间。
    """
    gaps: list[float] = []
    stop = False
    last = time.perf_counter()

    async def _ticker() -> None:
        nonlocal last
        while not stop:
            await asyncio.sleep(tick)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    try:
        # 先让心跳真的跑起来（否则同步 work 会在心跳启动之前就把循环占住，
        # 测出来的停顿永远是 0）
        await asyncio.sleep(0)
        result = await work()
        # 同步 work 跑完时，心跳的定时器已经过期但还没轮到它执行 —— 让出几次
        # 控制权，把它测到的那段停顿记下来（否则 finally 会先把它取消掉）。
        # 用 sleep(0) 而不是 sleep(tick)：后者会把「收尾等待」算进停顿里，
        # 让快速操作的读数虚高（下限从 5ms 变成 15ms）。
        for _ in range(50):
            seen = len(gaps)
            await asyncio.sleep(0)
            if len(gaps) > seen:
                break
    finally:
        stop = True
        ticker.cancel()
        with suppress(asyncio.CancelledError):
            await ticker
    return result, max(gaps or [0.0]) * 1000


def _start_turn(ctx: AppContext, monkeypatch, message: str, topic_id: str):
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_FakeAdapter()))
    return asyncio.create_task(ctx.run_turn(message, topic_id=topic_id))


# ---------------------------------------------------------------------------
# 1. 慢推理期间事件循环仍可响应
# ---------------------------------------------------------------------------


async def test_health_probe_stays_responsive_while_slow_prediction_runs(
    ctx: AppContext, topic: str, monkeypatch
):
    """慢话题预判（ONNX 推理）期间，健康探测类的轻请求不能被占住。

    修复前：预判直接跑在事件循环上（还带一次冷启动向量补算）→ 停顿 = 整个推理
    时长；修复后：推理进有上限的工作线程 → 停顿只剩心跳本身。
    """
    slow = _SlowEmbedding(delay=0.2)
    monkeypatch.setattr(ctx.predictor, "embedding", slow)

    result, gap_ms = await _measure_loop_lag(
        lambda: _start_turn(ctx, monkeypatch, "帮我看看这段代码", topic)
    )

    assert result["ok"] is True
    assert slow.calls >= 1, "这一轮应该真的走到了嵌入（否则测的不是慢推理）"
    assert gap_ms < 100, f"慢推理期间事件循环被占住 {gap_ms:.0f} ms（健康探测要等这么久）"


async def test_slow_retrieval_does_not_block_the_loop(
    ctx: AppContext, topic: str, monkeypatch
):
    """慢向量检索（记忆召回）不能占住事件循环。

    直接量「检索这一段」：`TurnOrchestrator.build_context` 就是把
    `app.build_injection` 交给执行器跑的（这条路径只读、无事务）。整轮一起量会
    掺进轮末与后台派生工作的耗时，看不出检索本身有没有被搬走。
    """
    slow = _SlowRecall(delay=0.2)
    monkeypatch.setattr(ctx.selector, "recall", slow)

    _, gap_ms = await _measure_loop_lag(
        lambda: ctx.heavy.run(
            ctx.build_injection, "回忆一下之前的偏好", topic_id=topic, short_term=[]
        )
    )

    assert slow.searches >= 1, "这一轮应该真的走到了向量召回"
    assert gap_ms < 100, f"慢检索期间事件循环被占住 {gap_ms:.0f} ms"


async def test_a_slow_retrieval_does_not_stall_the_turn_loop(
    ctx: AppContext, topic: str, monkeypatch
):
    """整轮一起看：慢检索期间事件循环的停顿也必须明显下降。

    阈值放到 250ms 是有意的：这一轮里还夹着轮末与后台派生工作（与本条修复无关，
    见 `_lead-logs/bench-heavy-work.md` 的「残余停顿」）。修复前这里测到 446ms
    （两次 0.2s 召回串行跑在事件循环上），修复后 ~150ms。
    """
    slow = _SlowRecall(delay=0.2)
    monkeypatch.setattr(ctx.selector, "recall", slow)

    result, gap_ms = await _measure_loop_lag(
        lambda: _start_turn(ctx, monkeypatch, "回忆一下之前的偏好", topic)
    )

    assert result["ok"] is True
    assert slow.searches >= 1
    assert gap_ms < 250, f"慢检索期间事件循环被占住 {gap_ms:.0f} ms"


async def test_a_cancel_request_is_served_while_slow_prediction_runs(
    ctx: AppContext, topic: str, monkeypatch
):
    """慢推理期间用户点「停止」：取消请求本身也要能被及时处理。"""
    slow = _SlowEmbedding(delay=0.3)
    monkeypatch.setattr(ctx.predictor, "embedding", slow)

    loop = asyncio.get_running_loop()
    turn = _start_turn(ctx, monkeypatch, "先别急，慢慢来", topic)
    fired: dict[str, float] = {}
    delay = 0.05

    def _cancel() -> None:
        fired["at"] = time.perf_counter()
        ctx.turns.cancel_active()

    loop.call_later(delay, _cancel)
    scheduled_at = time.perf_counter() + delay
    result = await asyncio.wait_for(turn, timeout=10)

    assert fired, "取消回调没有执行"
    lag_ms = (fired["at"] - scheduled_at) * 1000
    assert result["ok"] is False and result["reason"] == "cancelled"
    assert lag_ms < 100, f"取消请求被慢推理挡住了 {lag_ms:.0f} ms"


# ---------------------------------------------------------------------------
# 2. 取消之后：过时的计算结果不得提交
# ---------------------------------------------------------------------------


async def test_a_cancelled_turn_does_not_commit_the_stale_prediction(
    ctx: AppContext, topic: str, monkeypatch
):
    """取消发生在慢推理途中：算完回来发现这一轮已经取消 → **不提交**。

    提交在这里是「冷启动话题向量写回」：修复前推理是同步的，取消回调要等推理
    跑完才能执行，写回早就发生了；修复后取消先到，写回被跳过。
    """
    slow = _SlowEmbedding(delay=0.3)
    monkeypatch.setattr(ctx.predictor, "embedding", slow)

    turn = _start_turn(ctx, monkeypatch, "这个话题的向量还没算过", topic)
    await asyncio.sleep(0.05)
    assert slow.calls >= 1, "这一轮应该已经进入慢推理"
    ctx.turns.cancel_active()

    result = await asyncio.wait_for(turn, timeout=10)

    assert result["ok"] is False and result["reason"] == "cancelled"
    assert slow.saved == [], "取消后过时的预判结果不许提交（话题向量不该写回）"
    assert ctx.predictor.embedding.topic_vector(topic) is None


async def test_cancel_leaves_the_cache_and_database_consistent(
    ctx: AppContext, topic: str, monkeypatch
):
    """取消之后：缓存与数据库保持一致 —— 没有半截提交，用户消息仍然在。

    用户消息不是「计算出来的结果」：它属于用户，取消一轮不该把它从历史里抹掉
    （台账/日志要靠它区分「消息都没进」与「消息已保存、只是没回答」）。
    """
    slow = _SlowEmbedding(delay=0.3)
    monkeypatch.setattr(ctx.predictor, "embedding", slow)

    turn = _start_turn(ctx, monkeypatch, "记一下：我偏好清淡饮食", topic)
    await asyncio.sleep(0.05)
    ctx.turns.cancel_active()
    result = await asyncio.wait_for(turn, timeout=10)

    assert result["ok"] is False and result["reason"] == "cancelled"
    rows = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM embeddings WHERE doc_type = 'topic'"
    ).fetchone()
    assert rows["c"] == 0, "取消后不该留下话题向量（那是过时的计算结果）"
    messages = ctx.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
    assert messages["c"] == 1, "用户消息仍然要进历史（不是计算结果，不受取消影响）"
    assert slow.saved == []


async def test_a_cancelled_turn_does_not_suggest_a_topic_switch(
    ctx: AppContext, topic: str, monkeypatch
):
    """取消之后不发「要不要切到这个话题」的建议：它是用户可见的提交。"""
    slow = _SlowEmbedding(delay=0.3)
    monkeypatch.setattr(ctx.predictor, "embedding", slow)
    # 让预判一定建议切换：另一个话题的指纹文本与查询完全一致
    other = ctx.topics.nodes.create_topic("另一个话题")
    ctx.predictor.embedding = slow

    published: list[dict] = []

    async def _capture(event) -> None:
        published.append({"type": getattr(event, "type", None), "data": getattr(event, "data", {})})

    monkeypatch.setattr(ctx.bus, "publish", _capture)

    turn = _start_turn(ctx, monkeypatch, "另一个话题", topic)
    await asyncio.sleep(0.05)
    ctx.turns.cancel_active()
    result = await asyncio.wait_for(turn, timeout=10)

    assert result["ok"] is False and result["reason"] == "cancelled"
    suggested = [
        e for e in published if str(e["type"]).endswith("TOPIC_SWITCH_SUGGESTED")
    ]
    assert suggested == [], f"取消后仍然发了切换建议：{suggested}"
    assert other  # 另一个话题存在（这条用例的语义前提）


# ---------------------------------------------------------------------------
# 3. 修复没有改变「一轮正常对话」的结果
# ---------------------------------------------------------------------------


async def test_a_normal_turn_still_commits_the_topic_vector(
    ctx: AppContext, topic: str, monkeypatch
):
    """没有被取消时，冷启动向量照常写回（复用逻辑不能把正常提交也省掉）。"""
    slow = _SlowEmbedding(delay=0.01)
    monkeypatch.setattr(ctx.predictor, "embedding", slow)

    result = await _start_turn(ctx, monkeypatch, "正常的一轮", topic)

    assert result["ok"] is True
    assert slow.saved, "正常结束时应该把算好的话题向量写回"
    assert ctx.predictor.embedding.topic_vector(topic) is not None


async def test_prediction_is_reused_for_the_same_input_and_model_identity(
    tmp_path, monkeypatch
):
    """重复计算按「输入 + 模型身份」复用：同一段文本只嵌入一次。"""
    from agent.selector.onnx import OnnxEmbeddingBackend

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    backend = OnnxEmbeddingBackend(conn, model_dir=None)
    # 手工构造可用的后端：不依赖真实模型，只验证缓存行为
    backend._session = object()
    backend._tokenizer = object()
    backend._inputs = ["input_ids", "attention_mask", "token_type_ids"]
    backend.model_identity = "onnx:test:fp32:fake"
    backend.dims = 4
    calls = {"n": 0}

    def fake_embed(texts):
        calls["n"] += len(texts)
        return np.stack(
            [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32) for _ in texts]
        )

    monkeypatch.setattr(backend, "_embed_uncached", fake_embed)

    first = backend.embed_texts(["同一段文本"])
    second = backend.embed_texts(["同一段文本"])
    assert calls["n"] == 1, "同一输入 + 同一模型身份不该重复推理"
    assert first is not None and second is not None and np.allclose(first, second)

    # 换模型身份 → 不能命中旧结果（换档位之后向量空间不同）
    backend.model_identity = "onnx:test:int8:other"
    backend.embed_texts(["同一段文本"])
    assert calls["n"] == 2, "换了模型身份必须重新推理"


# ---------------------------------------------------------------------------
# 4. 后台整理不再占住事件循环（task-12）
# ---------------------------------------------------------------------------


def _seed_maintenance_messages(ctx: AppContext, count: int) -> None:
    """塞进用户消息：维护里最重的那段（工具候选聚类）按消息条数线性变慢。"""
    for i in range(count):
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, NULL, 'user', ?, 'text', '2026-08-01T00:00:00+00:00')",
            (f"maint{i}", f"帮我查一下今天的天气怎么样 {i}"),
        )
    ctx.conn.commit()


async def test_maintenance_does_not_block_the_loop(ctx: AppContext, monkeypatch):
    """维护一轮里最重的那段（聚类里逐条消息嵌入）不能占住事件循环。

    修复前：聚类整段跑在事件循环上 —— 12 条消息 × 50ms 慢嵌入 = 约 600ms 停顿。
    修复后：聚类进有上限的执行器，循环只等结果。
    """
    slow = _SlowEmbedding(delay=0.05)
    monkeypatch.setattr(ctx, "embedding", slow)
    _seed_maintenance_messages(ctx, 12)

    result, gap_ms = await _measure_loop_lag(ctx.maintenance.run_once)

    assert result["ok"] is True
    assert slow.calls >= 1, "这一轮应该真的走到了嵌入（否则测的不是重活）"
    assert gap_ms < 100, f"维护期间事件循环被占住 {gap_ms:.0f} ms"


async def test_a_cancel_request_is_served_while_maintenance_runs(
    ctx: AppContext, monkeypatch
):
    """维护期间用户点「停止」：取消请求本身也要能被及时处理。"""
    slow = _SlowEmbedding(delay=0.05)
    monkeypatch.setattr(ctx, "embedding", slow)
    _seed_maintenance_messages(ctx, 12)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(ctx.maintenance.run_once())
    fired: dict[str, float] = {}
    delay = 0.05

    def _cancel() -> None:
        fired["at"] = time.perf_counter()
        ctx.turns.cancel_active()

    loop.call_later(delay, _cancel)
    scheduled_at = time.perf_counter() + delay
    result = await asyncio.wait_for(task, timeout=30)

    assert fired, "取消回调没有执行"
    lag_ms = (fired["at"] - scheduled_at) * 1000
    assert result["ok"] is True
    assert lag_ms < 100, f"取消请求被维护挡住了 {lag_ms:.0f} ms"


async def test_maintenance_still_commits_its_results(ctx: AppContext):
    """没有被新一轮打断时，维护照旧落地：矛盾扫描下调置信度、工具候选请求审批。

    这条是「别为了数字好看改语义」的守门用例：清理范围、判定阈值、候选生成都没动。
    """
    from agent.knowledge.lifecycle import KnowledgeService

    _seed_maintenance_messages(ctx, 3)
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="general_fact", content="用户偏好清淡饮食")
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    before = ks.get(item.id).confidence
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES ('neg1', NULL, 'user', '其实我不喜欢清淡饮食', 'text', '2026-08-02T00:00:00+00:00')"
    )
    ctx.conn.commit()

    result = await ctx.maintenance.run_once()

    assert result["ok"] is True
    assert result["contradiction_hits"] == 1, result
    assert ks.get(item.id).confidence < before, "矛盾扫描仍要下调置信度"
    assert result["tool_candidates"] >= 1, "够大的簇仍要产出工具候选"


async def test_maintenance_discards_results_when_a_new_turn_starts(
    ctx: AppContext, monkeypatch
):
    """计算期间用户开始了新一轮 → 这次维护结果不落地（提交前校验代次）。

    代次 = 这一轮维护开始时 `ctx.turns.active` 的引用；计算期间引用变了就是「过时」。
    两个重活各触发一次：矛盾扫描（纯计算钩子）与工具候选聚类（慢嵌入钩子）。
    """

    class _Turns:
        active = None

    turns = _Turns()
    monkeypatch.setattr(ctx, "turns", turns)

    slow = _SlowEmbedding(delay=0.05)
    monkeypatch.setattr(ctx, "embedding", slow)
    _seed_maintenance_messages(ctx, 3)
    from agent.knowledge.lifecycle import KnowledgeService
    from agent.services import maintenance as maint

    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="general_fact", content="用户偏好清淡饮食")
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    before = ks.get(item.id).confidence
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES ('neg2', NULL, 'user', '其实我不喜欢清淡饮食', 'text', '2026-08-02T00:00:00+00:00')"
    )
    ctx.conn.commit()

    # 第一次「开始新一轮」：发生在矛盾扫描的纯计算期间
    real_find = maint.find_contradiction_hits

    def _flip_then_find(messages, knowledge):
        turns.active = object()
        return real_find(messages, knowledge)

    monkeypatch.setattr(maint, "find_contradiction_hits", _flip_then_find)
    # 第二次「开始新一轮」：发生在工具候选聚类的嵌入期间
    slow.on_call = lambda: setattr(turns, "active", object())

    result = await ctx.maintenance.run_once()

    assert result["ok"] is True
    assert result["contradiction_hits"] == 0, "过时的扫描结果不许提交"
    assert ks.get(item.id).confidence == before, "置信度不该被过时结果改动"
    assert result["tool_candidates"] == 0, "过时的聚类结果不许落成候选"


# ---------------------------------------------------------------------------
# 5. 真实测量（QIO_BENCH=1 时运行；数字进 _lead-logs/bench-heavy-work.md）
# ---------------------------------------------------------------------------

BENCH_ENABLED = os.environ.get("QIO_BENCH") == "1"
bench = pytest.mark.skipif(
    not BENCH_ENABLED, reason="真实测量：设 QIO_BENCH=1 才运行（见 _lead-logs/bench-heavy-work.md）"
)
REPO = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO / "frontend" / "src-tauri" / "resources" / "models"
BENCH_TOPICS = 8
BENCH_FRAGMENTS = 24
BENCH_PER_FRAGMENT = 10


def _bench_ctx(tmp_path, monkeypatch) -> AppContext:
    """真实模型 + 真实数据库的 AppContext（测量用）。"""
    monkeypatch.setenv("QIO_MODELS_DIR", str(MODELS_DIR))
    conn = connect(tmp_path / "bench.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


class _BenchSummaryAdapter:
    """基准用：把片段摘要成一条可索引的记忆（真实走 IndexBuilder → memory_index）。"""

    mode = "native"
    model = "bench-model"

    async def complete(self, messages, tools, **kwargs):
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=(
                    '{"title":"饮食","summary":"用户偏好清淡饮食，不吃辣",'
                    '"entities":[],"keywords":["清淡","饮食","偏好"]}'
                ),
            )
        )


async def _seed_bench(ctx: AppContext, topics: int, fragments: int, per_fragment: int = 10) -> str:
    """建话题 + 真实的记忆索引行（封块 → 摘要 → memory_index → 向量）。

    索引规模用**封块次数**表示：每次封块产生一条 memory_index 记录，正是检索与
    索引更新要处理的规模。
    """
    main = ctx.topics.nodes.create_topic("基准话题")
    for i in range(topics):
        ctx.topics.nodes.create_topic(f"基准话题 {i}")
    for i in range(fragments):
        for j in range(per_fragment):
            ctx.memory.append_message(
                topic_id=main.id,
                role="user",
                content=f"第 {i}-{j} 条：用户偏好清淡饮食，不吃辣",
            )
        closed = await ctx._close_fragment(main.id, _BenchSummaryAdapter())
        assert closed is not None, "封块失败：基准没有可检索的数据"
    ctx._refresh_selector()
    return main.id


def _time_sync(fn, *args, **kwargs):
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, (time.perf_counter() - t0) * 1000


async def _measure_sync_on_loop(fn, *args, **kwargs):
    """同步跑 `fn`（在事件循环上）：返回 (结果, 耗时 ms, 事件循环最大停顿 ms)。

    「跑在事件循环上」就是修复前的样子 —— 这个停顿等于期间真实 HTTP 请求
    （健康探测 / 取消 / 事件流）要等的时间。

    `fn` 必须是**同步**可调用对象。传协程函数只会创建协程对象、不会执行，
    于是量到的是「创建协程」的耗时（本文件曾经因此把维护的一轮记成 0.0ms），
    所以这里直接拦下来报错 —— 异步工作用 `_measure_async_on_loop`。
    """
    box: dict = {}

    async def _work() -> None:
        t0 = time.perf_counter()
        box["out"] = fn(*args, **kwargs)
        box["ms"] = (time.perf_counter() - t0) * 1000

    _, gap_ms = await _measure_loop_lag(_work)
    if inspect.iscoroutine(box.get("out")):
        box["out"].close()  # 别留下「never awaited」警告
        raise AssertionError(
            f"{fn!r} 是协程函数：_measure_sync_on_loop 不会 await 它，"
            "异步工作请用 _measure_async_on_loop"
        )
    return box.get("out"), float(box.get("ms", 0.0)), gap_ms


async def _measure_async_on_loop(factory):
    """await 一个协程（在事件循环上）：返回 (结果, 耗时 ms, 事件循环最大停顿 ms)。

    后台整理 `MaintenanceScheduler.run_once()` 在真实运行里就是被 `_tick` await 的，
    所以这样量到的耗时与停顿才是产品里的那一份。
    """
    box: dict = {}

    async def _work() -> None:
        t0 = time.perf_counter()
        box["out"] = await factory()
        box["ms"] = (time.perf_counter() - t0) * 1000

    _, gap_ms = await _measure_loop_lag(_work)
    return box.get("out"), float(box.get("ms", 0.0)), gap_ms


def _clear_embed_cache(ctx: AppContext) -> None:
    """清掉进程内的嵌入复用缓存：A/B 对比要测「真的去推理」的那条路。"""
    cache = getattr(getattr(ctx, "embedding", None), "_embed_cache", None)
    if cache is not None:
        cache.clear()


def _report(name: str, wall_ms: float, gap_ms: float, note: str = "") -> None:
    print(f"BENCH {name}: wall={wall_ms:.1f}ms loop_gap={gap_ms:.1f}ms {note}")


@bench
async def test_bench_heavy_ops(tmp_path, monkeypatch):
    """逐个测真实重活的耗时，以及它跑在事件循环上时造成的停顿。"""
    t0 = time.perf_counter()
    ctx = _bench_ctx(tmp_path, monkeypatch)
    load_ms = (time.perf_counter() - t0) * 1000
    assert ctx.embedding is not None and ctx.embedding.available(), "需要真实模型"
    _report("model_load", load_ms, 0.0, f"identity={ctx.embedding.model_identity}")

    topic = await _seed_bench(ctx, BENCH_TOPICS, BENCH_FRAGMENTS, BENCH_PER_FRAGMENT)

    # 1) 话题预判（冷启动：所有话题向量都要补算）
    ctx.conn.execute("DELETE FROM embeddings WHERE doc_type = 'topic'")
    _, predict_ms, predict_gap = await _measure_sync_on_loop(
        ctx.predictor.predict, "用户偏好清淡饮食", current_topic_id=topic
    )
    _report("topic_prediction_cold", predict_ms, predict_gap, f"topics={BENCH_TOPICS}")

    # 1b) 话题预判（热：向量都在）
    _, predict_warm_ms, predict_warm_gap = await _measure_sync_on_loop(
        ctx.predictor.predict, "用户偏好清淡饮食", current_topic_id=topic
    )
    _report("topic_prediction_warm", predict_warm_ms, predict_warm_gap)

    # 2) 记忆检索（build_injection：嵌入 + 向量/BM25 打分 + 预算装配）
    _, retrieval_ms, retrieval_gap = await _measure_sync_on_loop(
        ctx.build_injection, "用户偏好清淡饮食", topic_id=topic, short_term=[]
    )
    _report("memory_retrieval", retrieval_ms, retrieval_gap, f"index_rows={BENCH_FRAGMENTS}")

    # 3) 索引更新（封块后的增量 upsert：嵌入 + 写 embeddings + 重建矩阵）
    row = ctx.conn.execute("SELECT id FROM memory_index LIMIT 1").fetchone()
    if row is not None:
        _, index_ms, index_gap = await _measure_sync_on_loop(ctx._upsert_selector, row["id"])
        _report("index_update_one_doc", index_ms, index_gap)
    _, refresh_ms, refresh_gap = await _measure_sync_on_loop(ctx._refresh_selector)
    _report("index_full_rebuild", refresh_ms, refresh_gap, f"index_rows={BENCH_FRAGMENTS}")

    # 4) 后台整理（维护调度的一轮）：它是 async，必须 await 才算真的跑了
    maint_result, maint_ms, maint_gap = await _measure_async_on_loop(
        ctx.maintenance.run_once
    )
    _report(
        "maintenance_run_once",
        maint_ms,
        maint_gap,
        f"async（真实 await 一轮）result={maint_result}",
    )


@bench
async def test_bench_maintenance_breakdown(tmp_path, monkeypatch):
    """维护一轮里各子步骤的耗时归属（每个子步骤用**干净的 ctx**，互不消耗彼此的工作）。

    没有这一步，「维护 460ms」就只是一个总数，不知道该拆哪一段。
    """
    from agent.services.maintenance import (
        mine_tool_candidates,
        run_dreaming,
        scan_contradictions,
    )

    async def _fresh(name: str) -> AppContext:
        sub = tmp_path / name
        sub.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("QIO_MODELS_DIR", str(MODELS_DIR))
        conn = connect(sub / "bench.db")
        apply_migrations(conn)
        fresh = AppContext(Settings(data_dir=sub), conn, EventBus())
        await _seed_bench(fresh, BENCH_TOPICS, BENCH_FRAGMENTS, BENCH_PER_FRAGMENT)
        return fresh

    # 1) 两个 prune（同步 DB 写入）
    ctx = await _fresh("prune")
    _, ms, gap = await _measure_sync_on_loop(ctx.prune_tool_outputs)
    _report("maint_prune_tool_outputs", ms, gap, "同步")
    _, ms, gap = await _measure_sync_on_loop(ctx.prune_tool_records)
    _report("maint_prune_tool_records", ms, gap, "同步")

    # 2) 矛盾扫描（协程）
    ctx = await _fresh("scan")
    _, ms, gap = await _measure_async_on_loop(lambda: scan_contradictions(ctx))
    _report("maint_scan_contradictions", ms, gap, "async")

    # 3) dreaming（协程；本机无凭据，模型那步直接返回）
    ctx = await _fresh("dream")
    _, ms, gap = await _measure_async_on_loop(lambda: run_dreaming(ctx))
    _report("maint_run_dreaming", ms, gap, "async")

    # 4) 工具候选挖掘（协程；聚类里逐条嵌入，是最可疑的一段）
    ctx = await _fresh("mine")
    _, ms, gap = await _measure_async_on_loop(lambda: mine_tool_candidates(ctx))
    _report(
        "maint_mine_tool_candidates",
        ms,
        gap,
        f"async messages={BENCH_FRAGMENTS * BENCH_PER_FRAGMENT}",
    )

    # 4b) 同一段聚类，跑在事件循环上 vs 搬到执行器（同一进程内 apples-to-apples）
    from agent.services.maintenance import cluster_texts, plan_tool_candidates

    ctx = await _fresh("cluster")
    plan = plan_tool_candidates(ctx)
    assert plan is not None
    _clear_embed_cache(ctx)
    _, on_loop_ms, on_loop_gap = await _measure_sync_on_loop(
        cluster_texts, plan.texts, plan.embedding, plan.threshold
    )
    _report(
        "maint_cluster_on_loop",
        on_loop_ms,
        on_loop_gap,
        f"messages={len(plan.texts)}（修复前的样子）",
    )
    _clear_embed_cache(ctx)
    box: dict[str, float] = {}

    async def _exec() -> None:
        t0 = time.perf_counter()
        await ctx.heavy.run(cluster_texts, plan.texts, plan.embedding, plan.threshold)
        box["ms"] = (time.perf_counter() - t0) * 1000

    _, off_gap = await _measure_loop_lag(_exec)
    _report(
        "maint_cluster_in_executor",
        box.get("ms", 0.0),
        off_gap,
        f"messages={len(plan.texts)}",
    )

    # 5) 一整轮（同一个 ctx，作为对照）
    ctx = await _fresh("full")
    _, ms, gap = await _measure_async_on_loop(ctx.maintenance.run_once)
    _report("maint_run_once_total", ms, gap, "async 整轮")


@bench
async def test_bench_http_maintenance(tmp_path, monkeypatch):
    """真实 uvicorn：**维护一轮**期间健康探测 / 取消请求 / 事件流的响应情况。"""
    import threading
    import urllib.request

    import uvicorn

    from agent.api.server import create_app

    monkeypatch.setenv("QIO_MODELS_DIR", str(MODELS_DIR))
    conn = connect(tmp_path / "bench-http-maint.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    await _seed_bench(ctx, BENCH_TOPICS, BENCH_FRAGMENTS, BENCH_PER_FRAGMENT)

    port = 8732
    loop = asyncio.new_event_loop()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", loop="asyncio")
    )

    def _serve() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    for _ in range(400):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn 没起来"
    base = f"http://127.0.0.1:{port}"

    def _probe(path: str, *, method: str = "GET", timeout: float = 60.0) -> float:
        t0 = time.perf_counter()
        request = urllib.request.Request(f"{base}{path}", method=method)
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            resp.read()
        return (time.perf_counter() - t0) * 1000

    def _sse_connect(timeout: float = 60.0) -> float:
        t0 = time.perf_counter()
        resp = urllib.request.urlopen(f"{base}/api/events", timeout=timeout)
        try:
            return (time.perf_counter() - t0) * 1000
        finally:
            resp.close()

    out: dict[str, float] = {}
    probes: list[threading.Thread] = []

    def _client() -> None:
        time.sleep(0.02)  # 让维护先开始
        probes.extend(
            [
                threading.Thread(target=lambda: out.__setitem__("health", _probe("/api/health"))),
                threading.Thread(
                    target=lambda: out.__setitem__(
                        "cancel", _probe("/api/turns/cancel", method="POST")
                    )
                ),
                threading.Thread(target=lambda: out.__setitem__("events", _sse_connect())),
            ]
        )
        for probe in probes:
            probe.start()
        for probe in probes:
            probe.join(timeout=120)

    client = threading.Thread(target=_client)
    client.start()
    maint_started = time.perf_counter()
    fut = asyncio.run_coroutine_threadsafe(ctx.maintenance.run_once(), loop)
    maint_result = fut.result(timeout=300)
    maint_ms = (time.perf_counter() - maint_started) * 1000
    client.join(timeout=120)

    _report("http_health_during_maintenance", out.get("health", -1.0), 0.0, f"维护整轮 {maint_ms:.1f}ms")
    _report("http_cancel_during_maintenance", out.get("cancel", -1.0), 0.0)
    _report("http_sse_during_maintenance", out.get("events", -1.0), 0.0)
    print(f"BENCH maintenance_result: {maint_result}")

    server.should_exit = True
    thread.join(timeout=20)


@bench
async def test_bench_loop_gap_for_real_ops(tmp_path, monkeypatch):
    """真实重活跑在事件循环上 vs 搬到执行器：事件循环停顿的对比。"""
    ctx = _bench_ctx(tmp_path, monkeypatch)
    topic = await _seed_bench(ctx, BENCH_TOPICS, BENCH_FRAGMENTS, BENCH_PER_FRAGMENT)

    async def _on_loop() -> None:
        ctx.conn.execute("DELETE FROM embeddings WHERE doc_type = 'topic'")
        _clear_embed_cache(ctx)
        ctx.predictor.predict("用户偏好清淡饮食", current_topic_id=topic)

    async def _offloaded() -> None:
        ctx.conn.execute("DELETE FROM embeddings WHERE doc_type = 'topic'")
        _clear_embed_cache(ctx)
        plan = ctx.predictor.plan_prediction("用户偏好清淡饮食", current_topic_id=topic)
        assert plan is not None
        embedded = await ctx.heavy.run(ctx.predictor.compute_embedding, plan)
        ctx.predictor.finish_prediction(plan, embedded)

    _, on_loop_gap = await _measure_loop_lag(_on_loop)
    _, offloaded_gap = await _measure_loop_lag(_offloaded)
    _report("loop_gap_on_loop", on_loop_gap, on_loop_gap, "同步执行（修复前）")
    _report("loop_gap_offloaded", offloaded_gap, offloaded_gap, "执行器（修复后）")
    # 只做「确实变小」的健全性检查：具体幅度取决于推理本身的 GIL 占用
    # （见 _lead-logs/bench-heavy-work.md 的「残余停顿」一节），不适合写死阈值。
    assert offloaded_gap < on_loop_gap, (
        f"搬到执行器之后停顿没有下降：on_loop={on_loop_gap:.0f}ms "
        f"offloaded={offloaded_gap:.0f}ms"
    )


@bench
async def test_bench_prediction_phases(tmp_path, monkeypatch):
    """话题预判三段各自的耗时与事件循环停顿（看剩下来的时间花在哪一段）。"""
    ctx = _bench_ctx(tmp_path, monkeypatch)
    topic = await _seed_bench(ctx, BENCH_TOPICS, BENCH_FRAGMENTS, BENCH_PER_FRAGMENT)

    ctx.conn.execute("DELETE FROM embeddings WHERE doc_type = 'topic'")
    _clear_embed_cache(ctx)
    _, plan_ms, plan_gap = await _measure_sync_on_loop(
        ctx.predictor.plan_prediction, "用户偏好清淡饮食", current_topic_id=topic
    )
    plan = ctx.predictor.plan_prediction("用户偏好清淡饮食", current_topic_id=topic)
    assert plan is not None
    _, compute_ms, compute_gap = await _measure_sync_on_loop(
        ctx.predictor.compute_embedding, plan
    )
    embedded = ctx.predictor.compute_embedding(plan)
    _, finish_ms, finish_gap = await _measure_sync_on_loop(
        ctx.predictor.finish_prediction, plan, embedded
    )
    _report("phase_plan_input_read", plan_ms, plan_gap, "输入读取（事件循环一侧）")
    _report("phase_compute_embedding", compute_ms, compute_gap, "纯计算（可搬到执行器）")
    _report("phase_finish_commit", finish_ms, finish_gap, "结果提交（已搬到执行器；这里单测它自身耗时）")


@bench
async def test_bench_http_responsiveness(tmp_path, monkeypatch):
    """真实 uvicorn：慢推理期间健康探测 / 取消请求 / 事件流的响应情况。

    对比同一段真实重活「跑在服务端事件循环上」与「搬到执行器」两种情况下，
    真实 HTTP 请求要等多久。
    """
    import threading
    import urllib.request

    import uvicorn

    from agent.api.server import create_app

    monkeypatch.setenv("QIO_MODELS_DIR", str(MODELS_DIR))
    conn = connect(tmp_path / "bench-http.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    topic = await _seed_bench(ctx, BENCH_TOPICS, BENCH_FRAGMENTS, BENCH_PER_FRAGMENT)

    port = 8731
    loop = asyncio.new_event_loop()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", loop="asyncio")
    )

    def _serve() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    for _ in range(400):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn 没起来"
    base = f"http://127.0.0.1:{port}"

    def _probe(path: str, *, method: str = "GET", timeout: float = 30.0) -> float:
        t0 = time.perf_counter()
        request = urllib.request.Request(f"{base}{path}", method=method)
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            resp.read()
        return (time.perf_counter() - t0) * 1000

    def _sse_connect(timeout: float = 30.0) -> float:
        """事件流：测**连接建立（响应头到达）**的耗时。

        新连接不重放历史（见 bus 的策略），所以读第一个字节会一直等 —— 这里
        只测「服务端有没有被卡住不给响应」这一件事，读完头就关。
        """
        t0 = time.perf_counter()
        resp = urllib.request.urlopen(f"{base}/api/events", timeout=timeout)
        try:
            return (time.perf_counter() - t0) * 1000
        finally:
            resp.close()

    async def _block_on_loop() -> None:
        for _ in range(6):  # 重复六次：把阻塞窗口拉宽到几百毫秒，三个探针都落得进去
            ctx.conn.execute("DELETE FROM embeddings WHERE doc_type = 'topic'")
            _clear_embed_cache(ctx)
            ctx.predictor.predict("用户偏好清淡饮食", current_topic_id=topic)

    async def _run_offloaded() -> None:
        for _ in range(6):  # 同一份工作，但每一步都经过执行器
            ctx.conn.execute("DELETE FROM embeddings WHERE doc_type = 'topic'")
            _clear_embed_cache(ctx)
            plan = ctx.predictor.plan_prediction("用户偏好清淡饮食", current_topic_id=topic)
            assert plan is not None
            embedded = await ctx.heavy.run(ctx.predictor.compute_embedding, plan)
            await ctx.heavy.run(ctx.predictor.finish_prediction, plan, embedded)

    def _run_case(offloaded: bool) -> dict[str, float]:
        out: dict[str, float] = {}

        def _client() -> None:
            time.sleep(0.02)  # 让重活先开始
            # 三种请求**并发**打：它们都要落在同一个阻塞窗口里，串行的话只有
            # 第一个能测到阻塞（后面的等前面回来时窗口已经结束）。
            probes = [
                threading.Thread(target=lambda: out.__setitem__("health", _probe("/api/health"))),
                threading.Thread(
                    target=lambda: out.__setitem__(
                        "cancel", _probe("/api/turns/cancel", method="POST")
                    )
                ),
                threading.Thread(target=lambda: out.__setitem__("events", _sse_connect())),
            ]
            for probe in probes:
                probe.start()
            for probe in probes:
                probe.join(timeout=60)

        client = threading.Thread(target=_client)
        client.start()
        fut = asyncio.run_coroutine_threadsafe(
            _run_offloaded() if offloaded else _block_on_loop(), loop
        )
        fut.result(timeout=120)
        client.join(timeout=60)
        return out

    baseline_health = _probe("/api/health")
    baseline_cancel = _probe("/api/turns/cancel", method="POST")
    on_loop = _run_case(offloaded=False)
    offloaded = _run_case(offloaded=True)

    _report("http_health_baseline", baseline_health, baseline_health)
    _report("http_health_during_on_loop", on_loop["health"], on_loop["health"], "重活跑在事件循环上")
    _report("http_health_during_offloaded", offloaded["health"], offloaded["health"], "重活搬到执行器")
    _report("http_cancel_baseline", baseline_cancel, baseline_cancel)
    _report("http_cancel_during_on_loop", on_loop["cancel"], on_loop["cancel"])
    _report("http_cancel_during_offloaded", offloaded["cancel"], offloaded["cancel"])
    _report("http_sse_during_on_loop", on_loop["events"], on_loop["events"])
    _report("http_sse_during_offloaded", offloaded["events"], offloaded["events"])

    server.should_exit = True
    thread.join(timeout=20)
    assert offloaded["health"] < on_loop["health"], "执行器版本的健康探测应当更快"
