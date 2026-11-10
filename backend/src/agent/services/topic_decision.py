# -*- coding: utf-8 -*-
"""统一话题判断：本轮证据 + 集中提示词（本轮契约 §2.3）。

设计口径（docs/topic-application-contract-20261110.md §1/§2.3）：

* 相似度只是**证据**。预测器/分类器不再单独驱动「切换建议」，
  也不发布可执行事件；是否延续 / 换到已有话题 / 新建话题 / 暂时无法判断，
  唯一判断入口是本轮主模型（受控工具调用）。
* 本模块只做**纯数据 + 纯渲染**：不写库、不改状态、不发事件、不调用模型。
  拒绝范围、当前片段等业务状态由调用方读好后传进来（A 只读）。

四个入口：

    build_candidates(prediction, topics_service, limit=5)
    build_topic_decision_state(app, ctx, topic_id, prediction, refuse_scope=None)
    render_topic_decision_note(state, candidates=None)
    normalize_refused_attempts(refuse_scope, topic_id=None, limit=None)

渲染正文的策略措辞只在 prompts.PROMPTS["TOPIC_DECISION_POLICY"] 维护；
本模块负责把「本轮事实证据」按固定格式拼在策略后面，供 orchestrator 作为
system 小节注入（Lead 的接线：_Plan.topic_decision_note → extra_sections）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent import prompts
from agent.services.params import TOPIC_DECISION as _POLICY


@dataclass(frozen=True)
class TopicCandidate:
    """一个已有话题候选（证据）。

    score 只用于排序，不渲染成数字：模型不该把相似度当成判据，也不该把它
    当正式回答内容告诉用户。名称/摘要来自 TopicService 的真实数据。
    """

    topic_id: str
    name: str
    score: float = 0.0
    summary: str = ""


@dataclass
class TopicDiscussionState:
    """本轮讨论状态（只含真实来源的证据，不含模型猜测）。

    * scope_summary —— 当前片段摘要或话题指纹摘要（真实片段数据；没有就空）；
    * current_goal  —— 当前讨论目标。**不得逐字复制本轮用户原文**（P7 不变量：
      当前消息在模型输入里只能出现一次）；优先程序字段 ctx.topic_current_goal，
      否则用当前片段标题，都没有就空；
    * confirmed_facts —— **只有用户明确确认过的事实**。程序没有这样的来源时
      保持为空，绝不拿摘要/模型猜测填充（调用方可提供 ctx.topic_confirmed_facts）；
    * open_items    —— 尚未解决事项。同样只来自程序来源（ctx.topic_open_items），
      没有就空，不从自然语言里猜；
    * refused_attempts —— 用户此前对换题申请的选择（只来自 refuse_scope）；
    * candidates    —— 已有话题候选（build_candidates 的排序结果）。

    这里**没有** explicit_hint / explicit_target / explicit_create_name 之类的
    「程序已识别明确导航」字段（P1-01 删除）：进入主循环前的程序语义识别已退役，
    是否换题只来自本轮主模型在工具参数 intent 里声明的值。
    """

    topic_id: str
    topic_name: str
    scope_summary: str = ""
    current_goal: str = ""
    confirmed_facts: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)
    refused_attempts: list[dict] = field(default_factory=list)
    candidates: list[TopicCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _echoes_message(candidate: Any, message: Any) -> bool:
    """候选文本是否逐字等于/包含本轮用户原文（P7：当前消息只出现一次）。"""
    text = _string(candidate)
    source = _string(message)
    if not text:
        return False
    if not source:
        return False
    return text == source or source in text


def _clip(text: Any, limit: int) -> str:
    value = _string(text)
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit] + "…"


def _first(raw: Mapping, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001 - 只读路径，取不到就当没有
        return default


# ---------------------------------------------------------------------------
# 候选：排序 / 去重 / 上限（相似度只是证据，不设「未达门槛 = 应新建」）
# ---------------------------------------------------------------------------


def _fingerprint(topics_service: Any, topic_id: str) -> Any | None:
    """读取话题指纹（只读）；替身/旧服务没有该接口时返回 None。"""
    fingerprint = _get_attr(topics_service, "fingerprint")
    if not callable(fingerprint):
        return None
    try:
        return fingerprint(topic_id)
    except Exception:  # noqa: BLE001 - 读不到指纹不是错误，退回 id 作为名称
        return None


def _topic_meta(topics_service: Any, topic_id: str) -> tuple[str, str]:
    """(name, summary)：优先 TopicService.fingerprint，其次批量接口，最后节点名。"""
    name = topic_id
    summary = ""
    fp = _fingerprint(topics_service, topic_id)
    if fp is not None:
        title = _string(_get_attr(fp, "title"))
        if title:
            name = title
        summary = _string(_get_attr(fp, "summary_preview"))
        return name, summary
    lister = _get_attr(topics_service, "list_with_fingerprints")
    if callable(lister):
        try:
            for item in lister() or []:
                if _string(_get_attr(item, "topic_id")) == topic_id:
                    title = _string(_get_attr(item, "title"))
                    if title:
                        name = title
                    summary = _string(_get_attr(item, "summary_preview"))
                    break
        except Exception:  # noqa: BLE001
            pass
    nodes = _get_attr(topics_service, "nodes")
    getter = _get_attr(nodes, "get_topic")
    if callable(getter) and name == topic_id:
        try:
            node = getter(topic_id)
            node_name = _string(_get_attr(node, "name")) if node is not None else ""
            if node_name:
                name = node_name
        except Exception:  # noqa: BLE001
            pass
    return name, summary


def build_candidates(
    prediction: Any,
    topics_service: Any,
    limit: int | None = None,
) -> list[TopicCandidate]:
    """从预测结果构建已有话题候选（只排序/去重/截断，不判断是否该换题）。

    * 来源：prediction.scores + main_topic_id + aux_topic_ids；
    * 去重：同一 topic_id 只保留最高分的一条；
    * 排序：分数降序，同分按 topic_id 稳定排序；
    * 上限：limit（默认 params.TOPIC_DECISION.candidate_limit=5，硬上限也是它）。

    这里刻意**没有**「相似度低于门槛 ⇒ 应新建」的分支：候选为空只表示没有
    可切换的已有话题，不构成新建结论；分数高也不等于必须换题。
    """
    if prediction is None:
        return []
    cap = _POLICY.candidate_limit if limit is None else max(0, int(limit))
    cap = min(cap, _POLICY.candidate_limit)
    if cap <= 0:
        return []

    scores: dict[str, float] = {}

    def add(topic_id: Any, score: Any = 0.0) -> None:
        key = _string(topic_id)
        if not key:
            return
        try:
            value = float(score)
        except (TypeError, ValueError):
            value = 0.0
        if key not in scores or value > scores[key]:
            scores[key] = value

    raw_scores = _get_attr(prediction, "scores") or {}
    if not isinstance(raw_scores, Mapping):
        raw_scores = {}
    for topic_id, score in raw_scores.items():
        add(topic_id, score)
    # main / aux 即使没有分数（替身预测器）也要进入候选池；分数 0 排在后面。
    main_topic_id = _string(_get_attr(prediction, "main_topic_id"))
    if main_topic_id:
        add(main_topic_id, raw_scores.get(main_topic_id, 0.0))
    for aux_id in _get_attr(prediction, "aux_topic_ids") or ():
        add(aux_id, raw_scores.get(_string(aux_id), 0.0))

    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    candidates: list[TopicCandidate] = []
    for topic_id, score in ordered[:cap]:
        name, summary = _topic_meta(topics_service, topic_id)
        candidates.append(
            TopicCandidate(
                topic_id=topic_id,
                name=name or topic_id,
                score=float(score),
                summary=_clip(summary, _POLICY.candidate_summary_chars),
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# 拒绝范围（B 的 TopicDiscussionGuard 提供 dict|None；A 只读、宽兼容解析）
# ---------------------------------------------------------------------------

# 一条拒绝记录可能带的字段（只读这些，不把任意输入原样渲染给模型）。
_REFUSAL_FIELDS = (
    "action",
    "kind",
    "decision",
    "from_topic_id",
    "topic_id",
    "current_topic_id",
    "target_topic_id",
    "target_topic_name",
    "topic_name",
    "new_name",
    "name",
    "reason",
    "turn_id",
    "at",
    "created_at",
)

# 表示「用户自己选择留在当前话题」的原因代码（不展示代码本身）。
_USER_REFUSAL_REASONS = {
    "stay_current",
    "reject",
    "rejected",
    "denied",
    "declined",
    "user_rejected",
}

# 未知内部代码的形态：纯小写 ASCII 标识符（不把这类原始字符串写进证据）。
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_:.\-]*")


def _refusal_entries(refuse_scope: Any) -> list[Any]:
    """把 B 提供的拒绝范围归一化成「记录列表」。

    接受：None / list[dict] / 单条 dict / {"entries"|"refusals"|"items": [...]} /
    {key: dict} 映射。解析不出来就是空列表（不猜、不报错）。
    """
    if refuse_scope is None:
        return []
    if isinstance(refuse_scope, Mapping):
        for key in ("entries", "refusals", "items"):
            value = refuse_scope.get(key)
            if isinstance(value, (list, tuple)):
                return list(value)
        if any(field in refuse_scope for field in _REFUSAL_FIELDS):
            return [refuse_scope]
        values = list(refuse_scope.values())
        if values and all(isinstance(value, Mapping) for value in values):
            return values
        return []
    if isinstance(refuse_scope, (list, tuple)):
        return list(refuse_scope)
    return []


def _refusal_action(raw: Mapping) -> str:
    for key in ("action", "kind", "decision"):
        value = _string(raw.get(key)).lower()
        if not value:
            continue
        if "create" in value or "new" in value:
            return "create"
        if "switch" in value or "change" in value:
            return "switch"
    if _first(raw, "new_name", "name"):
        return "create"
    return "switch"


def _refusal_is_other_topic(raw: Mapping, topic_id: str | None) -> bool:
    """带**来源**话题且与当前话题不同的旧记录：属于别的讨论，不进本轮证据。

    注意：B 的 RefusedAttempt 里 topic_id/topic_name 是**目标**（申请要去的
    方向），不是来源话题；所以这里只认显式的 from/current/source 字段，
    不能拿 topic_id 当来源，否则会把正常的目标记录全部过滤掉。
    """
    if not topic_id:
        return False
    for key in ("from_topic_id", "current_topic_id", "source_topic_id"):
        value = _first(raw, key)
        if value:
            return value != str(topic_id)
    return False


def _refusal_target(raw: Mapping) -> str:
    """目标名称：优先契约/新格式字段，其次 B 的 RefusedAttempt（topic_name=目标显示名）。"""
    explicit = _first(raw, "target_topic_name", "new_name", "target_topic_id")
    if explicit:
        return explicit
    return _first(raw, "topic_name", "topic_id")


def _refusal_outcome(reason: str) -> tuple[str, str]:
    """(outcome, 模型可见原因)：outcome ∈ {user_refused, not_executed}。

    * 用户自己的选择（rejected / stay_current 等）：outcome=user_refused，不展示代码；
    * 超时 / 位置变化 / 重复申请等：outcome=not_executed，代码翻成 prompts 里的人话；
    * 未知 ASCII 内部代码：不原样写进证据，用兜底人话；
    * 其他自然语言原因：保留原文（截断），按用户选择证据追加说明。
    """
    text = _string(reason)
    if not text:
        return "user_refused", ""
    lowered = text.lower()
    if lowered in _USER_REFUSAL_REASONS:
        return "user_refused", ""
    label = prompts.TOPIC_DECISION_REFUSAL_REASON_LABELS.get(lowered)
    if label:
        return "not_executed", label
    if _CODE_PATTERN.fullmatch(lowered):
        return "not_executed", prompts.TOPIC_DECISION_REFUSAL_REASON_DEFAULT
    return "user_refused", _clip(text, _POLICY.refused_reason_chars)


def _normalize_refusal(raw: Mapping) -> dict | None:
    action = _refusal_action(raw)
    target = _refusal_target(raw)
    outcome, reason = _refusal_outcome(_string(raw.get("reason")))
    if not target and not reason:
        return None
    return {
        "action": action,
        "target": target,
        "reason": reason,
        "outcome": outcome,
    }


def normalize_refused_attempts(
    refuse_scope: Any,
    topic_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """拒绝范围 → 渲染用条目 {"action", "target", "reason"}（只读）。

    跨话题的旧记录会被忽略；同一段讨论的重复记录按原顺序保留（上限由
    params.TOPIC_DECISION.max_refused_attempts 控制）。
    """
    cap = _POLICY.max_refused_attempts if limit is None else max(0, int(limit))
    if cap <= 0:
        return []
    attempts: list[dict] = []
    for raw in _refusal_entries(refuse_scope):
        if not isinstance(raw, Mapping):
            continue
        if _refusal_is_other_topic(raw, topic_id):
            continue
        normalized = _normalize_refusal(raw)
        if normalized is None:
            continue
        attempts.append(normalized)
        if len(attempts) >= cap:
            break
    return attempts


# ---------------------------------------------------------------------------
# 讨论状态：只从真实消息 / 片段 / 操作结果取证据
# ---------------------------------------------------------------------------


def _topic_name(app: Any, topic_id: str) -> str:
    if not topic_id:
        return ""
    topics_service = _get_attr(app, "topics")
    fp = _fingerprint(topics_service, topic_id)
    if fp is not None:
        title = _string(_get_attr(fp, "title"))
        if title:
            return title
    nodes = _get_attr(topics_service, "nodes")
    getter = _get_attr(nodes, "get_topic")
    if callable(getter):
        try:
            node = getter(topic_id)
            name = _string(_get_attr(node, "name")) if node is not None else ""
            if name:
                return name
        except Exception:  # noqa: BLE001
            pass
    return topic_id


def _current_fragment(app: Any, ctx: Any, topic_id: str) -> Any | None:
    """本轮绑定片段（读不到就取该话题当前开放片段）；只读，不创建。"""
    fragments = _get_attr(app, "fragments")
    if fragments is None or not topic_id:
        return None
    getter = _get_attr(fragments, "get")
    bound_id = _string(_get_attr(ctx, "bound_fragment_id"))
    if bound_id and callable(getter):
        try:
            fragment = getter(bound_id)
            if fragment is not None and _string(_get_attr(fragment, "topic_id")) == topic_id:
                return fragment
        except Exception:  # noqa: BLE001
            pass
    opener = _get_attr(fragments, "open_fragment")
    if callable(opener):
        try:
            return opener(topic_id)
        except Exception:  # noqa: BLE001
            return None
    return None


def _fragment_title(app: Any, fragment: Any | None) -> str:
    """当前片段在 memory_index 里的标题（只读；没有就空，不拿正文代替）。"""
    if fragment is None:
        return ""
    conn = _get_attr(app, "conn")
    fragment_id = _string(_get_attr(fragment, "id"))
    if conn is None or not fragment_id:
        return ""
    execute = _get_attr(conn, "execute")
    if not callable(execute):
        return ""
    try:
        row = execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - 标题是旁路证据，读不到就空
        return ""
    if row is None:
        return ""
    try:
        return _string(row["title"])
    except Exception:  # noqa: BLE001 - sqlite3.Row / 替身形状差异
        return ""


def _current_goal(app: Any, ctx: Any, fragment: Any | None) -> str:
    """当前讨论目标：程序字段优先，其次片段标题；**绝不逐字复制本轮用户原文**。

    P7 不变量（tests/test_p7_message_uniqueness.py）要求当前用户消息在模型输入里
    只出现一次：它已经在 user 消息里，任何 system 小节再抄一遍都会变成两次。
    因此：
    * ctx.topic_current_goal（程序写入的真实目标）可用，但等于/包含本轮原文时丢弃；
    * 否则用当前开放片段的标题（来自此前真实讨论的索引），同样排除原文回显；
    * 都没有就留空 —— 宁缺毋滥，不拿本轮 message 或模型猜测填充。
    """
    message = _get_attr(ctx, "message")
    explicit = _string(_get_attr(ctx, "topic_current_goal"))
    if explicit and not _echoes_message(explicit, message):
        return _clip(explicit, _POLICY.current_goal_chars)
    title = _fragment_title(app, fragment)
    if title and not _echoes_message(title, message):
        return _clip(title, _POLICY.current_goal_chars)
    return ""


def _scope_summary(app: Any, topic_id: str, fragment: Any | None) -> str:
    """讨论范围：当前片段摘要 → 话题指纹摘要；都没有就空，不编造。"""
    if fragment is not None:
        summary = _clip(_get_attr(fragment, "summary"), _POLICY.scope_summary_chars)
        if summary:
            return summary
    fp = _fingerprint(_get_attr(app, "topics"), topic_id)
    if fp is not None:
        return _clip(_get_attr(fp, "summary_preview"), _POLICY.scope_summary_chars)
    return ""


def _program_string_list(
    ctx: Any,
    attr: str,
    *,
    limit: int,
    max_chars: int,
) -> list[str]:
    """只接受调用方（程序）显式给出的字符串列表；没有就是空，不猜。"""
    value = _get_attr(ctx, attr)
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = _clip(item, max_chars)
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def build_topic_decision_state(
    app: Any,
    ctx: Any,
    topic_id: str | None,
    prediction: Any,
    refuse_scope: Any = None,
) -> TopicDiscussionState:
    """组装本轮判断证据（全部来自真实数据；不写库、不改状态）。

    * app —— AppContext：只读 topics（指纹/节点）与 fragments（片段/消息）；
    * ctx —— TurnContext：读本轮消息、绑定片段、明确导航与程序事实字段；
    * refuse_scope —— B 的 TopicDiscussionGuard / ctx.topic_refusal_scope（dict|None）。
    """
    key = _string(topic_id)
    fragment = _current_fragment(app, ctx, key)
    return TopicDiscussionState(
        topic_id=key,
        topic_name=_topic_name(app, key),
        scope_summary=_scope_summary(app, key, fragment),
        current_goal=_current_goal(app, ctx, fragment),
        confirmed_facts=_program_string_list(
            ctx,
            "topic_confirmed_facts",
            limit=_POLICY.max_confirmed_facts,
            max_chars=160,
        ),
        open_items=_program_string_list(
            ctx,
            "topic_open_items",
            limit=_POLICY.max_open_items,
            max_chars=160,
        ),
        refused_attempts=normalize_refused_attempts(
            refuse_scope,
            topic_id=key or None,
            limit=_POLICY.max_refused_attempts,
        ),
        candidates=build_candidates(
            prediction, _get_attr(app, "topics"), limit=_POLICY.candidate_limit
        ),
    )


# ---------------------------------------------------------------------------
# 渲染：纯文本 system 小节（无 JSON / 无事件 / 无前端载荷）
# ---------------------------------------------------------------------------


def _render_refused(attempts: Sequence[Mapping]) -> list[str]:
    lines: list[str] = []
    for item in attempts:
        action = "create" if _string(item.get("action")) == "create" else "switch"
        label = (
            prompts.TOPIC_DECISION_ACTION_CREATE
            if action == "create"
            else prompts.TOPIC_DECISION_ACTION_SWITCH
        )
        target = _string(item.get("target"))
        reason = _string(item.get("reason"))
        outcome = _string(item.get("outcome")) or "user_refused"
        if outcome == "not_executed":
            shown = reason or prompts.TOPIC_DECISION_REFUSAL_REASON_DEFAULT
            if target:
                line = prompts.TOPIC_DECISION_REFUSED_NOT_EXECUTED_ITEM.format(
                    action=label, target=target, reason=shown
                )
            else:
                line = prompts.TOPIC_DECISION_REFUSED_NOT_EXECUTED_ITEM_NO_TARGET.format(
                    action=label, reason=shown
                )
        else:
            if target:
                line = prompts.TOPIC_DECISION_REFUSED_ITEM.format(
                    action=label, target=target
                )
            else:
                line = prompts.TOPIC_DECISION_REFUSED_ITEM_NO_TARGET.format(action=label)
            if reason:
                line += prompts.TOPIC_DECISION_REFUSED_REASON.format(reason=reason)
        lines.append(line)
    return lines


def _render_candidate(candidate: TopicCandidate, current_topic_id: str) -> str:
    summary = candidate.summary or prompts.TOPIC_DECISION_CANDIDATE_NO_SUMMARY
    line = prompts.TOPIC_DECISION_CANDIDATE_ITEM.format(
        name=candidate.name or candidate.topic_id,
        topic_id=candidate.topic_id,
        summary=summary,
    )
    if current_topic_id and candidate.topic_id == current_topic_id:
        line += prompts.TOPIC_DECISION_CANDIDATE_IS_CURRENT
    return line


def render_topic_decision_note(
    state: TopicDiscussionState,
    candidates: Sequence[TopicCandidate] | None = None,
) -> str:
    """渲染注入 system 的话题判断小节（纯文本，不使用【】标题）。

    策略正文引用 prompts.PROMPTS["TOPIC_DECISION_POLICY"]（唯一来源），
    本函数只追加本轮事实证据：当前话题、范围、当前目标、用户确认事实、
    未解决事项、拒绝范围、候选话题、明确导航提示。

    candidates 省略时用 state.candidates。不返回 JSON / 可执行载荷：它只是
    模型上下文文本。语义判断（是否换题 / 新建）只由本轮主模型在工具参数
    intent 里声明，这里不提供「程序已识别」的 explicit hint。
    """
    if state is None:
        return ""
    parts: list[str] = [
        prompts.TOPIC_DECISION_SECTION_TITLE,
        prompts.PROMPTS["TOPIC_DECISION_POLICY"],
        # P1-01：话题语义判断的唯一规则区块（含契约 §7 全部反例）；
        # 不在其他提示词里再写第二套同义规则。
        prompts.TOPIC_SEMANTIC_POLICY,
    ]
    evidence: list[str] = []
    if state.topic_id:
        evidence.append(
            prompts.TOPIC_DECISION_CURRENT.format(
                name=state.topic_name or state.topic_id,
                topic_id=state.topic_id,
            )
        )
    if state.scope_summary:
        evidence.append(
            prompts.TOPIC_DECISION_SCOPE.format(summary=state.scope_summary)
        )
    if state.current_goal:
        evidence.append(
            prompts.TOPIC_DECISION_GOAL.format(goal=state.current_goal)
        )
    if state.confirmed_facts:
        evidence.append(
            prompts.TOPIC_DECISION_CONFIRMED.format(
                facts="；".join(state.confirmed_facts)
            )
        )
    if state.open_items:
        evidence.append(
            prompts.TOPIC_DECISION_OPEN.format(items="；".join(state.open_items))
        )
    if state.refused_attempts:
        lines = _render_refused(state.refused_attempts)
        if lines:
            evidence.append(
                prompts.TOPIC_DECISION_REFUSED.format(attempts="；".join(lines))
            )
    chosen = list(candidates) if candidates is not None else list(state.candidates or ())
    if chosen:
        evidence.append(prompts.TOPIC_DECISION_CANDIDATES)
        evidence.extend(
            _render_candidate(candidate, state.topic_id) for candidate in chosen
        )
    else:
        evidence.append(prompts.TOPIC_DECISION_NO_CANDIDATE)
    parts.append(prompts.TOPIC_DECISION_EVIDENCE_HEADER)
    parts.extend(evidence)
    return "\n".join(part for part in parts if part and part.strip())
