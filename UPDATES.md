# graphify — Updates & How It Works

This document covers everything added in recent work: the `--live` mode, the `--clear live` command, the Redis integration, and a plain-English explanation of how the whole system operates.

---

## Table of Contents

1. [What was added](#1-what-was-added)
2. [How `--live` works](#2-how---live-works)
3. [How `--clear live` works](#3-how---clear-live-works)
4. [Redis integration](#4-redis-integration)
5. [Full pipeline overview](#5-full-pipeline-overview)
6. [File layout](#6-file-layout)
7. [Quick reference](#7-quick-reference)

---

## 1. What was added

### Branch: `feat/clear-live-command`

Two new commands added to `graphify/skill.md`:

| Command | What it does |
|---|---|
| `/graphify --live` | Start a persistent live server + file watcher. Graph updates as files change. |
| `/graphify <path> --live` | Same, on a specific directory. |
| `/graphify --clear live` | Stop the server, wipe the graph and inbox, start fresh. |

These commands were already implemented in `graphify/live.py`. The skill update wires them into the AI agent's command vocabulary so the LLM knows how to invoke them.

### Earlier: Redis integration (`b4ec7c9`)

`graphify live` gained Redis-backed room state management. The live server can now track multiple sessions and persist state across restarts using a Redis stream as an event log.

---

## 2. How `--live` works

### The core idea

Instead of rebuilding the graph from scratch every time, `graphify live` keeps the graph **in memory** and updates it incrementally as files change. Claude queries the live in-memory graph via MCP tools — never a stale snapshot.

### Single process, four responsibilities

```
graphify live <WATCH_PATH>
─────────────────────────────────────────────────────
single process

┌─────────────────────────────────────────────────────┐
│  LiveState (in-memory)                               │
│    nx.Graph   ←──── all reads/writes via RLock       │
│    communities, labels, update_count                 │
│    recent_additions: {update_id: [node_ids]}         │
└──────────┬──────────────────────────────────────────┘
           │
     ┌─────┴──────────────────────────────────┐
     ▼                  ▼           ▼          ▼
watchdog           MCP stdio     REST API   HTTP file
PollingObserver    (JSON-RPC)    port 7478  server 7480
0.6s debounce      Claude Code   curl        graph.html
```

**Thread model:**

| Thread | Role |
|---|---|
| Main (asyncio) | MCP stdio server — JSON-RPC 2.0 on stdin/stdout |
| watchdog | Polls filesystem every 0.6s, fires rebuild callbacks |
| debounce + rebuild | Waits for quiet period, then extracts and merges into LiveState |
| REST API | `ThreadingHTTPServer` — serves curl requests from the graphify skill |
| HTTP file server | Serves `graph.html` with auto-reload on graph changes |
| inbox-startup | Delayed thread: processes inbox files 3s after startup |

The 3-second startup delay exists because Louvain clustering on 1000+ nodes holds Python's GIL for 2–4 seconds. Without the delay, inbox processing blocks asyncio's MCP handshake and the client times out.

### The inbox

Files dropped into `inbox/` are processed immediately. The rest of the project is watched for code changes only. This prevents every editor save from triggering a rebuild.

```
WATCH_PATH/
├── inbox/                  ← drop files here
│   ├── paper.pdf
│   └── notes.md
└── graphify-out/
    ├── graph.json
    ├── graph.html
    └── .graphify_api_port  ← actual REST API port
```

**What happens when a file is dropped:**

1. watchdog detects the new file (polls every 0.6s).
2. Debounce waits 0.6s of quiet.
3. `_apply_inbox_delta` runs on all files in inbox.
4. New nodes/edges are merged into `LiveState.graph` under RLock.
5. `graph.json` and `graph.html` are rewritten.
6. All MCP tools and REST endpoints immediately return the updated graph.

**Extraction by file type:**

| File type | Extraction method | LLM needed |
|---|---|---|
| `.py .ts .go .rs ...` | AST via tree-sitter | No |
| `.md .txt .rst` | Regex entity extractor | No |
| `.pdf` | pypdf → text → regex extractor | No |
| Any (with `--semantic`) | Claude API | Yes |

### Port management

Three servers compete for ports. The REST API always starts first to claim 7478. The viz server starts second at 7480+. The actual port is written to `.graphify_api_port` so the skill reads the real port, not a hardcoded guess.

### Starting the server from Claude Code chat

```
/graphify --live
```

The skill:
1. Reads `.graphify_api_port` to check if a server is already running.
2. If not running: asks for confirmation, then kills stale processes and starts fresh.
3. Fetches `/api/stats`, `/api/gods`, `/api/sessions` and shows a clean summary (no raw JSON).

---

## 3. How `--clear live` works

### Purpose

Wipe everything and start over. Useful when the graph has accumulated noise or you want a clean session for a new topic.

### Steps (always asks permission first)

```
/graphify --clear live
```

1. **Resolve path** — uses the argument if given, otherwise `pwd`.
2. **Ask permission** — tells you exactly what will be deleted before doing anything. Waits for a clear "yes". Aborts on anything else.
3. **Stop server** — `pkill -f "graphify live"`. If no server was running, notes it and continues.
4. **Wipe data:**
   ```bash
   rm -rf WATCH_PATH/graphify-out
   rm -rf WATCH_PATH/inbox
   ```
5. **Start fresh server** — same path, same port (7478), debounce 0.6s.
6. **Confirm to user** — shows inbox path, graph URL, log path.

**This cannot be undone.** The skill always asks before running any destructive command.

---

## 4. Redis integration

### What was added

The live server gained a Redis-backed event log for room/session state. This lets multiple sessions share state and survive process restarts.

**Architecture:**

```
graphify live process
       │
       ├── XADD ingest.events → Redis Streams
       │         {type, path, content_hash, timestamp}
       │
       └── Consumer group reads events
               → routes to AST worker or semantic worker
               → delta applier writes to LiveState
               → XADD graph.deltas (notifies subscribers)
```

**Why Redis Streams (not pub/sub):**
- Events survive worker restarts (pub/sub drops messages when no subscriber).
- Consumer group semantics give exactly-once delivery.
- `XLEN ingest.events` gives a backpressure signal.

**Room state:**
Each session (room) has its own key namespace in Redis. The live server writes room metadata (node count, last update, graph path) so multiple Claude Code sessions can discover and restore each other's graphs.

---

## 5. Full pipeline overview

### Batch mode (`/graphify <path>`)

```
Step 1 — Install check
Step 2 — Detect files  (detect.py → .graphify_detect.json)
Step 2.5 — Transcribe video/audio  (Whisper, only if video detected)
Step 3A — AST extraction  (extract.py → .graphify_ast.json)  ─── parallel
Step 3B — Semantic extraction  (subagents → chunk files)      ─── parallel
Step 3C — Merge AST + semantic  → .graphify_extract.json
Step 4 — Build graph + cluster  (build.py, cluster.py, analyze.py)
Step 5 — Label communities  (human-readable names)
Step 6 — Generate outputs  (graph.html always, obsidian vault if --obsidian)
Step 7 — Optional exports  (Neo4j, SVG, GraphML, MCP server, wiki)
Step 8 — Token reduction benchmark  (if total_words > 5000)
Step 9 — Save manifest, update cost tracker, clean up temp files
```

**Edge taxonomy:**

| Tag | Meaning |
|---|---|
| `EXTRACTED` | Relationship explicitly stated in source (import, call, citation) |
| `INFERRED` | Reasonable inference from structure or context |
| `AMBIGUOUS` | Uncertain — flagged for review |

### Live mode (`/graphify --live`)

The same graph lives in memory and updates incrementally. No batch pipeline re-run needed.

```
file dropped into inbox/
  → watchdog detects (0.6s)
  → extract (AST or lightweight regex)
  → merge into nx.Graph (under RLock)
  → write graph.json + graph.html
  → REST API + MCP tools return updated state
```

### Query mode (`/graphify query "..."`)

```
question
  → find 1-3 best-matching start nodes (label term overlap)
  → BFS (default) or DFS traversal
  → rank traversed nodes by degree
  → filter noise via _is_quality_label
  → keep top 15 quality nodes, max 20 edges
  → answer using only what the graph contains
  → save Q&A as a node (improves future queries)
```

---

## 6. File layout

### Output directory

```
WATCH_PATH/
├── inbox/                         files to process (live mode only)
└── graphify-out/
    ├── graph.json                 current graph (node-link format)
    ├── graph.html                 interactive browser viewer
    ├── GRAPH_REPORT.md            audit report with god nodes + communities
    ├── cost.json                  cumulative token usage across runs
    ├── .graphify_api_port         actual REST API port (live mode)
    ├── .graphify_python           resolved Python interpreter path
    └── history/
        └── graph_TIMESTAMP.json  archived previous sessions (live mode)
```

### Package modules

```
graphify/
├── live.py        live server (LiveState, watcher, MCP server, REST API)
├── serve.py       static MCP server + shared traversal utilities
├── extract.py     AST extraction (tree-sitter, 20+ languages)
├── build.py       build nx.Graph from extracted JSON
├── cluster.py     Louvain community detection
├── analyze.py     god_nodes, surprising_connections, suggest_questions
├── export.py      to_html, to_json, to_obsidian, to_cypher, to_svg, to_graphml
├── detect.py      file type detection, manifest, .graphifyignore
├── cache.py       extraction cache keyed by content hash
├── ingest.py      URL fetching (arxiv, YouTube, Twitter, PDF, HTML)
├── transcribe.py  Whisper audio/video transcription
├── wiki.py        wiki article generation from community graph
└── skill.md       Claude Code skill definition (the /graphify commands)
```

---

## 7. Quick reference

### Commands

```
/graphify <path>                    full batch pipeline
/graphify <path> --update           incremental (only changed files)
/graphify <path> --cluster-only     re-cluster existing graph
/graphify --live                    start live server (current dir)
/graphify <path> --live             start live server (specific path)
/graphify --clear live              stop server, wipe data, start fresh
/graphify query "<question>"        BFS query against graph
/graphify query "<question>" --dfs  DFS query
/graphify path "A" "B"             shortest path between two concepts
/graphify explain "NodeName"        plain-language explanation of a node
/graphify add <url>                 fetch URL, add to corpus, update graph
```

### REST API (live mode only)

```
GET /api/stats                           node/edge/community counts
GET /api/query?q=<terms>&mode=bfs        BFS or DFS traversal
GET /api/node?label=<name>               single node details
GET /api/neighbors?label=<name>          direct neighbors
GET /api/path?source=<A>&target=<B>      shortest path
GET /api/gods?top_n=10                   most connected nodes
GET /api/community?id=<N>               all nodes in community
GET /api/sessions                        list archived sessions
GET /api/sessions?load=<filename>        restore a past session
```

### Install

```bash
pip install graphifyy            # base
pip install 'graphifyy[live]'   # + live mode (mcp, watchdog, pypdf)
pip install 'graphifyy[video]'  # + video transcription (faster-whisper, yt-dlp)
pip install 'graphifyy[all]'    # everything
```
