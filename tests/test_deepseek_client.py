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
