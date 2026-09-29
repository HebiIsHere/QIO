"""人工核验的场景集 → StressCorpus。

与自动生成语料的区别：**场景先存在，标签才有意义**。每个场景声明自己的
`kind`（事实修订 / 同话题干扰 / 跨话题 / 同名实体 / 近期噪声 / 用户偏好 /
项目推进 / 工具前置）、包含多条记忆与干扰项，每条查询都写明"为什么它属于这个场景"
（`why`）以及哪些记忆是**被推翻的旧版本**（`stale`）。

开发集/测试集按 **scenario_id** 划分：同一条记忆派生的问题必须落在同一侧，
否则会出现"边调阈值边测同一批内容"。
"""

from __future__ import annotations

import json
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.eval.stress_corpus import (
    Memory,
    RecallCase,
    StressCorpus,
    Topic,
    _overlap_ratio,
    _tier_of,
)

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "evals" / "scenarios" / "handmade.jsonl"
FROZEN_PATH = DEFAULT_PATH.with_name("FROZEN.json")


@dataclass(frozen=True)
class ScenarioSet:
    corpus: StressCorpus
    splits: dict[str, str]  # case_id -> dev / test
    kinds: dict[str, str]  # case_id -> 场景类型


def load_scenarios(path: str | Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def build_scenarios(
    scenarios: list[dict[str, Any]] | None = None,
    *,
    path: str | Path = DEFAULT_PATH,
) -> ScenarioSet:
    scenarios = scenarios if scenarios is not None else load_scenarios(path)
    topics: list[Topic] = []
    memories: list[Memory] = []
    cases: list[RecallCase] = []
    splits: dict[str, str] = {}
    kinds: dict[str, str] = {}
    texts: dict[str, str] = {}
    for scenario in scenarios:
        topic_id = f"t_{scenario['scenario_id']}"
        topics.append(
            Topic(
                id=topic_id,
                title=str(scenario["topic"]),
                keywords=(),
                query_title=str(scenario["topic"]),
            )
        )
        for memory in scenario["memories"]:
            memories.append(
                Memory(
                    id=str(memory["id"]),
                    text=str(memory["text"]),
                    topic_id=topic_id,
                    kind=str(memory.get("kind") or "knowledge"),
                    age_days=float(memory.get("age_days") or 0.0),
                    entity_ids=tuple(memory.get("entities") or ()),
                )
            )
            texts[str(memory["id"])] = str(memory["text"])
        for query in scenario["queries"]:
            expected = tuple(str(x) for x in query["expected"])
            overlap = max(_overlap_ratio(str(query["query"]), texts[m]) for m in expected)
            cases.append(
                RecallCase(
                    id=str(query["id"]),
                    category=str(scenario["kind"]),
                    query=str(query["query"]),
                    expected=expected,
                    stale=tuple(str(x) for x in query.get("stale") or ()),
                    keyword_answerable=overlap > 0,
                    overlap=round(overlap, 4),
                    tier=_tier_of(overlap),
                )
            )
            splits[str(query["id"])] = str(scenario.get("split") or "dev")
            kinds[str(query["id"])] = str(scenario["kind"])
    corpus = StressCorpus(seed=0, topics=topics, memories=memories, recall_cases=cases)
    return ScenarioSet(corpus=corpus, splits=splits, kinds=kinds)


def select_split(loaded: ScenarioSet, split: str) -> ScenarioSet:
    """取出开发集或测试集（按场景划分，问题不会跨侧）。"""
    cases = [c for c in loaded.corpus.recall_cases if loaded.splits.get(c.id) == split]
    corpus = StressCorpus(
        seed=loaded.corpus.seed,
        topics=loaded.corpus.topics,
        memories=loaded.corpus.memories,
        recall_cases=cases,
    )
    return ScenarioSet(
        corpus=corpus,
        splits={c.id: loaded.splits[c.id] for c in cases},
        kinds={c.id: loaded.kinds[c.id] for c in cases},
    )


def file_sha256(path: str | Path = DEFAULT_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(path: str | Path = DEFAULT_PATH, frozen_path: str | Path = FROZEN_PATH) -> dict:
    """把当前场景文件与划分冻结下来：记哈希 + 每条用例属于哪一侧。

    冻结之后，任何对场景文件或划分的改动都会让 `frozen_status()` 报 changed，
    必须显式重新冻结 —— 这就是"测试集不再用于调参"的可检验版本。
    """
    source = Path(path)
    loaded = build_scenarios(path=source)
    payload = {
        "frozen_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "file": source.name,
        "file_sha256": file_sha256(source),
        "counts": {
            "scenarios": len(load_scenarios(source)),
            "cases": len(loaded.corpus.recall_cases),
            "dev": sum(1 for v in loaded.splits.values() if v == "dev"),
            "test": sum(1 for v in loaded.splits.values() if v == "test"),
        },
        "cases": {c.id: loaded.splits[c.id] for c in loaded.corpus.recall_cases},
        "test_cases": [
            c.id for c in loaded.corpus.recall_cases if loaded.splits[c.id] == "test"
        ],
    }
    Path(frozen_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def frozen_status(
    path: str | Path = DEFAULT_PATH, frozen_path: str | Path = FROZEN_PATH
) -> dict:
    """对比当前文件与冻结记录。`status` 取 ok / changed / missing。"""
    frozen_file = Path(frozen_path)
    if not frozen_file.exists():
        return {"status": "missing", "detail": "尚未冻结"}
    frozen = json.loads(frozen_file.read_text(encoding="utf-8"))
    current_hash = file_sha256(path)
    if frozen.get("file_sha256") != current_hash:
        return {
            "status": "changed",
            "detail": f"场景文件已被修改（冻结于 {frozen.get('frozen_at')}）",
            "frozen_sha256": frozen.get("file_sha256"),
            "current_sha256": current_hash,
        }
    loaded = build_scenarios(path=path)
    current_cases = {c.id: loaded.splits[c.id] for c in loaded.corpus.recall_cases}
    if current_cases != frozen.get("cases"):
        return {"status": "changed", "detail": "用例划分与冻结记录不一致"}
    return {"status": "ok", "frozen_at": frozen.get("frozen_at"), "counts": frozen.get("counts")}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--freeze" in args:
        payload = freeze()
        print(json.dumps(payload["counts"], ensure_ascii=False))
        print(f"已冻结 → {FROZEN_PATH}")
        return 0
    status = frozen_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
