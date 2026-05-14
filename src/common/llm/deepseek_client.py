"""OpenAI-compatible client for DeepSeek chat/tool calls."""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Union

from openai import AsyncOpenAI

from .base_client import BaseLLMClient

logger = logging.getLogger(__name__)


class DeepSeekClient(BaseLLMClient):
    """DeepSeek client using the OpenAI-compatible API surface."""

    def __init__(self) -> None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not found in environment")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def generate_chat_completion(
        self,
        model: str,
        system_prompt: str,
        messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]],
        **kwargs: Any,
    ) -> Any:
        """Generate a DeepSeek response.

        When tools are supplied, returns an Anthropic-like response object with
        ``content`` blocks so TopicEditor can reuse its existing tool loop.
        Without tools, returns the assistant text for BaseLLMClient compatibility.
        """
        tools = kwargs.get("tools")
        openai_messages = self._to_openai_messages(system_prompt, messages)
        params: Dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "stream": False,
        }
        if tools:
            params["tools"] = self._to_openai_tools(tools)

        max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        for name in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed"):
            if kwargs.get(name) is not None:
                params[name] = kwargs[name]

        reasoning_effort = kwargs.get("reasoning_effort") or os.getenv("DEEPSEEK_REASONING_EFFORT")
        if reasoning_effort:
            params["reasoning_effort"] = reasoning_effort
        if _env_flag("DEEPSEEK_THINKING_ENABLED", True):
            params["extra_body"] = {"thinking": {"type": "enabled"}}

        response = await self.client.chat.completions.create(**params)
        if tools:
            return self._to_anthropic_like_response(response)
        message = response.choices[0].message if response.choices else None
        return (getattr(message, "content", None) or "").strip()

    def _to_openai_messages(
        self,
        system_prompt: str,
        messages: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = [{"role": "system", "content": str(system_prompt or "")}]
        for message in messages or []:
            role = message.get("role")
            content = message.get("content")
            if role == "assistant" and isinstance(content, list):
                raw_assistant = self._raw_openai_assistant_message(content)
                if raw_assistant is not None:
                    out.append(raw_assistant)
                    continue

                text_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                reasoning_content: str | None = None
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        text_parts.append(str(block.get("text")))
                    elif block.get("type") == "reasoning_content" and block.get("reasoning_content"):
                        reasoning_content = str(block.get("reasoning_content"))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": str(block.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name") or ""),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
                row: Dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n\n".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    row["tool_calls"] = tool_calls
                if reasoning_content:
                    row["reasoning_content"] = reasoning_content
                out.append(row)
            elif role == "user" and isinstance(content, list):
                text_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool_result":
                        out.append({
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": str(block.get("content") or ""),
                        })
                if text_parts:
                    out.append({"role": "user", "content": "\n\n".join(text_parts)})
            else:
                out.append({"role": role or "user", "content": _content_to_text(content)})
        return out

    def _to_openai_tools(self, tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for tool in tools or []:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description") or "",
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return converted

    def _to_anthropic_like_response(self, response: Any) -> Any:
        message = response.choices[0].message if response.choices else None
        content_blocks: List[Any] = []
        raw_message = _message_to_dict(message)
        if raw_message:
            content_blocks.append(SimpleNamespace(
                type="openai_assistant_message",
                message=raw_message,
            ))
        reasoning_content = _get_message_extra(message, "reasoning_content") if message is not None else None
        if reasoning_content:
            content_blocks.append(SimpleNamespace(
                type="reasoning_content",
                reasoning_content=str(reasoning_content),
            ))
        text = getattr(message, "content", None) if message is not None else None
        if text:
            content_blocks.append(SimpleNamespace(type="text", text=text))
        for tool_call in getattr(message, "tool_calls", None) or []:
            function = getattr(tool_call, "function", None)
            raw_args = getattr(function, "arguments", "") or "{}"
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed_args = {}
            content_blocks.append(SimpleNamespace(
                type="tool_use",
                id=getattr(tool_call, "id", None),
                name=getattr(function, "name", None),
                input=parsed_args,
            ))

        usage = getattr(response, "usage", None)
        normalized_usage = SimpleNamespace(
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )
        return SimpleNamespace(content=content_blocks, usage=normalized_usage, raw_response=response)

    @staticmethod
    def _raw_openai_assistant_message(content: Sequence[Dict[str, Any]]) -> Dict[str, Any] | None:
        for block in content:
            if isinstance(block, dict) and block.get("type") == "openai_assistant_message":
                raw = block.get("message")
                if isinstance(raw, dict):
                    row = dict(raw)
                    row["role"] = "assistant"
                    return row
        return None


def _get_message_extra(message: Any, name: str) -> Any:
    value = getattr(message, name, None)
    if value is not None:
        return value
    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        return extra.get(name)
    if isinstance(message, dict):
        return message.get(name)
    return None


def _message_to_dict(message: Any) -> Dict[str, Any]:
    if message is None:
        return {}
    if hasattr(message, "model_dump"):
        data = message.model_dump(exclude_none=True)
    elif isinstance(message, dict):
        data = {key: value for key, value in message.items() if value is not None}
    else:
        data = {
            "role": getattr(message, "role", "assistant"),
            "content": getattr(message, "content", None),
            "tool_calls": getattr(message, "tool_calls", None),
        }
        data = {key: value for key, value in data.items() if value is not None}
    data["role"] = "assistant"
    return data


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or block))
            else:
                parts.append(str(block))
        return "\n\n".join(parts)
    return str(content or "")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
