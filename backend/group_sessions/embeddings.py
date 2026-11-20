"""Group rooms — optional external `/v1/embeddings` adapter.

The local SentenceTransformer / PyTorch stack (`gte-multilingual-base`) was
removed so desktop builds stay lightweight and need no ML runtime in the
installer. Room memory now runs on zero-dependency LLM extraction + hybrid
BM25/n-gram retrieval by default (`retriever.py`).

When a user explicitly configures an external OpenAI-compatible `/v1/embeddings`
endpoint (cloud API or a standalone `llama-server --embedding` process), this
module encodes and scores vectors via `httpx` using only the Python standard
library for float32 storage and dot products.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from array import array
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)

MIN_SCORE = 0.50
RELATIVE_MARGIN = 0.10
DUPLICATE_SCORE = 0.92

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")
_TIMEOUT_SECONDS = 8.0


def _resolve_config() -> Optional[Dict[str, str]]:
    """Read optional external embedding config from app.json or env vars."""
    base_url = os.getenv("PEBBLE_EMBEDDING_URL", "").strip()
    model = os.getenv("PEBBLE_EMBEDDING_MODEL", "").strip()
    api_key = os.getenv("PEBBLE_EMBEDDING_API_KEY", "").strip()

    if not (base_url and model):
        try:
            from ..paths import load_app_config

            cfg = load_app_config().get("groupMemoryEmbedding")
            if isinstance(cfg, dict):
                base_url = base_url or str(cfg.get("baseUrl") or "").strip()
                model = model or str(cfg.get("model") or "").strip()
                api_key = api_key or str(cfg.get("apiKey") or "").strip()
        except Exception:  # noqa: BLE001 - config lookup must never break a turn
            return None

    if not base_url or not model:
        return None
    return {"base_url": base_url, "model": model, "api_key": api_key}


def _endpoint_url(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if not url.endswith("/embeddings"):
        url += "/embeddings"
    return url


def available() -> bool:
    """True only when an external `/v1/embeddings` endpoint is configured."""
    return _resolve_config() is not None


def _normalize(vec: Sequence[float]) -> List[float]:
    norm_sq = math.fsum(x * x for x in vec)
    if norm_sq <= 0.0:
        return [float(x) for x in vec]
    inv = 1.0 / math.sqrt(norm_sq)
    return [float(x) * inv for x in vec]


def _parse_response(payload: object, expected: int) -> Optional[List[List[float]]]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected:
        return None
    indexed: List[Tuple[int, List[float]]] = []
    for pos, item in enumerate(data):
        if not isinstance(item, dict):
            return None
        raw = item.get("embedding")
        if not isinstance(raw, list) or not raw:
            return None
        idx = int(item.get("index", pos))
        indexed.append((idx, _normalize([float(v) for v in raw])))
    indexed.sort(key=lambda pair: pair[0])
    return [vec for _, vec in indexed]


async def encode(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """L2-normalised vectors from external `/v1/embeddings`, or None."""
    clean = [t for t in texts if t and t.strip()]
    if not clean:
        return []
    cfg = _resolve_config()
    if cfg is None:
        return None

    url = _endpoint_url(cfg["base_url"])
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    trust_env = not any(h in url.lower() for h in _LOCAL_HOSTS)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, trust_env=trust_env) as client:
            resp = await client.post(
                url,
                json={"model": cfg["model"], "input": clean},
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning("group memory: remote embedding HTTP %s", resp.status_code)
                return None
            return _parse_response(resp.json(), len(clean))
    except Exception as exc:  # noqa: BLE001 - degrade silently to local retriever
        logger.warning("group memory: remote embedding failed: %s", exc)
        return None


def encode_sync(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """Synchronous wrapper for worker-thread callers."""
    clean = [t for t in texts if t and t.strip()]
    if not clean:
        return []
    if _resolve_config() is None:
        return None
    try:
        return asyncio.run(encode(clean))
    except RuntimeError:
        return None


def to_blob(vec: Sequence[float]) -> bytes:
    """Pack a float vector as raw little-endian float32 bytes."""
    arr = array("f", (float(x) for x in vec))
    return arr.tobytes()


def from_blob(blob: bytes) -> Optional[array]:
    """Unpack raw float32 bytes; returns None if corrupted or misaligned."""
    if not blob or (len(blob) % 4) != 0:
        return None
    arr = array("f")
    arr.frombytes(blob)
    return arr


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


def cosine_scores(
    probe: Sequence[float],
    rows: Sequence[Tuple[int, bytes]],
) -> Dict[int, float]:
    """Compute `{memory_id: cosine}` for stored vectors matching `probe` dim."""
    dim = len(probe)
    if dim == 0:
        return {}
    p = [float(x) for x in probe]
    out: Dict[int, float] = {}
    for memory_id, blob in rows:
        vec = from_blob(blob)
        if vec is None or len(vec) != dim:
            continue
        out[int(memory_id)] = _dot(vec, p)
    return out


def rank(
    probe: Sequence[float],
    rows: Sequence[Tuple[int, bytes]],
    limit: int = 6,
) -> List[int]:
    """Ids of the closest memories, best first."""
    scores = cosine_scores(probe, rows)
    if not scores:
        return []
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best = ordered[0][1]
    if best < MIN_SCORE:
        return []
    cutoff = max(MIN_SCORE, best - RELATIVE_MARGIN)
    return [mid for mid, score in ordered if score >= cutoff][:limit]


def rank_with_required(
    probe: Sequence[float],
    rows: Sequence[Tuple[int, bytes]],
    required_ids: Sequence[int],
    limit: int = 6,
    max_required: int = 2,
) -> List[int]:
    """Rank like `rank`, reserving slots for `mark == 2` standing memories."""
    scores = cosine_scores(probe, rows)
    if not scores:
        return []
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    required = {int(i) for i in required_ids}

    picked: List[int] = [mid for mid, _ in ordered if mid in required][:max_required]
    picked_set = set(picked)

    normal = [(mid, score) for mid, score in ordered if mid not in required]
    if normal:
        best = normal[0][1]
        if best >= MIN_SCORE:
            cutoff = max(MIN_SCORE, best - RELATIVE_MARGIN)
            normal = [item for item in normal if item[1] >= cutoff]
        else:
            normal = []

    for mid, _ in normal:
        if len(picked) >= limit:
            break
        if mid in picked_set:
            continue
        picked.append(mid)
        picked_set.add(mid)
    return picked[:limit]


def top_score(vec: Sequence[float], rows: Sequence[Tuple[int, bytes]]) -> float:
    """Highest cosine between `vec` and any stored vector, 0.0 when none."""
    scores = cosine_scores(vec, rows)
    return max(scores.values()) if scores else 0.0


def status() -> dict:
    """Small summary for diagnostics."""
    cfg = _resolve_config()
    return {
        "available": cfg is not None,
        "mode": "remote" if cfg is not None else "hybrid_lexical",
        "model": cfg["model"] if cfg is not None else None,
        "min_score": MIN_SCORE,
    }
