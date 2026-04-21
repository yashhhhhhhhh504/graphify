from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import networkx as nx

from graphify.live_state import LiveState, log


def archive_graph(out_dir: Path) -> Path | None:
    graph_json = out_dir / "graph.json"
    if not graph_json.exists():
        return None
    history_dir = out_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    archive = history_dir / f"graph-{ts}.json"
    try:
        shutil.copy2(graph_json, archive)
        log(f"archived previous session → history/graph-{ts}.json")
        return archive
    except Exception as exc:
        log(f"warning: could not archive graph ({exc})")
        return None


def list_history(out_dir: Path) -> list[dict]:
    history_dir = out_dir / "history"
    if not history_dir.exists():
        return []
    entries = []
    for f in sorted(history_dir.glob("graph-*.json"), reverse=True):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            entries.append({
                "file": f.name,
                "nodes": len(raw.get("nodes", [])),
                "edges": len(raw.get("links", [])),
            })
        except Exception:
            entries.append({"file": f.name, "nodes": "?", "edges": "?"})
    return entries


def bootstrap(watch_path: Path) -> tuple[nx.Graph, dict, dict]:
    """Start each live session with a clean empty graph. Archive whatever was there before."""
    out_dir = watch_path / "graphify-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_graph(out_dir)
    log("new session — starting with empty graph (previous graph archived to history/)")
    try:
        from graphify.export import to_html
        to_html(nx.Graph(), {}, str(out_dir / "graph.html"))
    except Exception:
        pass
    return nx.Graph(), {}, {}


def persist(G: nx.Graph, communities: dict, labels: dict, out_dir: Path) -> None:
    from graphify.export import to_json, to_html
    out_dir.mkdir(exist_ok=True, parents=True)
    graph_json = out_dir / "graph.json"
    tmp = graph_json.with_suffix(".json.tmp")
    try:
        to_json(G, communities, str(tmp))
        os.replace(tmp, graph_json)
    except Exception as exc:
        log(f"warning: could not persist graph.json ({exc})")
        tmp.unlink(missing_ok=True)
    try:
        if G.number_of_nodes() <= 5000:
            to_html(G, communities, str(out_dir / "graph.html"), community_labels=labels or None)
    except Exception as exc:
        log(f"warning: could not write graph.html ({exc})")


def recluster(G: nx.Graph) -> tuple[dict, dict]:
    from graphify.cluster import cluster as _cluster
    try:
        communities = _cluster(G) if G.number_of_nodes() > 0 else {}
    except Exception as exc:
        log(f"warning: clustering failed ({exc}), using empty communities")
        communities = {}
    for cid, members in communities.items():
        for nid in members:
            if nid in G.nodes:
                G.nodes[nid]["community"] = cid
    labels = {cid: f"Community {cid}" for cid in communities}
    return communities, labels


def merge_fragment(G: nx.Graph, G_new: nx.Graph) -> list[str]:
    """Merge G_new into G in place. Returns list of node IDs that are new."""
    new_nids = []
    for nid, data in G_new.nodes(data=True):
        if nid not in G:
            new_nids.append(nid)
        G.add_node(nid, **data)
    for u, v, data in G_new.edges(data=True):
        if u in G and v in G:
            G.add_edge(u, v, **data)
    return new_nids
