# token-diet

A plugin for Darshj's Codex Router that cuts what the CLI agents on a Mac spend per turn
(Codex desktop/CLI, the router's Claude and Grok bridges, Claude Code, Grok CLI, DJcode) and
gives them one shared, quantized memory bus so they recall a small pack of relevant notes
instead of loading memory files and old transcripts into every context.

It came out of a measured audit on 2026-09-16 (`docs/FINDINGS.md`, `docs/SSOT.md`). The
numbers below are from that machine; yours will differ, but the causes are structural.

| Path | Before | After |
| --- | --- | --- |
| Codex → router → Grok, first turn | 56,136 input tokens | 24,058 |
| Codex fixed prefix per turn | 51,750 chars | 30,050 |
| Grok skills reminder per turn | 103,068 chars | 10,264 |
| Claude Code `claude -p` prefix | 54,675 tokens | 46,813 |
| Bridged Claude context per turn (pre-fix) | 562k–616k at 8–21% cache | ≤190k at ~99% cache |

## What burns tokens

1. **Whole-thread resend with no compaction ceiling.** Codex sends the entire thread on
   every sampling request. Without a ceiling the context grows to the model window and
   every tool output is paid again each turn.
2. **A large fixed prefix.** Base instructions, skill listings (a mirrored `~/.agents/skills`
   tree that was read twice in 14 days), plugin instructions, `AGENTS.md`, hook reminders.
   Codex caps skills at 2% of the window; Grok pays its 100 KB skills reminder uncached.
3. **`service_tier = "priority"`** — "increased usage" on every turn for speed you may not need.
4. **Memory loaded as files.** A 17 KB memory index plus shared-memory files on every session
   instead of a ≤1.2k-token targeted recall.

## What the plugin does

`apply.py` (idempotent, backs up every file it edits as `<file>.pre-token-diet-<date>`):

- **codex** — removes `service_tier`, sets `model_auto_compact_token_limit = 110000`
  (`scope = "total"`), `tool_output_token_limit = 8000`, `project_doc_max_bytes = 4096`,
  `include_apps_instructions = false`, `[skills] max_context_tokens = 1200`,
  `[features] recommended_plugins = false`. `--agents-md` installs the 1.8 KB
  `configs/codex-AGENTS.md` template.
- **grok** — `[skills] ignore = ["~/.agents/skills", "~/.claude/skills"]`, conservative
  `[compaction.pruning]` and `[compaction.memory_flush]`, `[mcp] max_output_bytes = 20000`,
  `[session] auto_compact_threshold_percent = 30`, `[compat.claude]` off except agents;
  installs `~/.grok/rules/00-memory.md`.
- **claude** — `effortLevel: medium` (per-model `xhigh`/`max` capped to `high`),
  `autoCompactWindow: 200000`, `bashOutputMaxChars: 30000`, `MAX_MCP_OUTPUT_TOKENS=20000`,
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80`. `--archive-skills 'seedance-*'` moves skill groups to
  `~/.claude/skills-archive` (never deletes).
- **djcode** — replaces the launcher shim so DJcode uses its own `~/.djcode-config`
  instead of inheriting every Claude Code skill, hook, MCP server and memory file.
- **hooks** — a Claude Code `UserPromptSubmit` hook that injects ≤1,200 tokens of memory-bus
  recall, and a Codex `SessionStart` hook (only when no `hooks.json` exists yet; see below).

The router itself carries the bridge-side half (commit `a8c5870`): one CLI session per Codex
thread with only new items sent, tool outputs bounded to 24 KB head + 8 KB tail, Codex base
instructions trimmed from 20.9k to 12.9k chars on the Grok path, images stripped from
compaction prompts, and catalog entries that advertise a 160k window with a 110k compaction
ceiling so Codex compacts instead of growing toward 1M tokens.

## Memory bus

`memory-bus/` is a FastAPI service for a server you own with Qdrant and (optionally) Ollama:

- `POST /ingest` chunks text, stores chunks ≤400 tokens verbatim and compresses larger ones
  into ≤120-token engrams with a small local model (`qwen2.5:3b`; falls back to extractive
  compression when Ollama is absent), embeds with `all-MiniLM-L6-v2` (384-d) and upserts by
  content hash into the Qdrant collection `engrams` (cosine, int8 scalar quantization,
  vectors on disk, HNSW m=16).
- `GET /recall?q=&k=&max_tokens=` returns an MMR-deduplicated pack capped at `max_tokens`.
- `POST /compress` extractive salience compression of tool output or prose to a token target.
- `GET /health`.

The Mac client `memory-bus` (stdlib Python) provides `health`, `recall`, `ingest`, `compress`,
`hook claude|djcode|codex` and `tunnel` (an idempotent `ssh -N -f -L 8791:127.0.0.1:8791`).
`memory_bus_sync.py` runs every 30 minutes from launchd and pushes changed notes from the
Claude Code auto-memory directory, the reviewed mohini-memory store and the shared
agent-common-memory directory, scrubbing secret-looking lines before anything leaves the Mac.

Install:

```sh
# server (needs docker qdrant on :6333; ollama optional)
MB_SSH_HOST=root@your-server bash memory-bus/install.sh --migrate

# mac
MEMORY_BUS_SSH=root@your-server bash memory-bus/mac/install-mac.sh
memory-bus tunnel && memory-bus health
memory-bus recall "what was I doing on the router" --max-tokens 800
```

The service binds `127.0.0.1` only; reach it through the tunnel or set `MB_HOSTS` to a tailnet
address and `MEMORY_BUS_URL` on the client. `MB_TOKEN` / `MEMORY_BUS_TOKEN` add a bearer token.

## Apply

```sh
cd plugins/token-diet
python3 apply.py status
python3 apply.py apply --agents-md --archive-skills 'seedance-*' --keep-skills seedance-kb
python3 apply.py uninstall          # restores every backup, moves archived skills back
```

Verify afterwards: `codex exec -m <bridged model> -s read-only 'Reply OK' --json` and read
`input_tokens` from the `turn.completed` event; `claude -p ping --output-format json | jq .usage`.

## Things to know

- **Codex hook trust.** Codex pins each hook command with a `trusted_hash` under
  `[hooks.state]` in `config.toml`. Changing a hook's command silently disables it until you
  approve it again interactively; changing only `additionalContextLimit` does not. That is why
  `apply.py` never rewrites an existing `hooks.json` — call `memory-bus hook codex` from your
  existing SessionStart script instead.
- **`~/.agents/skills` mirror.** Codex desktop mirrors Claude skills into `~/.agents/skills`
  (`external-agent-import-sync-item-types = "all"`). `[[skills.config]] enabled = false` did
  not filter that listing in codex 0.154; moving the directory to an archive did. If the
  desktop app recreates it, Codex reports "Exceeded skills context budget" — archive it again.
- **Compaction on the bridged Claude path** re-opens a fresh CLI session, so a lower ceiling
  trades cached context for an uncached summary turn on very long Claude threads. Measure
  before lowering it further.
- `memory-bus compress` holds the embedder lock, so recall hooks return nothing during a long
  compress (they never block the prompt). A ~0.45 score floor drops filler hits.

## Rollback

`python3 apply.py uninstall` restores the `.pre-token-diet-*` backups. The server side is
`systemctl disable --now memory-bus; rm -r /opt/memory-bus /etc/systemd/system/memory-bus.service`
and, if wanted, dropping the Qdrant `engrams` collection (source collections are never written).
