#!/usr/bin/env python3
"""Mac -> Kali memory-bus sync (launchd, every 30 min). Stdlib only, Python 3.9.

Write-side of record stays the reviewed mohini-memory store (memory.sqlite3). This script only
READS and pushes changed documents to POST /ingest/batch:

  1. mohini-memory current revisions   (memory.py export)      source=mohini-memory:<id>   agent=codex
  2. mohini-memory INDEX.md                                    source=mohini-memory:INDEX  agent=codex
  3. Claude Code auto-memory *.md      (~/.claude/projects/<home-as-dashes>/memory)  source=claude-memory:<file> agent=claude
  4. agent-common-memory CURRENT.md + sources/{alienware,kali}/**.md               source=common:<label>:<path>

State: ~/.local/share/memory-bus/sync-state.json maps source -> sha256 of the last pushed text, so an
unchanged note costs nothing; the server additionally dedups by chunk hash. Secrets are scrubbed with
the same policy as agent-common-memory/sync.py before anything leaves the Mac. If the bus is unreachable
the run logs one line and exits 0 (launchd keeps the schedule).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOME = pathlib.Path.home()
BASE = os.environ.get("MEMORY_BUS_URL", "http://127.0.0.1:8791").rstrip("/")
TOKEN = os.environ.get("MEMORY_BUS_TOKEN", "")
STATE_DIR = HOME / ".local/share/memory-bus"
STATE = STATE_DIR / "sync-state.json"
MEMORY_PY = HOME / ".codex/skills/mohini-memory/scripts/memory.py"
MOHINI_ROOT = pathlib.Path(os.environ.get("MOHINI_MEMORY_HOME", str(HOME / ".local/share/mohini-memory")))
CLAUDE_MEM = pathlib.Path(os.environ.get("CLAUDE_MEMORY_DIR", str(HOME / ".claude/projects" / str(HOME).replace("/", "-") / "memory")))
COMMON = HOME / ".local/share/agent-common-memory"
MAX_DOC_CHARS = 60_000
BATCH = 40

SECRET = re.compile(r"(?i)(password|passwd|secret|api.?key|access.?token|bearer\s|credential|private.?key|sk-[a-z0-9]{8}|gh[pousr]_[A-Za-z0-9]{10}|xox[baprs]-)")
LONG = re.compile(r"[A-Za-z0-9_+/=\-]{56,}")


def scrub(text: str) -> str:
    return "\n".join("[sensitive line withheld]" if SECRET.search(l) or LONG.search(l) else l for l in text.splitlines())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg, flush=True)


def post(path: str, body: dict, timeout: float = 240):
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    req = Request(BASE + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ------------------------------------------------------------------ sources

def mohini_records():
    if not MEMORY_PY.exists() or not (MOHINI_ROOT / "memory.sqlite3").exists():
        return
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "export.json"
        run = subprocess.run([sys.executable, str(MEMORY_PY), "export", "--out", str(out)], capture_output=True, text=True, timeout=120)
        if run.returncode != 0:
            log("mohini export failed: " + run.stderr.strip()[:200])
            return
        data = json.loads(out.read_text())
    latest = {}
    for r in data.get("revisions", []):
        if r.get("mode") == "current" and r.get("revision", 0) >= latest.get(r["id"], {}).get("revision", 0):
            latest[r["id"]] = r
    for rid, r in sorted(latest.items()):
        lines = ["# " + r.get("title", rid), "id: %s  tier: %s  revision: %s  recorded: %s" % (rid, r.get("tier"), r.get("revision"), r.get("recorded_utc", "")[:10])]
        if r.get("tags"):
            lines.append("tags: " + ", ".join(r["tags"]))
        lines.append("")
        lines.append(r.get("content", ""))
        cp = r.get("checkpoint") or {}
        if cp:
            lines.append("")
            lines.append("Checkpoint state: %s. Objective: %s" % (cp.get("state"), cp.get("objective", "")))
            for res in (cp.get("results") or [])[-3:]:
                lines.append("- result [%s]: %s" % (res.get("status"), res.get("summary")))
            for nxt in (cp.get("next_actions") or [])[:4]:
                lines.append("- next: %s" % (nxt if isinstance(nxt, str) else json.dumps(nxt, ensure_ascii=False)))
            for art in (cp.get("artifacts") or [])[:4]:
                lines.append("- artifact: %s (%s)" % (art.get("path"), art.get("role", "")))
        for s in (r.get("sources") or [])[:5]:
            lines.append("- source: %s" % s.get("ref"))
        ts = None
        try:
            ts = int(time.mktime(time.strptime(r.get("recorded_utc", "")[:19], "%Y-%m-%dT%H:%M:%S")))
        except (ValueError, TypeError):
            pass
        yield {"text": "\n".join(lines), "source": "mohini-memory:" + rid, "agent": "codex",
               "tags": ["mohini-memory", "tier:" + str(r.get("tier"))] + list(r.get("tags") or [])[:8], "ts": ts, "doc": rid}
    idx = MOHINI_ROOT / "INDEX.md"
    if idx.exists():
        yield {"text": idx.read_text(errors="replace"), "source": "mohini-memory:INDEX", "agent": "codex", "tags": ["mohini-memory", "index"], "doc": "INDEX.md"}


def md_files(root: pathlib.Path, label: str, agent: str, tags):
    if not root.exists():
        return
    for p in sorted(root.rglob("*.md")):
        if p.name.startswith(".") or p.stat().st_size == 0:
            continue
        rel = p.relative_to(root).as_posix()
        yield {"text": p.read_text(errors="replace"), "source": "%s:%s" % (label, rel), "agent": agent,
               "tags": list(tags) + [p.stem[:60]], "ts": int(p.stat().st_mtime), "doc": rel}


def all_docs():
    yield from mohini_records()
    yield from md_files(CLAUDE_MEM, "claude-memory", "claude", ["claude-memory"])
    cur = COMMON / "CURRENT.md"
    if cur.exists():
        yield {"text": cur.read_text(errors="replace"), "source": "common:CURRENT", "agent": "shared", "tags": ["common", "current"], "doc": "CURRENT.md"}
    for label, agent in (("alienware", "claude-alienware"), ("kali", "hermes")):
        yield from md_files(COMMON / "sources" / label, "common:" + label, agent, ["common", label])


# --------------------------------------------------------------------- main

def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
    except ValueError:
        state = {}
    try:
        with urlopen(BASE + "/health", timeout=5) as r:
            h = json.loads(r.read().decode())
        if not h.get("ok"):
            log("bus unhealthy: %s" % json.dumps(h)[:200]); return 0
    except (URLError, OSError, ValueError) as e:
        log("bus unreachable at %s (%s); skipping this run" % (BASE, getattr(e, "reason", e))); return 0

    pending = []  # list of (item, sha)
    seen = skipped_unchanged = oversized = 0
    totals = {"ingested": 0, "skipped": 0, "queued_llm": 0, "items": 0}

    def flush():
        if not pending:
            return
        items = [it for it, _ in pending]
        try:
            r = post("/ingest/batch", {"items": items})
        except HTTPError as e:
            log("batch failed HTTP %d: %s" % (e.code, e.read()[:200].decode(errors="replace"))); pending.clear(); return
        except (URLError, OSError) as e:
            log("batch failed: %s" % e); pending.clear(); return
        for k in totals:
            totals[k] += r.get(k, 0)
        for it, digest in pending:
            state[it["source"]] = digest
        pending.clear()
        STATE.write_text(json.dumps(state, indent=0, sort_keys=True))

    for doc in all_docs():
        seen += 1
        text = scrub(doc["text"]).strip()
        if not text:
            continue
        if len(text) > MAX_DOC_CHARS:
            oversized += 1
            text = text[:MAX_DOC_CHARS]
        digest = sha(text)
        if state.get(doc["source"]) == digest:
            skipped_unchanged += 1
            continue
        item = {"text": text, "source": doc["source"][:300], "agent": doc["agent"], "tags": (doc.get("tags") or [])[:16],
                "ts": doc.get("ts"), "doc": doc.get("doc"), "llm": "queue"}
        pending.append((item, digest))
        if len(pending) >= BATCH:
            flush()
    flush()
    log("sync done: docs=%d unchanged=%d pushed_items=%d ingested_chunks=%d dedup_chunks=%d queued_llm=%d truncated_docs=%d"
        % (seen, skipped_unchanged, totals["items"], totals["ingested"], totals["skipped"], totals["queued_llm"], oversized))
    return 0


if __name__ == "__main__":
    sys.exit(main())
