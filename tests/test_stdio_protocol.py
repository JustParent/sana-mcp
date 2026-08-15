"""End-to-end MCP protocol test over stdio.

Spawns the server as a real subprocess and speaks JSON-RPC to it exactly as a
host does. This covers what in-process tests cannot: that stdout carries protocol
traffic and nothing else, that the server starts without credentials, and that a
failing tool call leaves the process healthy — it stays warm across many calls.

Still offline: no network, no credentials. Deliberately runs with the Sana
environment cleared, which is itself the scenario under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from test_server_smoke import EXPECTED_TOOLS

PROTOCOL_VERSION = "2024-11-05"
TIMEOUT = 60


class Server:
    """A running sana-mcp subprocess speaking JSON-RPC over stdio."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._next_id = 0

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return self._read(request_id)

    def _write(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _read(self, want_id: int) -> dict:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(f"server closed stdout before replying to id={want_id}")
            line = line.strip()
            if not line:
                continue
            # Anything non-JSON on stdout corrupts the protocol stream.
            assert line.startswith("{"), f"non-JSON on stdout: {line[:200]}"
            message = json.loads(line)
            if message.get("id") == want_id:
                return message


@pytest.fixture
def server():
    """Start the server with a cleared Sana environment and hand back a client."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SANA_")}
    env.pop("CLIENT_ID", None)
    env.pop("CLIENT_SECRET", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "sana_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    client = Server(proc)
    result = client.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    )
    client.initialize_result = result["result"]
    client.notify("notifications/initialized")

    try:
        yield client
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_server_starts_without_credentials(server):
    """A misconfigured server must still start, so tool calls can report the problem."""
    assert server.initialize_result["protocolVersion"] == PROTOCOL_VERSION
    assert server.initialize_result["serverInfo"]["name"] == "sana"


def test_tools_list_over_the_wire(server):
    tools = server.request("tools/list")["result"]["tools"]
    assert {tool["name"] for tool in tools} == EXPECTED_TOOLS

    for tool in tools:
        assert tool.get("description"), f"{tool['name']} has no description"
        assert tool["inputSchema"]["type"] == "object", f"{tool['name']} has a bad schema"


def test_tool_call_without_credentials_reports_the_fix(server):
    """The error an assistant sees must name the variable to set."""
    result = server.request(
        "tools/call", {"name": "sana_check_connection", "arguments": {}}
    )["result"]

    assert result.get("isError"), "expected an error without credentials"
    text = " ".join(block.get("text", "") for block in result["content"])
    assert "SANA_DOMAIN" in text


def test_server_survives_a_failed_tool_call(server):
    """The process is reused across many calls, so one failure must not poison it."""
    server.request("tools/call", {"name": "sana_check_connection", "arguments": {}})

    tools = server.request("tools/list")["result"]["tools"]
    assert len(tools) == len(EXPECTED_TOOLS)


def test_invalid_arguments_are_rejected_cleanly(server):
    """Validation errors travel as tool errors, not as a crashed server."""
    result = server.request(
        "tools/call",
        {"name": "sana_api_request", "arguments": {"method": "TRACE", "path": "/api/v0/users"}},
    )["result"]

    assert result.get("isError")
    text = " ".join(block.get("text", "") for block in result["content"])
    assert "method" in text

    # Still serving.
    assert server.request("tools/list")["result"]["tools"]
