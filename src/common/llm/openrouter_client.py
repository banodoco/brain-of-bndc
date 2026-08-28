"""OpenRouter client — DeepSeek wire format pointed at openrouter.ai.

OpenRouter speaks the OpenAI-compatible protocol, so this reuses
DeepSeekClient's message/tool/response conversion unchanged. Only the
endpoint, key, reasoning routing, and provider pinning differ.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from .deepseek_client import DeepSeekClient


class OpenRouterClient(DeepSeekClient):
    """DeepSeek wire-format client routed through OpenRouter.

    Inherits generate_chat_completion unchanged — OpenRouter speaks the
    same OpenAI-compatible protocol; only key and base URL differ.

    Differences from the direct DeepSeek endpoint, applied via the
    ``_apply_reasoning`` hook:

    - DeepSeek-direct's ``extra_body.thinking`` is NOT portable; it is
      translated to OpenRouter's unified ``reasoning`` param instead.
    - Requests are pinned to the official DeepSeek provider (slug
      ``deepseek``) with fallbacks disabled, so the model is always served
      by DeepSeek's own endpoint rather than a mirrored host.
    """

    PROVIDER_SLUG = "deepseek"

    def __init__(self) -> None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY not found in environment")
        base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def _apply_reasoning(
        self,
        params: Dict[str, Any],
        reasoning_effort: Any,
        thinking_enabled: bool,
    ) -> None:
        """Translate DeepSeek-direct reasoning controls to OpenRouter params.

        ``thinking_enabled`` (DEEPSEEK_THINKING_ENABLED / per-request kwarg)
        maps to ``reasoning.enabled``; ``reasoning_effort`` stays a top-level
        param (OpenRouter accepts the OpenAI-style name). Provider pinning
        rides along in the same body.
        """
        reasoning: Dict[str, Any] = {"enabled": bool(thinking_enabled)}
        params["extra_body"] = {
            "reasoning": reasoning,
            "provider": {"order": [self.PROVIDER_SLUG], "allow_fallbacks": False},
        }
        if reasoning_effort:
            params["reasoning_effort"] = reasoning_effort
