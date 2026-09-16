# Darshj's Codex Router API

Base URL `http://127.0.0.1:18740`. The router binds loopback only, speaks JSON,
and mounts the dashboard API under `/api/v1`. Every path below is relative to
that prefix unless it starts with `/dashboard` or is listed under
"Codex-facing endpoints". Shapes are taken from `dashboard_api.py` and
`stats.py`.

## Quick start

```sh
export DCR_TOKEN=$(cat ~/repos/darshj-codex-router/state/dashboard-token)

# 1. Is the router up, and what does it know? (no auth)
curl -s http://127.0.0.1:18740/api/v1/health | python3 -m json.tool

# 2. The last 24 hours, in total and per provider and model
curl -s -H "Authorization: Bearer $DCR_TOKEN" \
  'http://127.0.0.1:18740/api/v1/stats/summary?range=24h' | python3 -m json.tool

# 3. Make Claude Fable 5.1 the reserve default
curl -s -X PUT -H "Authorization: Bearer $DCR_TOKEN" -H 'Content-Type: application/json' \
  -d '{"reserve": {"model": "claude-max-fable"}}' \
  http://127.0.0.1:18740/api/v1/settings | python3 -m json.tool
```

`install.py token` prints the token and creates the file (mode 0600) if it
does not exist yet. The router also creates it at first start.

## Authentication

Two credentials are accepted on protected endpoints:

- `Authorization: Bearer <token>`, where `<token>` is the content of
  `state/dashboard-token`. Accepted from any client; use this from scripts.
- Cookie `dcr_session`, set by `POST /auth/login`. Its value is
  `HMAC-SHA256(key=token, msg="dcr")` as hex; `HttpOnly`, `SameSite=Strict`,
  `Path=/`, 30 days. A cookie session is same-origin only: when an `Origin` or
  `Referer` header is present its host must be `127.0.0.1` or `localhost`,
  otherwise `403 forbidden`; when neither header is present, `GET`/`HEAD`/
  `OPTIONS` are allowed and mutating methods are refused. The dashboard uses
  this.

Public, no credential needed: `GET /health`, `POST /auth/login`,
`GET /auth/status`. Everything else without a valid credential returns
`401 {"error": {"code": "unauthorized", "message": "..."}}`.

Rotating the token (`POST /auth/rotate`) rewrites the file; the old token and
every cookie derived from it stop working at once.

Request bodies are JSON objects of at most 64 KB (`413 payload_too_large`
above that, `400 invalid_json` for anything that is not a JSON object).

## Errors

Every error is JSON with the matching HTTP status:

```json
{"error": {"code": "invalid_settings", "message": "reserve.model must be one of: claude-max-fable, ..."}}
```

| Status | `code` | When |
| --- | --- | --- |
| 400 | `invalid_json`, `invalid_range`, `invalid_bucket`, `invalid_parameter`, `invalid_settings`, `bad_request` | body or query fails validation |
| 401 | `unauthorized` | no or wrong credential; wrong token on login |
| 403 | `forbidden` | cookie session from a foreign origin |
| 404 | `not_found` | unknown route, unknown thread override, missing dashboard file |
| 405 | `method_not_allowed` | wrong method on a known path |
| 413 | `payload_too_large` | body over 64 KB |
| 429 | `locked_out` | login locked; `Retry-After` header carries the seconds |
| 500 | `catalog_error`, `internal_error` | catalog unreadable; unexpected failure (see router log) |
| 502 | `ollama_refresh_failed` | discovery raised |
| 503 | `stats_unavailable` | stats store not open |

## Endpoints

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/health` | public |
| POST | `/auth/login` | public |
| GET | `/auth/status` | public |
| POST | `/auth/logout` | token |
| POST | `/auth/rotate` | token |
| GET | `/stats/summary` | token |
| GET | `/stats/timeseries` | token |
| GET | `/requests` | token |
| GET | `/usage` | token |
| GET | `/models` | token |
| GET | `/settings` | token |
| PUT | `/settings` | token |
| DELETE | `/settings/reserve/threads/{thread}` | token |
| POST | `/ollama/refresh` | token |
| GET | `/dashboard/`, `/dashboard/app.js`, `/dashboard/styles.css` | public static |

### GET /health

Auth: public. Liveness plus the facts the dashboard header needs. `counts`
are the router's counters since the service started; `install.py` waits on
this endpoint after loading the service.

```json
{
  "status": "ok",
  "version": "1.0.0",
  "uptime_s": 5321,
  "counts": {"claude": 42, "grok": 3, "openai": 118, "fallback": 0, "errors": 1},
  "reserve_model": "claude-max-opus-48",
  "ollama": {"enabled": true, "base_url": "http://127.0.0.1:11434", "online": true, "models": 2}
}
```

### POST /auth/login

Auth: public. Body `{"token": "<dashboard token>"}`. On success sets the
`dcr_session` cookie, clears the failure counter and returns:

```json
{"ok": true}
```

Errors: `401 unauthorized` for a wrong token. After 10 failures within 10
minutes (per router process) every attempt returns `429 locked_out` with a
`Retry-After` header until the window passes.

### GET /auth/status

Auth: public; never errors. `method` is `"bearer"`, `"cookie"` or `null`.

```json
{"authenticated": true, "method": "cookie"}
```

### POST /auth/logout

Auth: token. Clears the `dcr_session` cookie.

```json
{"ok": true}
```

### POST /auth/rotate

Auth: token. Writes a fresh 32-byte hex token to `state/dashboard-token`,
records a `token_rotated` event, and returns the new value exactly once. A
cookie-authenticated caller (the dashboard) receives the new cookie in the
same response so it stays signed in; every other cookie is now invalid.

```json
{"token": "9f1c...64 hex characters...e2"}
```

### GET /stats/summary

Auth: token. Query `range=1h|24h|7d|30d` (default `24h`). The current window
plus the equal-length window before it (for deltas). `totals`, every
`providers` entry and every `models` entry share one block shape; `latency`
values are milliseconds, ratios are fractions, `previous` has no `models`.

```json
{
  "range": "24h",
  "since": 1789345701.4, "until": 1789432101.4,
  "totals": {
    "requests": 163, "errors": 1, "error_ratio": 0.0061,
    "input_tokens": 2402550, "cached_tokens": 1890210, "fresh_tokens": 512340, "output_tokens": 48120,
    "tool_calls": 97, "resumed": 135, "resumed_ratio": 0.8282, "cache_hit_ratio": 0.7867,
    "active_threads": 9,
    "latency": {"avg": 8420.5, "p50": 6100, "p95": 21400}
  },
  "providers": {"claude": {"requests": 42, "...": "same block"}, "openai": {"...": "same block"}},
  "models": {"claude-max-fable": {"requests": 30, "...": "same block"}},
  "previous": {"since": 1789259301.4, "until": 1789345701.4,
               "totals": {"...": "same block"}, "providers": {"...": "..."}}
}
```

`input_tokens` is the total prompt size; `cached_tokens` the part served from
the CLI's prompt cache; `fresh_tokens` the difference. `cache_hit_ratio` is
`cached_tokens / input_tokens`.

### GET /stats/timeseries

Auth: token. Query `range` as above and `bucket=auto|<seconds>` (default
`auto`: 1h → 60 s, 24h → 900 s, 7d → 3600 s, 30d → 21600 s; explicit values
must be 10 to 604800). `buckets` lists every aligned bucket start in the
window (the last one is the current, partial bucket); `rows` is sparse, one
entry per bucket and provider that saw traffic, and every `t` appears in
`buckets`.

```json
{
  "range": "1h", "bucket_s": 60, "since": 1789428540, "until": 1789432101.4,
  "buckets": [1789428540, 1789428600, "..."],
  "providers": ["claude", "openai"],
  "rows": [
    {"t": 1789428600, "provider": "claude", "requests": 6, "errors": 0,
     "input_tokens": 109440, "cached_tokens": 91200, "output_tokens": 2210},
    {"t": 1789428600, "provider": "openai", "requests": 3, "errors": 0,
     "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
  ]
}
```

### GET /requests

Auth: token. Query `limit` (1 to 500, default 50), `before=<id>` for the
next page, `provider` (lowercase `[a-z0-9_-]`, e.g. `claude`, `grok`,
`ollama`, `openai`, `router`), `status=ok|error`. Newest first.
`next_before` is the cursor for the following page, or `null` on the last
one.

```json
{
  "requests": [
    {
      "id": 4812, "ts": 1789432101.4,
      "thread": "01a0a858-183d-7aa1-9211-c0e0f67bc38b",
      "model_requested": "claude-max-fable", "model_served": "claude-max-fable",
      "provider": "claude", "kind": "turn", "status": "ok", "error": null,
      "latency_ms": 7310, "input_tokens": 812, "cached_tokens": 41230, "output_tokens": 640,
      "resumed": 1, "tool_calls": 2, "path": "/responses"
    }
  ],
  "limit": 50,
  "next_before": 4763
}
```

`kind` is `turn`, `compact`, `command` (an in-chat `model:` command answered
by the router), `passthrough` (relayed to OpenAI) or `checkpoint`. `resumed`
is 1 when the turn continued an existing Claude Code or Grok CLI session.
`error` carries the bridge's error text on `status: "error"` rows. Here
`input_tokens` is the fresh part only (the router subtracts `cached_tokens`
before recording).

### GET /usage

Auth: token. The newest usage row per `(provider, window)`, as recorded from
each Claude turn's `rate_limit_event`, plus the router's counters.
`utilization` is a fraction, `resets_at` a Unix timestamp or `null`.

```json
{
  "windows": [
    {"id": 311, "ts": 1789432101.4, "provider": "claude", "window": "five_hour",
     "utilization": 0.42, "resets_at": 1789435200},
    {"id": 312, "ts": 1789432101.4, "provider": "claude", "window": "seven_day",
     "utilization": 0.18, "resets_at": 1789900800}
  ],
  "counts": {"claude": 42, "grok": 3, "openai": 118, "fallback": 0, "errors": 1},
  "generated_at": 1789432160.2
}
```

### GET /models

Auth: token. Every entry of the generated catalog with the router's view of
it. `provider` is `claude`, `grok`, `ollama` or `openai`. `online` means: the
Claude Code or Grok CLI executable exists, the Ollama server answered at the
last refresh, or always true for OpenAI entries.

```json
[
  {"slug": "claude-max-fable", "display_name": "Claude Fable 5.1 · Max", "provider": "claude",
   "context_window": 200000, "input_modalities": ["text", "image"], "supports_search_tool": true,
   "is_reserve_default": false, "online": true},
  {"slug": "ollama-llama3.2-3b", "display_name": "Llama3.2 3b · Ollama", "provider": "ollama",
   "context_window": 131072, "input_modalities": ["text"], "supports_search_tool": false,
   "is_reserve_default": false, "online": true}
]
```

Errors: `500 catalog_error` if `state/models.json` cannot be read.

### GET /settings

Auth: token. `reserve.model` is the stored default (`null` if none was ever
set); `reserve.effective` is what a reserve turn would use right now (stored
default, else Codex's own `model` key if it names a bridged model, else
`claude-max-opus-48`).

```json
{
  "reserve": {
    "model": "claude-max-opus-48",
    "threads": {"01a0a858-183d-7aa1-9211-c0e0f67bc38b": "claude-max-fable"},
    "effective": "claude-max-opus-48"
  },
  "ollama": {"enabled": true, "base_url": "http://127.0.0.1:11434"},
  "catalog_path": "/Users/you/repos/darshj-codex-router/state/models.json"
}
```

### PUT /settings

Auth: token. Body: any subset of `{"reserve": {"model", "threads"}, "ollama":
{"enabled", "base_url"}}`; unknown fields are rejected. The whole body is
validated before anything is applied. `reserve.model` and every
`reserve.threads` value must be a bridged slug; `ollama.enabled` must be a
boolean; `ollama.base_url` must be an `http(s)` URL whose host is `127.0.0.1`
or `localhost`. Reserve changes persist to `state/reserve.json` (event
`reserve_changed`), Ollama changes to `state/settings.json` (event
`settings_changed`). Returns the full object exactly as `GET /settings`.

```sh
curl -s -X PUT -H "Authorization: Bearer $DCR_TOKEN" -H 'Content-Type: application/json' \
  -d '{"ollama": {"enabled": true, "base_url": "http://127.0.0.1:11434"}}' \
  http://127.0.0.1:18740/api/v1/settings
```

Errors: `400 invalid_settings` with a message naming the offending field; an
empty body is also `400`.

### DELETE /settings/reserve/threads/{thread}

Auth: token. Removes one per-thread reserve override; that thread goes back
to the default. Records `reserve_changed`.

```json
{"ok": true, "thread": "01a0a858-183d-7aa1-9211-c0e0f67bc38b"}
```

Errors: `404 not_found` if the thread has no override.

### POST /ollama/refresh

Auth: token. Re-discovers Ollama models, regenerates `state/models.json` and
records an `ollama_refresh` event. Each model carries what discovery learned
plus its slug. Codex only reads the catalog at app start, hence `note`.

```json
{
  "models": [
    {"name": "llama3.2:3b", "slug": "ollama-llama3.2-3b", "size": 2019393189, "family": "llama",
     "parameter_size": "3.2B", "context_length": 131072, "vision": false}
  ],
  "catalog": "/Users/you/repos/darshj-codex-router/state/models.json",
  "note": "Restart Codex to see new models"
}
```

An unreachable Ollama server yields `"models": []` and a catalog without
Ollama entries; that is not an error. `502 ollama_refresh_failed` only when
discovery itself raises.

### GET /dashboard/

Public static files, allow-listed: `/dashboard/` serves
`dashboard/index.html`; `/dashboard/app.js` and `/dashboard/styles.css` their
files. Anything else under `/dashboard/` is a JSON `404 not_found`; there is
no directory listing and no path traversal. Responses carry
`Cache-Control: no-store`. The router redirects `/`, `/select` and
`/dashboard` (no trailing slash) to `/dashboard/`. The page calls the
endpoints above with cookie auth.

## Vocabulary

| Field | Values |
| --- | --- |
| `provider` | `claude`, `grok`, `ollama`, `openai`, `router` (in-chat commands answered by the router itself) |
| `kind` | `turn`, `compact`, `command`, `passthrough`, `checkpoint` |
| `status` | `ok`, `error` |
| `window` | `five_hour`, `seven_day` (Claude Max windows) |
| event kinds | `startup`, `reserve_changed`, `settings_changed`, `ollama_refresh`, `token_rotated` |
| `range` | `1h`, `24h`, `7d`, `30d` |

## Codex-facing endpoints

These exist for the Codex app, not for scripts. They require Codex's own
`Authorization: Bearer` header (forwarded to the upstream, never stored)
unless noted, and they reject any request whose `Host` is not loopback.

| Path | Purpose |
| --- | --- |
| `GET /health` (root, no prefix) | the original health JSON; public; `status`, `version`, `models`, `counts`, `reserve_model`, `ollama` |
| `GET /models` (root) | the generated catalog, as Codex fetches it |
| `POST /responses`, WebSocket `/responses` | the Responses API; bridged models run through Claude Code, Grok CLI or Ollama, other models are relayed |
| `POST /responses/compact` | compaction; bridged models produce a router checkpoint |
| `POST /responses/input_tokens` | relayed |
| `/live`, `/realtime`, `/alpha/search` | relayed verbatim (voice and Codex's search backend) |
| `POST /select` | same-origin form that sets the reserve default; kept for compatibility, redirects to `/dashboard/` |

The upstream for relayed paths is fixed at
`https://chatgpt.com/backend-api/codex`; the router is not a general proxy.
