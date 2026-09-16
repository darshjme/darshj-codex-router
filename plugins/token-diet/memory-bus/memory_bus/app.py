"""FastAPI surface for the memory bus.

Run:  python -m memory_bus.app            (binds MB_HOSTS, default 127.0.0.1:8791)
Env:  see memory_bus.engine.Settings
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import __version__
from .engine import CHUNK_VERBATIM_TOKENS, Engine, Settings

logging.basicConfig(level=os.environ.get("MB_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("memory-bus")

settings = Settings()
app = FastAPI(title="memory-bus", version=__version__, docs_url="/docs", redoc_url=None)
_engine: Optional[Engine] = None
_engine_lock = threading.Lock()


def engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = Engine(settings)
    return _engine


def auth(authorization: Optional[str] = Header(default=None)) -> None:
    if settings.token and authorization != "Bearer " + settings.token:
        raise HTTPException(status_code=401, detail="bad token")


class IngestItem(BaseModel):
    text: str = Field(..., min_length=1, max_length=400_000)
    source: str = Field(..., min_length=1, max_length=300)
    agent: str = Field("unknown", max_length=64)
    tags: List[str] = Field(default_factory=list)
    ts: Optional[int] = None
    llm: Optional[str] = Field(None, pattern="^(queue|sync|off)$")
    chunk_tokens: int = Field(CHUNK_VERBATIM_TOKENS, ge=64, le=1500)
    force: bool = False
    doc: Optional[str] = None


class IngestBatch(BaseModel):
    items: List[IngestItem] = Field(..., min_length=1, max_length=500)


class CompressBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2_000_000)
    target_tokens: int = Field(800, ge=32, le=20_000)
    query: Optional[str] = Field(None, max_length=2000)
    mode: str = Field("tool_output", pattern="^(tool_output|prose)$")
    keep_head: int = Field(3, ge=0, le=50)
    keep_tail: int = Field(3, ge=0, le=50)


@app.get("/health")
def health():
    return engine().health()


@app.post("/ingest", dependencies=[Depends(auth)])
def ingest(body: IngestItem):
    return engine().ingest(**body.model_dump())


@app.post("/ingest/batch", dependencies=[Depends(auth)])
def ingest_batch(body: IngestBatch):
    e = engine()
    results = [e.ingest(**item.model_dump()) for item in body.items]
    return {"items": len(results), "ingested": sum(r["ingested"] for r in results),
            "skipped": sum(r["skipped"] for r in results), "queued_llm": sum(r["queued_llm"] for r in results)}


@app.get("/recall", dependencies=[Depends(auth)])
def recall(q: str = Query(..., min_length=1, max_length=4000), k: int = Query(8, ge=1, le=64),
           max_tokens: int = Query(None, ge=32, le=20_000), lam: float = Query(0.7, ge=0.0, le=1.0, alias="lambda"),
           source: Optional[str] = None, agent: Optional[str] = None, tags: Optional[str] = None,
           full: bool = False, min_score: float = Query(0.0, ge=0.0, le=1.0), format: str = Query("json", pattern="^(json|text)$")):
    out = engine().recall(q, k=k, max_tokens=max_tokens, lambda_=lam, source=source, agent=agent,
                          tags=[t for t in (tags or "").split(",") if t], full=full, min_score=min_score)
    if format == "text":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(out["packed_text"])
    return out


@app.post("/compress", dependencies=[Depends(auth)])
def compress(body: CompressBody):
    return engine().compress(body.text, body.target_tokens, query=body.query, mode=body.mode,
                             keep_head=body.keep_head, keep_tail=body.keep_tail)


def _tailnet_ip() -> Optional[str]:
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "tailscale0"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for tok in out.split():
        if tok.startswith("100.") and "/" in tok:
            return tok.split("/")[0]
    return None


def main() -> None:
    hosts = list(settings.hosts)
    if settings.bind_tailnet:
        ip = _tailnet_ip()
        if ip and ip not in hosts:
            hosts.append(ip)
    engine()  # load model + ensure collection before accepting traffic
    servers = []
    for h in hosts:
        cfg = uvicorn.Config(app, host=h, port=settings.port, log_level="info", workers=1)
        servers.append(uvicorn.Server(cfg))
    log.info("memory-bus %s listening on %s port %d", __version__, hosts, settings.port)
    threads = [threading.Thread(target=s.run, daemon=True) for s in servers[1:]]
    for t in threads:
        t.start()
    servers[0].run()


if __name__ == "__main__":
    main()
