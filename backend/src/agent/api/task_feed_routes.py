"""对话页 v2：左侧任务栏的权威快照 / 详情 / 确认 / 取消路由。

接口返回 camelCase（与 frontend/src/conversation/contracts.ts 一一对应）：
前端只有一个共享类型来源，不在客户端再做一层字段翻译。

安全边界：这里的「取消」只对真实支持单独取消的来源开放（目前是宿主的子任务），
其余来源如实返回不可取消；申请取消也不宣称底层执行单元已经停止。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def register_task_feed_routes(app: Any, ctx: Any) -> None:
    from agent.services import task_feed as feed_service

    @app.get("/api/tasks")
    async def list_tasks(
        view: str = "current", cursor: str | None = None, limit: int = 30
    ) -> dict:
        """任务栏权威快照（当前 / 历史 + 计数 + 分页）。

        增量事件只用于刷新；断线、重连、后端重启后必须以本接口为准，
        历史任务不会因为一次快照被重新标成运行中。
        """
        if view not in ("current", "history"):
            raise HTTPException(status_code=400, detail="view 必须是 current 或 history")
        return feed_service.build_feed(ctx, view=view, cursor=cursor, limit=limit)

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str) -> dict:
        detail = feed_service.task_detail(ctx, task_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="这个任务已经不在可读取的范围内")
        return detail

    @app.post("/api/tasks/{task_id}/ack")
    async def ack_task(task_id: str) -> dict:
        """「知道了」：只把需要处理的失败/完成项收进历史，不删除结果。"""
        if not feed_service.acknowledge(ctx, task_id):
            raise HTTPException(status_code=500, detail="没能保存这个确认状态，可以重试")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict:
        """申请取消：只有真实支持单独取消的来源才会返回 accepted=true。"""
        return feed_service.cancel_task(ctx, task_id)
