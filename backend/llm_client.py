import asyncio
import json
import re
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

import httpx

from .paths import LLM_CONFIG_PATH

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 从 brain.py 复制的清理模式
THINK_TAG_PATTERN = re.compile(r"(?:<think>|<Think>)\s*(.*?)\s*(?:</think>|</Think>)", re.DOTALL | re.IGNORECASE)
EOT_TOKEN_PATTERN = re.compile(r"<\|eot_id\|>", re.IGNORECASE)
EOS_TOKEN_PATTERN = re.compile(r"</s>", re.IGNORECASE)
NEEDS_CHECK_PATTERN = re.compile(r"\[NEEDS CHECK:.*?\]", re.DOTALL | re.IGNORECASE)

SUPPORTED_PROVIDERS = [
    "openai", "deepseek", "qwen", "grok", "xiaomimimo",
    "chatgpt", "volcengine", "anthropic", "gemini",
    "llama-cpp", "ollama", "lmstudio", "local-openai",
]

_LOCAL_COMPAT_PROVIDERS = {"llama-cpp", "ollama", "lmstudio", "local-openai"}

# Providers that speak the OpenAI chat-completions dialect and therefore accept
# frequency_penalty / presence_penalty. Anthropic and Gemini are configured
# through compatible gateways, whose support for those two fields varies, so the
# project leaves them out rather than risking a 400.
_PENALTY_PROVIDERS = {
    "openai", "deepseek", "qwen", "grok", "xiaomimimo",
    "chatgpt", "volcengine", "ollama", "lmstudio", "local-openai",
}


async def call_llm_stream(
    provider_type: str,
    api_key: str,
    base_url: str,
    model: str,
    messages: List[Dict],
    temperature: float = 0.5,
    top_p: float = 0.9,
    max_tokens: Optional[int] = None,
    enable_thinking: bool = False,
    top_k: Optional[int] = None,
    min_p: Optional[float] = None,
    repeat_penalty: Optional[float] = None,
    frequency_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    enable_mtp: Optional[bool] = None,
) -> AsyncGenerator[Dict, None]:
    """
    完全独立的LLM流式调用。
    支持内置托管 llama-cpp 自动拉起/切换，以及外部本地引擎 (ollama / lmstudio / local-openai)
    的扩展采样与思考关闭兼容回退。

    Yields:
        {"type": "content", "content": str}
        {"type": "thinking", "content": str}  # 仅当 enable_thinking=True
        {"type": "error", "message": str}     # 出错时
    """
    if provider_type not in SUPPORTED_PROVIDERS:
        yield {"type": "error", "message": f"Unsupported provider_type: {provider_type}. Supported: {SUPPORTED_PROVIDERS}"}
        return

    if provider_type == "llama-cpp":
        from .llama_server_engine import is_managed_local_url, llama_server
        if is_managed_local_url(base_url):
            try:
                status = await asyncio.to_thread(
                    llama_server.ensure_running, (model or "").strip() or None, enable_mtp
                )
            except Exception as exc:
                yield {"type": "error", "message": f"Local LLM start failed: {exc}"}
                return
            if not (base_url or "").strip():
                base_url = str(status.get("base_url") or "")
            if not (model or "").strip():
                model = str(status.get("model") or model)

    # 构建URL（确保/chat/completions端点）
    url = base_url.strip().rstrip('/')
    if not url.endswith('/chat/completions'):
        url += '/chat/completions'

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    base_payload: Dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
    }
    # 最大输出长度（可选）：未设置时不下发，使用服务端默认
    if isinstance(max_tokens, int) and max_tokens > 0:
        base_payload["max_tokens"] = max_tokens

    payload: Dict = {
        **base_payload,
        "stream_options": {"include_usage": True},
        # 【强制禁用思考模式】companion 聊天不支持思考模式
        "thinking": {"type": "disabled"},
    }
    if provider_type in _LOCAL_COMPAT_PROVIDERS:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        if isinstance(top_k, int) and top_k > 0:
            payload["top_k"] = top_k
        if isinstance(min_p, (int, float)) and 0 <= float(min_p) <= 1:
            payload["min_p"] = float(min_p)
        if isinstance(repeat_penalty, (int, float)) and float(repeat_penalty) > 0:
            payload["repeat_penalty"] = float(repeat_penalty)
    if provider_type in _PENALTY_PROVIDERS:
        # OpenAI-compatible penalties. 0 is a meaningful value ("penalise
        # nothing"), so the check is on the type rather than on truthiness;
        # None means "leave the field out and keep the server default".
        if isinstance(frequency_penalty, (int, float)):
            payload["frequency_penalty"] = float(frequency_penalty)
        if isinstance(presence_penalty, (int, float)):
            payload["presence_penalty"] = float(presence_penalty)

    fallback_payload: Optional[Dict] = None
    if provider_type in _LOCAL_COMPAT_PROVIDERS:
        fallback_payload = dict(base_payload)
        for k in ("frequency_penalty", "presence_penalty"):
            if k in payload:
                fallback_payload[k] = payload[k]

    # 【强制禁用思考模式】忽略 enable_thinking 参数
    # companion 聊天中，所有模型都不允许使用思考模式
    enable_thinking = False

    try:
        # 本地回环必须绕过系统代理：httpx 的 trust_env 会经 getproxies() 读到 Windows
        # 注册表里的 ProxyServer，却不处理 ProxyOverride 中的 <local> / 127.*，
        # 请求会被错误转发给代理然后断开（RemoteProtocolError: Server disconnected）。
        # 云端 provider 仍保留代理行为，避免影响需要代理出网的场景。
        _local_hosts = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")
        _trust_env = not any(h in url.lower() for h in _local_hosts)
        attempts = [payload] + ([fallback_payload] if fallback_payload is not None else [])
        async with httpx.AsyncClient(timeout=300.0, trust_env=_trust_env) as client:
            for idx, req_payload in enumerate(attempts):
                async with client.stream("POST", url, json=req_payload, headers=headers) as response:
                    if response.status_code in (400, 422) and idx == 0 and len(attempts) > 1:
                        # Strict OpenAI-compatible local servers (some Ollama/LocalAI/vLLM setups)
                        # may reject non-standard fields like chat_template_kwargs / top_k / thinking.
                        await response.aread()
                        continue
                    if response.status_code != 200:
                        error_text = await response.aread()
                        yield {"type": "error", "message": f"HTTP {response.status_code}: {error_text.decode()}"}
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data = line[6:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)

                                usage = chunk.get("usage")
                                if isinstance(usage, dict):
                                    prompt_tokens = usage.get("prompt_tokens")
                                    if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                                        yield {
                                            "type": "usage",
                                            "prompt_tokens": prompt_tokens,
                                            "completion_tokens": usage.get("completion_tokens") or 0,
                                            "total_tokens": usage.get("total_tokens") or 0,
                                        }

                                choices = chunk.get("choices")
                                if choices and isinstance(choices, list) and len(choices) > 0:
                                    first_choice = choices[0]
                                    if first_choice and isinstance(first_choice, dict):
                                        delta = first_choice.get("delta", {}) or {}
                                        content = delta.get("content") or ""

                                        delta_reasoning = delta.get("reasoning_content") or ""
                                        if enable_thinking and delta_reasoning:
                                            yield {"type": "thinking", "content": delta_reasoning}

                                        if content:
                                            thoughts = THINK_TAG_PATTERN.findall(content)
                                            if enable_thinking and thoughts:
                                                cleaned_content = THINK_TAG_PATTERN.sub("", content)
                                                for thought in thoughts:
                                                    yield {"type": "thinking", "content": thought.strip()}
                                                yield {"type": "content", "content": cleaned_content}
                                            else:
                                                cleaned_content = THINK_TAG_PATTERN.sub("", content)
                                                yield {"type": "content", "content": cleaned_content}
                            except json.JSONDecodeError:
                                continue
                    return
    except Exception as e:
        yield {"type": "error", "message": f"LLM call failed: {str(e)}"}


def read_llm_providers_config() -> Optional[Dict]:
    """Read llm_providers.json preferring user config dir over legacy CWD fallback."""
    candidates = (
        Path(LLM_CONFIG_PATH),
        Path("llm_providers.json"),
        Path("./llm_providers.json"),
    )
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("providers"), list):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return None


def _provider_sampling_dict(provider: Dict, model_name: str) -> Dict:
    per_map = provider.get("modelParams") or {}
    per = per_map.get(model_name) if isinstance(per_map, dict) else None
    if not isinstance(per, dict):
        per = {}

    def pick(key: str):
        if key in per:
            return per[key]
        return provider.get(key)

    max_ctx = pick("maxContext") or 0
    if not max_ctx and provider.get("provider") == "llama-cpp" and model_name:
        try:
            from .llama_server_engine import get_merged_registry
            spec = get_merged_registry().get(model_name)
            if spec and spec.get("ctx"):
                max_ctx = int(spec["ctx"])
        except Exception:
            pass

    return {
        "temperature": pick("temperature"),
        "top_p": pick("topP"),
        "max_tokens": pick("maxTokens"),
        "max_context": int(max_ctx) if max_ctx else 0,
        "top_k": pick("topK"),
        "min_p": pick("minP"),
        "repeat_penalty": pick("repeatPenalty"),
        "frequency_penalty": pick("frequencyPenalty"),
        "presence_penalty": pick("presencePenalty"),
        "enable_mtp": bool(pick("enableMtp")) if pick("enableMtp") is not None else False,
    }


def _match_provider_for_model(providers: List[Dict], active_idx: int, requested_model: str = "") -> Optional[Dict]:
    if not providers:
        return None
    req = (requested_model or "").strip()
    if req:
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            models = provider.get("models") or []
            if req in models:
                return provider
        local_registry = None
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("provider") != "llama-cpp":
                continue
            if req == (provider.get("localModelKey") or "").strip():
                return provider
            if isinstance(provider.get("modelParams"), dict) and req in provider["modelParams"]:
                return provider
            if local_registry is None:
                try:
                    from .llama_server_engine import get_merged_registry
                    local_registry = get_merged_registry()
                except Exception:
                    local_registry = {}
            if req in local_registry:
                return provider

    if 0 <= active_idx < len(providers) and isinstance(providers[active_idx], dict):
        return providers[active_idx]
    return next((p for p in providers if isinstance(p, dict)), None)


def resolve_provider_model_config(requested_model: str = "") -> Optional[Dict]:
    """Resolve full LLM call config from disk, matching *requested_model* when possible."""
    config = read_llm_providers_config()
    if not config:
        return None
    providers = config.get("providers") or []
    active_idx = int(config.get("activeProviderIndex") or 0)
    provider = _match_provider_for_model(providers, active_idx, requested_model)
    if not provider:
        return None

    ptype = str(provider.get("provider") or "openai")
    models = [str(m).strip() for m in (provider.get("models") or []) if str(m).strip()]
    req = (requested_model or "").strip()
    if req:
        model_name = req
    elif ptype == "llama-cpp":
        local_key = str(provider.get("localModelKey") or "").strip()
        if local_key:
            model_name = local_key
        elif models:
            model_name = models[0]
        else:
            try:
                from .llama_server_engine import resolve_default_model_key
                model_name = resolve_default_model_key()
            except Exception:
                model_name = ""
    else:
        model_name = models[0] if models else "gpt-4"

    return {
        "provider_type": ptype,
        "api_key": str(provider.get("apiKey") or ""),
        "base_url": str(provider.get("baseUrl") or ""),
        "model": model_name,
        **_provider_sampling_dict(provider, model_name),
    }


def resolve_model_max_context(model: str = "") -> int:
    """Resolve explicit context window from user provider config or merged local GGUF registry."""
    config = read_llm_providers_config() or {}
    providers = config.get("providers") or []
    req = (model or "").strip()
    if req:
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            per_map = provider.get("modelParams") or {}
            per = per_map.get(req) if isinstance(per_map, dict) else None
            matched = (
                req in (provider.get("models") or [])
                or (provider.get("provider") == "llama-cpp" and req == (provider.get("localModelKey") or "").strip())
                or isinstance(per, dict)
            )
            if matched:
                if isinstance(per, dict) and int(per.get("maxContext") or 0) > 0:
                    return int(per["maxContext"])
                if int(provider.get("maxContext") or 0) > 0:
                    return int(provider["maxContext"])
        try:
            from .llama_server_engine import get_merged_registry
            spec = get_merged_registry().get(req)
            if spec and spec.get("ctx"):
                return int(spec["ctx"])
        except Exception:
            pass

    active_idx = int(config.get("activeProviderIndex") or 0)
    if providers and 0 <= active_idx < len(providers) and isinstance(providers[active_idx], dict):
        active_provider = providers[active_idx]
        if int(active_provider.get("maxContext") or 0) > 0:
            return int(active_provider["maxContext"])
    return 0


def load_default_llm_config() -> Optional[Dict]:
    """Load the active provider config from llm_providers.json."""
    return resolve_provider_model_config("")
