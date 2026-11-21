"""Minimal MCP stdio client (JSON-RPC 2.0, newline-delimited) with no dependencies.

Starts the server, performs ``initialize``, calls one tool, and shuts it down.
Sufficient for job submission and polling; ARTOS does not hold a long-lived
connection because jobs outlive any single controller process.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any


class MCPError(RuntimeError):
    pass


def _send(proc: subprocess.Popen, message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _read(proc: subprocess.Popen, want_id: int) -> dict[str, Any]:
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            raise MCPError("MCP server closed its stdout")
        line = line.strip()
        if not line or not line.startswith(b"{"):
            continue
        msg = json.loads(line)
        if msg.get("id") == want_id:
            if "error" in msg:
                raise MCPError(str(msg["error"]))
            return msg["result"]


def call_tool(command: list[str], tool: str, arguments: dict[str, Any], *, env: dict[str, str] | None = None, timeout: float = 120) -> str:
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "artos", "version": "0.2"}}})
        _read(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
        result = _read(proc, 2)
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if result.get("isError"):
            raise MCPError("; ".join(texts) or f"{tool} failed")
        return "\n".join(texts)
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
