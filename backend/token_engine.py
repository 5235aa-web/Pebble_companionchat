"""
token_engine.py - Provider-aware token counting.

Resolution order:
  1. local base_url (llama.cpp / ollama / vllm)  -> POST /tokenize
  2. openai / chatgpt                            -> tiktoken
  3. anthropic                                   -> POST /v1/messages/count_tokens
  4. gemini                                      -> POST :countTokens
  5. other vendors                               -> tokenizers.Tokenizer.from_file(
                                                      models/tokenizers/<provider>/tokenizer.json)
                                                   -> tiktoken fallback (exact=False)

Every result carries an `exact` flag so callers never present an estimate as
a measurement. Remote calls are bounded by a timeout and a circuit breaker.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

try:
    import tiktoken
except ImportError:  # pragma: no cover - dependency declared in requirements
    tiktoken = None

try:
    from tokenizers import Tokenizer as _HFTokenizer
except ImportError:  # pragma: no cover
    _HFTokenizer = None

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENIZER_ROOT = os.path.join(PROJECT_ROOT, "models", "tokenizers")

REMOTE_TIMEOUT = 3.0
REMOTE_FAILURE_THRESHOLD = 3
REMOTE_COOLDOWN_SECONDS = 60.0
REMOTE_CACHE_LIMIT = 512

LOCAL_PROVIDERS = {"llama-cpp", "llamacpp", "llama_cpp", "ollama", "vllm", "local", "lmstudio"}
OPENAI_PROVIDERS = {"openai", "chatgpt", "azure", "azure-openai"}

# Per-message framing cost of each chat template. These are the published
# constants for OpenAI's format; other templates use a close approximation and
# are superseded by the provider's own counter whenever one exists.
MESSAGE_OVERHEAD = {
    "openai": 3,
    "anthropic": 4,
    "gemini": 4,
    "local": 4,
    "default": 4,
}
PRIMING_OVERHEAD = {"openai": 3, "default": 2}


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    exact: bool
    method: str


@dataclass(frozen=True)
class TokenBreakdown:
    system: int
    history: int
    total: int
    exact: bool
    method: str

    def as_dict(self) -> Dict:
        return {
            "system_tokens": self.system,
            "history_tokens": self.history,
            "total_tokens": self.total,
            "exact": self.exact,
            "method": self.method,
        }


def _is_local_base_url(url: Optional[str]) -> bool:
    if not url:
        return False
    lowered = str(url).lower()
    return any(h in lowered for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"))


def _normalize_provider(provider_type: Optional[str]) -> str:
    p = (provider_type or "").strip().lower()
    if p in LOCAL_PROVIDERS:
        return "local"
    if p in OPENAI_PROVIDERS:
        return "openai"
    if p == "anthropic":
        return "anthropic"
    if p == "gemini":
        return "gemini"
    return p or "unknown"


def _estimate_from_text(text: str) -> int:
    """Character-class aware fallback. Latin text ~3.6 chars/token, CJK ~1.5."""
    if not text:
        return 0
    ascii_chars = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_chars += 1
    other_chars = len(text) - ascii_chars
    estimate = ascii_chars / 3.6 + other_chars / 1.5
    return max(1, int(estimate + 0.5))


def _estimating_method(reason: str) -> str:
    return f"estimate:{reason}"


# ==================== counters ====================


class _BaseCounter:
    exact = False
    method = "estimate:unknown"
    family = "default"

    def count_text(self, text: str) -> TokenCount:
        return TokenCount(_estimate_from_text(text), False, self.method)

    async def acount_text(self, text: str) -> TokenCount:
        return self.count_text(text)

    def count_messages(self, messages: Sequence[Dict]) -> TokenCount:
        total = PRIMING_OVERHEAD.get(self.family, PRIMING_OVERHEAD["default"])
        own = 0
        for msg in messages:
            own += MESSAGE_OVERHEAD.get(self.family, MESSAGE_OVERHEAD["default"])
            own += self.count_text(msg.get("content", "") or "").tokens
            own += self.count_text(msg.get("role", "") or "").tokens
        total += own
        return TokenCount(total, self.exact, self.method)

    async def acount_messages(self, messages: Sequence[Dict]) -> TokenCount:
        total = PRIMING_OVERHEAD.get(self.family, PRIMING_OVERHEAD["default"])
        for msg in messages:
            total += MESSAGE_OVERHEAD.get(self.family, MESSAGE_OVERHEAD["default"])
            total += (await self.acount_text(msg.get("content", "") or "")).tokens
            total += (await self.acount_text(msg.get("role", "") or "")).tokens
        return TokenCount(total, self.exact, self.method)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0 or not text:
            return ""
        if self.count_text(text).tokens <= max_tokens:
            return text
        # Binary search on characters; works for counters that cannot decode.
        low, high = 0, len(text)
        best = ""
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid]
            if self.count_text(candidate).tokens <= max_tokens:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    async def atruncate_to_tokens(self, text: str, max_tokens: int) -> str:
        return self.truncate_to_tokens(text, max_tokens)


class _CharacterCounter(_BaseCounter):
    def __init__(self, reason: str = "characters"):
        self.method = _estimating_method(reason)


class _TiktokenCounter(_BaseCounter):
    exact = True
    family = "openai"

    def __init__(self, encoding_name: str, encoder):
        self.encoding_name = encoding_name
        self._encoder = encoder
        self.method = f"tiktoken:{encoding_name}"

    def count_text(self, text: str) -> TokenCount:
        if not text:
            return TokenCount(0, True, self.method)
        try:
            return TokenCount(len(self._encoder.encode(text, disallowed_special=())), True, self.method)
        except Exception as exc:  # pragma: no cover
            logger.warning("tiktoken encode failed (%s), falling back to estimate", exc)
            return TokenCount(_estimate_from_text(text), False, _estimating_method("tiktoken-failure"))

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0 or not text:
            return ""
        ids = self._encoder.encode(text, disallowed_special=())
        if len(ids) <= max_tokens:
            return text
        try:
            return self._encoder.decode(ids[:max_tokens])
        except Exception:  # pragma: no cover
            return super().truncate_to_tokens(text, max_tokens)


class _HFTokenizerCounter(_BaseCounter):
    exact = True
    family = "default"

    def __init__(self, label: str, tokenizer):
        self._tokenizer = tokenizer
        self.method = f"tokenizer-file:{label}"

    def count_text(self, text: str) -> TokenCount:
        if not text:
            return TokenCount(0, True, self.method)
        try:
            return TokenCount(len(self._tokenizer.encode(text).ids), True, self.method)
        except Exception as exc:  # pragma: no cover
            logger.warning("tokenizer-file encode failed (%s), falling back to estimate", exc)
            return TokenCount(_estimate_from_text(text), False, _estimating_method("tokenizer-failure"))


class _LlamaCppCounter(_BaseCounter):
    family = "local"

    def __init__(self, base_url: str, fallback: _BaseCounter):
        self._endpoint = _strip_openai_suffix(base_url) + "/tokenize"
        self._fallback = fallback
        self.exact = True
        self.method = "llama.cpp:/tokenize"

    def count_text(self, text: str) -> TokenCount:
        if not text:
            return TokenCount(0, True, self.method)
        cached = _remote_cache_get("llama", text)
        if cached is not None:
            return TokenCount(cached, True, self.method)
        if _breaker_open(self._endpoint):
            return self._fallback.count_text(text)
        try:
            with httpx.Client(timeout=REMOTE_TIMEOUT, trust_env=False) as client:
                tokens = self._post_tokenize(client, text)
            _breaker_success(self._endpoint)
            _remote_cache_put("llama", text, tokens)
            return TokenCount(tokens, True, self.method)
        except Exception as exc:
            _breaker_failure(self._endpoint)
            logger.debug("llama.cpp /tokenize failed: %s", exc)
            result = self._fallback.count_text(text)
            return TokenCount(result.tokens, False, _estimating_method("llama.cpp-unreachable"))

    async def acount_text(self, text: str) -> TokenCount:
        if not text:
            return TokenCount(0, True, self.method)
        cached = _remote_cache_get("llama", text)
        if cached is not None:
            return TokenCount(cached, True, self.method)
        if _breaker_open(self._endpoint):
            return await self._fallback.acount_text(text)
        try:
            async with httpx.AsyncClient(timeout=REMOTE_TIMEOUT, trust_env=False) as client:
                tokens = await self._apost_tokenize(client, text)
            _breaker_success(self._endpoint)
            _remote_cache_put("llama", text, tokens)
            return TokenCount(tokens, True, self.method)
        except Exception as exc:
            _breaker_failure(self._endpoint)
            logger.debug("llama.cpp /tokenize failed: %s", exc)
            result = await self._fallback.acount_text(text)
            return TokenCount(result.tokens, False, _estimating_method("llama.cpp-unreachable"))

    @staticmethod
    def _parse(payload: Dict) -> int:
        tokens = payload.get("tokens")
        if isinstance(tokens, list):
            return len(tokens)
        raise ValueError("unexpected /tokenize payload")

    def _post_tokenize(self, client: httpx.Client, text: str) -> int:
        resp = client.post(self._endpoint, json={"content": text})
        if resp.status_code == 400:
            resp = client.post(self._endpoint, json={"prompt": text})
        resp.raise_for_status()
        return self._parse(resp.json())

    async def _apost_tokenize(self, client: httpx.AsyncClient, text: str) -> int:
        resp = await client.post(self._endpoint, json={"content": text})
        if resp.status_code == 400:
            resp = await client.post(self._endpoint, json={"prompt": text})
        resp.raise_for_status()
        return self._parse(resp.json())

    async def atruncate_to_tokens(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0 or not text:
            return ""
        if (await self.acount_text(text)).tokens <= max_tokens:
            return text
        low, high = 0, len(text)
        best = ""
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid]
            if (await self.acount_text(candidate)).tokens <= max_tokens:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best


class _AnthropicCounter(_BaseCounter):
    family = "anthropic"

    def __init__(self, base_url: str, api_key: str, model: str, fallback: _BaseCounter):
        self._endpoint = _strip_openai_suffix(base_url) + "/messages/count_tokens"
        self._api_key = api_key
        self._model = model
        self._fallback = fallback
        self.exact = bool(api_key)
        self.method = "anthropic:/messages/count_tokens"

    def count_text(self, text: str) -> TokenCount:
        return self.count_messages([{"role": "user", "content": text}])

    def count_messages(self, messages: Sequence[Dict]) -> TokenCount:
        system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
        payload_messages = [
            {"role": "assistant" if m.get("role") == "assistant" else "user", "content": m.get("content", "")}
            for m in messages
            if m.get("role") != "system"
        ]
        if not payload_messages:
            payload_messages = [{"role": "user", "content": ""}]
        if not self._api_key or _breaker_open(self._endpoint):
            return self._fallback.count_messages(messages)
        payload = {"model": self._model, "messages": payload_messages}
        if system_parts:
            payload["system"] = "\n\n".join(p for p in system_parts if p)
        key = _hash_payload(payload)
        cached = _remote_cache_get("anthropic", key)
        if cached is not None:
            return TokenCount(cached, True, self.method)
        try:
            headers = {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            with httpx.Client(timeout=REMOTE_TIMEOUT, trust_env=True) as client:
                resp = client.post(self._endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                tokens = int(resp.json().get("input_tokens", 0))
            _breaker_success(self._endpoint)
            _remote_cache_put("anthropic", key, tokens)
            return TokenCount(tokens, True, self.method)
        except Exception as exc:
            _breaker_failure(self._endpoint)
            logger.debug("anthropic count_tokens failed: %s", exc)
            result = self._fallback.count_messages(messages)
            return TokenCount(result.tokens, False, _estimating_method("anthropic-unreachable"))

    async def acount_messages(self, messages: Sequence[Dict]) -> TokenCount:
        return self.count_messages(messages)


class _GeminiCounter(_BaseCounter):
    family = "gemini"

    def __init__(self, base_url: str, api_key: str, model: str, fallback: _BaseCounter):
        base = _strip_openai_suffix(base_url)
        for suffix in ("/v1beta", "/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        self._base = base
        self._api_key = api_key
        self._model = model.split("/")[-1]
        self._fallback = fallback
        self.exact = bool(api_key)
        self.method = "gemini:countTokens"

    def _endpoint(self) -> str:
        return f"{self._base}/v1beta/models/{self._model}:countTokens?key={self._api_key}"

    def count_text(self, text: str) -> TokenCount:
        if not text:
            return TokenCount(0, bool(self._api_key), self.method)
        if not self._api_key:
            return self._fallback.count_text(text)
        endpoint = self._endpoint()
        if _breaker_open(endpoint):
            return self._fallback.count_text(text)
        cached = _remote_cache_get("gemini", text)
        if cached is not None:
            return TokenCount(cached, True, self.method)
        try:
            with httpx.Client(timeout=REMOTE_TIMEOUT, trust_env=True) as client:
                resp = client.post(endpoint, json={"contents": [{"parts": [{"text": text}]}]})
                resp.raise_for_status()
                tokens = int(resp.json().get("totalTokens", 0))
            _breaker_success(endpoint)
            _remote_cache_put("gemini", text, tokens)
            return TokenCount(tokens, True, self.method)
        except Exception as exc:
            _breaker_failure(endpoint)
            logger.debug("gemini countTokens failed: %s", exc)
            result = self._fallback.count_text(text)
            return TokenCount(result.tokens, False, _estimating_method("gemini-unreachable"))


# ==================== remote helpers ====================


def _strip_openai_suffix(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    for suffix in ("/chat/completions", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def _hash_payload(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


_remote_cache: Dict[str, int] = {}
_remote_cache_order: List[str] = []
_remote_lock = threading.Lock()


def _remote_cache_get(scope: str, text: str) -> Optional[int]:
    key = f"{scope}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"
    with _remote_lock:
        return _remote_cache.get(key)


def _remote_cache_put(scope: str, text: str, tokens: int) -> None:
    key = f"{scope}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"
    with _remote_lock:
        if key not in _remote_cache:
            _remote_cache_order.append(key)
        _remote_cache[key] = tokens
        while len(_remote_cache_order) > REMOTE_CACHE_LIMIT:
            oldest = _remote_cache_order.pop(0)
            _remote_cache.pop(oldest, None)


_breaker_failures: Dict[str, int] = {}
_breaker_opened_at: Dict[str, float] = {}


def _breaker_open(endpoint: str) -> bool:
    with _remote_lock:
        opened = _breaker_opened_at.get(endpoint)
        if opened is None:
            return False
        if time.monotonic() - opened >= REMOTE_COOLDOWN_SECONDS:
            _breaker_opened_at.pop(endpoint, None)
            _breaker_failures.pop(endpoint, None)
            return False
        return True


def _breaker_failure(endpoint: str) -> None:
    with _remote_lock:
        count = _breaker_failures.get(endpoint, 0) + 1
        _breaker_failures[endpoint] = count
        if count >= REMOTE_FAILURE_THRESHOLD:
            _breaker_opened_at[endpoint] = time.monotonic()


def _breaker_success(endpoint: str) -> None:
    with _remote_lock:
        _breaker_failures.pop(endpoint, None)
        _breaker_opened_at.pop(endpoint, None)


# ==================== resolution ====================


def _tiktoken_encoding_name(model: str) -> Optional[str]:
    m = (model or "").lower()
    for prefix in ("gpt-5", "gpt-4.1", "gpt-4o", "gpt-4.5", "o1", "o3", "o4", "chatgpt"):
        if m.startswith(prefix):
            return "o200k_base"
    for prefix in ("gpt-4-turbo", "gpt-4-32k", "gpt-4", "gpt-3.5"):
        if m.startswith(prefix):
            return "cl100k_base"
    return "o200k_base"


_tiktoken_cache: Dict[str, object] = {}


def _load_tiktoken(encoding_name: str):
    if tiktoken is None:
        return None
    with _remote_lock:
        if encoding_name in _tiktoken_cache:
            return _tiktoken_cache[encoding_name]
    try:
        encoder = tiktoken.get_encoding(encoding_name)
    except Exception as exc:
        logger.warning("tiktoken encoding %s unavailable (%s)", encoding_name, exc)
        return None
    with _remote_lock:
        _tiktoken_cache[encoding_name] = encoder
    return encoder


def _tiktoken_fallback(model: str) -> _BaseCounter:
    name = _tiktoken_encoding_name(model)
    encoder = _load_tiktoken(name)
    if encoder is None:
        return _CharacterCounter("tiktoken-unavailable")
    return _TiktokenCounter(name, encoder)


def _tokenizer_file_candidates(provider: str, model: str) -> List[str]:
    safe_model = (model or "").replace("/", "_").replace("\\", "_")
    names = []
    if safe_model:
        names.append(os.path.join(TOKENIZER_ROOT, provider, f"{safe_model}.json"))
    names.append(os.path.join(TOKENIZER_ROOT, provider, "tokenizer.json"))
    return names


def _load_tokenizer_file(provider: str, model: str):
    if _HFTokenizer is None:
        return None
    for path in _tokenizer_file_candidates(provider, model):
        if not os.path.isfile(path):
            continue
        try:
            return path, _HFTokenizer.from_file(path)
        except Exception as exc:
            logger.warning("failed to load tokenizer file %s: %s", path, exc)
    return None


_counter_cache: Dict[str, _BaseCounter] = {}


def get_counter(
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> _BaseCounter:
    """Resolve the most precise counter available for this configuration."""
    provider = _normalize_provider(provider_type)
    model = model or ""
    cache_key = f"{provider}|{model}|{base_url or ''}"
    with _remote_lock:
        cached = _counter_cache.get(cache_key)
    if cached is not None:
        return cached

    fallback = _tiktoken_fallback(model)

    if provider == "local" or (_is_local_base_url(base_url) and provider != "anthropic"):
        if base_url:
            counter: _BaseCounter = _LlamaCppCounter(base_url, fallback)
        else:
            counter = fallback
    elif provider == "openai":
        counter = fallback
    elif provider == "anthropic":
        counter = _AnthropicCounter(base_url or "https://api.anthropic.com/v1", api_key or "", model, fallback)
    elif provider == "gemini":
        counter = _GeminiCounter(base_url or "https://generativelanguage.googleapis.com", api_key or "", model, fallback)
    else:
        loaded = _load_tokenizer_file(provider, model)
        if loaded:
            path, tokenizer = loaded
            counter = _HFTokenizerCounter(os.path.relpath(path, PROJECT_ROOT), tokenizer)
        else:
            counter = fallback

    with _remote_lock:
        _counter_cache[cache_key] = counter
    return counter


# ==================== public API ====================


def count_text(
    text: str,
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> TokenCount:
    return get_counter(provider_type, model, base_url, api_key).count_text(text)


async def acount_text(
    text: str,
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> TokenCount:
    return await get_counter(provider_type, model, base_url, api_key).acount_text(text)


def count_messages(
    messages: Sequence[Dict],
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> TokenCount:
    return get_counter(provider_type, model, base_url, api_key).count_messages(messages)


async def acount_messages(
    messages: Sequence[Dict],
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> TokenCount:
    return await get_counter(provider_type, model, base_url, api_key).acount_messages(messages)


def count_full_context(
    system_prompt: str,
    messages: Sequence[Dict],
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> TokenBreakdown:
    """Count the complete request payload: system prompt plus history."""
    counter = get_counter(provider_type, model, base_url, api_key)
    system = counter.count_text(system_prompt) if system_prompt else TokenCount(0, counter.exact, counter.method)
    history = counter.count_messages(messages) if messages else TokenCount(0, counter.exact, counter.method)
    exact = system.exact and history.exact
    method = system.method if system.method else history.method
    return TokenBreakdown(
        system=system.tokens,
        history=history.tokens,
        total=system.tokens + history.tokens,
        exact=exact,
        method=method,
    )


async def acount_full_context(
    system_prompt: str,
    messages: Sequence[Dict],
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> TokenBreakdown:
    counter = get_counter(provider_type, model, base_url, api_key)
    system = await counter.acount_text(system_prompt) if system_prompt else TokenCount(0, counter.exact, counter.method)
    history = await counter.acount_messages(messages) if messages else TokenCount(0, counter.exact, counter.method)
    exact = system.exact and history.exact
    method = system.method if system.method else history.method
    return TokenBreakdown(
        system=system.tokens,
        history=history.tokens,
        total=system.tokens + history.tokens,
        exact=exact,
        method=method,
    )


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    if not text or max_tokens <= 0:
        return ""
    return get_counter(provider_type, model, base_url, api_key).truncate_to_tokens(text, max_tokens)


async def atruncate_to_tokens(
    text: str,
    max_tokens: int,
    provider_type: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Non-blocking truncation for request handlers."""
    if not text or max_tokens <= 0:
        return ""
    counter = get_counter(provider_type, model, base_url, api_key)
    return await counter.atruncate_to_tokens(text, max_tokens)


def describe(provider_type: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None) -> Dict:
    """Diagnostics for the active counter, used by the token status endpoint."""
    counter = get_counter(provider_type, model, base_url)
    return {
        "method": counter.method,
        "exact": counter.exact,
        "provider": _normalize_provider(provider_type),
        "tokenizer_root": TOKENIZER_ROOT,
    }


# ==================== Measured usage ====================

# Providers report the token accounting of a request itself, which already
# includes the chat template and every field the client cannot see. That number
# is strictly better than anything reconstructed locally, so the UI prefers it
# and falls back to the local counter only when no request has run yet.

_USAGE_CACHE: Dict[Tuple[str, str, str], Dict] = {}
USAGE_TTL_SECONDS = 3600


def _usage_cache_key(companion_id: str, branch_id: str) -> Tuple[str, str, str]:
    from . import companion_db

    return (str(companion_db.DB_PATH), companion_id, branch_id)


def record_usage(companion_id: str, branch_id: str, usage: Dict) -> Dict:
    """Store the accounting a provider returned for this branch."""
    from . import companion_db

    entry = {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "recorded_at": time.time(),
    }
    _USAGE_CACHE[(str(companion_db.DB_PATH), companion_id, branch_id)] = entry
    try:
        companion_db.set_branch_token_usage(companion_id, branch_id, entry)
    except Exception as exc:  # pragma: no cover - defensive DB persistence
        logger.debug("persist branch token_usage skipped: %s", exc)
    return entry


def get_recorded_usage(
    companion_id: str,
    branch_id: str,
    max_age: Optional[int] = None,
) -> Optional[Dict]:
    """Last measured usage for a branch, or None when absent/cleared."""
    from . import companion_db

    key = _usage_cache_key(companion_id, branch_id)
    entry = _USAGE_CACHE.get(key)
    if not entry:
        try:
            entry = companion_db.get_branch_token_usage(companion_id, branch_id)
        except Exception as exc:  # pragma: no cover
            logger.debug("read branch token_usage skipped: %s", exc)
            entry = None
        if entry:
            _USAGE_CACHE[key] = entry
    if not entry:
        return None
    if max_age is not None and max_age > 0 and entry.get("recorded_at", 0) > 0:
        if time.time() - entry["recorded_at"] > max_age:
            return None
    return entry


def clear_recorded_usage(companion_id: str, branch_id: str) -> None:
    """Drop the cached and persisted measurement, e.g. after branch mutation."""
    from . import companion_db

    _USAGE_CACHE.pop((str(companion_db.DB_PATH), companion_id, branch_id), None)
    try:
        companion_db.set_branch_token_usage(companion_id, branch_id, None)
    except Exception as exc:  # pragma: no cover
        logger.debug("clear branch token_usage skipped: %s", exc)


# ==================== Context limit correction ====================

# When a request overflows, the provider states the real limit in its error.
# Reading it back is the only way to learn limits the static table guesses wrong.

_CONTEXT_ERROR_PATTERN = re.compile(
    r"prompt_too_long|prompt is too long|context[_ ]length|maximum context length|too many tokens",
    re.IGNORECASE,
)

_CONTEXT_LIMIT_PATTERNS = (
    re.compile(r"maximum context length is (\d+)", re.IGNORECASE),
    re.compile(r"maximum:?\s*(\d+)\s*tokens", re.IGNORECASE),
    re.compile(r"(\d+)\s*token[s]?\s*maximum", re.IGNORECASE),
    re.compile(r">\s*(\d+)\s*maximum", re.IGNORECASE),
    re.compile(r"limit:?\s*(\d+)", re.IGNORECASE),
)


def parse_context_limit_error(message: str) -> Optional[Dict]:
    """
    Read the real context limit out of an overflow error message.

    Returns None when the message is not about context length. The limit is
    None when the wording carries no number that can be tied to it, which is
    better than reporting a wrong number as if it were measured.
    """
    if not message:
        return None
    if not _CONTEXT_ERROR_PATTERN.search(message):
        return None

    text = message.replace(",", " ").replace("_", " ")
    limit = None
    for pattern in _CONTEXT_LIMIT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                limit = int(match.group(1))
            except (TypeError, ValueError):
                limit = None
            break

    return {"is_prompt_too_long": True, "context_limit": limit}
