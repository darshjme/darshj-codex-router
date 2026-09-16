# SSOT — Darshj's Codex Router (rebrand + dashboard + Ollama)

Owner: orchestrator (main session). Only the orchestrator edits this file.
Date: 2026-09-16. Repo: `~/repos/codex-max-router` → renamed at the end to
`~/repos/darshj-codex-router`. Python 3.9 (`.venv/bin/python`), aiohttp 3.13,
stdlib `sqlite3`, `unittest`. No new runtime dependencies without a note here.
Git initialised at baseline commit `95cec45`. GitHub Actions stay disabled.

## 0. Product

**Darshj's Codex Router** (short: DCR). A loopback Responses router at
`127.0.0.1:18740` that puts Claude (Max), Grok (grok.com) and local Ollama
models inside the Codex desktop app's native model picker, with a web
dashboard (stats, models, requests, usage, settings, API) styled after
fxnex.us. Existing behaviour (Claude/Grok bridges, sessions, reserve routing,
voice/search passthrough, checkpoints) must keep working unchanged — 51 tests
in `test_router.py` are the regression gate and must stay green.

## 1. Module boundaries (one specialist per box; do not edit other boxes)

| Box | Files (create/own) | Specialist |
|---|---|---|
| A. Ollama provider | `ollama.py`, `test_ollama.py` | Prometheus |
| B. Stats store + dashboard API | `stats.py`, `dashboard_api.py`, `test_dashboard_api.py` | Prometheus |
| C. Dashboard front-end | `dashboard/index.html`, `dashboard/app.js`, `dashboard/styles.css` | Kamadeva + Prometheus |
| D. Rebrand, installer, docs | `install.py`, `README.md`, `CHANGELOG.md`, `docs/API.md`, `com.darshj.codex-router.plist` template inside install.py | Saraswati + Vayu |
| Integration (router.py, adapter.py, models.base.json, catalog generation) | orchestrator only | — |

Specialists MUST NOT edit `router.py`, `adapter.py`, `models.json`,
`test_router.py`. Integration points are declared in §6; implement against
those interfaces exactly. Every box ships its own tests and runs them:
`.venv/bin/python -m unittest <module>`.

## 2. Brand

- Name: **Darshj's Codex Router**. Tagline: "Every model you pay for, in one picker."
- Wordmark: `DCR` monogram square (lime `#e4f222` on ink) + "Darshj's Codex Router" in Sora 600.
- launchd label: `ai.darshj.codex-router` (old: `ai.darsh.codex-max-router`).
- Repo/dir: `darshj-codex-router`. GitHub: `darshjme/darshj-codex-router` (public).
- Port stays `18740`. Config keys stay `openai_base_url` + `model_catalog_json`.

### fxnex.us design tokens (extracted from live CSS, 2026-09-16)

```
--canvas: #f8f9f3   --paper: #fafbf7   --paper-2: #edf0e8   --paper-3: #e9efda
--ink: #191919      --ink-2: #20261e   --ink-3: #23291d     --ink-btn: #263220
--lime: #e4f222     --lime-soft: #d9ef70  --lime-2: #e4f35b  --lime-tint: #c9df60
--sage: #8a9282     --sage-2: #929787  --muted: #777        --muted-2: #999
--border: #d7ddc9   --border-2: #d9dace  --border-dark: #53624a
--white: #fff       --red-700: #bf000f  --red-50: #fef2f2  --red-200: #ffcaca
fonts: display "Sora" 600/700; ui "Plus Jakarta Sans" 500/600; body "Inter" 400/500
       (Google Fonts link allowed; ALWAYS give system fallbacks)
radii: buttons 7px · cards 12px · chips/inputs 5px · avatars 50%
buttons: lime bg, ink text, 13px/600, padding 12px 18px, hover translateY(-1px)
dark tone panels: bg --ink-3, text --paper, borders --border-dark
```

Dark mode: canvas `#20261e`, paper `#23291d`, text `#f8f9f3`, muted `#8a9282`,
lime accent unchanged. Respect `prefers-color-scheme` AND a manual toggle
stored in `localStorage` (`dcr-theme`).

### Chart palette (validated with the dataviz validator — do not change)

Series are **providers in fixed order**: 1 Claude, 2 OpenAI, 3 Grok, 4 Ollama.
```
light (surface #f8f9f3): #3f7a1f, #2a6bd6, #c24e14, #7a4fb8
dark  (surface #20261e): #63a634, #4f89ea, #e06d30, #a67cd8
status: good #3f7a1f · warning #b8860b · serious #c24e14 · critical #bf000f (icon + label, never colour alone)
```
Rules: one axis per chart, thin marks (bars 4px rounded ends, 2px lines,
2px surface gaps), legend for ≥2 series + direct labels ≤4, hover tooltip on
every plot, text in ink tokens never series colour, table view available.

## 3. Runtime layout

```
state/                       (0700, gitignored)
  reserve.json               existing
  stats.sqlite               NEW — stats store (box B)
  dashboard-token            NEW — 32-byte hex, 0600, created at startup if missing
  models.json                NEW — generated catalog = models.base.json + Ollama entries
  claude-work/ grok-work/    existing CLI cwds
models.base.json             tracked base catalog (renamed from models.json)
dashboard/                   static assets served at /dashboard/* (box C)
```
Codex reads `model_catalog_json` from a FILE at app start. The router
regenerates `state/models.json` at startup and on `POST /api/v1/ollama/refresh`;
install.py points `model_catalog_json` at `state/models.json`. New Ollama
models appear in Codex after the app is restarted (dashboard says so).

## 4. Box A — Ollama provider (`ollama.py`)

Base URL default `http://127.0.0.1:11434` (settings override). Pure asyncio +
aiohttp; no CLI. All functions accept an `aiohttp.ClientSession`.

```python
async def discover(session, base_url) -> list[dict]
    # GET /api/tags → [{'name': 'llama3.2:3b', 'size': int, 'family': str, 'parameter_size': str,
    #                    'context_length': int|None, 'vision': bool}]  (context/vision via POST /api/show per model:
    #                    model_info['<arch>.context_length'], capabilities contains 'vision')
    # Unreachable server → [] (never raises).
def slug_for(name) -> str          # 'llama3.2:3b' → 'ollama-llama3.2-3b'   (Codex slug: [a-z0-9.-], lowercase)
def display_for(name) -> str       # 'llama3.2:3b' → 'Llama3.2 3b · Ollama'
def catalog_entry(template: dict, model: dict) -> dict
    # copy of template (a claude-max-* entry from models.base.json) with slug/display_name/description,
    # input_modalities ['text'] or ['text','image'], supports_search_tool False,
    # context_window = min(context_length or 8192, 131072), max_context_window same, priority 9.
async def run_ollama(request, session, base_url, model_name, timeout=300, session_id=None, resume=False, delta=None) -> dict
    # Same contract as adapter.run_claude: uses adapter.translate(request, agent='Ollama', host='Ollama',
    # search_tools='none') for system/prompt/schema/tools/images; ignores resume/delta (stateless, sends full transcript).
    # POST /api/chat {model, stream:false, format: schema, options:{num_ctx: min(ctx, 32768)},
    #   messages:[{role:'system',content:system},{role:'user',content:prompt,images:[base64...]}]}
    # Parse message.content as JSON → adapter.structured_response-compatible output. Use adapter.uid, adapter.BridgeError,
    # adapter.UndeclaredTool semantics (raise UndeclaredTool for unknown tool names; router handles). Build the response dict
    # exactly like adapter.structured_response does (id, object, created_at, status, model=request['model'], output, usage
    # from prompt_eval_count/eval_count, rate_limit_headers {}), or call adapter.structured_response with a synthetic
    # result record {'type':'result','subtype':'success','is_error':False,'structured_output':<parsed>} and records=[].
    # Errors: connection refused → BridgeError('Ollama is not running at <url>'); HTTP error → BridgeError with body text;
    # non-JSON content → one retry with an appended "Return only the JSON object" user message, then BridgeError.
```
Tests (`test_ollama.py`, aiohttp TestServer as fake Ollama): discovery incl.
unreachable, slug/display, catalog entry fields, run_ollama happy path with a
tool call, image forwarding as base64, non-JSON retry, connection refused.

## 5. Box B — Stats store + dashboard API

### `stats.py`
SQLite at `state/stats.sqlite`, WAL, stdlib `sqlite3`, all writes via
`loop.run_in_executor`. Schema (create if missing, `PRAGMA user_version=1`):
```
requests(id INTEGER PK, ts REAL, thread TEXT, model_requested TEXT, model_served TEXT,
         provider TEXT,            -- claude|grok|ollama|openai|router
         kind TEXT,                -- turn|compact|command|passthrough|checkpoint
         status TEXT,              -- ok|error
         error TEXT, latency_ms INTEGER, input_tokens INTEGER, cached_tokens INTEGER,
         output_tokens INTEGER, resumed INTEGER, tool_calls INTEGER, path TEXT)
events(id PK, ts REAL, kind TEXT, detail TEXT)          -- startup|reserve_changed|ollama_refresh|token_rotated
usage(id PK, ts REAL, provider TEXT, window TEXT, utilization REAL, resets_at INTEGER)  -- from Claude rate_limit_event
```
API:
```python
class Stats:
    def __init__(self, path); async def open(self); async def close(self)
    async def record_request(self, **fields)          # fire-and-forget safe
    async def record_event(self, kind, detail='')
    async def record_usage(self, provider, window, utilization, resets_at)
    async def summary(self, since_ts) -> dict          # totals + per provider + per model: requests, errors, tokens (in/cached/out),
                                                        # latency avg/p50/p95, resumed_ratio, cache_hit_ratio, tool_calls
    async def timeseries(self, since_ts, bucket_s) -> list[{'t': bucket_start, 'provider': .., 'requests': n, 'errors': n, 'input_tokens': n, 'cached_tokens': n, 'output_tokens': n}]
    async def recent(self, limit=50, before_id=None, provider=None, status=None) -> list[dict]
    async def latest_usage(self) -> list[dict]         # newest row per (provider, window)
    async def active_threads(self, since_ts) -> int
    async def prune(self, keep_days=90)
```

### `dashboard_api.py`
`def build(router, stats, static_dir) -> aiohttp.web.Application` (a sub-app
mounted by the orchestrator at `/api/v1` and `/dashboard`). `router` exposes
(already exist): `router.reserve`, `router.reserve_target({})`,
`router.set_reserve_model(model, thread=None)`, `router.counts`,
`router.catalog` (Path), `router.state` (Path), `router.display_name(slug)`,
`adapter.MODELS`, `adapter.CLAUDE_MODELS`, `adapter.GROK_MODELS`, and NEW
(orchestrator adds): `router.ollama_models` (dict slug→name), `router.settings`
(dict persisted at `state/settings.json`: `{'ollama': {'enabled': bool, 'base_url': str}}`),
`await router.refresh_ollama()` → dict `{'models': [...], 'catalog': path}`,
`router.token_path` (Path to dashboard-token).

Auth: token from `state/dashboard-token` (create 0600 random 32-byte hex if
missing). Accept `Authorization: Bearer <token>` OR cookie `dcr_session`
(value = token HMAC-signed with itself is fine: `hmac.new(token, b'dcr', sha256).hexdigest()`),
HttpOnly, SameSite=Strict, Path=/, 30 days. Public (no auth): `GET /api/v1/health`,
`POST /api/v1/auth/login {token}` (429 after 10 failures/10 min per process),
`GET /api/v1/auth/status` → `{authenticated: bool}`. Everything else 401 JSON
`{error:{code:'unauthorized'}}`. Same-origin only for cookie auth (Origin/Referer
must be `http://127.0.0.1:18740` or localhost) — reject others 403.

Endpoints (JSON):
```
GET  /api/v1/health                      {status, version, uptime_s, counts, reserve_model, ollama:{enabled, base_url, online, models:n}}
POST /api/v1/auth/login {token}          sets cookie → {ok:true}
POST /api/v1/auth/logout                 clears cookie
GET  /api/v1/auth/status
POST /api/v1/auth/rotate                 new token written to file → {token} (shown once)
GET  /api/v1/stats/summary?range=1h|24h|7d|30d       (default 24h)
GET  /api/v1/stats/timeseries?range=&bucket=auto      (auto: 1h→1m, 24h→15m, 7d→1h, 30d→6h)
GET  /api/v1/requests?limit=50&before=<id>&provider=&status=
GET  /api/v1/usage                       latest windows per provider + Codex counts
GET  /api/v1/models                      [{slug, display_name, provider, context_window, input_modalities, supports_search_tool, is_reserve_default, online}]
GET  /api/v1/settings                    {reserve:{model, threads:{}}, ollama:{enabled, base_url}, catalog_path}
PUT  /api/v1/settings                    partial update of the same shape; validates; persists; records event
POST /api/v1/ollama/refresh              → {models:[...], catalog: path, note:'Restart Codex to see new models'}
DELETE /api/v1/settings/reserve/threads/{thread}
GET  /dashboard/                          serves dashboard/index.html; /dashboard/app.js, /dashboard/styles.css static (no directory listing)
```
Errors: `{error:{code, message}}` with proper status. Tests with a fake router
object (SimpleNamespace) covering auth (401/403/cookie/bearer/rotate/lockout),
every endpoint's happy path, validation errors, static serving.

## 6. Integration points (orchestrator implements after boxes land)

- `router.py`: `Stats` opened at startup; `record_request` called in
  `bridged()` (provider, latency, usage from response['usage'], resumed,
  tool_calls = count of *_call items), in passthrough (provider openai, kind
  passthrough), in reserve command path (kind command, provider router) and
  errors. `record_usage` from `response['rate_limit_headers']`-adjacent data:
  adapter already parses Claude `rate_limit_event` → expose utilization windows
  on the response as `response['usage_windows']` (orchestrator).
- `/` → redirect to `/dashboard/`. Old selector page removed (dashboard Models
  tab replaces it; `/select` POST kept for compatibility).
- `MODELS` gains Ollama slugs at startup (`adapter.MODELS.update(...)`) and
  `provider_of(slug)`; `bridged()` dispatches to `ollama.run_ollama` for
  `ollama-*`.
- Catalog: `models.base.json` + Ollama entries → `state/models.json`.

## 7. Box C — Dashboard front-end (vanilla, no framework, no CDN JS)

Single page app in `dashboard/index.html` + `app.js` + `styles.css`, fonts via
Google Fonts `<link>` (Sora, Plus Jakarta Sans, Inter) with fallbacks. Fetches
`/api/v1/*` with `credentials: 'same-origin'`. Layout: left rail (wordmark,
nav: Overview · Models · Requests · Usage · Settings · API), top bar (range
picker 1h/24h/7d/30d, theme toggle, health dot), content.

- **Login view** when `/auth/status` is false: token field, "Where is my
  token?" → `cat ~/repos/darshj-codex-router/state/dashboard-token`.
- **Overview**: stat tiles (Requests, Fresh tokens, Cached tokens, Output
  tokens, Cache hit %, Errors, Active threads, p95 latency) with delta vs the
  previous equal period; timeseries chart (stacked bars by provider, single
  axis, hover tooltip, legend + direct labels, table toggle); provider
  breakdown list; recent requests (10).
- **Models**: cards grouped by provider (Claude · Grok · Ollama · OpenAI
  native); each shows display name, slug (copyable), context, modalities,
  status dot; "Set as reserve default" button; reserve section: current
  default + per-thread overrides table with delete; Ollama section: base URL,
  enabled toggle, Refresh button (shows returned note), model list.
- **Requests**: table (time, provider, model, kind, status, latency, tokens
  in/cached/out, resumed, tool calls, thread short id); filters provider/status;
  "Load more" cursor; error rows show the error text on expand.
- **Usage**: Claude 5h/7d windows as meters (from `/usage`), Codex counts,
  Grok/Ollama request counts; reset times relative + absolute.
- **Settings**: token rotate (shows new token once, copy), theme, Ollama
  settings, catalog path, "restart Codex" hint.
- **API**: endpoint list with method chips + copyable curl using
  `Authorization: Bearer $DCR_TOKEN`; links to `docs/API.md`.
- Empty states, loading skeletons, error toasts with "what to do next".
  Keyboard reachable, visible focus, AA contrast, `aria-live` for toasts.
  Responsive ≥ 360px (rail collapses to a top bar).
- Charts hand-drawn SVG per §2 palette; tooltips as HTML overlay.
- Provide `dashboard/mock.js`?? NO — instead the front-end must run against
  the real API; box B's fake router + TestServer can serve it for manual QA.

## 8. Box D — Rebrand, installer, docs

- `install.py`: `install` boots out old label `ai.darsh.codex-max-router` if
  loaded and removes its plist; writes `ai.darshj.codex-router.plist`
  (ProgramArguments: `.venv/bin/python router.py --catalog state/models.json
  --base-catalog models.base.json --port 18740`, WorkingDirectory = repo,
  KeepAlive, logs to state/); sets `model_catalog_json` to
  `<repo>/state/models.json`; `token` prints the dashboard token (creating it
  if missing); `uninstall` reverses; `migrate` moves an old install's state.
- `README.md` (≤ 500 lines): what it is, install, picker, dashboard (with
  screenshots section placeholder paths `docs/img/*.png`), Ollama, reserve
  routing, token use, API pointer, privacy/security model, rollback. Keep the
  accurate technical content that exists today (session resume, passthroughs,
  checkpoints) — rewrite, don't drop.
- `docs/API.md`: every endpoint from §5 with request/response examples.
- `CHANGELOG.md`: new top section "2026-09-16 (rebrand + dashboard)".
- `LICENSE` MIT (darshjme).

## 9. Acceptance (E2E by orchestrator)

1. `unittest` green for all modules; `test_router.py` unchanged and green.
2. Service restarted under new label; `curl /api/v1/health` ok.
3. Dashboard loads at `http://127.0.0.1:18740/dashboard/`, login with token,
   every tab renders real data after a `codex exec` run through the router;
   screenshot each tab (light + dark) into `docs/img/`.
4. Ollama: with a fake server in tests; if `brew install ollama` + a ≤1GB
   model is feasible within 10 minutes, real E2E via `codex exec --model
   ollama-<name>`; otherwise document as contract-tested.
5. Codex desktop still lists all models after restart; reserve routing intact.
6. Repo renamed to `~/repos/darshj-codex-router`, config/plist repointed,
   pushed to `github.com/darshjme/darshj-codex-router` (public), no secrets
   (`state/` ignored; verify with `git ls-files`).
