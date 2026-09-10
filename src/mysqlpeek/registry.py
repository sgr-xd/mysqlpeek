"""Instance registry.

Holds one lazily-created connection and policy engine per configured instance. Lazy
matters: an unreachable staging box must not stop the server from starting and serving
production queries.

Instance selection is an explicit per-call argument, never stored state. A
`use_instance` tool would be tidier to call, but it leaves the active instance invisible
to the model across a long conversation — and reading the wrong environment is exactly
the mistake this tool should make impossible.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .config import AppConfig, InstanceConfig
from .connection import Connection
from .policy import PolicyEngine


class UnknownInstance(KeyError):
    """A caller named an instance that is not configured."""

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        super().__init__(name)

    def __str__(self) -> str:
        return f"unknown instance {self.name!r}; configured instances: {', '.join(self.known)}"


@dataclass(frozen=True)
class InstanceHandle:
    config: InstanceConfig
    connection: Connection
    policy: PolicyEngine

    @property
    def name(self) -> str:
        return self.config.name


class InstanceRegistry:
    def __init__(self, app: AppConfig) -> None:
        self._app = app
        self._handles: dict[str, InstanceHandle] = {}
        self._lock = threading.Lock()

    @property
    def names(self) -> list[str]:
        return sorted(self._app.instances)

    @property
    def default_name(self) -> str:
        return self._app.default_instance

    def config(self, name: str) -> InstanceConfig:
        try:
            return self._app.instances[name]
        except KeyError:
            raise UnknownInstance(name, self.names) from None

    def get(self, name: str | None = None) -> InstanceHandle:
        """Resolve an instance name to its handle, creating the connection on first use."""
        resolved = (name or self._app.default_instance).strip()
        config = self.config(resolved)

        handle = self._handles.get(resolved)
        if handle is not None:
            return handle

        with self._lock:
            handle = self._handles.get(resolved)
            if handle is None:
                handle = InstanceHandle(
                    config=config,
                    connection=Connection(config),
                    policy=PolicyEngine(config.limits),
                )
                self._handles[resolved] = handle
        return handle

    def describe_all(self) -> list[dict[str, object]]:
        out = []
        for name in self.names:
            entry = self._app.instances[name].describe()
            entry["is_default"] = name == self._app.default_instance
            entry["connected"] = name in self._handles
            out.append(entry)
        return out

    def close_all(self) -> None:
        for handle in self._handles.values():
            handle.connection.close()
        self._handles.clear()
