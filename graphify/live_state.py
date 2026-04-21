from __future__ import annotations

import re
import sys
import threading
import time
from dataclasses import dataclass, field

import networkx as nx


def log(msg: str) -> None:
    print(f"[graphify live] {msg}", file=sys.stderr, flush=True)


_STOP_WORDS = frozenset({
    "the", "and", "for", "this", "that", "with", "from", "are", "was", "were",
    "has", "have", "had", "but", "not", "also", "can", "will", "may", "use",
    "used", "been", "into", "its", "it's", "than", "then", "when", "where",
    "there", "their", "they", "them", "such", "which", "while", "would",
    "could", "should", "about", "after", "before", "between",
})

_GIT_NOISE = frozenset({
    "On branch", "Untracked files", "Initial commit", "Changes to be committed",
    "Changes not staged", "nothing to commit", "Your branch", "HEAD detached",
})


def is_quality_label(label: str, degree: int = 0) -> bool:
    if not label or len(label) < 4:
        return False
    if re.match(r"^[\d.:,\-/]+$", label):
        return False
    if label[0] in "#$>!|%@":
        return False
    if re.match(r"^Header level \d+$", label, re.IGNORECASE):
        return False
    if label.lower() in _STOP_WORDS:
        return False
    for pat in _GIT_NOISE:
        if label.startswith(pat):
            return False
    if degree < 2 and len(label) < 6:
        return False
    return True


@dataclass
class LiveState:
    graph: nx.Graph = field(default_factory=nx.Graph)
    communities: dict[int, list[str]] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    last_update_ts: float = 0.0
    update_count: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    recent_additions: dict[int, list[str]] = field(default_factory=dict)

    def snapshot(self) -> tuple[nx.Graph, dict[int, list[str]], dict[int, str]]:
        with self.lock:
            return self.graph, self.communities, self.labels

    def replace(
        self,
        graph: nx.Graph,
        communities: dict[int, list[str]],
        labels: dict[int, str] | None = None,
    ) -> None:
        with self.lock:
            self.graph = graph
            self.communities = communities
            self.labels = labels or {cid: f"Community {cid}" for cid in communities}
            self.last_update_ts = time.time()
            self.update_count += 1


def recent_recap(state: LiveState, G: nx.Graph, max_show: int = 8) -> str:
    with state.lock:
        if not state.recent_additions:
            return ""
        latest = max(state.recent_additions)
        nids_raw = list(state.recent_additions.get(latest, []))
    nids = [n for n in nids_raw if n in G.nodes]
    quality = [n for n in nids if is_quality_label(G.nodes[n].get("label", n), G.degree(n))]
    if not quality:
        return f"\n\n── Update #{latest}: {len(nids)} nodes added (all filtered as low-quality) ──"
    shown = [G.nodes[n].get("label", n) for n in quality[:max_show]]
    extra = len(quality) - max_show
    out = f"\n\n── Update #{latest}: {len(nids)} nodes added ({len(quality)} meaningful) ──\n"
    out += "\n".join(f"  + {lbl}" for lbl in shown)
    if extra > 0:
        out += f"\n  ... and {extra} more"
    return out
