# Codex Max Router

Adds Claude and Grok entries to the existing Codex model catalog:

| Codex model | Bridged CLI model |
| --- | --- |
| Claude Fable 5.1 · Max (`claude-max-fable`) | Claude Code `claude-fable-5-1` |
| Claude Opus 5 · Max (`claude-max-opus`) | Claude Code `claude-opus-5` |
| Claude Opus 4.8 · Max (`claude-max-opus-48`) | Claude Code `claude-opus-4-8` |
| Claude Sonnet 5 · Max (`claude-max-sonnet`) | Claude Code `claude-sonnet-5` |
| Grok 4.6 · grok.com (`grok-max`) | Grok CLI `grok-4.6` |

Claude runs through the installed, unmodified Claude Code binary using its own
signed-in account. Grok runs through the installed, unmodified Grok CLI using
its grok.com login. Model names are pinned rather than passed as moving
aliases, so the two Opus entries stay distinct.

Each entry exposes Codex's own reasoning-effort slider. The selected effort is
passed to Claude Code as `--effort`, which accepts `low`, `medium`, `high`,
`xhigh`, and `max`. Codex rungs that Claude Code does not name are clamped to
the nearest supported level. Requests without an effort leave Claude's own
default in place. `xhigh` and `max` raise the bridge timeout to 10 minutes.

Each Claude turn also reports Claude Max usage. Claude Code emits a per-run
`rate_limit_event` with the 5-hour and 7-day window utilization and reset times;
the router translates that into the `x-codex-primary-*` (session) and
`x-codex-secondary-*` (weekly) used-percent / window-minutes / reset-at
headers that Codex reads from each response, on both the HTTP and WebSocket
paths. Codex's separate account usage poll still goes directly to
chatgpt.com and reflects the OpenAI account, not Claude.

Claude entries carry the same Codex capabilities as Astra: images, web search,
voice, sub-agents, computer/browser use, dynamic tools, and skills/plugins.

The router listens on `127.0.0.1:18740`. The existing OpenAI provider and Astra
default remain selected. Ordinary OpenAI traffic is forwarded to the original
ChatGPT Codex endpoint. An explicit Astra quota-exhaustion response, before any
model output, switches that request to Claude Opus. Ordinary rate limits,
authentication failures, and failures after partial output do not trigger replay.

## Use

Fully quit and reopen the Codex desktop app, start a fresh task, and select
one of the Claude or Grok entries in its existing model picker, then set effort
with the same slider Codex uses for its own models.

The router runs as the per-user launchd service `ai.darsh.codex-max-router`.
Its health endpoint is `http://127.0.0.1:18740/health`.

## How it works

The model catalog is supplied with Codex's `model_catalog_json` option and
traffic is routed with `openai_base_url`. The app binary is not patched.
Claude Code and Grok CLI emit a structured message/tool-call envelope. The
router translates it into Responses events, and **Codex executes the tools
under Codex's own permissions**. Built-in tools, plugins, and MCP servers on
those CLIs are disabled for bridge calls, except Grok Imagine (`image_gen`,
`image_edit`, `image_to_video`, `reference_to_video`) and native web
search/fetch when Codex declares search. The router never reads or copies
Claude or Grok login tokens. Incoming OpenAI authorization headers go only to
the fixed OpenAI endpoint.

Claude summaries use a marked base64 checkpoint in the Responses protocol's
`encrypted_content` field. These router checkpoints are **not encrypted** and
must not be mistaken for OpenAI-issued encrypted state. The router itself logs
no prompts; Claude Code keeps its usual local session transcripts for resumed
threads (see Token use). Bounded continuation history remains in process memory.

## Codex capabilities on Claude entries

**Images.** Screenshots, pasted images, and image tool results are forwarded to
Claude Code as native image blocks over `--input-format stream-json`. Each image
is replaced in the JSON transcript by a numbered `[image N]` placeholder, in
order, so the model can tell which message an image belongs to. This covers
computer use and browser use, whose tool results are screenshots.

**Voice.** Codex voice is OpenAI's own speech model, which delegates the actual
work to the thread's selected model. Its endpoints (`/live` WebRTC offer and the
`/realtime` WebSocket) are relayed verbatim to the fixed upstream, so voice works
on a Claude thread and the work is done by Claude.

**Web search.** Codex's search backend (`/alpha/search`) is relayed to the fixed
upstream, so `web.run` keeps working. When a request declares a search tool
directly instead, Claude Code's own `WebSearch`/`WebFetch` (or Grok's
`web_search`/`web_fetch`) are enabled for that call; all other tools still
return to Codex for execution.

**Grok Imagine.** On `grok-max`, Grok CLI keeps `image_gen`, `image_edit`,
`image_to_video`, and `reference_to_video`. Image and video generation therefore
uses the signed-in grok.com Imagine account, not Codex/OpenAI image tools.
Stills are copied into the router media directory and inlined in the Codex
turn; videos are saved as files and linked by absolute path. Video still
starts from an image: Grok generates a frame, then animates it.

**Sub-agents, dynamic tools, skills, plugins, MCP.** These reach the model as
ordinary Codex tool declarations (including `additional_tools` added mid-thread)
and are executed by Codex under Codex's permissions. The bridge allows four
concurrent Claude calls and keeps 256 continuations for two hours so a fan-out of
sub-agents does not evict each other's context.

**Token use.** Codex re-sends the whole thread on every sampling request,
including one per tool call. The bridge keeps one CLI session (Claude Code or
Grok CLI) per Codex thread and, when a request continues the previous response,
resumes that session and sends only the new items. For Claude the rest of the
transcript is then a prompt-cache read rather than a fresh upload; xAI did not
report cache hits in testing, so on Grok the saving is the avoided re-upload of
tool declarations and transcript duplication rather than a cache discount. Tool
declarations are sent once per session. All bridged entries advertise a 200k
context window so Codex compacts long threads instead of growing them toward 1M
tokens per turn. If a turn names a tool that was not declared, the bridge
corrects the model inside the same session rather than failing the turn and
letting Codex retry from scratch. Session transcripts live under
`~/.claude/projects/` and `~/.grok/sessions/` for this bridge's working
directories and are pruned after 48 hours when the service starts.

**Luna reserve.** When the ChatGPT account's advanced-model quota is exhausted,
Codex desktop hides its model picker and sends every turn as `gpt-reserve`. The
router serves those turns with a bridged model of your choice instead of
OpenAI's reserve model, and reports `gpt-reserve` back so the app stays happy.
The app's pill still reads "Luna reserve"; the first turn of each thread says
which model actually answered. Choose the model:

- In any thread, send `model: opus-48` (aliases: `fable`, `opus`, `opus-48`,
  `sonnet`, `grok`, or a full slug). The router answers immediately and applies
  it to that thread's reserve turns.
- Global default: open <http://127.0.0.1:18740/> and pick one. Falls back to
  Codex's own `model` key in `~/.codex/config.toml` if it names a bridged model,
  then to Claude Opus 4.8.

Choices persist in `state/reserve.json`. Nothing in the app is patched and no
account state is altered; OpenAI's reserve model is simply never called.

**OpenAI-encrypted checkpoints.** A thread compacted while on an OpenAI model
carries state only OpenAI can read. The router asks the upstream once per
checkpoint for a plain-text handoff summary, using the request's own
authorization, caches it in memory, and hands Claude a readable checkpoint. If
that conversion is unavailable, Claude is told the earlier context is missing
rather than the turn failing.

## Limits

- Audio, video, and file attachment inputs are still rejected explicitly.
- Checkpoint conversion needs a working OpenAI model; without one, the visible
  messages are all Claude receives.
- Claude quota exhaustion surfaces as a bridge error with Claude's own wording.
  There is no automatic fallback from Claude to an OpenAI model.
- Claude output is buffered until its structured turn completes. This adds
  latency and may use more quota than a direct Claude Code session.
- Native server-side OpenAI capabilities are not implemented by Claude.
- The desktop app may require a restart to reload its catalog and endpoint.
  The running GUI itself could not be inspected because its automation access
  is blocked; the native app-server model list was verified instead.
- Failover responds to backend quota errors. It cannot override a desktop UI
  that blocks a request before sending it; select Claude directly in that case.
- The catalog is a snapshot of the native models installed during setup.
  Revisit it after major Codex model updates.
- Claude Code falls back to another model when its own safety guardrails refuse
  a request. The bridge reports the Codex slug that was selected; check the
  Claude Code session records if you need the model that actually answered.

## Local development and rollback

Requires Python 3.9+, the installed official Claude Code executable, and the
installed Grok CLI (`~/.local/bin/grok`) for the Grok picker entry.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest -v test_router
.venv/bin/python install.py install
```

To remove the router's two configuration keys and stop its service, preserving
unrelated settings and leaving source/state/backups on disk:

```sh
.venv/bin/python install.py uninstall
```

The original configuration backup is under
`~/.codex/backups/codex-max-router/`. The installer records only its own changes
in `state/installation.json` and refuses to overwrite conflicting configuration.

## Reference checked

The user supplied [duolahypercho/codex-router](https://github.com/duolahypercho/codex-router).
Its current README explicitly separates Claude subscription agent bridges from
the model picker and offers API-key Claude models separately. That checkout was
inspected, not installed or modified. This smaller local integration addresses
the user's specific native-picker requirement through their own Claude Code.
