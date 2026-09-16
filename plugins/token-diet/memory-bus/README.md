# memory-bus (SSOT D6)

Qdrant "supercompress" memory bus on a server you own (tested on a 40-core CPU-only Kali box). Agents (Claude Code, Codex, Grok, DJcode)
recall <=1.2k tokens of relevant memory per turn instead of loading multi-KB memory files.
Full design, evidence and ADRs: `../reports/memory-bus.md`.

```
Mac                                             Kali .172
─────────────────────────────                   ─────────────────────────────────────────────
mohini-memory sqlite (write-side of record) ──┐
~/.claude/.../memory/*.md                     ├─ mac/memory_bus_sync.py (launchd, 30 min) ──► POST /ingest/batch
agent-common-memory CURRENT + sources         ┘        via ssh -L 8791:127.0.0.1:8791              │
                                                                                                    ▼
Claude UserPromptSubmit hook ─┐                                                     memory_bus/app.py (FastAPI :8791)
Codex SessionStart hook ──────┼─ client/memory-bus (stdlib) ──► GET /recall ◄──── all-MiniLM-L6-v2 (384-d, CPU)
Grok GROK.md instruction ─────┤                                POST /compress     Qdrant `engrams` (int8 SQ, on-disk)
DJcode UserPromptSubmit ──────┘                                GET  /health       Ollama qwen2.5:3b (async engram worker)
```

## Layout

| path | role |
|---|---|
| `memory_bus/textops.py` | pure-stdlib core: token estimate, chunking, MMR, packing, extractive compression, scrubbing (tested) |
| `memory_bus/engine.py` | embeddings + Qdrant `engrams` + ingest/recall/compress + Ollama engram worker |
| `memory_bus/app.py` | FastAPI: `/health`, `/ingest`, `/ingest/batch`, `/recall`, `/compress`; multi-host bind (127.0.0.1 + tailnet if present) |
| `migrate.py` | agent_memory / super_memories / shared_memory -> `engrams` with `legacy:<collection>` source tags (re-embeds; sources untouched) |
| `systemd/memory-bus.service` | Restart=always, root, WorkingDirectory=/opt/memory-bus, venv via uv |
| `install.sh` | apply-phase deploy from the Mac (rsync + `uv venv --system-site-packages` + unit + health) |
| `client/memory-bus` | Mac CLI, stdlib only, Python 3.9: health/recall/ingest/compress/hook/tunnel |
| `mac/memory_bus_sync.py` | Mac -> bus sync of mohini-memory export + INDEX.md + Claude memory + common memory |
| `mac/ai.darsh.memory-bus-sync.plist` | launchd every 1800 s (opens the tunnel first) |
| `mac/install-mac.sh` | installs client + sync under `~/.local/share/memory-bus`, links `~/.local/bin/memory-bus` |
| `hooks/` | Claude settings snippet, Codex hooks.json replacement, GROK.md, DJcode settings snippet |
| `tests/test_textops.py` | `python3 -m unittest discover -s tests -v` (15 tests, run on Mac /usr/bin/python3 3.9) |

## Port and reachability (verified 2026-09-16)

* `127.0.0.1:8790` is **already taken** by `mohini-panel.service` (Remix control panel, pid 1322). The bus uses **8791** (`MB_PORT`).
* Tailscale on Kali is **logged out** (`tailscale ip -4` -> "NeedsLogin"), so there is no `100.x` address. The app
  binds the tailnet IP automatically when `tailscale0` appears (`MB_BIND_TAILNET=1`). Until then the Mac reaches it with
  `memory-bus tunnel` (`ssh -N -f -L 8791:127.0.0.1:8791 $MEMORY_BUS_SSH`) and `MEMORY_BUS_URL` defaults to
  `http://127.0.0.1:8791`.

## API

```
GET  /health
POST /ingest        {text, source, agent?, tags?[], ts?, llm?: queue|sync|off, chunk_tokens?=400, force?, doc?}
POST /ingest/batch  {items:[...]}                       -> {items, ingested, skipped, queued_llm}
GET  /recall?q=&k=8&max_tokens=1200&lambda=0.7&source=&agent=&tags=a,b&full=0&format=json|text
POST /compress      {text, target_tokens=800, query?, mode: tool_output|prose, keep_head=3, keep_tail=3}
```
Optional bearer auth: set `MB_TOKEN` on Kali and `MEMORY_BUS_TOKEN` on the Mac.

## Deploy (apply phase only; nothing here was deployed during recon)

```sh
bash memory-bus/install.sh --migrate      # Kali: /opt/memory-bus, unit, health, legacy migration
bash memory-bus/mac/install-mac.sh        # Mac: client + launchd sync
memory-bus tunnel && memory-bus health
memory-bus recall "codex-max-router token use fix" --max-tokens 800
```
