"""C 臂：用 Jev 做除召回以外的五项判定。

Jev 没有向量输出，所以召回这一项直接 `NotImplementedError` —— 这不是偷懒，
是它的形态决定的（调研结论）。

每项工作都把「候选集合」放进 state/criteria，让 Jev 在闭合集合里选，
这样答案一定落在我们自己定义的标签空间里，不需要解析自由文本。
"""

from __future__ import annotations

from typing import Any

from agent.eval.stress_corpus import StressCorpus


class JevArm:
    name = "jev"

    def __init__(self, client: Any, *, max_topics: int = 200) -> None:
        self.client = client
        self.max_topics = max_topics
        self._topics: list = []
        self._entities: list = []
        self._tools: list = []

    def recall(self, cases, k: int = 5):
        raise NotImplementedError("Jev 没有向量输出，无法参与召回")

    def topic(self, cases):
        criteria = {
            t.id: f"{t.title} {(' '.join(t.keywords) if t.keywords else '')}".strip()
            for t in self._topics
        }
        criteria["__new__"] = "与上面所有已有话题都不属于同一个方向的新主题"
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"message": case.message, "current_topic": case.current_topic_id},
                {
                    "topic": {
                        "type": "choice",
                        "instructions": "这条消息属于哪个已有话题？如果它是与现有话题都不同的新方向，选 __new__。",
                        "criteria": criteria,
                    }
                },
            )
            picked = answer.choice("topic")
            if picked == "__new__" or picked is None:
                mode, predicted_topic = "new_topic", None
            elif picked == case.current_topic_id:
                mode, predicted_topic = "in_topic", picked
            else:
                mode, predicted_topic = "switch", picked
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_mode,
                    "predicted": mode,
                    "expected_topic": case.expected_topic_id,
                    "predicted_topic": predicted_topic,
                }
            )
        return rows, latencies

    def entity(self, cases):
        criteria = {c.id: f"{c.name}（{c.summary[:30]}）" for c in self._entities}
        criteria["__none__"] = "没有任何一张卡与这条消息指的是同一个对象"
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"message": case.query},
                {
                    "card": {
                        "type": "choice",
                        "instructions": "这条消息在说哪个对象？没有匹配就选 __none__。",
                        "criteria": criteria,
                    }
                },
            )
            picked = answer.choice("card")
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_card_id,
                    "predicted": None if picked == "__none__" else picked,
                }
            )
        return rows, latencies

    def tool(self, cases):
        criteria = {t.name: t.description for t in self._tools}
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"request": case.query},
                {
                    "tool": {
                        "type": "choice",
                        "instructions": "这个请求最该用哪个工具？",
                        "criteria": criteria,
                    }
                },
            )
            picked = answer.choice("tool")
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_tool,
                    "ranked": [picked] if picked else [],
                    "predicted": picked,
                }
            )
        return rows, latencies

    def dedup(self, cases):
        titles = [t.title for t in self._topics]
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"new_topic_name": case.candidate_name, "existing_topics": titles},
                {
                    "duplicate": {
                        "type": "noul",
                        "instructions": "这个新话题名和已有话题里的某一个是不是在讲同一件事？",
                        "criteria": {
                            "true": "与某个已有话题指向同一个讨论对象",
                            "false": "是已有话题都没覆盖的新方向",
                        },
                    }
                },
            )
            value = answer.noul("duplicate")
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_duplicate,
                    "expected_duplicate_of": case.duplicate_of,
                    "predicted": value >= 0.5,
                    "predicted_duplicate_of": None,
                }
            )
        return rows, latencies

    def attach(self, corpus: StressCorpus) -> "JevArm":
        """把语料里的话题与实体挂上（跑批器调用）。"""
        self._topics = list(corpus.topics)[: self.max_topics]
        self._entities = list(corpus.entities)
        self._tools = list(corpus.tools)
        return self

    def usage(self) -> dict[str, Any]:
        return self.client.summary() if hasattr(self.client, "summary") else {}
