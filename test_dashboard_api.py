"""Tests for stats.py and dashboard_api.py (SSOT §5, box B).

A ``types.SimpleNamespace`` stands in for the Router (only the attributes the
dashboard consumes), while ``Stats`` is the real SQLite store on a temp path.
Run: ``.venv/bin/python -m unittest test_dashboard_api -v``.
"""
import json
import sqlite3
import stat
import tempfile
import time
import types
import unittest
from pathlib import Path
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import adapter
import dashboard_api
from stats import Stats, StatsClosed, percentile

CATALOG = {'models': [
    {'slug': 'claude-max-fable', 'display_name': 'Claude Fable 5.1 · Max', 'context_window': 200000,
     'input_modalities': ['text', 'image'], 'supports_search_tool': True},
    {'slug': 'grok-max', 'display_name': 'Grok 4.6 · grok.com', 'context_window': 200000,
     'input_modalities': ['text', 'image'], 'supports_search_tool': True},
    {'slug': 'ollama-llama3.2-3b', 'display_name': 'Llama3.2 3b · Ollama', 'context_window': 8192,
     'input_modalities': ['text'], 'supports_search_tool': False},
    {'slug': 'gpt-6-astra', 'display_name': 'GPT-6-Astra', 'context_window': 400000,
     'input_modalities': ['text', 'image'], 'supports_search_tool': True},
]}
LOCAL = {'Origin': 'http://127.0.0.1:18740'}
EVIL = {'Origin': 'https://evil.example'}


def fake_router(path):
    """Router stand-in exposing exactly the attributes SSOT §5 promises."""
    state = path / 'state'
    state.mkdir(mode=0o700)
    catalog = path / 'catalog.json'
    catalog.write_text(json.dumps(CATALOG))
    claude = path / 'claude'
    claude.write_text('#!/bin/sh\n')
    claude.chmod(0o755)
    r = types.SimpleNamespace()
    r.state = state
    r.token_path = state / 'dashboard-token'
    r.catalog = catalog
    r.claude = str(claude)
    r.grok = str(path / 'missing-grok')
    r.reserve = {'model': 'claude-max-fable', 'threads': {'thread-1': 'grok-max'}}
    r.settings = {'ollama': {'enabled': True, 'base_url': 'http://127.0.0.1:11434'}}
    r.counts = {'claude': 3, 'grok': 1, 'openai': 2, 'fallback': 0, 'errors': 1}
    r.sessions = {}
    r.ollama_online = True
    r.ollama_models = {'ollama-llama3.2-3b': 'llama3.2:3b'}
    r.saved_reserve = 0
    r.saved_settings = 0
    r.refreshed = 0

    def reserve_target(data):
        thread = data.get('prompt_cache_key')
        return (r.reserve['threads'].get(thread) if thread else None) or r.reserve['model'] or 'claude-max-opus-48'

    def set_reserve_model(model, thread=None):
        if model not in adapter.MODELS:
            raise ValueError('not a bridged model: %s' % model)
        if thread:
            r.reserve['threads'][thread] = model
        else:
            r.reserve['model'] = model
        r.saved_reserve += 1

    def save_reserve():
        r.saved_reserve += 1

    def save_settings():
        r.saved_settings += 1

    async def refresh_ollama():
        r.refreshed += 1
        return {'models': [{'name': 'llama3.2:3b', 'slug': 'ollama-llama3.2-3b'}], 'catalog': state / 'models.json'}

    r.reserve_target = reserve_target
    r.set_reserve_model = set_reserve_model
    r.save_reserve = save_reserve
    r.save_settings = save_settings
    r.refresh_ollama = refresh_ollama
    r.display_name = lambda slug: slug
    return r


class DashboardCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.router = fake_router(path)
        self.stats = Stats(path / 'state' / 'stats.sqlite')
        await self.stats.open()
        self.static = path / 'dashboard'
        self.static.mkdir()
        (self.static / 'index.html').write_text('<!doctype html><title>DCR test</title>')
        (self.static / 'app.js').write_text('console.log("dcr");')
        (self.static / 'styles.css').write_text('body{margin:0}')
        (self.static / 'secret.txt').write_text('not served')
        root = web.Application()
        root.add_subapp('/api/v1', dashboard_api.build(self.router, self.stats, self.static))
        root.add_subapp('/dashboard', dashboard_api.static_app(self.static))
        # No cookie jar: every test sends cookies explicitly so rotation is observable.
        self.client = TestClient(TestServer(root), cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()
        self.token = self.router.token_path.read_text().strip()
        self.bearer = {'Authorization': 'Bearer ' + self.token}

    async def asyncTearDown(self):
        await self.client.close()
        await self.stats.close()
        self.temp.cleanup()

    async def login(self):
        """Log in and return the raw cookie value."""
        r = await self.client.post('/api/v1/auth/login', json={'token': self.token})
        self.assertEqual(r.status, 200)
        return r.cookies['dcr_session'].value

    async def seed(self):
        """30 requests over the last 30 minutes across four providers, 5 older ones, 3 usage samples."""
        now = time.time()
        providers = ('claude', 'claude', 'grok', 'ollama', 'openai')
        for i in range(30):
            provider = providers[i % 5]
            await self.stats.record_request(
                ts=now - 60 * (i + 1), thread='thread-%d' % (i % 4), model_requested='gpt-reserve',
                model_served={'claude': 'claude-max-fable', 'grok': 'grok-max', 'ollama': 'ollama-llama3.2-3b',
                              'openai': 'gpt-6-astra'}[provider],
                provider=provider, kind='turn', status='error' if i % 7 == 3 else 'ok',
                error='boom %d' % i if i % 7 == 3 else None, latency_ms=100 + i * 10,
                input_tokens=1000, cached_tokens=400 if provider == 'claude' else 0, output_tokens=50,
                resumed=i % 2 == 1, tool_calls=i % 3, path='/responses')
        for i in range(5):
            await self.stats.record_request(ts=now - 36 * 3600 - i, thread='old', provider='claude',
                                            model_served='claude-max-opus', kind='turn', latency_ms=500)
        await self.stats.record_usage('claude', 'five_hour', 0.44, 1789525800)
        await self.stats.record_usage('claude', 'seven_day', 0.07, 1790082000)
        await self.stats.record_usage('claude', 'five_hour', 0.51, 1789525900)

    # ---- auth ----
    def test_token_file_created_with_0600(self):
        self.assertTrue(self.router.token_path.is_file())
        self.assertEqual(stat.S_IMODE(self.router.token_path.stat().st_mode), 0o600)
        self.assertEqual(len(self.token), 64)
        int(self.token, 16)
        # An existing token is reused, not overwritten.
        self.assertEqual(dashboard_api.Auth(self.router.token_path).token, self.token)

    async def test_requires_auth(self):
        for method, path in [('GET', '/api/v1/stats/summary'), ('GET', '/api/v1/models'), ('GET', '/api/v1/settings'),
                             ('PUT', '/api/v1/settings'), ('POST', '/api/v1/auth/rotate'), ('POST', '/api/v1/auth/logout'),
                             ('POST', '/api/v1/ollama/refresh'), ('DELETE', '/api/v1/settings/reserve/threads/x'),
                             ('GET', '/api/v1/nope')]:
            r = await self.client.request(method, path)
            self.assertEqual(r.status, 401, path)
            self.assertEqual((await r.json())['error']['code'], 'unauthorized')
        r = await self.client.get('/api/v1/models', headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(r.status, 401)

    async def test_bearer_ok_even_cross_origin(self):
        r = await self.client.get('/api/v1/settings', headers=dict(self.bearer, **EVIL))
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())['reserve']['model'], 'claude-max-fable')

    async def test_login_sets_cookie_and_cookie_works(self):
        r = await self.client.post('/api/v1/auth/login', json={'token': self.token})
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {'ok': True})
        cookie = r.cookies['dcr_session']
        self.assertEqual(cookie.value, dashboard_api.Auth(self.router.token_path).cookie_value())
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Strict')
        self.assertEqual(cookie['path'], '/')
        self.assertEqual(int(cookie['max-age']), 30 * 86400)
        headers = {'Cookie': 'dcr_session=' + cookie.value}
        r = await self.client.get('/api/v1/settings', headers=headers)
        self.assertEqual(r.status, 200)
        r = await self.client.get('/api/v1/auth/status', headers=headers)
        self.assertEqual(await r.json(), {'authenticated': True, 'method': 'cookie'})
        r = await self.client.get('/api/v1/settings', headers={'Cookie': 'dcr_session=' + 'f' * 64})
        self.assertEqual(r.status, 401)

    async def test_auth_status_public(self):
        r = await self.client.get('/api/v1/auth/status')
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {'authenticated': False, 'method': None})
        r = await self.client.get('/api/v1/auth/status', headers=self.bearer)
        self.assertEqual(await r.json(), {'authenticated': True, 'method': 'bearer'})

    async def test_wrong_token_401(self):
        r = await self.client.post('/api/v1/auth/login', json={'token': 'nope'})
        self.assertEqual(r.status, 401)
        self.assertNotIn('dcr_session', r.cookies)
        r = await self.client.post('/api/v1/auth/login', data=b'not json', headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status, 400)
        self.assertEqual((await r.json())['error']['code'], 'invalid_json')

    async def test_lockout_after_10_failures(self):
        for _ in range(10):
            r = await self.client.post('/api/v1/auth/login', json={'token': 'bad'})
            self.assertEqual(r.status, 401)
        r = await self.client.post('/api/v1/auth/login', json={'token': self.token})
        self.assertEqual(r.status, 429)
        self.assertEqual((await r.json())['error']['code'], 'locked_out')
        self.assertTrue(int(r.headers['Retry-After']) > 0)
        # Bearer access is unaffected by the login lockout.
        r = await self.client.get('/api/v1/settings', headers=self.bearer)
        self.assertEqual(r.status, 200)

    async def test_cookie_same_origin_rules(self):
        cookie = {'Cookie': 'dcr_session=' + await self.login()}
        r = await self.client.post('/api/v1/auth/logout', headers=dict(cookie, **EVIL))
        self.assertEqual(r.status, 403)
        self.assertEqual((await r.json())['error']['code'], 'forbidden')
        r = await self.client.get('/api/v1/settings', headers=dict(cookie, **EVIL))
        self.assertEqual(r.status, 403)
        r = await self.client.get('/api/v1/settings', headers=dict(cookie, Referer='http://attacker.test/x'))
        self.assertEqual(r.status, 403)
        # Mutating without any Origin/Referer is refused; a GET without them is fine.
        r = await self.client.post('/api/v1/auth/logout', headers=cookie)
        self.assertEqual(r.status, 403)
        r = await self.client.get('/api/v1/settings', headers=cookie)
        self.assertEqual(r.status, 200)
        for origin in (LOCAL, {'Origin': 'http://localhost:18740'}, {'Referer': 'http://127.0.0.1:18740/dashboard/'}):
            r = await self.client.put('/api/v1/settings', json={'ollama': {'enabled': True}}, headers=dict(cookie, **origin))
            self.assertEqual(r.status, 200, origin)

    async def test_logout_clears_cookie(self):
        cookie = {'Cookie': 'dcr_session=' + await self.login()}
        r = await self.client.post('/api/v1/auth/logout', headers=dict(cookie, **LOCAL))
        self.assertEqual(r.status, 200)
        self.assertEqual(r.cookies['dcr_session'].value, '')
        self.assertIn('max-age=0', r.headers['Set-Cookie'].lower())

    async def test_rotate_invalidates_old_cookie_and_token(self):
        old_cookie = await self.login()
        r = await self.client.post('/api/v1/auth/rotate', headers=self.bearer)
        self.assertEqual(r.status, 200)
        new_token = (await r.json())['token']
        self.assertNotEqual(new_token, self.token)
        self.assertEqual(self.router.token_path.read_text().strip(), new_token)
        self.assertEqual(stat.S_IMODE(self.router.token_path.stat().st_mode), 0o600)
        self.assertNotIn('dcr_session', r.cookies)  # bearer callers get no cookie
        r = await self.client.get('/api/v1/settings', headers={'Cookie': 'dcr_session=' + old_cookie})
        self.assertEqual(r.status, 401)
        r = await self.client.get('/api/v1/settings', headers=self.bearer)
        self.assertEqual(r.status, 401)
        r = await self.client.get('/api/v1/settings', headers={'Authorization': 'Bearer ' + new_token})
        self.assertEqual(r.status, 200)
        # A cookie-authenticated rotate hands back a cookie for the new token.
        r = await self.client.post('/api/v1/auth/login', json={'token': new_token})
        cookie = r.cookies['dcr_session'].value
        r = await self.client.post('/api/v1/auth/rotate', headers=dict({'Cookie': 'dcr_session=' + cookie}, **LOCAL))
        self.assertEqual(r.status, 200)
        newest = r.cookies['dcr_session'].value
        self.assertNotEqual(newest, cookie)
        r = await self.client.get('/api/v1/settings', headers={'Cookie': 'dcr_session=' + newest})
        self.assertEqual(r.status, 200)
        events = [dict(x) for x in await self.stats._run(lambda: self.stats.conn.execute('SELECT * FROM events').fetchall())]
        self.assertEqual([e['kind'] for e in events], ['token_rotated', 'token_rotated'])

    # ---- endpoints ----
    async def test_health(self):
        r = await self.client.get('/api/v1/health')
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['version'], dashboard_api.read_version())
        self.assertGreaterEqual(body['uptime_s'], 0)
        self.assertEqual(body['counts'], self.router.counts)
        self.assertEqual(body['reserve_model'], 'claude-max-fable')
        self.assertEqual(body['ollama'], {'enabled': True, 'base_url': 'http://127.0.0.1:11434', 'online': True, 'models': 1})

    async def test_summary_math(self):
        await self.seed()
        r = await self.client.get('/api/v1/stats/summary', headers=self.bearer)
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body['range'], '24h')
        totals = body['totals']
        self.assertEqual(totals['requests'], 30)
        self.assertEqual(totals['errors'], 4)
        self.assertEqual(totals['error_ratio'], round(4 / 30, 4))
        self.assertEqual(totals['input_tokens'], 30000)
        self.assertEqual(totals['cached_tokens'], 4800)
        self.assertEqual(totals['fresh_tokens'], 25200)
        self.assertEqual(totals['output_tokens'], 1500)
        self.assertEqual(totals['cache_hit_ratio'], 0.16)
        self.assertEqual(totals['resumed'], 15)
        self.assertEqual(totals['resumed_ratio'], 0.5)
        self.assertEqual(totals['tool_calls'], 30)
        self.assertEqual(totals['active_threads'], 4)
        self.assertEqual(totals['latency'], {'avg': 245.0, 'p50': 240, 'p95': 380})
        self.assertGreaterEqual(totals['latency']['p95'], totals['latency']['p50'])
        providers = body['providers']
        self.assertEqual({k: v['requests'] for k, v in providers.items()},
                         {'claude': 12, 'grok': 6, 'ollama': 6, 'openai': 6})
        self.assertEqual(providers['claude']['cache_hit_ratio'], 0.4)
        self.assertEqual(providers['grok']['cache_hit_ratio'], 0.0)
        self.assertEqual({k: v['errors'] for k, v in providers.items()}, {'claude': 1, 'grok': 1, 'ollama': 1, 'openai': 1})
        self.assertEqual(body['models']['claude-max-fable']['requests'], 12)
        self.assertEqual(body['models']['grok-max']['latency']['p95'], 370)
        previous = body['previous']
        self.assertEqual(previous['totals']['requests'], 5)
        self.assertEqual(previous['totals']['latency']['p50'], 500)
        self.assertEqual(previous['providers']['claude']['requests'], 5)
        self.assertAlmostEqual(previous['until'], body['since'])
        self.assertAlmostEqual(body['since'] - previous['since'], 86400, places=3)

    async def test_summary_ranges(self):
        await self.seed()
        for name, expected, expected_previous in (('1h', 30, 0), ('24h', 30, 5), ('7d', 35, 0), ('30d', 35, 0)):
            r = await self.client.get('/api/v1/stats/summary?range=' + name, headers=self.bearer)
            body = await r.json()
            self.assertEqual(body['totals']['requests'], expected, name)
            self.assertEqual(body['previous']['totals']['requests'], expected_previous, name)
        r = await self.client.get('/api/v1/stats/summary?range=2h', headers=self.bearer)
        self.assertEqual(r.status, 400)
        self.assertEqual((await r.json())['error']['code'], 'invalid_range')
        # Empty store: zeros and null percentiles, never a division error.
        await self.stats.prune(keep_days=0)
        r = await self.client.get('/api/v1/stats/summary', headers=self.bearer)
        totals = (await r.json())['totals']
        self.assertEqual(totals['requests'], 0)
        self.assertEqual(totals['latency'], {'avg': None, 'p50': None, 'p95': None})
        self.assertEqual(totals['cache_hit_ratio'], 0.0)

    async def test_timeseries_buckets(self):
        await self.seed()
        r = await self.client.get('/api/v1/stats/timeseries', headers=self.bearer)
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body['range'], '24h')
        self.assertEqual(body['bucket_s'], 900)
        self.assertEqual(len(body['buckets']), 96)
        self.assertEqual(body['buckets'][-1] - body['buckets'][0], 95 * 900)
        self.assertEqual(body['buckets'][0] % 900, 0)
        self.assertLessEqual(body['buckets'][-1], body['until'])
        self.assertEqual(sum(row['requests'] for row in body['rows']), 30)
        self.assertEqual(sum(row['errors'] for row in body['rows']), 4)
        self.assertEqual(sum(row['cached_tokens'] for row in body['rows']), 4800)
        self.assertEqual(body['providers'], ['claude', 'grok', 'ollama', 'openai'])
        buckets = set(body['buckets'])
        self.assertTrue(all(row['t'] in buckets for row in body['rows']))
        self.assertEqual(body['rows'], sorted(body['rows'], key=lambda x: (x['t'], x['provider'])))
        for query, count in (('range=1h', 60), ('range=7d', 168), ('range=30d', 120), ('range=1h&bucket=300', 12)):
            r = await self.client.get('/api/v1/stats/timeseries?' + query, headers=self.bearer)
            self.assertEqual(len((await r.json())['buckets']), count, query)
        for bad in ('range=1h&bucket=5', 'range=1h&bucket=abc', 'range=9d'):
            r = await self.client.get('/api/v1/stats/timeseries?' + bad, headers=self.bearer)
            self.assertEqual(r.status, 400, bad)

    async def test_recent_pagination_and_filters(self):
        await self.seed()
        r = await self.client.get('/api/v1/requests?limit=10', headers=self.bearer)
        self.assertEqual(r.status, 200)
        page = await r.json()
        self.assertEqual(len(page['requests']), 10)
        ids = [row['id'] for row in page['requests']]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(page['next_before'], ids[-1])
        self.assertEqual(page['requests'][0]['provider'], 'claude')  # the last inserted row is an old claude one
        r = await self.client.get('/api/v1/requests?limit=10&before=%d' % page['next_before'], headers=self.bearer)
        older = await r.json()
        self.assertTrue(all(row['id'] < page['next_before'] for row in older['requests']))
        self.assertEqual(len(older['requests']), 10)
        r = await self.client.get('/api/v1/requests?limit=500', headers=self.bearer)
        self.assertEqual(len((await r.json())['requests']), 35)
        self.assertIsNone((await r.json())['next_before'])
        r = await self.client.get('/api/v1/requests?provider=grok', headers=self.bearer)
        rows = (await r.json())['requests']
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row['provider'] == 'grok' for row in rows))
        r = await self.client.get('/api/v1/requests?status=error', headers=self.bearer)
        rows = (await r.json())['requests']
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['status'] == 'error' and row['error'].startswith('boom') for row in rows))
        r = await self.client.get('/api/v1/requests', headers=self.bearer)
        self.assertEqual(len((await r.json())['requests']), 35)
        for bad in ('limit=0', 'limit=abc', 'limit=501', 'before=x', 'status=meh', 'provider=Bad Provider'):
            r = await self.client.get('/api/v1/requests?' + bad, headers=self.bearer)
            self.assertEqual(r.status, 400, bad)

    async def test_usage_latest_per_window(self):
        await self.seed()
        r = await self.client.get('/api/v1/usage', headers=self.bearer)
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body['counts'], self.router.counts)
        windows = {(w['provider'], w['window']): w for w in body['windows']}
        self.assertEqual(set(windows), {('claude', 'five_hour'), ('claude', 'seven_day')})
        self.assertEqual(windows[('claude', 'five_hour')]['utilization'], 0.51)
        self.assertEqual(windows[('claude', 'five_hour')]['resets_at'], 1789525900)
        self.assertEqual(windows[('claude', 'seven_day')]['utilization'], 0.07)

    async def test_models_mapping(self):
        r = await self.client.get('/api/v1/models', headers=self.bearer)
        self.assertEqual(r.status, 200)
        models = {m['slug']: m for m in await r.json()}
        self.assertEqual({k: v['provider'] for k, v in models.items()},
                         {'claude-max-fable': 'claude', 'grok-max': 'grok', 'ollama-llama3.2-3b': 'ollama', 'gpt-6-astra': 'openai'})
        self.assertEqual({k: v['online'] for k, v in models.items()},
                         {'claude-max-fable': True, 'grok-max': False, 'ollama-llama3.2-3b': True, 'gpt-6-astra': True})
        self.assertEqual([k for k, v in models.items() if v['is_reserve_default']], ['claude-max-fable'])
        self.assertEqual(models['ollama-llama3.2-3b']['input_modalities'], ['text'])
        self.assertFalse(models['ollama-llama3.2-3b']['supports_search_tool'])
        self.assertEqual(models['grok-max']['context_window'], 200000)
        self.assertEqual(models['claude-max-fable']['display_name'], 'Claude Fable 5.1 · Max')
        self.router.catalog.write_text('{broken')
        r = await self.client.get('/api/v1/models', headers=self.bearer)
        self.assertEqual(r.status, 500)
        self.assertEqual((await r.json())['error']['code'], 'catalog_error')

    async def test_settings_get_and_put(self):
        r = await self.client.get('/api/v1/settings', headers=self.bearer)
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {
            'reserve': {'model': 'claude-max-fable', 'threads': {'thread-1': 'grok-max'}, 'effective': 'claude-max-fable'},
            'ollama': {'enabled': True, 'base_url': 'http://127.0.0.1:11434'},
            'catalog_path': str(self.router.catalog)})
        r = await self.client.put('/api/v1/settings', json={'reserve': {'model': 'grok-max'}}, headers=self.bearer)
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body['reserve']['model'], 'grok-max')
        self.assertEqual(self.router.reserve['model'], 'grok-max')
        self.assertEqual(self.router.saved_reserve, 1)
        self.assertEqual(self.router.saved_settings, 0)
        r = await self.client.put('/api/v1/settings', json={'ollama': {'enabled': False, 'base_url': 'http://localhost:11435/'}},
                                  headers=self.bearer)
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())['ollama'], {'enabled': False, 'base_url': 'http://localhost:11435'})
        self.assertEqual(self.router.settings['ollama'], {'enabled': False, 'base_url': 'http://localhost:11435'})
        self.assertEqual(self.router.saved_settings, 1)
        r = await self.client.put('/api/v1/settings', json={'reserve': {'threads': {'thread-2': 'claude-max-sonnet'}}},
                                  headers=self.bearer)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.router.reserve['threads'], {'thread-1': 'grok-max', 'thread-2': 'claude-max-sonnet'})
        events = [dict(x) for x in await self.stats._run(lambda: self.stats.conn.execute('SELECT * FROM events').fetchall())]
        self.assertEqual([e['kind'] for e in events], ['reserve_changed', 'settings_changed', 'reserve_changed'])
        self.assertEqual(json.loads(events[1]['detail']), {'ollama': {'enabled': False, 'base_url': 'http://localhost:11435'}})

    async def test_settings_put_invalid(self):
        cases = [
            ({'reserve': {'model': 'gpt-6-astra'}}, 'reserve.model'),
            ({'reserve': {'model': 5}}, 'reserve.model'),
            ({'reserve': 'grok-max'}, 'reserve must'),
            ({'reserve': {'threads': {'t': 'nope'}}}, 'reserve.threads'),
            ({'reserve': {'threads': []}}, 'reserve.threads'),
            ({'reserve': {'bogus': 1}}, 'Unknown reserve'),
            ({'ollama': {'base_url': 'http://example.com:11434'}}, 'ollama.base_url'),
            ({'ollama': {'base_url': 'http://192.168.1.5:11434'}}, 'ollama.base_url'),
            ({'ollama': {'base_url': 'ftp://127.0.0.1:11434'}}, 'ollama.base_url'),
            ({'ollama': {'base_url': 'http://user:pw@127.0.0.1:11434'}}, 'ollama.base_url'),
            ({'ollama': {'base_url': 'http://127.0.0.1:99999'}}, 'ollama.base_url'),
            ({'ollama': {'base_url': ''}}, 'ollama.base_url'),
            ({'ollama': {'enabled': 'yes'}}, 'ollama.enabled'),
            ({'ollama': {'enabled': 1}}, 'ollama.enabled'),
            ({'ollama': {'extra': True}}, 'Unknown ollama'),
            ({'theme': 'dark'}, 'Unknown field'),
            ({}, 'No settings'),
        ]
        for body, fragment in cases:
            r = await self.client.put('/api/v1/settings', json=body, headers=self.bearer)
            self.assertEqual(r.status, 400, body)
            payload = await r.json()
            self.assertEqual(payload['error']['code'], 'invalid_settings', body)
            self.assertIn(fragment, payload['error']['message'], body)
        r = await self.client.put('/api/v1/settings', data=b'[1,2]', headers=dict(self.bearer, **{'Content-Type': 'application/json'}))
        self.assertEqual(r.status, 400)
        self.assertEqual((await r.json())['error']['code'], 'invalid_json')
        # Nothing was applied by any rejected body.
        self.assertEqual(self.router.reserve, {'model': 'claude-max-fable', 'threads': {'thread-1': 'grok-max'}})
        self.assertEqual(self.router.settings, {'ollama': {'enabled': True, 'base_url': 'http://127.0.0.1:11434'}})
        self.assertEqual(self.router.saved_settings, 0)
        self.assertEqual(self.router.saved_reserve, 0)

    async def test_ollama_refresh_returns_note(self):
        r = await self.client.post('/api/v1/ollama/refresh', headers=self.bearer)
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body['note'], 'Restart Codex to see new models')
        self.assertEqual(body['models'], [{'name': 'llama3.2:3b', 'slug': 'ollama-llama3.2-3b'}])
        self.assertEqual(body['catalog'], str(self.router.state / 'models.json'))
        self.assertEqual(self.router.refreshed, 1)

        async def failing():
            raise RuntimeError('Ollama is not running at http://127.0.0.1:11434')
        self.router.refresh_ollama = failing
        with self.assertLogs('dcr.dashboard', 'ERROR'):
            r = await self.client.post('/api/v1/ollama/refresh', headers=self.bearer)
        self.assertEqual(r.status, 502)
        self.assertEqual((await r.json())['error'], {'code': 'ollama_refresh_failed',
                                                     'message': 'Ollama is not running at http://127.0.0.1:11434'})

    async def test_delete_thread_override(self):
        r = await self.client.delete('/api/v1/settings/reserve/threads/thread-1', headers=self.bearer)
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {'ok': True, 'thread': 'thread-1'})
        self.assertEqual(self.router.reserve['threads'], {})
        self.assertEqual(self.router.saved_reserve, 1)
        r = await self.client.delete('/api/v1/settings/reserve/threads/thread-1', headers=self.bearer)
        self.assertEqual(r.status, 404)
        self.assertEqual((await r.json())['error']['code'], 'not_found')

    async def test_unopened_stats_store_is_503_not_500(self):
        # router.py hands build() a Stats it opens later in startup(); if that open never
        # happens, DB-backed endpoints must say so instead of crashing with a traceback.
        closed = Stats(Path(self.temp.name) / 'never-opened.sqlite')
        app = web.Application()
        app.add_subapp('/api/v1', dashboard_api.build(self.router, closed, self.static))
        async with TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar()) as client:
            for path in ('/api/v1/stats/summary', '/api/v1/stats/timeseries', '/api/v1/requests', '/api/v1/usage'):
                with self.assertLogs('dcr.dashboard', 'WARNING'):
                    r = await client.get(path, headers=self.bearer)
                self.assertEqual(r.status, 503, path)
                self.assertEqual((await r.json())['error']['code'], 'stats_unavailable', path)
            # Endpoints that never touch the store keep working.
            for path in ('/api/v1/health', '/api/v1/models', '/api/v1/settings'):
                r = await client.get(path, headers=self.bearer)
                self.assertEqual(r.status, 200, path)

    async def test_unknown_routes_are_json(self):
        r = await self.client.get('/api/v1/nope', headers=self.bearer)
        self.assertEqual(r.status, 404)
        self.assertEqual((await r.json())['error']['code'], 'not_found')
        r = await self.client.delete('/api/v1/models', headers=self.bearer)
        self.assertEqual(r.status, 405)
        self.assertEqual((await r.json())['error']['code'], 'method_not_allowed')

    # ---- static ----
    async def test_static_files(self):
        expected = {'/dashboard/': ('text/html', '<!doctype html><title>DCR test</title>'),
                    '/dashboard/index.html': ('text/html', '<!doctype html><title>DCR test</title>'),
                    '/dashboard/app.js': ('application/javascript', 'console.log("dcr");'),
                    '/dashboard/styles.css': ('text/css', 'body{margin:0}')}
        for path, (content_type, body) in expected.items():
            r = await self.client.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertEqual(r.headers['Cache-Control'], 'no-store', path)
            self.assertEqual(r.content_type, content_type, path)
            self.assertEqual(await r.text(), body, path)
        for path in ('/dashboard/secret.txt', '/dashboard/nested/app.js', '/dashboard/..%2Fcatalog.json',
                     '/dashboard/%2e%2e/catalog.json', '/dashboard/mock.js'):
            r = await self.client.get(path)
            self.assertIn(r.status, (400, 403, 404), path)
            self.assertNotIn('catalog', await r.text(), path)
        # A missing asset is a 404, not a crash.
        (self.static / 'app.js').unlink()
        r = await self.client.get('/dashboard/app.js')
        self.assertEqual(r.status, 404)
        self.assertEqual((await r.json())['error']['code'], 'not_found')


class StatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'nested' / 'stats.sqlite'
        self.stats = Stats(self.path)
        await self.stats.open()

    async def asyncTearDown(self):
        await self.stats.close()
        self.temp.cleanup()

    def test_schema_and_pragmas(self):
        conn = sqlite3.connect(str(self.path))
        try:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {'requests', 'events', 'usage'})
            columns = [row[1] for row in conn.execute('PRAGMA table_info(requests)')]
            self.assertEqual(columns, ['id', 'ts', 'thread', 'model_requested', 'model_served', 'provider', 'kind', 'status',
                                       'error', 'latency_ms', 'input_tokens', 'cached_tokens', 'output_tokens', 'resumed',
                                       'tool_calls', 'path'])
        finally:
            conn.close()

    async def test_reopen_keeps_data(self):
        await self.stats.record_request(provider='claude', latency_ms=10)
        await self.stats.close()
        await self.stats.open()
        self.assertEqual(len(await self.stats.recent()), 1)

    async def test_record_request_defaults_and_unknown_fields(self):
        self.assertTrue(await self.stats.record_request(provider='claude', mystery='ignored', resumed=True,
                                                        latency_ms='12.7', input_tokens=None))
        self.assertTrue(await self.stats.record_request(provider='grok', error='timed out'))
        rows = await self.stats.recent()
        self.assertEqual(len(rows), 2)
        grok, claude = rows
        self.assertEqual(claude['status'], 'ok')
        self.assertEqual(claude['resumed'], 1)
        self.assertEqual(claude['latency_ms'], 12)
        self.assertEqual(claude['input_tokens'], 0)
        self.assertEqual(claude['tool_calls'], 0)
        self.assertIsNone(claude['thread'])
        self.assertAlmostEqual(claude['ts'], time.time(), delta=5)
        self.assertEqual(grok['status'], 'error')
        self.assertEqual(grok['error'], 'timed out')
        self.assertIsNone(grok['latency_ms'])

    async def test_writes_never_raise_when_closed(self):
        await self.stats.close()
        with self.assertLogs('dcr.stats', 'WARNING') as logs:
            self.assertFalse(await self.stats.record_request(provider='claude'))
            self.assertFalse(await self.stats.record_event('startup'))
            self.assertFalse(await self.stats.record_usage('claude', 'five_hour', 0.1, None))
        self.assertEqual(len(logs.output), 3)
        with self.assertRaises(StatsClosed):
            await self.stats.summary(0)
        await self.stats.open()  # asyncTearDown closes it again

    async def test_events_and_usage(self):
        await self.stats.record_event('startup')
        await self.stats.record_event('reserve_changed', {'model': 'grok-max'})
        with self.assertLogs('dcr.stats', 'WARNING'):
            self.assertFalse(await self.stats.record_usage('claude', 'five_hour', 'lots', None))
        await self.stats.record_usage('claude', 'five_hour', 0.2, None)
        await self.stats.record_usage('grok', 'day', 0.9, 1790000000)
        events = await self.stats._run(lambda: [dict(r) for r in self.stats.conn.execute('SELECT * FROM events ORDER BY id')])
        self.assertEqual([(e['kind'], e['detail']) for e in events], [('startup', ''), ('reserve_changed', '{"model": "grok-max"}')])
        usage = await self.stats.latest_usage()
        self.assertEqual([(u['provider'], u['window'], u['utilization'], u['resets_at']) for u in usage],
                         [('claude', 'five_hour', 0.2, None), ('grok', 'day', 0.9, 1790000000)])

    async def test_active_threads_and_prune(self):
        now = time.time()
        for i in range(4):
            await self.stats.record_request(ts=now - i, thread='t%d' % (i % 2), provider='claude')
        await self.stats.record_request(ts=now - 100 * 86400, thread='ancient', provider='claude')
        await self.stats.record_request(ts=now - 3, thread='', provider='claude')
        await self.stats.record_event('startup')
        self.assertEqual(await self.stats.active_threads(now - 3600), 2)
        self.assertEqual(await self.stats.active_threads(0), 3)
        removed = await self.stats.prune(keep_days=90)
        self.assertEqual(removed, {'requests': 1, 'events': 0, 'usage': 0})
        self.assertEqual(len(await self.stats.recent()), 5)
        self.assertEqual(await self.stats.prune(keep_days=0), {'requests': 5, 'events': 1, 'usage': 0})

    async def test_timeseries_alignment(self):
        base = 1_800_000_000  # a multiple of 900
        await self.stats.record_request(ts=base + 10, provider='claude', input_tokens=5)
        await self.stats.record_request(ts=base + 899, provider='claude', status='error')
        await self.stats.record_request(ts=base + 900, provider='grok', cached_tokens=2)
        rows = await self.stats.timeseries(base, 900)
        self.assertEqual(rows, [
            {'t': base, 'provider': 'claude', 'requests': 2, 'errors': 1, 'input_tokens': 5, 'cached_tokens': 0, 'output_tokens': 0},
            {'t': base + 900, 'provider': 'grok', 'requests': 1, 'errors': 0, 'input_tokens': 0, 'cached_tokens': 2, 'output_tokens': 0}])
        self.assertEqual(await self.stats.timeseries(base + 900, 900), rows[1:])
        with self.assertRaises(ValueError):
            await self.stats.timeseries(base, 0)

    def test_percentile_nearest_rank(self):
        self.assertIsNone(percentile([], 0.5))
        self.assertEqual(percentile([7], 0.95), 7)
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 2)
        self.assertEqual(percentile(list(range(1, 101)), 0.95), 95)
        self.assertEqual(percentile(list(range(1, 101)), 0.5), 50)


if __name__ == '__main__':
    unittest.main()
