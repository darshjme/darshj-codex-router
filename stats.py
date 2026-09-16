"""SQLite request/event/usage store behind the dashboard (SSOT §5, box B).

One ``sqlite3`` connection lives on a single-thread executor and every call
goes through ``loop.run_in_executor`` on that executor, so writes are
serialised and the event loop never blocks on disk. The file lives at
``state/stats.sqlite`` in WAL mode with ``PRAGMA user_version`` = 1.

Tables (see SSOT §5 for column meaning):

    requests(id, ts, thread, model_requested, model_served, provider, kind, status, error,
             latency_ms, input_tokens, cached_tokens, output_tokens, resumed, tool_calls, path)
    events(id, ts, kind, detail)
    usage(id, ts, provider, window, utilization, resets_at)

Token semantics follow ``adapter.structured_response``: ``input_tokens`` is the
full prompt (fresh + cached), ``cached_tokens`` the cache-read subset, so
``cache_hit_ratio = cached_tokens / input_tokens`` and ``fresh_tokens =
input_tokens - cached_tokens``. Percentiles are nearest-rank over the fetched
latencies, computed in Python (fine at loopback-router scale).
"""
import asyncio
import json
import logging
import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger('dcr.stats')

SCHEMA_VERSION = 1
REQUEST_COLUMNS = ('ts', 'thread', 'model_requested', 'model_served', 'provider', 'kind', 'status', 'error',
                   'latency_ms', 'input_tokens', 'cached_tokens', 'output_tokens', 'resumed', 'tool_calls', 'path')
INT_COLUMNS = ('latency_ms', 'input_tokens', 'cached_tokens', 'output_tokens', 'resumed', 'tool_calls')
MAX_RECENT = 500
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS requests ('
    ' id INTEGER PRIMARY KEY, ts REAL NOT NULL, thread TEXT, model_requested TEXT, model_served TEXT,'
    ' provider TEXT, kind TEXT, status TEXT, error TEXT, latency_ms INTEGER, input_tokens INTEGER,'
    ' cached_tokens INTEGER, output_tokens INTEGER, resumed INTEGER, tool_calls INTEGER, path TEXT)',
    'CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts)',
    'CREATE INDEX IF NOT EXISTS requests_provider_ts ON requests(provider, ts)',
    'CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT, detail TEXT)',
    'CREATE INDEX IF NOT EXISTS events_ts ON events(ts)',
    'CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, ts REAL NOT NULL, provider TEXT, window TEXT,'
    ' utilization REAL, resets_at INTEGER)',
    'CREATE INDEX IF NOT EXISTS usage_provider_window ON usage(provider, window, id)',
)


class StatsClosed(RuntimeError):
    """A read or write was attempted before open() or after close()."""


def _int(value: Any) -> Optional[int]:
    """Coerce a stored counter to int; None (or garbage) stays None."""
    if value is None or isinstance(value, bool):
        return int(value) if value is not None else None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _ratio(numerator: float, denominator: float) -> float:
    """numerator / denominator rounded to 4 places, 0.0 for an empty denominator."""
    return round(numerator / denominator, 4) if denominator else 0.0


def percentile(values: List[int], fraction: float) -> Optional[int]:
    """Nearest-rank percentile of an ascending list; None when the list is empty."""
    if not values:
        return None
    rank = int(math.ceil(fraction * len(values)))
    return values[max(0, min(len(values) - 1, rank - 1))]


class _Aggregate:
    """Running totals for one slice (all requests, one provider or one model)."""
    __slots__ = ('requests', 'errors', 'input_tokens', 'cached_tokens', 'output_tokens',
                 'tool_calls', 'resumed', 'latencies', 'threads')

    def __init__(self) -> None:
        self.requests = 0
        self.errors = 0
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0
        self.tool_calls = 0
        self.resumed = 0
        self.latencies: List[int] = []
        self.threads: set = set()

    def add(self, row: sqlite3.Row) -> None:
        """Fold one requests row (see Stats._period for the column order) into the totals."""
        self.requests += 1
        if row['status'] == 'error':
            self.errors += 1
        self.input_tokens += row['input_tokens'] or 0
        self.cached_tokens += row['cached_tokens'] or 0
        self.output_tokens += row['output_tokens'] or 0
        self.tool_calls += row['tool_calls'] or 0
        self.resumed += 1 if row['resumed'] else 0
        if row['latency_ms'] is not None:
            self.latencies.append(int(row['latency_ms']))
        if row['thread']:
            self.threads.add(row['thread'])

    def result(self) -> Dict[str, Any]:
        """Serialisable totals block: counts, tokens, ratios and latency percentiles."""
        latencies = sorted(self.latencies)
        return {'requests': self.requests, 'errors': self.errors,
                'input_tokens': self.input_tokens, 'cached_tokens': self.cached_tokens,
                'fresh_tokens': max(self.input_tokens - self.cached_tokens, 0),
                'output_tokens': self.output_tokens, 'tool_calls': self.tool_calls,
                'resumed': self.resumed, 'resumed_ratio': _ratio(self.resumed, self.requests),
                'cache_hit_ratio': _ratio(self.cached_tokens, self.input_tokens),
                'error_ratio': _ratio(self.errors, self.requests),
                'active_threads': len(self.threads),
                'latency': {'avg': round(sum(latencies) / len(latencies), 1) if latencies else None,
                            'p50': percentile(latencies, 0.5), 'p95': percentile(latencies, 0.95)}}


class Stats:
    """Async facade over the SQLite store; every DB call runs on one worker thread."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.conn: Optional[sqlite3.Connection] = None
        self.executor: Optional[ThreadPoolExecutor] = None

    # ---- lifecycle ----
    async def open(self) -> None:
        """Connect (creating the file, WAL mode and schema) on the worker thread. Idempotent."""
        if self.conn is not None:
            return
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dcr-stats')
        loop = asyncio.get_running_loop()
        try:
            self.conn = await loop.run_in_executor(self.executor, self._connect)
        except Exception:
            self.executor.shutdown(wait=True)
            self.executor = None
            raise

    async def close(self) -> None:
        """Close the connection on the worker thread and stop the executor. Idempotent."""
        if self.conn is None:
            return
        conn, executor = self.conn, self.executor
        self.conn = None
        self.executor = None
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(executor, conn.close)
        finally:
            executor.shutdown(wait=True)

    def _connect(self) -> sqlite3.Connection:
        """Worker-thread half of open(): connect, set pragmas, create/upgrade the schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            log.warning('stats: %s has schema version %d, newer than %d', self.path, version, SCHEMA_VERSION)
        for statement in SCHEMA:
            conn.execute(statement)
        if version < SCHEMA_VERSION:
            conn.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
        return conn

    async def _run(self, fn: Callable, *args: Any) -> Any:
        """Run fn(*args) on the single worker thread; raises StatsClosed when not open."""
        if self.conn is None or self.executor is None:
            raise StatsClosed('Stats is not open')
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, fn, *args)

    # ---- writes (never raise: a stats hiccup must not break a Codex turn) ----
    async def record_request(self, **fields: Any) -> bool:
        """Insert one requests row from keyword fields (unknown keys are ignored).

        Defaults: ts=now, status='error' when error is set else 'ok', counters 0,
        latency_ms None. Returns True when stored; logs and returns False otherwise.
        """
        row = self._request_row(fields)
        try:
            await self._run(self._insert, 'requests', row)
            return True
        except (sqlite3.Error, RuntimeError) as error:
            log.warning('stats: request not recorded: %s', error)
            return False

    async def record_event(self, kind: str, detail: Any = '') -> bool:
        """Insert one events row; dict/list details are stored as JSON text."""
        if not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        try:
            await self._run(self._insert, 'events', {'ts': time.time(), 'kind': str(kind), 'detail': detail})
            return True
        except (sqlite3.Error, RuntimeError) as error:
            log.warning('stats: event %s not recorded: %s', kind, error)
            return False

    async def record_usage(self, provider: str, window: str, utilization: float,
                           resets_at: Optional[int]) -> bool:
        """Insert one usage row (a Claude rate-limit window sample)."""
        try:
            row = {'ts': time.time(), 'provider': str(provider), 'window': str(window),
                   'utilization': float(utilization), 'resets_at': _int(resets_at)}
        except (TypeError, ValueError) as error:
            log.warning('stats: usage not recorded: %s', error)
            return False
        try:
            await self._run(self._insert, 'usage', row)
            return True
        except (sqlite3.Error, RuntimeError) as error:
            log.warning('stats: usage not recorded: %s', error)
            return False

    @staticmethod
    def _request_row(fields: Dict[str, Any]) -> Dict[str, Any]:
        """Normalise record_request keyword fields into a full requests row."""
        row: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in REQUEST_COLUMNS:
                log.debug('stats: ignoring unknown request field %s', key)
                continue
            if key == 'ts':
                try:
                    row['ts'] = float(value)
                except (TypeError, ValueError):
                    row['ts'] = time.time()
            elif key in INT_COLUMNS:
                row[key] = _int(value)
            else:
                row[key] = None if value is None else str(value)
        row.setdefault('ts', time.time())
        for key in INT_COLUMNS:
            if key != 'latency_ms' and row.get(key) is None:
                row[key] = 0
        row.setdefault('latency_ms', None)
        row['resumed'] = 1 if row.get('resumed') else 0
        if not row.get('status'):
            row['status'] = 'error' if row.get('error') else 'ok'
        for key in REQUEST_COLUMNS:
            row.setdefault(key, None)
        return row

    def _insert(self, table: str, row: Dict[str, Any]) -> int:
        """Worker-thread INSERT of one dict row; returns the new row id."""
        columns = list(row)
        sql = 'INSERT INTO %s (%s) VALUES (%s)' % (table, ', '.join(columns), ', '.join('?' * len(columns)))
        cursor = self.conn.execute(sql, [row[c] for c in columns])
        return cursor.lastrowid

    # ---- reads ----
    async def summary(self, since_ts: float) -> Dict[str, Any]:
        """Totals, per-provider and per-model blocks for [since_ts, now), plus the
        equal-length prior period under ``previous`` for dashboard deltas."""
        return await self._run(self._summary, float(since_ts), time.time())

    def _summary(self, since: float, until: float) -> Dict[str, Any]:
        """Worker-thread half of summary()."""
        length = max(until - since, 0.0)
        current = self._period(since, until)
        previous = self._period(since - length, since)
        return {'since': since, 'until': until, 'totals': current['totals'],
                'providers': current['providers'], 'models': current['models'],
                'previous': {'since': since - length, 'until': since,
                             'totals': previous['totals'], 'providers': previous['providers']}}

    def _period(self, since: float, until: float) -> Dict[str, Any]:
        """Aggregate every requests row with since <= ts < until."""
        rows = self.conn.execute(
            'SELECT provider, model_served, status, latency_ms, input_tokens, cached_tokens, output_tokens,'
            ' resumed, tool_calls, thread FROM requests WHERE ts >= ? AND ts < ?', (since, until)).fetchall()
        totals, providers, models = _Aggregate(), {}, {}
        for row in rows:
            totals.add(row)
            providers.setdefault(row['provider'] or 'unknown', _Aggregate()).add(row)
            models.setdefault(row['model_served'] or 'unknown', _Aggregate()).add(row)
        return {'totals': totals.result(),
                'providers': {k: providers[k].result() for k in sorted(providers)},
                'models': {k: models[k].result() for k in sorted(models)}}

    async def timeseries(self, since_ts: float, bucket_s: int) -> List[Dict[str, Any]]:
        """Sparse per-(bucket, provider) counts since since_ts; ``t`` is the bucket
        start, aligned to multiples of bucket_s since the epoch."""
        bucket = int(bucket_s)
        if bucket <= 0:
            raise ValueError('bucket_s must be positive')
        return await self._run(self._timeseries, float(since_ts), bucket)

    def _timeseries(self, since: float, bucket: int) -> List[Dict[str, Any]]:
        """Worker-thread half of timeseries()."""
        rows = self.conn.execute(
            'SELECT CAST(ts / ? AS INTEGER) * ? AS t, provider, COUNT(*) AS requests,'
            " SUM(status = 'error') AS errors, SUM(COALESCE(input_tokens, 0)) AS input_tokens,"
            ' SUM(COALESCE(cached_tokens, 0)) AS cached_tokens, SUM(COALESCE(output_tokens, 0)) AS output_tokens'
            ' FROM requests WHERE ts >= ? GROUP BY t, provider ORDER BY t, provider',
            (bucket, bucket, since)).fetchall()
        return [{'t': int(r['t']), 'provider': r['provider'] or 'unknown', 'requests': int(r['requests']),
                 'errors': int(r['errors'] or 0), 'input_tokens': int(r['input_tokens'] or 0),
                 'cached_tokens': int(r['cached_tokens'] or 0), 'output_tokens': int(r['output_tokens'] or 0)}
                for r in rows]

    async def recent(self, limit: int = 50, before_id: Optional[int] = None, provider: Optional[str] = None,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Newest requests first (id descending), optionally older than before_id
        and filtered by provider/status. limit is clamped to 1..MAX_RECENT."""
        limit = max(1, min(int(limit), MAX_RECENT))
        before = _int(before_id) if before_id is not None else None
        return await self._run(self._recent, limit, before, provider, status)

    def _recent(self, limit: int, before: Optional[int], provider: Optional[str],
                status: Optional[str]) -> List[Dict[str, Any]]:
        """Worker-thread half of recent()."""
        where, args = [], []
        if before is not None:
            where.append('id < ?')
            args.append(before)
        if provider:
            where.append('provider = ?')
            args.append(provider)
        if status:
            where.append('status = ?')
            args.append(status)
        sql = 'SELECT * FROM requests'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    async def latest_usage(self) -> List[Dict[str, Any]]:
        """Newest usage row per (provider, window), ordered by provider then window."""
        return await self._run(self._latest_usage)

    def _latest_usage(self) -> List[Dict[str, Any]]:
        """Worker-thread half of latest_usage()."""
        rows = self.conn.execute(
            'SELECT u.* FROM usage u JOIN (SELECT provider, window, MAX(id) AS id FROM usage'
            ' GROUP BY provider, window) latest ON u.id = latest.id ORDER BY u.provider, u.window').fetchall()
        return [dict(r) for r in rows]

    async def active_threads(self, since_ts: float) -> int:
        """Distinct non-empty thread ids seen since since_ts."""
        return await self._run(self._active_threads, float(since_ts))

    def _active_threads(self, since: float) -> int:
        """Worker-thread half of active_threads()."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT thread) FROM requests WHERE ts >= ? AND thread IS NOT NULL AND thread != ''",
            (since,)).fetchone()
        return int(row[0]) if row else 0

    async def prune(self, keep_days: float = 90) -> Dict[str, int]:
        """Delete rows older than keep_days from every table; returns deleted counts."""
        cutoff = time.time() - float(keep_days) * 86400
        return await self._run(self._prune, cutoff)

    def _prune(self, cutoff: float) -> Dict[str, int]:
        """Worker-thread half of prune()."""
        removed = {}
        for table in ('requests', 'events', 'usage'):
            removed[table] = self.conn.execute('DELETE FROM %s WHERE ts < ?' % table, (cutoff,)).rowcount
        return removed
