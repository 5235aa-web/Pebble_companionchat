"""
token_counter.py - Token accounting.

Counting itself lives in token_engine, which dispatches per provider: local
llama.cpp /tokenize, OpenAI tiktoken, Anthropic and Gemini count endpoints, or
a vendor tokenizer file dropped into models/tokenizers/<provider>/. This module
keeps the stable public surface (context windows, thresholds, status) and
forwards counting calls.
"""
import logging
from typing import List, Dict, Optional

from . import token_engine

logger = logging.getLogger(__name__)

# Retained for callers that imported it directly; token_engine's character
# fallback is language aware and supersedes it.
TOKEN_ESTIMATION_RATIO = 4 / 3

# 默认上下文窗口大小（按模型）- 2026年4月最新更新
DEFAULT_CONTEXT_WINDOWS = {
    # ============ OpenAI 模型 (2026年4月最新) ============
    # GPT-5 系列 (最新旗舰)
    "gpt-5.5": 128_000,           # 旗舰模型，复杂推理和编码
    "gpt-5.4": 128_000,           # 标准旗舰
    "gpt-5.4-mini": 128_000,      # 轻量级
    "gpt-5.4-nano": 128_000,      # 最轻量
    
    # GPT-4.1 系列 (1M 上下文)
    "gpt-4.1": 1_048_576,         # 1M 上下文
    "gpt-4.1-mini": 1_048_576,    # 1M 上下文
    "gpt-4.1-nano": 1_048_576,    # 1M 上下文
    
    # o 系列推理模型
    "o3": 200_000,
    "o3-pro": 200_000,
    "o4-mini": 200_000,
    
    # GPT-4o 系列
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,               # 标准版 8k
    "gpt-4-32k": 32_768,          # 长上下文版 32k
    "gpt-3.5-turbo": 16_384,
    
    # ============ Anthropic Claude 模型 (2026年4月最新) ============
    # Claude 4.x 系列 (最新)
    "claude-opus-4-7": 1_000_000, # 1M 上下文
    "claude-opus-4-6": 1_000_000, # 1M 上下文
    "claude-sonnet-4-6": 1_000_000, # 1M 上下文
    "claude-sonnet-4-5": 200_000, # 200K 上下文
    "claude-haiku-4-5": 200_000,  # 200K 上下文
    "claude-opus-4": 200_000,     # 已弃用，迁移到 4.6/4.7
    "claude-sonnet-4": 200_000,   # 已弃用，迁移到 4.6
    
    # Claude 3.x 系列 (旧版，部分已退役)
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,  # 已退役
    "claude-3-7-sonnet": 200_000, # 已退役
    "claude-3.5-sonnet": 200_000,
    "claude-3.5-haiku": 200_000,
    
    # ============ Google Gemini 模型 (2026年4月最新) ============
    # Gemini 3.x 系列 (最新)
    "gemini-3.1-ultra": 2_000_000,  # 2M 上下文 - 最大
    "gemini-3.1-pro": 2_000_000,    # 2M 上下文
    "gemini-3.1-flash": 1_000_000,  # 1M 上下文
    "gemini-3.1-flash-lite": 1_000_000, # 1M 上下文
    "gemini-3-pro": 1_000_000,      # 1M 上下文
    "gemini-3-flash": 1_000_000,    # 1M 上下文
    
    # Gemini 2.x 系列
    "gemini-2.5-pro": 1_048_576,    # 1M 上下文
    "gemini-2.5-flash": 200_000,    # 200K 上下文
    "gemini-2.0-flash": 1_048_576,  # 1M 上下文
    "gemini-2.0-flash-lite": 1_000_000, # 1M 上下文
    
    # Gemini 1.5 系列
    "gemini-1.5-pro": 2_000_000,    # 2M 上下文 (企业/API)
    "gemini-1.5-flash": 1_000_000,  # 1M 上下文
    "gemini-1.5-flash-8b": 1_000_000, # 1M 上下文
    
    # ============ xAI Grok 模型 (2026年4月最新) ============
    "grok-4.1-fast": 2_000_000,     # 2M 上下文 - 最大
    "grok-4.1": 256_000,            # 256K 上下文
    "grok-4.1-mini": 128_000,       # 128K 上下文
    "grok-4-fast": 2_000_000,       # 2M 上下文
    "grok-4": 256_000,              # 256K 上下文
    "grok-3": 1_000_000,            # 1M 上下文
    "grok-3-mini": 128_000,         # 128K 上下文
    "grok-2": 128_000,              # 已弃用
    "grok": 128_000,
    
    # ============ DeepSeek 模型 (2026年4月最新) ============
    # DeepSeek V4 系列 (最新)
    "deepseek-v4-pro": 1_000_000,   # 1M 上下文
    "deepseek-v4-flash": 1_000_000, # 1M 上下文
    
    # DeepSeek V3 系列
    "deepseek-v3": 128_000,         # 128K 上下文
    "deepseek-v3.1": 128_000,       # 128K 上下文
    "deepseek-v3.2": 160_000,       # 160K 上下文 (部分托管)
    "DeepSeek-V3": 128_000,
    
    # DeepSeek R1 系列 (推理模型)
    "deepseek-r1": 128_000,         # 128K 上下文
    "deepseek-r1-0528": 128_000,    # 128K 上下文
    "DeepSeek-R1": 128_000,
    
    # ============ Mistral 模型 (2026年4月最新) ============
    # Mistral Large 3 系列 (最新)
    "mistral-large-3": 256_000,     # 256K 上下文
    "mistral-large-2512": 256_000,  # 256K 上下文
    
    # Mistral Large 2 系列
    "mistral-large-2": 128_000,     # 128K 上下文
    "mistral-large-2411": 128_000,  # 128K 上下文
    "mistral-large-2407": 128_000,  # 128K 上下文 (已退役)
    
    # Pixtral 系列 (多模态)
    "pixtral-large": 128_000,       # 128K 上下文
    "pixtral-12": 128_000,          # 128K 上下文
    
    # Codestral 系列 (代码)
    "codestral": 256_000,           # 256K 上下文
    
    # Mistral Small/Medium
    "mistral-small": 32_768,        # 32K 上下文
    "mistral-medium": 32_768,       # 32K 上下文
    "mistral-large": 128_000,       # 128K 上下文 (旧版别名)
    
    # ============ Meta Llama 模型 (2026年4月最新) ============
    # Llama 4 系列 (最新)
    "llama-4-scout": 10_485_760,    # 10M 上下文 - 最大！
    "llama-4-maverick": 1_048_576,  # 1M 上下文
    
    # Llama 3.3 系列
    "llama-3.3-70b": 128_000,       # 128K 上下文
    "llama-3.3": 128_000,
    
    # Llama 3.2 系列
    "llama-3.2-90b": 128_000,       # 128K 上下文 (视觉)
    "llama-3.2-11b": 128_000,       # 128K 上下文 (视觉)
    "llama-3.2-3b": 128_000,        # 128K 上下文
    "llama-3.2-1b": 128_000,        # 128K 上下文
    "llama-3.2": 128_000,
    
    # Llama 3.1 系列
    "llama-3.1-405b": 128_000,      # 128K 上下文
    "llama-3.1-70b": 128_000,       # 128K 上下文
    "llama-3.1-8b": 128_000,        # 128K 上下文
    "llama-3.1": 128_000,
    
    # Llama 3 (原始)
    "llama-3": 8_192,               # 8K 上下文
    
    # ============ 通义千问 Qwen 模型 (2026年最新) ============
    "qwen2.5-1m": 1_000_000,        # 100万上下文
    "qwen2.5-max": 128_000,
    "qwen2.5-turbo": 128_000,
    "qwen2.5-coder": 128_000,
    "qwen2.5": 128_000,
    "qwen-max": 128_000,
    "qwen-plus": 128_000,
    "qwen-turbo": 128_000,
    "qwen": 128_000,
    
    # ============ 小米 MiMo 模型 (2026年最新) ============
    "mimo-v2-pro": 1_000_000,       # 100万上下文
    "mimo-v2-flash": 128_000,
    "mimo-v2-omni": 128_000,
    "mimo-v2": 128_000,
    "xiaomi-mimo": 128_000,
    
    # ============ Kimi Moonshot 模型 ============
    "kimi-k2": 200_000,
    "kimi-k2.5": 200_000,
    "kimi-k1.5": 128_000,
    "kimi": 200_000,
    "moonshot": 200_000,
    
    # ============ 火山引擎豆包模型 ============
    "doubao-pro": 128_000,
    "doubao-lite": 128_000,
    "doubao-2.0": 128_000,
    "doubao": 128_000,
    
    # ============ 其他模型 ============
    # 默认
    "default": 128_000,
}

# 压缩触发阈值（上下文限制的百分比）
COMPACTION_THRESHOLD_RATIO = 0.50


def get_encoder(model_name: str = "gpt-4"):
    """Legacy accessor. Prefer token_engine.get_counter for provider awareness."""
    counter = token_engine.get_counter(None, model_name, None)
    return getattr(counter, "_encoder", None)


def estimate_tokens_text(
    text: str,
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> int:
    """Token count for plain text, using the most precise counter available."""
    return token_engine.count_text(text, provider_type, model_name, base_url, api_key).tokens


def estimate_tokens_messages(
    messages: List[Dict],
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> int:
    """Token count for a message list, including chat template overhead."""
    return token_engine.count_messages(messages, provider_type, model_name, base_url, api_key).tokens


async def estimate_tokens_messages_async(
    messages: List[Dict],
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> int:
    result = await token_engine.acount_messages(messages, provider_type, model_name, base_url, api_key)
    return result.tokens


def count_full_context(
    system_prompt: str,
    messages: List[Dict],
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict:
    """System prompt plus history breakdown, for the token status endpoint."""
    breakdown = token_engine.count_full_context(
        system_prompt, messages, provider_type, model_name, base_url, api_key
    )
    return breakdown.as_dict()


async def acount_full_context(
    system_prompt: str,
    messages: List[Dict],
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict:
    breakdown = await token_engine.acount_full_context(
        system_prompt, messages, provider_type, model_name, base_url, api_key
    )
    return breakdown.as_dict()


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Trim text so the counter reports at most max_tokens."""
    return token_engine.truncate_to_tokens(text, max_tokens, provider_type, model_name, base_url, api_key)


async def atruncate_to_tokens(
    text: str,
    max_tokens: int,
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Non-blocking variant used inside request handlers."""
    return await token_engine.atruncate_to_tokens(
        text, max_tokens, provider_type, model_name, base_url, api_key
    )


def describe_counter(
    model_name: str = "gpt-4",
    provider_type: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict:
    """Which counter backs this configuration, and whether it is exact."""
    return token_engine.describe(provider_type, model_name, base_url)



def get_context_window(model_name: str) -> int:
    """获取模型的上下文窗口大小"""
    model_lower = model_name.lower()
    
    # 尝试精确匹配
    if model_name in DEFAULT_CONTEXT_WINDOWS:
        return DEFAULT_CONTEXT_WINDOWS[model_name]
    
    # 尝试模糊匹配（部分匹配）
    for key, value in DEFAULT_CONTEXT_WINDOWS.items():
        if key in model_lower:
            return value
    
    return DEFAULT_CONTEXT_WINDOWS["default"]


def get_compaction_threshold(model_name: str) -> int:
    """
    获取压缩触发阈值（token 数）
    返回上下文限制的 50%
    """
    context_window = get_context_window(model_name)
    threshold = int(context_window * COMPACTION_THRESHOLD_RATIO)
    return threshold


def should_compact(token_count: int, model_name: str) -> bool:
    """判断是否需要压缩"""
    threshold = get_compaction_threshold(model_name)
    return token_count >= threshold


def get_token_status(
    token_count: int,
    model_name: str = "gpt-4",
    system_tokens: int = 0,
    history_tokens: Optional[int] = None,
    exact: Optional[bool] = None,
    method: str = "",
    context_window_override: Optional[int] = None,
) -> Dict:
    """
    Build the context-usage status for the UI.

    Percentage is measured against the model context window; the compaction
    threshold (50% of the window) is where the UI starts recommending a
    compaction. Bands are ordered so that every branch is reachable.

    `context_window_override` comes from the provider settings. Providers offer
    the same model with different windows depending on plan, so a value the user
    filled in always wins over the built-in table.
    """
    if context_window_override and context_window_override > 0:
        context_window = int(context_window_override)
        threshold = int(context_window * COMPACTION_THRESHOLD_RATIO)
    else:
        context_window = get_context_window(model_name) or 1
        threshold = get_compaction_threshold(model_name)
    # One decimal: a branch at 0.4% used to render as "0%" and looked broken.
    percentage = max(0.0, round(min(100.0, (token_count / context_window) * 100), 1))
    threshold_percent = int(COMPACTION_THRESHOLD_RATIO * 100)

    if percentage < threshold_percent:
        status = "normal"
    elif percentage < 80:
        status = "warning"
    else:
        status = "danger"

    return {
        "current": token_count,
        "threshold": threshold,
        "limit": context_window,
        "percentage": percentage,
        "should_compact": token_count >= threshold,
        "status": status,
        "system_tokens": system_tokens,
        "history_tokens": token_count if history_tokens is None else history_tokens,
        "exact": exact,
        "method": method,
    }

