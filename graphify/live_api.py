"""
Lightweight REST API so the graphify skill can query the live graph via curl.
All routes are GET with query-string params. Runs in a daemon thread.
"""
from __future__ import annotations

import json
import os
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from graphify.live_state import log


def start_api_server(state, handlers: dict, watch_path: Path, port: int = 7478) -> int:
    class ApiHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send_json(self, data: dict, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            route = parsed.path.rstrip("/")

            try:
                result = self._dispatch(route, params)
                if result is None:
                    self._send_json({"error": f"unknown route: {route}"}, 404)
                else:
                    self._send_json({"result": result})
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)

        def _int(self, p: dict, key: str, default: int) -> int:
            try:
                return int(p.get(key, default))
            except (ValueError, TypeError):
                return default

        def _dispatch(self, route: str, p: dict) -> str | None:
            if route == "/api/stats":
                return handlers["graph_stats"]({})
            if route == "/api/query":
                return handlers["query_graph"]({
                    "question": p.get("q", ""),
                    "mode": p.get("mode", "bfs"),
                    "depth": self._int(p, "depth", 3),
                    "token_budget": self._int(p, "budget", 500),
                })
            if route == "/api/node":
                return handlers["get_node"]({"label": p.get("label", "")})
            if route == "/api/neighbors":
                return handlers["get_neighbors"]({
                    "label": p.get("label", ""),
                    "relation_filter": p.get("relation", ""),
                })
            if route == "/api/path":
                return handlers["shortest_path"]({
                    "source": p.get("source", ""),
                    "target": p.get("target", ""),
                    "max_hops": self._int(p, "max_hops", 8),
                })
            if route == "/api/gods":
                return handlers["god_nodes"]({"top_n": self._int(p, "top_n", 10)})
            if route == "/api/community":
                return handlers["get_community"]({"community_id": self._int(p, "id", 0)})
            if route == "/api/sessions":
                return handlers["list_sessions"]({"load": p.get("load", "")})
            return None

    for p in range(port, port + 20):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", p), ApiHandler)
            httpd.allow_reuse_address = True
            break
        except OSError:
            continue
    else:
        log(f"warning: could not bind REST API on ports {port}–{port + 19}")
        return 0

    threading.Thread(target=httpd.serve_forever, daemon=True, name="graphify-live-api").start()

    # Write the actual port so the skill can find us even if 7478 was taken
    try:
        import atexit
        port_file = watch_path / "graphify-out" / ".graphify_api_port"
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(str(p))
        atexit.register(lambda: port_file.unlink(missing_ok=True))
    except Exception:
        pass

    return p
