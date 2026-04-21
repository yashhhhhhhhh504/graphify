"""
HTTP file server that serves graphify-out/ in the browser.
Injects a small polling script into graph.html so it auto-reloads on graph changes.
"""
from __future__ import annotations

import json
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from graphify.live_state import LiveState, log

# Injected into graph.html just before </body>. Polls /version every 2s and
# reloads when the server's update counter changes.
_AUTORELOAD_JS = """
<script>
(function(){
  var v = null;
  function poll(){
    fetch('/version').then(function(r){return r.json();}).then(function(d){
      if(v === null){ v = d.version; }
      else if(d.version !== v){ location.reload(); }
    }).catch(function(){});
    setTimeout(poll, 2000);
  }
  poll();
})();
</script>
"""

_CONTENT_TYPES = {
    ".html": "text/html",
    ".json": "application/json",
    ".js": "application/javascript",
    ".css": "text/css",
}


def start_http_server(out_dir: Path, state: LiveState, port: int = 7477) -> int:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path.lstrip("/") or "graph.html"

            if path == "version":
                G, comm, _ = state.snapshot()
                body = json.dumps({
                    "version": state.update_count,
                    "nodes": G.number_of_nodes(),
                    "edges": G.number_of_edges(),
                    "communities": len(comm),
                }).encode()
                self._send(body, "application/json")
                return

            target = (out_dir / path).resolve()
            try:
                target.relative_to(out_dir.resolve())
            except ValueError:
                self.send_error(403)
                return

            if not target.is_file():
                self.send_error(404, f"Not found: {path}")
                return

            try:
                content = target.read_bytes()
            except OSError as exc:
                self.send_error(500, f"Read error: {exc}")
                return
            if path == "graph.html":
                content = content.replace(b"</body>", _AUTORELOAD_JS.encode() + b"</body>", 1)

            suffix = "." + path.rsplit(".", 1)[-1] if "." in path else ""
            ctype = _CONTENT_TYPES.get(suffix, "application/octet-stream")
            self._send(content, ctype)

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    for p in range(port, port + 20):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", p), Handler)
            httpd.allow_reuse_address = True
            break
        except OSError:
            continue
    else:
        log(f"warning: could not bind HTTP server on ports {port}–{port + 19}")
        return 0

    threading.Thread(target=httpd.serve_forever, daemon=True, name="graphify-live-http").start()
    return p


def tprint(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
