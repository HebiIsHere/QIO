"""Tool proposal generation by the MAIN model."""

from __future__ import annotations

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.tools.spec import TOOL_PROPOSAL_PROMPT, ToolProposal, validate_tool_proposal


class ToolCreator:
    def __init__(self, adapter: BaseAdapter) -> None:
        self.adapter = adapter

    async def propose(
        self,
        user_request: str,
        context: str | None = None,
        *,
        temperature: float = 0.3,
    ) -> tuple[ToolProposal | None, str | None]:
        """Ask the main model for an explained tool proposal."""
        prompt = (
            TOOL_PROPOSAL_PROMPT
            + "\n\nUser request:\n"
            + user_request
        )
        if context:
            prompt += "\n\nRelevant context:\n" + context[:4000]
        try:
            completion = await self.adapter.complete(
                [ChatMessage(role="user", content=prompt)],
                tools=[],
                temperature=temperature,
            )
        except Exception as exc:
            return None, f"model call failed: {exc}"
        return validate_tool_proposal(completion.message.content or "")