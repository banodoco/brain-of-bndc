import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module(relative_path: str, module_name: str):
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_anthropic_like_response_includes_stop_reason():
    _load_module("src/common/llm/base_client.py", "src.common.llm.base_client")
    deepseek_module = _load_module(
        "src/common/llm/deepseek_client.py",
        "src.common.llm.deepseek_client_under_test",
    )

    client = deepseek_module.DeepSeekClient.__new__(deepseek_module.DeepSeekClient)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content="hello",
                    tool_calls=None,
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
    )

    normalized = client._to_anthropic_like_response(response)

    assert normalized.stop_reason == "stop"
    assert normalized.usage.input_tokens == 3
    assert normalized.usage.output_tokens == 5
    assert any(block.type == "text" and block.text == "hello" for block in normalized.content)


def _normalized_usage(module, usage):
    client = module.DeepSeekClient.__new__(module.DeepSeekClient)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(role="assistant", content="hi", tool_calls=None),
            )
        ],
        usage=usage,
    )
    return client._to_anthropic_like_response(response).usage


def test_anthropic_like_response_carries_deepseek_cache_hit_miss_fields():
    _load_module("src/common/llm/base_client.py", "src.common.llm.base_client")
    deepseek_module = _load_module(
        "src/common/llm/deepseek_client.py",
        "src.common.llm.deepseek_client_under_test",
    )

    normalized = _normalized_usage(
        deepseek_module,
        SimpleNamespace(
            prompt_tokens=5814,
            completion_tokens=8,
            prompt_cache_hit_tokens=5760,
            prompt_cache_miss_tokens=54,
        ),
    )

    assert normalized.input_tokens == 5814
    assert normalized.output_tokens == 8
    assert normalized.cache_hit_input_tokens == 5760
    assert normalized.cache_miss_input_tokens == 54


def test_anthropic_like_response_falls_back_to_nested_cached_tokens():
    _load_module("src/common/llm/base_client.py", "src.common.llm.base_client")
    deepseek_module = _load_module(
        "src/common/llm/deepseek_client.py",
        "src.common.llm.deepseek_client_under_test",
    )

    normalized = _normalized_usage(
        deepseek_module,
        SimpleNamespace(
            prompt_tokens=5814,
            completion_tokens=8,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5760),
        ),
    )

    assert normalized.input_tokens == 5814
    assert normalized.cache_hit_input_tokens == 5760
    assert normalized.cache_miss_input_tokens == 5814 - 5760


def test_anthropic_like_response_handles_dict_usage_with_no_cache_fields():
    _load_module("src/common/llm/base_client.py", "src.common.llm.base_client")
    deepseek_module = _load_module(
        "src/common/llm/deepseek_client.py",
        "src.common.llm.deepseek_client_under_test",
    )

    normalized = _normalized_usage(
        deepseek_module,
        {"prompt_tokens": 100, "completion_tokens": 7},
    )

    assert normalized.input_tokens == 100
    assert normalized.output_tokens == 7
    # No cache info reported -> (0, 0) at the client layer. Downstream accounting
    # (TopicEditor._extract_usage) treats an absent cache split as all-miss.
    assert normalized.cache_hit_input_tokens == 0
    assert normalized.cache_miss_input_tokens == 0
