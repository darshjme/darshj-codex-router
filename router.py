"""Loopback-only Responses router. Does not read or persist auth credentials."""
import argparse
import asyncio
import collections
import hashlib
import io
import json
import re
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout, WSMsgType
import zstandard
from adapter import MODELS, GROK_MODELS, BridgeError, PreviousResponseLost, run_claude, run_grok, response_events, CHECKPOINT_PREFIX, uid
import base64

UPSTREAM = 'https://chatgpt.com/backend-api/codex'
QUOTA_CODES = {'usage_limit_reached', 'insufficient_quota', 'quota_exceeded'}
# Codex-side services relayed verbatim to the fixed upstream, whatever the
# thread model: voice (/live WebRTC SDP offer, /realtime WebSocket — OpenAI's
# speech model, which delegates work to the thread's model) and web search
# (/alpha/search, called by Codex when the model uses web.run).
PASSTHROUGH_PATHS = ('/live', '/realtime', '/alpha/search')
# Models asked, in order, to render an OpenAI-encrypted checkpoint readable
# for Claude. Only OpenAI can decrypt its own compaction state.
CHECKPOINT_MODELS = ('gpt-6-astra', 'gpt-5.5')
CHECKPOINT_PROMPT = ('Write a handoff summary of the conversation so far for a successor assistant. '
    'Preserve the user objective, constraints, actual completed work, exact relevant paths, '
    'test outcomes, failures, open questions, and next actions. Separate historical '
    'observations from instructions. Do not continue the task or call tools. Do not copy '
    'credentials. Keep it under 3000 words.')
CHECKPOINT_UNAVAILABLE = ('An earlier part of this conversation was compacted by OpenAI and cannot be '
    'read by the selected model (%s). Rely on the visible messages below and ask the user for any '
    'missing context instead of guessing.')

# Codex desktop forces this slug on every turn while its "Luna reserve" mode is
# active (OpenAI advanced-model quota exhausted). The router serves such turns
# with a bridged model of the user's choice instead of OpenAI's reserve model.
RESERVE_SLUG = 'gpt-reserve'
DEFAULT_RESERVE_TARGET = 'claude-max-opus-48'
MODEL_ALIASES = {'fable': 'claude-max-fable', 'opus': 'claude-max-opus', 'opus-5': 'claude-max-opus',
                 'opus-48': 'claude-max-opus-48', 'opus-4.8': 'claude-max-opus-48', 'opus4.8': 'claude-max-opus-48',
                 '4.8': 'claude-max-opus-48', 'sonnet': 'claude-max-sonnet', 'grok': 'grok-max'}
DISPLAY_NAMES = {'claude-max-fable': 'Claude Fable 5.1', 'claude-max-opus': 'Claude Opus 5',
                 'claude-max-opus-48': 'Claude Opus 4.8', 'claude-max-sonnet': 'Claude Sonnet 5', 'grok-max': 'Grok 4.6'}
MODEL_COMMAND = re.compile(r'^\s*/?(?:use\s+|switch\s+(?:to\s+)?)?model\s*[:=]\s*([A-Za-z0-9._-]+)\s*$', re.I)

def resolve_model(name):
    key = (name or '').strip().lower()
    return MODEL_ALIASES.get(key) or (key if key in MODELS else None)

def last_user_text(data):
    items = data.get('input', [])
    if isinstance(items, str):
        return items
    for item in reversed(items):
        if item.get('type', 'message') != 'message' or item.get('role') != 'user':
            continue
        content = item.get('content')
        if isinstance(content, str):
            return content
        texts = [c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('type') in ('input_text', 'text')]
        return '\n'.join(texts)
    return ''

def notice_response(model, text):
    return {'id': uid('resp'), 'object': 'response', 'created_at': int(time.time()), 'status': 'completed',
            'model': model, 'output': [{'type': 'message', 'id': uid('msg'), 'status': 'completed',
            'role': 'assistant', 'channel': 'final', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
            'error': None, 'incomplete_details': None,
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
                      'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}

LOST_SESSION_MARKERS = ('No conversation found', 'session not found', 'No session found', 'could not find session')

def is_lost_session(error):
    text = str(error).lower()
    return any(m.lower() in text for m in LOST_SESSION_MARKERS)

def is_passthrough(path):
    return any(path == prefix or path.startswith(prefix + '/') for prefix in PASSTHROUGH_PATHS)

def decode_zstd(body):
    """Decode zstd, including frames without a content-size header.

    Codex has sent those frames; ZstdDecompressor.decompress() then raises and
    used to 500 the turn so the desktop app looked hung. If the body is already
    JSON despite a zstd Content-Encoding, use it as-is.
    """
    dctx = zstandard.ZstdDecompressor()
    try:
        return dctx.decompress(body, max_output_size=32 * 1024 * 1024)
    except zstandard.ZstdError:
        pass
    try:
        chunks, total = [], 0
        reader = dctx.stream_reader(io.BytesIO(body))
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 32 * 1024 * 1024:
                raise BridgeError('Compressed request exceeded 32 MiB.')
            chunks.append(chunk)
        if chunks:
            return b''.join(chunks)
    except zstandard.ZstdError:
        pass
    stripped = body.lstrip()
    if stripped[:1] in (b'{', b'['):
        return body
    raise BridgeError('Request body was not valid zstd.')

def sse_packet(event):
    return ('event: %s\ndata: %s\n\n' % (event['type'], json.dumps(event))).encode()

def bridge_ack(model):
    stub = {'id': uid('resp'), 'object': 'response', 'created_at': int(time.time()),
            'status': 'in_progress', 'model': model, 'output': [], 'error': None,
            'incomplete_details': None, 'usage': None}
    return stub, [
        {'type': 'response.created', 'sequence_number': 0, 'response': stub},
        {'type': 'response.in_progress', 'sequence_number': 1, 'response': stub},
    ]

def rest_events(response, start=2):
    seq = start
    for event in response_events(response):
        if event['type'] in ('response.created', 'response.in_progress'):
            continue
        event['sequence_number'] = seq
        seq += 1
        yield event

def router_checkpoint(text):
    return {'type': 'compaction', 'id': uid('cmp'),
            'encrypted_content': CHECKPOINT_PREFIX + base64.b64encode(text.encode()).decode()}

def sse_events(text):
    for block in text.split('\n\n'):
        for line in block.splitlines():
            if line.startswith('data:'):
                try:
                    yield json.loads(line[5:].strip())
                except ValueError:
                    continue

def is_quota(event):
    if not isinstance(event, dict):
        return False
    error = event.get('error') or event.get('response', {}).get('error') or {}
    return isinstance(error, dict) and (error.get('code') in QUOTA_CODES or error.get('type') in QUOTA_CODES)

def forward_headers(headers):
    excluded = {'host', 'connection', 'upgrade', 'content-length', 'content-encoding',
                'accept-encoding', 'sec-websocket-key', 'sec-websocket-version',
                'sec-websocket-extensions', 'sec-websocket-protocol', 'origin', 'cookie'}
    return {k: v for k, v in headers.items() if k.lower() not in excluded}

class Router:
    def __init__(self, catalog, claude, state, grok=None, port=18740):
        self.port = port
        self.catalog = Path(catalog)
        self.claude = claude
        self.grok = grok or str(Path.home() / '.local/bin/grok')
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.claude_cwd = self.state / 'claude-work'
        self.claude_cwd.mkdir(exist_ok=True, mode=0o700)
        self.grok_cwd = self.state / 'grok-work'
        self.grok_cwd.mkdir(exist_ok=True, mode=0o700)
        self.reserve_file = self.state / 'reserve.json'
        self.codex_config = Path.home() / '.codex/config.toml'
        self.reserve = self.load_reserve()
        self.history = collections.OrderedDict()
        self.checkpoints = collections.OrderedDict()
        # response id -> (Claude Code session id, model): lets the next turn of
        # the same Codex thread resume that session and send only new items.
        self.sessions = collections.OrderedDict()
        # Sized for Codex sub-agent fan-out: several agents share this bridge.
        self.semaphore = asyncio.Semaphore(4)
        self.counts = {'claude': 0, 'grok': 0, 'openai': 0, 'fallback': 0, 'errors': 0}
        self.session = None

    # ---- Luna reserve: which bridged model answers gpt-reserve turns ----
    def load_reserve(self):
        try:
            data = json.loads(self.reserve_file.read_text())
            if isinstance(data, dict):
                return {'model': data.get('model') if data.get('model') in MODELS else None,
                        'threads': {k: v for k, v in (data.get('threads') or {}).items() if v in MODELS}}
        except (OSError, ValueError):
            pass
        return {'model': None, 'threads': {}}

    def save_reserve(self):
        threads = list(self.reserve['threads'].items())[-256:]
        self.reserve['threads'] = dict(threads)
        tmp = self.reserve_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.reserve))
        tmp.replace(self.reserve_file)

    def set_reserve_model(self, model, thread=None):
        if model not in MODELS:
            raise ValueError('not a bridged model: %s' % model)
        if thread:
            self.reserve['threads'][thread] = model
        else:
            self.reserve['model'] = model
        self.save_reserve()

    def configured_default(self):
        """Codex's own default `model` key, if it names a bridged model."""
        try:
            m = re.search(r'^\s*model\s*=\s*"([^"]+)"', self.codex_config.read_text(), re.M)
        except OSError:
            return None
        return m.group(1) if m and m.group(1) in MODELS else None

    def reserve_target(self, data):
        thread = data.get('prompt_cache_key')
        return (self.reserve['threads'].get(thread) if thread else None) or self.reserve['model'] \
            or self.configured_default() or DEFAULT_RESERVE_TARGET

    def display_name(self, slug):
        try:
            for m in json.loads(self.catalog.read_text())['models']:
                if m['slug'] == slug:
                    return m['display_name'].split(' \u00b7 ')[0]
        except (OSError, ValueError, KeyError):
            pass
        return DISPLAY_NAMES.get(slug, slug)

    def reserve_menu(self):
        return ', '.join('`model: %s`' % a for a in ('fable', 'opus', 'opus-48', 'sonnet', 'grok'))

    def reserve_rewrite(self, data):
        """For a gpt-reserve turn: answer an in-chat model command directly, or
        return (rewritten request, notice) with the chosen bridged model."""
        if data.get('model') != RESERVE_SLUG:
            return data, None, None
        thread = data.get('prompt_cache_key')
        command = MODEL_COMMAND.match(last_user_text(data))
        # Only a bare user message counts as a command, never a turn carrying tool output.
        items = data.get('input', [])
        tool_turn = any(isinstance(x, dict) and x.get('type', 'message') != 'message' and x.get('type') != 'additional_tools'
                        for x in (items if isinstance(items, list) else []))
        if command and not tool_turn:
            target = resolve_model(command.group(1))
            if target:
                self.set_reserve_model(target, thread)
                text = 'Switched: reserve turns in this thread now use %s through the router.' % self.display_name(target)
            else:
                text = ('Unknown model "%s". Choose one of %s, or pick a default at http://127.0.0.1:%d/.'
                        % (command.group(1), self.reserve_menu(), self.port))
            direct = notice_response(RESERVE_SLUG, text)
            self.record_direct(data, direct, target or self.reserve_target(data))
            return data, direct, None
        target = self.reserve_target(data)
        rewritten = dict(data, model=target)
        notice = None
        if not data.get('previous_response_id'):
            notice = ('Codex is in Luna reserve mode; this thread is served by %s through your own subscription via '
                      'the local router. To switch, send %s, or set a default at http://127.0.0.1:%d/.'
                      % (self.display_name(target), self.reserve_menu(), self.port))
        return rewritten, None, notice

    def record_direct(self, data, direct, target):
        """Chain a router-generated reply into the thread: full history behind it,
        and the live CLI session carried forward when the model is unchanged."""
        previous = data.get('previous_response_id')
        try:
            expanded = self.expand(data)
        except BridgeError:
            expanded = dict(data)
        self.remember(expanded, direct)
        saved = self.sessions.get(previous) if previous else None
        if saved and saved[1] == target:
            self.sessions[direct['id']] = saved

    def finish_reserve(self, response, notice):
        """Report the slug Codex asked for; add the first-turn notice."""
        response['model'] = RESERVE_SLUG
        if notice:
            response['output'].insert(0, {'type': 'message', 'id': uid('msg'), 'status': 'completed',
                'role': 'assistant', 'channel': 'commentary',
                'content': [{'type': 'output_text', 'text': notice, 'annotations': []}]})
        return response

    def selector_page(self):
        current = self.reserve_target({})
        rows = ''.join(
            '<label><input type="radio" name="model" value="%s"%s> %s <code>%s</code></label>'
            % (slug, ' checked' if slug == current else '', self.display_name(slug), slug) for slug in MODELS)
        return ('<!doctype html><meta charset="utf-8"><title>Codex Max Router</title>'
                '<style>body{font:15px -apple-system,system-ui;max-width:34em;margin:3em auto;padding:0 1em}'
                'label{display:block;padding:.5em 0;border-bottom:1px solid #ddd}button{margin-top:1em;padding:.5em 1.2em}'
                'code{color:#666;font-size:.85em}</style>'
                '<h2>Model for Codex reserve turns</h2>'
                '<p>While Codex desktop is in Luna reserve mode it sends every turn as <code>gpt-reserve</code>. '
                'The router serves those turns with this model instead. Per-thread: send <code>model: opus-48</code> '
                '(or fable, opus, sonnet, grok) as a chat message.</p>'
                '<form method="post" action="/select">%s<button>Save</button></form>'
                '<p><small>Current: <b>%s</b></small></p>' % (rows, self.display_name(current)))

    def prune_sessions(self, max_age=48 * 3600):
        """Drop bridge session transcripts (Claude Code and Grok CLI) older than max_age."""
        cutoff = time.time() - max_age
        removed = 0
        claude_dir = Path.home() / '.claude/projects' / str(self.claude_cwd.resolve()).replace('/', '-')
        grok_dir = Path.home() / '.grok/sessions' / urllib.parse.quote(str(self.grok_cwd.resolve()), safe='')
        entries = list(claude_dir.glob('*.jsonl')) if claude_dir.is_dir() else []
        entries += [d for d in grok_dir.iterdir() if d.is_dir()] if grok_dir.is_dir() else []
        for entry in entries:
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    async def startup(self, app):
        self.prune_sessions()
        self.session = ClientSession(timeout=ClientTimeout(total=360), trust_env=False)

    async def cleanup(self, app):
        await self.session.close()

    def remember(self, request, response):
        if not response.get('id'):
            return
        items = request.get('input', [])
        if isinstance(items, str):
            items = [{'type': 'message', 'role': 'user', 'content': items}]
        self.history[response['id']] = (time.monotonic(), items + response.get('output', []))
        while len(self.history) > 256:
            self.history.popitem(last=False)

    def alias(self, response, new_id):
        """Re-key a finished response under the id Codex was already told
        (the early ack), so its next previous_response_id still resumes."""
        old_id = response.get('id')
        for table in (self.history, self.sessions):
            if old_id in table:
                table[new_id] = table.pop(old_id)
        response['id'] = new_id

    def expand(self, request):
        request = dict(request)
        if isinstance(request.get('input'), str):
            request['input'] = [{'type': 'message', 'role': 'user', 'content': request['input']}]
        previous = request.get('previous_response_id')
        if previous:
            saved = self.history.get(previous)
            if not saved or time.monotonic() - saved[0] > 7200:
                raise PreviousResponseLost(previous)
            request['input'] = saved[1] + request.get('input', [])
            request.pop('previous_response_id', None)
        return request

    async def readable_checkpoint(self, item, headers):
        """Replace an OpenAI-encrypted compaction item with a router checkpoint.

        The upstream is asked once (per distinct checkpoint) for a plain summary,
        using the incoming request's own authorization; the result is cached in
        memory only. Any failure degrades to an explicit unavailability notice."""
        secret = item.get('encrypted_content', '')
        key = hashlib.sha256(secret.encode()).hexdigest()
        if key in self.checkpoints:
            return router_checkpoint(self.checkpoints[key])
        summary, reason = None, 'no summary returned'
        for model in CHECKPOINT_MODELS:
            body = {'model': model, 'stream': True, 'store': False,
                    'input': [item, {'type': 'message', 'role': 'user', 'content': CHECKPOINT_PROMPT}]}
            try:
                async with self.session.post(UPSTREAM + '/responses', json=body,
                        headers=forward_headers(headers), allow_redirects=False) as upstream:
                    payload = (await upstream.read()).decode(errors='replace')
                    if upstream.status != 200:
                        try:
                            error = json.loads(payload).get('error') or {}
                        except ValueError:
                            error = {}
                        reason = str(error.get('code') or error.get('type') or upstream.status)
                        continue
                    for event in sse_events(payload):
                        if event.get('type') == 'response.completed':
                            summary = '\n'.join(part['text'] for out in event['response'].get('output', [])
                                if out.get('type') == 'message' for part in out.get('content', [])
                                if part.get('type') == 'output_text') or None
                        elif event.get('type') in ('error', 'response.failed'):
                            error = event.get('error') or event.get('response', {}).get('error') or {}
                            reason = str(error.get('code') or error.get('type') or 'upstream error')
            except (OSError, asyncio.TimeoutError) as error:
                reason = type(error).__name__
            if summary:
                break
        if summary:
            self.checkpoints[key] = summary
            while len(self.checkpoints) > 64:
                self.checkpoints.popitem(last=False)
            return router_checkpoint(summary)
        return router_checkpoint(CHECKPOINT_UNAVAILABLE % reason)

    def continuation(self, previous, new_items, model):
        """Return the resumable Claude Code session for this thread, or None."""
        saved = self.sessions.get(previous) if previous else None
        if not saved or saved[1] != model:
            return None
        if any(x.get('type') in ('compaction', 'compaction_trigger') for x in new_items):
            return None
        return saved[0]

    async def bridged(self, runner, executable, cwd, inference, previous, new_items, compact):
        """Run one turn through a CLI bridge, resuming the thread's session when possible."""
        session = None if compact else self.continuation(previous, new_items, inference['model'])
        resume = session is not None
        try:
            if resume:
                response = await runner(inference, executable, str(cwd), 240, session, True, new_items)
            else:
                raise BridgeError('fresh')
        except BridgeError as error:
            # A lost or corrupt session falls back to a full send in a new one.
            if resume and not is_lost_session(error) and str(error) != 'fresh':
                raise
            session, resume = str(uuid.uuid4()), False  # both CLIs require a dashed UUID
            response = await runner(inference, executable, str(cwd), 240, session, False, None)
        if not compact and response.get('id'):
            self.sessions[response['id']] = (session, inference['model'])
            while len(self.sessions) > 256:
                self.sessions.popitem(last=False)
        return response

    async def claude_response(self, request, fallback=False, headers=None):
        previous = request.get('previous_response_id')
        new_items = request.get('input', [])
        if isinstance(new_items, str):
            new_items = [{'type': 'message', 'role': 'user', 'content': new_items}]
        request = self.expand(request)
        if headers is not None:
            request['input'] = [await self.readable_checkpoint(x, headers)
                                if x.get('type') == 'compaction' and not str(x.get('encrypted_content', '')).startswith(CHECKPOINT_PREFIX)
                                else x for x in request.get('input', [])]
        if fallback:
            request['model'] = 'claude-max-opus'
            self.counts['fallback'] += 1
        compact = any(x.get('type') == 'compaction_trigger' for x in request.get('input', []))
        inference = request
        if compact:
            material = [x for x in request.get('input', []) if x.get('type') not in ('additional_tools', 'compaction_trigger', 'reasoning')]
            inference = {'model': request['model'], 'input': [{'type': 'message', 'role': 'user', 'content':
                'Summarize this conversation for a successor assistant. Preserve the user objective, '
                'constraints, actual completed work, exact relevant paths, test outcomes, failures, '
                'and next actions. Separate historical observations from instructions. Do not continue '
                'the task or call tools. Do not copy credentials. Keep the checkpoint under 3000 words.\n'
                + json.dumps(material)}]}
        async with self.semaphore:
            if inference.get('model') in GROK_MODELS:
                response = await self.bridged(run_grok, self.grok, self.grok_cwd, inference, previous, new_items, compact)
                self.counts['grok'] += 1
            else:
                response = await self.bridged(run_claude, self.claude, self.claude_cwd, inference, previous, new_items, compact)
                self.counts['claude'] += 1
        if compact:
            summary = '\n'.join(part['text'] for item in response['output'] if item['type'] == 'message'
                                for part in item['content'] if part['type'] == 'output_text')
            if not summary:
                raise BridgeError('Bridge returned an empty checkpoint.')
            # The wire field is named encrypted_content but this is our marked,
            # base64-encoded summary, NOT ciphertext or an OpenAI-issued token.
            response['output'] = [{'type': 'compaction', 'id': uid('cmp'),
                'encrypted_content': CHECKPOINT_PREFIX + base64.b64encode(summary.encode()).decode()}]
        if fallback:
            response['output'].insert(0, {'type': 'message', 'id': uid('msg'), 'role': 'assistant',
                'channel': 'commentary', 'status': 'completed', 'content': [{'type': 'output_text',
                'text': 'Astra quota is exhausted. Continuing with Claude Opus through your Claude Max subscription.',
                'annotations': []}]})
        self.remember({'input': []} if compact else request, response)
        return response

    async def handle(self, request):
        # Origin/Host checks prevent drive-by browser use and DNS rebinding. The
        # selector form is the one browser client, and only from its own origin.
        host = request.host.split(':')[0]
        origin = request.headers.get('Origin')
        same_origin = origin and origin.rstrip('/') in ('http://' + request.host, 'http://127.0.0.1:%d' % self.port,
                                                        'http://localhost:%d' % self.port)
        if host not in ('127.0.0.1', 'localhost') or (origin and not (same_origin and request.path == '/select')):
            raise web.HTTPForbidden(text='Loopback native clients only')
        if request.path == '/health':
            return web.json_response({'status': 'ok', 'models': list(MODELS), 'counts': self.counts,
                                      'reserve_model': self.reserve_target({})})
        if request.path in ('/', '/select') and request.method == 'GET':
            return web.Response(text=self.selector_page(), content_type='text/html')
        if request.path == '/select' and request.method == 'POST':
            if not same_origin:
                raise web.HTTPForbidden(text='Same-origin form only')
            form = await request.post()
            try:
                self.set_reserve_model(form.get('model'))
            except ValueError as error:
                raise web.HTTPBadRequest(text=str(error))
            raise web.HTTPSeeOther('/')
        if not request.headers.get('Authorization', '').startswith('Bearer '):
            raise web.HTTPUnauthorized(text='Codex authentication required')
        if request.path == '/models':
            return web.json_response(json.loads(self.catalog.read_text()))
        if request.path == '/responses' and request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self.websocket(request)
        if request.path == '/responses' and request.method == 'POST':
            return await self.http_response(request)
        if is_passthrough(request.path):
            if request.headers.get('Upgrade', '').lower() == 'websocket':
                return await self.relay_websocket(request)
            return await self.passthrough(request)
        # Fixed upstream and bounded paths: never an arbitrary URL proxy.
        if request.path in ('/responses/compact', '/responses/input_tokens'):
            if request.path == '/responses/compact':
                body = await self.read_body(request)
                data = json.loads(body)
                if data.get('model') in MODELS:
                    data.setdefault('input', []).append({'type': 'compaction_trigger'})
                    response = await self.claude_response(data, headers=request.headers)
                    return web.json_response({'output': response['output'], 'usage': response['usage']},
                                             headers=response.get('rate_limit_headers') or {})
            return await self.passthrough(request)
        raise web.HTTPNotFound()

    async def passthrough(self, request):
        body = await self.read_body(request)
        async with self.session.request(request.method, UPSTREAM + request.path_qs,
                data=body, headers=forward_headers(request.headers), allow_redirects=False) as upstream:
            return web.Response(status=upstream.status, body=await upstream.read(),
                                content_type=upstream.content_type)

    async def relay_websocket(self, request):
        """Bidirectional relay for the voice WebSocket; no inspection or fallback."""
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)
        upstream = await self.session.ws_connect(UPSTREAM.replace('https:', 'wss:') + request.path_qs,
            headers=forward_headers(request.headers), heartbeat=20, max_msg_size=32 * 1024 * 1024)
        async def pump(source, sink):
            async for msg in source:
                if msg.type == WSMsgType.TEXT:
                    await sink.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await sink.send_bytes(msg.data)
                else:
                    break
            await sink.close()
        try:
            await asyncio.gather(pump(ws, upstream), pump(upstream, ws), return_exceptions=True)
        finally:
            await upstream.close()
        return ws

    async def http_response(self, request):
        body = await self.read_body(request)
        try:
            data = json.loads(body)
            data, direct, notice = self.reserve_rewrite(data)
            if direct:
                return web.json_response(direct) if not data.get('stream', True) else web.Response(
                    body=b''.join(sse_packet(e) for e in response_events(direct)), content_type='text/event-stream')
            fallback = False
            if data.get('model') not in MODELS:
                async with self.session.post(UPSTREAM + '/responses', json=data,
                        headers=forward_headers(request.headers), allow_redirects=False) as upstream:
                    payload = await upstream.read()
                    try:
                        error = json.loads(payload)
                    except ValueError:
                        error = {}
                    if data.get('model') == 'gpt-6-astra' and is_quota(error):
                        fallback = True
                    else:
                        self.counts['openai'] += 1
                        return web.Response(status=upstream.status, body=payload,
                                            content_type=upstream.content_type)
            response = await self.claude_response(data, fallback, request.headers)
            if notice is not None or data.get('model') != json.loads(body).get('model'):
                self.finish_reserve(response, notice)
            usage_headers = dict(response.get('rate_limit_headers') or {})
            if not data.get('stream', True):
                response.pop('rate_limit_headers', None)
                return web.json_response(response, headers=usage_headers)
            stub, acks = bridge_ack(response['model'])
            self.alias(response, stub['id'])
            payload = b''.join(sse_packet(e) for e in acks) + b''.join(
                sse_packet(e) for e in rest_events(response))
            return web.Response(body=payload, content_type='text/event-stream', headers=usage_headers)
        except PreviousResponseLost as error:
            # Codex answers this by resending the whole thread; no error count.
            return web.json_response({'error': error.payload()}, status=404)
        except (BridgeError, asyncio.TimeoutError) as error:
            self.counts['errors'] += 1
            return web.json_response({'error': {'type': 'router_error', 'message': str(error) or 'Bridge timed out'}}, status=502)

    async def read_body(self, request):
        body = await request.read()
        encoding = (request.headers.get('Content-Encoding') or '').lower()
        if encoding == 'zstd':
            body = decode_zstd(body)
        return body

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)
        upstream = None
        sticky_claude = False
        async def dispatch(data):
            nonlocal upstream, sticky_claude
            if data.get('generate') is False:
                # Codex preconnect warmup carries no user inference request.
                return
            requested = data.get('model', '')
            data, direct, notice = self.reserve_rewrite(data)
            if direct:
                for event in response_events(direct):
                    await ws.send_json(event)
                return
            model = data.get('model', '')
            if model in MODELS or (sticky_claude and model == 'gpt-6-astra'):
                self.expand(data)  # raises PreviousResponseLost before any ack goes out
                # Ack immediately so Codex does not retry while Grok/Claude run.
                stub, acks = bridge_ack(RESERVE_SLUG if requested == RESERVE_SLUG else model if model in MODELS else 'claude-max-opus')
                for event in acks:
                    await ws.send_json(event)
                response = await self.claude_response(data, sticky_claude and model not in MODELS, request.headers)
                if requested == RESERVE_SLUG:
                    self.finish_reserve(response, notice)
                self.alias(response, stub['id'])
                for event in rest_events(response):
                    await ws.send_json(event)
                return
            if upstream is None or upstream.closed:
                upstream = await self.session.ws_connect(UPSTREAM.replace('https:', 'wss:') + '/responses',
                    headers=forward_headers(request.headers), heartbeat=20, max_msg_size=32 * 1024 * 1024)
            await upstream.send_json(data)
            partial = False
            buffered = []
            async for msg in upstream:
                if msg.type != WSMsgType.TEXT:
                    raise BridgeError('OpenAI stream closed before completion; no fallback was attempted.')
                event = json.loads(msg.data)
                kind = event.get('type', '')
                if is_quota(event) and model == 'gpt-6-astra' and not partial:
                    sticky_claude = True
                    response = await self.claude_response(data, True, request.headers)
                    for event in response_events(response):
                        await ws.send_json(event)
                    return
                if kind.startswith('response.output_') or kind in ('response.custom_tool_call_input.delta', 'response.function_call_arguments.delta'):
                    partial = True
                if partial or kind in ('error', 'response.failed', 'response.completed'):
                    for initial in buffered:
                        await ws.send_json(initial)
                    buffered.clear()
                    await ws.send_json(event)
                else:
                    buffered.append(event)
                if kind == 'response.completed':
                    self.counts['openai'] += 1
                    try:
                        self.remember(self.expand(data), event['response'])
                    except BridgeError:
                        pass
                    return
                if kind in ('error', 'response.failed'):
                    return
            raise BridgeError('OpenAI stream ended before completion.')
        active = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if active and not active.done():
                    active.cancel()
                    await asyncio.gather(active, return_exceptions=True)
                async def guarded(payload):
                    try:
                        await dispatch(payload)
                    except asyncio.CancelledError:
                        raise
                    except PreviousResponseLost as error:
                        await ws.send_json({'type': 'error', 'status': 404, 'error': error.payload()})
                    except Exception as error:
                        self.counts['errors'] += 1
                        message = str(error) if isinstance(error, BridgeError) else type(error).__name__
                        await ws.send_json({'type': 'error', 'status': 502,
                            'error': {'type': 'router_error', 'code': 'router_error', 'message': message}})
                active = asyncio.create_task(guarded(data))
        finally:
            if active and not active.done():
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            if upstream:
                await upstream.close()
        return ws

def create_app(catalog, claude, state, grok=None, port=18740):
    router = Router(catalog, claude, state, grok, port)
    app = web.Application(client_max_size=32 * 1024 * 1024, handler_args={'auto_decompress': False})
    app['router'] = router
    app.on_startup.append(router.startup)
    app.on_cleanup.append(router.cleanup)
    app.router.add_route('*', '/{path:.*}', router.handle)
    return app

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18740)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--claude', default=str(Path.home() / '.local/bin/claude'))
    parser.add_argument('--grok', default=str(Path.home() / '.local/bin/grok'))
    parser.add_argument('--state', default=str(Path(__file__).parent / 'state'))
    args = parser.parse_args()
    web.run_app(create_app(args.catalog, args.claude, args.state, args.grok, args.port), host='127.0.0.1', port=args.port,
                access_log=None, handler_cancellation=True)
