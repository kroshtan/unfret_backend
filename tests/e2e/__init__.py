"""The dev sweep (``make e2e``). The MCP client in mcp/ (its own environment) needs only solders and httpx."""

import sys
from pathlib import Path

_MCP_SRC = str(Path(__file__).resolve().parents[2] / "mcp" / "src")
if _MCP_SRC not in sys.path:
    sys.path.insert(0, _MCP_SRC)
