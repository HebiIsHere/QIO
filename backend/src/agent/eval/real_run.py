"""真实环境数据：用产品自己的路径建记忆库，再由真实模型生成提问。

与合成语料的区别只有一处，但很关键：记忆**不是**我拼的模板，而是
「真实中文文档 → 产品封块 → 真实模型按产品提示词写摘要」这条链路产出的，
存进 `memory_index` 的就是生产里会存的那种摘要。

刻意偏离默认的地方（报告里必须标注）：

- `fragment.max_turns` 调成 1，让每篇文档封一次块、产出一条记忆；
- 语料以「用户贴一段资料」的形式进入片段，不是用户自己的话。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.adapters.base import BaseAdapter, ChatMessage, Completion
from agent.eval.real_corpus import (
    DEFAULT_CACHE,
    BudgetGuard,
    WikiDoc,
    load_wiki_docs,
    read_jsonl,
    shard_manifest,
    write_jsonl,
)
from agent.eval.stress_corpus import (
    CATEGORIES,
    DedupCase,
    EntityCard,
    EntityCase,
    Memory,
    RecallCase,
    StressCorpus,
    ToolCase,
    ToolSpecLite,
    Topic,
    TopicCase,
)

# 免费档（qwen/qwen3.8-27b:free）实测会被 429 限流，不适合批量跑；
# 换成最便宜的付费档：约 $0.02/M 输入、$0.09/M 输出，一千条记忆约 $0.2。
DEFAULT_MODEL = "openai/gpt-oss-20b"
FALLBACK_MODEL = "mistralai/mistral-nemo"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
STORE_CACHE = "store.jsonl"
QUESTIONS_CACHE = "questions.jsonl"
CASES_CACHE = "real_cases.json"

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def post_chat(payload: dict[str, Any], api_key: str, *, tries: int = 4, timeout: float = 120.0) -> dict:
    """带退避重试的 chat 调用：限流（429）与 5xx 都重试，其它错误直接抛。"""
    import httpx

    last: Exception | None = None
    for attempt in range(tries):
        try:
            response = httpx.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=timeout,
            )
            if response.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"{response.status_code}", request=response.request, response=response
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
            time.sleep(min(8.0, 1.0 * (2**attempt)))
    raise RuntimeError(f"模型调用连续失败：{type(last).__name__}: {last}")


@dataclass
class StoreResult:
    data_dir: Path
    memories: list[Memory] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    entities: list[EntityCard] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    adapter: Any = None
    adapter_model: str = ""
    copied_key_ids: list[str] = field(default_factory=list)


def seed_credentials(conn, source_db: Path | None = None) -> list[str]:
    """把用户真实库里的 active 凭据复制一份到实验库。

    实验的记忆库跑在独立数据目录（不污染日常使用的 app.db），但模型 Key 存在真实库里，
    所以这里只读地复制「标签 + 端点 + 默认模型 + 密钥」；密钥走 OS 凭据库，
    全程不打印、不落文件。返回新库里的 key_id，供跑完清理。
    """
    import sqlite3

    from agent.config import default_data_dir
    from agent.credentials.store import CredentialStore

    source_db = Path(source_db) if source_db else default_data_dir() / "app.db"
    current = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    if not source_db.exists() or source_db == current:
        return []
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(
            "SELECT * FROM credentials WHERE status = 'active' AND enabled = 1"
        ).fetchall()
    except sqlite3.OperationalError:
        src.close()
        return []
    src_store = CredentialStore(src)
    dst_store = CredentialStore(conn)
    copied: list[str] = []
    for row in rows:
        secret = src_store.get_secret(row["id"])
        if not secret:
            continue
        created = dst_store.create(
            row["id"],
            secret,
            json.loads(row["tags"] or "[]"),
            endpoint=row["endpoint"],
            default_model=row["default_model"],
            note="stress copy",
        )
        copied.append(created if isinstance(created, str) else row["id"])
    src.close()
    return copied


class OpenRouterAdapter(BaseAdapter):
    """给产品提炼链路用的适配器：一进一出，同时记录真实花费。"""

    mode = "native"
    endpoint = "https://openrouter.ai/api/v1"
    tools_in_prompt = False

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, guard: BudgetGuard | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.guard = guard or BudgetGuard()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[Any],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        payload = {
            "model": self.model,
            "messages": [
                {"role": m.role, "content": m.content or ""}
                for m in messages
                if m.role in ("system", "user", "assistant")
            ],
            "temperature": 0.2 if temperature is None else temperature,
        }
        import asyncio

        data = await asyncio.to_thread(post_chat, payload, self.api_key)
        usage = data.get("usage") or {}
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.guard.add(float(usage.get("cost") or 0.0))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return Completion(message=ChatMessage(role="assistant", content=text))

    def to_chat(self, messages: list[ChatMessage]) -> Completion:
        return Completion(message=ChatMessage(role="assistant", content=""))


def chat_json(
    api_key: str,
    model: str,
    prompt: str,
    guard: BudgetGuard,
    *,
    timeout: float = 120.0,
) -> Any:
    """同步调用一次，返回解析后的 JSON（失败返回 None）。"""
    data = post_chat(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
        api_key,
        timeout=timeout,
    )
    usage = data.get("usage") or {}
    guard.add(float(usage.get("cost") or 0.0))
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def ask_json(adapter, prompt: str, *, temperature: float = 0.3) -> Any:
    """用**产品自己的适配器**问一次并解析 JSON（凭据、端点、重试策略都走生产那条路）。"""
    async def _run() -> str:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)], tools=[], temperature=temperature
        )
        return completion.message.content or ""

    text = asyncio.run(_run())
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def build_store(
    docs: list[WikiDoc],
    *,
    data_dir: Path,
    adapter: Any | None = None,
    limit: int | None = None,
    raw_text: bool = True,
) -> StoreResult:
    """把文档喂进产品路径，产出真实记忆库。

    `raw_text=True` 跳过「模型写摘要」这一步：记忆内容直接用原文，索引仍由产品
    自己的 `IndexBuilder` 写、话题仍由产品的 `NodeService` 建。跑得快、不受模型
    语言漂移影响；代价是记忆形态与生产不一致（生产存的是模型摘要），报告里要标注。
    """
    from agent.config import default_data_dir

    # 真实凭据在用户日常使用的库里；必须在改写 QIO_DATA_DIR 之前把它记下来。
    source_db = default_data_dir() / "app.db"
    os.environ["QIO_DATA_DIR"] = str(data_dir)
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    settings = Settings(data_dir=data_dir)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    apply_migrations(conn)
    ctx = AppContext(settings, conn, EventBus())
    ctx.settings_store.set("fragment.max_turns", "30")
    ctx.fragments.max_turns = 30
    # 凭据总要带过来：原文模式下摘要不调模型，但提问生成仍要用产品那把 Key。
    copied_key_ids = seed_credentials(conn, source_db)
    result = StoreResult(data_dir=data_dir)
    if adapter is None:
        adapter = asyncio.run(ctx.build_adapter())
    if adapter is None:
        raise SystemExit("没有可用的 main-loop 凭据：先在设置里配一把模型 Key")
    result.adapter = adapter
    result.adapter_model = getattr(adapter, "model", "")
    result.copied_key_ids = copied_key_ids
    chosen = docs[:limit] if limit else docs
    for doc in chosen:
        # 话题名直接用文档标题本身：这才是用户看到的那种话题名
        topic = ctx.topics.nodes.create_topic(doc.title[:32])
        topic_id = topic.id
        ctx.memory.append_message(
            topic_id=topic_id,
            role="user",
            content=doc.text,
        )
        ctx.memory.append_message(
            topic_id=topic_id,
            role="assistant",
            content="已记录这份资料的要点。",
        )
        fragment = ctx.fragments.open_fragment(topic_id)
        if fragment is None:
            result.failures.append({"title": doc.title, "error": "没有开放片段"})
            continue
        try:
            asyncio.run(ctx.memory_lifecycle.close_fragment(topic_id, None))
            if raw_text:
                # 关键：生产里"记忆正文"存在 fragments.summary，检索器读的是
                # `summary + title`。IndexBuilder.build(summary_text=...) 只用来算关键词与
                # token 数，**不落盘** —— 早先跳过模型摘要时就漏了这一步，导致评测链路上
                # 只剩下标题，整批实验实际测的是"标题检索"。这里必须显式写回正文。
                conn.execute(
                    "UPDATE fragments SET summary = ?, summary_model = 'raw-text', "
                    "summary_version = 1 WHERE id = ?",
                    (doc.text, fragment.id),
                )
                ctx.index_builder.build(
                    fragment_id=fragment.id,
                    topic_id=topic_id,
                    title=doc.title,
                    summary_text=doc.text,
                    entities=[],
                    keywords=[],
                    message_texts=[doc.text],
                )
            else:
                asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=4))
        except Exception as exc:
            result.failures.append({"title": doc.title, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows = conn.execute(
            "SELECT mi.id AS index_id, mi.title, mi.topic_id, f.summary "
            "FROM memory_index mi LEFT JOIN fragments f ON f.id = mi.fragment_id "
            "WHERE mi.topic_id = ?",
            (topic_id,),
        ).fetchall()
        if not rows:
            result.failures.append({"title": doc.title, "error": "封块后没有产生记忆索引"})
            continue
        for row in rows:
            text = " ".join([row["summary"] or "", row["title"] or ""]).strip()
            result.memories.append(
                Memory(
                    id=row["index_id"],
                    text=text,
                    topic_id=row["topic_id"],
                    kind="knowledge",
                    age_days=0.0,
                )
            )
        result.topics.append(Topic(id=topic_id, title=doc.title[:32], keywords=()))
    emit_entities(conn, result)
    return result


def emit_entities(conn, result: StoreResult) -> None:
    rows = conn.execute(
        "SELECT id, name, aliases, summary FROM entity_cards WHERE state = 'active'"
    ).fetchall()
    for row in rows:
        result.entities.append(
            EntityCard(
                id=row["id"],
                name=row["name"],
                aliases=tuple(json.loads(row["aliases"] or "[]")),
                summary=row["summary"] or "",
            )
        )


QUESTION_PROMPT = """你是测试集生成器。下面是若干条"记忆摘要"，每条有 id。
请为每条生成 3 个中文提问，模拟用户日后回忆这件事时会怎么问：

- literal：几乎照抄摘要里的说法；
- partial：保留一部分原词，换掉另一部分；
- rewrite：完全换一种说法，不出现摘要里的原词（但语义仍指向这条记忆）。

**必须用简体中文（不要粤语、不要繁体）**，语气像普通话用户在日常聊天里提问。
只输出 JSON，不要解释。格式：
{{"items": [{{"id": "<记忆id>", "literal": "...", "partial": "...", "rewrite": "..."}}]}}

记忆列表：
{payload}"""


def generate_questions(
    store: StoreResult,
    *,
    adapter: Any | None = None,
    guard: BudgetGuard | None = None,
    batch: int = 8,
    cache_path: Path | None = None,
    cache_only: bool = False,
) -> dict[str, dict[str, str]]:
    """为每条真实记忆生成三类提问。

    缓存按**记忆正文的哈希**而不是 index id：重建记忆库后 id 会变，但内容没变，
    这时不该再花一次模型调用。
    """
    cache_path = cache_path or (DEFAULT_CACHE / QUESTIONS_CACHE)
    keyed = {row["key"]: row for row in read_jsonl(cache_path) if row.get("key")}
    by_key = {_doc_key(m.text): m for m in store.memories}
    out: dict[str, dict[str, str]] = {}
    for key, row in keyed.items():
        memory = by_key.get(key)
        if memory is not None:
            out[memory.id] = {
                k: row[k] for k in ("literal", "partial", "rewrite") if k in row
            }
    pending = [m for m in store.memories if m.id not in out]
    if not pending:
        return out
    adapter = adapter or store.adapter
    if adapter is None or cache_only:
        return out
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        payload = "\n".join(f"- id={m.id} 摘要：{m.text[:400]}" for m in chunk)
        data = ask_json(adapter, QUESTION_PROMPT.format(payload=payload))
        if not data:
            continue
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if item["id"] not in {m.id for m in chunk}:
                continue
            out[item["id"]] = {
                "literal": str(item.get("literal") or ""),
                "partial": str(item.get("partial") or ""),
                "rewrite": str(item.get("rewrite") or ""),
            }
        text_of = {m.id: m.text for m in chunk}
        write_jsonl(
            cache_path,
            [
                {"key": _doc_key(text_of.get(k, "") or by_id_text(store, k)), "id": k, **v}
                for k, v in out.items()
            ],
        )
    return out


def _doc_key(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def by_id_text(store: StoreResult, memory_id: str) -> str:
    for memory in store.memories:
        if memory.id == memory_id:
            return memory.text
    return ""


def load_store(data_dir: Path) -> StoreResult:
    """从已有的实验库里把记忆/话题/实体读回来（用于跑臂，不重建）。"""
    import sqlite3

    from agent.storage.migrate import apply_migrations

    db = Path(data_dir) / "app.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    result = StoreResult(data_dir=Path(data_dir))
    rows = conn.execute(
        "SELECT mi.id AS index_id, mi.title, mi.topic_id, f.summary "
        "FROM memory_index mi LEFT JOIN fragments f ON f.id = mi.fragment_id"
    ).fetchall()
    for row in rows:
        result.memories.append(
            Memory(
                id=row["index_id"],
                text=" ".join([row["summary"] or "", row["title"] or ""]).strip(),
                topic_id=row["topic_id"],
                kind="knowledge",
                age_days=0.0,
            )
        )
    # 话题关键词按产品口径来：取该话题下各条记忆索引里的关键词；索引里没有就退回标题分词 ——
    # 否则规则臂拿不到任何可比对的词，会被白白判死（这正是第一次跑出 A=0.0 的原因）。
    from agent.selector.tokenize import tokenize

    per_topic: dict[str, list[str]] = {}
    for row in conn.execute("SELECT topic_id, keywords FROM memory_index"):
        per_topic.setdefault(row["topic_id"], []).extend(json.loads(row["keywords"] or "[]"))
    for row in conn.execute("SELECT id, name FROM nodes WHERE type = 'topic'"):
        words = list(dict.fromkeys(per_topic.get(row["id"], [])))[:8]
        result.topics.append(
            Topic(
                id=row["id"],
                title=row["name"],
                keywords=tuple(words) or tuple(tokenize(row["name"] or "")[:6]),
            )
        )
    for row in conn.execute("SELECT id, name, aliases, summary FROM entity_cards"):
        result.entities.append(
            EntityCard(
                id=row["id"],
                name=row["name"],
                aliases=tuple(json.loads(row["aliases"] or "[]")),
                summary=row["summary"] or "",
            )
        )
    conn.close()
    return result


def assemble_corpus(
    store: StoreResult,
    questions: dict[str, dict[str, str]],
    *,
    n_queries: int | None = None,
    cases_path: Path | None = None,
) -> StressCorpus:
    """把真实记忆与生成的问题装配成既有 `StressCorpus`，后续三臂直接复用。"""
    from agent.eval.stress_corpus import _overlap_ratio, _tier_of

    generated = load_cases(cases_path) if cases_path else {}
    by_id = {m.id: m for m in store.memories}
    cases: list[RecallCase] = []
    kinds = ("literal", "partial", "rewrite")
    index = 0
    for memory in store.memories:
        item = questions.get(memory.id) or {}
        for kind in kinds:
            query = (item.get(kind) or "").strip()
            if not query:
                continue
            overlap = _overlap_ratio(query, memory.text)
            cases.append(
                RecallCase(
                    id=f"rq_{index:05d}",
            # 场景标签不再轮流分配：真实语料里没有构造事实更新/近期噪声等场景，
            # 就如实按"提问类型"标注，避免给数字安上不成立的解释。
            category=f"q_{kind}",
                    query=query,
                    expected=(memory.id,),
                    stale=(),
                    keyword_answerable=overlap > 0,
                    overlap=round(overlap, 4),
                    tier=_tier_of(overlap),
                )
            )
            index += 1
    if n_queries:
        cases = cases[:n_queries]
    entity_cases = [
        EntityCase(id=f"en_{i:05d}", query=f"{card.name}负责什么？", expected_card_id=card.id)
        for i, card in enumerate(store.entities)
    ]
    topic_cases = bind_topic_cases(store, generated)
    dedup_cases = generated.get("dedup") or []
    return StressCorpus(
        seed=0,
        topics=list(store.topics),
        memories=list(by_id.values()),
        entities=list(store.entities),
        tools=[],
        recall_cases=cases,
        topic_cases=topic_cases,
        entity_cases=entity_cases,
        tool_cases=[],
        dedup_cases=dedup_cases,
    )


def load_cases(path: Path | None) -> dict:
    """读回生成好的用例（话题 / 去重），没有就返回空。"""
    if path is None or not Path(path).exists():
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list] = {}
    if raw.get("topic"):
        out["topic"] = [
            TopicCase(
                id=r["id"],
                category=r["category"],
                message=r["message"],
                current_topic_id=r.get("current_topic_id"),
                expected_topic_id=r.get("expected_topic_id"),
                expected_mode=r["expected_mode"],
            )
            for r in raw["topic"]
        ]
    if raw.get("dedup"):
        out["dedup"] = [
            DedupCase(
                id=r["id"],
                candidate_name=r["candidate_name"],
                expected_duplicate=bool(r["expected_duplicate"]),
                duplicate_of=r.get("duplicate_of"),
            )
            for r in raw["dedup"]
        ]
    return out


def bind_topic_cases(store: StoreResult, generated: dict) -> list[TopicCase]:
    """把话题用例绑到**当前**记忆库上。

    踩过的坑：`real_cases.json` 是在旧的记忆库上生成的，里面记的 `current_topic_id`
    是那批节点的 id；重建库之后 id 全变，用例全部悬空（`current_topic_id=None`），
    而 in_topic 这类在定义上就不可能判对。所以这里逐条校验 id 是否存在，
    对不上的直接丢弃；若整批都悬空，就退回按标题构造的、必然绑得上的用例。
    """
    known = {t.id for t in store.topics}
    bound: list[TopicCase] = []
    for row in generated.get("topic") or []:
        # 用例可能是 TopicCase 对象（走 load_cases），也可能是字典（直接手写），两种都要认。
        current = row.get("current_topic_id") if isinstance(row, dict) else row.current_topic_id
        expected = row.get("expected_topic_id") if isinstance(row, dict) else row.expected_topic_id
        if current is not None and current not in known:
            continue
        if expected is not None and expected not in known:
            continue
        bound.append(row if isinstance(row, TopicCase) else TopicCase(**row))
    if bound:
        return bound
    return [
        TopicCase(
            id=f"tp_{i:05d}",
            category="in_topic",
            message=f"继续 {topic.title} 这个话题",
            current_topic_id=topic.id,
            expected_topic_id=topic.id,
            expected_mode="in_topic",
        )
        for i, topic in enumerate(store.topics)
    ]


TOPIC_CASE_PROMPT = """你是测试集生成器。下面是若干真实话题（id + 标题），都是某位用户和他的助手长期讨论过的东西。
请为每个话题写两条**普通话自然口语**的消息：

- continue：用户想继续聊这个话题时会怎么说（不要直接抄标题，要像日常说话）；
- new：与这些话题都无关、明显开启新方向的另一条消息（每条都要不一样）。

必须用简体中文。只输出 JSON，不要解释。格式：
{{"items": [{{"id": "<话题id>", "continue": "...", "new": "..."}}]}}

话题列表：
{payload}"""

DEDUP_CASE_PROMPT = """你是测试集生成器。下面是若干真实话题标题。
请为每个标题生成两个**候选新话题名**：

- duplicate：换一种说法表达**同一个**话题（应当被判为重复）；
- novel：一个看起来相似、但实际是完全不同方向的新话题（不该被判为重复）。

必须用简体中文，长度像真实话题名。只输出 JSON。格式：
{{"items": [{{"id": "<话题id>", "duplicate": "...", "novel": "..."}}]}}

话题列表：
{payload}"""


def generate_cases(
    store: StoreResult,
    *,
    adapter: Any,
    batch: int = 10,
    cache_path: Path | None = None,
    limit: int | None = None,
) -> dict:
    """用真实模型造话题与去重用例；结果落盘，重跑不再计费。"""
    path = Path(cache_path) if cache_path else (DEFAULT_CACHE / CASES_CACHE)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    topics = store.topics[:limit] if limit else store.topics
    payload_rows = generate_case_rows(topics, adapter, batch)
    chosen = [t for t in topics if t.id in payload_rows]
    topic_cases: list[dict] = []
    for i, topic in enumerate(chosen):
        row = payload_rows[topic.id]
        other = chosen[(i + 7) % len(chosen)] if len(chosen) > 1 else topic
        topic_cases.append(
            {
                "id": f"tp_{i:05d}",
                "category": "in_topic",
                "message": row["continue"],
                "current_topic_id": topic.id,
                "expected_topic_id": topic.id,
                "expected_mode": "in_topic",
            }
        )
        topic_cases.append(
            {
                "id": f"ts_{i:05d}",
                "category": "switch",
                "message": row["continue"],
                "current_topic_id": other.id,
                "expected_topic_id": topic.id,
                "expected_mode": "switch",
            }
        )
        topic_cases.append(
            {
                "id": f"tn_{i:05d}",
                "category": "new_topic",
                "message": row["new"],
                "current_topic_id": topic.id,
                "expected_topic_id": None,
                "expected_mode": "new_topic",
            }
        )
    dedup_cases: list[dict] = []
    for i, topic in enumerate(chosen):
        row = payload_rows[topic.id]
        if not (row.get("duplicate") and row.get("novel")):
            continue
        dedup_cases.append(
            {
                "id": f"dd_{i:05d}",
                "candidate_name": row["duplicate"],
                "expected_duplicate": True,
                "duplicate_of": topic.id,
            }
        )
        dedup_cases.append(
            {
                "id": f"dn_{i:05d}",
                "candidate_name": row["novel"],
                "expected_duplicate": False,
                "duplicate_of": None,
            }
        )
    payload = {"topic": topic_cases, "dedup": dedup_cases}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def generate_case_rows(topics: list[Topic], adapter: Any, batch: int) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for start in range(0, len(topics), batch):
        chunk = topics[start : start + batch]
        listed = "\n".join(f"- id={t.id} 标题：{t.title}" for t in chunk)
        data = ask_json(adapter, TOPIC_CASE_PROMPT.format(payload=listed))
        if isinstance(data, dict):
            for item in data.get("items") or []:
                if isinstance(item, dict) and item.get("continue"):
                    rows[str(item.get("id"))] = {
                        "continue": str(item.get("continue")),
                        "new": str(item.get("new") or ""),
                        "duplicate": "",
                        "novel": "",
                    }
        data2 = ask_json(adapter, DEDUP_CASE_PROMPT.format(payload=listed))
        if isinstance(data2, dict):
            for item in data2.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id"))
                if key in rows and item.get("duplicate") and item.get("novel"):
                    rows[key]["duplicate"] = str(item["duplicate"])
                    rows[key]["novel"] = str(item["novel"])
    return {k: v for k, v in rows.items() if v["continue"] and v["new"]}


def _api_key() -> str:
    from agent.eval.jev_client import resolve_api_key

    key = resolve_api_key()
    if not key:
        raise SystemExit("缺少 OPENROUTER_API_KEY（User 作用域或环境变量）")
    return key


def build_standalone_adapter(data_dir: Path):
    """在实验库上单独取一个产品适配器（凭据在建库时已复制过来）。"""
    async def _build():
        from agent.api.bus import EventBus
        from agent.config import Settings
        from agent.services.app import AppContext
        from agent.storage.db import connect

        os.environ["QIO_DATA_DIR"] = str(data_dir)
        settings = Settings(data_dir=data_dir)
        conn = connect(settings.db_path)
        ctx = AppContext(settings, conn, EventBus())
        return await ctx.build_adapter()

    return _build()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实环境数据：建库 → 生成提问 → 装配语料")
    parser.add_argument(
        "--step",
        choices=("docs", "store", "questions", "cases", "corpus", "arms"),
        default="corpus",
    )
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--data-dir", default=str(DEFAULT_CACHE / "qio-real"))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default="stress_real.json")
    parser.add_argument("--max-usd", type=float, default=1.0)
    parser.add_argument("--k", type=int, default=5, help="召回评测的 top-k")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cache_dir = Path(args.cache_dir)
    guard = BudgetGuard(max_usd=args.max_usd, stop_at=args.max_usd * 0.9)
    if args.step == "docs":
        docs = load_wiki_docs(args.n, cache_dir=cache_dir)
        print(json.dumps({"docs": len(docs), **shard_manifest(cache_dir)}, ensure_ascii=False))
        return 0
    if args.step == "arms":
        from agent.eval.experiment_log import aggregate, aggregate_by, load_cases, log_case, start_run
        from agent.eval.stress_arms import LocalEmbeddingArm, RulesArm

        store = load_store(Path(args.data_dir))
        questions = generate_questions(
            store, cache_path=cache_dir / QUESTIONS_CACHE, cache_only=True
        )
        corpus = assemble_corpus(
            store, questions, cases_path=cache_dir / CASES_CACHE
        )
        guard = BudgetGuard(max_usd=args.max_usd, stop_at=args.max_usd * 0.9)
        arms = [RulesArm(corpus), LocalEmbeddingArm(corpus)]
        started = time.perf_counter()
        run = start_run(
            name=f"real-arms-{len(corpus.memories)}",
            config={
                "data_dir": str(args.data_dir),
                "k": args.k,
                "memories": len(corpus.memories),
                "topics": len(corpus.topics),
                "recall_cases": len(corpus.recall_cases),
                "memory_text": "fragments.summary(raw text) + title",
            },
        )
        for arm in arms:
            ranked, latencies = arm.recall(corpus.recall_cases, k=args.k)
            for case, ids, ms in zip(corpus.recall_cases, ranked, latencies):
                log_case(
                    run, job="recall", case_id=case.id, query=case.query,
                    gold=list(case.expected), ranked=ids, latency_ms=ms,
                    extra={"arm": arm.name, "tier": case.tier, "category": case.category},
                )
            topic_rows, topic_lat = arm.topic(corpus.topic_cases)
            for case, row, ms in zip(corpus.topic_cases, topic_rows, topic_lat):
                log_case(
                    run, job="topic", case_id=case.id, query=case.message,
                    gold=[case.expected_topic_id or "__new__"],
                    ranked=[row.get("predicted_topic") or "__new__"],
                    predicted=row.get("predicted"), latency_ms=ms,
                    extra={
                        "arm": arm.name,
                        "expected_mode": case.expected_mode,
                        "predicted_mode": row.get("predicted"),
                        "mode_correct": row.get("predicted") == case.expected_mode,
                    },
                )
            for job, cases, runner in (
                ("entity", corpus.entity_cases, arm.entity),
                ("dedup", corpus.dedup_cases, arm.dedup),
                ("tool", corpus.tool_cases, arm.tool),
            ):
                if not cases:
                    continue
                rows, lat = runner(cases)
                for case, row, ms in zip(cases, rows, lat):
                    gold = [r for r in (getattr(case, "expected_card_id", None), getattr(case, "expected_tool", None), getattr(case, "duplicate_of", None)) if r]
                    if job == "dedup":
                        gold = ["dup" if case.expected_duplicate else "new"]
                    predicted = row.get("predicted")
                    ranked = row.get("ranked") or ([predicted] if predicted else [])
                    if job == "dedup":
                        ranked = ["dup" if predicted else "new"]
                    log_case(
                        run, job=job, case_id=case.id,
                        query=getattr(case, "query", getattr(case, "candidate_name", "")),
                        gold=gold or ["?"], ranked=ranked, predicted=str(predicted), latency_ms=ms,
                        extra={"arm": arm.name},
                    )
        rows = load_cases(run)
        payload: dict[str, Any] = {}
        for arm in arms:
            arm_rows = [r for r in rows if r.get("extra", {}).get("arm") == arm.name]
            recall_rows = [r for r in arm_rows if r["job"] == "recall"]
            payload[arm.name] = {
                "recall": aggregate(recall_rows, job="recall", k=args.k),
                "recall_by_tier": aggregate_by(recall_rows, "tier"),
                "topic": aggregate([r for r in arm_rows if r["job"] == "topic"], job="topic", k=1),
                "entity": aggregate([r for r in arm_rows if r["job"] == "entity"], job="entity", k=1),
                "tool": aggregate([r for r in arm_rows if r["job"] == "tool"], job="tool", k=args.k),
                "dedup": aggregate([r for r in arm_rows if r["job"] == "dedup"], job="dedup", k=1),
                "topic_mode_accuracy": round(
                    sum(1 for r in arm_rows if r["job"] == "topic" and r["extra"].get("mode_correct")) /
                    max(1, sum(1 for r in arm_rows if r["job"] == "topic")), 4
                ),
            }
        payload["_run"] = {"run_id": run.run_id, "dir": str(run.directory)}
        payload["_corpus"] = {
            "memories": len(corpus.memories),
            "topics": len(corpus.topics),
            "entities": len(corpus.entities),
            "recall_cases": len(corpus.recall_cases),
            "questions_cached": len(questions),
            "wall_s": round(time.perf_counter() - started, 1),
            "budget": guard.summary(),
            "data_dir": str(args.data_dir),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        print(text)
        out = Path(args.out)
        if not out.is_absolute():
            out = Path(__file__).resolve().parents[3] / "evals" / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote → {out}", file=sys.stderr)
        return 0
    if args.step == "cases":
        store = load_store(Path(args.data_dir))
        if store.adapter is None:
            store.adapter = asyncio.run(build_standalone_adapter(Path(args.data_dir)))
        payload_cases = generate_cases(
            store, adapter=store.adapter, cache_path=cache_dir / CASES_CACHE, limit=args.n
        )
        print(
            json.dumps(
                {
                    "topic_cases": len(payload_cases.get("topic", [])),
                    "dedup_cases": len(payload_cases.get("dedup", [])),
                    "adapter": getattr(store.adapter, "model", ""),
                },
                ensure_ascii=False,
            )
        )
        return 0
    docs = load_wiki_docs(args.n, cache_dir=cache_dir)
    started = time.perf_counter()
    store = build_store(docs, data_dir=Path(args.data_dir))
    questions = generate_questions(
        store,
        adapter=store.adapter,
        guard=guard,
        batch=args.batch,
        cache_path=cache_dir / QUESTIONS_CACHE,
    )
    corpus = assemble_corpus(store, questions, n_queries=None)
    payload = {
        "docs": len(docs),
        "memories": len(store.memories),
        "topics": len(store.topics),
        "entities": len(store.entities),
        "recall_cases": len(corpus.recall_cases),
        "questions": len(questions),
        "failures": store.failures[:10],
        "adapter": {"model": store.adapter_model},
        "budget": guard.summary(),
        "wall_s": round(time.perf_counter() - started, 1),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[3] / "evals" / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
