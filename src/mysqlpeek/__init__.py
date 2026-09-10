"""mysqlpeek — a read-only MySQL / MariaDB MCP server for one or many instances."""

from .config import AppConfig, ConfigError, InstanceConfig, Limits
from .connection import Connection, QueryError, QueryResult
from .registry import InstanceHandle, InstanceRegistry, UnknownInstance

__all__ = [
    "AppConfig",
    "ConfigError",
    "Connection",
    "InstanceConfig",
    "InstanceHandle",
    "InstanceRegistry",
    "Limits",
    "QueryError",
    "QueryResult",
    "UnknownInstance",
]
