"""R18 §2.1：轮次受理依据快照的共享读取点（只读、受理这一刻一次性读取）。

「/api/turns」「/api/turns/{turn_id}/resend」与互动板提交
（interactive_store.submit 内的 accept_turn）都必须使用**同一份**受理快照，而不是各自
复制一份读取逻辑：

* `topic_id` / `fragment_id` —— 受理那一刻的真实话题与片段；
* `intent_id` / `intent_version` —— 提交这一刻待落实的历史接续选择（没有则 None）；
* `nav_version` / `position_version` —— 两类版本（位置写入的 CAS 依据；排队/执行期间
  用户再导航后，旧轮次不得用事后重读的「当前最新位置」回填依据）。

依据的键集由 R17 契约 §2.1 冻结：不得新增键、不得改变 TurnManager 签名。缺省/None
表示旧调用方（轮内仍按旧路径临时读当前位置）。
"""
from __future__ import annotations


def _position_snapshot(ctx) -> dict:
    """完整位置状态 wire 对象（与 `api.server._position_snapshot` 同一读取口径）。"""
    service = getattr(ctx, "position_state", None)
    if service is None:
        # 迁移未就绪/旧测试替身：如实走同一读取函数，不在这里拼接第二份事实。
        from agent.services.position_state import PositionStateService

        service = PositionStateService(ctx.conn)
    return service.read()


def submission_basis(ctx) -> dict:
    """轮次受理依据快照：调用方必须在受理那一刻读一次并随轮次冻结，禁止事后重读。"""
    position = _position_snapshot(ctx)
    pending = ctx.bindings.peek_intent()
    return {
        "topic_id": position.get("topic_id"),
        "fragment_id": position.get("fragment_id"),
        "intent_id": pending.intent_id if pending is not None else None,
        "intent_version": pending.version if pending is not None else None,
        "nav_version": int(position.get("nav_version") or 0),
        "position_version": int(position.get("position_version") or 0),
    }
