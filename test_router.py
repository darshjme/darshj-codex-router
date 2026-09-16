import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import zstandard
import router
import adapter
from adapter import translate, response_events, BridgeError, MODELS, CLAUDE_MODELS, GROK_MODELS, effort_of, rate_limit_headers

def result():
    return {'id': 'resp_test', 'object': 'response', 'status': 'completed', 'model': 'claude-max-sonnet',
            'output': [{'id': 'msg_test', 'type': 'message', 'status': 'completed', 'role': 'assistant',
                'channel': 'final', 'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}}

class AdapterTests(unittest.TestCase):
    def test_lite_namespaced_tools(self):
        req = {'input': [{'type': 'additional_tools', 'tools': [{'type': 'namespace', 'name': 'functions',
            'tools': [{'type': 'custom', 'name': 'exec'}]}]}, {'role': 'user', 'content': 'hello'}]}
        _, prompt, _, tools, _, _ = translate(req)
        self.assertIn('functions.exec', tools)
        self.assertEqual(json.loads(prompt)['conversation'][0]['content'], 'hello')

    def test_media_not_silently_dropped(self):
        # Images are forwarded natively; anything else is refused loudly.
        _, prompt, _, _, images, _ = translate({'input': [{'type': 'message', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,abc'}]}]})
        self.assertEqual(images[0]['source']['data'], 'abc')
        self.assertIn('[image 1]', prompt)
        with self.assertRaises(BridgeError):
            translate({'input': [{'type': 'message', 'content': [{'type': 'input_file', 'file_data': 'x'}]}]})
        with self.assertRaises(BridgeError):
            translate({'input': [{'type': 'message', 'content': [{'type': 'input_image', 'image_url': 'file:///tmp/x.png'}]}]})

    def test_event_contract(self):
        events = list(response_events(result()))
        self.assertEqual(events[0]['type'], 'response.created')
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))

    def test_quota_is_specific(self):
        self.assertTrue(router.is_quota({'error': {'type': 'usage_limit_reached'}}))
        for code in ['rate_limit_exceeded', 'invalid_api_key', 'server_error']:
            self.assertFalse(router.is_quota({'error': {'code': code}}))

    def test_catalog_slugs_are_pinned_model_names(self):
        catalog = json.loads((Path(__file__).parent / 'models.base.json').read_text())
        claude = {m['slug'] for m in catalog['models'] if m['slug'].startswith('claude-max-')}
        grok = {m['slug'] for m in catalog['models'] if m['slug'].startswith('grok-')}
        self.assertEqual(claude, set(CLAUDE_MODELS))
        self.assertEqual(grok, set(GROK_MODELS))
        self.assertEqual(claude | grok, set(MODELS))
        for entry in catalog['models']:
            if entry['slug'] in MODELS:
                efforts = [x['effort'] for x in entry['supported_reasoning_levels']]
                self.assertEqual(efforts, list(adapter.EFFORTS))
                self.assertIn(entry['default_reasoning_level'], efforts)

    def test_catalog_parity_with_astra(self):
        catalog = json.loads((Path(__file__).parent / 'models.base.json').read_text())
        astra = next(m for m in catalog['models'] if m['slug'] == 'gpt-6-astra')
        for entry in catalog['models']:
            if entry['slug'] in MODELS:
                self.assertEqual(entry['input_modalities'], ['text', 'image'], entry['slug'])
                # Codex re-sends the whole thread each turn; cap the window so it compacts.
                self.assertLessEqual(entry['context_window'], 200000, entry['slug'])
                self.assertTrue(entry['supports_search_tool'], entry['slug'])
                self.assertEqual(entry['model_messages'].keys(), astra['model_messages'].keys())

    def test_effort_slider_mapping(self):
        self.assertEqual(effort_of({'reasoning': {'effort': 'xhigh'}}), 'xhigh')
        self.assertEqual(effort_of({'reasoning_effort': 'HIGH'}), 'high')
        # Codex rungs Claude Code does not name are clamped, not passed through.
        self.assertEqual(effort_of({'reasoning': {'effort': 'ultra'}}), 'max')
        self.assertEqual(effort_of({'reasoning': {'effort': 'minimal'}}), 'low')
        for absent in [{}, {'reasoning': {}}, {'reasoning_effort': 'bogus'}, {'reasoning': None}]:
            self.assertIsNone(effort_of(absent))

    def test_claude_usage_becomes_codex_headers(self):
        records = [{'type': 'system'}, {'type': 'rate_limit_event', 'rate_limit_info': {
            'unifiedWindows': {'five_hour': {'utilization': 0.44, 'resetsAt': 1789525800},
                               'seven_day': {'utilization': 0.07, 'resetsAt': 1790082000}}}},
            {'type': 'result'}]
        self.assertEqual(rate_limit_headers(records), {
            'x-codex-primary-used-percent': '44', 'x-codex-primary-window-minutes': '300',
            'x-codex-primary-reset-at': '1789525800',
            'x-codex-secondary-used-percent': '7', 'x-codex-secondary-window-minutes': '10080',
            'x-codex-secondary-reset-at': '1790082000'})
        self.assertEqual(rate_limit_headers([{'type': 'result'}]), {})
        response = dict(result(), rate_limit_headers={'x-codex-primary-used-percent': '44'})
        events = list(response_events(response))
        self.assertTrue(all(e['headers'] == {'x-codex-primary-used-percent': '44'} for e in events))
        self.assertNotIn('rate_limit_headers', events[-1]['response'])


    def test_tool_outputs_are_bounded_at_the_bridge(self):
        big = 'a' * 40000 + 'Z' * 1000
        req = {'input': [{'role': 'user', 'content': 'go'},
                         {'type': 'function_call_output', 'call_id': 'c1', 'output': big},
                         {'type': 'custom_tool_call_output', 'call_id': 'c2', 'output': 'small'}]}
        _, prompt, _, _, _, _ = translate(req)
        conv = json.loads(prompt)['conversation']
        out = conv[1]['output']
        self.assertLessEqual(len(out.encode()), adapter.OUTPUT_LIMIT + 64)
        self.assertTrue(out.startswith('a' * adapter.OUTPUT_HEAD))
        self.assertTrue(out.endswith('Z' * 1000))
        self.assertIn('[bridge truncated %d bytes]' % (len(big) - adapter.OUTPUT_LIMIT), out)
        self.assertEqual(conv[2]['output'], 'small')
        # Image parts of a list-shaped output survive; only the text part is cut.
        parts = [{'type': 'input_image', 'image_url': 'data:image/png;base64,abc'}, {'type': 'input_text', 'text': big}]
        _, prompt, _, _, images, _ = translate({'input': [{'type': 'function_call_output', 'call_id': 'c3', 'output': parts}]})
        out = json.loads(prompt)['conversation'][0]['output']
        self.assertEqual(len(images), 1)
        self.assertEqual(out[0]['text'], '[image 1]')
        self.assertIn('[bridge truncated', out[1]['text'])
        # The per-send budget caps the sum of tool outputs; earlier ones keep priority.
        delta = [{'type': 'function_call_output', 'call_id': 'c%d' % i, 'output': chr(65 + i) * 30000} for i in range(5)]
        _, prompt, _, _, _, _ = translate({'input': []}, delta=delta)
        conv = json.loads(prompt)['conversation']
        self.assertEqual([x['output'] for x in conv[:3]], [d['output'] for d in delta[:3]])
        self.assertTrue(all('[bridge truncated' in x['output'] for x in conv[3:]))
        total = sum(len(x['output'].encode()) for x in conv)
        self.assertLessEqual(total, adapter.TURN_OUTPUT_BUDGET + adapter.OUTPUT_FLOOR + 2 * 64)

    def test_grok_drops_codex_only_prompt_sections(self):
        base = ('You are Codex, an agent based on GPT-6. Shared workspace.\n\n'
                '# When to ask the user for permission\n\napproval text\n\n'
                '# Autonomy and persistence\n\nkeep this\n\n# Personality\n\nwarm\n\n'
                '## Writing style\n\nplain\n\n# Plugins\n\nbundle\n\n## How to use plugins\n\nnaming\n')
        req = {'input': [{'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': base}]},
                         {'type': 'message', 'role': 'user', 'content': 'hi'}]}
        _, prompt, _, _, _, _ = translate(req, trim=adapter.trim_base_instructions)
        text = json.loads(prompt)['conversation'][0]['content'][0]['text']
        self.assertTrue(text.startswith('You are Codex, an agent based on GPT-6. Shared workspace.'))
        self.assertIn('# Autonomy and persistence\n\nkeep this', text)
        self.assertIn('## Writing style\n\nplain', text)
        for gone in ('# When to ask the user for permission', 'approval text', '# Personality', 'warm', '# Plugins', 'naming'):
            self.assertNotIn(gone, text)
        # Claude keeps the full prompt (one cache write per session); other developer text is untouched.
        _, prompt, _, _, _, _ = translate(req)
        self.assertIn('# Personality', json.loads(prompt)['conversation'][0]['content'][0]['text'])
        self.assertEqual(adapter.trim_base_instructions('# Personality\nother developer note'), '# Personality\nother developer note')

    def test_grok_trim_matches_headings_case_insensitively(self):
        # Codex's per-model prompt variants differ in heading case (gpt-reserve base
        # prompt: "## Technical communication"); the trim must still catch them.
        base = ('You are Codex, an agent based on GPT-6.\n\n# personality\n\nwarm\n\n'
                '## Writing style\n\nplain\n\n## Technical communication\n\nlead with outcome\n\n'
                '# Working with the user\n\nkeep\n')
        text = adapter.trim_base_instructions(base)
        self.assertNotIn('warm', text)
        self.assertNotIn('lead with outcome', text)
        self.assertIn('## Writing style\n\nplain', text)
        self.assertIn('# Working with the user\n\nkeep', text)

    def test_checkpoint_images_become_placeholders(self):
        data = 'data:image/png;base64,' + 'A' * 5000
        material = [{'type': 'message', 'role': 'user', 'content': [
                        {'type': 'input_text', 'text': 'look'},
                        {'type': 'input_image', 'image_url': data}]},
                    {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                        {'type': 'input_image', 'image_url': {'url': data, 'detail': 'high'}},
                        {'type': 'input_text', 'text': 'done'}]},
                    {'type': 'message', 'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': data}}]},
                    {'type': 'message', 'role': 'user', 'content': data}]
        stripped = router.strip_checkpoint_images(material)
        text = json.dumps(stripped)
        self.assertNotIn('base64', text)
        self.assertNotIn('AAAA', text)
        self.assertEqual(stripped[0]['content'][0], {'type': 'input_text', 'text': 'look'})
        self.assertEqual(stripped[0]['content'][1], {'type': 'input_text', 'text': '[image 1]'})
        self.assertEqual(stripped[1]['output'][0], {'type': 'input_text', 'text': '[image 2]'})
        self.assertEqual(stripped[1]['output'][1], {'type': 'input_text', 'text': 'done'})
        self.assertEqual(stripped[2]['content'][0], {'type': 'input_text', 'text': '[image 3]'})
        self.assertEqual(stripped[3]['content'], '[image 4]')
        self.assertLess(len(text), 1000)
        # The original material is not mutated.
        self.assertIn('AAAA', json.dumps(material))


class ClaudeInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, request):
        captured = {}
        class FakeProcess:
            returncode = 0
            pid = 1
            async def communicate(self, data):
                payload = {'type': 'result', 'subtype': 'success', 'is_error': False,
                           'structured_output': {'message': 'OK', 'channel': 'final', 'tool_calls': []},
                           'usage': {'input_tokens': 1, 'output_tokens': 1}}
                return json.dumps(payload).encode(), b''
        async def fake_exec(*args, **kwargs):
            captured['args'] = args
            return FakeProcess()
        with patch('asyncio.create_subprocess_exec', fake_exec):
            response = await adapter.run_claude(request, '/fake/claude', '/tmp')
        return captured['args'], response

    async def test_selected_model_and_effort_reach_claude_code(self):
        for slug, expected in CLAUDE_MODELS.items():
            args, response = await self.invoke(
                {'model': slug, 'input': 'hi', 'reasoning': {'effort': 'xhigh'}})
            self.assertEqual(args[args.index('--model') + 1], expected)
            self.assertEqual(args[args.index('--effort') + 1], 'xhigh')
            self.assertEqual(response['model'], slug)

    async def test_api_failure_surfaces_real_reason(self):
        # Claude Code reports auth/usage failures with subtype "success" and
        # is_error true; the human reason lives in the result text.
        class FakeProcess:
            returncode = 0
            pid = 1
            async def communicate(self, data):
                payload = {'type': 'result', 'subtype': 'success', 'is_error': True,
                           'result': 'Failed to authenticate. API Error: 401 OAuth access token is invalid.'}
                return json.dumps(payload).encode(), b''
        async def fake_exec(*args, **kwargs):
            return FakeProcess()
        with patch('asyncio.create_subprocess_exec', fake_exec):
            with self.assertRaises(BridgeError) as raised:
                await adapter.run_claude({'model': 'claude-max-fable', 'input': 'hi'}, '/fake/claude', '/tmp')
        self.assertIn('401 OAuth access token is invalid', str(raised.exception))
        self.assertNotIn('failed: success', str(raised.exception))

    async def test_absent_effort_leaves_claude_default(self):
        args, _ = await self.invoke({'model': 'claude-max-fable', 'input': 'hi'})
        self.assertNotIn('--effort', args)
        self.assertEqual(args[args.index('--model') + 1], 'claude-fable-5-1')

    async def capture(self, request, **kwargs):
        captured = {}
        class FakeProcess:
            returncode = 0
            pid = 1
            async def communicate(self, data):
                captured['stdin'] = data
                payload = {'type': 'result', 'subtype': 'success', 'is_error': False,
                           'structured_output': {'message': 'OK', 'channel': 'final', 'tool_calls': []},
                           'usage': {'input_tokens': 1, 'output_tokens': 1}}
                return json.dumps(payload).encode(), b''
        async def fake_exec(*args, **kwargs):
            captured['args'] = args
            return FakeProcess()
        with patch('asyncio.create_subprocess_exec', fake_exec):
            await adapter.run_claude(request, '/fake/claude', '/tmp', **kwargs)
        return captured

    async def test_images_forwarded_natively(self):
        png = 'data:image/png;base64,' + 'iVBORw0KGgo='
        request = {'model': 'claude-max-fable', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'what colour?'},
                {'type': 'input_image', 'image_url': png, 'detail': 'original'}]},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                {'type': 'input_text', 'text': 'screenshot taken'},
                {'type': 'input_image', 'image_url': 'https://example.com/shot.png'}]}]}
        captured = await self.capture(request)
        args = captured['args']
        self.assertEqual(args[args.index('--input-format') + 1], 'stream-json')
        lines = [json.loads(l) for l in captured['stdin'].decode().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        content = lines[0]['message']['content']
        images = [c for c in content if c['type'] == 'image']
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]['source'], {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='})
        self.assertEqual(images[1]['source'], {'type': 'url', 'url': 'https://example.com/shot.png'})
        # The transcript keeps a numbered placeholder where each image sat.
        transcript = json.dumps(json.loads(content[0]['text'])['conversation'])
        self.assertIn('[image 1]', transcript)
        self.assertIn('[image 2]', transcript)
        self.assertNotIn('iVBORw0KGgo=', transcript)

    async def test_non_image_media_still_rejected(self):
        with self.assertRaises(BridgeError):
            translate({'input': [{'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_audio', 'input_audio': {'data': 'AAAA', 'format': 'wav'}}]}]})

    async def test_web_search_enables_native_claude_tools(self):
        request = {'model': 'claude-max-fable', 'input': 'latest news',
                   'tools': [{'type': 'web_search'}, {'type': 'function', 'name': 'exec', 'parameters': {}}]}
        captured = await self.capture(request)
        args = captured['args']
        self.assertEqual(args[args.index('--tools') + 1], 'WebSearch,WebFetch')
        self.assertIn('WebSearch', args[args.index('--system-prompt') + 1])
        captured = await self.capture({'model': 'claude-max-fable', 'input': 'hi'})
        self.assertEqual(captured['args'][captured['args'].index('--tools') + 1], '')


    async def test_resumed_session_sends_only_new_items(self):
        full = {'model': 'claude-max-fable', 'tools': [{'type': 'function', 'name': 'exec', 'parameters': {}}],
                'input': [{'type': 'message', 'role': 'user', 'content': 'first'},
                          {'type': 'message', 'role': 'assistant', 'content': 'ok'},
                          {'type': 'function_call_output', 'call_id': 'c1', 'output': 'done'}]}
        delta = full['input'][-1:]
        captured = await self.capture(full, session='sid-1', resume=True, delta=delta)
        args = captured['args']
        self.assertEqual(args[args.index('--resume') + 1], 'sid-1')
        self.assertNotIn('--session-id', args)
        self.assertNotIn('--no-session-persistence', args)
        body = json.loads(json.loads(captured['stdin'].decode())['message']['content'][0]['text'])
        self.assertEqual(body['conversation'], delta)
        self.assertTrue(body['continuation'])
        # Tools declared earlier in the thread are not re-described (they are in the
        # session already) but remain known for validating the tool call.
        self.assertEqual(body['available_tools'], [])
        added = [{'type': 'additional_tools', 'tools': [{'type': 'function', 'name': 'newtool', 'parameters': {}}]}] + delta
        captured = await self.capture(dict(full, input=full['input'] + added), session='sid-1', resume=True, delta=added)
        body = json.loads(json.loads(captured['stdin'].decode())['message']['content'][0]['text'])
        self.assertEqual([t['qualified_name'] for t in body['available_tools']], ['newtool'])
        captured = await self.capture(full, session='sid-2', resume=False)
        args = captured['args']
        self.assertEqual(args[args.index('--session-id') + 1], 'sid-2')
        self.assertNotIn('--resume', args)
        body = json.loads(json.loads(captured['stdin'].decode())['message']['content'][0]['text'])
        self.assertEqual(len(body['conversation']), 3)
        self.assertNotIn('continuation', body)

    async def test_undeclared_tool_is_corrected_in_session(self):
        # First answer names a JS helper as if it were a tool; the bridge must correct
        # Claude inside the same session (cheap, cached) rather than fail the turn.
        answers = [{'message': '', 'channel': 'commentary', 'tool_calls': [{'name': 'functions.exec_command', 'input': '{"cmd":"ls"}'}]},
                   {'message': '', 'channel': 'commentary', 'tool_calls': [{'name': 'functions.exec', 'input': 'text(await tools.exec_command({cmd:"ls"}));'}]}]
        calls = []
        class FakeProcess:
            returncode = 0
            pid = 1
            async def communicate(self, data):
                calls.append(json.loads(data.decode()))
                payload = {'type': 'result', 'subtype': 'success', 'is_error': False,
                           'structured_output': answers[len(calls) - 1], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
                return json.dumps(payload).encode(), b''
        args_seen = []
        async def fake_exec(*args, **kwargs):
            args_seen.append(args)
            return FakeProcess()
        request = {'model': 'claude-max-fable', 'input': [
            {'type': 'additional_tools', 'tools': [{'type': 'namespace', 'name': 'functions', 'tools': [{'type': 'custom', 'name': 'exec'}]}]},
            {'type': 'message', 'role': 'user', 'content': 'list files'}]}
        with patch('asyncio.create_subprocess_exec', fake_exec):
            response = await adapter.run_claude(request, '/fake/claude', '/tmp', 240, 'sid-9', False, None)
        self.assertEqual(len(args_seen), 2)
        self.assertEqual(args_seen[0][args_seen[0].index('--session-id') + 1], 'sid-9')
        self.assertEqual(args_seen[1][args_seen[1].index('--resume') + 1], 'sid-9')
        correction = calls[1]['message']['content'][0]['text']
        self.assertIn('functions.exec_command', correction)
        self.assertIn('functions.exec', correction)
        self.assertEqual(response['output'][0]['type'], 'custom_tool_call')
        self.assertEqual(response['output'][0]['name'], 'exec')
        # A second wrong answer still fails loudly rather than looping.
        answers.append(answers[0]); answers[1] = answers[0]
        calls.clear(); args_seen.clear()
        with patch('asyncio.create_subprocess_exec', fake_exec):
            with self.assertRaises(BridgeError):
                await adapter.run_claude(request, '/fake/claude', '/tmp', 240, 'sid-9', False, None)
        self.assertEqual(len(args_seen), 2)

    def test_prompt_names_only_declared_tools_callable(self):
        system, _, _, _, _, _ = translate({'input': 'hi', 'tools': [{'type': 'custom', 'name': 'exec'}]})
        self.assertIn('qualified_name', system)
        self.assertIn('exec_command', system)

class GrokInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, request, **kwargs):
        captured = {}
        class FakeProcess:
            returncode = 0
            pid = 1
            async def communicate(self, data):
                captured['stdin'] = data
                payload = {'type': 'result', 'subtype': 'success', 'is_error': False,
                           'structured_output': {'message': 'OK', 'channel': 'final', 'tool_calls': []},
                           'usage': {'input_tokens': 1, 'output_tokens': 1}}
                return json.dumps(payload).encode(), b''
        async def fake_exec(*args, **kwargs):
            captured['args'] = args
            captured['env'] = kwargs.get('env') or {}
            if '--prompt-file' in args:
                captured['prompt'] = Path(args[args.index('--prompt-file') + 1]).read_text()
            elif '--prompt-json' in args:
                captured['prompt'] = json.loads(args[args.index('--prompt-json') + 1])[0]['text']
            return FakeProcess()
        with patch('asyncio.create_subprocess_exec', fake_exec), tempfile.TemporaryDirectory() as cwd:
            response = await adapter.run_grok(request, '/fake/grok', cwd, **kwargs)
        return captured, response

    async def test_only_media_from_this_turn_is_attached(self):
        import os, time
        class FakeProcess:
            returncode = 0
            pid = 1
            async def communicate(self, data):
                payload = {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'none',
                           'structured_output': {'message': 'done', 'channel': 'final', 'tool_calls': []},
                           'usage': {'input_tokens': 1, 'output_tokens': 1}}
                return json.dumps(payload).encode(), b''
        async def fake_exec(*args, **kwargs):
            # Simulate Imagine writing a fresh file during the turn.
            (Path(cwd) / 'images').mkdir(exist_ok=True)
            (Path(cwd) / 'images' / 'fresh.jpg').write_bytes(b'FRESH')
            return FakeProcess()
        with tempfile.TemporaryDirectory() as cwd:
            stale_dir = Path(cwd) / 'media'; stale_dir.mkdir()
            stale = stale_dir / 'imagine-1.jpg'; stale.write_bytes(b'STALE')
            os.utime(stale, (time.time() - 3600, time.time() - 3600))
            with patch('asyncio.create_subprocess_exec', fake_exec):
                response = await adapter.run_grok({'model': 'grok-max', 'input': 'count lines'}, '/fake/grok', cwd)
            text = response['output'][0]['content'][0]['text']
            import base64
            self.assertEqual(text.count('\n- `'), 1, text)
            self.assertIn(base64.b64encode(b'FRESH').decode(), text)
            self.assertNotIn(base64.b64encode(b'STALE').decode(), text)

    async def test_grok_resumes_session_with_delta_only(self):
        full = {'model': 'grok-max', 'tools': [{'type': 'function', 'name': 'exec', 'parameters': {}}],
                'input': [{'type': 'message', 'role': 'user', 'content': 'first'},
                          {'type': 'message', 'role': 'assistant', 'content': 'ok'},
                          {'type': 'function_call_output', 'call_id': 'c1', 'output': 'done'}]}
        delta = full['input'][-1:]
        captured, _ = await self.invoke(full, session='11111111-1111-4111-8111-111111111111', resume=True, delta=delta)
        args = captured['args']
        self.assertEqual(args[args.index('--resume') + 1], '11111111-1111-4111-8111-111111111111')
        self.assertNotIn('--session-id', args)
        body = json.loads(captured['prompt'])
        self.assertEqual(body['conversation'], delta)
        self.assertTrue(body['continuation'])
        self.assertEqual(body['available_tools'], [])
        captured, _ = await self.invoke(full, session='22222222-2222-4222-8222-222222222222', resume=False)
        args = captured['args']
        self.assertEqual(args[args.index('--session-id') + 1], '22222222-2222-4222-8222-222222222222')
        self.assertNotIn('--resume', args)
        self.assertEqual(len(json.loads(captured['prompt'])['conversation']), 3)

    async def test_grok_undeclared_tool_corrected_in_session(self):
        answers = [{'message': '', 'channel': 'commentary', 'tool_calls': [{'name': 'functions.exec_command', 'input': '{}'}]},
                   {'message': '', 'channel': 'commentary', 'tool_calls': [{'name': 'functions.exec', 'input': 'text(1)'}]}]
        seen = []
        class FakeProcess:
            returncode = 0
            pid = 1
            async def communicate(self, data):
                payload = {'type': 'result', 'subtype': 'success', 'is_error': False,
                           'structured_output': answers[len(seen) - 1], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
                return json.dumps(payload).encode(), b''
        async def fake_exec(*args, **kwargs):
            seen.append(args)
            if '--prompt-file' in args:
                seen[-1] = args + (Path(args[args.index('--prompt-file') + 1]).read_text(),)
            return FakeProcess()
        request = {'model': 'grok-max', 'input': [
            {'type': 'additional_tools', 'tools': [{'type': 'namespace', 'name': 'functions', 'tools': [{'type': 'custom', 'name': 'exec'}]}]},
            {'type': 'message', 'role': 'user', 'content': 'list files'}]}
        with patch('asyncio.create_subprocess_exec', fake_exec), tempfile.TemporaryDirectory() as cwd:
            response = await adapter.run_grok(request, '/fake/grok', cwd, 240, '33333333-3333-4333-8333-333333333333', False, None)
        self.assertEqual(len(seen), 2)
        self.assertIn('--resume', seen[1])
        self.assertIn('functions.exec_command', seen[1][-1])
        self.assertEqual(response['output'][0]['name'], 'exec')

    async def test_selected_model_and_effort_reach_grok(self):
        captured, response = await self.invoke(
            {'model': 'grok-max', 'input': 'hi', 'reasoning': {'effort': 'high'}})
        args = captured['args']
        self.assertEqual(args[args.index('--model') + 1], 'grok-4.6')
        self.assertEqual(args[args.index('--effort') + 1], 'high')
        self.assertEqual(args[args.index('--output-format') + 1], 'streaming-messages-json')
        self.assertEqual(response['model'], 'grok-max')
        self.assertIn('--prompt-file', args)
        self.assertNotIn('--print', args)
        self.assertIn('--disable-web-search', args)
        self.assertIn('--always-approve', args)
        self.assertIn('--no-leader', args)
        self.assertEqual(args[args.index('--max-turns') + 1], adapter.GROK_MAX_TURNS)
        self.assertEqual(args[args.index('--tools') + 1], adapter.GROK_IMAGINE_TOOLS)
        self.assertIn('Grok Imagine', args[args.index('--system-prompt-override') + 1])
        self.assertEqual(captured['env'].get('GROK_CLAUDE_MCPS_ENABLED'), 'false')
        self.assertEqual(captured['env'].get('GROK_CURSOR_MCPS_ENABLED'), 'false')

    async def test_search_enables_native_grok_tools(self):
        captured, _ = await self.invoke({'model': 'grok-max', 'input': 'news',
                                         'tools': [{'type': 'web_search'}]})
        args = captured['args']
        self.assertEqual(args[args.index('--tools') + 1],
                         adapter.GROK_IMAGINE_TOOLS + ',' + adapter.GROK_SEARCH_TOOLS)
        self.assertNotIn('--disable-web-search', args)
        self.assertIn('web_search and web_fetch', args[args.index('--system-prompt-override') + 1])

    async def test_images_use_prompt_json(self):
        png = 'data:image/png;base64,' + 'iVBORw0KGgo='
        captured, _ = await self.invoke({'model': 'grok-max', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'what colour?'},
                {'type': 'input_image', 'image_url': png}]}]})
        args = captured['args']
        blocks = json.loads(args[args.index('--prompt-json') + 1])
        self.assertEqual(blocks[0]['type'], 'text')
        self.assertEqual(blocks[1], {'type': 'image', 'data': 'iVBORw0KGgo=', 'mimeType': 'image/png'})
        self.assertNotIn('source', blocks[1])
        self.assertNotIn('--prompt-file', args)

    async def test_invalid_output_includes_cli_stderr(self):
        class FakeProcess:
            returncode = 1
            pid = 1
            async def communicate(self, data):
                return b'', b'Error: --prompt-json: Invalid ACP content blocks: missing field `data`\n'
        async def fake_exec(*args, **kwargs):
            return FakeProcess()
        with patch('asyncio.create_subprocess_exec', fake_exec), tempfile.TemporaryDirectory() as cwd:
            with self.assertRaises(adapter.BridgeError) as raised:
                await adapter.run_grok({'model': 'grok-max', 'input': 'hi'}, '/fake/grok', cwd)
        self.assertIn('missing field `data`', str(raised.exception))
        self.assertIn('exit 1', str(raised.exception))

    def test_imagine_media_is_copied_and_inlined(self):
        with tempfile.TemporaryDirectory() as cwd:
            source = Path(cwd) / 'images'
            source.mkdir()
            png = source / '1.png'
            png.write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * 16)
            mp4 = source / 'clip.mp4'
            mp4.write_bytes(b'ftyp')
            records = [{'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'content': str(png)}]}},
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'text', 'text': 'saved ' + str(mp4)}]}}]
            paths = adapter.collect_grok_media(records, cwd, None)
            self.assertEqual(set(paths), {png.resolve(), mp4.resolve()})
            response = {'output': [{'type': 'message', 'id': 'msg', 'content': [
                {'type': 'output_text', 'text': 'done', 'annotations': []}]}]}
            updated = adapter.attach_imagine_media(response, records, cwd, {})
            text = updated['output'][0]['content'][0]['text']
            self.assertIn('Grok Imagine', text)
            media = Path(cwd) / 'media'
            self.assertTrue((media / 'imagine-1.png').is_file())
            self.assertTrue((media / 'imagine-2.mp4').is_file())
            self.assertIn('data:image/png;base64,', text)
            self.assertIn(str(media / 'imagine-2.mp4'), text)
            self.assertNotIn('data:video', text)

    def test_structured_output_falls_back_to_result_json(self):
        result = {'type': 'result', 'subtype': 'success', 'is_error': False,
                  'result': '{"message":"hi","channel":"final","tool_calls":[]}'}
        data = adapter.structured_payload(result, [])
        self.assertEqual(data['message'], 'hi')
        records = [{'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': '{"message":"from-text","channel":"final","tool_calls":[]}'}]}}]
        data = adapter.structured_payload({'type': 'result'}, records)
        self.assertEqual(data['message'], 'from-text')


    async def test_grok_receives_trimmed_codex_prompt(self):
        base = 'You are Codex, an agent based on GPT-6.\n\n# Personality\n\nwarm\n\n# Autonomy and persistence\n\nkeep\n'
        req = {'model': 'grok-max', 'input': [{'type': 'message', 'role': 'developer', 'content': base},
                                              {'type': 'message', 'role': 'user', 'content': 'hi'}]}
        captured, _ = await self.invoke(req)
        text = json.loads(captured['prompt'])['conversation'][0]['content']
        self.assertNotIn('# Personality', text)
        self.assertIn('# Autonomy and persistence', text)

    async def test_grok_preamble_points_at_memory_bus(self):
        captured, _ = await self.invoke({'model': 'grok-max', 'input': [{'type': 'message', 'role': 'user', 'content': 'hi'}]})
        args = captured['args']
        system = args[args.index('--system-prompt-override') + 1]
        self.assertIn('Durable user memory is available via the memory-bus', system)
        self.assertLess(len(adapter.GROK_MEMORY_SYSTEM), 200)
        self.assertLess(system.index('memory-bus'), system.index('Image and video generation'))


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        (path / 'catalog.json').write_text('{"models":[]}')
        self.app = router.create_app(path / 'catalog.json', '/unused/claude', path)
        # Never read the real ~/.codex/config.toml from tests.
        self.app['router'].codex_config = path / 'codex-config.toml'
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_native_auth_and_origin_required(self):
        r = await self.client.get('/models')
        self.assertEqual(r.status, 401)
        r = await self.client.get('/health', headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(r.status, 403)

    async def test_compressed_http_claude(self):
        async def fake(*args):
            return result()
        with patch('router.run_claude', fake):
            r = await self.client.post('/responses', data=zstandard.ZstdCompressor().compress(
                json.dumps({'model': 'claude-max-sonnet', 'input': 'hello', 'stream': False}).encode()),
                headers={'Authorization': 'Bearer test', 'Content-Encoding': 'zstd'})
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())['id'], 'resp_test')

    async def test_zstd_without_content_size(self):
        raw = json.dumps({'model': 'claude-max-sonnet', 'input': 'hello', 'stream': False}).encode()
        async def fake(*args):
            return result()
        with patch('router.run_claude', fake):
            r = await self.client.post('/responses', data=raw,
                headers={'Authorization': 'Bearer test', 'Content-Encoding': 'zstd'})
            self.assertEqual(r.status, 200, await r.text())
            self.assertEqual((await r.json())['id'], 'resp_test')
            skippable = b'\x50\x2a\x4d\x18\x04\x00\x00\x00skip' + zstandard.ZstdCompressor().compress(raw)
            r = await self.client.post('/responses', data=skippable,
                headers={'Authorization': 'Bearer test', 'Content-Encoding': 'zstd'})
            self.assertEqual(r.status, 200, await r.text())

    async def test_websocket_acks_before_bridge_finishes(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def fake(*args):
            started.set()
            await release.wait()
            return result()
        with patch('router.run_claude', fake):
            ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer test'})
            await ws.send_json({'model': 'claude-max-sonnet', 'input': []})
            first = await asyncio.wait_for(ws.receive_json(), 3)
            self.assertEqual(first['type'], 'response.created')
            await asyncio.wait_for(started.wait(), 3)
            release.set()
            seen = [first]
            while seen[-1]['type'] != 'response.completed':
                seen.append(await asyncio.wait_for(ws.receive_json(), 3))
            await ws.close()
            self.assertEqual(seen[1]['type'], 'response.in_progress')

    async def test_http_grok(self):
        async def fake(*args):
            return dict(result(), model='grok-max')
        with patch('router.run_grok', fake):
            r = await self.client.post('/responses',
                json={'model': 'grok-max', 'input': 'hello', 'stream': False},
                headers={'Authorization': 'Bearer test'})
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual(body['model'], 'grok-max')
            self.assertEqual(self.app['router'].counts['grok'], 1)

    async def test_previous_response_context(self):
        r = self.app['router']
        r.remember({'input': [{'role': 'user', 'content': 'first'}]}, result())
        expanded = r.expand({'previous_response_id': 'resp_test', 'input': [{'role': 'user', 'content': 'second'}]})
        self.assertEqual(len(expanded['input']), 3)
        with self.assertRaises(BridgeError):
            r.expand({'previous_response_id': 'missing'})

    async def test_compaction_roundtrip(self):
        async def fake(*args):
            return result()
        with patch('router.run_claude', fake):
            response = await self.app['router'].claude_response({'model': 'claude-max-sonnet',
                'input': [{'role': 'user', 'content': 'Summarize me'}, {'type': 'compaction_trigger'}]})
        self.assertEqual(response['output'][0]['type'], 'compaction')
        _, prompt, _, _, _, _ = translate({'input': response['output']})
        self.assertIn('OK', json.loads(prompt)['conversation'][0]['content'])
        self.assertEqual(list(response_events(response))[-1]['type'], 'response.completed')

    async def test_compaction_prompt_carries_no_image_data(self):
        seen = []
        async def fake(req, *args):
            seen.append(req)
            return result()
        data = 'data:image/png;base64,' + 'B' * 20000
        with patch('router.run_claude', fake):
            await self.app['router'].claude_response({'model': 'claude-max-sonnet', 'input': [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'see'},
                                                                {'type': 'input_image', 'image_url': data}]},
                {'type': 'function_call_output', 'call_id': 'c1', 'output': [{'type': 'input_image', 'image_url': data}]},
                {'type': 'compaction_trigger'}]})
        summary_request = json.dumps(seen[-1])
        self.assertIn('Summarize this conversation', summary_request)
        self.assertNotIn('BBBB', summary_request)
        self.assertIn('[image 1]', summary_request)
        self.assertIn('[image 2]', summary_request)

    async def run_upstream_case(self, code, partial=False):
        async def upstream_handler(req):
            ws = web.WebSocketResponse()
            await ws.prepare(req)
            async for msg in ws:
                if partial:
                    await ws.send_json({'type': 'response.output_text.delta', 'delta': 'partial'})
                await ws.send_json({'type': 'error', 'error': {'code': code}})
            return ws
        app = web.Application()
        app.router.add_get('/responses', upstream_handler)
        server = TestServer(app)
        await server.start_server()
        calls = []
        async def fake(req, *args):
            calls.append(req)
            return result()
        try:
            with patch('router.UPSTREAM', str(server.make_url('')).rstrip('/')), patch('router.run_claude', fake):
                ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer test'})
                await ws.send_json({'type': 'response.create', 'model': 'gpt-6-astra', 'input': []})
                seen = []
                while True:
                    event = await asyncio.wait_for(ws.receive_json(), 3)
                    seen.append(event)
                    if event['type'] in ('error', 'response.completed'):
                        break
                await ws.close()
                return calls, seen
        finally:
            await server.close()

    async def test_voice_live_and_realtime_passthrough(self):
        seen = []
        async def live(request):
            seen.append((request.method, request.path_qs, request.headers.get('Content-Type'), await request.text()))
            return web.Response(text='v=0 answer', content_type='application/sdp')
        async def realtime_ws(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await ws.send_str('echo:' + msg.data)
                elif msg.type == web.WSMsgType.BINARY:
                    await ws.send_bytes(b'bin:' + msg.data)
                break
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_post('/live', live)
        app.router.add_get('/realtime', realtime_ws)
        async with TestServer(app) as server:
            with patch('router.UPSTREAM', str(server.make_url('')).rstrip('/')):
                r = await self.client.post('/live?model=gpt-live-1-codex', data='v=0 offer',
                        headers={'Authorization': 'Bearer t', 'Content-Type': 'application/sdp'})
                self.assertEqual(r.status, 200)
                self.assertEqual(await r.text(), 'v=0 answer')
                self.assertEqual(seen[0][:3], ('POST', '/live?model=gpt-live-1-codex', 'application/sdp'))
                ws = await self.client.ws_connect('/realtime?model=gpt-realtime', headers={'Authorization': 'Bearer t'})
                await ws.send_str('hello')
                self.assertEqual((await ws.receive()).data, 'echo:hello')
                await ws.close()
                ws = await self.client.ws_connect('/realtime', headers={'Authorization': 'Bearer t'})
                await ws.send_bytes(b'\x01\x02')
                self.assertEqual((await ws.receive()).data, b'bin:\x01\x02')
                await ws.close()
        r = await self.client.post('/live', data='x', headers={'Authorization': 'Bearer t'})
        self.assertNotEqual(r.status, 404)
        r = await self.client.post('/anything-else', data='x', headers={'Authorization': 'Bearer t'})
        self.assertEqual(r.status, 404)

    async def test_alpha_search_passthrough(self):
        seen = []
        async def search(request):
            seen.append((request.path_qs, await request.json()))
            return web.json_response({'results': [{'title': 'python.org'}]})
        app = web.Application()
        app.router.add_post('/alpha/search', search)
        async with TestServer(app) as server:
            with patch('router.UPSTREAM', str(server.make_url('')).rstrip('/')):
                r = await self.client.post('/alpha/search', json={'model': 'claude-max-sonnet', 'commands': {'search_query': [{'q': 'python'}]}},
                        headers={'Authorization': 'Bearer t'})
                self.assertEqual(r.status, 200)
                self.assertEqual((await r.json())['results'][0]['title'], 'python.org')
                self.assertEqual(seen[0][1]['commands']['search_query'][0]['q'], 'python')

    def sse(self, events):
        return ''.join('event: %s\ndata: %s\n\n' % (e['type'], json.dumps(e)) for e in events)

    async def test_openai_checkpoint_converted_once_then_cached(self):
        hits = []
        async def responses(request):
            body = await request.json()
            hits.append(body)
            self.assertEqual(body['input'][0]['type'], 'compaction')
            self.assertEqual(body['input'][0]['encrypted_content'], 'gAAAAopaque')
            return web.Response(text=self.sse([
                {'type': 'response.created', 'response': {'id': 'r'}},
                {'type': 'response.completed', 'response': {'id': 'r', 'output': [{'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'The user was building a CLI in Rust; tests pass.'}]}]}}]),
                content_type='text/event-stream')
        app = web.Application()
        app.router.add_post('/responses', responses)
        prompts = []
        async def fake(req, *args):
            prompts.append(translate(req)[1])
            return result()
        request = {'model': 'claude-max-sonnet', 'input': [
            {'type': 'compaction', 'encrypted_content': 'gAAAAopaque'},
            {'type': 'message', 'role': 'user', 'content': 'continue'}]}
        async with TestServer(app) as server:
            with patch('router.UPSTREAM', str(server.make_url('')).rstrip('/')), patch('router.run_claude', fake):
                for _ in range(2):
                    r = await self.client.post('/responses', json=dict(request, stream=False), headers={'Authorization': 'Bearer t'})
                    self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['model'], router.CHECKPOINT_MODELS[0])
        for prompt in prompts:
            conversation = json.loads(prompt)['conversation']
            self.assertIn('building a CLI in Rust', conversation[0]['content'])
            self.assertEqual(conversation[1]['content'], 'continue')

    async def test_openai_checkpoint_falls_back_to_notice(self):
        async def responses(request):
            return web.json_response({'error': {'type': 'usage_limit_reached', 'message': 'limit'}}, status=429)
        app = web.Application()
        app.router.add_post('/responses', responses)
        prompts = []
        async def fake(req, *args):
            prompts.append(translate(req)[1])
            return result()
        request = {'model': 'claude-max-sonnet', 'input': [
            {'type': 'compaction', 'encrypted_content': 'gAAAAother'},
            {'type': 'message', 'role': 'user', 'content': 'continue'}]}
        async with TestServer(app) as server:
            with patch('router.UPSTREAM', str(server.make_url('')).rstrip('/')), patch('router.run_claude', fake):
                r = await self.client.post('/responses', json=dict(request, stream=False), headers={'Authorization': 'Bearer t'})
                self.assertEqual(r.status, 200, await r.text())
        conversation = json.loads(prompts[0])['conversation']
        self.assertIn('cannot be read by the selected model', conversation[0]['content'])
        self.assertIn('usage_limit_reached', conversation[0]['content'])

    async def test_thread_continues_one_claude_session(self):
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append({'items': len(req['input']), 'session': session, 'resume': resume, 'delta': delta, 'model': req['model']})
            return dict(result(), id='resp_%d' % len(calls), model=req['model'])
        r = self.app['router']
        with patch('router.run_claude', fake):
            first = await r.claude_response({'model': 'claude-max-sonnet', 'input': [{'type': 'message', 'role': 'user', 'content': 'go'}]})
            second = await r.claude_response({'model': 'claude-max-sonnet', 'previous_response_id': first['id'],
                'input': [{'type': 'function_call_output', 'call_id': 'c', 'output': 'x'}]})
            third = await r.claude_response({'model': 'claude-max-sonnet', 'previous_response_id': second['id'],
                'input': [{'type': 'message', 'role': 'user', 'content': 'more'}]})
            # A different model cannot resume the old session; a compaction item forces a full send.
            await r.claude_response({'model': 'claude-max-fable', 'previous_response_id': third['id'],
                'input': [{'type': 'message', 'role': 'user', 'content': 'switch'}]})
            await r.claude_response({'model': 'claude-max-sonnet', 'previous_response_id': third['id'],
                'input': [{'type': 'compaction', 'encrypted_content': adapter.CHECKPOINT_PREFIX + 'YQ=='}]})
        self.assertEqual(calls[0]['resume'], False)
        self.assertEqual(str(uuid.UUID(calls[0]['session'])), calls[0]['session'])  # dashed form for --session-id
        self.assertEqual(calls[1]['resume'], True)
        self.assertEqual(calls[1]['session'], calls[0]['session'])
        self.assertEqual(calls[1]['delta'], [{'type': 'function_call_output', 'call_id': 'c', 'output': 'x'}])
        self.assertEqual(calls[1]['items'], 3)
        self.assertEqual(calls[2]['resume'], True)
        self.assertEqual(calls[2]['session'], calls[0]['session'])
        self.assertEqual(calls[3]['resume'], False)
        self.assertNotEqual(calls[3]['session'], calls[0]['session'])
        self.assertEqual(calls[4]['resume'], False)

    async def test_grok_thread_continues_one_session(self):
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append((session, resume, delta))
            return dict(result(), id='resp_g%d' % len(calls), model='grok-max')
        r = self.app['router']
        with patch('router.run_grok', fake):
            first = await r.claude_response({'model': 'grok-max', 'input': 'go'})
            await r.claude_response({'model': 'grok-max', 'previous_response_id': first['id'],
                                     'input': [{'type': 'function_call_output', 'call_id': 'c', 'output': 'x'}]})
        self.assertEqual(calls[0][1], False)
        self.assertEqual(calls[1][1], True)
        self.assertEqual(calls[1][0], calls[0][0])
        self.assertEqual(calls[1][2], [{'type': 'function_call_output', 'call_id': 'c', 'output': 'x'}])

    async def test_bridged_session_survives_router_restart(self):
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append({'session': session, 'resume': resume, 'delta': delta, 'items': len(req['input'])})
            return dict(result(), id='resp_persist_%d' % len(calls), model='grok-max')
        r = self.app['router']
        with patch('router.run_grok', fake):
            first = await r.claude_response({'model': 'grok-max', 'input': 'go'})
        self.assertTrue((r.state / 'sessions.json').is_file())
        restarted = router.Router(r.catalog, '/unused/claude', r.state, grok=r.grok)
        restarted.codex_config = r.codex_config
        self.assertEqual(restarted.sessions.get(first['id'])[0], calls[0]['session'])
        self.assertEqual(len(restarted.history), 0)
        with patch('router.run_grok', fake):
            await restarted.claude_response({
                'model': 'grok-max', 'previous_response_id': first['id'],
                'input': [{'type': 'message', 'role': 'user', 'content': 'hi'}]})
        self.assertEqual(calls[1]['resume'], True)
        self.assertEqual(calls[1]['session'], calls[0]['session'])
        self.assertEqual(calls[1]['delta'], [{'type': 'message', 'role': 'user', 'content': 'hi'}])
        self.assertEqual(calls[1]['items'], 1)

    async def test_lost_cli_session_after_restart_asks_codex_to_resend(self):
        async def first_run(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            return dict(result(), id='resp_gone', model='grok-max')
        r = self.app['router']
        with patch('router.run_grok', first_run):
            first = await r.claude_response({'model': 'grok-max', 'input': 'go'})
        restarted = router.Router(r.catalog, '/unused/claude', r.state, grok=r.grok)
        restarted.codex_config = r.codex_config
        async def missing(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            if resume:
                raise router.BridgeError('Grok CLI failed (exit 1): No conversation found with session ID: ' + session)
            return dict(result(), id='resp_should_not', model='grok-max')
        with patch('router.run_grok', missing):
            with self.assertRaises(router.PreviousResponseLost) as raised:
                await restarted.claude_response({
                    'model': 'grok-max', 'previous_response_id': first['id'], 'input': 'hi'})
        self.assertEqual(raised.exception.payload()['code'], 'previous_response_not_found')

    async def test_lost_session_falls_back_to_full_send(self):
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append((session, resume, delta))
            if resume:
                raise BridgeError('Claude Code failed (exit 1): No conversation found with session ID: ' + session)
            return dict(result(), id='resp_%d' % len(calls))
        r = self.app['router']
        with patch('router.run_claude', fake):
            first = await r.claude_response({'model': 'claude-max-sonnet', 'input': 'go'})
            second = await r.claude_response({'model': 'claude-max-sonnet', 'previous_response_id': first['id'], 'input': 'next'})
        self.assertEqual([c[1] for c in calls], [False, True, False])
        self.assertNotEqual(calls[2][0], calls[0][0])
        self.assertIsNone(calls[2][2])
        self.assertEqual(second['id'], 'resp_3')

    async def test_websocket_ack_id_still_resumes(self):
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append((session, resume))
            return dict(result(), id='resp_inner_%d' % len(calls))
        with patch('router.run_claude', fake):
            ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer t'})
            await ws.send_json({'model': 'claude-max-sonnet', 'input': [{'type': 'message', 'role': 'user', 'content': 'go'}]})
            seen = None
            while True:
                event = await ws.receive_json()
                if event['type'] == 'response.completed':
                    seen = event['response']['id']
                    break
            await ws.send_json({'model': 'claude-max-sonnet', 'previous_response_id': seen,
                                'input': [{'type': 'function_call_output', 'call_id': 'c', 'output': 'x'}]})
            while (await ws.receive_json())['type'] != 'response.completed':
                pass
            await ws.close()
        self.assertEqual(calls[1][1], True, 'second turn must resume the first session')
        self.assertEqual(calls[1][0], calls[0][0])

    async def test_old_bridge_sessions_are_pruned(self):
        import os
        r = self.app['router']
        slug = str(r.claude_cwd.resolve()).replace('/', '-')
        folder = Path.home() / '.claude/projects' / slug
        folder.mkdir(parents=True, exist_ok=True)
        old = folder / 'test-old.jsonl'; new = folder / 'test-new.jsonl'
        old.write_text('{}'); new.write_text('{}')
        os.utime(old, (1, 1))
        import urllib.parse
        gfolder = Path.home() / '.grok/sessions' / urllib.parse.quote(str(r.grok_cwd.resolve()), safe='')
        gfolder.mkdir(parents=True, exist_ok=True)
        gold = gfolder / 'test-old-grok'; gnew = gfolder / 'test-new-grok'
        gold.mkdir(exist_ok=True); gnew.mkdir(exist_ok=True)
        (gold / 'x').write_text('{}'); (gnew / 'x').write_text('{}')
        os.utime(gold, (1, 1))
        r.sessions['resp_keep'] = (gold.name, 'grok-max')
        try:
            self.assertEqual(r.prune_sessions(), 1)
            self.assertFalse(old.exists()); self.assertTrue(new.exists())
            self.assertTrue(gold.exists(), 'mapped grok session must survive prune')
            self.assertTrue(gnew.exists())
        finally:
            import shutil
            for f in (old, new):
                if f.exists(): f.unlink()
            for d in (gold, gnew):
                shutil.rmtree(d, ignore_errors=True)
            for d in (folder, gfolder):
                try: d.rmdir()
                except OSError: pass

    async def test_reserve_requests_are_served_by_chosen_bridged_model(self):
        # Codex desktop forces model=gpt-reserve when the OpenAI advanced quota is
        # exhausted; the router serves those turns with the user's own configured model.
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append(req['model'])
            return dict(result(), id='resp_r%d' % len(calls), model=req['model'])
        (Path(self.temp.name) / 'config.toml').write_text('model = "claude-max-opus-48"\n')
        self.app['router'].codex_config = Path(self.temp.name) / 'config.toml'
        with patch('router.run_claude', fake):
            ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer t'})
            await ws.send_json({'model': 'gpt-reserve', 'input': [{'type': 'message', 'role': 'user', 'content': 'go'}]})
            events = []
            while True:
                e = await ws.receive_json(); events.append(e)
                if e['type'] == 'response.completed': break
            done = events[-1]['response']
            self.assertEqual(done['model'], 'gpt-reserve')  # what Codex asked for
            texts = [i['content'][0]['text'] for i in done['output'] if i['type'] == 'message']
            self.assertTrue(any('Opus 4.8' in t for t in texts), texts)
            await ws.send_json({'model': 'gpt-reserve', 'previous_response_id': done['id'],
                                'input': [{'type': 'message', 'role': 'user', 'content': 'more'}]})
            while True:
                e = await ws.receive_json()
                if e['type'] == 'response.completed': break
            texts = [i['content'][0]['text'] for i in e['response']['output'] if i['type'] == 'message']
            self.assertFalse(any('Opus 4.8' in t for t in texts), 'notice only on the first turn')
            await ws.close()
        self.assertEqual(calls, ['claude-max-opus-48', 'claude-max-opus-48'])
        # HTTP path too, and an unbridged/missing config falls back to the default.
        (Path(self.temp.name) / 'config.toml').write_text('model = "gpt-6-astra"\n')
        with patch('router.run_claude', fake):
            r = await self.client.post('/responses', json={'model': 'gpt-reserve', 'stream': False, 'input': 'hi'},
                                       headers={'Authorization': 'Bearer t'})
            self.assertEqual(r.status, 200)
        self.assertEqual(calls[-1], router.DEFAULT_RESERVE_TARGET)

    async def ws_turn(self, ws, payload):
        await ws.send_json(payload)
        while True:
            e = await ws.receive_json()
            if e['type'] in ('response.completed', 'error'):
                return e

    async def test_reserve_requests_are_served_by_chosen_bridged_model(self):
        # Codex desktop forces model=gpt-reserve when the OpenAI advanced quota is
        # exhausted; the router serves those turns with the user's chosen bridged model.
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append(req['model'])
            return dict(result(), id='resp_r%d' % len(calls), model=req['model'])
        r = self.app['router']
        self.assertEqual(r.reserve_target({}), router.DEFAULT_RESERVE_TARGET)  # Opus 4.8 out of the box
        with patch('router.run_claude', fake):
            ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer t'})
            done = (await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-A',
                'input': [{'type': 'message', 'role': 'user', 'content': 'go'}]}))['response']
            self.assertEqual(done['model'], 'gpt-reserve')  # what Codex asked for
            texts = [i['content'][0]['text'] for i in done['output'] if i['type'] == 'message']
            self.assertTrue(any('Opus 4.8' in t for t in texts), texts)
            e = await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-A', 'previous_response_id': done['id'],
                'input': [{'type': 'message', 'role': 'user', 'content': 'more'}]})
            texts = [i['content'][0]['text'] for i in e['response']['output'] if i['type'] == 'message']
            self.assertFalse(any('Opus 4.8' in t for t in texts), 'notice only on the first turn')
            await ws.close()
        self.assertEqual(calls, ['claude-max-opus-48', 'claude-max-opus-48'])
        # HTTP path honours the global choice saved by the selector page.
        r.set_reserve_model('claude-max-sonnet')
        with patch('router.run_claude', fake):
            resp = await self.client.post('/responses', json={'model': 'gpt-reserve', 'stream': False, 'input': 'hi'},
                                          headers={'Authorization': 'Bearer t'})
            self.assertEqual(resp.status, 200)
            self.assertEqual((await resp.json())['model'], 'gpt-reserve')
        self.assertEqual(calls[-1], 'claude-max-sonnet')
        # Grok as reserve target goes through the Grok runner.
        r.set_reserve_model('grok-max')
        gcalls = []
        async def gfake(req, *a, **k):
            gcalls.append(req['model']); return dict(result(), model='grok-max')
        with patch('router.run_grok', gfake):
            resp = await self.client.post('/responses', json={'model': 'gpt-reserve', 'stream': False, 'input': 'hi'},
                                          headers={'Authorization': 'Bearer t'})
            self.assertEqual(resp.status, 200)
        self.assertEqual(gcalls, ['grok-max'])
        # The choice survives a restart.
        again = router.Router(self.app['router'].catalog, '/unused/claude', self.app['router'].state)
        again.codex_config = self.app['router'].codex_config
        self.assertEqual(again.reserve_target({}), 'grok-max')
        # Codex's own default model (Settings -> default model) is honoured when nothing else is set.
        fresh = router.Router(self.app['router'].catalog, '/unused/claude', Path(self.temp.name) / 'other')
        fresh.codex_config = Path(self.temp.name) / 'codex-config.toml'
        fresh.codex_config.write_text('model = "claude-max-fable"\n')
        self.assertEqual(fresh.reserve_target({}), 'claude-max-fable')
        fresh.codex_config.write_text('model = "gpt-6-astra"\n')
        self.assertEqual(fresh.reserve_target({}), router.DEFAULT_RESERVE_TARGET)

    async def test_in_chat_model_selector(self):
        calls = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            calls.append(req['model'])
            return dict(result(), id='resp_s%d' % len(calls), model=req['model'])
        with patch('router.run_claude', fake):
            ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer t'})
            # Switch command is answered by the router itself, without a model call.
            e = await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-B',
                'input': [{'type': 'message', 'role': 'developer', 'content': 'env'},
                          {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'model: sonnet'}]}]})
            self.assertEqual(e['type'], 'response.completed')
            text = e['response']['output'][0]['content'][0]['text']
            self.assertIn('Sonnet 5', text)
            self.assertEqual(calls, [])
            e = await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-B', 'previous_response_id': e['response']['id'],
                'input': [{'type': 'message', 'role': 'user', 'content': 'now work'}]})
            self.assertEqual(calls, ['claude-max-sonnet'])
            # Another thread keeps the global default.
            await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-C', 'input': 'hi'})
            self.assertEqual(calls[-1], router.DEFAULT_RESERVE_TARGET)
            # Unknown name: helpful reply, no model call.
            e = await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-D', 'input': 'model: gemini'})
            self.assertIn('fable', e['response']['output'][0]['content'][0]['text'])
            self.assertEqual(len(calls), 2)
            # Ordinary prose that merely mentions a model is not intercepted.
            await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-E', 'input': 'which model: sonnet or opus is better?'})
            self.assertEqual(len(calls), 3)
            await ws.close()

    async def test_model_command_keeps_thread_history_and_tools(self):
        seen = []
        async def fake(req, executable, cwd, timeout=240, session=None, resume=False, delta=None):
            seen.append({'kinds': [x.get('type') for x in req['input']], 'session': session, 'resume': resume, 'model': req['model']})
            return dict(result(), id='resp_h%d' % len(seen), model=req['model'])
        tools = {'type': 'additional_tools', 'tools': [{'type': 'namespace', 'name': 'functions',
                 'tools': [{'type': 'custom', 'name': 'exec'}]}]}
        with patch('router.run_claude', fake):
            ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer t'})
            first = (await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-H',
                'input': [tools, {'type': 'message', 'role': 'user', 'content': 'hello'}]}))['response']
            switched = (await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-H',
                'previous_response_id': first['id'], 'input': 'model: opus-48'}))['response']
            # Same model as before: the Claude session carries on; the tools are still there.
            worked = (await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-H',
                'previous_response_id': switched['id'], 'input': 'run ls'}))['response']
            self.assertIn('additional_tools', seen[1]['kinds'])
            self.assertTrue(seen[1]['resume'])
            self.assertEqual(seen[1]['session'], seen[0]['session'])
            # Switching to a different model: full history (with tools) goes to the new model.
            switched = (await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-H',
                'previous_response_id': worked['id'], 'input': 'model: sonnet'}))['response']
            await self.ws_turn(ws, {'model': 'gpt-reserve', 'prompt_cache_key': 'thread-H',
                'previous_response_id': switched['id'], 'input': 'run ls again'})
            self.assertEqual(seen[2]['model'], 'claude-max-sonnet')
            self.assertIn('additional_tools', seen[2]['kinds'])
            self.assertFalse(seen[2]['resume'])
            await ws.close()

    async def test_selector_page(self):
        # The old selector page is now the dashboard; '/' redirects there.
        page = await self.client.get('/', allow_redirects=False)
        self.assertEqual(page.status, 302)
        self.assertEqual(page.headers['Location'], '/dashboard/')
        base = str(self.client.make_url('')).rstrip('/')
        # Cross-site form posts are refused; same-origin ones apply.
        r = await self.client.post('/select', data={'model': 'claude-max-fable'}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status, 403)
        r = await self.client.post('/select', data={'model': 'claude-max-fable'}, headers={'Origin': base}, allow_redirects=False)
        self.assertEqual(r.status, 303)
        self.assertEqual(self.app['router'].reserve_target({}), 'claude-max-fable')
        r = await self.client.post('/select', data={'model': 'gpt-6-astra'}, headers={'Origin': base}, allow_redirects=False)
        self.assertEqual(r.status, 400)
        health = await (await self.client.get('/health')).json()
        self.assertEqual(health['reserve_model'], 'claude-max-fable')

    async def test_lost_previous_response_uses_openai_wire_shape(self):
        r = await self.client.post('/responses', json={'model': 'claude-max-sonnet', 'stream': False,
                'previous_response_id': 'resp_gone', 'input': 'hi'}, headers={'Authorization': 'Bearer t'})
        self.assertEqual(r.status, 404)
        error = (await r.json())['error']
        self.assertEqual(error['code'], 'previous_response_not_found')
        self.assertEqual(error['param'], 'previous_response_id')
        self.assertIn('resp_gone', error['message'])
        ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer t'})
        await ws.send_json({'model': 'claude-max-sonnet', 'previous_response_id': 'resp_gone', 'input': 'hi'})
        event = await ws.receive_json()
        self.assertEqual(event['type'], 'error')
        self.assertEqual(event['error']['code'], 'previous_response_not_found')
        await ws.close()
        self.assertEqual(self.app['router'].counts['errors'], 0)

    async def test_provider_dispatch_and_stats_recording(self):
        import types
        recorded = []
        class FakeStats:
            async def record_request(self, **f): recorded.append(f)
            async def record_event(self, *a): pass
            async def record_usage(self, *a): recorded.append({'usage': a})
            async def close(self): pass
        r = self.app['router']
        r.stats = FakeStats()
        self.assertEqual(router.provider_of('claude-max-fable'), 'claude')
        self.assertEqual(router.provider_of('grok-max'), 'grok')
        self.assertEqual(router.provider_of('ollama-llama3.2-3b'), 'ollama')
        self.assertEqual(router.provider_of('gpt-6-astra'), 'openai')
        async def fake_claude(req, *a, **k):
            out = dict(result(), id='resp_c1')
            out['output'].append({'type': 'function_call', 'id': 'fc', 'call_id': 'c', 'name': 'exec', 'arguments': '{}', 'status': 'completed'})
            out['usage'] = {'input_tokens': 1000, 'output_tokens': 20, 'total_tokens': 1020, 'input_tokens_details': {'cached_tokens': 900}}
            out['rate_limit_headers'] = {'x-codex-primary-used-percent': '42', 'x-codex-primary-reset-at': '1789545600'}
            return out
        with patch('router.run_claude', fake_claude):
            await r.claude_response({'model': 'claude-max-sonnet', 'prompt_cache_key': 'T1', 'input': 'go'})
        turn = recorded[0]
        self.assertEqual((turn['provider'], turn['kind'], turn['status'], turn['thread']), ('claude', 'turn', 'ok', 'T1'))
        self.assertEqual((turn['input_tokens'], turn['cached_tokens'], turn['output_tokens'], turn['tool_calls'], turn['resumed']), (100, 900, 20, 1, 0))
        self.assertEqual(recorded[1]['usage'], ('claude', 'five_hour', 0.42, 1789545600))
        # Ollama models register into MODELS and dispatch through the Ollama runner.
        fake_provider = types.SimpleNamespace(
            discover=None, slug_for=lambda n: 'ollama-' + n.replace(':', '-'),
            catalog_entry=lambda t, m: dict(t, slug='ollama-' + m['name'].replace(':', '-'), display_name=m['name']),
            run_ollama=None)
        async def discover(session, base_url): return [{'name': 'llama3.2:3b', 'context_length': 8192, 'vision': False}]
        calls = []
        async def run_ollama(req, session, base_url, name, timeout, sid, resume, delta):
            calls.append((req['model'], base_url, name)); return dict(result(), id='resp_o1', model=req['model'])
        fake_provider.discover = discover; fake_provider.run_ollama = run_ollama
        base = Path(self.temp.name) / 'models.base.json'
        base.write_text(json.dumps({'models': [{'slug': 'claude-max-sonnet', 'display_name': 'Claude Sonnet 5 · Max', 'input_modalities': ['text', 'image']}]}))
        r.base_catalog = base
        with patch('router.ollama_provider', fake_provider):
            info = await r.refresh_ollama()
            self.assertEqual(info['models'][0]['slug'], 'ollama-llama3.2-3b')
            self.assertIn('ollama-llama3.2-3b', adapter.MODELS)
            generated = json.loads(r.catalog.read_text())['models']
            self.assertEqual([m['slug'] for m in generated], ['claude-max-sonnet', 'ollama-llama3.2-3b'])
            await r.claude_response({'model': 'ollama-llama3.2-3b', 'input': 'hi'})
            self.assertEqual(calls, [('ollama-llama3.2-3b', 'http://127.0.0.1:11434', 'llama3.2:3b')])
            self.assertEqual(recorded[-1]['provider'], 'ollama')
            self.assertEqual(r.counts['ollama'], 1)
            # A second refresh with nothing found unregisters the slug.
            async def none(session, base_url): return []
            fake_provider.discover = none
            await r.refresh_ollama()
            self.assertNotIn('ollama-llama3.2-3b', adapter.MODELS)
            self.assertFalse(r.ollama_online)
        # Errors are recorded too.
        async def boom(req, *a, **k): raise BridgeError('nope')
        with patch('router.run_claude', boom):
            with self.assertRaises(BridgeError):
                await r.claude_response({'model': 'claude-max-sonnet', 'input': 'go'})
        self.assertEqual((recorded[-1]['status'], recorded[-1]['error']), ('error', 'nope'))

    async def test_settings_persist(self):
        r = self.app['router']
        self.assertEqual(r.settings['ollama']['base_url'], 'http://127.0.0.1:11434')
        r.settings['ollama']['enabled'] = False
        r.save_settings()
        again = router.Router(r.catalog, '/unused/claude', r.state)
        self.assertFalse(again.settings['ollama']['enabled'])
        self.assertEqual(again.settings['ollama']['base_url'], 'http://127.0.0.1:11434')

    async def test_history_sized_for_subagent_fanout(self):
        r = self.app['router']
        self.assertGreaterEqual(r.semaphore._value, 4)
        for i in range(300):
            r.remember({'input': []}, {'id': 'resp_%d' % i, 'output': []})
        self.assertGreaterEqual(len(r.history), 256)

    async def test_quota_falls_back_to_claude(self):
        calls, seen = await self.run_upstream_case('usage_limit_reached')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['model'], 'claude-max-opus')
        self.assertEqual(seen[-1]['type'], 'response.completed')
        self.assertEqual(self.app['router'].counts['fallback'], 1)

    async def test_transient_rate_limit_no_fallback(self):
        calls, seen = await self.run_upstream_case('rate_limit_exceeded')
        self.assertEqual(calls, [])
        self.assertEqual(seen[-1]['type'], 'error')

    async def test_partial_output_never_replayed(self):
        calls, seen = await self.run_upstream_case('usage_limit_reached', partial=True)
        self.assertEqual(calls, [])
        self.assertEqual(seen[-1]['type'], 'error')

    async def test_disconnect_cancels_claude(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def fake(*args):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        with patch('router.run_claude', fake):
            ws = await self.client.ws_connect('/responses', headers={'Authorization': 'Bearer test'})
            await ws.send_json({'model': 'claude-max-sonnet', 'input': []})
            await asyncio.wait_for(started.wait(), 3)
            await ws.close()
            await asyncio.wait_for(cancelled.wait(), 3)

if __name__ == '__main__':
    unittest.main()
