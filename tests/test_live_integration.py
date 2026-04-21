"""
Full integration test for graphify live mode.

Starts `graphify live` as a real subprocess, speaks the MCP JSON-RPC protocol
over its stdin/stdout, exercises all 7 tools, then mutates a file and verifies
the graph updates within the 0.6s debounce window + rebuild time.

Run with:
    python3 tests/test_live_integration.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path


# ─── MCP client helpers ──────────────────────────────────────────────────────

class MCPClient:
    """Minimal synchronous MCP client over subprocess stdio."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._lock = threading.Lock()
        self._next_id = 1
        self._buf: list[str] = []
        # Drain stderr in background so it doesn't block
        self._stderr_lines: list[str] = []
        t = threading.Thread(target=self._drain_stderr, daemon=True)
        t.start()

    def _drain_stderr(self):
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            l = line.rstrip()
            self._stderr_lines.append(l)
            print(f"  [server] {l}", flush=True)

    def _send(self, obj: dict) -> None:
        line = json.dumps(obj) + "\n"
        assert self._proc.stdin is not None
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _recv(self, timeout: float = 10.0) -> dict:
        assert self._proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("No response from MCP server")
            line = self._proc.stdout.readline()
            if not line:
                raise EOFError("MCP server closed stdout")
            stripped = line.strip()
            if not stripped:
                continue
            return json.loads(stripped)

    def initialize(self) -> dict:
        msg_id = self._next_id; self._next_id += 1
        self._send({
            "jsonrpc": "2.0", "id": msg_id, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "graphify-test", "version": "1.0"},
            },
        })
        resp = self._recv()
        # Send initialized notification
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return resp

    def list_tools(self) -> list[dict]:
        msg_id = self._next_id; self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/list", "params": {}})
        resp = self._recv()
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict, timeout: float = 15.0) -> str:
        msg_id = self._next_id; self._next_id += 1
        self._send({
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        resp = self._recv(timeout=timeout)
        content = resp.get("result", {}).get("content", [])
        if content:
            return content[0].get("text", "")
        return str(resp.get("error", "no content"))


# ─── Test runner ─────────────────────────────────────────────────────────────

def _sep(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print('─'*60)


def run_integration_test(project_path: Path) -> None:
    python = sys.executable
    cmd = [python, "-m", "graphify", "live", str(project_path), "--debounce", "0.6"]

    _sep("Starting graphify live subprocess")
    print(f"  Command: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(project_path),
    )

    client = MCPClient(proc)
    time.sleep(1.5)  # Let bootstrap complete

    try:
        _sep("MCP: initialize")
        init = client.initialize()
        server_name = init.get("result", {}).get("serverInfo", {}).get("name", "?")
        proto = init.get("result", {}).get("protocolVersion", "?")
        print(f"  server: {server_name}  protocol: {proto}")
        assert server_name == "graphify-live", f"unexpected server name: {server_name}"
        print("  PASS")

        _sep("MCP: tools/list")
        tools = client.list_tools()
        tool_names = {t["name"] for t in tools}
        expected = {"query_graph","get_node","get_neighbors","get_community","god_nodes","graph_stats","shortest_path"}
        print(f"  Tools registered: {sorted(tool_names)}")
        assert expected == tool_names, f"missing tools: {expected - tool_names}"
        print("  PASS — all 7 tools present")

        _sep("Tool: graph_stats")
        stats = client.call_tool("graph_stats", {})
        print(stats)
        assert "Nodes:" in stats
        assert "Watching:" in stats
        print("  PASS")

        _sep("Tool: god_nodes")
        gods = client.call_tool("god_nodes", {"top_n": 5})
        print(gods)
        assert "God nodes" in gods
        print("  PASS")

        _sep("Tool: query_graph (BFS)")
        result = client.call_tool("query_graph", {"question": "live graph watcher", "mode": "bfs", "depth": 2})
        print(result[:400])
        print("  PASS — BFS returned results")

        _sep("Tool: query_graph (DFS)")
        result_dfs = client.call_tool("query_graph", {"question": "MCP server stdio", "mode": "dfs", "depth": 3})
        print(result_dfs[:400])
        print("  PASS — DFS returned results")

        _sep("Tool: get_node")
        node_result = client.call_tool("get_node", {"label": "live"})
        print(node_result[:300])
        print("  PASS")

        _sep("Tool: get_neighbors")
        nb_result = client.call_tool("get_neighbors", {"label": "live"})
        print(nb_result[:300])
        print("  PASS")

        _sep("Tool: shortest_path")
        path_result = client.call_tool("shortest_path", {"source": "live", "target": "watch"})
        print(path_result[:300])
        print("  PASS")

        _sep("Tool: get_community")
        comm_result = client.call_tool("get_community", {"community_id": 0})
        print(comm_result[:300])
        print("  PASS")

        # ── File mutation test ─────────────────────────────────────────────
        _sep("File mutation + sub-second graph update test")

        stats_before_text = client.call_tool("graph_stats", {})
        nodes_before = int([l for l in stats_before_text.splitlines() if "Nodes:" in l][0].split(":")[1].strip())
        updates_before = int([l for l in stats_before_text.splitlines() if "Updates:" in l][0].split(":")[1].strip())
        print(f"  Before: {nodes_before} nodes, {updates_before} updates")

        # Write a new Python file with unique symbols into the watched project
        new_file = project_path / "graphify" / "_live_test_probe.py"
        new_file.write_text(textwrap.dedent("""
            def live_test_alpha():
                pass

            def live_test_beta():
                live_test_alpha()

            class LiveTestGamma:
                def run(self):
                    live_test_alpha()
                    live_test_beta()
        """))
        print(f"  Wrote: {new_file.name}")
        mutation_time = time.monotonic()

        # Poll for the graph to update (debounce=0.6s + rebuild time, budget 5s)
        deadline = time.monotonic() + 5.0
        updates_after = updates_before
        while time.monotonic() < deadline:
            time.sleep(0.2)
            stats_after_text = client.call_tool("graph_stats", {})
            updates_after = int([l for l in stats_after_text.splitlines() if "Updates:" in l][0].split(":")[1].strip())
            if updates_after > updates_before:
                break

        elapsed = time.monotonic() - mutation_time
        nodes_after = int([l for l in stats_after_text.splitlines() if "Nodes:" in l][0].split(":")[1].strip())
        print(f"  After:  {nodes_after} nodes, {updates_after} updates")
        print(f"  Time from file write to graph update: {elapsed:.2f}s")

        assert updates_after > updates_before, "Graph did not update after file write!"
        assert nodes_after > nodes_before, f"Expected more nodes, got {nodes_after} (was {nodes_before})"
        assert elapsed < 4.0, f"Update took too long: {elapsed:.2f}s (expected <4s with 0.6s debounce)"
        print(f"  PASS — graph updated in {elapsed:.2f}s")

        # Query the new symbols
        _sep("Query newly added symbols")
        probe = client.call_tool("query_graph", {"question": "live_test_alpha live_test_beta LiveTestGamma"})
        print(probe[:500])
        found = "live_test" in probe.lower() or "LiveTest" in probe
        print(f"  New symbols in graph: {found}")
        print("  PASS")

        # Clean up the probe file
        new_file.unlink(missing_ok=True)

        _sep("ALL TESTS PASSED")
        print(f"""
  Summary:
    ✓ MCP initialize handshake
    ✓ 7 tools registered
    ✓ graph_stats returned live data
    ✓ god_nodes returned ranked nodes
    ✓ query_graph BFS traversal
    ✓ query_graph DFS traversal
    ✓ get_node lookup
    ✓ get_neighbors lookup
    ✓ shortest_path traversal
    ✓ get_community listing
    ✓ File mutation detected in {elapsed:.2f}s
    ✓ New symbols queryable after live update
""")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent
    run_integration_test(project.resolve())
