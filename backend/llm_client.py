import re
import json
from pathlib import Path
from typing import List, Dict, AsyncGenerator, Optional

import httpx

from .paths import LLM_CONFIG_PATH

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
) -> AsyncGenerator[Dict, None]:
    """
    完全独立的LLM流式调用。
    从 brain._call_external_llm_stream 复制并简化。

    top_k / min_p / repeat_penalty 只在 llama-cpp 下下发：这三个是 llama.cpp 服务端
    扩展字段，云端 provider 收到可能直接 400。不传则维持服务端默认（repeat_penalty=1.0
    等于不惩罚，这是长回复退化成复读的直接原因）。

    frequency_penalty / presence_penalty 走另一条路：它们是 OpenAI 兼容协议的字段，
    云端 provider 认，llama.cpp 也有自己的 repeat_penalty 覆盖同类需求，所以只发给
    _PENALTY_PROVIDERS。前者按 token 出现次数线性惩罚，压得住反复出现的短语；后者按
    是否出现过惩罚，逼模型换词换话题。两者都不传时不改变服务端默认行为。

    Yields:
        {"type": "content", "content": str}
        {"type": "thinking", "content": str}  # 仅当 enable_thinking=True
        {"type": "error", "message": str}     # 出错时
    """
    if provider_type not in SUPPORTED_PROVIDERS:
        yield {"type": "error", "message": f"Unsupported provider_type: {provider_type}. Supported: {SUPPORTED_PROVIDERS}"}
        return

    # 构建URL（确保/chat/completions端点）
    url = base_url.strip().rstrip('/')
    if not url.endswith('/chat/completions'):
        url += '/chat/completions'
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
        "stream_options": {"include_usage": True},
        # 【强制禁用思考模式】companion 聊天不支持思考模式
        "thinking": {"type": "disabled"},
    }
    # 最大输出长度（可选）：未设置时不下发，使用服务端默认
    if isinstance(max_tokens, int) and max_tokens > 0:
        payload["max_tokens"] = max_tokens
    # llama-cpp: 默认关闭思考模式
    if provider_type == "llama-cpp":
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        # llama.cpp 服务端扩展采样参数。不传时沿用服务端默认（repeat_penalty=1.0 无惩罚）。
        if isinstance(top_k, int) and top_k > 0:
            payload["top_k"] = top_k
        if isinstance(min_p, (int, float)) and 0 <= float(min_p) <= 1:
            payload["min_p"] = float(min_p)
        if isinstance(repeat_penalty, (int, float)) and float(repeat_penalty) > 0:
            payload["repeat_penalty"] = float(repeat_penalty)
    elif provider_type in _PENALTY_PROVIDERS:
        # OpenAI-compatible penalties. 0 is a meaningful value ("penalise
        # nothing"), so the check is on the type rather than on truthiness;
        # None means "leave the field out and keep the server default".
        if isinstance(frequency_penalty, (int, float)):
            payload["frequency_penalty"] = float(frequency_penalty)
        if isinstance(presence_penalty, (int, float)):
            payload["presence_penalty"] = float(presence_penalty)
    
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
        async with httpx.AsyncClient(timeout=300.0, trust_env=_trust_env) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
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
                            
                            # With include_usage set, the provider sends the real
                            # token accounting on a final chunk whose choices are
                            # empty. Passing it through lets the UI show measured
                            # usage instead of a local estimate.
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
                                    
                                    # DeepSeek Reasoner: reasoning_content
                                    delta_reasoning = delta.get("reasoning_content") or ""
                                    if enable_thinking and delta_reasoning:
                                        yield {"type": "thinking", "content": delta_reasoning}
                                    
                                    if content:
                                        # 清理思考标签（防止LLM在content中也输出）
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
    except Exception as e:
        yield {"type": "error", "message": f"LLM call failed: {str(e)}"}


def load_default_llm_config() -> Optional[Dict]:
    """
    加载用户在应用里选中的 provider 配置。

    真正的文件在 `paths.LLM_CONFIG_PATH`（界面写入的那个）。裸相对名只作为旧安装的
    兜底，且排在它后面：项目根也有一份同名样例，先读它会静默地用一份用户没选过的
    配置去生成摘要。
    """
    config: Dict = {}
    config_path = None
    for candidate in (
        Path(LLM_CONFIG_PATH),
        Path("llm_providers.json"),
        Path("./llm_providers.json"),
    ):
        try:
            with candidate.open("r", encoding="utf-8") as f:
                config = json.load(f)
                config_path = str(candidate)
                break
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    
    if not config_path:
        return None
    
    providers = config.get("providers", [])
    active_idx = config.get("activeProviderIndex", 0)
    
    if providers and isinstance(providers, list) and active_idx < len(providers):
        p = providers[active_idx]
        if isinstance(p, dict):
            return {
                "provider_type": p.get("provider", "openai"),
                "api_key": p.get("apiKey", ""),
                "base_url": p.get("baseUrl", ""),
                "model": (p.get("models", ["gpt-4"]) or ["gpt-4"])[0] if p.get("models") else "gpt-4",
            }
    
    return None
