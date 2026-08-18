"""OpenAIClient tests: the tools path returns the Anthropic-like block shape
the AdminChatAgent tool loop consumes (same contract as DeepSeekClient)."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module(relative_path: str, module_name: str):
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# The tests conftest stubs src.common.llm as a namespace module, so the real
# clients cannot be imported through the package. Load them directly (same
# approach as test_deepseek_client.py) so the relative imports resolve.
_load_module("src/common/llm/base_client.py", "src.common.llm.base_client")
_stub_deepseek = sys.modules.get("src.common.llm.deepseek_client")
_load_module("src/common/llm/deepseek_client.py", "src.common.llm.deepseek_client")
_openai_module = _load_module(
    "src/common/llm/openai_client.py", "src.common.llm.openai_client"
)
OpenAIClient = _openai_module.OpenAIClient
# Restore the conftest stub so other test modules keep seeing the fake.
if _stub_deepseek is not None:
    sys.modules["src.common.llm.deepseek_client"] = _stub_deepseek


def _make_fake_openai_response(*, content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=message)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4),
    )


def _make_client(monkeypatch=None, *, response=None):
    """OpenAIClient with a fake AsyncOpenAI transport (no real API key)."""
    client = OpenAIClient.__new__(OpenAIClient)
    transport = MagicMock()
    transport.chat.completions.create = AsyncMock(return_value=response)
    client.client = transport
    return client, transport


def test_openai_client_text_only_returns_string():
    """No tools → plain assistant text (BaseLLMClient compatibility)."""
    client, transport = _make_client(
        response=_make_fake_openai_response(content="hello from openai"),
    )

    async def run():
        return await client.generate_chat_completion(
            model="gpt-5.6-sol",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096,
        )

    result = asyncio.run(run())
    assert result == "hello from openai"


def test_openai_client_tools_path_returns_anthropic_like_blocks():
    """Tools supplied → Anthropic-like response with tool_use blocks, exactly
    the shape the admin-agent loop expects (type/name/input/id)."""
    tool_call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(
            name="log_live_update_feedback",
            arguments='{"topic_id": "t-1", "feedback_text": "fix"}',
        ),
    )
    client, transport = _make_client(
        response=_make_fake_openai_response(
            content=None,
            tool_calls=[tool_call],
            finish_reason="tool_calls",
        ),
    )

    async def run():
        return await client.generate_chat_completion(
            model="gpt-5.6-sol",
            system_prompt="sys",
            messages=[{"role": "user", "content": "fix the update"}],
            max_tokens=4096,
            tools=[
                {
                    "name": "log_live_update_feedback",
                    "description": "Log feedback",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )

    result = asyncio.run(run())
    blocks = result.content
    tool_blocks = [b for b in blocks if b.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].name == "log_live_update_feedback"
    assert tool_blocks[0].id == "call_1"
    assert tool_blocks[0].input == {"topic_id": "t-1", "feedback_text": "fix"}
    assert result.stop_reason == "tool_calls"
    # The OpenAI-format tools were actually sent.
    sent_params = transport.chat.completions.create.call_args.kwargs
    assert sent_params["tools"][0]["type"] == "function"
    assert sent_params["tools"][0]["function"]["name"] == "log_live_update_feedback"


def test_openai_client_gpt5_model_uses_max_completion_tokens():
    """gpt-5-class models (like a GPT Sol variant) get max_completion_tokens,
    not max_tokens."""
    client, transport = _make_client(
        response=_make_fake_openai_response(content="ok"),
    )

    async def run():
        return await client.generate_chat_completion(
            model="gpt-5.6-sol",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096,
        )

    asyncio.run(run())
    sent = transport.chat.completions.create.call_args.kwargs
    assert sent["model"] == "gpt-5.6-sol"
    assert "max_completion_tokens" in sent
    assert sent["max_completion_tokens"] == 4096
    assert "max_tokens" not in sent
