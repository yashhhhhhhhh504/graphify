"""
MCP stdio server for live mode. Wraps the same query logic as serve.py
but reads from LiveState instead of a static graph.json.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph

from graphify.live_state import LiveState, log, is_quality_label, recent_recap
from graphify.live_graph import list_history
from graphify.live_api import start_api_server
from graphify.live_http import start_http_server


def run_mcp(state: LiveState, watch_path: Path, api_port: int = 0) -> None:
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp import types
    except ImportError as e:
        raise ImportError("mcp package not installed. Run: pip install 'graphifyy[live]'") from e

    from graphify.serve import _score_nodes, _bfs, _dfs, _subgraph_to_text, _find_node, _filter_blank_stdin
    from graphify.analyze import god_nodes as _god_nodes

    server = Server("graphify-live")

    # ── Tool handlers ────────────────────────────────────────────────────────

    def _query_graph(args: dict) -> str:
        G, _, _ = state.snapshot()
        if G.number_of_nodes() == 0:
            return "Graph is empty. Drop files into inbox/ or wait for the watcher."
        q = args.get("question", "")
        if not q:
            return "question is required."
        mode = args.get("mode", "bfs")
        depth = min(int(args.get("depth", 3)), 6)
        budget = int(args.get("token_budget", 500))
        terms = [t.lower() for t in q.split() if len(t) > 2]
        scored = _score_nodes(G, terms)
        start = [nid for _, nid in scored[:3]]
        if not start:
            return "No matching nodes found."
        nodes, edges = (_dfs if mode == "dfs" else _bfs)(G, start, depth)

        recent_labels = []
        if state.recent_additions:
            for upd in sorted(state.recent_additions)[-2:]:
                for nid in state.recent_additions.get(upd, []):
                    if nid in nodes and nid in G.nodes:
                        lbl = G.nodes[nid].get("label", nid)
                        if is_quality_label(lbl, G.degree(nid)):
                            recent_labels.append(lbl)

        header = (
            f"Traversal: {mode.upper()} depth={depth} | "
            f"Start: {[G.nodes[n].get('label', n) for n in start]} | "
            f"{len(nodes)} nodes found (graph updated {state.update_count}x)\n\n"
        )
        body = _subgraph_to_text(G, nodes, edges, budget)

        if recent_labels:
            shown = recent_labels[:20]
            extra = len(recent_labels) - 20
            recap = "\n\n── Recent additions ──\n" + "\n".join(f"  + {l}" for l in shown)
            if extra > 0:
                recap += f"\n  ... and {extra} more"
        else:
            recap = ""

        return header + body + recap

    def _get_node(args: dict) -> str:
        G, _, _ = state.snapshot()
        matches = _find_node(G, args["label"])
        if not matches:
            return f"No node matching '{args['label']}'."
        nid = matches[0]
        d = G.nodes[nid]
        return "\n".join([
            f"Node: {d.get('label', nid)}",
            f"  ID: {nid}",
            f"  Source: {d.get('source_file', '')} {d.get('source_location', '')}".rstrip(),
            f"  Type: {d.get('file_type', '')}",
            f"  Community: {d.get('community', '')}",
            f"  Degree: {G.degree(nid)}",
        ])

    def _get_neighbors(args: dict) -> str:
        G, _, _ = state.snapshot()
        matches = _find_node(G, args["label"])
        if not matches:
            return f"No node matching '{args['label']}'."
        nid = matches[0]
        rel_filter = (args.get("relation_filter") or "").lower()
        lines = [f"Neighbors of {G.nodes[nid].get('label', nid)}:"]
        for nb in G.neighbors(nid):
            d = G.edges[nid, nb]
            rel = d.get("relation", "")
            if rel_filter and rel_filter not in rel.lower():
                continue
            lines.append(f"  --> {G.nodes[nb].get('label', nb)} [{rel}] [{d.get('confidence', '')}]")
        return "\n".join(lines)

    def _get_community(args: dict) -> str:
        G, comm, _ = state.snapshot()
        try:
            cid = int(args.get("community_id", 0))
        except (ValueError, TypeError):
            return "community_id must be an integer."
        members = comm.get(cid, [])
        if not members:
            return f"Community {cid} not found."
        lines = [f"Community {cid} ({len(members)} nodes):"]
        for n in members:
            d = G.nodes.get(n, {})
            lines.append(f"  {d.get('label', n)} [{d.get('source_file', '')}]")
        return "\n".join(lines)

    def _god(args: dict) -> str:
        G, _, _ = state.snapshot()
        if G.number_of_nodes() == 0:
            return "Graph is empty."
        try:
            top_n = int(args.get("top_n", 10))
        except (ValueError, TypeError):
            top_n = 10
        top = _god_nodes(G, top_n=top_n)
        lines = ["God nodes (most connected):"]
        lines += [f"  {i}. {n['label']} — {n['degree']} edges" for i, n in enumerate(top, 1)]
        return "\n".join(lines)

    def _stats(_: dict) -> str:
        G, comm, _ = state.snapshot()
        confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
        total = max(len(confs), 1)
        last = "never" if state.last_update_ts == 0 else f"{int(time.time() - state.last_update_ts)}s ago"
        history = list_history(watch_path / "graphify-out")
        history_line = f"  Past sessions: {len(history)}"
        if history:
            h = history[0]
            history_line += f" (latest: {h['file']} — {h['nodes']} nodes, {h['edges']} edges)"
        quality_count = sum(
            1 for n, d in G.nodes(data=True)
            if is_quality_label(d.get("label", n), G.degree(n))
        )
        return (
            f"Live graph stats:\n"
            f"  Nodes: {G.number_of_nodes()} total  ({quality_count} meaningful)\n"
            f"  Edges: {G.number_of_edges()}\n"
            f"  Communities: {len(comm)}\n"
            f"  Updates: {state.update_count}\n"
            f"  Last update: {last}\n"
            f"  EXTRACTED: {round(confs.count('EXTRACTED') / total * 100)}%\n"
            f"  INFERRED:  {round(confs.count('INFERRED') / total * 100)}%\n"
            f"  AMBIGUOUS: {round(confs.count('AMBIGUOUS') / total * 100)}%\n"
            f"  Watching: {watch_path.resolve()}\n"
            f"{history_line}"
            + recent_recap(state, G, max_show=8)
        )

    def _shortest_path(args: dict) -> str:
        G, _, _ = state.snapshot()
        src_scored = _score_nodes(G, [t.lower() for t in args["source"].split()])
        tgt_scored = _score_nodes(G, [t.lower() for t in args["target"].split()])
        if not src_scored:
            return f"No node matching source '{args['source']}'."
        if not tgt_scored:
            return f"No node matching target '{args['target']}'."
        s, t = src_scored[0][1], tgt_scored[0][1]
        try:
            path = nx.shortest_path(G, s, t)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return f"No path found between '{G.nodes[s].get('label', s)}' and '{G.nodes[t].get('label', t)}'."
        if len(path) - 1 > int(args.get("max_hops", 8)):
            return f"Path too long ({len(path)-1} hops, max={args.get('max_hops', 8)})."
        segs: list[str] = []
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            ed = G.edges[u, v]
            rel = ed.get("relation", "")
            conf = ed.get("confidence", "")
            if i == 0:
                segs.append(G.nodes[u].get("label", u))
            segs.append(f"--{rel}[{conf}]--> {G.nodes[v].get('label', v)}")
        return f"Shortest path ({len(path)-1} hops):\n  " + " ".join(segs)

    def _list_sessions(args: dict) -> str:
        load_file = (args.get("load") or "").strip()
        if load_file:
            history_path = watch_path / "graphify-out" / "history" / load_file
            if not history_path.exists():
                return f"Session file not found: {load_file}"
            try:
                from graphify.cluster import cluster as _cluster
                raw = json.loads(history_path.read_text(encoding="utf-8"))
                try:
                    G = json_graph.node_link_graph(raw, edges="links")
                except TypeError:
                    G = json_graph.node_link_graph(raw)
                communities = _cluster(G) if G.number_of_nodes() > 0 else {}
                labels = {cid: f"Community {cid}" for cid in communities}
                state.replace(G, communities, labels)
                return f"Loaded: {load_file} — {G.number_of_nodes()} nodes, {G.number_of_edges()} edges"
            except Exception as exc:
                return f"Failed to load {load_file}: {exc}"

        history = list_history(watch_path / "graphify-out")
        if not history:
            return "No past sessions in graphify-out/history/"
        lines = [f"Past sessions ({len(history)}):"]
        for h in history:
            lines.append(f"  {h['file']}  —  {h['nodes']} nodes, {h['edges']} edges")
        lines.append("\nTo restore: call list_sessions with load='<filename>'")
        return "\n".join(lines)

    handlers = {
        "query_graph": _query_graph,
        "get_node": _get_node,
        "get_neighbors": _get_neighbors,
        "get_community": _get_community,
        "god_nodes": _god,
        "graph_stats": _stats,
        "shortest_path": _shortest_path,
        "list_sessions": _list_sessions,
    }

    # ── Wire up MCP tool definitions ─────────────────────────────────────────

    @server.list_tools()
    async def list_tools():
        from mcp import types
        return [
            types.Tool(name="query_graph", description="BFS/DFS traversal of the live graph.", inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "mode": {"type": "string", "enum": ["bfs", "dfs"], "default": "bfs"},
                    "depth": {"type": "integer", "default": 3},
                    "token_budget": {"type": "integer", "default": 2000},
                },
                "required": ["question"],
            }),
            types.Tool(name="get_node", description="Details for a single node.", inputSchema={
                "type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"],
            }),
            types.Tool(name="get_neighbors", description="Direct neighbors of a node.", inputSchema={
                "type": "object",
                "properties": {"label": {"type": "string"}, "relation_filter": {"type": "string"}},
                "required": ["label"],
            }),
            types.Tool(name="get_community", description="All nodes in a community.", inputSchema={
                "type": "object", "properties": {"community_id": {"type": "integer"}}, "required": ["community_id"],
            }),
            types.Tool(name="god_nodes", description="Most connected nodes.", inputSchema={
                "type": "object", "properties": {"top_n": {"type": "integer", "default": 10}},
            }),
            types.Tool(name="graph_stats", description="Node/edge/community counts and last update time.", inputSchema={
                "type": "object", "properties": {},
            }),
            types.Tool(name="shortest_path", description="Shortest path between two concepts.", inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "max_hops": {"type": "integer", "default": 8},
                },
                "required": ["source", "target"],
            }),
            types.Tool(name="list_sessions", description="List or restore archived past sessions.", inputSchema={
                "type": "object",
                "properties": {"load": {"type": "string"}},
            }),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        from mcp import types
        fn = handlers.get(name)
        if not fn:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
        try:
            return [types.TextContent(type="text", text=fn(arguments or {}))]
        except Exception as exc:
            return [types.TextContent(type="text", text=f"Error in {name}: {exc}")]

    # REST API starts first — must claim its port before the viz server tries
    actual_api_port = 0
    if api_port:
        actual_api_port = start_api_server(state, handlers, watch_path, port=api_port)
        if actual_api_port:
            log(f"REST API ready → http://localhost:{actual_api_port}/api/stats")

    # Viz server gets 7480+ to avoid colliding with REST (7478)
    viz_port = start_http_server(watch_path / "graphify-out", state, port=7480)
    if viz_port:
        graph_url = f"http://localhost:{viz_port}/graph.html"
        log(f"graph viewer ready → {graph_url}  (auto-reloads on changes)")
        try:
            import subprocess
            subprocess.Popen(["open", graph_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    async def _async_main() -> None:
        async with stdio_server() as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    _filter_blank_stdin()
    log("MCP server ready on stdio")
    try:
        asyncio.run(_async_main())
    except Exception:
        pass

    # Keep process alive after MCP stdin closes so REST API daemon threads stay up
    if actual_api_port:
        log("MCP stdin closed — REST API still running, Ctrl+C to stop")
        try:
            import time as _time
            while True:
                _time.sleep(1)
        except KeyboardInterrupt:
            pass
