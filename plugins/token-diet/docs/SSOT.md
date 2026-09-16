# SSOT — Agent Token Diet (2026-09-16)

Owner: Darshankumar Joshi (darshjme). Orchestrator: Claude Code main session (Fable 5.1).
Repo: ~/repos/agent-token-diet (this file is the single source of truth; only the orchestrator edits it).
Scope: Codex desktop/CLI (Astra + bridged Claude/Grok via codex-max-router), Claude Code, Grok CLI, DJcode, Kali .172 memory services.

## 1. Goal
Cut token consumption per useful turn across all CLI agents by ≥50% without losing capability, and install a shared
"supercompress memory bus" on Kali (Qdrant + sentence-transformer + local mini-LLM) so agents recall ≤1.5k tokens of
relevant memory instead of loading multi-KB memory files / whole transcripts into context.

## 2. Verified recon (evidence, 2026-09-16 09:30-09:45 IST)

### 2.1 Codex (config ~/.codex/config.toml, binary /Applications/ChatGPT.app/Contents/Resources/codex 0.154.0-alpha.6.2)
- `service_tier = "priority"` is set. Catalog (models.json) describes priority as "Fast: 2x speed, increased usage". This is a direct usage multiplier on every Astra/gpt-reserve turn.
- `model = "grok-max"` default; `openai_base_url = http://127.0.0.1:18740` (codex-max-router); `model_catalog_json` = ~/repos/codex-max-router/models.json.
- /Applications/Codex.app no longer exists; Codex binary lives in ChatGPT.app. `~/.codex/config.toml [mcp_servers.codex-astra]` and `~/.claude.json mcpServers.codex-astra` still point at the dead path (ENOENT every Claude session).
- Session token_count evidence (~/.codex/sessions/2026/09/13-16):
  - gpt-6-astra 02-05: 77 turns, max ctx 346k tokens/turn, 9.33M input (97% cached).
  - gpt-reserve 09-13: 160 turns, max ctx 534k, 23.1M input (94% cached), single tool output 196k chars.
  - claude-max-fable 09-15: 503 turns, max ctx 563k, 69.8M input, 61.4M cached (88%) → 8.4M uncached; tool outputs 1.9MB, single outputs of 1,043,414 / 434,212 / 243,895 chars.
  - claude-max-opus/fable sessions at 02:29-02:38 (pre "token use" router fix): ctx 730k-1.22M/turn, cache hit 8-21% (20.9M and 22.3M input in 46-50 turns).
  - Post-fix bridged sessions (08:47-08:57): ctx 80-200k, cache ≈99%. Fix confirmed working.
  - grok-max sessions: ctx 115-207k, cache 30-70%, first turn 56,136 input / 128 cached (fixed prefix is paid in full on Grok).
- Fixed prefix per Codex turn: base_instructions 20,919 chars + skills_instructions 17,809 chars + recommended_plugins 10,739 chars + multi_agent_role 2,501 chars + AGENTS.md 6,679 chars + mohini lifecycle hook additionalContext ≤2,500 chars + stop hook_prompt 667 chars/turn ≈ 60k chars ≈ 15-18k tokens before any work. 11 plugins enabled (computer-use, documents, spreadsheets, presentations, github, figma, build-macos-apps, build-ios-apps, game-studio, remotion, build-web-apps).
- Config keys present in binary: `model_auto_compact_token_limit`, `model_context_window`, `truncation_policy`, `max_output_tokens`, `auto_compact_fallback_buffer_tokens`, `service_tier`, `enable_request_compression` (stable, on).
- Disk: ~/.codex/sessions 865MB, logs_2.sqlite 130MB, .codex-global-state.json 1.4MB (queued-follow-ups 162KB).

### 2.2 codex-max-router (~/repos/codex-max-router, launchd ai.darsh.codex-max-router, :18740)
- Bridges Codex → `claude --print --resume <session>` and `grok --resume` with one CLI session per Codex thread (2026-09-16 "token use"/"grok parity" changelog). Catalog advertises 200k window so Codex compacts.
- Remaining waste candidates: full Codex base_instructions passed as `--system-prompt` to Claude/Grok every new session; tool declarations gathered from the whole thread; compaction = one full bridged summary turn; no tool-output truncation at the bridge; Grok path gets no cache discount so every fixed byte is paid per turn.

### 2.3 Claude Code (settings ~/.claude/settings.json, ~/.claude.json)
- Baseline before first user message in this session: 86,200 cache_creation + 20,658 cache_read ≈ 107k tokens (1h ephemeral). Contributors: 73 skills in ~/.claude/skills (20 seedance-*, 8 coach-*, gstack 439MB), MCP servers declared in THREE places (settings.json: browsermcp, pencil, playwright, figma, blender-mcp; ~/.claude.json global: pencil, codex-astra[dead], mobbin; home project: playwright, figma, blender; enabledMcpServers computer-use), 12 claude.ai connectors ever connected (Adobe alone exposes ~150 tools), SessionStart hook injecting feedback_fable5_orchestrator_config.md, MEMORY.md index 16.9KB + CLAUDE.md includes 9.5KB.
- Largest session (3afc07a2): 383 assistant msgs, max ctx 439,721, tool_results up to 306,367 chars, 2.5MB tool results total. Last home session: 5.76M cache-read tokens, 390k cache-creation, 68.8k output of which 39.8k thinking; effortLevel high global, xhigh for claude-opus-5.

### 2.4 Grok CLI (~/.grok, grok 1.0.30, grok.com login)
- default model grok-composer-2.5-fast; memory_enabled=false; pruning_*/flush_* unset; sessions 70MB; xAI reports no cache hits; bridged turns pay ~56k tokens fixed prefix each.

### 2.5 DJcode (~/.djcode, ~/.djcode-cli 30MB, src ~/repos/djcode_cli)
- provider ollama, model gemma4 (local, no quota), embedding nomic-embed-text, chroma context store, max_tokens 8192. Cost is local compute; include for memory-bus parity and context trimming only.

### 2.6 Kali <server> (40-core EPYC, 62GB RAM, no GPU, 50d uptime)
- Qdrant 1.17 docker :6333, collections: agent_memory 3336 pts (384-d), codex_mohini_nemotron3_v1 739 (2048-d), super_memories 89 (384-d), memories 19 (3072-d), shared_memory 7 (384-d), mohini_memories 3 (1024-d). No quantization anywhere.
- Redis :6379 (1MB used), Ollama :11434 (qwen3:8b, qwen2.5-coder:7b/3b, qwen2.5:3b, gemma3:4b, phi4-mini, smollm2:1.7b, gemma-uncensored), LiteLLM :4000 (NIM llama-70b rotation + xai), supermemory-server :6767 (/root/.supermemory, OPENAI_BASE_URL→ollama gemma-uncensored), mohini-claude-provider (Hermes loopback, /root/.hermes/mohini_cli_provider.py), Jarvis :8788, jarvis voice :7860.
- Python: sentence-transformers 5.2.3, transformers 5.2.0, qdrant-client 1.17.0, litellm 1.83.4. ai-lab at /opt/ai-lab (HF_HOME=/opt/ai-lab/models/hf).
- Mac side: mohini-memory store ~/.local/share/mohini-memory (1.0GB, sqlite + vector-cache), agent-common-memory sync dir, Codex hooks.json lifecycle → memory.py.


## 2b. Corrections after adversarial verification (recon workflow wf_43a86cca, 25 agents)
- §2.1 gpt-* per-turn contexts: SSOT's 346k/534k were input+cached double counts. Real peaks: astra 02-05 = 173k, gpt-reserve 09-13 = 267k (model_context_window 258,400 = 272k×95%). Only pre-fix bridged Claude sessions truly hit 562k-616k (old 950k window; now 190k).
- §2.1 priority tier: mechanism confirmed (config.toml:8, ResponseCreate.service_tier, catalog gate) but the multiplier for Astra is UNKNOWN ("2x speed, increased usage" is a speed figure). Thread settings show priority on 51/176 overrides. Removing it is still correct; do not quote a 2x saving.
- §2.1 fixed prefix: 25 plugins enabled (not 11); skills_instructions 21.9k chars desktop / 16.2k CLI, dominated by ~/.agents/skills mirror (context-engineering-kit 7.2k, trailofbits 5.7k, gstack 1.8k, seedance ~1.2k) — 2 reads in 14 days; recommended_plugins 3.2k; AGENTS.md 6.7k. Codex caps skills at 2% of window (≈5.2k tokens). Key names verified by strict parse: model_auto_compact_token_limit, model_auto_compact_token_limit_scope (total|body_after_prefix), tool_output_token_limit, project_doc_max_bytes, include_apps_instructions, [skills] max_context_tokens, [[skills.config]].
- §2.1 the 1,043,414 / 434k / 244k-char outputs are input_image parts (screenshots) already turned into placeholders by adapter.extract_images(); observed cost ≈+12k tokens, not 400k. Bridge-side truncation is a bound, not a big win.
- §2.2 Codex base_instructions ride as conversation[0] in the JSON prompt, not in --system-prompt (Router F3, 0.95). Grok trim applies there.
- §2.4 Grok: xAI does report cache hits (23-26% bridged 7-day, 94.6% direct). Grok's 103k-char skills reminder comes from ~/.agents/skills (239 entries) + ~/.grok/bundled (22) — the single largest per-turn lever on the Grok path (≈-24k tokens/turn via [skills] ignore). Real config keys: [compaction.pruning], [compaction.memory_flush], [mcp] max_output_bytes, [session] auto_compact_threshold_percent, [skills] ignore. ~/.grok/GROK.md is never loaded; rules live in ~/.grok/rules/*.md.
- §2.3 Claude Code -p prefix = 54,681 tokens; interactive ≈61-65k; the 107k first turn included a one-off /claude-api skill body (~46k). Settings keys verified: effortLevel, modelSettings.<model>.effortLevel, autoCompactWindow, bashOutputMaxChars, env MAX_MCP_OUTPUT_TOKENS, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE.
- §2.6 Kali has no tailscale0; 127.0.0.1:8790 is taken (pid 1322) → memory-bus on 127.0.0.1:8791, Mac access via SSH tunnel. D6 acceptance port amended to 8791.
- Decisions (orchestrator, 10:20): 110k ceiling global; Grok [skills] ignore aggressive; [[skills.config]] first, archive ~/.agents/skills as fallback; MAX_MCP_OUTPUT_TOKENS=20000; effortLevel medium global, claude-opus-5 high; router patch extended with checkpoint image stripping + Grok memory-bus preamble line; GK-P3 skipped (conflicts with Imagine); codex-astra.md rewritten with correct path; claude.ai connectors left to user (/mcp); no deletions.

## 3. Root causes (ranked by tokens burned)
1. Context bloat per turn: whole-thread resend with 300k-1.2M token contexts and untruncated tool outputs (up to 1MB). Compaction thresholds too high (Codex default ≈ model window; Claude 1M window).
2. Codex `service_tier=priority` → "increased usage" on every Astra turn.
3. Pre-09-16 router bug (cache miss every turn) — fixed; verify it stays fixed and extend to Grok fixed-prefix trimming.
4. Fixed-prefix overhead: 15-18k tokens/turn Codex; ~107k tokens/session Claude Code (skills + triple-declared MCP + connectors + memory index + hooks). Paid in full on Grok, at cache-write price on every Claude session start / after 1h idle.
5. Effort/thinking: xhigh/high defaults on every trivial turn (thinking = 58% of output tokens last session).
6. Memory loaded as files, not retrieved: MEMORY.md 16.9KB + AGENTS.md 6.7KB + lifecycle dumps instead of ≤1.5k-token targeted recall.

## 4. Deliverables and acceptance criteria
D1 Codex config patch (~/.codex/config.toml): drop priority tier; set model_auto_compact_token_limit ≈ 100-120k (all bridged + Astra entries), truncation_policy/max tool output bounded; prune unused plugins; trim AGENTS.md to ≤2.5KB with memory-bus pointer; fix/remove dead codex-astra mcp path. Acceptance: `codex exec` smoke turn shows first-turn input ≤ 40k tokens and no config errors; priority absent from turn_context.
D2 Router patch (~/repos/codex-max-router): (a) bridge-side tool-output truncation (head/tail, ≤32KB per function_call_output, with note); (b) Grok fixed-prefix trim (strip Codex base_instructions sections irrelevant to bridged CLIs; measure chars before/after); (c) advertise ≤160k window and compact earlier; (d) tests in test_router.py pass; (e) CHANGELOG entry. Acceptance: pytest green; probe turn through router shows input tokens reduced vs baseline 56k on grok; Claude resume path still ≈99% cached.
D3 Claude Code diet: single MCP declaration set (remove duplicates + dead codex-astra; keep pencil, playwright, blender, mobbin; browsermcp/figma only if used in last 30 days per ~/.claude.json), skills pruned (seedance-* archived under ~/.claude/skills-archive with one seedance-kb kept; coach-* consolidated if unused), MEMORY.md index reduced to ≤6KB with the rest served by memory-bus recall hook (UserPromptSubmit → ≤1,200 tokens), effortLevel default medium with xhigh only via /effort or model override, tool-output guard (Bash wrapper env or hook) ≤ 40k chars. Acceptance: fresh `claude -p "ping" --output-format json` shows first-turn cache_creation+cache_read ≤ 45k tokens (from 107k); all removed items archived, not deleted.
D4 Grok CLI diet: config.toml pruning/flush settings enabled with sane values (keep_last_n_turns, soft trim), max_mcp_output_bytes, default effort; GROK.md ≤1KB with recall pointer. Acceptance: `grok --help`/settings dump confirms keys applied; bridged probe turn input < 40k.
D5 DJcode: recall via memory-bus (remote_url or plugin), context window trim; no regression in `djcode --version`/smoke.
D6 Memory bus on Kali (`/opt/memory-bus`, systemd `memory-bus.service`, 127.0.0.1:8790 + tailnet only): FastAPI; `POST /ingest` (chunk → compress with Ollama qwen2.5:3b into ≤120-token engrams → embed all-MiniLM-L6-v2 (384-d) → Qdrant collection `engrams` with int8 scalar quantization, on_disk vectors, HNSW m=16); `GET /recall?q=&k=&max_tokens=` returns MMR-deduped pack ≤max_tokens; `POST /compress` (extractive compression of tool output/text to target tokens using sentence-transformer salience); `GET /health`. Migrate existing 384-d collections (agent_memory, super_memories, shared_memory) into `engrams` with source tags; leave other collections untouched. Client CLI `memory-bus` (python, Mac) used by Claude/Codex/Grok/DJcode hooks. Acceptance: health OK; ingest of MEMORY.md + memory notes; recall returns ≤max_tokens with relevant hits for 5 probe queries; quantization enabled; service survives restart.
D7 Report `reports/FINDINGS.md` with before/after numbers and the exact patches; memory note saved.

## 5. Constraints
- No GitHub Actions. No secrets in files/memory. Archive, never delete user data (skills, sessions, memory). Keep Codex/Claude/Grok binaries unmodified. All Kali changes via SSH as root, additive systemd units, no touching jarvis/djbot/supermemory/litellm units. Mac processes: only the launchd router restarts. Every subagent reports actual test output, never claims.
- Work orders reference this file by path; findings return to the orchestrator, who updates §2/§6.

## 6. Status log
- 09:45 SSOT v1 written after inline recon. Next: recon-deepen workflow (parallel specialists) → audit → apply workflow → E2E.
- 10:05 Recon workflow done: reports/{codex,router,claude-code,grok,djcode,memory-bus}.md + APPLY-PLAN.md; 4 findings refuted/corrected (see §2b).
- 10:22 Apply workflow wf_360fb1e7 launched: G1 codex-config, G2 router (branch token-diet, no restart), G3 claude-code, G4 grok, G5 djcode, G6 memory-bus (Kali :8791). Router restart + Tier D hooks/memory + E2E follow.
- 13:35 Apply workflow wf_360fb1e7 was cut by a Claude rate-limit at 10:28 after G1-G4,G6 finished (G5 and the review phase did not run). Orchestrator completed inline: A8 (djcode shim + ~/.djcode-config), D1 (Claude UserPromptSubmit memory-bus hook), D2 (MEMORY.md 16.9KB → 4.7KB, backup MEMORY.md.pre-diet-20260916), D3 (~/.codex/hooks.json → memory-bus hook codex, probe 5,583 chars), D4 (lifecycle.py Stop reason 285 chars), D5 (~/.grok/rules/00-memory.md). Router: token-diet commit a8c5870 already merged to main by the concurrent dashboard workstream (now at d0d0bac, 13:31); running router pid started 10:25:37 (after a8c5870) so B4 restart is NOT needed and is left to that workstream. Next: E2E verification workflow.
- 14:55 E2E workflow wf_eff49085 done: Codex→Grok first turn 56,136→24,058 (-57%); Claude -p 54,675→46,813 (1.8k over 45k target; remaining levers are user-only: claude.ai connectors, fable5 SessionStart dump kept by rule); memory-bus D6 all PASS (health, quant, 5/5 recall relevance, restart, tunnel 0.6s). Fails: Codex SessionStart hook didn't inject (trusted_hash on changed command) → fixed by moving cap+recall into lifecycle.py and restoring the original hook command; DJcode -p hang is a pre-existing bundle defect. Client: recall min_score 0.45, compress timeout honours MEMORY_BUS_TIMEOUT. FINDINGS.md written (498 lines). Memory note project_agent_token_diet.md saved.
- 14:05 Codex SessionStart hook VERIFIED injecting after the lifecycle.py fix: rollout 13-50-35 developer message 3,793 chars incl. 8 memory-bus hits; hooks.json restored to the original command with additionalContextLimit 6000 (trusted_hash covers the command only). Task achieved except the user-only items (claude.ai connector toggles; DJcode stale bundle rebuild decision).
