"""Text operations for the memory bus.

Stdlib-only by contract so the same code runs on the Mac client (Python 3.9,
no deps) and inside the Kali service. Vectors are plain lists of floats
(dimension 384). numpy is used opportunistically for MMR when it happens to be
importable (it is on Kali via torch, and on macOS system Python); every path
has a pure-Python fallback with identical selection semantics.

Recall candidate pools are <= 128, so the pure-Python path is sub-millisecond
there. Compress can see thousands of units (one per line of tool output), so
its MMR runs incrementally over a bounded, salience-pruned pool; see
MAX_COMPRESS_UNITS / _mmr_pool_size. (2026-09-16: the original O(n^3 * d)
MMR hung the service for >300 s on a 3,000-line input.)
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

try:  # optional accelerator; never required
    import numpy as _np
except Exception:  # pragma: no cover - depends on the host
    _np = None

Vector = List[float]

# compress: units above this are coalesced (consecutive lines merged) before embedding
MAX_COMPRESS_UNITS = 4096
# compress: MMR runs over at most this many salience-ranked candidates (plus forced head/tail)
MIN_MMR_POOL = 256
# mmr: below this many candidates the pure-Python path is used even when numpy is present
_NUMPY_MIN_N = 64

# ---------------------------------------------------------------- tokens ----

def est_tokens(text: str) -> int:
    """tiktoken-free estimate: ~4 chars per token, never below 1 for non-empty."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def truncate_to_tokens(text: str, tokens: int) -> str:
    """Cut text to roughly `tokens`, preferring a whitespace boundary."""
    limit = max(0, tokens * 4)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + " …"


# ------------------------------------------------------------- hashing ----

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", text)).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


# ------------------------------------------------------------ chunking ----

_PARA = re.compile(r"\n[ \t]*\n+")
_HEADING = re.compile(r"^(#{1,6} |\* |- |\d+\. )")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENT.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _split_oversized(paragraph: str, hard_max_tokens: int) -> List[str]:
    """Split a single paragraph that exceeds the hard cap on sentence boundaries."""
    if est_tokens(paragraph) <= hard_max_tokens:
        return [paragraph]
    out: List[str] = []
    buf = ""
    for sent in split_sentences(paragraph) or [paragraph]:
        if est_tokens(sent) > hard_max_tokens:
            # pathological: no sentence boundaries; hard-cut by chars
            if buf:
                out.append(buf)
                buf = ""
            step = hard_max_tokens * 4
            for i in range(0, len(sent), step):
                out.append(sent[i:i + step])
            continue
        candidate = (buf + " " + sent).strip() if buf else sent
        if est_tokens(candidate) > hard_max_tokens and buf:
            out.append(buf)
            buf = sent
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def chunk_text(text: str, chunk_tokens: int = 400, hard_max_tokens: int = 1500) -> List[str]:
    """Greedy paragraph packing.

    Paragraphs are merged while the running chunk stays <= chunk_tokens.
    A single paragraph larger than chunk_tokens becomes its own chunk (the
    caller compresses chunks > chunk_tokens); anything above hard_max_tokens
    is split on sentences so no chunk is unbounded.
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    paragraphs: List[str] = []
    for para in _PARA.split(text):
        para = para.strip()
        if not para:
            continue
        paragraphs.extend(_split_oversized(para, hard_max_tokens))
    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        starts_section = bool(_HEADING.match(para)) and para.startswith("#")
        candidate = (buf + "\n\n" + para) if buf else para
        if buf and (est_tokens(candidate) > chunk_tokens or (starts_section and est_tokens(buf) > chunk_tokens // 2)):
            chunks.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


# ------------------------------------------------------------- vectors ----

def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def norm(a: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in a)) or 1.0


def unit(a: Sequence[float]) -> Vector:
    n = norm(a)
    return [x / n for x in a]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return dot(a, b) / (norm(a) * norm(b))


def centroid(vectors: Sequence[Sequence[float]]) -> Vector:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            acc[i] += v[i]
    return unit([x / len(vectors) for x in acc])


# ----------------------------------------------------------------- MMR ----

def mmr(query_vec: Sequence[float], cand_vecs: Sequence[Sequence[float]], k: int,
        lambda_: float = 0.7, relevance: Optional[Sequence[float]] = None,
        use_numpy: bool = True) -> List[int]:
    """Maximal Marginal Relevance. Returns indices into cand_vecs in selection order.

    relevance[i] may be supplied (e.g. Qdrant scores) instead of recomputing
    cosine(query, cand). Vectors are assumed L2-normalized (dot == cosine).

    Incremental: each candidate keeps its max similarity to the selected set,
    updated once per selection, so the cost is O(k * n * d) instead of the
    O(k^2 * n * d) of recomputing the max on every step. Redundancy is 0.0
    while nothing is selected (same as the original). Ties go to the lowest
    index on both paths.
    """
    n = len(cand_vecs)
    if n == 0 or k <= 0:
        return []
    k = min(k, n)
    rel = list(relevance) if relevance is not None else [dot(query_vec, v) for v in cand_vecs]
    if use_numpy and _np is not None and n >= _NUMPY_MIN_N:
        return _mmr_numpy(rel, cand_vecs, k, lambda_)
    return _mmr_pure(rel, cand_vecs, k, lambda_)


def _mmr_pure(rel: Sequence[float], cand_vecs: Sequence[Sequence[float]], k: int, lambda_: float) -> List[int]:
    n = len(cand_vecs)
    max_sim: List[Optional[float]] = [None] * n  # None == nothing selected yet -> redundancy 0.0
    selected: List[int] = []
    remaining = list(range(n))
    while remaining and len(selected) < k:
        best_i, best_score = -1, -1e9
        for i in remaining:
            redundancy = max_sim[i] if max_sim[i] is not None else 0.0
            score = lambda_ * rel[i] - (1.0 - lambda_) * redundancy
            if score > best_score:
                best_i, best_score = i, score
        selected.append(best_i)
        remaining.remove(best_i)
        if remaining and len(selected) < k:
            v = cand_vecs[best_i]
            for i in remaining:
                d = dot(cand_vecs[i], v)
                m = max_sim[i]
                if m is None or d > m:
                    max_sim[i] = d
    return selected


def _mmr_numpy(rel: Sequence[float], cand_vecs: Sequence[Sequence[float]], k: int, lambda_: float) -> List[int]:
    V = _np.asarray(cand_vecs, dtype=_np.float64)
    r = _np.asarray(rel, dtype=_np.float64)
    n = V.shape[0]
    max_sim = _np.full(n, -_np.inf)
    alive = _np.ones(n, dtype=bool)
    selected: List[int] = []
    for _ in range(k):
        redundancy = _np.where(_np.isneginf(max_sim), 0.0, max_sim)
        score = lambda_ * r - (1.0 - lambda_) * redundancy
        score[~alive] = -_np.inf
        i = int(score.argmax())  # first max wins -> lowest index on ties, like the pure path
        selected.append(i)
        alive[i] = False
        if len(selected) < k:
            max_sim = _np.maximum(max_sim, V @ V[i])
    return selected


# ------------------------------------------------------------- packing ----

def pack_hits(hits: Sequence[dict], max_tokens: int, min_tail_tokens: int = 40,
              render: Optional[Callable[[dict], str]] = None) -> Tuple[List[dict], str, int]:
    """Pack ordered hits into <= max_tokens.

    Returns (kept_hits, packed_text, tokens). A hit that does not fit is
    truncated when at least min_tail_tokens of budget remain, otherwise packing
    stops. Each kept hit gets a 'packed_text' key with what was emitted.
    """
    render = render or (lambda h: "- [%s] %s" % (h.get("source", "?"), h.get("text", "")))
    kept: List[dict] = []
    lines: List[str] = []
    used = 0
    for h in hits:
        line = render(h)
        cost = est_tokens(line) + 1  # newline
        if used + cost <= max_tokens:
            kept.append(dict(h, packed_text=line))
            lines.append(line)
            used += cost
            continue
        remaining = max_tokens - used
        if remaining >= min_tail_tokens:
            line = truncate_to_tokens(line, remaining - 2)  # -2: newline + ellipsis rounding
            kept.append(dict(h, packed_text=line, truncated=True))
            lines.append(line)
            used += est_tokens(line) + 1
        break
    return kept, "\n".join(lines), used


# ------------------------------------------------- extractive compress ----

_LINE = re.compile(r"\n")


def split_units(text: str, mode: str) -> List[str]:
    if mode == "tool_output":
        return [ln.rstrip() for ln in _LINE.split(text) if ln.strip()]
    units: List[str] = []
    for para in _PARA.split(text.strip()):
        units.extend(split_sentences(para))
    return units


def extractive_compress(text: str, embed: Callable[[List[str]], List[Vector]], target_tokens: int,
                        query_vec: Optional[Vector] = None, mode: str = "prose",
                        keep_head: int = 3, keep_tail: int = 3, lambda_: float = 0.8) -> dict:
    """Salience-based extractive compression.

    1. Split into units (lines for tool output, sentences for prose).
    2. Embed units; reference = centroid (blended 50/50 with query_vec if given).
    3. Force-keep head/tail units for tool_output (exit codes, tracebacks live there).
    4. Greedily add units by MMR (salience vs. redundancy) until target_tokens.
    5. Emit kept units in original order with '[…]' gap markers.
    """
    tokens_in = est_tokens(text)
    if tokens_in <= target_tokens:
        return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_in, "kept": None, "total": None, "changed": False}
    units = split_units(text, mode)
    if not units:
        return {"text": truncate_to_tokens(text, target_tokens), "tokens_in": tokens_in,
                "tokens_out": min(tokens_in, target_tokens), "kept": 0, "total": 0, "changed": True}
    joiner = "\n" if mode == "tool_output" else " "
    units = _coalesce_units(units, MAX_COMPRESS_UNITS, joiner)
    if mode != "tool_output":
        keep_head, keep_tail = (1, 0)
    forced: List[int] = []
    if len(units) > keep_head + keep_tail:
        forced = list(range(keep_head)) + list(range(len(units) - keep_tail, len(units)))
    else:
        forced = list(range(len(units)))
    vecs = [unit(v) for v in embed(units)]
    ref = centroid(vecs)
    if query_vec:
        q = unit(query_vec)
        ref = unit([0.5 * a + 0.5 * b for a, b in zip(ref, q)])
    salience = [dot(v, ref) for v in vecs]
    # position prior: earlier lines slightly favoured (errors/first results)
    n = len(units)
    salience = [s + 0.02 * (1.0 - i / max(1, n - 1)) for i, s in enumerate(salience)]
    budget = target_tokens
    kept = set()
    for i in forced:
        cost = est_tokens(units[i]) + 1
        if budget - cost >= 0:
            kept.add(i)
            budget -= cost
    # MMR only over the salience-ranked pool that can plausibly fit the budget
    pool = _mmr_pool(salience, units, target_tokens, forced)
    order = [pool[j] for j in mmr(ref, [vecs[i] for i in pool], k=len(pool), lambda_=lambda_,
                                  relevance=[salience[i] for i in pool])]
    for i in order:
        if i in kept:
            continue
        cost = est_tokens(units[i]) + 1
        if cost > budget:
            if budget >= 24:
                units[i] = truncate_to_tokens(units[i], budget - 1)
                kept.add(i)
                budget = 0
            continue
        kept.add(i)
        budget -= cost
        if budget <= 0:
            break
    out_lines: List[str] = []
    last = -1
    for i in sorted(kept):
        if last >= 0 and i != last + 1:
            out_lines.append("[…]")
        out_lines.append(units[i])
        last = i
    if last < n - 1 and n - 1 not in kept:
        out_lines.append("[…]")
    out = joiner.join(out_lines)
    return {"text": out, "tokens_in": tokens_in, "tokens_out": est_tokens(out), "kept": len(kept), "total": n, "changed": True}


def _coalesce_units(units: List[str], max_units: int, joiner: str) -> List[str]:
    """Merge consecutive units so len(result) <= max_units. Order and content are preserved."""
    n = len(units)
    if n <= max_units:
        return units
    group = math.ceil(n / max_units)
    return [joiner.join(units[i:i + group]) for i in range(0, n, group)]


def _mmr_pool(salience: Sequence[float], units: Sequence[str], target_tokens: int, forced: Sequence[int]) -> List[int]:
    """Indices MMR should rank: forced head/tail plus the top-M units by salience.

    M is a few times the number of units the budget could hold (at least
    MIN_MMR_POOL), so a 3,000-line input at target 300 ranks ~400 candidates
    instead of all 3,000. Small inputs are returned whole (behaviour unchanged).
    """
    n = len(units)
    if n <= MIN_MMR_POOL:
        return list(range(n))
    mean_cost = max(1.0, sum(est_tokens(u) + 1 for u in units) / n)
    m = min(n, max(MIN_MMR_POOL, int(4 * math.ceil(target_tokens / mean_cost))))
    ranked = sorted(range(n), key=lambda i: -salience[i])
    pool = set(forced)
    for i in ranked:
        if len(pool) >= m + len(forced):
            break
        pool.add(i)
    return sorted(pool)


# ------------------------------------------------------------ scrubbing ----

_SECRET = re.compile(r"(?i)(password|passwd|secret|api.?key|access.?token|bearer\s|credential|private.?key|sk-[a-z0-9]{8}|gh[pousr]_[A-Za-z0-9]{10}|xox[baprs]-)")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_+/=\-]{56,}")


def scrub(text: str) -> str:
    """Drop lines that look like credentials. Same policy as agent-common-memory/sync.py."""
    out = []
    for line in text.splitlines():
        if _SECRET.search(line) or _LONG_TOKEN.search(line):
            out.append("[sensitive line withheld]")
        else:
            out.append(line)
    return "\n".join(out)
