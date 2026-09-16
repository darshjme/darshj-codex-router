"""Local Ollama models as a Codex bridge: discovery, catalog entries and turns.

Pure asyncio + aiohttp against Ollama's HTTP API (/api/tags, /api/show,
/api/chat); no CLI. Turns are stateless: the whole transcript is sent every
time, so the session_id/resume/delta parameters exist only for signature
parity with adapter.run_claude. Tool requests return to Codex through the
same JSON envelope the Claude and Grok bridges use. Nothing leaves the
machine unless the conversation itself references an http(s) image URL,
which is fetched so Ollama receives bytes.
"""
import asyncio
import base64
import copy
import json
import re
from typing import Any, Dict, List, Optional

import aiohttp

import adapter
from adapter import BridgeError, UndeclaredTool

DEFAULT_BASE_URL = 'http://127.0.0.1:11434'
DEFAULT_CONTEXT = 8192
# Codex re-sends the whole thread each turn; cap the advertised window so it
# compacts, and cap num_ctx so a local model does not exhaust unified memory.
CATALOG_CONTEXT_CAP = 131072
CHAT_CONTEXT_CAP = 32768
CATALOG_PRIORITY = 9
# Ollama embeds images in the JSON body; keep a fetched URL image bounded.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
VISION_FAMILIES = ('clip', 'mllama')
DISCOVERY_TIMEOUT = aiohttp.ClientTimeout(total=10)
IMAGE_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=60)
RETRY_NOTE = ('Return only the JSON object that matches the required schema. '
              'No prose, no markdown fences, nothing before or after it.')
# Model name -> context_length learned by discover() (or lazily by run_ollama);
# run_ollama sizes num_ctx from it. The orchestrator may clear it on refresh.
CONTEXT_LENGTHS: Dict[str, int] = {}
_SLUG_JUNK = re.compile(r'[^a-z0-9.]+')
_FENCE = re.compile(r'^```[a-zA-Z0-9]*\s*(.*?)\s*```$', re.S)

def _url(base_url: str, path: str) -> str:
    return base_url.rstrip('/') + path

def _int(value: Any) -> Optional[int]:
    """`value` when it is a real int (bools excluded), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None

async def _show(session: aiohttp.ClientSession, base_url: str, name: str) -> dict:
    """POST /api/show for one model; {} when Ollama cannot describe it."""
    try:
        # Newer servers read `model`, older ones `name`; both are harmless together.
        async with session.post(_url(base_url, '/api/show'), json={'model': name, 'name': name},
                                timeout=DISCOVERY_TIMEOUT) as response:
            if response.status != 200:
                return {}
            shown = await response.json(content_type=None)
            return shown if isinstance(shown, dict) else {}
    except Exception:  # discovery must never raise; a missing show is tolerated
        return {}

def _context_length(shown: dict) -> Optional[int]:
    """Native context from model_info['<arch>.context_length'], if present."""
    info = shown.get('model_info')
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if isinstance(key, str) and key.endswith('.context_length') and _int(value):
            return value
    return None

def _families(*detail_dicts: Any) -> List[str]:
    found = []
    for details in detail_dicts:
        if not isinstance(details, dict):
            continue
        families = details.get('families')
        if isinstance(families, list):
            found += [f.lower() for f in families if isinstance(f, str)]
        family = details.get('family')
        if isinstance(family, str):
            found.append(family.lower())
    return found

def _describe(tag: dict, shown: dict) -> dict:
    """One discover() record from a /api/tags entry and its /api/show reply."""
    tag_details = tag.get('details') if isinstance(tag.get('details'), dict) else {}
    shown_details = shown.get('details') if isinstance(shown.get('details'), dict) else {}
    capabilities = shown.get('capabilities')
    vision = (isinstance(capabilities, list) and 'vision' in capabilities) or any(
        family in VISION_FAMILIES for family in _families(tag_details, shown_details))
    context = _context_length(shown)
    if context:
        CONTEXT_LENGTHS[tag['name']] = context
    return {'name': tag['name'],
            'size': _int(tag.get('size')) or 0,
            'family': tag_details.get('family') or shown_details.get('family') or '',
            'parameter_size': tag_details.get('parameter_size') or shown_details.get('parameter_size') or '',
            'context_length': context,
            'vision': bool(vision)}

async def discover(session: aiohttp.ClientSession, base_url: str) -> List[dict]:
    """Models served by Ollama at base_url; [] when it is unreachable (never raises)."""
    try:
        async with session.get(_url(base_url, '/api/tags'), timeout=DISCOVERY_TIMEOUT) as response:
            if response.status != 200:
                return []
            body = await response.json(content_type=None)
        listed = body.get('models') if isinstance(body, dict) else None
        listed = [m for m in listed or [] if isinstance(m, dict) and isinstance(m.get('name'), str)]
        shown = await asyncio.gather(*(_show(session, base_url, m['name']) for m in listed))
        return [_describe(tag, detail) for tag, detail in zip(listed, shown)]
    except Exception:  # unreachable, slow, or malformed: the picker simply has no Ollama entries
        return []

def slug_for(name: str) -> str:
    """Codex catalog slug for an Ollama model: 'llama3.2:3b' -> 'ollama-llama3.2-3b'."""
    return 'ollama-' + _SLUG_JUNK.sub('-', name.lower()).strip('-')

def display_for(name: str) -> str:
    """Picker label for an Ollama model: 'llama3.2:3b' -> 'Llama3.2 3b · Ollama'."""
    label = ' '.join(name.replace(':', ' ').split())
    return label[:1].upper() + label[1:] + ' · Ollama'

def _description(model: dict) -> str:
    facts = [str(x) for x in (model.get('parameter_size'), model.get('family')) if x]
    detail = ' (%s)' % ', '.join(facts) if facts else ''
    modality = 'Text and images' if model.get('vision') else 'Text'
    return ('%s%s running locally through Ollama. %s and all Codex tools; '
            'no web search, nothing leaves this machine.' % (model['name'], detail, modality))

def catalog_entry(template: dict, model: dict) -> dict:
    """A models.json entry for `model` (a discover() record), cloned from a
    claude-max-* template so every Codex-required key stays present."""
    entry = copy.deepcopy(template)
    context = min(_int(model.get('context_length')) or DEFAULT_CONTEXT, CATALOG_CONTEXT_CAP)
    entry['slug'] = slug_for(model['name'])
    entry['display_name'] = display_for(model['name'])
    entry['description'] = _description(model)
    entry['input_modalities'] = ['text', 'image'] if model.get('vision') else ['text']
    entry['supports_search_tool'] = False
    entry['context_window'] = context
    entry['max_context_window'] = context
    entry['priority'] = CATALOG_PRIORITY
    return entry

async def _fetch(session: aiohttp.ClientSession, url: str) -> bytes:
    """Bytes of an http(s) image, refusing anything over MAX_IMAGE_BYTES."""
    limit = 'Image at %s exceeds %d MB and was not sent.' % (url, MAX_IMAGE_BYTES // (1024 * 1024))
    try:
        async with session.get(url, timeout=IMAGE_FETCH_TIMEOUT) as response:
            if response.status >= 400:
                raise BridgeError('Image URL %s returned HTTP %d.' % (url, response.status))
            if (response.content_length or 0) > MAX_IMAGE_BYTES:
                raise BridgeError(limit)
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise BridgeError(limit)
                chunks.append(chunk)
    except asyncio.TimeoutError:
        raise BridgeError('Timed out fetching image %s.' % url)
    except aiohttp.ClientError as error:
        raise BridgeError('Could not fetch image %s: %s' % (url, error))
    return b''.join(chunks)

async def _image_base64(session: aiohttp.ClientSession, image: dict) -> str:
    """Base64 payload for one translate() image block (base64 or url source)."""
    source = image.get('source') or {}
    if source.get('type') == 'base64' and source.get('data'):
        return source['data']
    if source.get('type') == 'url' and source.get('url'):
        return base64.b64encode(await _fetch(session, source['url'])).decode()
    raise BridgeError('Unsupported image source was not sent to Ollama.')

async def _context_for(session: aiohttp.ClientSession, base_url: str, name: str) -> int:
    """Native context of `name`: the discover() cache, else /api/show, else the default.
    Only a positive answer is cached so an outage does not pin the default."""
    context = CONTEXT_LENGTHS.get(name)
    if not context:
        context = _context_length(await _show(session, base_url, name))
        if context:
            CONTEXT_LENGTHS[name] = context
    return context or DEFAULT_CONTEXT

def _error_text(text: str) -> str:
    """Ollama's {'error': ...} string when the body is JSON, else the raw body."""
    try:
        body = json.loads(text)
        if isinstance(body, dict) and isinstance(body.get('error'), str):
            text = body['error']
    except ValueError:
        pass
    text = text.strip() or 'no response body'
    return text if len(text) <= 500 else text[:500] + '...'

async def _chat(session: aiohttp.ClientSession, base_url: str, payload: dict, timeout: float) -> dict:
    """POST /api/chat and return the decoded body; every failure is a BridgeError."""
    try:
        async with session.post(_url(base_url, '/api/chat'), json=payload,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            text = await response.text()
            if response.status >= 400:
                raise BridgeError('Ollama HTTP %d: %s' % (response.status, _error_text(text)))
    except aiohttp.ClientConnectorError:
        raise BridgeError('Ollama is not running at %s' % base_url)
    except asyncio.TimeoutError:
        raise BridgeError('Ollama timed out after %ss.' % timeout)
    except aiohttp.ClientError as error:
        raise BridgeError('Ollama request failed: %s' % error)
    try:
        body = json.loads(text)
    except ValueError:
        raise BridgeError('Ollama returned invalid JSON.')
    if not isinstance(body, dict):
        raise BridgeError('Ollama returned an unexpected response shape.')
    return body

def _content(body: dict) -> str:
    message = body.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    return content if isinstance(content, str) else ''

def _usage(body: dict) -> dict:
    return {'input_tokens': _int(body.get('prompt_eval_count')) or 0,
            'output_tokens': _int(body.get('eval_count')) or 0}

def _parse(content: str) -> Optional[dict]:
    """The JSON envelope in `content` (fences tolerated), or None when it is
    not a well-formed {message, channel, tool_calls[{name, input}]} object."""
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    if not text.startswith('{'):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get('message'), str):
        return None
    calls = data.get('tool_calls')
    if calls is None:
        data['tool_calls'] = calls = []
    if not isinstance(calls, list) or not all(
            isinstance(c, dict) and isinstance(c.get('name'), str) and isinstance(c.get('input'), str)
            for c in calls):
        return None
    return data

async def _envelope(session: aiohttp.ClientSession, base_url: str, payload: dict, timeout: float):
    """(envelope dict, raw content, usage) from /api/chat; one retry for stray prose."""
    body = await _chat(session, base_url, payload, timeout)
    content = _content(body)
    data = _parse(content)
    if data is None:
        retry = dict(payload, messages=payload['messages'] + [
            {'role': 'assistant', 'content': content}, {'role': 'user', 'content': RETRY_NOTE}])
        body = await _chat(session, base_url, retry, timeout)
        content = _content(body)
        data = _parse(content)
        if data is None:
            excerpt = content.strip()[:200] or '(empty)'
            raise BridgeError('Ollama did not return the required JSON envelope: ' + excerpt)
    return data, content, _usage(body)

def _response(request: dict, data: dict, usage: dict, tools: dict, default_model: str) -> dict:
    """adapter.structured_response over a synthetic Claude-style result record."""
    result = {'type': 'result', 'subtype': 'success', 'is_error': False,
              'structured_output': data, 'usage': usage}
    return adapter.structured_response(request, [], result, tools, default_model)

async def run_ollama(request: dict, session: aiohttp.ClientSession, base_url: str, model_name: str,
                     timeout: float = 300, session_id: Optional[str] = None, resume: bool = False,
                     delta: Optional[list] = None) -> dict:
    """One Ollama turn with the same contract as adapter.run_claude. Ollama
    keeps no session, so the whole transcript is sent every turn and
    session_id/resume/delta are accepted but unused."""
    system, prompt, schema, tools, images, _ = adapter.translate(
        request, agent='Ollama', host='Ollama', search_tools='none')
    user: Dict[str, Any] = {'role': 'user', 'content': prompt}
    if images:
        user['images'] = [await _image_base64(session, image) for image in images]
    messages = [{'role': 'system', 'content': system}, user]
    num_ctx = min(await _context_for(session, base_url, model_name), CHAT_CONTEXT_CAP)
    payload = {'model': model_name, 'stream': False, 'format': schema,
               'options': {'num_ctx': num_ctx}, 'messages': messages}
    default_model = slug_for(model_name)
    data, content, usage = await _envelope(session, base_url, payload, timeout)
    try:
        return _response(request, data, usage, tools, default_model)
    except UndeclaredTool as error:
        # One correction, as the Claude and Grok bridges do, before the router sees it.
        corrected = dict(payload, messages=messages + [
            {'role': 'assistant', 'content': content},
            {'role': 'user', 'content': adapter.correction_note(error)}])
        data, _, usage = await _envelope(session, base_url, corrected, timeout)
        return _response(request, data, usage, tools, default_model)
