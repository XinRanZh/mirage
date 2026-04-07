"""Unified LLM interface for MIRAGE.

Uses litellm as a universal gateway — supports 100+ LLM providers
(OpenAI, Anthropic, AWS Bedrock, Azure, Google, Ollama, etc.)
via a single interface. No provider-specific config files needed.

Configuration priority:
  1. Explicit arguments (model, api_key, api_base)
  2. Environment variables (MIRAGE_MODEL, MIRAGE_API_KEY, MIRAGE_API_BASE)
  3. Provider-specific env vars (OPENAI_API_KEY, ANTHROPIC_API_KEY, AWS_*, etc.)
"""

from __future__ import annotations

import os
from typing import List, Optional

import litellm

# Suppress litellm's verbose logging by default
litellm.suppress_debug_info = True


def completion(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 2048,
    timeout: int = 120,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
) -> str:
    """Call an LLM via litellm's universal interface.

    Args:
        messages: Chat messages in OpenAI format.
        model: Model identifier (e.g., "claude-sonnet-4-5-20241022",
               "gpt-4o", "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0").
               Falls back to MIRAGE_MODEL env var.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens to generate.
        timeout: Request timeout in seconds.
        api_key: API key. Falls back to MIRAGE_API_KEY or provider-specific env vars.
        api_base: Custom API base URL. Falls back to MIRAGE_API_BASE env var.

    Returns:
        The model's response text.
    """
    model = model or os.environ.get("MIRAGE_MODEL", "claude-sonnet-4-5-20241022")
    api_key = api_key or os.environ.get("MIRAGE_API_KEY")
    api_base = api_base or os.environ.get("MIRAGE_API_BASE")

    kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout": timeout,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base

    resp = litellm.completion(**kwargs)
    return resp.choices[0].message.content
