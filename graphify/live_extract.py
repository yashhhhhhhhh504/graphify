"""
Extraction logic for live mode.

Three paths:
  - Code files  → AST via graphify.extract (no LLM)
  - Text/PDF    → lightweight regex (no LLM)
  - Non-code    → Claude API when --semantic is set
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS
from graphify.live_state import LiveState, log
from graphify.live_graph import persist, recluster, merge_fragment


# ── Lightweight regex extractor (no LLM) ────────────────────────────────────

_GIT_LINE_PREFIXES = (
    "On branch ", "Untracked files:", "Changes to be committed",
    "Changes not staged", "nothing to commit", "Your branch is",
    "HEAD detached", "modified:", "deleted:", "new file:",
    "diff --git", "index ", "--- a/", "+++ b/", "@@",
)


def extract_text_lightweight(path: Path) -> tuple[list[dict], list[dict]]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return [], []

    stem = re.sub(r"[^a-z0-9_]", "_", path.stem.lower())[:30]
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def _node(label: str, loc: str = "") -> str | None:
        label = label.strip()
        if len(label) < 4 or len(label) > 80:
            return None
        if re.match(r"^[\d.:,\-/]+$", label):
            return None
        if re.match(r"^Header level \d+$", label, re.IGNORECASE):
            return None
        if any(label.startswith(p) for p in _GIT_LINE_PREFIXES):
            return None
        if label[0] in "#$>!|%@":
            return None
        nid = f"{stem}_{re.sub(r'[^a-z0-9]', '_', label.lower())[:40]}"
        if nid not in seen:
            seen.add(nid)
            nodes.append({
                "id": nid, "label": label, "file_type": "document",
                "source_file": str(path), "source_location": loc or None,
            })
        return nid

    def _edge(src: str, tgt: str, rel: str, conf: str = "INFERRED", score: float = 0.7) -> None:
        if src and tgt and src != tgt:
            edges.append({
                "source": src, "target": tgt, "relation": rel,
                "confidence": conf, "confidence_score": score,
                "source_file": str(path), "weight": score,
            })

    root = _node(path.stem, "file")
    nearby: list[str] = []

    for m in re.finditer(r"^(#{1,4})\s+(.+)$", text, re.MULTILINE):
        nid = _node(m.group(2).strip(), f"line {text[:m.start()].count(chr(10)) + 1}")
        if nid and root:
            _edge(root, nid, "contains", "EXTRACTED", 1.0)
            nearby.append(nid)

    for m in re.finditer(r"\*\*(.+?)\*\*|__(.+?)__|\*([^*\n]{2,40})\*|_([^_\n]{2,40})_", text):
        label = next(g for g in m.groups() if g)
        nid = _node(label)
        if nid:
            nearby.append(nid)

    for m in re.finditer(r"https?://\S+", text):
        url = m.group(0).rstrip(".,)")
        domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
        nid = _node(domain, "url")
        if nid and root:
            _edge(root, nid, "references", "EXTRACTED", 1.0)

    for m in re.finditer(r"\b([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){1,4})\b", text):
        label = m.group(1)
        if label.split()[0] not in {"The", "This", "These", "Those", "That", "An", "A"}:
            nid = _node(label)
            if nid:
                nearby.append(nid)

    for m in re.finditer(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text):
        nid = _node(m.group(1))
        if nid:
            nearby.append(nid)

    # Numbered / bulleted list items as nodes
    for m in re.finditer(r"^[\s]*(?:\d+\.|[-*+])\s+(.{6,80})$", text, re.MULTILINE):
        item = m.group(1).strip().rstrip(".,;:")
        nid = _node(item, f"line {text[:m.start()].count(chr(10)) + 1}")
        if nid and root:
            _edge(root, nid, "contains", "EXTRACTED", 1.0)
            nearby.append(nid)

    # Key: Value pairs (e.g. "Author: Jane Smith", "Status: active")
    for m in re.finditer(r"^([A-Za-z][A-Za-z _-]{2,30}):\s+(.{3,60})$", text, re.MULTILINE):
        key_nid = _node(m.group(1).strip())
        val_nid = _node(m.group(2).strip())
        if key_nid and val_nid and root:
            _edge(root, key_nid, "contains", "EXTRACTED", 1.0)
            _edge(key_nid, val_nid, "defines", "EXTRACTED", 1.0)

    # Inline code spans (backtick) as concept nodes
    for m in re.finditer(r"`([^`\n]{3,50})`", text):
        nid = _node(m.group(1))
        if nid and root:
            _edge(root, nid, "references", "INFERRED", 0.8)
            nearby.append(nid)

    for i, src in enumerate(nearby):
        for tgt in nearby[i + 1: i + 4]:
            _edge(src, tgt, "co_occurs_with", "INFERRED", 0.6)

    return nodes, edges


def extract_pdf_text(path: Path) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(p for p in pages if p.strip())
    except Exception as exc:
        log(f"pdf text extraction failed for {path.name}: {exc}")
        return ""


# ── Delta appliers ───────────────────────────────────────────────────────────

def apply_inbox_delta(state: LiveState, watch_path: Path, inbox_files: list[Path]) -> bool:
    from graphify.build import build_from_json

    code = [p for p in inbox_files if p.suffix.lower() in CODE_EXTENSIONS and p.exists()]
    text = [p for p in inbox_files if p.suffix.lower() in DOC_EXTENSIONS and p.exists()]
    pdfs = [p for p in inbox_files if p.suffix.lower() == ".pdf" and p.exists()]

    all_nodes: list[dict] = []
    all_edges: list[dict] = []

    if code:
        try:
            from graphify.extract import extract
            result = extract(code)
            all_nodes.extend(result.get("nodes", []))
            all_edges.extend(result.get("edges", []))
        except Exception as exc:
            log(f"inbox AST extraction failed: {exc}")

    for f in text:
        n, e = extract_text_lightweight(f)
        all_nodes.extend(n)
        all_edges.extend(e)
        log(f"inbox: {len(n)} nodes from {f.name}")

    for f in pdfs:
        pdf_text = extract_pdf_text(f)
        if pdf_text.strip():
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False,
                prefix=f.stem + "_", encoding="utf-8"
            ) as tmp:
                tmp.write(pdf_text)
                tmp_path = Path(tmp.name)
            try:
                n, e = extract_text_lightweight(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
            for node in n:
                node["source_file"] = str(f)
            for edge in e:
                edge["source_file"] = str(f)
            all_nodes.extend(n)
            all_edges.extend(e)
            log(f"inbox: {len(n)} nodes from PDF {f.name}")
        else:
            log(f"inbox: no text from {f.name} (scanned PDF? try --semantic)")

    if not all_nodes:
        log(f"inbox: {len(inbox_files)} file(s) yielded no extractable content")
        return False

    fragment = {"nodes": all_nodes, "edges": all_edges, "input_tokens": 0, "output_tokens": 0}
    try:
        G_new = build_from_json(fragment)
    except Exception as exc:
        log(f"inbox: graph build failed: {exc}")
        return False

    with state.lock:
        G = state.graph.copy()
        new_nids = merge_fragment(G, G_new)
        communities, labels = recluster(G)
        next_update = state.update_count + 1
        for nid in new_nids:
            if nid in G.nodes:
                G.nodes[nid]["_added_at"] = next_update
        state.recent_additions[next_update] = new_nids
        state.replace(G, communities, labels)

    persist(*state.snapshot(), out_dir=watch_path / "graphify-out")
    log(
        f"inbox: {len(inbox_files)} file(s) → "
        f"{state.graph.number_of_nodes()} nodes, {state.graph.number_of_edges()} edges"
    )
    return True


def apply_code_delta(state: LiveState, watch_path: Path, changed_files: list[Path]) -> bool:
    from graphify.extract import extract
    from graphify.build import build_from_json

    code_files = [p for p in changed_files if p.suffix.lower() in CODE_EXTENSIONS and p.exists()]
    if not code_files:
        return False

    try:
        result = extract(code_files)
    except Exception as exc:
        log(f"AST extraction failed: {exc}")
        return False

    try:
        G_new_code = build_from_json(result)
    except Exception as exc:
        log(f"code delta: graph build failed: {exc}")
        return False

    changed_str = {str(p.resolve()) for p in code_files} | {str(p) for p in code_files}
    changed_rel: set[str] = set()
    for p in code_files:
        try:
            changed_rel.add(str(p.relative_to(watch_path.resolve())))
        except ValueError:
            pass

    with state.lock:
        G = state.graph.copy()

        to_remove = [
            nid for nid, data in G.nodes(data=True)
            if data.get("file_type") == "code"
            and (data.get("source_file") or "") in (changed_str | changed_rel)
        ]
        G.remove_nodes_from(to_remove)

        new_nids = merge_fragment(G, G_new_code)
        communities, labels = recluster(G)
        next_update = state.update_count + 1
        for nid in new_nids:
            if nid in G.nodes:
                G.nodes[nid]["_added_at"] = next_update
        state.recent_additions[next_update] = new_nids
        state.replace(G, communities, labels)

    persist(*state.snapshot(), out_dir=watch_path / "graphify-out")
    log(
        f"code delta: {len(code_files)} file(s) → "
        f"{state.graph.number_of_nodes()} nodes, {state.graph.number_of_edges()} edges"
    )
    return True


# ── Semantic extraction (Claude API) ────────────────────────────────────────

_SEMANTIC_SYSTEM_PROMPT = """You are a knowledge graph extraction service.

You receive the full text of ONE file. Extract named entities and relationships. Return
ONLY by calling the submit_graph_fragment tool — no prose, no explanation.

WHAT TO EXTRACT:
- Concepts, techniques, algorithms, systems, components, data structures
- Authors, organizations, citations, references
- Decisions, trade-offs, constraints, requirements
- Rationale — WHY something was done: create a node for each design decision or
  explanation, then add a `rationale_for` edge pointing to the concept it explains
- Errors, failure modes, and what handles them

EDGE RELATIONS (use these exact strings):
  calls, implements, references, cites, depends_on, extends, uses, defines,
  contains, configures, produces, consumes, rationale_for, conceptually_related_to,
  semantically_similar_to, shares_data_with, co_occurs_with

CONFIDENCE RULES:
- EXTRACTED (score=1.0): relationship is explicit in the text
- INFERRED (score=0.6–0.9): reasonable inference — direct structural evidence 0.8-0.9,
  reasonable but uncertain 0.6-0.7
- AMBIGUOUS (score=0.1–0.3): uncertain, flag for review. Never use 0.5 as a default.

SEMANTIC SIMILARITY: if two concepts in the file solve the same problem or represent
the same idea without any structural link, add a `semantically_similar_to` edge
(INFERRED, score 0.6–0.9). Only add when genuinely non-obvious.

SOURCE LOCATION: set source_location to "line N" for key nodes so the graph links
back to the exact line in the file.

NODE IDs: lowercase snake_case prefixed with the file stem, e.g. "readme_transformer"
Do not invent edges to concepts not mentioned in the file.
Aim for 8–40 nodes per file — be thorough, not sparse."""

_SEMANTIC_TOOL = {
    "name": "submit_graph_fragment",
    "description": "Submit extracted nodes and edges from the given file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "file_type": {"type": "string", "enum": ["document", "paper", "image", "code"]},
                        "source_location": {"type": "string", "description": "e.g. 'line 42'"},
                    },
                    "required": ["id", "label", "file_type"],
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "relation": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["EXTRACTED", "INFERRED", "AMBIGUOUS"]},
                        "confidence_score": {"type": "number"},
                    },
                    "required": ["source", "target", "relation", "confidence", "confidence_score"],
                },
            },
        },
        "required": ["nodes", "edges"],
    },
}


def _semantic_extract_one(path: Path, *, model: str, rel_path: str) -> dict | None:
    """Try claude CLI first (uses Claude Code auth), fall back to Anthropic SDK."""
    frag = _extract_via_claude_cli(path, rel_path=rel_path)
    if frag is not None:
        return frag
    log(f"claude CLI unavailable, falling back to Anthropic SDK for {rel_path}")
    return _extract_via_sdk(path, model=model or "claude-sonnet-4-6", rel_path=rel_path)


def _extract_via_claude_cli(path: Path, *, rel_path: str) -> dict | None:
    import json as _json
    import shutil
    import subprocess

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if len(text) > 120_000:
        text = text[:120_000] + "\n\n[truncated at 120000 chars]"

    stem = re.sub(r"[^a-z0-9_]", "_", path.stem.lower())[:30]
    prompt = (
        f"{_SEMANTIC_SYSTEM_PROMPT}\n\n"
        f"File path: {rel_path}\n"
        f"Node IDs must be prefixed with \"{stem}_\".\n\n"
        "Output ONLY valid JSON — no prose, no markdown fences:\n"
        "{\"nodes\":[{\"id\":\"string\",\"label\":\"string\","
        "\"file_type\":\"document|paper|image|code\","
        "\"source_location\":\"line N or null\"}],"
        "\"edges\":[{\"source\":\"node_id\",\"target\":\"node_id\","
        "\"relation\":\"relation_type\",\"confidence\":\"EXTRACTED|INFERRED|AMBIGUOUS\","
        "\"confidence_score\":0.0}]}\n\n"
        f"--- BEGIN FILE ---\n{text}\n--- END FILE ---"
    )

    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt, "--output-format", "json",
             "--no-session-persistence"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:
        log(f"claude CLI subprocess error for {rel_path}: {exc}")
        return None

    if proc.returncode != 0:
        log(f"claude CLI non-zero exit for {rel_path}: {proc.stderr[:200]}")
        return None

    raw = proc.stdout.strip()
    try:
        envelope = _json.loads(raw)
        raw = envelope.get("result", raw)
    except Exception:
        pass

    # Strip markdown fences if Claude wrapped the JSON
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```\s*$", "", raw)

    try:
        frag = _json.loads(raw.strip())
    except Exception as exc:
        log(f"claude CLI JSON parse failed for {rel_path}: {exc}")
        return None

    for n in frag.get("nodes", []):
        n.setdefault("source_file", rel_path)
    for e in frag.get("edges", []):
        e.setdefault("source_file", rel_path)
        e.setdefault("weight", 1.0)
    return frag


def _extract_via_sdk(path: Path, *, model: str, rel_path: str) -> dict | None:
    try:
        import anthropic
    except ImportError:
        log("anthropic package missing — install with: pip install 'graphifyy[live]'")
        return None

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if len(text) > 120_000:
        text = text[:120_000] + "\n\n[truncated at 120000 chars]"

    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=6000,
            system=[{"type": "text", "text": _SEMANTIC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[_SEMANTIC_TOOL],
            tool_choice={"type": "tool", "name": "submit_graph_fragment"},
            messages=[{
                "role": "user",
                "content": f"File path: {rel_path}\n\n--- BEGIN FILE ---\n{text}\n--- END FILE ---",
            }],
        )
    except Exception as exc:
        log(f"semantic SDK call failed for {rel_path}: {exc}")
        return None

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_graph_fragment":
            frag = dict(block.input)
            for n in frag.get("nodes", []):
                n.setdefault("source_file", rel_path)
            for e in frag.get("edges", []):
                e.setdefault("source_file", rel_path)
                e.setdefault("weight", 1.0)
            return frag
    return None


_NON_CODE_EXTENSIONS = frozenset(
    ext for ext in [".md", ".txt", ".rst", ".pdf", ".png", ".jpg", ".jpeg", ".webp"]
)


def apply_semantic_delta(
    state: LiveState, watch_path: Path, changed_files: list[Path], *, model: str
) -> bool:
    sem_files = [p for p in changed_files if p.suffix.lower() in _NON_CODE_EXTENSIONS and p.exists()]
    if not sem_files:
        return False

    log(f"semantic extraction: {len(sem_files)} file(s) via {model}")

    fragments: list[dict] = []
    for p in sem_files:
        try:
            rel = str(p.resolve().relative_to(watch_path.resolve()))
        except ValueError:
            rel = str(p)
        frag = _semantic_extract_one(p, model=model, rel_path=rel)
        if frag:
            fragments.append(frag)

    if not fragments:
        log("semantic extraction produced nothing")
        return False

    changed_rel = set()
    for p in sem_files:
        try:
            changed_rel.add(str(p.resolve().relative_to(watch_path.resolve())))
        except ValueError:
            pass

    with state.lock:
        G = state.graph.copy()

        to_remove = [
            nid for nid, data in G.nodes(data=True)
            if data.get("file_type") in {"document", "paper", "image"}
            and (data.get("source_file") or "") in changed_rel
        ]
        G.remove_nodes_from(to_remove)

        for frag in fragments:
            for node in frag.get("nodes", []):
                nid = node["id"]
                G.add_node(nid, **{k: v for k, v in node.items() if k != "id"})
            for edge in frag.get("edges", []):
                src, tgt = edge["source"], edge["target"]
                if src in G and tgt in G:
                    G.add_edge(src, tgt, **{k: v for k, v in edge.items() if k not in ("source", "target")})

        communities, labels = recluster(G)
        state.replace(G, communities, labels)

    persist(*state.snapshot(), out_dir=watch_path / "graphify-out")
    log(f"semantic delta: {len(fragments)} file(s) → {state.graph.number_of_nodes()} nodes")
    return True
