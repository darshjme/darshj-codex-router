"""Tests for the Ollama bridge against an in-process fake Ollama server."""
import base64
import contextlib
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

import ollama
from adapter import BridgeError, UndeclaredTool

OK = {'message': 'Hello from llama', 'channel': 'final', 'tool_calls': []}
EXEC_CALL = {'message': '', 'channel': 'commentary',
             'tool_calls': [{'name': 'functions.exec', 'input': 'text(await tools.exec_command({cmd:"ls"}));'}]}
WRONG_CALL = {'message': '', 'channel': 'commentary',
              'tool_calls': [{'name': 'functions.exec_command', 'input': '{"cmd":"ls"}'}]}
TOOLS = {'type': 'additional_tools', 'tools': [{'type': 'namespace', 'name': 'functions',
         'tools': [{'type': 'custom', 'name': 'exec'}]}]}
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + bytes(range(24))
TAGS = [{'name': 'llama3.2:3b', 'model': 'llama3.2:3b', 'size': 2019393189,
         'details': {'family': 'llama', 'families': ['llama'], 'parameter_size': '3.2B'}},
        {'name': 'llava:7b', 'model': 'llava:7b', 'size': 4733363377,
         'details': {'family': 'llama', 'families': ['llama', 'clip'], 'parameter_size': '7B'}}]
SHOWS = {'llama3.2:3b': {'details': {'family': 'llama', 'families': ['llama']},
                         'model_info': {'general.architecture': 'llama', 'llama.context_length': 131072,
                                        'llama.embedding_length': 3072},
                         'capabilities': ['completion', 'tools']},
         'llava:7b': {'details': {'family': 'llama', 'families': ['llama', 'clip']},
                      'model_info': {'general.architecture': 'llama'},
                      'capabilities': ['completion', 'vision']}}

def request_for(*items, model='ollama-llama3.2-3b'):
    return {'model': model, 'input': list(items)}

def user(text):
    return {'type': 'message', 'role': 'user', 'content': text}

def unreachable_url():
    """A loopback URL nothing listens on (fresh ephemeral port, released)."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return 'http://127.0.0.1:%d' % sock.getsockname()[1]

def template():
    here = Path(__file__).parent
    catalog = here / 'models.base.json'
    if not catalog.exists():
        catalog = here / 'models.json'
    models = json.loads(catalog.read_text())['models']
    return next(m for m in models if m['slug'] == 'claude-max-sonnet')

class FakeOllama:
    """Minimal Ollama stand-in: /api/tags, /api/show, /api/chat and one image URL."""
    def __init__(self, answers=(), tags=(), shows=None, chat_status=200, chat_error='boom', tags_status=200):
        self.answers = list(answers)
        self.tags = list(tags)
        self.shows = dict(shows or {})
        self.chat_status = chat_status
        self.chat_error = chat_error
        self.tags_status = tags_status
        self.chats = []
        self.show_requests = []
        self.app = web.Application()
        self.app.router.add_get('/api/tags', self.handle_tags)
        self.app.router.add_post('/api/show', self.handle_show)
        self.app.router.add_post('/api/chat', self.handle_chat)
        self.app.router.add_get('/shot.png', self.handle_image)

    async def handle_tags(self, request):
        if self.tags_status != 200:
            return web.json_response({'error': 'unavailable'}, status=self.tags_status)
        return web.json_response({'models': self.tags})

    async def handle_show(self, request):
        body = await request.json()
        name = body.get('model') or body.get('name')
        self.show_requests.append(name)
        if name not in self.shows:
            return web.json_response({'error': "model '%s' not found" % name}, status=404)
        return web.json_response(self.shows[name])

    async def handle_chat(self, request):
        payload = await request.json()
        self.chats.append(payload)
        if self.chat_status != 200:
            return web.json_response({'error': self.chat_error}, status=self.chat_status)
        answer = self.answers.pop(0) if self.answers else OK
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return web.json_response({'model': payload['model'], 'created_at': '2026-09-16T00:00:00Z',
                                  'message': {'role': 'assistant', 'content': content}, 'done': True,
                                  'prompt_eval_count': 12, 'eval_count': 3})

    async def handle_image(self, request):
        return web.Response(body=PNG_BYTES, content_type='image/png')

class NamingTests(unittest.TestCase):
    def test_slug_for(self):
        self.assertEqual(ollama.slug_for('llama3.2:3b'), 'ollama-llama3.2-3b')
        self.assertEqual(ollama.slug_for('Qwen2.5-Coder:7B'), 'ollama-qwen2.5-coder-7b')
        self.assertEqual(ollama.slug_for('hf.co/User/Model-GGUF:Q4_K_M'), 'ollama-hf.co-user-model-gguf-q4-k-m')
        self.assertEqual(ollama.slug_for('gemma3:latest'), 'ollama-gemma3-latest')
        for name in ['llama3.2:3b', 'hf.co/User/Model-GGUF:Q4_K_M', 'a__b::c', ':leading']:
            slug = ollama.slug_for(name)
            self.assertRegex(slug, r'^ollama-[a-z0-9.-]+$', name)
            self.assertNotIn('--', slug)
            self.assertFalse(slug.endswith('-'))

    def test_display_for(self):
        self.assertEqual(ollama.display_for('llama3.2:3b'), 'Llama3.2 3b · Ollama')
        self.assertEqual(ollama.display_for('Qwen2.5-Coder:7B'), 'Qwen2.5-Coder 7B · Ollama')
        self.assertEqual(ollama.display_for('hf.co/user/model:q4'), 'Hf.co/user/model q4 · Ollama')
        self.assertEqual(ollama.display_for('gemma3'), 'Gemma3 · Ollama')

class CatalogEntryTests(unittest.TestCase):
    def test_fields_and_template_preserved(self):
        base = template()
        frozen = json.dumps(base, sort_keys=True)
        model = {'name': 'llama3.2:3b', 'size': 1, 'family': 'llama', 'parameter_size': '3.2B',
                 'context_length': 131072, 'vision': False}
        entry = ollama.catalog_entry(base, model)
        self.assertEqual(entry['slug'], 'ollama-llama3.2-3b')
        self.assertEqual(entry['display_name'], 'Llama3.2 3b · Ollama')
        self.assertIn('Ollama', entry['description'])
        self.assertIn('llama3.2:3b', entry['description'])
        self.assertEqual(entry['input_modalities'], ['text'])
        self.assertFalse(entry['supports_search_tool'])
        self.assertEqual(entry['context_window'], 131072)
        self.assertEqual(entry['max_context_window'], 131072)
        self.assertEqual(entry['priority'], 9)
        overridden = {'slug', 'display_name', 'description', 'input_modalities', 'supports_search_tool',
                      'context_window', 'max_context_window', 'priority'}
        self.assertEqual(set(entry), set(base))
        for key in set(base) - overridden:
            self.assertEqual(entry[key], base[key], key)
        # The template is cloned, never mutated, so the next entry starts clean.
        self.assertEqual(json.dumps(base, sort_keys=True), frozen)
        entry['model_messages']['persistent_instructions'] = 'changed'
        self.assertNotEqual(base['model_messages']['persistent_instructions'], 'changed')

    def test_vision_and_context_capping(self):
        base = template()
        vision = ollama.catalog_entry(base, {'name': 'llava:7b', 'context_length': 1048576, 'vision': True})
        self.assertEqual(vision['input_modalities'], ['text', 'image'])
        self.assertEqual(vision['context_window'], 131072)
        self.assertEqual(vision['max_context_window'], 131072)
        unknown = ollama.catalog_entry(base, {'name': 'tiny', 'context_length': None, 'vision': False})
        self.assertEqual(unknown['context_window'], 8192)
        small = ollama.catalog_entry(base, {'name': 'tiny', 'context_length': 4096})
        self.assertEqual(small['context_window'], 4096)
        self.assertEqual(small['input_modalities'], ['text'])

class OllamaServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ollama.CONTEXT_LENGTHS.clear()
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        ollama.CONTEXT_LENGTHS.clear()

    @contextlib.asynccontextmanager
    async def serve(self, fake):
        async with TestServer(fake.app) as server:
            yield str(server.make_url('')).rstrip('/')

    @staticmethod
    def sent_prompt(payload):
        return json.loads(payload['messages'][1]['content'])

class DiscoveryTests(OllamaServerCase):
    async def test_tags_and_show_are_combined(self):
        fake = FakeOllama(tags=TAGS, shows=SHOWS)
        async with self.serve(fake) as base:
            models = await ollama.discover(self.session, base + '/')
        self.assertEqual(models, [
            {'name': 'llama3.2:3b', 'size': 2019393189, 'family': 'llama', 'parameter_size': '3.2B',
             'context_length': 131072, 'vision': False},
            {'name': 'llava:7b', 'size': 4733363377, 'family': 'llama', 'parameter_size': '7B',
             'context_length': None, 'vision': True}])
        self.assertEqual(sorted(fake.show_requests), ['llama3.2:3b', 'llava:7b'])
        self.assertEqual(ollama.CONTEXT_LENGTHS, {'llama3.2:3b': 131072})

    async def test_vision_from_families_when_capabilities_missing(self):
        shows = {'llava:7b': {'details': {'families': ['llama', 'clip']}, 'model_info': {}}}
        fake = FakeOllama(tags=[TAGS[1]], shows=shows)
        async with self.serve(fake) as base:
            models = await ollama.discover(self.session, base)
        self.assertTrue(models[0]['vision'])
        self.assertIsNone(models[0]['context_length'])
        mllama = {'name': 'llama3.2-vision:11b', 'details': {'family': 'mllama', 'families': ['mllama']}}
        fake = FakeOllama(tags=[mllama], shows={})
        async with self.serve(fake) as base:
            models = await ollama.discover(self.session, base)
        self.assertTrue(models[0]['vision'])

    async def test_missing_show_and_fields_are_tolerated(self):
        fake = FakeOllama(tags=[{'name': 'mystery:latest'}], shows={})
        async with self.serve(fake) as base:
            models = await ollama.discover(self.session, base)
        self.assertEqual(models, [{'name': 'mystery:latest', 'size': 0, 'family': '', 'parameter_size': '',
                                   'context_length': None, 'vision': False}])

    async def test_unreachable_or_failing_server_is_empty(self):
        self.assertEqual(await ollama.discover(self.session, unreachable_url()), [])
        async with self.serve(FakeOllama(tags=TAGS, tags_status=500)) as base:
            self.assertEqual(await ollama.discover(self.session, base), [])
        self.assertEqual(await ollama.discover(self.session, 'http://not a url'), [])

class RunOllamaTests(OllamaServerCase):
    async def test_happy_path_returns_message(self):
        fake = FakeOllama(answers=[OK])
        async with self.serve(fake) as base:
            response = await ollama.run_ollama(request_for(user('hi')), self.session, base, 'llama3.2:3b')
        self.assertEqual(response['object'], 'response')
        self.assertEqual(response['status'], 'completed')
        self.assertTrue(response['id'].startswith('resp_'))
        self.assertEqual(response['model'], 'ollama-llama3.2-3b')
        self.assertEqual(response['rate_limit_headers'], {})
        self.assertEqual(response['usage'], {'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15,
                                             'input_tokens_details': {'cached_tokens': 0},
                                             'output_tokens_details': {'reasoning_tokens': 0}})
        item = response['output'][0]
        self.assertEqual(item['type'], 'message')
        self.assertEqual(item['channel'], 'final')
        self.assertEqual(item['content'], [{'type': 'output_text', 'text': 'Hello from llama', 'annotations': []}])
        self.assertEqual(len(fake.chats), 1)
        payload = fake.chats[0]
        self.assertEqual(payload['model'], 'llama3.2:3b')
        self.assertIs(payload['stream'], False)
        self.assertEqual(payload['format']['required'], ['message', 'channel', 'tool_calls'])
        self.assertEqual(payload['options'], {'num_ctx': 8192})
        self.assertEqual(payload['messages'][0]['role'], 'system')
        self.assertIn('You are Ollama', payload['messages'][0]['content'])
        self.assertEqual(payload['messages'][1]['role'], 'user')
        self.assertNotIn('images', payload['messages'][1])
        self.assertEqual(self.sent_prompt(payload)['conversation'][0]['content'], 'hi')

    async def test_num_ctx_uses_discovered_context_capped(self):
        fake = FakeOllama(answers=[OK, OK, OK], tags=TAGS, shows=dict(SHOWS, **{
            'small:1b': {'model_info': {'qwen2.context_length': 16384}}}))
        async with self.serve(fake) as base:
            await ollama.discover(self.session, base)
            await ollama.run_ollama(request_for(user('hi')), self.session, base, 'llama3.2:3b')
            self.assertEqual(fake.chats[-1]['options']['num_ctx'], 32768)
            # Not yet discovered: /api/show is consulted once and remembered.
            fake.show_requests.clear()
            await ollama.run_ollama(request_for(user('hi')), self.session, base, 'small:1b')
            await ollama.run_ollama(request_for(user('hi')), self.session, base, 'small:1b')
            self.assertEqual(fake.chats[-1]['options']['num_ctx'], 16384)
            self.assertEqual(fake.show_requests, ['small:1b'])

    async def test_tool_call_becomes_custom_tool_call(self):
        fake = FakeOllama(answers=[EXEC_CALL])
        request = request_for(TOOLS, user('list files'))
        async with self.serve(fake) as base:
            response = await ollama.run_ollama(request, self.session, base, 'llama3.2:3b')
        item = response['output'][0]
        self.assertEqual(item['type'], 'custom_tool_call')
        self.assertEqual(item['name'], 'exec')
        self.assertEqual(item['namespace'], 'functions')
        self.assertEqual(item['input'], EXEC_CALL['tool_calls'][0]['input'])
        self.assertTrue(item['call_id'].startswith('call_'))
        self.assertEqual(len(response['output']), 1)
        declared = self.sent_prompt(fake.chats[0])['available_tools']
        self.assertEqual([t['qualified_name'] for t in declared], ['functions.exec'])

    async def test_full_transcript_sent_even_when_resume_requested(self):
        fake = FakeOllama(answers=[OK])
        earlier = user('first question')
        latest = user('second question')
        request = request_for(TOOLS, earlier, {'type': 'message', 'role': 'assistant', 'content': 'answer'}, latest)
        async with self.serve(fake) as base:
            await ollama.run_ollama(request, self.session, base, 'llama3.2:3b', 240,
                                    'session-1', True, [latest])
        conversation = self.sent_prompt(fake.chats[0])['conversation']
        self.assertEqual([m['content'] for m in conversation], ['first question', 'answer', 'second question'])
        self.assertNotIn('continuation', self.sent_prompt(fake.chats[0]))

    async def test_images_forwarded_as_base64(self):
        fake = FakeOllama(answers=[OK])
        async with self.serve(fake) as base:
            request = request_for(
                {'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': 'what colour?'},
                    {'type': 'input_image', 'image_url': 'data:image/png;base64,iVBORw0KGgo='}]},
                {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                    {'type': 'input_text', 'text': 'screenshot taken'},
                    {'type': 'input_image', 'image_url': base + '/shot.png'}]})
            await ollama.run_ollama(request, self.session, base, 'llava:7b')
        payload = fake.chats[0]
        self.assertEqual(payload['messages'][1]['images'],
                         ['iVBORw0KGgo=', base64.b64encode(PNG_BYTES).decode()])
        transcript = json.dumps(self.sent_prompt(payload)['conversation'])
        self.assertIn('[image 1]', transcript)
        self.assertIn('[image 2]', transcript)
        self.assertNotIn('iVBORw0KGgo=', transcript)
        self.assertIn('[image N]', payload['messages'][0]['content'])

    async def test_oversized_url_image_rejected(self):
        fake = FakeOllama(answers=[OK])
        async with self.serve(fake) as base:
            request = request_for({'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_image', 'image_url': base + '/shot.png'}]})
            with patch('ollama.MAX_IMAGE_BYTES', len(PNG_BYTES) - 1):
                with self.assertRaises(BridgeError) as raised:
                    await ollama.run_ollama(request, self.session, base, 'llava:7b')
            self.assertIn('exceeds', str(raised.exception))
            self.assertEqual(fake.chats, [])
            request = request_for({'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_image', 'image_url': base + '/missing.png'}]})
            with self.assertRaises(BridgeError) as raised:
                await ollama.run_ollama(request, self.session, base, 'llava:7b')
            self.assertIn('404', str(raised.exception))

    async def test_non_json_content_retries_once(self):
        fake = FakeOllama(answers=['Sure! Here is my answer as prose.', OK])
        async with self.serve(fake) as base:
            response = await ollama.run_ollama(request_for(user('hi')), self.session, base, 'llama3.2:3b')
        self.assertEqual(response['output'][0]['content'][0]['text'], 'Hello from llama')
        self.assertEqual(len(fake.chats), 2)
        retry = fake.chats[1]['messages']
        self.assertEqual(retry[:2], fake.chats[0]['messages'])
        self.assertEqual(retry[2], {'role': 'assistant', 'content': 'Sure! Here is my answer as prose.'})
        self.assertEqual(retry[3]['role'], 'user')
        self.assertIn('Return only the JSON object', retry[3]['content'])
        # A second failure stops loudly instead of looping.
        fake = FakeOllama(answers=['nope', '{"not": "an envelope"}'])
        async with self.serve(fake) as base:
            with self.assertRaises(BridgeError) as raised:
                await ollama.run_ollama(request_for(user('hi')), self.session, base, 'llama3.2:3b')
        self.assertIn('JSON envelope', str(raised.exception))
        self.assertEqual(len(fake.chats), 2)

    async def test_fenced_json_accepted_without_retry(self):
        fake = FakeOllama(answers=['```json\n' + json.dumps(OK) + '\n```'])
        async with self.serve(fake) as base:
            response = await ollama.run_ollama(request_for(user('hi')), self.session, base, 'llama3.2:3b')
        self.assertEqual(response['output'][0]['content'][0]['text'], 'Hello from llama')
        self.assertEqual(len(fake.chats), 1)

    async def test_connection_refused_mentions_url(self):
        base = unreachable_url()
        with self.assertRaises(BridgeError) as raised:
            await ollama.run_ollama(request_for(user('hi')), self.session, base, 'llama3.2:3b')
        self.assertEqual(str(raised.exception), 'Ollama is not running at ' + base)

    async def test_http_error_surfaces_body(self):
        fake = FakeOllama(chat_status=404, chat_error="model 'nope:latest' not found, try pulling it first")
        async with self.serve(fake) as base:
            with self.assertRaises(BridgeError) as raised:
                await ollama.run_ollama(request_for(user('hi')), self.session, base, 'nope:latest')
        self.assertIn('404', str(raised.exception))
        self.assertIn("model 'nope:latest' not found", str(raised.exception))

    async def test_undeclared_tool_corrected_once_then_raised(self):
        fake = FakeOllama(answers=[WRONG_CALL, EXEC_CALL])
        request = request_for(TOOLS, user('list files'))
        async with self.serve(fake) as base:
            response = await ollama.run_ollama(request, self.session, base, 'llama3.2:3b')
        self.assertEqual(response['output'][0]['type'], 'custom_tool_call')
        self.assertEqual(response['output'][0]['name'], 'exec')
        self.assertEqual(len(fake.chats), 2)
        correction = fake.chats[1]['messages'][-1]
        self.assertEqual(correction['role'], 'user')
        self.assertIn('functions.exec_command', correction['content'])
        self.assertIn('functions.exec', correction['content'])
        self.assertEqual(fake.chats[1]['messages'][-2], {'role': 'assistant', 'content': json.dumps(WRONG_CALL)})
        fake = FakeOllama(answers=[WRONG_CALL, WRONG_CALL])
        async with self.serve(fake) as base:
            with self.assertRaises(UndeclaredTool) as raised:
                await ollama.run_ollama(request, self.session, base, 'llama3.2:3b')
        self.assertEqual(raised.exception.name, 'functions.exec_command')
        self.assertEqual(raised.exception.available, ['functions.exec'])
        self.assertEqual(len(fake.chats), 2)

    async def test_empty_turn_is_an_error(self):
        fake = FakeOllama(answers=[{'message': '', 'channel': 'final', 'tool_calls': []}])
        async with self.serve(fake) as base:
            with self.assertRaises(BridgeError):
                await ollama.run_ollama(request_for(user('hi')), self.session, base, 'llama3.2:3b')

if __name__ == '__main__':
    unittest.main()
