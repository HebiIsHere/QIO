"""轻量服务注册表（依赖注入，attach 模式）。

现有工具构造保持不变；新工具可用 `inject = ["retriever", ...]` 声明依赖，
ToolRegistry.register 时会自动把已注册的服务 setattr 到工具实例上。
放在 agent.tools 下以避免与 agent.core 包初始化形成循环导入。
"""

from __future__ import annotations

from typing import Any


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, Any] = {}

    def register(self, name: str, service: Any) -> None:
        self._services[name] = service

    def resolve(self, name: str) -> Any:
        return self._services.get(name)

    def attach(self, tool: Any) -> None:
        """把工具声明的服务注入到实例属性（缺省不报错）。"""
        inject = getattr(tool, "inject", None) or []
        if not inject:
            return
        for name in inject:
            service = self._services.get(name)
            if service is not None:
                setattr(tool, name, service)

    def names(self) -> list[str]:
        return sorted(self._services)
