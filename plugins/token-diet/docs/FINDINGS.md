# FINDINGS — Agent Token Diet (2026-09-16)

Deliverable D7 of `SSOT.md`. Every number below comes from a log in `patches/` or `reports/`, from the SSOT §2/§2b
recon, or from the critic's re-measurement at ~14:30 IST; the source is named next to each figure. Nothing was
re-measured for this report. Secrets are not reproduced. Scope: Codex (desktop + `codex exec`, bridged through
codex-max-router to Claude and Grok), Claude Code, Grok CLI, DJcode, and the new memory bus on Kali <kali-host>.

Headline: Codex → Grok first turn 56,136 → 24,058 input tokens (-57%); Claude Code `-p` prefix 54,675 → 46,813
(-14%, 1,813 over its 45k ceiling). The 110k compaction ceiling, tool-output bounds and effort change are configured
and parse-verified but not yet exercised by a long session. The memory bus is live, quantized, survives restarts and
answers recall in ~0.6 s through the tunnel. Open: the Codex SessionStart memory hook does not inject; DJcode `-p`
hangs on a pre-existing bundle defect.

---

## 1. Why tokens were burning

Ranked by tokens burned, as the SSOT §3 ordering was corrected by the adversarial recon (§2b) and APPLY-PLAN §1.
"Refuted" lines record claims the first recon made that verification threw out.

### 1.1 Whole-thread contexts with no compaction ceiling (largest)

- Evidence (SSOT §2.1, §2b): claude-max-fable session 09-15: 503 turns, 69.8M input tokens, 61.4M cache-read (88%)
  → 8.4M uncached. Pre-fix bridged Claude sessions on 09-16 02:29-02:38 hit 562k-616k tokens per turn (old 950k
  advertised window) with cache hit 8-21%. gpt-reserve 09-13: 160 turns, 23.1M input (94% cached), real peak
  context 267k. gpt-6-astra 02-05: 77 turns, 9.33M input (97% cached), real peak 173k.
- Refuted: the SSOT's "346k / 534k per turn" for the gpt-* sessions were input+cached double counts (§2b).
- Fix: `model_auto_compact_token_limit = 110000` (scope `total`) in `~/.codex/config.toml`, mirrored as
  `auto_compact_token_limit: 110000` on all router catalog entries; grok-max window 200k → 160k; Claude Code
  `autoCompactWindow 200000` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80`; DJcode `CLAUDE_CODE_AUTO_COMPACT_WINDOW=32000`.
- Status: configured and parse-verified; no session has crossed 110k since apply, so the behaviour is unproven
  (critic "unverified").

### 1.2 Fixed prefix paid on every turn

- Codex (G1 measurement, exec grok-max rollout 09:29): base_instructions 20,919 + developer messages 20,888 (skills
  16,253, multi_agent 2,700, collab 920, perms 341, saved-records 674) + recommended_plugins 3,208 + AGENTS.md 6,735
  = 51,750 chars before any work. Desktop rollout 09:19 with two model switches: 99,809 chars.
- Refuted (§2b): 25 plugins enabled, not 11; the skills block was dominated by the `~/.agents/skills` mirror
  (context-engineering-kit 7.2k, trailofbits 5.7k, gstack 1.8k, seedance ~1.2k chars) with 2 reads in 14 days.
- Grok bridged path (SSOT §2.4, §2b; reports/grok.md): first turn 56,136 input tokens, of which a 103,068-char
  `<system-reminder>` skills list from `~/.agents/skills` (239 entries) + `~/.grok/bundled` (22).
- Refuted (Router F1): the skills list was attributed to `~/.claude/skills`; 0 of 261 entries came from there, so
  archiving Claude skills would not have touched Grok. `GROK_CLAUDE_SKILLS_ENABLED=false` is honoured; the real
  off-switch is `[skills] ignore` in `~/.grok/config.toml`.
- Claude Code (G3 baseline): `claude -p ping` = 2 + 30,790 cache_creation + 23,883 cache_read = 54,675 tokens.
  73 entries in `~/.claude/skills` (20 seedance-*, 9 coach-*), MCP servers declared in three places, 12 claude.ai
  connectors, SessionStart hook injecting a 3,638-char file, MEMORY.md 16,883 B + CLAUDE.md includes ≈9.5 KB.
- Refuted (Claude F1): the "107k first turn" baseline included a one-off `/claude-api` skill body of ≈45-46k tokens
  (100,753 − 54,681); the real interactive prefix was ≈61-65k, not 78k.

### 1.3 Cache misses

- Pre-09-16 router bug: every bridged Claude turn re-uploaded the thread (cache hit 8-21% on the 02:29-02:38
  sessions, 20.9M and 22.3M input in 46-50 turns). Fixed before this work; post-fix sessions 08:47-08:57 ran at
  ≈99% cache with 80-200k contexts (SSOT §2.1).
- Grok: a fresh bridged session pays the whole prefix (E2E-codex-grok §3: cached 0 on turn 1).
- Refuted (§2b): "xAI reports no cache hits" — it reports 23-26% on bridged 7-day traffic and 94.6% direct.

### 1.4 Memory loaded as files instead of retrieved

- MEMORY.md 16,883 B, CLAUDE.md includes ≈9.5 KB, AGENTS.md 6,679 B, agent-common-memory INDEX.md 83 KB, Codex
  lifecycle SessionStart dump ≤2,500 chars, Stop `<hook_prompt>` 667 chars per tool turn (SSOT §2.1, §2.6;
  reports/memory-bus.md §3.4).
- Fix: memory bus (section 4) + trimmed index files + recall hooks.

### 1.5 `service_tier = "priority"` on Codex

- Mechanism confirmed (config.toml line 8, `ResponseCreate.service_tier`, catalog gate); thread settings showed
  priority on 51/176 overrides (§2b).
- Refuted (Codex F1): "≈2x quota, 2.3M tokens saved on the 02-05 session". The multiplier for Astra is unknown
  ("2x speed, increased usage" is a speed figure); that session ran priority on only 14/77 turns. Removing the key
  is zero-loss hygiene and is not counted toward the ≥50% goal.

### 1.6 Effort / thinking defaults

- Last home Claude session: 68.8k output tokens, 39.8k of them thinking (58%); effortLevel `high` global,
  `xhigh` for claude-opus-5 (SSOT §2.3).
- Fix: `effortLevel medium`, claude-opus-5 `high`; Grok `default_reasoning_effort = "medium"`.
- Status: output saving unmeasured (APPLY-PLAN §4 #6).

### 1.7 Untruncated tool outputs (smallest)

- Refuted (Router F2): the 1,043,414 / 434,212 / 243,895-char outputs were `input_image` parts that
  `adapter.extract_images()` already turns into placeholders; observed cost +12.2k tokens, not 400k. Text tool
  output in the 09-15 session was 1.68 MB; a 32 KB cap removes ≈86k chars ≈ 35k tokens (≈0.05% of 69.8M input).
- Kept as a defensive bound (24 KiB head + 8 KiB tail per output, 96 KiB per send) plus checkpoint image stripping,
  which closed a real gap: `router.py` stringified base64 image parts into the compaction prompt.

---

## 2. What was changed

All changes are additive or backed up; nothing was deleted. Sizes and paths from G1-G6 and the E2E logs; every
backup file listed below was confirmed present by `ls` while writing this report.

### 2.1 Codex (`patches/G1-codex-config.md`, SSOT §6 13:35)

| File | Change | Backup | Rollback |
|---|---|---|---|
| `~/.codex/config.toml` | `service_tier` removed; `model_auto_compact_token_limit=110000`, `_scope="total"`; `tool_output_token_limit=8000`; `project_doc_max_bytes=4096`; `include_apps_instructions=false`; `[skills] max_context_tokens=1200` + 4 `[[skills.config]] enabled=false`; `[features] recommended_plugins=false`; 13 `[plugins.*] enabled=false`; `[mcp_servers.codex-astra]` → ChatGPT.app path, `enabled=false` | `~/.codex/config.toml.pre-diet-20260916` (11,022 B); `~/.codex/backups/config.toml.20260916` | `cp ~/.codex/config.toml.pre-diet-20260916 ~/.codex/config.toml` |
| `~/.codex/AGENTS.md` | Replaced with 1,754 B / 227 words, memory-bus pointer | `~/.codex/AGENTS.md.pre-diet-20260916` (6,679 B) | `cp ~/.codex/AGENTS.md.pre-diet-20260916 ~/.codex/AGENTS.md` |
| `~/.agents/skills` | 64 entries moved to archive; dir recreated with only the `mohini-memory` symlink (decision #3 fallback: `[[skills.config]]` did not filter) | `~/.agents/skills-archive-20260916` | `rm ~/.agents/skills/mohini-memory && rmdir ~/.agents/skills && mv ~/.agents/skills-archive-20260916 ~/.agents/skills` |
| `~/.codex/hooks.json` | SessionStart → `memory-bus hook codex --max-tokens 1000` (timeout 12, additionalContextLimit 6000, matcher `startup\|resume\|clear\|compact`); other events unchanged | `~/.codex/hooks.json.pre-diet-20260916` (1,862 B) | `cp ~/.codex/hooks.json.pre-diet-20260916 ~/.codex/hooks.json` |
| `~/.codex/skills/mohini-memory/scripts/lifecycle.py` | Stop `<hook_prompt>` reason capped (285 chars) | `lifecycle.py.pre-diet-20260916` (5,701 B) | `cp` the backup over the file |

### 2.2 codex-max-router (`patches/G2-router.md`, `E2E-codex-grok.md` §1)

| File | Change | Backup | Rollback |
|---|---|---|---|
| `adapter.py` | `bound_tool_outputs()` 24 KiB head + 8 KiB tail per output, 96 KiB per send; `trim_base_instructions()` Grok-only, case-insensitive headings; `GROK_MEMORY_SYSTEM` (142 chars) in the Grok preamble | `adapter.py.pre-diet-20260916` | `git revert a8c5870` or `cp` backup |
| `router.py` | `strip_checkpoint_images()`; compaction prompt built from `strip_checkpoint_images(bound_tool_outputs(material))` | `router.py.pre-diet-20260916` | same |
| `test_router.py` | +7 tests (53 → 60; 63 at HEAD 3db023f) | `test_router.py.pre-diet-20260916` | same |
| `models.json` → now `models.base.json` (generated `state/models.json`) | grok-max window 200000 → 160000; `auto_compact_token_limit: 110000` on 7 entries; `service_tiers` removed from claude-max-*/grok-max | `models.json.pre-diet-20260916` | same; the catalog is now generated from `models.base.json` on restart |
| `CHANGELOG.md` | `## 2026-09-16 (token diet)` entry (line 24) | `CHANGELOG.md.pre-diet-20260916` | same |

Commit `a8c5870` (branch `token-diet`) was merged to `main` by the concurrent dashboard workstream; listener
pid 69285 started 13:32:41 on `d0d0bac`, which contains it. HEAD is now `3db023f` (tests only). The launchd
service was not restarted by this work.

### 2.3 Claude Code (`patches/G3-claude-code.md`, `E2E-claude-djcode.md`, SSOT §6 13:35)

| File | Change | Backup | Rollback |
|---|---|---|---|
| `~/.claude/settings.json` | `effortLevel` high → medium; `modelSettings.claude-opus-5` xhigh → high; `autoCompactWindow 200000`; `bashOutputMaxChars 30000`; env `MAX_MCP_OUTPUT_TOKENS=20000`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; ignored `mcpServers` block removed; UserPromptSubmit memory-bus hook added (timeout 6). SessionStart fable5 dump kept (S4 not applied) | `~/.claude/settings.json.pre-diet-20260916` (2,629 B) | `cp ~/.claude/settings.json.pre-diet-20260916 ~/.claude/settings.json` |
| `~/.claude.json` | `claude mcp remove codex-astra -s user`; `claude mcp remove figma -s local` | `~/.claude.json.pre-diet-20260916` (69,918 B) — restore only the two entries, never the whole file | `claude mcp add …` from the backup's entries |
| `~/.claude/skills/` | 19 seedance-* + 9 coach-* symlinks moved (73 → 45 entries); seedance-kb and arcads-seedance-prompting kept | `~/.claude/skills-archive/` (28 entries) | `mv ~/.claude/skills-archive/* ~/.claude/skills/` |
| `~/.claude/codex-astra.md` | Rewritten, 909 B, ChatGPT.app binary path, `codex exec` recipe, no MCP section | `codex-astra.md.pre-diet-20260916` (3,766 B) | `cp` backup |
| `~/.claude/mohini-shared-memory.md` | Trimmed to 1,353 B | `mohini-shared-memory.md.pre-diet-20260916` (3,569 B) | `cp` backup |
| `~/.claude/CLAUDE.md` | 2,457 B; Codex-bridge sentence corrected; COMPUTER-USE-DIET block added | `CLAUDE.md.pre-diet-20260916` (2,183 B) | `cp` backup |
| `~/.claude/projects/-Users-darshjme/memory/MEMORY.md` | Index reduced to 4,716 B; delisted files stay on disk and are served by recall | `MEMORY.md.pre-diet-20260916` (16,883 B) | `cp` backup |

### 2.4 Grok CLI (`patches/G4-grok.md`, `E2E-codex-grok.md` §3)

| File | Change | Backup | Rollback |
|---|---|---|---|
| `~/.grok/config.toml` | `[models] default = "grok-4.6"` (composer left the catalog), `default_reasoning_effort = "medium"`; `[session] auto_compact_threshold_percent = 30`; `[compaction.pruning]` keep_last_n_turns 3, soft_trim 3000/1200/800, hard_clear 8; `[compaction.memory_flush]` (inert while `[memory] enabled=false`); `[mcp] max_output_bytes = 20000`; `[skills] ignore = ["~/.agents/skills", "~/.claude/skills"]`; `[compat.claude]` skills/hooks/mcps = false, agents = true | `~/.grok/config.toml.pre-diet-20260916` (411 B) | `cp ~/.grok/config.toml.pre-diet-20260916 ~/.grok/config.toml` |
| `~/.grok/rules/00-memory.md` | New, 640 B: `memory-bus recall` / `compress` rules. Loaded (seen in the session's `prompt_context.json`); `~/.grok/GROK.md` (757 B) is never loaded | none (new file) | `rm ~/.grok/rules/00-memory.md` |

### 2.5 DJcode (`patches/G5-djcode.md`)

| File | Change | Backup | Rollback |
|---|---|---|---|
| `~/.djcode/bin/djcode` | Shim (730 B) exports `CLAUDE_CONFIG_DIR=~/.djcode-config`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=32000`, `BASH_MAX_OUTPUT_LENGTH=12000`, `MAX_MCP_OUTPUT_TOKENS=8000`, `DO_NOT_TRACK=1`, `ANTHROPIC_BASE_URL=http://localhost:11434`, `ANTHROPIC_MODEL=gemma4`, placeholder API key; all `${VAR:-default}` | `~/.djcode/bin/djcode.pre-diet-20260916` (257 B) | `cp ~/.djcode/bin/djcode.pre-diet-20260916 ~/.djcode/bin/djcode` |
| `~/.djcode-config/CLAUDE.md`, `settings.json` | New: 444 B rules; settings with env, `effortLevel medium`, UserPromptSubmit `memory-bus hook djcode --max-tokens 1000` | none (new dir) | `mv ~/.djcode-config ~/.djcode-config.rolled-back-20260916` |

### 2.6 Memory bus (`patches/G6-memory-bus.md`, `E2E-memory-bus.md`)

| Location | Change | Backup | Rollback |
|---|---|---|---|
| Kali `/opt/memory-bus`, `/etc/systemd/system/memory-bus.service` | New FastAPI service on 127.0.0.1:8791 (8790 is mohini-panel); `Restart=always`; venv with `--system-site-packages` | none (additive) | `systemctl disable --now memory-bus; rm /etc/systemd/system/memory-bus.service; systemctl daemon-reload; rm -r /opt/memory-bus` |
| Qdrant collection `engrams` | New: 384-d cosine, int8 scalar quantization q=0.99, vectors on_disk, HNSW m=16. Migrated agent_memory/super_memories/shared_memory (3,162 points) with source tags; sources untouched (3336/89/7) | none needed (sources never written) | drop `engrams` |
| Mac `~/.local/share/memory-bus`, `~/.local/bin/memory-bus`, `~/Library/LaunchAgents/ai.darsh.memory-bus-sync.plist` | Client CLI + half-hourly sync (StartInterval 1800) + SSH tunnel | none (additive) | `launchctl bootout gui/$(id -u)/ai.darsh.memory-bus-sync; rm -r ~/.local/share/memory-bus ~/.local/bin/memory-bus ~/Library/LaunchAgents/ai.darsh.memory-bus-sync.plist; pkill -f 'ssh.*-L 8791:127.0.0.1:8791'` |
| `memory-bus/memory_bus/textops.py` (repo) | O(n³) MMR replaced with incremental MMR (numpy path + pure fallback); compress ranks head/tail + top-M salience; >4096 units coalesced. 4 new tests (15 → 19 OK) | `textops.py.pre-diet-20260916`, `tests/test_textops.py.pre-diet-20260916`, `install.sh.pre-diet-20260916` | `cp` backups, redeploy with `bash install.sh` |

---

## 3. Before / after numbers

Only figures present in the logs. "Probe" = one model turn; token counts are as reported by the provider.

### 3.1 Per-turn / per-session prefix

| Surface | Metric | Before | After | Delta | Source |
|---|---|---|---|---|---|
| Codex → Grok bridged | first-turn input tokens | 56,136 | 24,058 | -57.1% | SSOT §2.1; E2E-codex-grok §2c |
| Codex → Grok bridged | same, earlier same-day session (12:39, after G1-G4 only) | 56,136 | 34,951 | -37.7% | E2E-codex-grok §3 |
| Codex exec, gpt-reserve path | first-turn input tokens | 45,955 (derived) | 27,076 | -41% | G1 measurement table |
| Codex-side fixed prefix (grok-max exec) | chars | 51,750 | 32,966 | -36% | G1; E2E-codex-grok §2 |
| Codex skills_instructions | chars | 16,253 | 5,119 | -68% | E2E-codex-grok §2c |
| Codex recommended_plugins | chars | 3,208 | 764 | -76% | same |
| Codex AGENTS.md block | chars | 6,735 | 1,782 | -74% | same |
| Codex base_instructions sent to Grok | chars | 20,919 | 12,937 | -38.2% | G2; E2E-codex-grok §3 |
| Grok skills `<system-reminder>` | chars | 103,068 | 10,264 | -90% | G4; E2E-codex-grok §3 |
| Grok `inspect` skills / active hooks | count | 345 / 4 | 26 / 0 | | G4; E2E-codex-grok §3 |
| Grok direct headless probe (not bridged) | turn-1 input tokens | — | 17,038 | | G4 verify 2 |
| Claude Code `claude -p ping` | cache_creation + cache_read + input | 54,675 | 46,813 | -7,862 (-14.4%) | G3 baseline; E2E-claude-djcode §1 |
| Claude Code `claude -p ping` | after Tier A only (G3) | 54,675 | 52,439 | -2,236 | G3 |
| Claude Code skills dir | entries | 73 | 45 (+28 archived) | | G3; E2E §5 |
| Claude Code stream-json init | skills / slash_commands | 92 / 130 | 71 / 103 | | G3 A5 |
| MEMORY.md | bytes | 16,883 | 4,716 | -72% | E2E-claude-djcode §6 |
| CLAUDE.md includes (codex-astra + mohini-shared) | bytes | 3,766 + 3,569 | 909 + 1,353 | | G3 A7 |
| Codex AGENTS.md | bytes | 6,679 | 1,754 | -74% | G1 |
| grok-max advertised window (`model_context_window` in rollout) | tokens | 190,000 | 152,000 (=160k × 95%) | | E2E-codex-grok §2c |

### 3.2 Configuration state

| Item | Before | After | Source |
|---|---|---|---|
| `service_tier` in Codex turn_context | `priority` (config.toml:8) | absent (0 occurrences in rollout 01a0a93f) | E2E-codex-grok §2 |
| Codex compaction ceiling | none (≈ model window) | 110,000 total scope; `codex debug models` shows 110000 on astra/reserve/claude-max-*/grok-max | G1 |
| Claude effortLevel | high (opus-5 xhigh) | medium (opus-5 high) | G3 |
| Claude MCP declarations | 3 places incl. dead codex-astra | user=[mobbin, pencil], home local=[blender, playwright]; `claude mcp list` grep codex-astra/figma = 0 | E2E-claude-djcode §4 |
| Grok default model / effort | grok-composer-2.5-fast / (catalog high) | grok-4.6 / medium | G4 |

### 3.3 Memory bus

| Metric | Value | Source |
|---|---|---|
| Points in `engrams` | 0 → 3,162 (migration) → 4,472 (first sync) → 4,483 | G6; critic |
| Migration | agent_memory 3336 seen / 3110 ingested / 226 hash-dups, 84.2 s; super_memories 89/46/44; shared_memory 7/6/1 | G6 |
| First Mac sync | docs 514, pushed 514, chunks 1,312, dedup 260, queued_llm 152 (all drained, failed 0) | G6 |
| Idempotent re-sync | docs 515, unchanged 515, pushed 0 | E2E-memory-bus §5 |
| Recall probes (5, `--max-tokens 1200`) | 427-1,145 tokens, 8 hits each, top score 0.533-0.776, on-topic top hit 5/5 | E2E-memory-bus §3 |
| Recall wall via tunnel (healthy process) | 0.58-0.71 s CLI; raw curl 0.47-0.55 s; Kali-local 0.29-0.34 s | E2E-memory-bus §6 |
| Recall wall (degraded process P1) | 2.5-4.0 s; CLI timeouts during a compress | E2E-memory-bus §3, §7 |
| Compress `reports/codex.md` | 8,301 → 789 tokens, kept 17/308 units, 3-4 s on P2; 111.6 s on P1 | E2E-memory-bus §4 |
| Compress 3000-line probe | before fix: server 645 s (client timeout 60 s); after: 7,224 → 276 tokens, 4.08 s | G6 |
| Claude hook probe | 3,139 chars additionalContext, 1.53 s | E2E-claude-djcode §2 |
| Codex hook probe (standalone) | 3,587 chars, 3.67 s | E2E-codex-grok §2b |
| DJcode hook probe (standalone) | 3,365 chars, 0.57 s | E2E-claude-djcode §2 |
| Embedder load after restart | 0.21-0.22 s | G6; E2E-memory-bus §1 |
| Service restarts today | 13:31:53 clean, 13:43:27 SIGKILL after TimeoutStopSec=20; both recovered <15 s; NRestarts 0 | G6; E2E-memory-bus §7 |
| Kali CPU (healthy process) | 4.5% avg over 45:56 | critic |

---

## 4. Memory bus

### 4.1 What it is

A FastAPI service on Kali (`/opt/memory-bus`, `memory-bus.service`, 127.0.0.1:8791) that turns memory files and
notes into ≤400-token chunks, embeds them with `sentence-transformers/all-MiniLM-L6-v2` (384-d, cached offline,
8 torch threads) and stores them in Qdrant collection `engrams` (int8 scalar quantization q=0.99, vectors and
payload on disk, HNSW m=16). Chunks over 400 tokens get an extractive ≤120-token engram immediately; a background
worker rewrites them with Ollama `qwen2.5:3b` (32 threads, ≈7-9 tok/s) later, same point id. Point ids are
`uuid5(sha256(normalized chunk))`, so re-ingesting the same text is a no-op; credential-looking lines are scrubbed on
both ends. The reviewed mohini-memory store stays the write-side of record; the bus only reads its export. Kali has
no Tailscale address today, so the Mac reaches the bus through `ssh -N -f -L 8791:127.0.0.1:8791 root@<kali-host>`
(`memory-bus tunnel`, idempotent, re-run by the sync job). Design: `reports/memory-bus.md` §5; code: `memory-bus/`.

### 4.2 Endpoints (`reports/memory-bus.md` §5.3)

| Endpoint | Request | Response |
|---|---|---|
| `GET /health` | — | `{ok, uptime_s, model, dim, embedder_load_s, collection{points, vectors_on_disk, hnsw_m, quantization, …}, ollama{model, reachable, mode, queue, done, failed}}` |
| `POST /ingest` | `{text ≤400k, source, agent, tags[], ts?, llm: queue\|sync\|off, chunk_tokens=400, force=false, doc?}` | `{ingested, skipped, ids[], queued_llm, chunks}` |
| `POST /ingest/batch` | `{items:[…≤500]}` | `{items, ingested, skipped, queued_llm}` |
| `GET /recall` | `q, k=8 (≤64), max_tokens=1200, lambda=0.7, source, agent, tags, full, min_score, format=json\|text` | `{query, candidates, tokens, hits[{id, score, text, source, agent, ts, tags, engram_kind}], packed_text, ms}` |
| `POST /compress` | `{text ≤2M, target_tokens=800, query?, mode=tool_output\|prose, keep_head=3, keep_tail=3}` | `{text, tokens_in, tokens_out, kept, total, changed, ms}` |

Recall = query embed → Qdrant `query_points` (oversampled, rescored) → MMR (λ 0.7) → pack `- [source] text` lines
until `max_tokens`. Compress is purely extractive (sentence/line salience against the centroid, head/tail forced for
tool output, `[…]` gap markers); it never calls the LLM. Optional bearer auth via `MB_TOKEN` / `MEMORY_BUS_TOKEN`
(not enabled today).

### 4.3 Mac client

`~/.local/bin/memory-bus` (stdlib Python 3.9): `health`, `recall "q" [-k] [--max-tokens] [--source] [--json]`,
`ingest --source S [--agent A] [--file F | < stdin] [--llm queue|sync|off]`, `compress [--target N] [--mode] [< stdin]`,
`hook claude|djcode|codex`, `tunnel`. Env: `MEMORY_BUS_URL` (default `http://127.0.0.1:8791`), `MEMORY_BUS_TIMEOUT`
(hooks, default 4 s; recall uses 15 s; `compress` hard-codes 60 s — see risks).

### 4.4 How each CLI uses it

| CLI | Mechanism | Budget | Observed |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` hooks.UserPromptSubmit[1] → `memory-bus hook claude --max-tokens 1200` (timeout 6 s); stdout `{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext}}`; silent on no hit or bus down | ≤1,200 tokens per prompt | Working standalone: 3,139 chars, 1.53 s, correct top hit for "resume traderworld deploy" |
| Codex | `~/.codex/hooks.json` SessionStart → `memory-bus hook codex --max-tokens 1000`, which runs `lifecycle.py` on the same stdin, caps its context at 1,500 chars and appends recall; PostToolUse/Stop/PreCompact/SessionEnd/Interrupt still call `lifecycle.py` directly | ≤6,000 chars | Standalone 3,587 chars, 3.67 s. NOT injected in the 13:34 `codex exec` turn (section 5) |
| Grok | No hook system; `~/.grok/rules/00-memory.md` instructs `memory-bus recall "<topic>" --max-tokens 800` before substantive work and `compress --target 800` for large output; router preamble adds a 142-char memory-bus sentence to bridged sessions | 800 tokens on demand | Rule file confirmed loaded via `prompt_context.json`; no bridged turn has exercised a recall call yet |
| DJcode | `~/.djcode-config/settings.json` hooks.UserPromptSubmit → `memory-bus hook djcode --max-tokens 1000` (timeout 6 s); same schema as Claude | ≤1,000 tokens | Standalone 3,365 chars, 0.57 s; never observed inside DJcode (`-p` hangs) |

### 4.5 How to add memory

1. Preferred: save a reviewed note with the mohini-memory skill
   (`/usr/bin/python3 ~/.codex/skills/mohini-memory/scripts/memory.py save …`, see the skill's `references/commands.md`).
   The launchd job `ai.darsh.memory-bus-sync` runs every 30 min (`~/.local/share/memory-bus/bin/memory_bus_sync.py`)
   and pushes: the mohini-memory export (current revisions + INDEX.md), `~/.claude/projects/*/memory/*.md`, and
   agent-common-memory `CURRENT.md` + `sources/{alienware,kali}`. Per-source sha state lives in
   `~/.local/share/memory-bus/sync-state.json`; unchanged docs are skipped; changed docs re-ingest and dedup by hash.
2. Immediate: `python3 ~/.local/share/memory-bus/bin/memory_bus_sync.py` (or `launchctl kickstart gui/$(id -u)/ai.darsh.memory-bus-sync`).
3. Ad hoc text: `memory-bus ingest --source <label> --agent <claude|codex|grok|djcode> --file <path>` or pipe on stdin.
   Use `--llm off` for bulk, `--llm sync` only when you want the qwen engram inline (≈44 s per oversized chunk).

---

## 5. Not done, user actions, risks

Taken from the critic's verification pass; facts are reproduced as stated there.

### 5.1 Acceptance criteria not met

- D3 acceptance ceiling: `claude -p ping` prefix = 2 + 28,367 cache_creation + 18,444 cache_read = 46,813 tokens,
  1,813 over the 45,000 ceiling (run jitter ≈1.2k, so a lucky run could pass; do not count it). Why: S4 (SessionStart
  `jq --rawfile` dump of feedback_fable5_orchestrator_config.md, ~1.8k tokens) was deliberately skipped in G3 because
  the work order required preserving that injection; and four claude.ai connectors flipped from needs-auth to
  Connected between recon and apply, adding ~1-2k of tool lists. Smallest next action: replace the SessionStart hook
  command in `~/.claude/settings.json` with a one-line pointer (`memory-bus recall "fable5 orchestrator protocol"
  --max-tokens 400` returns that exact note as top hit, score 0.776) and re-probe once with `--model claude-sonnet-5`;
  that alone is projected to cross the line.
- D5 smoke turn: `djcode -p ping --output-format json` hangs indefinitely before any HTTP request (3 reproductions:
  new shim, hooks removed, clean config dir; pre-diet shim also hangs per G5 run D; mainline claude 2.1.273 with
  identical env completes in 17.2 s / 15,607 input tokens). Not caused by the diet, but "no regression in smoke"
  cannot be signed off and the memory-bus recall hook has never been observed firing inside DJcode. The bundle
  `~/.djcode-cli/bin/cli.mjs` (20.9 MB, 15 Apr) idles in the libuv FSEvents loop after
  `[ERROR] [3P telemetry] Telemetry init failed: resourceFromAttributes is not a function`. Smallest next action:
  rebuild the bundle from `~/repos/djcode_cli` HEAD (APPLY-PLAN open item 8 / DJ-P4; needs the user to lift decision
  #8) and rerun the `-p` probe; independently change the shim default `ANTHROPIC_MODEL` from gemma4 (not pulled, 404)
  to qwen2.5:1.5b or pull gemma4.
- D7 memory note: no note saved yet. Claude auto-memory dir has no token-diet file; mohini-memory `search "diet"`
  returns 0 results and INDEX.md has no diet/memory-bus entry; agent-common-memory CURRENT.md has no mention. The
  E2E-memory-bus claim that a "13:35 memory-note write" caused docs 514→515 is unsupported (the +1 doc is more likely
  one of the patches/E2E logs). Smallest next action: `memory.py` save a `working.agent-token-diet-20260916` note
  (objective, decisions, before/after numbers, open items) and add a MEMORY.md index line. This report is the
  other half of D7.
- D1/D3 memory-recall design on Codex: the new SessionStart hook (`memory-bus hook codex`) did NOT inject into the
  13:34 exec turn (0 hook developer messages in rollout 01a0a93f; the old lifecycle hook injected 674 chars in the
  10:22 exec smoke and in the still-running desktop thread 01a0a938). Standalone the command returns 3,587 chars in
  3.67 s. Codex sessions currently get zero memory context — a capability regression versus pre-diet until fixed.
  Hypothesis (unproven): `[hooks.state."…hooks.json:session_start:0:0"] trusted_hash` at config.toml:378-379
  predates the 13:32 hooks.json rewrite. Smallest next action: start one interactive `codex` in a trusted git dir,
  accept the hook trust prompt if shown, then scan the newest rollout for a developer message starting
  "Saved records below".
- D1 "prune unused plugins" residual: `<recommended_plugins>` is still injected (764 chars, down from 3,208) because
  `[features] recommended_plugins=false` does not gate the fragment in 0.154.0-alpha.6.2 (byte-identical prefixes with
  `--enable`/`--disable` per G1). Minor; likely tied to `[marketplaces.*]`. Next action only if wanted: remove/disable
  the marketplace catalogue entries and re-measure.

### 5.2 Configured but unverified

- D2(c) "Claude resume path still ≈99% cached" after the router patch: router `state/stats.sqlite` has exactly one
  Claude turn since the 10:25 restart (13:34:11 claude-max-sonnet, thread 01a0a93e, input 30,914, cached 0, resumed 0
  = a fresh session, no resume). The 99% figure comes from 08:47-08:57 sessions that predate a8c5870.
  `bound_tool_outputs`/`strip_checkpoint_images` could in principle change the prefix between turns and break the
  cache. Needs one 2-turn bridged claude-max-* probe and a look at cached_tokens on turn 2.
- D2(a) tool-output truncation and D2 checkpoint image stripping: unit-tested only; no live large
  `function_call_output` and no live compaction have passed through the patched router.
- D1 `model_auto_compact_token_limit=110000` (shown by `codex debug models`), `tool_output_token_limit=8000`,
  `project_doc_max_bytes=4096`: parse-accepted only; no session has crossed 110k and `token_count.info` in this
  build exposes no compaction field.
- D4 `[compaction.pruning]` / `[compaction.memory_flush]`: parsed by grok (configSources) but no multi-turn bridged
  session has been run; memory_flush is inert while `[memory] enabled=false`.
- D1 priority-tier removal on an actual gpt-6-astra turn: only proven absent on grok-max (turn_context) and
  gpt-reserve (router-served Claude) turns; no Astra rollout was scanned for service_tier after apply.
- D5 recall via memory-bus inside DJcode: hook JSON valid standalone, never executed by the DJcode runtime.
- D6 F1 degraded-process state (pid 2424336: 100-233% idle CPU, recall 2-4 s, compress 111 s): root cause not
  determined; the current process is healthy (4.5% CPU over 46 min) but there is no evidence it will not recur.
- D6 "5 probe queries" relevance was judged by the E2E agent, not by the user; filler hit scores are 0.36-0.45.
- G5 incident: `~/.claude/.claude.json` (inode 44006422, 13:38) and `~/.claude.json` (inode 44006464, 13:46) are
  distinct files, both 69,427 B, 83 keys, 9 projects, onboarding true. Not byte-diffed; a concurrent Claude session
  can rewrite either from stale in-memory state.

### 5.3 User actions

1. `/mcp` in Claude Code: disconnect the claude.ai connectors you do not use (Microsoft 365, Google Drive, Gmail,
   Google Calendar; Adobe alone exposes ~150 tools). All five are currently Connected and their tool lists sit in
   every prefix (~1-2k tokens on `-p`, more interactively). Decision #7 left this to you; it is the second lever
   for D3.
2. Codex: open one interactive `codex` session in a trusted git directory and accept the SessionStart hook trust
   prompt if it appears (hooks.json was rewritten 13:32; trusted_hash at `~/.codex/config.toml:378`). Then confirm a
   "Saved records below" developer message appears in the new rollout. Until then Codex sessions receive no memory
   context.
3. Decision #8 (no DJcode runtime edits): decide whether to lift it so the stale `~/.djcode-cli/bin/cli.mjs` bundle
   can be rebuilt from `~/repos/djcode_cli`; without that D5's smoke cannot pass. Also `ollama pull gemma4` or accept
   qwen2.5:1.5b as the DJcode default.
4. Figma MCP in `~/.codex/config.toml` (`[mcp_servers.figma]`) fails OAuth on every `codex exec` start (2 AuthRequired
   stderr lines per run). Re-authenticate via Codex or set `enabled=false`; no token cost, just noise.
5. G5 accidentally SIGUSR2-killed pid 6711, a 92-day-old `tsdown --config-loader unrun … --no-clean` watcher. If that
   watcher was yours, restart it.
6. Optional: confirm you want the 110k global auto-compact ceiling kept on the claude-max-* bridged entries (see
   risks) — it trades cached context for uncached compaction summaries.
7. Optional: `~/.grok/config.toml` `[mcp] max_output_bytes` was written as 20000 (documented default) rather than
   the work-order's 65536; confirm or change.

### 5.4 Risks

- `[skills] max_context_tokens=1200` is at the edge: Codex's 5,275-char skills block fits only because
  `~/.agents/skills` was archived (64 entries → 1 symlink). `~/.codex/config.toml:363` still has
  `external-agent-import-sync-item-types = "all"`, so the Codex desktop mirror-sync can recreate `~/.agents/skills`;
  when it does, the "Exceeded skills context budget … skill descriptions were removed" error item returns and ALL
  skill descriptions vanish from the model-visible list (seen in G1 smoke #1). Verified clean as of now
  (`ls ~/.agents/skills` = mohini-memory only); needs a periodic check or a config change to stop the sync.
- 110k global auto-compact ceiling on claude-max-* bridged entries: pre-diet bridged Claude sessions ran at 99% cache
  with 80-200k contexts; the bridge's compaction is one full uncached summary turn. Compacting at 110k instead of
  ~190k can cost more than it saves on long Claude threads. Unmeasured (zero post-patch Claude resume turns).
- Codex SessionStart hook not injecting = silent regression: the old lifecycle hook delivered 674 chars of reviewed
  checkpoints; the new one delivers 0 in exec. If the same happens in desktop sessions after their next restart,
  Codex loses memory continuity entirely while everything looks green (exit 0, no error item).
- memory-bus head-of-line blocking (F2): `Embedder.__call__` holds one lock; a single `/compress` call blocks every
  `/recall` and both hooks (Claude hook budget `MEMORY_BUS_TIMEOUT=4` s, DJcode 6 s) → hooks silently inject nothing
  during any compress. Seen live: 5/5 recall probes timed out during one compress on the degraded process.
- memory-bus degraded-process state (F1) with unknown cause: 23 min CPU in 11 min wall at idle, load avg 0.88 → 18.4
  on Kali; it also made `systemctl stop` need SIGKILL (F4). `Restart=always` hides it; a runaway would burn the
  40-core box until noticed.
- CLI `memory-bus compress` hard-codes timeout=60 (`bin/memory-bus` line 102) and ignores `MEMORY_BUS_TIMEOUT`; on a
  slow server it fails as "unreachable", which reads like a tunnel outage.
- Recall packs pad to k=8 with weak hits (0.36-0.45 scores in probes 2 and 3); every hook injection carries ~300
  tokens of filler, directly against the ≤1.2k-token design goal. A ~0.45 score floor is a one-line change.
- Claude Code SessionStart still injects the full feedback_fable5_orchestrator_config.md via `jq --rawfile` on every
  session (~1.8k tokens); this is the item that keeps D3 over the ceiling.
- Grok skills reminder is still 10,264 chars per turn (21 bundled skills + mohini-memory symlink survives
  `[skills] ignore`) and the router's `available_tools` block is 43,678 chars — the single largest item on the Grok
  path now; xAI reported 0 cache on the fresh session so both are paid every turn.
- Grok default model changed grok-composer-2.5-fast → grok-4.6 (served as grok-4.6-build) because composer left
  the catalog; per-token price of the replacement was not compared. Direct probe cost $0.0117 for 17k tokens.
- Two compaction layers now stack on the Grok path (Codex 110k total-scope ceiling; Grok
  auto_compact_threshold_percent=30 of 500k = 150k). Codex should always compact first, but the interaction is
  untested.
- DJcode: isolation leaks when cwd=$HOME (project-level `.claude/` for ~ IS `~/.claude/`, so its
  skills, commands, settings.json and settings.local.json with 367 allow rules are still read/watched; from any other
  cwd the isolation holds); and the shim default `ANTHROPIC_MODEL=gemma4` 404s on the local Ollama (only
  qwen2.5:1.5b pulled), so the default will still fail once the hang is fixed.
- G5 run C clobbered `~/.claude/.claude.json` to a 151 B stub (restored from
  `~/.claude/backups/.claude.json.backup.1789546095542`); the two .claude.json files are separate inodes and were
  not diffed.
- Router catalog is now generated (`models.base.json` → `state/models.json`). Any future hand edit to
  `state/models.json` is overwritten on restart; base is the source of truth (verified base carries 160k/110k, no
  tiers).
- The E2E memory-bus log attributes a +1 doc and two unit restarts to "another actor" by journal client ports; if
  those were the orchestrator's own probes, the restart accounting in the SSOT should say so. Restarts during E2E
  invalidated the first round of timing numbers and could recur if two workstreams share the unit.

---

## 6. How to verify later (copy-pasteable; model probes cost one small turn each)

### Codex (D1)

```sh
B=/Applications/ChatGPT.app/Contents/Resources/codex
$B exec --strict-config -C /tmp 'x' --json </dev/null 2>&1 | head -3      # expect no "unknown configuration field"
wc -c -w ~/.codex/AGENTS.md                                                 # expect ≤2560 bytes
ls ~/.agents/skills                                                         # expect: mohini-memory only (risk: mirror-sync)
cd ~ && $B exec -m grok-max -s read-only -C /tmp --skip-git-repo-check 'Reply with the single word OK.' --json </dev/null \
  | grep -o '"input_tokens":[0-9]*' | head -1                               # expect ≤40000 (was 24058)
R=$(ls -t ~/.codex/sessions/2026/*/*/rollout-*.jsonl | head -1)
grep -c '"service_tier"' "$R"; grep -c 'Saved records below' "$R"          # expect 0 ; expect ≥1 once the hook is trusted
```

### Router (D2)

```sh
cd ~/repos/codex-max-router && git log --oneline -1 && git merge-base --is-ancestor a8c5870 HEAD && echo has-token-diet
.venv/bin/python -m unittest test_router 2>&1 | tail -3                    # expect OK (63 at 3db023f)
curl -s 127.0.0.1:18740/health | jq .status
python3 -c "import json;d=json.load(open('models.base.json'));print([(m['slug'],m.get('context_window'),m.get('auto_compact_token_limit'),'service_tiers' in m) for m in d['models'] if m['slug'] in ('grok-max','claude-max-sonnet','gpt-6-astra')])"
sqlite3 state/stats.sqlite "select ts,model,input_tokens,cached_tokens from requests order by ts desc limit 5"   # check cached_tokens on a Claude resume turn
grep -n '2026-09-16 (token diet)' CHANGELOG.md
```

### Claude Code (D3)

```sh
cd ~ && claude -p "ping" --output-format json --model claude-sonnet-5 --max-turns 1 \
  | jq '.usage | .input_tokens + .cache_creation_input_tokens + .cache_read_input_tokens'   # target ≤45000 (was 46813)
jq '{effortLevel, modelSettings, autoCompactWindow, bashOutputMaxChars, env, hasMcpServers: has("mcpServers")}' ~/.claude/settings.json
jq -c '.hooks.UserPromptSubmit[].hooks[].command' ~/.claude/settings.json    # expect the memory-bus hook line
claude mcp list 2>&1 | grep -ciE 'codex-astra|figma'                         # expect 0
ls ~/.claude/skills | wc -l; ls ~/.claude/skills-archive | wc -l             # expect 45 / 28
echo '{"prompt":"resume traderworld deploy","session_id":"t","cwd":"~","hook_event_name":"UserPromptSubmit"}' \
  | ~/.local/share/memory-bus/bin/memory-bus hook claude --max-tokens 1200 | jq -r '.hookSpecificOutput.additionalContext' | wc -c
```

### Grok (D4)

```sh
cd ~/repos/codex-max-router/state/grok-work && ~/.grok/bin/grok inspect --json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d['skills']),len([h for h in d['hooks'] if not h.get('disabled')]),d['configSources'])"   # expect 26 0 …config.toml
S=$(ls -t ~/.grok/sessions/*codex-max-router*grok-work*/ | head -1)        # newest bridged session id
~/.grok/bin/grok usage "$S" 1                                                # turn 1 inputTokens < 40000
wc -c ~/.grok/rules/00-memory.md                                             # 640
```

### DJcode (D5)

```sh
~/.djcode/bin/djcode --version                                               # 1.0.0 (DJcode)
grep -c '^export' ~/.djcode/bin/djcode                                       # 8
jq '.hooks.UserPromptSubmit[0].hooks[0].command' ~/.djcode-config/settings.json
cd /tmp && ANTHROPIC_MODEL=qwen2.5:1.5b perl -e 'alarm 90; exec @ARGV' ~/.djcode/bin/djcode -p ping --output-format json --max-turns 1 </dev/null
# currently hangs (exit 142); passes only after the bundle rebuild
```

### Memory bus (D6)

```sh
ssh root@<kali-host> 'systemctl is-active memory-bus; systemctl is-enabled memory-bus; ps -o %cpu= -p $(systemctl show -p MainPID --value memory-bus); curl -s 127.0.0.1:8791/health | jq -c "{ok,points:.collection.points,q:.collection.quantization,disk:.collection.vectors_on_disk,m:.collection.hnsw_m,ollama:.ollama}"'
ssh root@<kali-host> 'curl -s 127.0.0.1:6333/collections/engrams | jq -c ".result.config.quantization_config,.result.config.params.vectors.on_disk,.result.config.hnsw_config.m"'
memory-bus tunnel && memory-bus health | jq .ok
time memory-bus recall "router" --max-tokens 300 >/dev/null                  # expect < 2 s (0.6 s healthy)
for q in "traderworld phase 4 deploy" "codex max router claude cache resume" "nyayaforge vakalat cutover" "fable 5 orchestrator protocol subagents" "kali qdrant ollama models"; do
  memory-bus recall "$q" --max-tokens 1200 --json | jq -c '{tokens,hits:(.hits|length),top:.hits[0].source,score:.hits[0].score}'; done
memory-bus compress --target 800 < ~/repos/agent-token-diet/reports/codex.md | wc -c   # ≈3.2 KB, stderr shows tokens 8301 -> ~789
python3 ~/.local/share/memory-bus/bin/memory_bus_sync.py                    # second run: pushed_items=0
launchctl print gui/$(id -u)/ai.darsh.memory-bus-sync | grep -E 'runs|last exit|run interval'
# restart check (only when nothing else is using the bus):
ssh root@<kali-host> 'systemctl restart memory-bus && sleep 15 && curl -sf 127.0.0.1:8791/health >/dev/null && echo survives-restart'
```

### Memory note (D7)

```sh
/usr/bin/python3 ~/.codex/skills/mohini-memory/scripts/memory.py search "agent-token-diet"   # expect ≥1 hit once the note is saved
grep -n 'token-diet\|token diet' ~/.claude/projects/-Users-darshjme/memory/MEMORY.md
```

Critic confidence on the verified numbers: 0.88 (inferences only: the hook trusted_hash hypothesis, the F1 cause,
the S4 projection).
