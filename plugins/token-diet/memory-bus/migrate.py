#!/usr/bin/env python3
"""Migrate legacy 384-d Qdrant collections into `engrams` with source tags.

Sources (SSOT §4 D6): agent_memory (3336 pts, embedded with intfloat/multilingual-e5-small),
super_memories (89), shared_memory (7). Vectors are NOT copied: the source model differs
from all-MiniLM-L6-v2, so every text is re-embedded by the engine. Source collections are
never modified or deleted. Idempotent: engram ids are uuid5(content hash).

Usage (on Kali, inside /opt/memory-bus):
  .venv/bin/python migrate.py --dry-run
  .venv/bin/python migrate.py                      # all three collections, llm=off
  .venv/bin/python migrate.py --only super_memories --llm queue
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from qdrant_client import QdrantClient

from memory_bus.engine import Engine, Settings

SOURCES = {
    "agent_memory": {"agent": "hermes", "tags": ["legacy", "agent_memory"]},
    "super_memories": {"agent": "mohini", "tags": ["legacy", "super_memories"]},
    "shared_memory": {"agent": "prime", "tags": ["legacy", "shared_memory"]},
}


def to_item(collection: str, payload: dict) -> dict | None:
    text = (payload.get("text") or "").strip()
    if len(text) < 12:
        return None
    meta = SOURCES[collection]
    tags = list(meta["tags"])
    for key in ("kind", "tier", "category", "bus"):
        if payload.get(key):
            tags.append("%s:%s" % (key, payload[key]))
    for t in payload.get("tags") or []:
        if isinstance(t, str):
            tags.append(t)
    if payload.get("importance") is not None:
        tags.append("importance:%s" % payload["importance"])
    ts = payload.get("ts") or payload.get("timestamp") or payload.get("createdAt")
    if isinstance(ts, (int, float)):
        ts = int(ts / 1000) if ts > 10_000_000_000 else int(ts)
    else:
        ts = None
    src_doc = payload.get("source") or ""
    source = "legacy:%s" % collection + (":" + src_doc if src_doc else "")
    return {"text": text, "source": source, "agent": meta["agent"], "tags": tags, "ts": ts, "doc": source}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", action="append", choices=sorted(SOURCES), help="restrict to collection(s)")
    ap.add_argument("--llm", default="off", choices=["off", "queue", "sync"], help="LLM engram mode for oversized chunks")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N points per collection (testing)")
    a = ap.parse_args()

    settings = Settings()
    settings.llm_mode = a.llm
    src = QdrantClient(url=settings.qdrant_url, timeout=60)
    eng = None if a.dry_run else Engine(settings)
    report = {}
    for name in (a.only or sorted(SOURCES)):
        if not src.collection_exists(name):
            report[name] = {"error": "missing"}
            continue
        offset = None
        seen = ingested = skipped = queued = empty = 0
        t0 = time.time()
        while True:
            points, offset = src.scroll(collection_name=name, limit=a.batch, offset=offset, with_payload=True, with_vectors=False)
            for p in points:
                seen += 1
                item = to_item(name, p.payload or {})
                if not item:
                    empty += 1
                    continue
                if a.dry_run:
                    if seen <= 2:
                        print(json.dumps({k: (v[:120] if isinstance(v, str) else v) for k, v in item.items()}, ensure_ascii=False))
                    continue
                r = eng.ingest(**item, llm=a.llm)
                ingested += r["ingested"]
                skipped += r["skipped"]
                queued += r["queued_llm"]
                if seen % 500 == 0:
                    print(json.dumps({"collection": name, "seen": seen, "ingested": ingested, "skipped": skipped}), flush=True)
            if offset is None or (a.limit and seen >= a.limit):
                break
        report[name] = {"seen": seen, "ingested": ingested, "skipped_existing": skipped, "empty": empty,
                        "queued_llm": queued, "seconds": round(time.time() - t0, 1)}
    if eng is not None:
        report["engrams"] = eng.collection_info()
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
