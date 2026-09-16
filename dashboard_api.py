"""Dashboard HTTP API and static file app for Darshj's Codex Router (SSOT §5, box B).

``build(router, stats, static_dir)`` returns the ``/api/v1`` sub-application and
``static_app(static_dir)`` the ``/dashboard`` one; router.py mounts them with
``app.add_subapp('/api/v1', ...)`` and ``app.add_subapp('/dashboard', ...)``.
Routes below are written relative to those prefixes.

Auth (loopback tool, single user):
  * The token lives at ``router.token_path`` (``state/dashboard-token``), 32
    random bytes as hex, created with mode 0600 when missing.
  * ``Authorization: Bearer <token>`` is accepted from anywhere (curl/scripts).
  * The browser gets cookie ``dcr_session`` = ``HMAC-SHA256(token, b'dcr')``
    (HttpOnly, SameSite=Strict, Path=/, 30 days) from ``POST auth/login``. A
    cookie session must be same-origin: when an Origin or Referer header is
    present its host must be 127.0.0.1 or localhost; when neither is present a
    safe method (GET/HEAD/OPTIONS) is allowed and a mutating one is refused.
  * Public: ``GET health``, ``POST auth/login`` (429 after 10 failures in 10
    minutes, per process), ``GET auth/status``. Everything else answers 401
    ``{"error": {"code": "unauthorized", ...}}`` without credentials.
  * ``POST auth/rotate`` writes a new token; cookies derive from the token so
    every old cookie stops working at once.

Every error body is ``{"error": {"code": str, "message": str}}``.

Route table (method, path, auth):
  GET    health                                public
  POST   auth/login                            public   {token} -> sets cookie, {ok: true}
  GET    auth/status                           public   {authenticated, method}
  POST   auth/logout                           auth     clears cookie
  POST   auth/rotate                           auth     {token}
  GET    stats/summary?range=1h|24h|7d|30d     auth
  GET    stats/timeseries?range=&bucket=auto|N auth     auto: 1h->60s, 24h->900s, 7d->3600s, 30d->21600s
  GET    requests?limit=&before=&provider=&status=   auth
  GET    usage                                 auth
  GET    models                                auth
  GET    settings                              auth
  PUT    settings                              auth     partial {reserve:{model,threads}, ollama:{enabled,base_url}}
  DELETE settings/reserve/threads/{thread}     auth
  POST   ollama/refresh                        auth
  GET    /dashboard/, app.js, styles.css       none     (static_app; Cache-Control: no-store)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional
from aiohttp import web
import adapter
from stats import StatsClosed

log = logging.getLogger('dcr.dashboard')

DEFAULT_VERSION = '1.0.0'
VERSION_FILE = Path(__file__).resolve().parent / 'VERSION'
TOKEN_BYTES = 32
COOKIE_NAME = 'dcr_session'
COOKIE_MAX_AGE = 30 * 86400
LOCKOUT_FAILURES = 10
LOCKOUT_WINDOW_S = 600
RANGES = {'1h': 3600, '24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400}
DEFAULT_RANGE = '24h'
AUTO_BUCKETS = {'1h': 60, '24h': 900, '7d': 3600, '30d': 6 * 3600}
MIN_BUCKET_S, MAX_BUCKET_S = 10, 7 * 86400
DEFAULT_LIMIT, MAX_LIMIT = 50, 500
STATUSES = ('ok', 'error')
PROVIDER_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost')
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')
DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434'
REFRESH_NOTE = 'Restart Codex to see new models'
MAX_BODY = 64 * 1024
STATIC_FILES = {'index.html': 'text/html', 'app.js': 'application/javascript', 'styles.css': 'text/css'}
HTTP_CODES = {400: 'bad_request', 401: 'unauthorized', 403: 'forbidden', 404: 'not_found',
              405: 'method_not_allowed', 413: 'payload_too_large', 415: 'unsupported_media_type'}


class ApiError(Exception):
    """Raised inside handlers; the error middleware turns it into a JSON error."""

    def __init__(self, status: int, code: str, message: str, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def error(status: int, code: str, message: str, headers: Optional[Dict[str, str]] = None) -> web.Response:
    """JSON error response in the SSOT shape."""
    return web.json_response({'error': {'code': code, 'message': message}}, status=status, headers=headers)


def public(handler):
    """Mark a handler as reachable without authentication."""
    handler.dcr_public = True
    return handler


def read_version() -> str:
    """Contents of the repo-root VERSION file, or DEFAULT_VERSION when absent/empty."""
    try:
        return VERSION_FILE.read_text().strip() or DEFAULT_VERSION
    except OSError:
        return DEFAULT_VERSION


def provider_of(slug: str) -> str:
    """Provider for a catalog slug: claude | grok | ollama | openai."""
    if slug in adapter.CLAUDE_MODELS:
        return 'claude'
    if slug in adapter.GROK_MODELS:
        return 'grok'
    if slug.startswith('ollama-'):
        return 'ollama'
    return 'openai'


def origin_host(value: str) -> Optional[str]:
    """Lower-cased hostname of an Origin/Referer header value, None when unparsable."""
    try:
        return urllib.parse.urlsplit(value).hostname
    except ValueError:
        return None


def same_origin(request: web.Request) -> bool:
    """Loopback-origin check for cookie sessions (see module docstring)."""
    for header in ('Origin', 'Referer'):
        value = request.headers.get(header)
        if value:
            return origin_host(value) in LOOPBACK_HOSTS
    return request.method in SAFE_METHODS


def validate_base_url(value: Any) -> str:
    """Return the normalised Ollama base URL or raise ApiError(400) — http(s) to loopback only."""
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, 'invalid_settings', 'ollama.base_url must be a non-empty string.')
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise ApiError(400, 'invalid_settings', 'ollama.base_url is not a valid URL.')
    if parts.scheme not in ('http', 'https') or parts.hostname not in LOOPBACK_HOSTS \
            or parts.username or parts.password or parts.query or parts.fragment:
        raise ApiError(400, 'invalid_settings',
                       'ollama.base_url must be an http(s) URL to 127.0.0.1 or localhost, e.g. %s.' % DEFAULT_OLLAMA_URL)
    if port is not None and not 1 <= port <= 65535:
        raise ApiError(400, 'invalid_settings', 'ollama.base_url port is out of range.')
    return value.rstrip('/')


def executable_exists(path: Any) -> bool:
    """True when the CLI at path (absolute or on PATH) exists and is executable."""
    return bool(path) and shutil.which(str(path)) is not None


class Auth:
    """Dashboard token, derived cookie value and the per-process login lockout."""

    def __init__(self, token_path) -> None:
        self.token_path = Path(token_path)
        self.token = self.ensure_token()
        self.failures: list = []

    def ensure_token(self) -> str:
        """Read the token file, creating a fresh 0600 random one when missing or empty."""
        try:
            token = self.token_path.read_text().strip()
        except OSError:
            token = ''
        if token:
            return token
        return self.write_token(secrets.token_hex(TOKEN_BYTES))

    def write_token(self, token: str) -> str:
        """Atomically write token to the file with mode 0600."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_name(self.token_path.name + '.tmp')
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(token + '\n')
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(self.token_path))
        return token

    def rotate(self) -> str:
        """Replace the token on disk and in memory; old cookies stop matching."""
        self.token = self.write_token(secrets.token_hex(TOKEN_BYTES))
        self.failures = []
        return self.token

    def cookie_value(self) -> str:
        """Cookie payload: HMAC-SHA256 of b'dcr' keyed with the token."""
        return hmac.new(self.token.encode(), b'dcr', hashlib.sha256).hexdigest()

    def matches_token(self, candidate: Any) -> bool:
        """Constant-time comparison against the current token."""
        return isinstance(candidate, str) and hmac.compare_digest(candidate.encode(), self.token.encode())

    def matches_cookie(self, candidate: Any) -> bool:
        """Constant-time comparison against the current cookie value."""
        return isinstance(candidate, str) and hmac.compare_digest(candidate.encode(), self.cookie_value().encode())

    def retry_after(self, now: float) -> int:
        """Seconds until login is allowed again; 0 when not locked out."""
        self.failures = [t for t in self.failures if now - t < LOCKOUT_WINDOW_S]
        if len(self.failures) < LOCKOUT_FAILURES:
            return 0
        return max(1, int(LOCKOUT_WINDOW_S - (now - self.failures[0])) + 1)

    def record_failure(self, now: float) -> None:
        """Remember one failed login attempt."""
        self.failures.append(now)


class Dashboard:
    """Handler set for the /api/v1 sub-app; one instance per build()."""

    def __init__(self, router, stats, static_dir) -> None:
        self.router = router
        self.stats = stats
        self.static_dir = Path(static_dir) if static_dir is not None else None
        self.auth = Auth(router.token_path)
        self.started = time.monotonic()
        self.version = read_version()

    # ---- auth plumbing ----
    def authenticate(self, request: web.Request) -> Optional[str]:
        """'bearer' or 'cookie' when the request carries valid credentials, else None.
        Raises ApiError(403) for a valid cookie used cross-origin."""
        header = request.headers.get('Authorization', '')
        if header.startswith('Bearer '):
            return 'bearer' if self.auth.matches_token(header[7:].strip()) else None
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and self.auth.matches_cookie(cookie):
            if not same_origin(request):
                raise ApiError(403, 'forbidden', 'Cookie sessions are accepted from the dashboard origin only.')
            return 'cookie'
        return None

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        """401 for every non-public route without valid credentials."""
        if not getattr(request.match_info.handler, 'dcr_public', False):
            mode = self.authenticate(request)
            if mode is None:
                raise ApiError(401, 'unauthorized',
                               'Send Authorization: Bearer <token> or log in at /dashboard/ '
                               '(token: state/dashboard-token).')
            request['dcr_auth'] = mode
        return await handler(request)

    def set_cookie(self, response: web.Response) -> None:
        """Attach the session cookie with the SSOT attributes."""
        response.set_cookie(COOKIE_NAME, self.auth.cookie_value(), max_age=COOKIE_MAX_AGE,
                            path='/', httponly=True, samesite='Strict')

    # ---- request helpers ----
    @staticmethod
    async def json_body(request: web.Request) -> Dict[str, Any]:
        """Parse a JSON object body; 400 for anything else, 413 when oversized."""
        if request.content_length is not None and request.content_length > MAX_BODY:
            raise ApiError(413, 'payload_too_large', 'Body exceeds %d bytes.' % MAX_BODY)
        raw = await request.read()
        if len(raw) > MAX_BODY:
            raise ApiError(413, 'payload_too_large', 'Body exceeds %d bytes.' % MAX_BODY)
        try:
            body = json.loads(raw.decode('utf-8')) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'invalid_json', 'Body must be a JSON object.')
        if not isinstance(body, dict):
            raise ApiError(400, 'invalid_json', 'Body must be a JSON object.')
        return body

    @staticmethod
    def range_of(request: web.Request):
        """(name, seconds) for ?range=, default 24h; 400 for unknown values."""
        name = request.query.get('range') or DEFAULT_RANGE
        if name not in RANGES:
            raise ApiError(400, 'invalid_range', 'range must be one of: %s.' % ', '.join(RANGES))
        return name, RANGES[name]

    @staticmethod
    def bucket_of(request: web.Request, range_name: str) -> int:
        """Bucket width in seconds for ?bucket=auto|<seconds>."""
        raw = (request.query.get('bucket') or 'auto').strip().lower()
        if raw == 'auto':
            return AUTO_BUCKETS[range_name]
        try:
            bucket = int(raw)
        except ValueError:
            raise ApiError(400, 'invalid_bucket', 'bucket must be "auto" or a whole number of seconds.')
        if not MIN_BUCKET_S <= bucket <= MAX_BUCKET_S:
            raise ApiError(400, 'invalid_bucket', 'bucket must be between %d and %d seconds.' % (MIN_BUCKET_S, MAX_BUCKET_S))
        return bucket

    @staticmethod
    def int_param(request: web.Request, name: str, default: Optional[int], low: int,
                  high: Optional[int]) -> Optional[int]:
        """Integer query parameter within [low, high]; 400 when malformed."""
        raw = request.query.get(name)
        if raw is None or raw == '':
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ApiError(400, 'invalid_parameter', '%s must be an integer.' % name)
        if value < low or (high is not None and value > high):
            raise ApiError(400, 'invalid_parameter', '%s must be between %d and %s.' % (name, low, high if high is not None else 'inf'))
        return value

    def ollama_settings(self) -> Dict[str, Any]:
        """{'enabled', 'base_url'} from router.settings with SSOT defaults."""
        settings = getattr(self.router, 'settings', None) or {}
        block = settings.get('ollama') or {}
        return {'enabled': bool(block.get('enabled', True)),
                'base_url': str(block.get('base_url') or DEFAULT_OLLAMA_URL)}

    def settings_payload(self) -> Dict[str, Any]:
        """Body of GET/PUT settings."""
        reserve = self.router.reserve
        return {'reserve': {'model': reserve.get('model'), 'threads': dict(reserve.get('threads') or {}),
                            'effective': self.router.reserve_target({})},
                'ollama': self.ollama_settings(), 'catalog_path': str(self.router.catalog)}

    # ---- public endpoints ----
    @public
    async def health(self, request: web.Request) -> web.Response:
        """GET health — liveness plus the headline numbers, no auth."""
        ollama = self.ollama_settings()
        return web.json_response({
            'status': 'ok', 'version': self.version, 'uptime_s': int(time.monotonic() - self.started),
            'counts': dict(self.router.counts), 'reserve_model': self.router.reserve_target({}),
            'ollama': {'enabled': ollama['enabled'], 'base_url': ollama['base_url'],
                       'online': bool(getattr(self.router, 'ollama_online', False)),
                       'models': len(getattr(self.router, 'ollama_models', None) or {})}})

    @public
    async def login(self, request: web.Request) -> web.Response:
        """POST auth/login {token} — sets the session cookie; 401 wrong token, 429 locked out."""
        now = time.time()
        wait = self.auth.retry_after(now)
        if wait:
            raise ApiError(429, 'locked_out', 'Too many failed logins; try again in %d s.' % wait,
                           {'Retry-After': str(wait)})
        body = await self.json_body(request)
        if not self.auth.matches_token(body.get('token')):
            self.auth.record_failure(now)
            raise ApiError(401, 'unauthorized', 'Invalid token.')
        self.auth.failures = []
        response = web.json_response({'ok': True})
        self.set_cookie(response)
        return response

    @public
    async def status(self, request: web.Request) -> web.Response:
        """GET auth/status — {authenticated, method} without ever erroring."""
        try:
            mode = self.authenticate(request)
        except ApiError:
            mode = None
        return web.json_response({'authenticated': mode is not None, 'method': mode})

    # ---- authenticated endpoints ----
    async def logout(self, request: web.Request) -> web.Response:
        """POST auth/logout — clears the session cookie."""
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE_NAME, path='/')
        return response

    async def rotate(self, request: web.Request) -> web.Response:
        """POST auth/rotate — new token on disk, returned once; old cookies die with the old token.
        A cookie-authenticated caller gets the new cookie so the dashboard stays signed in."""
        token = self.auth.rotate()
        await self.stats.record_event('token_rotated', 'dashboard')
        response = web.json_response({'token': token})
        if request.get('dcr_auth') == 'cookie':
            self.set_cookie(response)
        return response

    async def stats_summary(self, request: web.Request) -> web.Response:
        """GET stats/summary?range= — Stats.summary plus the range name."""
        name, seconds = self.range_of(request)
        data = await self.stats.summary(time.time() - seconds)
        data['range'] = name
        return web.json_response(data)

    async def stats_timeseries(self, request: web.Request) -> web.Response:
        """GET stats/timeseries?range=&bucket= — dense bucket starts + sparse rows.

        ``buckets`` holds exactly range/bucket aligned starts (the last is the
        current, partial bucket); ``rows`` are Stats.timeseries entries whose
        ``t`` always appears in ``buckets``."""
        name, seconds = self.range_of(request)
        bucket = self.bucket_of(request, name)
        count = max(1, seconds // bucket)
        now = time.time()
        last = int(now // bucket) * bucket
        since = last - (count - 1) * bucket
        rows = await self.stats.timeseries(since, bucket)
        return web.json_response({
            'range': name, 'bucket_s': bucket, 'since': since, 'until': now,
            'buckets': [since + i * bucket for i in range(count)],
            'providers': sorted({r['provider'] for r in rows}), 'rows': rows})

    async def requests_list(self, request: web.Request) -> web.Response:
        """GET requests?limit=&before=&provider=&status= — newest first with a cursor."""
        limit = self.int_param(request, 'limit', DEFAULT_LIMIT, 1, MAX_LIMIT)
        before = self.int_param(request, 'before', None, 1, None)
        provider = request.query.get('provider') or None
        if provider is not None and not PROVIDER_RE.match(provider):
            raise ApiError(400, 'invalid_parameter', 'provider must match %s.' % PROVIDER_RE.pattern)
        status = request.query.get('status') or None
        if status is not None and status not in STATUSES:
            raise ApiError(400, 'invalid_parameter', 'status must be one of: %s.' % ', '.join(STATUSES))
        rows = await self.stats.recent(limit=limit, before_id=before, provider=provider, status=status)
        return web.json_response({'requests': rows, 'limit': limit,
                                  'next_before': rows[-1]['id'] if len(rows) == limit else None})

    async def usage(self, request: web.Request) -> web.Response:
        """GET usage — latest rate-limit window per provider plus the router's counters."""
        windows = await self.stats.latest_usage()
        return web.json_response({'windows': windows, 'counts': dict(self.router.counts),
                                  'generated_at': time.time()})

    async def models(self, request: web.Request) -> web.Response:
        """GET models — catalog entries with provider, online flag and reserve marker."""
        try:
            entries = json.loads(self.router.catalog.read_text())['models']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ApiError(500, 'catalog_error', 'Cannot read %s: %s' % (self.router.catalog, exc))
        reserve = self.router.reserve_target({})
        online = {'claude': executable_exists(getattr(self.router, 'claude', None)),
                  'grok': executable_exists(getattr(self.router, 'grok', None)),
                  'ollama': bool(getattr(self.router, 'ollama_online', False)), 'openai': True}
        out = []
        for entry in entries:
            slug = entry.get('slug') if isinstance(entry, dict) else None
            if not isinstance(slug, str):
                continue
            provider = provider_of(slug)
            out.append({'slug': slug, 'display_name': entry.get('display_name') or slug, 'provider': provider,
                        'context_window': entry.get('context_window'),
                        'input_modalities': entry.get('input_modalities') or ['text'],
                        'supports_search_tool': bool(entry.get('supports_search_tool', False)),
                        'is_reserve_default': slug == reserve, 'online': online[provider]})
        return web.json_response(out)

    async def settings_get(self, request: web.Request) -> web.Response:
        """GET settings."""
        return web.json_response(self.settings_payload())

    async def settings_put(self, request: web.Request) -> web.Response:
        """PUT settings — validate the whole partial body first, then apply and persist."""
        body = await self.json_body(request)
        if not body:
            raise ApiError(400, 'invalid_settings', 'No settings supplied.')
        unknown = sorted(set(body) - {'reserve', 'ollama'})
        if unknown:
            raise ApiError(400, 'invalid_settings', 'Unknown field(s): %s.' % ', '.join(unknown))
        reserve = body.get('reserve')
        ollama = body.get('ollama')
        model, threads = None, {}
        if reserve is not None:
            if not isinstance(reserve, dict):
                raise ApiError(400, 'invalid_settings', 'reserve must be an object.')
            unknown = sorted(set(reserve) - {'model', 'threads'})
            if unknown:
                raise ApiError(400, 'invalid_settings', 'Unknown reserve field(s): %s.' % ', '.join(unknown))
            if 'model' in reserve:
                model = reserve['model']
                if not isinstance(model, str) or model not in adapter.MODELS:
                    raise ApiError(400, 'invalid_settings',
                                   'reserve.model must be one of: %s.' % ', '.join(adapter.MODELS))
            if 'threads' in reserve:
                threads = reserve['threads']
                if not isinstance(threads, dict):
                    raise ApiError(400, 'invalid_settings', 'reserve.threads must be an object of thread -> model.')
                for thread, target in threads.items():
                    if not isinstance(thread, str) or not thread.strip() or len(thread) > 200:
                        raise ApiError(400, 'invalid_settings', 'reserve.threads keys must be thread ids.')
                    if not isinstance(target, str) or target not in adapter.MODELS:
                        raise ApiError(400, 'invalid_settings',
                                       'reserve.threads[%s] must be one of: %s.' % (thread, ', '.join(adapter.MODELS)))
        ollama_update: Dict[str, Any] = {}
        if ollama is not None:
            if not isinstance(ollama, dict):
                raise ApiError(400, 'invalid_settings', 'ollama must be an object.')
            unknown = sorted(set(ollama) - {'enabled', 'base_url'})
            if unknown:
                raise ApiError(400, 'invalid_settings', 'Unknown ollama field(s): %s.' % ', '.join(unknown))
            if 'enabled' in ollama:
                if not isinstance(ollama['enabled'], bool):
                    raise ApiError(400, 'invalid_settings', 'ollama.enabled must be true or false.')
                ollama_update['enabled'] = ollama['enabled']
            if 'base_url' in ollama:
                ollama_update['base_url'] = validate_base_url(ollama['base_url'])
        changes: Dict[str, Any] = {}
        if reserve is not None:
            try:
                if model is not None:
                    self.router.set_reserve_model(model)
                for thread, target in threads.items():
                    self.router.set_reserve_model(target, thread)
            except ValueError as exc:
                raise ApiError(400, 'invalid_settings', str(exc))
            changes['reserve'] = {'model': model, 'threads': threads}
            await self.stats.record_event('reserve_changed', changes['reserve'])
        if ollama_update:
            settings = getattr(self.router, 'settings', None)
            if not isinstance(settings, dict):
                settings = {}
                self.router.settings = settings
            block = settings.get('ollama')
            if not isinstance(block, dict):
                block = {}
                settings['ollama'] = block
            block.update(ollama_update)
            self.router.save_settings()
            changes['ollama'] = ollama_update
            await self.stats.record_event('settings_changed', {'ollama': ollama_update})
        return web.json_response(self.settings_payload())

    async def delete_thread(self, request: web.Request) -> web.Response:
        """DELETE settings/reserve/threads/{thread} — drop one per-thread reserve override."""
        thread = request.match_info['thread']
        threads = self.router.reserve.setdefault('threads', {})
        if thread not in threads:
            raise ApiError(404, 'not_found', 'No reserve override for thread %s.' % thread)
        threads.pop(thread)
        save = getattr(self.router, 'save_reserve', None)
        if callable(save):
            save()
        else:
            log.warning('router has no save_reserve(); thread override removal is in-memory only')
        await self.stats.record_event('reserve_changed', {'thread': thread, 'model': None})
        return web.json_response({'ok': True, 'thread': thread})

    async def ollama_refresh(self, request: web.Request) -> web.Response:
        """POST ollama/refresh — rediscover Ollama models, regenerate the catalog, remind to restart Codex."""
        try:
            result = await self.router.refresh_ollama()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception('ollama refresh failed')
            raise ApiError(502, 'ollama_refresh_failed', str(exc) or type(exc).__name__)
        payload = dict(result) if isinstance(result, dict) else {'models': [], 'catalog': None}
        payload.setdefault('models', [])
        if payload.get('catalog') is not None:
            payload['catalog'] = str(payload['catalog'])
        payload['note'] = REFRESH_NOTE
        await self.stats.record_event('ollama_refresh', {'models': len(payload['models'] or [])})
        return web.json_response(payload)


DASHBOARD_KEY = web.AppKey('dashboard', Dashboard)


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Every failure leaves as {error:{code,message}}; unexpected ones are logged with a traceback."""
    try:
        return await handler(request)
    except ApiError as exc:
        return error(exc.status, exc.code, exc.message, exc.headers or None)
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        return error(exc.status, HTTP_CODES.get(exc.status, 'http_error'), exc.reason or 'HTTP %d' % exc.status)
    except asyncio.CancelledError:
        raise
    except StatsClosed:
        log.warning('dashboard API: stats store is not open (%s %s)', request.method, request.path)
        return error(503, 'stats_unavailable', 'The stats store is not open; check the router log and restart the service.')
    except Exception as exc:
        log.exception('dashboard API failure on %s %s', request.method, request.path)
        return error(500, 'internal_error', 'Unexpected %s; see the router log.' % type(exc).__name__)


def build(router, stats, static_dir) -> web.Application:
    """The /api/v1 sub-app. ``static_dir`` is recorded for the companion static_app() builder."""
    dashboard = Dashboard(router, stats, static_dir)
    app = web.Application(middlewares=[error_middleware, dashboard.auth_middleware])
    app[DASHBOARD_KEY] = dashboard
    routes = app.router
    routes.add_get('/health', dashboard.health)
    routes.add_post('/auth/login', dashboard.login)
    routes.add_post('/auth/logout', dashboard.logout)
    routes.add_get('/auth/status', dashboard.status)
    routes.add_post('/auth/rotate', dashboard.rotate)
    routes.add_get('/stats/summary', dashboard.stats_summary)
    routes.add_get('/stats/timeseries', dashboard.stats_timeseries)
    routes.add_get('/requests', dashboard.requests_list)
    routes.add_get('/usage', dashboard.usage)
    routes.add_get('/models', dashboard.models)
    routes.add_get('/settings', dashboard.settings_get)
    routes.add_put('/settings', dashboard.settings_put)
    routes.add_delete('/settings/reserve/threads/{thread}', dashboard.delete_thread)
    routes.add_post('/ollama/refresh', dashboard.ollama_refresh)
    return app


def static_app(static_dir) -> web.Application:
    """The /dashboard sub-app: index.html at its root plus app.js and styles.css.

    Only the three named files are served (allow-list, so no traversal and no
    directory listing); everything else is a JSON 404. Responses carry
    ``Cache-Control: no-store`` so a rebuilt dashboard is never stale."""
    root = Path(static_dir).resolve()

    async def serve(request: web.Request) -> web.Response:
        """Serve one allow-listed file from root."""
        name = request.match_info.get('name') or 'index.html'
        content_type = STATIC_FILES.get(name)
        if content_type is None:
            return error(404, 'not_found', 'No such dashboard file.', {'Cache-Control': 'no-store'})
        try:
            body = (root / name).read_bytes()
        except OSError:
            return error(404, 'not_found', '%s is not installed under %s.' % (name, root),
                         {'Cache-Control': 'no-store'})
        return web.Response(body=body, content_type=content_type, charset='utf-8',
                            headers={'Cache-Control': 'no-store'})

    app = web.Application()
    app.router.add_get('/', serve)
    app.router.add_get('/{name}', serve)
    return app

