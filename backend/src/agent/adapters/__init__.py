"""Model adapter layer: native / text / unsupported three-state probing."""

from agent.adapters.base import (
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    ToolCall,
    ToolSpec,
)
from agent.adapters.native import NativeAdapter
from agent.adapters.probe import ProbeCache, ProbeResult, probe_adapter
from agent.adapters.text import TextAdapter

__all__ = [
    "AdapterMode",
    "BaseAdapter",
    "ChatMessage",
    "Completion",
    "ToolCall",
    "ToolSpec",
    "NativeAdapter",
    "TextAdapter",
    "ProbeCache",
    "ProbeResult",
    "probe_adapter",
]