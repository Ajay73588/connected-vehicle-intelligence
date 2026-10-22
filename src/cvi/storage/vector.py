"""Embeddings + pgvector retrieval.

Embedder: signed feature hashing of unigrams+bigrams into 256 dims, L2-normalised (a deterministic, dependency-free
"bag of hashed n-grams"). It runs fully offline, so retrieval works in Docker/CI without an external model. It is NOT
a neural embedding: semantic matches depend on shared vocabulary. The `embed()` seam is where a sentence-transformer or
hosted embedding model is plugged in (same dimension in the schema -> change EMBED_DIM + migration)."""

from __future__ import annotations

import hashlib
import math
import re

import psycopg

from cvi.common.config import get_settings

EMBED_DIM = 256
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "and", "or", "to", "is", "in", "on", "for", "with", "at", "by", "it", "this", "that"}


def embed(text: str) -> list[float]:
    toks = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]
    feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
    v = [0.0] * EMBED_DIM
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
        v[h % EMBED_DIM] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _cosine_topk(rows: list[dict], query_vec: list[float], limit: int, key: str = "embedding") -> list[dict]:
    import numpy as np

    if not rows:
        return []
    mat = np.array([[float(x) for x in r[key].strip("[]").split(",")] for r in rows])
    q = np.array(query_vec)
    sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-12)
    order = np.argsort(-sims)[:limit]
    return [{**{k: v for k, v in rows[i].items() if k != key}, "score": float(sims[i])} for i in order]


def search_knowledge(conn: psycopg.Connection, query: str, limit: int = 4) -> list[dict]:
    qv = embed(query)
    if get_settings().vector_backend == "python":
        rows = conn.execute("SELECT id, title, category, content, embedding::text AS embedding FROM knowledge_docs").fetchall()
        return _cosine_topk(rows, qv, limit)
    q = vec_literal(qv)
    return conn.execute(
        "SELECT id, title, category, content, 1 - (embedding <=> %s::vector) AS score "
        "FROM knowledge_docs ORDER BY embedding <=> %s::vector LIMIT %s", [q, q, limit]).fetchall()


def similar_incidents(conn: psycopg.Connection, tenant_id: int, text: str, limit: int = 3) -> list[dict]:
    qv = embed(text)
    if get_settings().vector_backend == "python":
        rows = conn.execute("SELECT alert_id, summary, embedding::text AS embedding FROM incident_embeddings WHERE tenant_id = %s "
                            "ORDER BY alert_id DESC LIMIT 5000", [tenant_id]).fetchall()
        return _cosine_topk(rows, qv, limit)
    q = vec_literal(qv)
    return conn.execute(
        "SELECT e.alert_id, e.summary, 1 - (e.embedding <=> %s::vector) AS score FROM incident_embeddings e "
        "WHERE e.tenant_id = %s ORDER BY e.embedding <=> %s::vector LIMIT %s", [q, tenant_id, q, limit]).fetchall()
