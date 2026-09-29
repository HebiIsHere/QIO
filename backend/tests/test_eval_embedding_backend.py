# -*- coding: utf-8 -*-
"""评测接真实嵌入模型：默认路径不变，只有显式开开关才走模型。

这些用例都不联网，也不要求本机有模型文件（唯一需要真实模型的那条用
skipif 跳过），所以没有模型的机器和 CI 依然能全绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from agent.eval import embedding_backend
from agent.eval.retrieval_eval import load_cases as load_retrieval_cases
from agent.eval.retrieval_eval import rank_case
from agent.eval.topic_eval import evaluate as eval_topic
from agent.selector.base import RecallBackend, ScoredDoc

EVALS = Path(__file__).resolve().parents[1] / "evals"
RETRIEVAL_CASES = {
    c["id"]: c for c in load_retrieval_cases(EVALS / "retrieval" / "cases.jsonl")
}


class _FixedRecall(RecallBackend):
    """词表无关的召回后端：永远只返回一条指定文档。"""

    name = "fixed"

    def __init__(self, doc_id: str) -> None:
        self._doc_id = doc_id

    def available(self) -> bool:
        return True

    def index(self, docs) -> None:
        self._indexed = list(docs)

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        return [ScoredDoc(doc_id=self._doc_id, score=1.0, source=self.name)]


class _CachingEmbedding:
    """确定性假后端，行为对齐 OnnxEmbeddingBackend：话题向量算出后就留在实例里。

    正是这个缓存让「每个用例一个新实例」成为必须：跨用例复用实例时，
    第二个用例会拿到上一个用例为同名话题算出的向量。
    """

    def __init__(self) -> None:
        self._topic_vectors: dict[str, np.ndarray] = {}

    @staticmethod
    def _vec(text: str) -> np.ndarray:
        if "阿尔法" in text:
            return np.array([1.0, 0.0], dtype=np.float32)
        return np.array([0.0, 1.0], dtype=np.float32)

    def available(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts])

    def topic_vector(self, topic_id: str) -> np.ndarray | None:
        return self._topic_vectors.get(topic_id)

    def update_topic_vector(self, topic_id: str, text: str) -> None:
        self._topic_vectors[topic_id] = self._vec(text)


def _topic_case(
    case_id: str, title: str, message: str, current: str, expected: str
) -> dict:
    return {
        "id": case_id,
        "message": message,
        "current_topic": current,
        "topics": [{"id": "t_x", "title": title, "keywords": []}],
        "expected": expected,
    }


def test_retrieval_eval_uses_injected_recall_backend():
    """注入召回后端后，排序结果必须由它决定，而不是默认的关键词后端。"""
    case = RETRIEVAL_CASES["fact_update_db"]
    assert rank_case(case)[0] == "sqlite_decision"

    ranked = rank_case(case, recall_factory=lambda: _FixedRecall("chatter_1"))
    assert ranked[0] == "chatter_1"


def test_topic_eval_builds_a_fresh_embedding_per_case():
    """同一个话题 id 出现在两个用例里时，第二个用例不能被第一个用例的向量污染。"""
    cases = [
        _topic_case("case_a", "阿尔法项目", "阿尔法 进展", "t_x", "in_topic"),
        _topic_case("case_b", "贝塔项目", "阿尔法 进展", "t_x", "new_topic"),
    ]
    metrics = eval_topic(cases, embedding_factory=_CachingEmbedding)
    assert metrics["rows"] == [
        {"id": "case_a", "expected": "in_topic", "predicted": "in_topic"},
        {"id": "case_b", "expected": "new_topic", "predicted": "new_topic"},
    ]


def test_build_backend_reports_missing_model(tmp_path: Path):
    backend, note = embedding_backend.build_backend(tmp_path)
    assert backend is None
    assert str(tmp_path) in note


LOCAL_MODEL_DIR = embedding_backend.resolve_model_dir()
HAS_LOCAL_MODEL = (LOCAL_MODEL_DIR / "model.onnx").exists() and (
    LOCAL_MODEL_DIR / "tokenizer.json"
).exists()


@pytest.mark.skipif(not HAS_LOCAL_MODEL, reason="本机没有内置模型文件")
def test_build_backend_loads_local_model():
    backend, note = embedding_backend.build_backend()
    assert backend is not None, note
    assert backend.available()
    assert backend.dims == 512
    vectors = backend.embed_texts(["数据库 迁移"])
    assert vectors is not None
    assert vectors.shape == (1, 512)


def test_run_cli_refuses_to_overwrite_baseline_with_model_run():
    from agent.eval.run import main

    with pytest.raises(SystemExit):
        main(["--embedding", "onnx", "--baseline"])


def test_run_cli_default_payload_shape_is_unchanged(capsys):
    """默认路径的产物形状必须与 baseline.json 一致（不加额外标记字段）。"""
    from agent.eval.run import main

    assert main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"topic_prediction", "retrieval", "anchor_continuation"}
