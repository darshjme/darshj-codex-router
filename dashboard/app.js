/*
 * Darshj's Codex Router — dashboard front-end.
 * Vanilla JS, one module, no framework. Talks to /api/v1 exactly as defined in
 * docs/SSOT.md §5. Sections: constants · utils · state · api · toasts · charts ·
 * components · views · app. The only global is `DCR`.
 */
const DCR = (function () {
  'use strict';

  /* ================================================================ constants */
  const API_BASE = '/api/v1';
  const RANGES = ['1h', '24h', '7d', '30d'];
  const RANGE_MS = { '1h': 3600e3, '24h': 86400e3, '7d': 7 * 86400e3, '30d': 30 * 86400e3 };
  const BUCKET_MS = { '1h': 60e3, '24h': 900e3, '7d': 3600e3, '30d': 6 * 3600e3 }; // SSOT §5 bucket=auto
  const NEXT_RANGE = { '1h': '24h', '24h': '7d', '7d': '30d', '30d': null };      // used to derive "vs previous period"
  const SERIES = ['claude', 'openai', 'grok', 'ollama'];                           // fixed chart order (SSOT §2)
  const PROVIDER_LABEL = { claude: 'Claude', openai: 'OpenAI', grok: 'Grok', ollama: 'Ollama', router: 'Router' };
  const MODEL_GROUPS = [['claude', 'Claude'], ['grok', 'Grok'], ['ollama', 'Ollama'], ['openai', 'OpenAI native']];
  const VIEWS = { overview: 'Overview', models: 'Models', requests: 'Requests', usage: 'Usage', settings: 'Settings', api: 'API' };
  const THEMES = ['light', 'dark', 'system'];
  const REFRESH_MS = 30000;
  const PAGE_SIZE = 50;
  const ROUTER_ORIGIN = 'http://127.0.0.1:18740';
  const TOKEN_CMD = 'cat ~/repos/darshj-codex-router/state/dashboard-token';
  const RESTART_CMD = 'launchctl kickstart -k gui/$(id -u)/ai.darshj.codex-router';
  const HINT_UNREACHABLE = 'Router unreachable — check `launchctl list | grep codex-router`';
  const HINT_LOG = 'check the log in ~/repos/darshj-codex-router/state/ and restart with `' + RESTART_CMD + '`';
  const DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434';
  const TS_KEYS = ['timeseries', 'series', 'buckets', 'rows', 'items', 'data'];
  const REQ_KEYS = ['requests', 'rows', 'items', 'data'];
  const MODEL_KEYS = ['models', 'items', 'data'];
  const WINDOW_KEYS = ['windows', 'usage', 'latest', 'rows', 'items'];
  const WINDOW_LABEL = { five_hour: '5-hour window', '5h': '5-hour window', primary: '5-hour window',
    seven_day: '7-day window', '7d': '7-day window', secondary: '7-day window' };
  const COUNTER_LABEL = { claude: 'Claude turns', grok: 'Grok turns', ollama: 'Ollama turns', openai: 'OpenAI passthrough',
    fallback: 'Reserve fallbacks', errors: 'Errors', router: 'Router commands' };

  const ICON_COPY = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3.5a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></svg>';
  const ICON_THEME = {
    light: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/></svg>',
    dark: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M13 10.5A6 6 0 0 1 5.5 3a6 6 0 1 0 7.5 7.5z"/></svg>',
    system: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor" stroke="none"/></svg>'
  };

  /* ================================================================ utils */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  /** Escape any API-provided text before it goes into an HTML string. */
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, c => ESC_MAP[c]); }
  function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : (d || 0); }
  function clamp(v, a, b) { return Math.min(b, Math.max(a, v)); }
  function f2(v) { return Math.round(v * 100) / 100; }

  const INT = new Intl.NumberFormat('en-US');
  function fmtInt(v) { return INT.format(Math.round(num(v))); }
  function fmtCompact(v) {
    const n = num(v), a = Math.abs(n);
    if (a < 1000) return fmtInt(n);
    const units = [[1e9, 'B'], [1e6, 'M'], [1e3, 'k']];
    for (const [div, u] of units) {
      if (a >= div) {
        const x = n / div;
        return (Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(1).replace(/\.0$/, '')) + u;
      }
    }
    return fmtInt(n);
  }
  function fmtPct(frac, digits) { return (num(frac) * 100).toFixed(digits == null ? 1 : digits).replace(/\.0$/, '') + '%'; }
  function fmtMs(ms) {
    if (ms == null || !Number.isFinite(Number(ms))) return '—';
    ms = num(ms);
    if (ms < 1000) return fmtInt(ms) + ' ms';
    return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + ' s';
  }
  function fmtDuration(ms) {
    let s = Math.max(0, Math.round(num(ms) / 1000));
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + m + 'm';
    if (m) return m + 'm';
    return s + 's';
  }
  /** Epoch seconds (SQLite REAL), epoch ms or ISO string → ms. */
  function toMs(ts) {
    if (ts == null) return 0;
    const n = Number(ts);
    if (Number.isFinite(n)) return n > 1e12 ? n : n * 1000;
    const p = Date.parse(ts);
    return Number.isFinite(p) ? p : 0;
  }
  function relTime(ms) {
    if (!ms) return '—';
    const diff = Date.now() - ms;
    if (diff < 45e3) return 'just now';
    const m = Math.round(diff / 60e3); if (m < 60) return m + 'm ago';
    const h = Math.round(diff / 3600e3); if (h < 24) return h + 'h ago';
    const d = Math.round(diff / 86400e3); if (d < 14) return d + 'd ago';
    return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  function absTime(ms) { return ms ? new Date(ms).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—'; }
  function clock(ms) { return new Date(ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }); }
  function dateShort(ms) { return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
  function fmtContext(n) {
    n = num(n);
    if (!n) return '—';
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    return Math.round(n / 1000) + 'k';
  }
  function shortId(id) { return String(id || '').slice(0, 8); }
  function providerLabel(p) {
    p = String(p || '').toLowerCase();
    return PROVIDER_LABEL[p] || (p ? p[0].toUpperCase() + p.slice(1) : 'Unknown');
  }
  function seriesClass(p) { p = String(p || '').toLowerCase(); return SERIES.includes(p) ? 's-' + p : 's-router'; }
  function titleCase(s) { return String(s || '').replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }
  /** Escape, then turn `backticked` spans into <code> — used for hints that carry shell commands. */
  function inlineCode(text) { return esc(text).replace(/`([^`]+)`/g, '<code>$1</code>'); }
  /** Accept a bare array or an object wrapping the array under one of `keys`. */
  function listOf(payload, keys) {
    if (Array.isArray(payload)) return payload;
    if (payload && typeof payload === 'object') {
      for (const k of keys) if (Array.isArray(payload[k])) return payload[k];
    }
    return [];
  }
  function storage(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
    } catch (e) { /* storage unavailable (private mode) — feature degrades to in-memory */ }
    return null;
  }
  async function copyText(text, what) {
    let ok = true;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', '');
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
    }
    if (ok) toast((what || 'Copied') + ' copied to clipboard', 'success');
    else toast('Copy failed — select the text and copy it manually', 'error');
  }
  function copyBtn(text, what) {
    return '<button type="button" class="btn btn-ghost btn-icon" data-copy="' + esc(text) + '" data-what="' + esc(what) +
      '" aria-label="Copy ' + esc(what.toLowerCase()) + '" title="Copy ' + esc(what.toLowerCase()) + '">' + ICON_COPY + '</button>';
  }
  function statusHtml(kind, label) {
    const ico = kind === 'ok' ? '✓' : kind === 'error' ? '✕' : kind === 'warning' || kind === 'serious' ? '⚠' : '•';
    return '<span class="status status-' + esc(kind) + '"><span class="ico" aria-hidden="true">' + ico + '</span>' + esc(label) + '</span>';
  }
  function swatch(p) { return '<span class="swatch ' + seriesClass(p) + '" aria-hidden="true"></span>'; }
  function providerHtml(p) { return '<span class="provider">' + swatch(p) + esc(providerLabel(p)) + '</span>'; }
  function emptyHtml(title, body, action) {
    return '<div class="empty"><strong>' + esc(title) + '</strong>' + (body ? '<span>' + inlineCode(body) + '</span>' : '') +
      (action ? '<div><button type="button" class="btn btn-ghost btn-sm" data-action="' + esc(action.action) + '">' + esc(action.label) + '</button></div>' : '') + '</div>';
  }
  function errorHtml(err) {
    return '<div class="empty"><strong>Couldn\'t load this page</strong><span>' + inlineCode(err && err.message || HINT_UNREACHABLE) +
      '</span><div><button type="button" class="btn btn-primary btn-sm" data-action="retry">Retry</button></div></div>';
  }
  function skel(cls, n) { let s = ''; for (let i = 0; i < (n || 1); i++) s += '<div class="skeleton ' + cls + '" aria-hidden="true"></div>'; return s; }

  /* ================================================================ state */
  const state = {
    mode: 'loading',                       // loading | login | app
    view: 'overview',
    range: RANGES.includes(storage('dcr-range')) ? storage('dcr-range') : '24h',
    theme: THEMES.includes(storage('dcr-theme')) ? storage('dcr-theme') : 'system',
    health: null,
    healthError: null,
    models: null,                          // GET /models list
    settings: null,                        // GET /settings object
    overview: { loaded: false, loading: false, error: null, summary: null, rows: [], prevRows: null, recent: [], mode: 'chart' },
    requests: { loaded: false, loading: false, error: null, rows: [], provider: '', status: '', done: false, expanded: new Set() },
    models_: { loaded: false, loading: false, error: null },
    usage: { loaded: false, loading: false, error: null, windows: [], counts: null },
    settings_: { loaded: false, loading: false, error: null, rotatedToken: null, confirmRotate: false },
    ollamaDiscovered: null,                // models[] from the last POST /ollama/refresh
    refreshTimer: 0,
    lastErrorToast: { message: '', at: 0 }
  };

  /* ================================================================ api */
  class ApiError extends Error {
    constructor(status, code, message) { super(message); this.status = status; this.code = code; }
  }
  /** Turn a status + server message into a message that says what to do next. */
  function explain(status, msg) {
    switch (status) {
      case 401: return 'Session expired — sign in again with your dashboard token';
      case 403: return 'Request blocked (same-origin only) — open ' + ROUTER_ORIGIN + '/dashboard/ directly';
      case 404: return (msg || 'Endpoint not found') + ' — the router may be an older build; restart it with `' + RESTART_CMD + '`';
      case 429: return 'Too many attempts — wait 10 minutes, then try again';
      case 400:
      case 422: return (msg || 'Invalid value') + ' — check the value and try again';
      default:
        if (status >= 500) return (msg || 'Router error ' + status) + ' — ' + HINT_LOG;
        return (msg || 'Request failed (' + status + ')') + ' — retry; if it persists, ' + HINT_LOG;
    }
  }
  async function request(method, path, body, opts) {
    opts = opts || {};
    const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(API_BASE + path, init); }
    catch (e) { throw new ApiError(0, 'network', HINT_UNREACHABLE); }
    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    if (res.ok) return data;
    const err = (data && data.error) || {};
    if (res.status === 401 && !opts.noAuthRedirect) app.onUnauthorized();
    throw new ApiError(res.status, err.code || 'http_' + res.status, explain(res.status, err.message));
  }
  const api = {
    get: (p, o) => request('GET', p, undefined, o),
    post: (p, b, o) => request('POST', p, b === undefined ? {} : b, o),
    put: (p, b, o) => request('PUT', p, b, o),
    del: (p, o) => request('DELETE', p, undefined, o)
  };
  /** Toast an error, but not the same one again within 60s (background refresh while the router is down). */
  function reportError(err, silent) {
    if (!err || err.status === 401) return;
    const msg = err.message || HINT_UNREACHABLE;
    const now = Date.now();
    if (silent && state.lastErrorToast.message === msg && now - state.lastErrorToast.at < 60e3) return;
    state.lastErrorToast = { message: msg, at: now };
    toast(msg, 'error');
  }

  /* ================================================================ toasts */
  function toast(message, kind, opts) {
    const host = $('#toasts');
    if (!host) return;
    kind = kind || 'info';
    const el = document.createElement('div');
    el.className = 'toast toast-' + kind;
    el.innerHTML = '<span class="toast-text">' + inlineCode(message) + '</span>' +
      '<button type="button" class="toast-close" aria-label="Dismiss notification">×</button>';
    host.appendChild(el);
    const ttl = (opts && opts.timeout) || (kind === 'error' ? 10000 : 5000);
    const timer = setTimeout(() => el.remove(), ttl);
    $('.toast-close', el).addEventListener('click', () => { clearTimeout(timer); el.remove(); });
    while (host.children.length > 4) host.firstElementChild.remove();
  }

  /* ================================================================ charts */
  const charts = (function () {
    const chartData = new WeakMap();   // host element → { buckets, geom, range, bucket, focus }

    /** Group timeseries rows into a full grid of buckets (empty periods show as zero). */
    function buildBuckets(rows, range, now) {
      const bucket = BUCKET_MS[range], span = RANGE_MS[range];
      const start = Math.floor((now - span) / bucket) * bucket;
      const end = Math.floor(now / bucket) * bucket;
      const blank = t => ({ t, values: { claude: 0, openai: 0, grok: 0, ollama: 0 }, total: 0, errors: 0 });
      const map = new Map();
      for (let t = start; t <= end; t += bucket) map.set(t, blank(t));
      for (const r of rows || []) {
        const p = String(r.provider || '').toLowerCase();
        if (!SERIES.includes(p)) continue;               // router rows (commands) are not a model provider
        const t = Math.floor(toMs(r.t) / bucket) * bucket;
        if (t < start) continue;
        if (!map.has(t)) map.set(t, blank(t));
        const b = map.get(t), n = num(r.requests);
        b.values[p] += n; b.total += n; b.errors += num(r.errors);
      }
      return { bucket, buckets: Array.from(map.values()).sort((a, b) => a.t - b.t) };
    }

    function niceTicks(max, count) {
      if (max <= 0) return [0, 1];
      let step;
      if (max < count) step = 1;
      else {
        const raw = max / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), norm = raw / mag;
        step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
      }
      const top = Math.ceil(max / step) * step, ticks = [];
      for (let v = 0; v <= top + 1e-9; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
      return ticks;
    }

    /** Which buckets get an x-axis label: clean clock/day boundaries, falling back to even spacing. */
    function labelIndexes(buckets, range) {
      const picks = [];
      let dayCount = 0;
      buckets.forEach((b, i) => {
        const d = new Date(b.t);
        if (range === '1h') { if (d.getMinutes() % 10 === 0) picks.push(i); }
        else if (range === '24h') { if (d.getMinutes() === 0 && d.getHours() % 4 === 0) picks.push(i); }
        else if (range === '7d') { if (d.getHours() === 0) picks.push(i); }
        else if (d.getHours() === 0) { if (dayCount % 5 === 0) picks.push(i); dayCount++; }
      });
      if (picks.length >= 2) return picks;
      const step = Math.max(1, Math.floor(buckets.length / 6)), out = [];
      for (let i = 0; i < buckets.length; i += step) out.push(i);
      return out;
    }
    function timeLabel(t, range) { return (range === '1h' || range === '24h') ? clock(t) : dateShort(t); }
    function bucketRange(t, bucket) { return dateShort(t) + ' ' + clock(t) + ' – ' + clock(t + bucket); }

    function roundedTop(x, y, w, h, r, cls) {
      r = Math.min(r, h, w / 2);
      const x2 = x + w, yb = y + h;
      const d = 'M' + f2(x) + ',' + f2(yb) + ' V' + f2(y + r) + ' Q' + f2(x) + ',' + f2(y) + ' ' + f2(x + r) + ',' + f2(y) +
        ' H' + f2(x2 - r) + ' Q' + f2(x2) + ',' + f2(y) + ' ' + f2(x2) + ',' + f2(y + r) + ' V' + f2(yb) + ' Z';
      return '<path class="' + cls + '" d="' + d + '"/>';
    }

    /** Render a stacked bar chart into `host` (.chart-wrap) at the given pixel width. */
    function render(host, data, range, width) {
      const buckets = data.buckets;
      const W = Math.max(280, Math.floor(width || host.clientWidth || 640)), H = 240;
      const padL = 46, padR = 10, padT = 22, padB = 26;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const n = Math.max(1, buckets.length), slot = plotW / n;
      const bw = clamp(slot - 2, 1.5, 24);                 // 2px surface gap between neighbours, ≤24px thick
      const max = buckets.reduce((m, b) => Math.max(m, b.total), 0);
      const ticks = niceTicks(max, 4), top = ticks[ticks.length - 1] || 1;
      const y = v => padT + plotH - (v / top) * plotH;
      const GAP = 2;
      let out = '';

      for (const tk of ticks) {
        const yy = Math.round(y(tk)) + 0.5;
        out += '<line class="grid-line" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + yy + '" y2="' + yy + '"/>' +
          '<text class="axis-text" x="' + (padL - 8) + '" y="' + (yy + 4) + '" text-anchor="end">' + fmtCompact(tk) + '</text>';
      }

      buckets.forEach((b, i) => {
        const x = padL + i * slot + (slot - bw) / 2;
        const segs = SERIES.filter(p => b.values[p] > 0);
        let yCursor = padT + plotH, inner = '';
        segs.forEach((p, si) => {
          const h = (b.values[p] / top) * plotH, y0 = yCursor - h, isTop = si === segs.length - 1;
          yCursor = y0;
          if (h < 0.75) return;                            // sub-pixel sliver: lives in the tooltip/table only
          if (isTop) inner += roundedTop(x, y0, bw, h, Math.min(4, bw / 2), seriesClass(p));
          else {
            const cut = h > GAP + 1 ? GAP : 0;            // 2px surface gap cut from the top of lower segments
            inner += '<rect class="' + seriesClass(p) + '" x="' + f2(x) + '" y="' + f2(y0 + cut) + '" width="' + f2(bw) + '" height="' + f2(h - cut) + '"/>';
          }
        });
        out += '<g class="bar" data-i="' + i + '">' + inner + '</g>';
      });

      // direct labels on the three biggest bars, skipping any that would collide
      const ranked = buckets.map((b, i) => ({ i, total: b.total })).filter(o => o.total > 0)
        .sort((a, b) => b.total - a.total).slice(0, 3).sort((a, b) => a.i - b.i);
      let lastRight = -Infinity;
      for (const o of ranked) {
        const label = fmtCompact(o.total), wEst = label.length * 6.6 + 8;
        const cx = clamp(padL + o.i * slot + slot / 2, padL + wEst / 2, W - padR - wEst / 2);
        if (cx - wEst / 2 < lastRight + 4) continue;
        lastRight = cx + wEst / 2;
        out += '<text class="chart-label" x="' + f2(cx) + '" y="' + f2(y(buckets[o.i].total) - 5) + '" text-anchor="middle">' + label + '</text>';
      }

      for (const i of labelIndexes(buckets, range)) {
        const cx = padL + i * slot + slot / 2;
        out += '<text class="axis-text" x="' + f2(cx) + '" y="' + (H - 8) + '" text-anchor="middle">' + esc(timeLabel(buckets[i].t, range)) + '</text>';
      }

      const aria = 'Requests per ' + fmtDuration(data.bucket) + ' over the last ' + range + ', stacked by provider. ' +
        'Use Left and Right arrow keys to read each bar, or switch to the table view.';
      host.innerHTML = '<svg class="chart-svg" viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" tabindex="0" aria-label="' + esc(aria) + '">' +
        '<rect class="hot" x="0" y="' + padT + '" width="' + f2(slot) + '" height="' + plotH + '" visibility="hidden"/>' + out + '</svg>' +
        '<div class="chart-tip" hidden></div>';
      chartData.set(host, { buckets, geom: { padL, padT, plotH, slot, W, y }, range, bucket: data.bucket, focus: -1 });
      bind(host);
    }

    function hide(host) {
      const tip = $('.chart-tip', host), hot = $('.hot', host);
      if (tip) tip.hidden = true;
      if (hot) hot.setAttribute('visibility', 'hidden');
      $$('.bar.is-hot', host).forEach(el => el.classList.remove('is-hot'));
    }

    function show(host, i, announce) {
      const c = chartData.get(host);
      if (!c || !c.buckets[i]) return;
      const b = c.buckets[i], g = c.geom, tip = $('.chart-tip', host), hot = $('.hot', host), svg = $('svg', host);
      hot.setAttribute('x', f2(g.padL + i * g.slot));
      hot.setAttribute('visibility', 'visible');
      $$('.bar.is-hot', host).forEach(el => el.classList.remove('is-hot'));
      const bar = $('.bar[data-i="' + i + '"]', host);
      if (bar) bar.classList.add('is-hot');

      // content — built with textContent (values are numbers, labels are our constants)
      tip.replaceChildren();
      const title = document.createElement('div');
      title.className = 'tip-title';
      title.textContent = bucketRange(b.t, c.bucket);
      tip.appendChild(title);
      const lines = [];
      for (const p of SERIES) {
        const row = document.createElement('div'); row.className = 'tip-row';
        const key = document.createElement('span'); key.className = 'tip-key ' + seriesClass(p);
        const val = document.createElement('span'); val.className = 'tip-val'; val.textContent = fmtInt(b.values[p]);
        const name = document.createElement('span'); name.className = 'tip-name'; name.textContent = providerLabel(p);
        row.append(key, val, name); tip.appendChild(row);
        lines.push(providerLabel(p) + ' ' + fmtInt(b.values[p]));
      }
      const total = document.createElement('div'); total.className = 'tip-row tip-total';
      const tv = document.createElement('span'); tv.className = 'tip-val'; tv.textContent = fmtInt(b.total);
      const tn = document.createElement('span'); tn.className = 'tip-name'; tn.textContent = 'total' + (b.errors ? ' · ' + fmtInt(b.errors) + ' errors' : '');
      total.append(document.createElement('span'), tv, tn); tip.appendChild(total);
      tip.hidden = false;

      // position: beside the bar, flipped when it would overflow the host
      const hostRect = host.getBoundingClientRect(), svgRect = svg.getBoundingClientRect();
      const scale = svgRect.width / g.W;
      const barX = (svgRect.left - hostRect.left) + (g.padL + i * g.slot + g.slot / 2) * scale;
      const barTop = (svgRect.top - hostRect.top) + g.y(b.total) * scale;
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      let left = barX + 14;
      if (left + tw > hostRect.width) left = Math.max(0, barX - 14 - tw);
      tip.style.left = Math.round(left) + 'px';
      tip.style.top = Math.round(clamp(barTop - 10, 0, Math.max(0, hostRect.height - th))) + 'px';

      if (announce) {
        const live = $('#chart-live');
        if (live) live.textContent = bucketRange(b.t, c.bucket) + ': ' + lines.join(', ') + ', total ' + fmtInt(b.total);
      }
    }

    function bind(host) {
      if (host.dataset.bound) return;
      host.dataset.bound = '1';
      host.addEventListener('pointermove', e => {
        const c = chartData.get(host), svg = $('svg', host);
        if (!c || !svg) return;
        const r = svg.getBoundingClientRect();
        const x = (e.clientX - r.left) / (r.width / c.geom.W);
        const i = Math.floor((x - c.geom.padL) / c.geom.slot);
        if (i < 0 || i >= c.buckets.length) { if (c.focus < 0) hide(host); return; }
        show(host, i, false);
      });
      host.addEventListener('pointerleave', () => {
        const c = chartData.get(host);
        if (c && c.focus >= 0) show(host, c.focus, false); else hide(host);
      });
      host.addEventListener('keydown', e => {
        const c = chartData.get(host);
        if (!c || !e.target.classList.contains('chart-svg')) return;
        const n = c.buckets.length;
        let i = c.focus;
        if (e.key === 'ArrowRight') i = i < 0 ? n - 1 : Math.min(n - 1, i + 1);
        else if (e.key === 'ArrowLeft') i = i < 0 ? n - 1 : Math.max(0, i - 1);
        else if (e.key === 'Home') i = 0;
        else if (e.key === 'End') i = n - 1;
        else if (e.key === 'Escape') { c.focus = -1; hide(host); return; }
        else return;
        e.preventDefault();
        c.focus = i;
        show(host, i, true);
      });
      host.addEventListener('focusout', e => {
        if (host.contains(e.relatedTarget)) return;
        const c = chartData.get(host);
        if (c) c.focus = -1;
        hide(host);
      });
    }

    function tableHtml(data, range) {
      const rows = data.buckets.map(b =>
        '<tr><th scope="row" class="nowrap">' + esc(bucketRange(b.t, data.bucket)) + '</th>' +
        SERIES.map(p => '<td class="num">' + fmtInt(b.values[p]) + '</td>').join('') +
        '<td class="num"><strong>' + fmtInt(b.total) + '</strong></td></tr>').join('');
      return '<div class="table-scroll tall"><table class="table"><caption>Requests per ' + esc(fmtDuration(data.bucket)) +
        ' bucket over the last ' + esc(range) + ', by provider</caption><thead><tr><th scope="col">Bucket</th>' +
        SERIES.map(p => '<th scope="col" class="num">' + esc(providerLabel(p)) + '</th>').join('') +
        '<th scope="col" class="num">Total</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
    }

    return { buildBuckets, render, tableHtml };
  })();

  /* ================================================================ components */

  /** Normalise GET /stats/summary: totals + per provider + per model (SSOT §5 field names, flat or nested latency). */
  function normSummary(raw) {
    const s = raw || {};
    const totalsRaw = s.totals || s.total || s.all || s;
    const provRaw = s.providers || s.by_provider || s.per_provider || {};
    const modelRaw = s.models || s.by_model || s.per_model || {};
    const toMap = (v, key) => {
      if (Array.isArray(v)) { const o = {}; v.forEach(it => { if (it && it[key] != null) o[String(it[key]).toLowerCase()] = it; }); return o; }
      return v && typeof v === 'object' ? v : {};
    };
    const bucket = b => {
      b = b || {};
      const lat = b.latency || {}, tok = b.tokens || {};
      const input = num(b.input_tokens != null ? b.input_tokens : tok.input);
      const cached = num(b.cached_tokens != null ? b.cached_tokens : tok.cached);
      const output = num(b.output_tokens != null ? b.output_tokens : tok.output);
      return {
        requests: num(b.requests), errors: num(b.errors),
        input_tokens: input, cached_tokens: cached, output_tokens: output,
        latency_avg: b.latency_avg != null ? num(b.latency_avg) : (lat.avg != null ? num(lat.avg) : null),
        latency_p50: b.latency_p50 != null ? num(b.latency_p50) : (lat.p50 != null ? num(lat.p50) : null),
        latency_p95: b.latency_p95 != null ? num(b.latency_p95) : (lat.p95 != null ? num(lat.p95) : null),
        resumed_ratio: b.resumed_ratio != null ? num(b.resumed_ratio) : null,
        cache_hit_ratio: b.cache_hit_ratio != null ? num(b.cache_hit_ratio) : (input + cached > 0 ? cached / (input + cached) : 0),
        tool_calls: num(b.tool_calls)
      };
    };
    const providers = {};
    const pm = toMap(provRaw, 'provider');
    Object.keys(pm).forEach(k => { providers[k.toLowerCase()] = bucket(pm[k]); });
    const models = {};
    const mm = toMap(modelRaw, 'model');
    Object.keys(mm).forEach(k => { models[k] = bucket(mm[k]); });
    const active = s.active_threads != null ? s.active_threads : (s.threads != null ? s.threads : totalsRaw.active_threads);
    return { totals: bucket(totalsRaw), providers, models, active_threads: active != null ? num(active) : null };
  }

  /** Sums over [now-span, now) and [now-2span, now-span) from the next-larger timeseries, so both sides share a bucket grid. */
  function comparison(prevRows, range) {
    if (!Array.isArray(prevRows)) return null;
    const span = RANGE_MS[range], now = Date.now();
    const sum = (from, to) => {
      const acc = { requests: 0, errors: 0, input_tokens: 0, cached_tokens: 0, output_tokens: 0 };
      for (const r of prevRows) {
        const t = toMs(r.t);
        if (t < from || t >= to) continue;
        for (const k in acc) acc[k] += num(r[k]);
      }
      return acc;
    };
    return { cur: sum(now - span, now), prev: sum(now - 2 * span, now - span) };
  }
  function deltaHtml(cur, prev, upIsGood, label, pp) {
    if (cur == null || prev == null) return '<span class="delta" title="No previous period to compare against">— ' + esc(label) + '</span>';
    if (prev === 0 && cur === 0) return '<span class="delta">— no change ' + esc(label) + '</span>';
    if (prev === 0) return '<span class="delta ' + (upIsGood ? 'good' : 'bad') + '"><span class="arrow" aria-hidden="true">▲</span><span class="sr-only">up,</span> new ' + esc(label) + '</span>';
    const diff = pp ? cur - prev : (cur - prev) / prev;
    const flat = Math.abs(diff) < (pp ? 0.0005 : 0.0005), up = diff > 0;
    const cls = flat ? '' : (up === upIsGood ? 'good' : 'bad');
    const arrow = flat ? '—' : (up ? '▲' : '▼');
    const amount = pp ? (Math.abs(diff) * 100).toFixed(1).replace(/\.0$/, '') + ' pp' : fmtPct(Math.abs(diff), 1);
    return '<span class="delta ' + cls + '"><span class="arrow" aria-hidden="true">' + arrow + '</span><span class="sr-only">' +
      (flat ? 'unchanged' : (up ? 'up' : 'down')) + '</span> ' + (flat ? '' : amount + ' ') + esc(label) + '</span>';
  }
  function tileHtml(label, value, delta, title) {
    return '<div class="tile"><p class="eyebrow">' + esc(label) + '</p><div class="value"' + (title ? ' title="' + esc(title) + '"' : '') + '>' + value + '</div>' + delta + '</div>';
  }
  function onlineState(v) {
    if (v === true) return { cls: 'good', label: 'online' };
    if (v === false) return { cls: 'critical', label: 'offline' };
    return { cls: '', label: 'status unknown' };
  }
  function modelDisplay(slug) {
    const m = (state.models || []).find(x => x.slug === slug);
    return m && m.display_name ? m.display_name : slug;
  }

  /** Shared Ollama settings panel (Models + Settings). */
  function ollamaPanelHtml(id) {
    const s = (state.settings && state.settings.ollama) || {};
    const h = (state.health && state.health.ollama) || {};
    const enabled = s.enabled != null ? !!s.enabled : !!h.enabled;
    const base = s.base_url || h.base_url || DEFAULT_OLLAMA_URL;
    const online = h.online === true;
    const found = (state.models || []).filter(m => String(m.provider || '').toLowerCase() === 'ollama');
    let list;
    if (found.length) {
      list = '<ul class="ollama-list" aria-label="Discovered Ollama models">' + found.map(m => {
        const st = onlineState(m.online);
        const mods = Array.isArray(m.input_modalities) ? m.input_modalities : ['text'];
        return '<li><span class="dot ' + st.cls + '" aria-hidden="true"></span><span class="sr-only">' + st.label + ' · </span>' +
          '<strong>' + esc(m.display_name || m.slug) + '</strong><code>' + esc(m.slug) + '</code>' + copyBtn(m.slug, 'Slug') +
          '<span class="meta">' + fmtContext(m.context_window) + ' ctx' + (mods.includes('image') ? ' · vision' : '') + '</span></li>';
      }).join('') + '</ul>';
    } else if (!enabled) {
      list = emptyHtml('Ollama is disabled', 'Turn it on, save, then Refresh to discover local models.');
    } else if (online) {
      list = emptyHtml('Ollama is running at ' + base + ' but has no models', 'Run `ollama pull llama3.2`, then Refresh.');
    } else {
      list = emptyHtml('Ollama not detected at ' + base, 'Install from ollama.com, run `ollama pull llama3.2`, then Refresh.');
    }
    const statusChip = state.health
      ? '<span class="chip"><span class="dot ' + (online ? 'good' : 'critical') + '" aria-hidden="true"></span>' + (online ? 'Online' : 'Offline') + ' · ' + fmtInt(h.models) + ' models</span>'
      : '';
    return '<section class="card" aria-labelledby="' + id + '-title" data-panel="ollama">' +
      '<div class="card-head"><div><p class="eyebrow">Local models</p><h2 id="' + id + '-title">Ollama</h2></div>' + statusChip + '</div>' +
      '<form class="form-row" data-form="ollama" novalidate>' +
      '<div class="field grow"><label for="' + id + '-url">Base URL</label><input class="input" id="' + id + '-url" name="base_url" type="url" value="' + esc(base) + '" placeholder="' + DEFAULT_OLLAMA_URL + '" required spellcheck="false"></div>' +
      '<label class="switch"><input type="checkbox" name="enabled"' + (enabled ? ' checked' : '') + '><span class="track" aria-hidden="true"></span><span>Enabled</span></label>' +
      '<button class="btn btn-primary btn-sm" type="submit">Save</button>' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="ollama-refresh">Refresh models</button>' +
      '</form>' +
      '<p class="hint" style="margin-top:10px">New Ollama models reach the Codex picker after you restart Codex desktop.</p>' +
      list + '</section>';
  }
  async function ollamaSave(form) {
    const base = form.elements.base_url.value.trim();
    if (!/^https?:\/\/\S+$/.test(base)) { toast('Base URL must start with http:// or https:// — e.g. ' + DEFAULT_OLLAMA_URL, 'error'); form.elements.base_url.focus(); return; }
    const btn = $('button[type="submit"]', form); btn.disabled = true;
    try {
      await api.put('/settings', { ollama: { enabled: form.elements.enabled.checked, base_url: base } });
      toast('Ollama settings saved', 'success');
      await reloadCatalog();
    } catch (e) { reportError(e); }
    finally { btn.disabled = false; }
  }
  async function ollamaRefresh(btn) {
    btn.disabled = true; btn.textContent = 'Refreshing…';
    try {
      const r = await api.post('/ollama/refresh');
      const found = listOf(r, ['models']);
      state.ollamaDiscovered = found;
      toast((r && r.note ? r.note : 'Ollama catalog refreshed') + ' · ' + fmtInt(found.length) + ' model' + (found.length === 1 ? '' : 's') + ' found', 'success', { timeout: 8000 });
      await reloadCatalog();
    } catch (e) { reportError(e); btn.disabled = false; btn.textContent = 'Refresh models'; }
  }
  /** Re-fetch models + settings + health and repaint whichever view is showing. */
  async function reloadCatalog() {
    const [m, s, h] = await Promise.allSettled([api.get('/models'), api.get('/settings'), api.get('/health', { noAuthRedirect: true })]);
    if (m.status === 'fulfilled') state.models = listOf(m.value, MODEL_KEYS);
    if (s.status === 'fulfilled') state.settings = s.value;
    if (h.status === 'fulfilled') { state.health = h.value; state.healthError = null; paintHealth(); }
    const failed = [m, s, h].find(r => r.status === 'rejected');
    if (failed) reportError(failed.reason);
    views[state.view].paint(true);
  }
  /** Handles actions shared by every view that embeds the Ollama panel. Returns true if handled. */
  function sharedAction(action, el) {
    if (action === 'ollama-refresh') { ollamaRefresh(el); return true; }
    if (action === 'retry') { views[state.view].load({ force: true }); return true; }
    return false;
  }

  /** Repaint now, unless the user is interacting inside `root` — then repaint when focus leaves. */
  function paintWhenIdle(root, paintFn) {
    const a = document.activeElement;
    if (a && a !== root && root.contains(a)) {
      if (!root.dataset.deferred) {
        root.dataset.deferred = '1';
        root.addEventListener('focusout', function onOut(e) {
          if (e.relatedTarget && root.contains(e.relatedTarget)) return;
          root.removeEventListener('focusout', onOut);
          delete root.dataset.deferred;
          paintFn();
        });
      }
      return;
    }
    delete root.dataset.deferred;
    paintFn();
  }
  function refocus(id) { const el = id && document.getElementById(id); if (el) el.focus(); }

  /* ================================================================ views */
  const views = {};

  /* ---------- Overview ---------- */
  views.overview = {
    root() { return $('#view-overview'); },
    show() { this.paint(true); this.load({}); },
    async load(opts) {
      const o = state.overview, range = state.range, next = NEXT_RANGE[range];
      if (o.loading && !opts.force) return;
      o.loading = true;
      if (!opts.silent && o.loaded) this.root().classList.add('is-loading');
      const results = await Promise.allSettled([
        api.get('/stats/summary?range=' + range),
        api.get('/stats/timeseries?range=' + range + '&bucket=auto'),
        next ? api.get('/stats/timeseries?range=' + next + '&bucket=auto') : Promise.resolve(null),
        api.get('/requests?limit=10')
      ]);
      o.loading = false;
      this.root().classList.remove('is-loading');
      if (state.range !== range || state.mode !== 'app') return;      // superseded while in flight
      const [s, ts, prev, rec] = results;
      if (s.status === 'fulfilled') o.summary = normSummary(s.value);
      if (ts.status === 'fulfilled') o.rows = listOf(ts.value, TS_KEYS);
      if (prev.status === 'fulfilled') o.prevRows = prev.value ? listOf(prev.value, TS_KEYS) : null;
      if (rec.status === 'fulfilled') o.recent = listOf(rec.value, REQ_KEYS);
      const failed = results.filter(r => r.status === 'rejected');
      const coreOk = s.status === 'fulfilled' && ts.status === 'fulfilled';
      o.error = coreOk || o.loaded ? null : failed[0].reason;
      o.loaded = o.loaded || coreOk;
      o.loadedRange = coreOk ? range : o.loadedRange;
      if (failed.length) reportError(failed[0].reason, !!opts.silent);
      this.paint(false);
    },
    paint(force) {
      const root = this.root(), o = state.overview;
      const draw = () => {
        if (!o.loaded) {
          root.innerHTML = o.error ? errorHtml(o.error) :
            '<div class="tiles">' + skel('sk-tile', 8) + '</div><div class="card">' + skel('sk-line w-40') + skel('sk-block') + '</div>' +
            '<div class="grid-2">' + skel('sk-card') + skel('sk-card') + '</div>';
          return;
        }
        const range = state.range, t = o.summary.totals, cmp = comparison(o.prevRows, range), vs = 'vs prev ' + range;
        const cur = cmp ? cmp.cur : {}, prev = cmp ? cmp.prev : {};
        const pick = k => cmp ? [cur[k], prev[k]] : [null, null];
        const hit = pair => (pair[0] == null ? null : (pair[0].input + pair[0].cached > 0 ? pair[0].cached / (pair[0].input + pair[0].cached) : 0));
        const curHit = cmp ? hit([{ input: cur.input_tokens, cached: cur.cached_tokens }]) : null;
        const prevHit = cmp ? hit([{ input: prev.input_tokens, cached: prev.cached_tokens }]) : null;
        const tiles = [
          tileHtml('Requests', fmtInt(t.requests), deltaHtml(...pick('requests'), true, vs)),
          tileHtml('Fresh tokens', fmtCompact(t.input_tokens), deltaHtml(...pick('input_tokens'), true, vs), fmtInt(t.input_tokens) + ' uncached input tokens'),
          tileHtml('Cached tokens', fmtCompact(t.cached_tokens), deltaHtml(...pick('cached_tokens'), true, vs), fmtInt(t.cached_tokens) + ' cached input tokens'),
          tileHtml('Output tokens', fmtCompact(t.output_tokens), deltaHtml(...pick('output_tokens'), true, vs), fmtInt(t.output_tokens) + ' output tokens'),
          tileHtml('Cache hit', fmtPct(t.cache_hit_ratio), deltaHtml(curHit, prevHit, true, vs, true), 'cached ÷ (fresh + cached) input tokens'),
          tileHtml('Errors', fmtInt(t.errors), deltaHtml(...pick('errors'), false, vs)),
          tileHtml('Active threads', o.summary.active_threads == null ? '—' : fmtInt(o.summary.active_threads), deltaHtml(null, null, true, vs), 'Distinct Codex threads seen in this range'),
          tileHtml('p95 latency', fmtMs(t.latency_p95), deltaHtml(null, null, false, vs))
        ].join('');

        const data = charts.buildBuckets(o.rows, range, Date.now());
        const hasData = data.buckets.some(b => b.total > 0);
        const legend = '<ul class="legend" aria-label="Providers">' + SERIES.map(p => '<li>' + swatch(p) + esc(providerLabel(p)) + '</li>').join('') + '</ul>';
        let plot;
        if (!hasData) plot = '<div class="chart-empty">' + emptyHtml('No requests in the last ' + range, 'Run a Codex turn through the router and this chart fills in.') + '</div>';
        else if (o.mode === 'table') plot = charts.tableHtml(data, range);
        else plot = '<div class="chart-wrap" id="chart-host"></div>';
        const chartCard = '<section class="card" aria-labelledby="chart-title"><div class="card-head"><div><p class="eyebrow">Requests by provider</p>' +
          '<h2 id="chart-title">Last ' + esc(range) + ' · ' + esc(fmtDuration(data.bucket)) + ' buckets</h2></div>' +
          '<fieldset class="seg"><legend>Chart display</legend>' +
          '<label><input type="radio" name="chart-mode" id="chart-mode-chart" value="chart"' + (o.mode === 'chart' ? ' checked' : '') + '><span>Chart</span></label>' +
          '<label><input type="radio" name="chart-mode" id="chart-mode-table" value="table"' + (o.mode === 'table' ? ' checked' : '') + '><span>Table</span></label>' +
          '</fieldset></div>' + legend + plot + '<div class="sr-only" aria-live="polite" id="chart-live"></div></section>';

        const provOrder = SERIES.concat(Object.keys(o.summary.providers).filter(k => !SERIES.includes(k)));
        const provRows = provOrder.filter(p => o.summary.providers[p]).map(p => {
          const b = o.summary.providers[p];
          return '<tr><td>' + providerHtml(p) + '</td><td class="num">' + fmtInt(b.requests) + '</td>' +
            '<td class="num" title="' + fmtInt(b.input_tokens + b.cached_tokens + b.output_tokens) + ' tokens">' + fmtCompact(b.input_tokens + b.cached_tokens + b.output_tokens) + '</td>' +
            '<td class="num">' + fmtInt(b.errors) + '</td><td class="num">' + fmtMs(b.latency_avg) + '</td></tr>';
        }).join('');
        const breakdown = '<section class="card" aria-labelledby="prov-title"><div class="card-head"><div><p class="eyebrow">Breakdown</p><h2 id="prov-title">By provider</h2></div></div>' +
          (provRows ? '<div class="table-scroll"><table class="table"><caption class="sr-only">Requests, tokens, errors and average latency per provider</caption><thead><tr><th scope="col">Provider</th><th scope="col" class="num">Requests</th><th scope="col" class="num">Tokens</th><th scope="col" class="num">Errors</th><th scope="col" class="num">Avg latency</th></tr></thead><tbody>' + provRows + '</tbody></table></div>'
            : emptyHtml('No provider activity in the last ' + range, 'Run a Codex turn through the router to see the split.')) + '</section>';

        const recentRows = (o.recent || []).slice(0, 10).map(r => {
          const ms = toMs(r.ts), ok = String(r.status || '').toLowerCase() !== 'error';
          return '<tr><td class="nowrap"><time datetime="' + esc(new Date(ms).toISOString()) + '" title="' + esc(absTime(ms)) + '">' + esc(relTime(ms)) + '</time></td>' +
            '<td>' + providerHtml(r.provider) + '</td><td><code>' + esc(r.model_served || r.model_requested || '—') + '</code></td>' +
            '<td>' + statusHtml(ok ? 'ok' : 'error', ok ? 'ok' : 'error') + '</td><td class="num">' + fmtMs(r.latency_ms) + '</td></tr>';
        }).join('');
        const recent = '<section class="card" aria-labelledby="recent-title"><div class="card-head"><div><p class="eyebrow">Latest</p><h2 id="recent-title">Recent requests</h2></div></div>' +
          (recentRows ? '<div class="table-scroll"><table class="table"><caption class="sr-only">The ten most recent requests</caption><thead><tr><th scope="col">Time</th><th scope="col">Provider</th><th scope="col">Model</th><th scope="col">Status</th><th scope="col" class="num">Latency</th></tr></thead><tbody>' + recentRows + '</tbody></table></div>'
            : emptyHtml('No requests yet', 'The first Codex turn through the router shows up here.')) +
          '<div class="card-foot"><a href="#/requests">All requests →</a></div></section>';

        root.innerHTML = '<div class="tiles">' + tiles + '</div>' + chartCard + '<div class="grid-2">' + breakdown + recent + '</div>';
        if (hasData && o.mode === 'chart') this.mountChart(data, range);
      };
      if (force) draw(); else paintWhenIdle(root, draw);
    },
    mountChart(data, range) {
      const host = $('#chart-host');
      if (!host) return;
      let lastW = 0;
      const draw = () => {
        const w = Math.floor(host.clientWidth);
        if (!w || w === lastW) return;
        lastW = w;
        charts.render(host, data, range, w);
      };
      draw();
      if (typeof ResizeObserver !== 'undefined') {
        const ro = new ResizeObserver(() => requestAnimationFrame(draw));
        ro.observe(host);
      }
    },
    change(el) {
      if (el.name === 'chart-mode') { state.overview.mode = el.value; this.paint(true); refocus('chart-mode-' + el.value); }
    },
    action(action, el) { sharedAction(action, el); }
  };

  /* ---------- Models ---------- */
  views.models = {
    root() { return $('#view-models'); },
    show() { this.paint(true); this.load({}); },
    async load(opts) {
      const m = state.models_;
      if (m.loading && !opts.force) return;
      m.loading = true;
      const [mod, set, h] = await Promise.allSettled([api.get('/models'), api.get('/settings'), api.get('/health', { noAuthRedirect: true })]);
      m.loading = false;
      if (state.mode !== 'app') return;
      if (mod.status === 'fulfilled') state.models = listOf(mod.value, MODEL_KEYS);
      if (set.status === 'fulfilled') state.settings = set.value;
      if (h.status === 'fulfilled') { state.health = h.value; state.healthError = null; paintHealth(); }
      const failed = [mod, set, h].find(r => r.status === 'rejected');
      m.error = mod.status === 'fulfilled' || m.loaded ? null : failed.reason;
      m.loaded = m.loaded || mod.status === 'fulfilled';
      if (failed) reportError(failed.reason, !!opts.silent);
      this.paint(false);
    },
    paint(force) {
      const root = this.root(), m = state.models_;
      const draw = () => {
        if (!m.loaded) {
          root.innerHTML = m.error ? errorHtml(m.error) : '<div class="card">' + skel('sk-line w-40') + '<div class="model-grid">' + skel('sk-card', 3) + '</div></div>' + skel('sk-card');
          return;
        }
        const reserve = (state.settings && state.settings.reserve) || {};
        const reserveModel = reserve.model || (state.health && state.health.reserve_model) || '';
        const list = state.models || [];
        const groups = MODEL_GROUPS.map(([prov, title]) => {
          const items = list.filter(x => String(x.provider || '').toLowerCase() === prov);
          const cards = items.length ? items.map(x => this.card(x, reserveModel)).join('') :
            (prov === 'ollama' ? '' : emptyHtml('No ' + title + ' models in the catalog', 'Check models.base.json and restart the router.'));
          if (prov === 'ollama' && !items.length) return '';
          return '<section class="model-group" aria-labelledby="group-' + prov + '"><h2 id="group-' + prov + '">' + esc(title) + '<span class="count">' + items.length + '</span></h2><div class="model-grid">' + cards + '</div></section>';
        }).join('');
        const known = MODEL_GROUPS.map(g => g[0]);
        const others = list.filter(x => !known.includes(String(x.provider || '').toLowerCase()));
        const otherGroup = others.length ? '<section class="model-group" aria-labelledby="group-other"><h2 id="group-other">Other<span class="count">' + others.length + '</span></h2><div class="model-grid">' + others.map(x => this.card(x, reserveModel)).join('') + '</div></section>' : '';

        const threads = reserve.threads && typeof reserve.threads === 'object' ? Object.keys(reserve.threads) : [];
        const threadRows = threads.map(th => '<tr><td><code title="' + esc(th) + '">' + esc(shortId(th)) + '</code></td><td>' + esc(modelDisplay(reserve.threads[th])) + ' <span class="sub"><code>' + esc(reserve.threads[th]) + '</code></span></td>' +
          '<td class="num"><button type="button" class="btn btn-danger btn-sm" data-action="delete-thread" data-thread="' + esc(th) + '" aria-label="Remove override for thread ' + esc(shortId(th)) + '">Delete</button></td></tr>').join('');
        const reserveCard = '<section class="card" aria-labelledby="reserve-title" id="reserve"><div class="card-head"><div><p class="eyebrow">Reserve routing</p><h2 id="reserve-title">Codex reserve turns</h2></div></div>' +
          '<p class="lead">When Codex desktop is in reserve mode it sends every turn as <code>gpt-reserve</code>; the router answers those turns with the default below — or a per-thread override — through your own subscription.</p>' +
          '<div class="kv" style="margin:12px 0 16px"><span class="label">Default</span><span><strong>' + esc(reserveModel ? modelDisplay(reserveModel) : 'Router default') + '</strong>' + (reserveModel ? ' <code>' + esc(reserveModel) + '</code>' : '') + '</span></div>' +
          '<h3 style="margin-bottom:8px">Per-thread overrides</h3>' +
          (threadRows ? '<div class="table-scroll"><table class="table"><caption class="sr-only">Threads whose reserve turns use a different model</caption><thead><tr><th scope="col">Thread</th><th scope="col">Model</th><th scope="col" class="num"><span class="sr-only">Actions</span></th></tr></thead><tbody>' + threadRows + '</tbody></table></div>'
            : emptyHtml('No per-thread overrides', 'Every reserve turn uses the default. Overrides are set from inside a Codex chat and appear here.')) + '</section>';

        root.innerHTML = '<div class="model-groups">' + groups + otherGroup + '</div>' + reserveCard + ollamaPanelHtml('models-ollama');
      };
      if (force) draw(); else paintWhenIdle(root, draw);
    },
    card(m, reserveModel) {
      const st = onlineState(m.online);
      const mods = Array.isArray(m.input_modalities) && m.input_modalities.length ? m.input_modalities : ['text'];
      const isReserve = !!m.is_reserve_default || (reserveModel && m.slug === reserveModel);
      const prov = String(m.provider || '').toLowerCase();
      const foot = isReserve ? '<span class="reserve-badge"><span aria-hidden="true">✓</span> Reserve default</span>'
        : (prov === 'openai' ? '<span class="hint">Served by OpenAI directly</span>'
          : '<button type="button" class="btn btn-ghost btn-sm" data-action="set-reserve" data-slug="' + esc(m.slug) + '">Set as reserve default</button>');
      return '<article class="model-card' + (isReserve ? ' is-reserve' : '') + '" aria-label="' + esc(m.display_name || m.slug) + '">' +
        '<div class="model-title"><span class="model-name">' + esc(m.display_name || m.slug) + '</span>' +
        '<span class="online" title="' + esc(st.label) + '"><span class="dot ' + st.cls + '" aria-hidden="true"></span><span class="sr-only">' + esc(st.label) + '</span></span></div>' +
        '<div class="slug"><code>' + esc(m.slug) + '</code>' + copyBtn(m.slug, 'Slug') + '</div>' +
        '<div class="chips">' + mods.map(x => '<span class="chip-mod">' + esc(x) + '</span>').join('') + (m.supports_search_tool ? '<span class="chip-mod">search</span>' : '') + '</div>' +
        '<div class="model-meta"><span>Context <strong>' + esc(fmtContext(m.context_window)) + '</strong></span></div>' +
        '<div class="model-foot">' + foot + '</div></article>';
    },
    async action(action, el) {
      if (sharedAction(action, el)) return;
      if (action === 'set-reserve') {
        const slug = el.dataset.slug;
        el.disabled = true;
        try {
          await api.put('/settings', { reserve: { model: slug } });
          toast('Reserve default set to ' + modelDisplay(slug), 'success');
          await reloadCatalog();
        } catch (e) { reportError(e); el.disabled = false; }
      } else if (action === 'delete-thread') {
        const th = el.dataset.thread;
        el.disabled = true;
        try {
          await api.del('/settings/reserve/threads/' + encodeURIComponent(th));
          toast('Override removed for thread ' + shortId(th), 'success');
          await reloadCatalog();
          refocus('reserve-title');
        } catch (e) { reportError(e); el.disabled = false; }
      }
    },
    submit(form) { if (form.dataset.form === 'ollama') ollamaSave(form); }
  };

  /* ---------- Requests ---------- */
  views.requests = {
    root() { return $('#view-requests'); },
    show() { this.paint(true); this.load({}); },
    async load(opts) {
      const r = state.requests;
      if (r.loading && !opts.force) return;
      // background refresh: don't yank away pages the user has scrolled into
      if (opts.silent && (r.rows.length > PAGE_SIZE || r.expanded.size)) return;
      const more = !!opts.more;
      r.loading = true;
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (r.provider) params.set('provider', r.provider);
      if (r.status) params.set('status', r.status);
      if (more && r.rows.length) params.set('before', String(r.rows[r.rows.length - 1].id));
      try {
        const data = listOf(await api.get('/requests?' + params.toString()), REQ_KEYS);
        if (state.mode !== 'app') return;
        r.rows = more ? r.rows.concat(data) : data;
        r.done = data.length < PAGE_SIZE;
        r.error = null;
        r.loaded = true;
      } catch (e) {
        r.error = r.loaded ? null : e;
        reportError(e, !!opts.silent);
      } finally { r.loading = false; }
      this.paint(more || !!opts.force);
      if (more) refocus(r.done ? 'req-end' : 'req-more');
    },
    ensureFrame() {
      const root = this.root(), r = state.requests;
      if ($('#req-frame', root)) return;
      root.innerHTML = '<div class="card" id="req-frame"><div class="filters" role="group" aria-label="Filters">' +
        '<div class="field"><label for="f-provider">Provider</label><select class="select" id="f-provider" name="provider">' +
        '<option value="">All providers</option>' + SERIES.concat(['router']).map(p => '<option value="' + p + '"' + (r.provider === p ? ' selected' : '') + '>' + esc(providerLabel(p)) + '</option>').join('') + '</select></div>' +
        '<div class="field"><label for="f-status">Status</label><select class="select" id="f-status" name="status">' +
        '<option value="">All statuses</option><option value="ok"' + (r.status === 'ok' ? ' selected' : '') + '>ok</option><option value="error"' + (r.status === 'error' ? ' selected' : '') + '>error</option></select></div>' +
        '<button type="button" class="btn btn-ghost" data-action="reload">Refresh</button></div>' +
        '<div id="req-body"></div><div class="load-more" id="req-foot"></div></div>';
    },
    paint(force) {
      const root = this.root(), r = state.requests;
      const draw = () => {
        this.ensureFrame();
        const body = $('#req-body', root), foot = $('#req-foot', root);
        if (!r.loaded) {
          body.innerHTML = r.error ? errorHtml(r.error) : skel('sk-line', 6);
          foot.innerHTML = '';
          return;
        }
        if (!r.rows.length) {
          body.innerHTML = (r.provider || r.status)
            ? emptyHtml('No requests match these filters', 'Try another provider or status.', { action: 'clear-filters', label: 'Clear filters' })
            : emptyHtml('No requests recorded yet', 'Run a Codex turn through the router and it appears here within 30 seconds.');
          foot.innerHTML = '';
          return;
        }
        const rows = r.rows.map(row => {
          const id = String(row.id), ms = toMs(row.ts);
          const isErr = String(row.status || '').toLowerCase() === 'error';
          const open = r.expanded.has(id);
          const served = row.model_served || row.model_requested || '—';
          const requested = row.model_requested && row.model_requested !== row.model_served ? '<span class="sub">for <code>' + esc(row.model_requested) + '</code></span>' : '';
          const statusCell = isErr
            ? '<button type="button" class="disclose" data-action="toggle-error" data-id="' + esc(id) + '" aria-expanded="' + (open ? 'true' : 'false') + '" aria-controls="req-err-' + esc(id) + '">' + statusHtml('error', 'error') + '<span class="caret" aria-hidden="true">▼</span></button>'
            : statusHtml('ok', 'ok');
          const resumed = row.resumed ? '<span aria-hidden="true">✓</span><span class="sr-only">resumed</span>' : '<span aria-hidden="true">—</span><span class="sr-only">not resumed</span>';
          let html = '<tr data-id="' + esc(id) + '"' + (isErr ? ' class="row-clickable"' : '') + '>' +
            '<td class="nowrap"><time datetime="' + esc(new Date(ms).toISOString()) + '" title="' + esc(absTime(ms)) + '">' + esc(relTime(ms)) + '</time></td>' +
            '<td>' + providerHtml(row.provider) + '</td>' +
            '<td><code>' + esc(served) + '</code>' + requested + '</td>' +
            '<td><span class="kind">' + esc(row.kind || '—') + '</span></td>' +
            '<td>' + statusCell + '</td>' +
            '<td class="num">' + fmtMs(row.latency_ms) + '</td>' +
            '<td class="num">' + fmtCompact(row.input_tokens) + '</td><td class="num">' + fmtCompact(row.cached_tokens) + '</td><td class="num">' + fmtCompact(row.output_tokens) + '</td>' +
            '<td class="num">' + resumed + '</td><td class="num">' + fmtInt(row.tool_calls) + '</td>' +
            '<td><code title="' + esc(row.thread || '') + '">' + esc(shortId(row.thread) || '—') + '</code></td></tr>';
          if (isErr) html += '<tr class="row-detail" id="req-err-' + esc(id) + '"' + (open ? '' : ' hidden') + '><td colspan="12"><p class="eyebrow" style="margin-bottom:6px">Error</p><pre class="error-text">' + esc(row.error || 'No error text recorded') + '</pre></td></tr>';
          return html;
        }).join('');
        body.innerHTML = '<div class="table-scroll"><table class="table"><caption class="sr-only">Request log, newest first</caption><thead><tr>' +
          '<th scope="col">Time</th><th scope="col">Provider</th><th scope="col">Model</th><th scope="col">Kind</th><th scope="col">Status</th><th scope="col" class="num">Latency</th>' +
          '<th scope="col" class="num" title="Fresh input tokens">In</th><th scope="col" class="num" title="Cached input tokens">Cached</th><th scope="col" class="num" title="Output tokens">Out</th>' +
          '<th scope="col" class="num" title="Session resumed">Resumed</th><th scope="col" class="num" title="Tool calls">Tools</th><th scope="col">Thread</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
        foot.innerHTML = r.done
          ? '<span class="hint" id="req-end" tabindex="-1">End of history · ' + fmtInt(r.rows.length) + ' shown</span>'
          : '<button type="button" class="btn btn-ghost" id="req-more" data-action="more"' + (r.loading ? ' disabled' : '') + '>' + (r.loading ? 'Loading…' : 'Load more') + '</button>';
      };
      if (force) draw(); else paintWhenIdle(root, draw);
    },
    change(el) {
      const r = state.requests;
      if (el.name === 'provider') r.provider = el.value;
      else if (el.name === 'status') r.status = el.value;
      else return;
      r.expanded.clear();
      this.load({ force: true });
    },
    action(action, el) {
      const r = state.requests;
      if (sharedAction(action, el)) return;
      if (action === 'more') { el.disabled = true; el.textContent = 'Loading…'; this.load({ more: true, force: true }); }
      else if (action === 'reload') { r.expanded.clear(); this.load({ force: true }); }
      else if (action === 'clear-filters') { r.provider = ''; r.status = ''; $('#req-frame') && $('#req-frame').remove(); this.paint(true); this.load({ force: true }); }
      else if (action === 'toggle-error') { this.toggle(el.dataset.id); refocus(null); const b = $('.disclose[data-id="' + CSS.escape(el.dataset.id) + '"]'); if (b) b.focus(); }
    },
    toggle(id) {
      const r = state.requests;
      if (r.expanded.has(id)) r.expanded.delete(id); else r.expanded.add(id);
      const detail = document.getElementById('req-err-' + id), btn = $('.disclose[data-id="' + CSS.escape(id) + '"]');
      if (detail) detail.hidden = !r.expanded.has(id);
      if (btn) btn.setAttribute('aria-expanded', r.expanded.has(id) ? 'true' : 'false');
    },
    rowClick(tr, e) {
      if (e.target.closest('button, a, code, time')) return;
      this.toggle(tr.dataset.id);
    }
  };

  /* ---------- Usage ---------- */
  views.usage = {
    root() { return $('#view-usage'); },
    show() { this.paint(true); this.load({}); },
    async load(opts) {
      const u = state.usage;
      if (u.loading && !opts.force) return;
      u.loading = true;
      const [us, h] = await Promise.allSettled([api.get('/usage'), api.get('/health', { noAuthRedirect: true })]);
      u.loading = false;
      if (state.mode !== 'app') return;
      if (h.status === 'fulfilled') { state.health = h.value; state.healthError = null; paintHealth(); }
      if (us.status === 'fulfilled') {
        u.windows = listOf(us.value, WINDOW_KEYS);
        u.counts = (us.value && us.value.counts && typeof us.value.counts === 'object') ? us.value.counts
          : (state.health && state.health.counts) || null;
        u.error = null; u.loaded = true;
      } else {
        u.error = u.loaded ? null : us.reason;
        reportError(us.reason, !!opts.silent);
      }
      this.paint(false);
    },
    paint(force) {
      const root = this.root(), u = state.usage;
      const draw = () => {
        if (!u.loaded) {
          root.innerHTML = u.error ? errorHtml(u.error) : '<div class="card panel-dark">' + skel('sk-line w-40') + skel('sk-line') + skel('sk-line') + '</div><div class="card">' + skel('sk-line w-70') + '</div>';
          return;
        }
        const now = Date.now();
        const byProvider = {};
        for (const w of u.windows) {
          const p = String(w.provider || 'claude').toLowerCase();
          (byProvider[p] = byProvider[p] || []).push(w);
        }
        const meters = Object.keys(byProvider).map(p => {
          const items = byProvider[p].map(w => {
            let frac = num(w.utilization);
            if (frac > 1.5) frac = frac / 100;                     // tolerate percent-scaled values
            frac = clamp(frac, 0, 1);
            const pct = Math.round(frac * 100);
            const sev = frac >= 0.9 ? 'serious' : frac >= 0.75 ? 'warning' : 'ok';
            const sevLabel = sev === 'serious' ? 'near limit' : sev === 'warning' ? 'high' : 'ok';
            const resets = toMs(w.resets_at);
            const resetText = resets ? (resets > now ? 'resets in ' + fmtDuration(resets - now) : 'reset ' + relTime(resets)) + ' · ' + (resets - now > 86400e3 ? absTime(resets) : clock(resets)) : 'reset time unknown';
            const reported = toMs(w.ts);
            const label = WINDOW_LABEL[String(w.window || '').toLowerCase()] || titleCase(w.window || 'window');
            return '<div class="meter ' + sev + '"><div class="meter-head"><span class="label">' + esc(label) + '</span>' + statusHtml(sev, sevLabel) + '</div>' +
              '<div class="meter-value">' + pct + '% used</div>' +
              '<div class="meter-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '" aria-label="' + esc(label) + ' utilization"><div class="meter-fill" style="width:' + pct + '%"></div></div>' +
              '<p class="hint">' + esc(resetText) + (reported ? ' · reported ' + esc(relTime(reported)) : '') + '</p></div>';
          }).join('');
          return '<div class="stack"><p class="eyebrow">' + esc(providerLabel(p)) + ' subscription</p><div class="meters">' + items + '</div></div>';
        }).join('<hr style="border:0;border-top:1px solid var(--panel-line);margin:20px 0">');
        const windowsCard = '<section class="card panel-dark" aria-labelledby="usage-title"><div class="card-head"><div><p class="eyebrow">Rate limits</p><h2 id="usage-title">Usage windows</h2></div></div>' +
          (meters || emptyHtml('No Claude usage reported yet', 'It appears after the first Claude turn through the router.')) + '</section>';

        const counts = u.counts || {};
        const keys = Object.keys(COUNTER_LABEL).filter(k => counts[k] != null).concat(Object.keys(counts).filter(k => !COUNTER_LABEL[k]));
        const counters = keys.map(k => '<div class="counter"><p class="eyebrow">' + esc(COUNTER_LABEL[k] || titleCase(k)) + '</p><div class="value">' + fmtInt(counts[k]) + '</div></div>').join('');
        const since = state.health && state.health.uptime_s != null ? ' · up ' + fmtDuration(num(state.health.uptime_s) * 1000) : '';
        const countersCard = '<section class="card" aria-labelledby="counters-title"><div class="card-head"><div><p class="eyebrow">Codex counters</p><h2 id="counters-title">Since router start' + esc(since) + '</h2></div></div>' +
          (counters ? '<div class="counters">' + counters + '</div>' : emptyHtml('No counters yet', 'Counters start at the first request after the router boots.')) + '</section>';
        root.innerHTML = windowsCard + countersCard;
      };
      if (force) draw(); else paintWhenIdle(root, draw);
    },
    action(action, el) { sharedAction(action, el); }
  };

  /* ---------- Settings ---------- */
  views.settings = {
    root() { return $('#view-settings'); },
    show() { this.paint(true); this.load({}); },
    async load(opts) {
      const s = state.settings_;
      if (s.loading && !opts.force) return;
      s.loading = true;
      const [set, mod, h] = await Promise.allSettled([api.get('/settings'), api.get('/models'), api.get('/health', { noAuthRedirect: true })]);
      s.loading = false;
      if (state.mode !== 'app') return;
      if (set.status === 'fulfilled') state.settings = set.value;
      if (mod.status === 'fulfilled') state.models = listOf(mod.value, MODEL_KEYS);
      if (h.status === 'fulfilled') { state.health = h.value; state.healthError = null; paintHealth(); }
      s.error = set.status === 'fulfilled' || s.loaded ? null : set.reason;
      s.loaded = s.loaded || set.status === 'fulfilled';
      const failed = [set, mod, h].find(r => r.status === 'rejected');
      if (failed) reportError(failed.reason, !!opts.silent);
      this.paint(false);
    },
    paint(force) {
      const root = this.root(), s = state.settings_;
      const draw = () => {
        const themeCard = '<section class="card" aria-labelledby="theme-title"><div class="card-head"><div><p class="eyebrow">Appearance</p><h2 id="theme-title">Theme</h2></div></div>' +
          '<div class="radios" role="radiogroup" aria-label="Theme">' + [['light', 'Light'], ['dark', 'Dark'], ['system', 'Auto (system)']].map(([v, l]) =>
            '<label class="radio"><input type="radio" name="theme" value="' + v + '"' + (state.theme === v ? ' checked' : '') + '>' + l + '</label>').join('') + '</div></section>';

        let tokenBody;
        if (s.rotatedToken) {
          tokenBody = '<div class="token-box"><p class="hint">Copy it now — it is shown once. Every other dashboard session is now signed out; this one stays signed in.</p>' +
            '<div class="codeblock"><pre><code>' + esc(s.rotatedToken) + '</code></pre><button type="button" class="btn btn-ghost btn-sm copy-btn-block" data-copy="' + esc(s.rotatedToken) + '" data-what="Token">Copy</button></div>' +
            '<p class="hint">Scripts that read the token file pick up the new value automatically: <code>' + esc(TOKEN_CMD) + '</code></p></div>';
        } else if (s.confirmRotate) {
          tokenBody = '<div class="row"><span>Rotate the dashboard token? Other sessions and any script using the old token stop working immediately.</span>' +
            '<button type="button" class="btn btn-primary btn-sm" data-action="rotate-confirm" id="rotate-confirm">Yes, rotate</button><button type="button" class="btn btn-ghost btn-sm" data-action="rotate-cancel">Cancel</button></div>';
        } else {
          tokenBody = '<div class="row"><p class="lead">Generates a new token, writes it to <code>state/dashboard-token</code> and signs out every other session.</p>' +
            '<button type="button" class="btn btn-ghost" data-action="rotate" id="rotate-btn">Rotate dashboard token</button></div>';
        }
        const tokenCard = '<section class="card" aria-labelledby="token-title"><div class="card-head"><div><p class="eyebrow">Access</p><h2 id="token-title">Dashboard token</h2></div></div>' + tokenBody + '</section>';

        const catalogPath = state.settings && state.settings.catalog_path ? String(state.settings.catalog_path) : '';
        const catalogCard = '<section class="card" aria-labelledby="catalog-title"><div class="card-head"><div><p class="eyebrow">Codex</p><h2 id="catalog-title">Model catalog</h2></div></div>' +
          '<div class="field"><label for="catalog-path">Catalog file (model_catalog_json)</label><div class="row"><input class="input grow" id="catalog-path" readonly value="' + esc(catalogPath || (s.loaded ? 'unknown' : 'loading…')) + '">' + (catalogPath ? copyBtn(catalogPath, 'Catalog path') : '') + '</div></div>' +
          '<p class="hint" style="margin-top:8px">Restart Codex desktop after adding models — Codex reads this file only at app start.</p></section>';

        const sessionCard = '<section class="card" aria-labelledby="session-title"><div class="card-head"><div><p class="eyebrow">Session</p><h2 id="session-title">Sign out</h2></div></div>' +
          '<div class="row"><p class="lead">Clears the dashboard cookie on this browser. The token itself stays valid.</p><button type="button" class="btn btn-ghost" data-action="logout">Sign out</button></div></section>';

        root.innerHTML = themeCard + tokenCard + (s.loaded || s.error ? ollamaPanelHtml('settings-ollama') : '<div class="card">' + skel('sk-line w-40') + skel('sk-line') + '</div>') + catalogCard + sessionCard;
      };
      if (force) draw(); else paintWhenIdle(root, draw);
    },
    change(el) { if (el.name === 'theme') setTheme(el.value); },
    async action(action, el) {
      const s = state.settings_;
      if (sharedAction(action, el)) return;
      if (action === 'rotate') { s.confirmRotate = true; this.paint(true); refocus('rotate-confirm'); }
      else if (action === 'rotate-cancel') { s.confirmRotate = false; this.paint(true); refocus('rotate-btn'); }
      else if (action === 'rotate-confirm') {
        el.disabled = true;
        try {
          const r = await api.post('/auth/rotate');
          s.rotatedToken = r && r.token ? String(r.token) : '';
          s.confirmRotate = false;
          if (!s.rotatedToken) toast('Token rotated but not returned — read it with `' + TOKEN_CMD + '`', 'error');
          else toast('Token rotated — copy it now, it is shown once', 'success', { timeout: 8000 });
          this.paint(true);
          refocus('token-title');
        } catch (e) { reportError(e); el.disabled = false; }
      } else if (action === 'logout') {
        el.disabled = true;
        try { await api.post('/auth/logout', {}, { noAuthRedirect: true }); } catch (e) { /* cookie may already be gone; fall through to the login view */ }
        toast('Signed out', 'success');
        showLogin();
      }
    },
    submit(form) { if (form.dataset.form === 'ollama') ollamaSave(form); }
  };

  /* ---------- API ---------- */
  const ENDPOINTS = [
    { m: 'GET', p: '/api/v1/health', d: 'Liveness: version, uptime, counters, reserve model, Ollama status.', auth: false },
    { m: 'POST', p: '/api/v1/auth/login', d: 'Exchange the dashboard token for a session cookie.', auth: false, body: { token: '$DCR_TOKEN' }, cookie: true },
    { m: 'POST', p: '/api/v1/auth/logout', d: 'Clear the session cookie.', auth: true },
    { m: 'GET', p: '/api/v1/auth/status', d: 'Whether this request is authenticated: {authenticated}.', auth: false },
    { m: 'POST', p: '/api/v1/auth/rotate', d: 'Write a new dashboard token and return it once; other sessions are signed out.', auth: true },
    { m: 'GET', p: '/api/v1/stats/summary?range=24h', d: 'Totals, per-provider and per-model stats for range 1h | 24h | 7d | 30d.', auth: true },
    { m: 'GET', p: '/api/v1/stats/timeseries?range=24h&bucket=auto', d: 'Bucketed requests, errors and tokens per provider (auto: 1h→1m, 24h→15m, 7d→1h, 30d→6h).', auth: true },
    { m: 'GET', p: '/api/v1/requests?limit=50&before=<id>&provider=&status=', d: 'Request log, newest first; page backwards with before=<last id>.', auth: true, curlPath: '/api/v1/requests?limit=50' },
    { m: 'GET', p: '/api/v1/usage', d: 'Latest Claude rate-limit windows per provider plus Codex counters.', auth: true },
    { m: 'GET', p: '/api/v1/models', d: 'Catalog as Codex sees it: slug, provider, context window, modalities, reserve flag, online.', auth: true },
    { m: 'GET', p: '/api/v1/settings', d: 'Reserve default + per-thread overrides, Ollama settings, catalog path.', auth: true },
    { m: 'PUT', p: '/api/v1/settings', d: 'Partial update of the same shape; validated, persisted, logged as an event.', auth: true, body: { reserve: { model: 'claude-max-opus-48' } } },
    { m: 'POST', p: '/api/v1/ollama/refresh', d: 'Re-discover Ollama models and regenerate state/models.json.', auth: true },
    { m: 'DELETE', p: '/api/v1/settings/reserve/threads/{thread}', d: 'Remove one per-thread reserve override.', auth: true, curlPath: '/api/v1/settings/reserve/threads/THREAD_ID' },
    { m: 'GET', p: '/dashboard/', d: 'This dashboard (static files, no directory listing).', auth: false }
  ];
  function curlFor(e) {
    const url = ROUTER_ORIGIN + (e.curlPath || e.p);
    const parts = ['curl -s'];
    if (e.m !== 'GET') parts.push('-X ' + e.m);
    if (e.auth) parts.push('-H "Authorization: Bearer $DCR_TOKEN"');
    if (e.body) parts.push('-H "Content-Type: application/json"', "-d '" + JSON.stringify(e.body) + "'");
    if (e.cookie) parts.push('-c cookies.txt');
    parts.push('"' + url + '"');
    return parts.join(' \\\n  ');
  }
  views.api = {
    root() { return $('#view-api'); },
    show() { this.paint(true); },
    load() { /* static content */ },
    paint() {
      const root = this.root();
      if (root.dataset.painted) return;
      root.dataset.painted = '1';
      const rows = ENDPOINTS.map((e, i) => {
        const curl = curlFor(e);
        return '<div class="endpoint"><span class="method method-' + e.m.toLowerCase() + '">' + e.m + '</span>' +
          '<div><div class="path">' + esc(e.p) + '</div><div class="purpose">' + esc(e.d) + '</div></div>' +
          '<span class="badge' + (e.auth ? ' badge-token' : '') + '">' + (e.auth ? 'Token' : 'Public') + '</span>' +
          '<details><summary>curl example</summary><div class="codeblock"><pre><code id="curl-' + i + '">' + esc(curl) + '</code></pre>' +
          '<button type="button" class="btn btn-ghost btn-sm copy-btn-block" data-copy="' + esc(curl) + '" data-what="curl command">Copy</button></div></details></div>';
      }).join('');
      const exportCmd = 'export DCR_TOKEN=$(' + TOKEN_CMD + ')';
      root.innerHTML = '<section class="card" aria-labelledby="api-intro-title"><div class="card-head"><div><p class="eyebrow">Reference</p><h2 id="api-intro-title">Using the API</h2></div>' +
        '<a class="chip" href="https://github.com/darshjme/darshj-codex-router/blob/main/docs/API.md" target="_blank" rel="noopener">docs/API.md ↗</a></div>' +
        '<p class="lead">Every endpoint lives under <code>' + ROUTER_ORIGIN + '/api/v1</code>, on loopback only. Token endpoints accept <code>Authorization: Bearer &lt;token&gt;</code> or the dashboard cookie. Errors are <code>{"error": {"code", "message"}}</code>.</p>' +
        '<p class="hint" style="margin-top:10px">Read the token into your shell first:</p>' +
        '<div class="codeblock"><pre><code>' + esc(exportCmd) + '</code></pre><button type="button" class="btn btn-ghost btn-sm copy-btn-block" data-copy="' + esc(exportCmd) + '" data-what="Command">Copy</button></div>' +
        '</section><section class="card" aria-labelledby="api-list-title"><div class="card-head"><div><p class="eyebrow">Endpoints</p><h2 id="api-list-title">' + ENDPOINTS.length + ' routes</h2></div></div>' + rows + '</section>';
    },
    action(action, el) { sharedAction(action, el); }
  };

  /* ================================================================ app: theme, health, auth, routing, refresh */
  function applyTheme() {
    const root = document.documentElement;
    if (state.theme === 'light' || state.theme === 'dark') root.setAttribute('data-theme', state.theme);
    else root.removeAttribute('data-theme');
    const label = state.theme === 'system' ? 'Auto' : state.theme === 'light' ? 'Light' : 'Dark';
    const next = THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length];
    const btn = $('#theme-toggle');
    if (btn) {
      $('#theme-label').textContent = label;
      $('#theme-icon').innerHTML = ICON_THEME[state.theme];
      btn.setAttribute('aria-label', 'Theme: ' + label + '. Switch to ' + (next === 'system' ? 'Auto' : next));
    }
    $$('input[name="theme"]').forEach(r => { r.checked = r.value === state.theme; });
  }
  function setTheme(mode) {
    if (!THEMES.includes(mode)) return;
    state.theme = mode;
    storage('dcr-theme', mode);
    applyTheme();
    // series colours are CSS variables; redraw the chart so the SVG picks up the dark/light palette
    if (state.view === 'overview' && state.overview.loaded) views.overview.paint(true);
  }
  function setRange(range) {
    if (!RANGES.includes(range) || range === state.range) return;
    state.range = range;
    storage('dcr-range', range);
    $$('input[name="range"]').forEach(r => { r.checked = r.value === range; });
    if (state.view === 'overview') views.overview.load({ force: true });
  }

  async function loadHealth(silent) {
    try {
      state.health = await api.get('/health', { noAuthRedirect: true });
      state.healthError = null;
    } catch (e) {
      state.healthError = e;
      if (!silent) reportError(e, true);
    }
    paintHealth();
  }
  function paintHealth() {
    const h = state.health, dot = $('#health-dot'), text = $('#health-text'), up = $('#health-uptime'), chip = $('#reserve-chip-model');
    if (!dot) return;
    if (state.healthError) {
      dot.className = 'dot critical';
      text.textContent = 'Router unreachable' + (h && h.version ? ' · v' + h.version : '');
      up.textContent = '';
      return;
    }
    if (!h) return;
    const ok = String(h.status || '').toLowerCase() === 'ok';
    dot.className = 'dot ' + (ok ? 'good' : 'warning');
    text.textContent = (ok ? 'Healthy' : titleCase(h.status || 'degraded')) + (h.version ? ' · v' + h.version : '');
    up.textContent = h.uptime_s != null ? 'up ' + fmtDuration(num(h.uptime_s) * 1000) + ' · ' + fmtInt(h.counts && (num(h.counts.claude) + num(h.counts.grok) + num(h.counts.openai) + num(h.counts.ollama))) + ' turns' : '';
    if (chip) chip.textContent = h.reserve_model ? modelDisplay(h.reserve_model) : 'router default';
  }

  function setMode(mode) {
    state.mode = mode;
    $('#app').dataset.mode = mode;
    $('#rail').hidden = mode !== 'app';
    $('#topbar').hidden = mode !== 'app';
    $('#view-login').hidden = mode !== 'login';
    if (mode !== 'app') $$('.view[data-view]:not([data-view="login"])').forEach(s => { s.hidden = true; });
  }
  function showLogin(err) {
    stopTimer();
    setMode('login');
    // forget everything from the previous session
    state.models = null; state.settings = null; state.ollamaDiscovered = null;
    state.overview = { loaded: false, loading: false, error: null, summary: null, rows: [], prevRows: null, recent: [], mode: 'chart' };
    state.requests = { loaded: false, loading: false, error: null, rows: [], provider: '', status: '', done: false, expanded: new Set() };
    state.models_ = { loaded: false, loading: false, error: null };
    state.usage = { loaded: false, loading: false, error: null, windows: [], counts: null };
    state.settings_ = { loaded: false, loading: false, error: null, rotatedToken: null, confirmRotate: false };
    $$('.view[data-view]:not([data-view="login"])').forEach(s => { s.innerHTML = ''; delete s.dataset.painted; });
    document.title = "Sign in · Darshj's Codex Router";
    const errEl = $('#login-error');
    if (err && err.status !== 401) { errEl.innerHTML = inlineCode(err.message); errEl.hidden = false; }
    else errEl.hidden = true;
    const input = $('#token');
    if (input) input.focus();
  }
  async function enterApp() {
    setMode('app');
    applyTheme();
    $$('input[name="range"]').forEach(r => { r.checked = r.value === state.range; });
    loadHealth(true);
    if (!location.hash) history.replaceState(null, '', '#/overview');
    navigate();
    startTimer();
  }
  async function checkAuth() {
    try {
      const s = await api.get('/auth/status', { noAuthRedirect: true });
      if (s && s.authenticated) enterApp(); else showLogin();
    } catch (e) { showLogin(e); }
  }
  async function login(e) {
    e.preventDefault();
    const input = $('#token'), errEl = $('#login-error'), btn = $('#login-submit');
    const token = input.value.trim();
    const fail = msg => { errEl.innerHTML = inlineCode(msg); errEl.hidden = false; input.focus(); };
    errEl.hidden = true;
    if (!token) { fail('Paste the token first — get it with `' + TOKEN_CMD + '`'); return; }
    btn.disabled = true;
    try {
      await api.post('/auth/login', { token }, { noAuthRedirect: true });
      input.value = '';
      enterApp();
    } catch (err) {
      if (err.status === 401) fail('That token did not match — re-read it with `' + TOKEN_CMD + '` and paste the whole line');
      else fail(err.message);
    } finally { btn.disabled = false; }
  }

  function parseHash() {
    const m = /^#\/([a-z]+)/.exec(location.hash || '');
    return m && VIEWS[m[1]] ? m[1] : 'overview';
  }
  function navigate() {
    if (state.mode !== 'app') return;
    const view = parseHash();
    const prev = state.view;
    state.view = view;
    if (prev === 'settings' && view !== 'settings') { state.settings_.rotatedToken = null; state.settings_.confirmRotate = false; }
    $$('.nav a').forEach(a => { if (a.dataset.view === view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    $$('.view[data-view]').forEach(s => { s.hidden = s.dataset.view !== view; });
    $('#page-title').textContent = VIEWS[view];
    document.title = VIEWS[view] + " · Darshj's Codex Router";
    $('#range-picker').hidden = view !== 'overview';
    views[view].show();
    if (prev !== view) $('#main').focus({ preventScroll: true });
  }

  function refresh(silent) {
    if (state.mode !== 'app') return;
    loadHealth(true);
    views[state.view].load({ silent: !!silent });
  }
  function startTimer() {
    stopTimer();
    state.refreshTimer = setInterval(() => { if (document.visibilityState === 'visible') refresh(true); }, REFRESH_MS);
  }
  function stopTimer() { if (state.refreshTimer) { clearInterval(state.refreshTimer); state.refreshTimer = 0; } }

  function bindGlobal() {
    window.addEventListener('hashchange', navigate);
    document.addEventListener('visibilitychange', () => {
      if (state.mode !== 'app') return;
      if (document.visibilityState === 'visible') { refresh(true); startTimer(); } else stopTimer();
    });
    $('#login-form').addEventListener('submit', login);
    $('#theme-toggle').addEventListener('click', () => setTheme(THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length]));
    $('#range-picker').addEventListener('change', e => { if (e.target.name === 'range') setRange(e.target.value); });

    document.addEventListener('click', e => {
      const copy = e.target.closest('[data-copy]');
      if (copy) { copyText(copy.dataset.copy, copy.dataset.what || 'Text'); return; }
      const main = $('#main');
      if (!main.contains(e.target) || state.mode !== 'app') return;
      const actionEl = e.target.closest('[data-action]');
      const view = views[state.view];
      if (actionEl && view.action) { view.action(actionEl.dataset.action, actionEl, e); return; }
      const row = e.target.closest('tr.row-clickable');
      if (row && view.rowClick) view.rowClick(row, e);
    });
    $('#main').addEventListener('change', e => {
      const view = views[state.view];
      if (state.mode === 'app' && view.change && e.target.name) view.change(e.target);
    });
    $('#main').addEventListener('submit', e => {
      const view = views[state.view];
      if (state.mode !== 'app' || !e.target.dataset.form) return;
      e.preventDefault();
      if (view.submit) view.submit(e.target, e);
    });
  }

  const app = {
    init() {
      applyTheme();
      bindGlobal();
      checkAuth();
    },
    onUnauthorized() {
      if (state.mode === 'app') {
        toast('Session expired — sign in again with your dashboard token', 'error');
        showLogin();
      }
    }
  };

  return { init: app.init, navigate, refresh: () => refresh(false) };
})();

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', DCR.init);
else DCR.init();
