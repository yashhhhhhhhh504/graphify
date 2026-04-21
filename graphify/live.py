"""
graphify live — entry point.

Starts a persistent process that is simultaneously a file watcher and an MCP
stdio server (or a browser-friendly terminal mode). All the real logic lives in:

  live_state.py    — LiveState dataclass, quality filtering, logging
  live_graph.py    — bootstrap, persist, archive, merge helpers
  live_extract.py  — inbox, code (AST), and semantic (Claude API) extraction
  live_watcher.py  — watchdog file watcher thread
  live_api.py      — REST HTTP API (for curl/skill)
  live_http.py     — HTTP file server (browser viz + auto-reload)
  live_mcp.py      — MCP stdio server (for Claude Code / Claude Desktop)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS
from graphify.live_state import LiveState, log
from graphify.live_graph import bootstrap, persist
from graphify.live_extract import apply_inbox_delta
from graphify.live_watcher import start_watcher
from graphify.live_mcp import run_mcp
from graphify.live_http import start_http_server, tprint


def _setup_inbox(watch_path: Path) -> Path:
    inbox = watch_path / "inbox"
    inbox.mkdir(exist_ok=True)
    readme = inbox / "README.md"
    if not readme.exists():
        readme.write_text(
            "# graphify inbox\n\n"
            "Drop any file here and graphify adds it to the live graph within 0.6 seconds.\n\n"
            "Supported formats:\n"
            "- `.md`, `.txt`, `.rst` — notes, docs, ideas\n"
            "- `.py`, `.ts`, `.go`, `.rs`, ... — code files (AST extraction)\n\n"
            "No commands needed. Just drop and it appears in the graph.\n",
            encoding="utf-8",
        )
    return inbox


def _process_inbox_at_startup(state: LiveState, watch_path: Path, inbox: Path) -> None:
    existing = [
        p for p in inbox.rglob("*")
        if p.is_file()
        and not p.name.startswith(".")
        and p.name != "README.md"
        and p.suffix.lower() in (CODE_EXTENSIONS | DOC_EXTENSIONS | {".pdf"})
    ]
    if not existing:
        return
    log(f"inbox: processing {len(existing)} pre-existing file(s) at startup")
    apply_inbox_delta(state, watch_path, existing)


def _terminal_mode(
    state: LiveState,
    watch_path: Path,
    *,
    debounce: float,
    semantic: bool,
    semantic_model: str,
    port: int = 7477,
) -> None:
    out_dir = watch_path / "graphify-out"
    actual_port = start_http_server(out_dir, state, port=port)
    graph_url = f"http://localhost:{actual_port}/graph.html" if actual_port else str(out_dir / "graph.html")

    G, comm, _ = state.snapshot()
    print()
    print("  graphify live ─────────────────────────────────────────────")
    print(f"  Watching : {watch_path}")
    print(f"  Graph    : {G.number_of_nodes()} nodes · {G.number_of_edges()} edges · {len(comm)} communities")
    if actual_port:
        print(f"  Browser  : {graph_url}  (auto-reloads on changes)")
    else:
        print(f"  Browser  : open {out_dir / 'graph.html'}")
    print(f"  Debounce : {debounce}s")
    print(f"  Semantic : {'on (' + semantic_model + ')' if semantic else 'off (code only)'}")
    print("  Press Ctrl+C to stop")
    print("  ────────────────────────────────────────────────────────────")
    print()

    if actual_port and (out_dir / "graph.html").exists():
        import webbrowser
        webbrowser.open(graph_url)
        tprint(f"opened {graph_url}")

    # Patch replace() so terminal gets a live update line on each graph change
    original_replace = state.replace

    def _patched_replace(graph, communities, labels=None):
        original_replace(graph, communities, labels)
        tprint(
            f"graph updated → {graph.number_of_nodes()} nodes · "
            f"{graph.number_of_edges()} edges · {len(communities)} communities  "
            f"(update #{state.update_count})"
        )
        if actual_port:
            tprint(f"browser auto-reloading → {graph_url}")

    state.replace = _patched_replace  # type: ignore[method-assign]

    start_watcher(state, watch_path, debounce=debounce, semantic=semantic, semantic_model=semantic_model)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        print("  graphify live stopped.")


def live(
    watch_path: Path,
    *,
    debounce: float = 0.6,
    semantic: bool = False,
    semantic_model: str = "claude-sonnet-4-5",
    terminal: bool = False,
    port: int = 7477,
    api_port: int = 7478,
) -> None:
    watch_path = watch_path.resolve()
    if not watch_path.exists():
        print(f"error: path does not exist: {watch_path}", file=sys.stderr)
        sys.exit(1)
    if not watch_path.is_dir():
        print(f"error: not a directory: {watch_path}", file=sys.stderr)
        sys.exit(1)

    if semantic and not os.environ.get("ANTHROPIC_API_KEY"):
        msg = "warning: --semantic requires ANTHROPIC_API_KEY — non-code changes will not be processed."
        print(msg) if terminal else log(msg)

    G, communities, labels = bootstrap(watch_path)
    state = LiveState()
    state.replace(G, communities, labels)

    inbox = _setup_inbox(watch_path)

    if terminal:
        _process_inbox_at_startup(state, watch_path, inbox)
    else:
        # Delay inbox processing so the MCP handshake finishes first.
        # Louvain on a large inbox holds the GIL and will time out the MCP client.
        def _delayed():
            time.sleep(3)
            _process_inbox_at_startup(state, watch_path, inbox)
        threading.Thread(target=_delayed, daemon=True, name="inbox-startup").start()

    if terminal:
        _terminal_mode(
            state, watch_path,
            debounce=debounce, semantic=semantic,
            semantic_model=semantic_model, port=port,
        )
    else:
        start_watcher(state, watch_path, debounce=debounce, semantic=semantic, semantic_model=semantic_model)
        os.environ["GRAPHIFY_WATCH_PATH"] = str(watch_path)
        run_mcp(state, watch_path, api_port=api_port)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="graphify live", description="Live streaming knowledge graph")
    parser.add_argument("path", nargs="?", default=".", help="Folder to watch and serve")
    parser.add_argument("--debounce", type=float, default=0.6)
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--model", default="claude-sonnet-4-5")
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("--port", type=int, default=7477)
    parser.add_argument("--api-port", type=int, default=7478)
    args = parser.parse_args()
    live(
        Path(args.path),
        debounce=args.debounce,
        semantic=args.semantic,
        semantic_model=args.model,
        terminal=args.terminal,
        port=args.port,
        api_port=args.api_port,
    )
