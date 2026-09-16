# Changes

## 2026-09-16 (luna reserve)

- An unknown `previous_response_id` (router restart or eviction) is now
  reported in OpenAI's wire shape (`404`, `previous_response_not_found`) before
  any ack, so Codex resends the full thread instead of failing after retries.
  Verified with a router restart in the middle of a four-tool-call task.
- The in-chat `model:` command keeps the thread's history and tool declarations
  behind it and carries the live session forward when the model is unchanged
  (a first cut had dropped them, leaving the next turn without tools).
- `gpt-reserve` turns from Codex desktop's Luna reserve mode are served by a
  bridged model of the user's choice (default Claude Opus 4.8) and reported
  back as `gpt-reserve`, with a first-turn notice naming the real model.
- Model selector: in-chat `model: <fable|opus|opus-48|sonnet|grok>` per thread,
  and a same-origin-only page at `http://127.0.0.1:18740/` for the default;
  choices persist in `state/reserve.json`.
- Fixed the HTTP streaming path overwriting the response id after the early
  ack, which broke `previous_response_id` continuation there too.

## 2026-09-16 (grok parity)

- Grok threads now keep one Grok CLI session per Codex thread and resume it
  with only the new items, matching the Claude path. Tool declarations are
  sent once; undeclared tool names are corrected inside the session.
- `grok-max` advertises a 200k context window so Codex compacts long threads.
- Grok Imagine attachments are limited to media produced during the current
  turn; the copy destination is no longer rescanned, which had re-attached
  every earlier image to every later answer.
- Grok CLI session directories older than 48 hours are pruned at service start.

## 2026-09-16 (token use)

- One Claude Code session per Codex thread: continuation requests resume it and
  send only the new items, so the transcript prefix is a prompt-cache read
  instead of a full upload every turn. Tool declarations are sent once.
- Fixed the WebSocket early-ack overwriting the response id, which had made
  every `previous_response_id` miss and forced a cold full send per turn.
- Claude catalog entries now advertise a 200k context window so Codex compacts
  instead of growing threads toward 1M tokens per request.
- An undeclared tool name (for example `functions.exec_command`) is corrected
  inside the same session rather than failing the turn for Codex to retry.
- Bridge session transcripts older than 48 hours are pruned at service start.

## 2026-09-16 (capabilities)

- Images now reach Claude as native image blocks (stream-json stdin) with
  numbered `[image N]` placeholders in the transcript, so screenshots from
  computer use and browser use work. Catalog entries advertise image input.
- Voice works on Claude threads: `/live` and `/realtime` are relayed verbatim to
  the fixed upstream instead of 404ing (this had broken voice for every model).
- Web search works: `/alpha/search` is relayed, and a directly declared search
  tool enables Claude Code's own WebSearch/WebFetch for that call.
- OpenAI-encrypted checkpoints are converted once to a readable summary through
  the upstream and cached, instead of failing the turn.
- Continuation history raised to 256 entries / 2 hours and concurrency to 4 for
  Codex sub-agent fan-out.

## 2026-09-16 (hang)

- Codex HTTP zstd bodies no longer 500 the turn when the frame omits a
  content size or is already JSON.
- Bridged WebSocket turns ack `response.created` immediately so Codex does not
  retry while Grok/Claude run.
- Grok bridge disables inherited Claude/Cursor MCP, uses `--no-leader`, and
  accepts structured output from the result text when `structured_output` is
  missing.

## 2026-09-16 (imagine)

- Grok picker entries now run Grok Imagine natively: `image_gen`, `image_edit`,
  `image_to_video`, and `reference_to_video` on the signed-in grok.com account.
- Generated stills and clips are copied into the router media directory and
  inlined in the Codex turn (image data URLs; video as saved paths). Codex
  tools stay disabled for Imagine; only Imagine and optional web search run
  inside Grok CLI.

## 2026-09-16 (grok)

- Added a native Codex catalog entry for Grok 4.6 (`grok-max`) through the
  signed-in Grok CLI, using the same Responses envelope as the Claude bridge.
- Grok native tools stay disabled except `web_search`/`web_fetch` when Codex
  declares search. Effort, images, checkpoints, and Codex tool dispatch match
  the Claude path. Grok.com login is used; API keys are not read.

## 2026-09-16 (later)

- Surfaced Claude Max usage per turn: Claude Code's `rate_limit_event`
  (5-hour and 7-day windows) is mapped onto Codex's `x-codex-primary-*` and
  `x-codex-secondary-*` usage headers on HTTP responses and stream events.
- Bridge errors now carry Claude Code's real failure text (for example
  `401 OAuth access token is invalid` or a usage limit) instead of the bare
  result subtype, which previously read as `Claude Code failed: success`.
- Added Claude Fable 5.1 and split Opus into separate Opus 5 and Opus 4.8
  catalog entries, pinning exact Claude Code model names instead of aliases.
- Mapped Codex's reasoning-effort slider onto Claude Code's `--effort` flag for
  every Claude entry, clamping unsupported rungs and extending the timeout at
  `xhigh` and `max`.

## 2026-09-16

- Added native Codex catalog entries for Claude Opus and Sonnet through Max.
- Added a loopback WebSocket/HTTP Responses bridge using official Claude Code.
- Preserved native GPT models and default provider; added guarded quota fallback.
- Added external Codex tool dispatch, conversation checkpoints, cancellation,
  bounded continuation history, reversible configuration installation, and a
  per-user launchd service.
