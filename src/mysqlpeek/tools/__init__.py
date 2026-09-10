"""MCP tool families. Each module exposes `register(...)`."""

from mcp.types import ToolAnnotations

# Every mysqlpeek tool is a read. Declaring it in the protocol lets a client show the
# difference, and lets a cautious host auto-approve these without a prompt.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

__all__ = ["READ_ONLY"]
