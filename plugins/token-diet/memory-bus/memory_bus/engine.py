"""Memory-bus engine: embeddings, Qdrant `engrams` collection, ingest/recall/compress.

Runs on Kali. Dependencies: sentence-transformers (system site-packages, already
installed: 5.2.3), qdrant-client 1.17, torch. Ollama is reached with urllib.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Dict, List, Optional, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from . import textops

log = logging.getLogger("memory-bus")

NAMESPACE = uuid.UUID("7f1e6a2c-9b3d-4e5f-8a1b-2c3d4e5f6a7b")  # fixed: ids are uuid5(NAMESPACE, hash)

ENGRAM_MAX_TOKENS = 120          # SSOT D6: <=120-token engram per oversized chunk
CHUNK_VERBATIM_TOKENS = 400      # SSOT D6: chunks <= 400 tokens are stored verbatim
RAW_KEEP_CHARS = 8000            # raw chunk kept in payload (on_disk) for full=1 recall

LLM_PROMPT = (
    "You compress notes for an engineering memory system. Rewrite the text below as a dense, "
    "factual memory of at most 90 words. Keep identifiers, paths, ports, hostnames, numbers, dates, "
    "decisions and outcomes verbatim. No preamble, no bullet symbols, no commentary.\n\nTEXT:\n{text}\n\nMEMORY:"
)


class Settings:
    def __init__(self) -> None:
        env = os.environ.get
        self.hosts = [h.strip() for h in env("MB_HOSTS", "127.0.0.1").split(",") if h.strip()]
        self.port = int(env("MB_PORT", "8791"))          # 8790 is taken by mohini-panel.service on Kali
        self.bind_tailnet = env("MB_BIND_TAILNET", "1") == "1"
        self.token = env("MB_TOKEN", "")                 # optional bearer token
        self.qdrant_url = env("MB_QDRANT_URL", "http://127.0.0.1:6333")
        self.collection = env("MB_COLLECTION", "engrams")
        self.model = env("MB_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        self.dim = int(env("MB_DIM", "384"))
        self.torch_threads = int(env("MB_TORCH_THREADS", "8"))   # measured fastest on the 40-core EPYC
        self.ollama_url = env("MB_OLLAMA_URL", "http://127.0.0.1:11434")
        self.ollama_model = env("MB_OLLAMA_MODEL", "qwen2.5:3b")
        self.ollama_threads = int(env("MB_OLLAMA_THREADS", "32"))  # 8.8 tok/s vs 2.4 tok/s at default
        self.ollama_timeout = float(env("MB_OLLAMA_TIMEOUT", "180"))
        self.llm_mode = env("MB_LLM_MODE", "queue")      # queue | sync | off
        self.llm_queue_max = int(env("MB_LLM_QUEUE_MAX", "5000"))
        self.recall_pool = int(env("MB_RECALL_POOL", "48"))
        self.default_max_tokens = int(env("MB_DEFAULT_MAX_TOKENS", "1200"))


class Embedder:
    def __init__(self, model_name: str, threads: int) -> None:
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(threads)
        t0 = time.time()
        self.model = SentenceTransformer(model_name, device="cpu")
        self.dim = int(self.model.get_sentence_embedding_dimension())
        self.load_seconds = round(time.time() - t0, 2)
        self._lock = threading.Lock()
        log.info("embedder %s loaded in %.2fs dim=%d", model_name, self.load_seconds, self.dim)

    def __call__(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        with self._lock:
            vecs = self.model.encode(list(texts), batch_size=32, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]


class Ollama:
    def __init__(self, url: str, model: str, threads: int, timeout: float) -> None:
        self.url, self.model, self.threads, self.timeout = url.rstrip("/"), model, threads, timeout

    def reachable(self) -> bool:
        try:
            with urlopen(self.url + "/api/tags", timeout=3) as r:
                return r.status == 200
        except (URLError, OSError, ValueError):
            return False

    def engram(self, text: str) -> Optional[str]:
        body = {
            "model": self.model,
            "prompt": LLM_PROMPT.format(text=text[:6000]),
            "stream": False,
            "keep_alive": "30m",
            "options": {"num_predict": ENGRAM_MAX_TOKENS + 40, "temperature": 0.1, "num_thread": self.threads},
        }
        req = Request(self.url + "/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as r:
                out = json.load(r)
        except (URLError, OSError, ValueError) as e:
            log.warning("ollama engram failed: %s", e)
            return None
        summary = textops.normalize(out.get("response", ""))
        if len(summary) < 20:
            return None
        return textops.truncate_to_tokens(summary, ENGRAM_MAX_TOKENS + 30)


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.started = time.time()
        self.embed = Embedder(settings.model, settings.torch_threads)
        if self.embed.dim != settings.dim:
            raise RuntimeError("model dim %d != MB_DIM %d" % (self.embed.dim, settings.dim))
        self.qdrant = QdrantClient(url=settings.qdrant_url, timeout=30)
        self.ollama = Ollama(settings.ollama_url, settings.ollama_model, settings.ollama_threads, settings.ollama_timeout)
        self.ensure_collection()
        self.llm_queue: "queue.Queue[dict]" = queue.Queue(maxsize=settings.llm_queue_max)
        self.llm_done = 0
        self.llm_failed = 0
        if settings.llm_mode != "off":
            threading.Thread(target=self._llm_worker, name="llm-worker", daemon=True).start()

    # ------------------------------------------------------------ Qdrant --
    def ensure_collection(self) -> None:
        name = self.s.collection
        if not self.qdrant.collection_exists(name):
            self.qdrant.create_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(size=self.s.dim, distance=qm.Distance.COSINE, on_disk=True),
                hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=100),
                quantization_config=qm.ScalarQuantization(
                    scalar=qm.ScalarQuantizationConfig(type=qm.ScalarType.INT8, quantile=0.99, always_ram=False)),
                on_disk_payload=True,
            )
            log.info("created collection %s", name)
        for field, schema in (("source", qm.PayloadSchemaType.KEYWORD), ("agent", qm.PayloadSchemaType.KEYWORD),
                              ("hash", qm.PayloadSchemaType.KEYWORD), ("tags", qm.PayloadSchemaType.KEYWORD),
                              ("ts", qm.PayloadSchemaType.INTEGER)):
            try:
                self.qdrant.create_payload_index(collection_name=name, field_name=field, field_schema=schema)
            except Exception:  # already exists
                pass

    def collection_info(self) -> dict:
        info = self.qdrant.get_collection(self.s.collection)
        cfg = info.config
        vec = cfg.params.vectors
        quant = cfg.quantization_config
        return {
            "name": self.s.collection,
            "points": info.points_count,
            "status": str(info.status),
            "vector_size": getattr(vec, "size", None),
            "vectors_on_disk": getattr(vec, "on_disk", None),
            "hnsw_m": cfg.hnsw_config.m,
            "hnsw_ef_construct": cfg.hnsw_config.ef_construct,
            "quantization": (quant.model_dump() if hasattr(quant, "model_dump") else str(quant)) if quant else None,
            "on_disk_payload": cfg.params.on_disk_payload,
        }

    @staticmethod
    def point_id(h: str) -> str:
        return str(uuid.uuid5(NAMESPACE, h))

    def existing_ids(self, ids: Sequence[str]) -> set:
        if not ids:
            return set()
        found = self.qdrant.retrieve(collection_name=self.s.collection, ids=list(ids), with_payload=False, with_vectors=False)
        return {str(p.id) for p in found}

    # ------------------------------------------------------------ ingest --
    def ingest(self, text: str, source: str, agent: str = "unknown", tags: Optional[List[str]] = None,
               ts: Optional[int] = None, llm: Optional[str] = None, chunk_tokens: int = CHUNK_VERBATIM_TOKENS,
               force: bool = False, doc: Optional[str] = None) -> dict:
        llm_mode = llm or self.s.llm_mode
        text = textops.scrub(text)
        chunks = textops.chunk_text(text, chunk_tokens=chunk_tokens)
        if not chunks:
            return {"ingested": 0, "skipped": 0, "ids": [], "queued_llm": 0, "chunks": 0}
        ts = int(ts or time.time())
        tags = sorted({t.strip() for t in (tags or []) if t and t.strip()})
        hashes = [textops.content_hash(c) for c in chunks]
        ids = [self.point_id(h) for h in hashes]
        present = set() if force else self.existing_ids(ids)
        todo = [(i, c, h, pid) for i, (c, h, pid) in enumerate(zip(chunks, hashes, ids)) if pid not in present]
        queued = 0
        points: List[qm.PointStruct] = []
        engram_texts: List[str] = []
        metas: List[dict] = []
        for i, chunk, h, pid in todo:
            payload = {"source": source, "agent": agent, "ts": ts, "tags": tags, "hash": h, "chunk_index": i,
                       "chunks": len(chunks), "doc": doc or source, "model": self.s.model}
            if textops.est_tokens(chunk) <= chunk_tokens:
                payload.update(text=chunk, engram_kind="verbatim")
            else:
                payload["raw"] = chunk[:RAW_KEEP_CHARS]
                summary = None
                if llm_mode == "sync":
                    summary = self.ollama.engram(chunk)
                if summary:
                    payload.update(text=summary, engram_kind="llm")
                else:
                    ext = textops.extractive_compress(chunk, self.embed, ENGRAM_MAX_TOKENS, mode="prose")
                    payload.update(text=ext["text"], engram_kind="extractive")
                    if llm_mode == "queue":
                        try:
                            self.llm_queue.put_nowait({"id": pid, "raw": chunk})
                            queued += 1
                        except queue.Full:
                            log.warning("llm queue full; %s stays extractive", pid)
            engram_texts.append(payload["text"])
            metas.append((pid, payload))
        if metas:
            vectors = self.embed(engram_texts)
            for (pid, payload), vec in zip(metas, vectors):
                points.append(qm.PointStruct(id=pid, vector=vec, payload=payload))
            self.qdrant.upsert(collection_name=self.s.collection, points=points, wait=True)
        return {"ingested": len(points), "skipped": len(chunks) - len(todo), "ids": [m[0] for m in metas],
                "queued_llm": queued, "chunks": len(chunks)}

    def _llm_worker(self) -> None:
        while True:
            job = self.llm_queue.get()
            try:
                summary = self.ollama.engram(job["raw"])
                if not summary:
                    self.llm_failed += 1
                    continue
                vec = self.embed([summary])[0]
                got = self.qdrant.retrieve(collection_name=self.s.collection, ids=[job["id"]], with_payload=True)
                if not got:
                    continue
                payload = dict(got[0].payload or {})
                payload.update(text=summary, engram_kind="llm", llm_model=self.s.ollama_model)
                self.qdrant.upsert(collection_name=self.s.collection,
                                   points=[qm.PointStruct(id=job["id"], vector=vec, payload=payload)], wait=True)
                self.llm_done += 1
            except Exception as e:  # never kill the worker
                self.llm_failed += 1
                log.warning("llm worker error: %s", e)
            finally:
                self.llm_queue.task_done()

    # ------------------------------------------------------------ recall --
    def recall(self, q: str, k: int = 8, max_tokens: Optional[int] = None, lambda_: float = 0.7,
               source: Optional[str] = None, agent: Optional[str] = None, tags: Optional[List[str]] = None,
               full: bool = False, min_score: float = 0.0) -> dict:
        t0 = time.time()
        max_tokens = max_tokens or self.s.default_max_tokens
        qvec = self.embed([q])[0]
        must = []
        if source:
            must.append(qm.FieldCondition(key="source", match=qm.MatchValue(value=source)))
        if agent:
            must.append(qm.FieldCondition(key="agent", match=qm.MatchValue(value=agent)))
        for t in tags or []:
            must.append(qm.FieldCondition(key="tags", match=qm.MatchValue(value=t)))
        pool = max(self.s.recall_pool, k * 4)
        resp = self.qdrant.query_points(
            collection_name=self.s.collection, query=qvec, limit=pool,
            query_filter=qm.Filter(must=must) if must else None,
            with_payload=True, with_vectors=True, score_threshold=min_score or None,
            search_params=qm.SearchParams(hnsw_ef=128, quantization=qm.QuantizationSearchParams(
                ignore=False, rescore=True, oversampling=2.0)),
        )
        cands = resp.points or []
        vecs = [p.vector if isinstance(p.vector, list) else list(p.vector) for p in cands]
        order = textops.mmr(qvec, vecs, k=k, lambda_=lambda_, relevance=[float(p.score) for p in cands])
        hits = []
        for i in order:
            p = cands[i]
            pl = p.payload or {}
            text = pl.get("raw") if (full and pl.get("raw")) else pl.get("text", "")
            hits.append({"id": str(p.id), "score": round(float(p.score), 4), "text": text, "source": pl.get("source"),
                         "agent": pl.get("agent"), "ts": pl.get("ts"), "tags": pl.get("tags", []),
                         "engram_kind": pl.get("engram_kind")})
        kept, packed, used = textops.pack_hits(hits, max_tokens)
        return {"query": q, "k": k, "max_tokens": max_tokens, "candidates": len(cands), "tokens": used,
                "hits": kept, "packed_text": packed, "ms": int((time.time() - t0) * 1000)}

    # ---------------------------------------------------------- compress --
    def compress(self, text: str, target_tokens: int, query: Optional[str] = None, mode: str = "tool_output",
                 keep_head: int = 3, keep_tail: int = 3) -> dict:
        t0 = time.time()
        qvec = self.embed([query])[0] if query else None
        out = textops.extractive_compress(text, self.embed, target_tokens, query_vec=qvec, mode=mode,
                                          keep_head=keep_head, keep_tail=keep_tail)
        out["ms"] = int((time.time() - t0) * 1000)
        return out

    # ------------------------------------------------------------ health --
    def health(self) -> dict:
        try:
            info = self.collection_info()
            ok = True
        except Exception as e:
            info = {"error": str(e)[:200]}
            ok = False
        return {"ok": ok, "uptime_s": int(time.time() - self.started), "model": self.s.model, "dim": self.s.dim,
                "embedder_load_s": self.embed.load_seconds, "collection": info,
                "ollama": {"model": self.s.ollama_model, "reachable": self.ollama.reachable(), "mode": self.s.llm_mode,
                           "queue": self.llm_queue.qsize(), "done": self.llm_done, "failed": self.llm_failed}}
