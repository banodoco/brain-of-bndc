import importlib.util
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_llm_stub() -> None:
    if "src.common.llm" in sys.modules:
        return

    llm_module = types.ModuleType("src.common.llm")
    llm_module.__path__ = []

    async def get_llm_response(*_args, **_kwargs):
        return "Generated description"

    class ClaudeClient:
        async def generate_chat_completion(self, **_kwargs):
            return "yes|approved"

    class DeepSeekClient:
        async def generate_chat_completion(self, **_kwargs):
            return "yes|approved"

    llm_module.get_llm_response = get_llm_response
    llm_module.ClaudeClient = ClaudeClient
    llm_module.DeepSeekClient = DeepSeekClient
    sys.modules["src.common.llm"] = llm_module

    claude_client_module = types.ModuleType("src.common.llm.claude_client")
    claude_client_module.ClaudeClient = ClaudeClient
    sys.modules["src.common.llm.claude_client"] = claude_client_module

    deepseek_client_module = types.ModuleType("src.common.llm.deepseek_client")
    deepseek_client_module.DeepSeekClient = DeepSeekClient
    sys.modules["src.common.llm.deepseek_client"] = deepseek_client_module


def _install_tweepy_stub() -> None:
    if "tweepy" in sys.modules:
        return

    tweepy_module = types.ModuleType("tweepy")

    class OAuthHandler:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def set_access_token(self, *args, **kwargs):
            self.access_args = args
            self.access_kwargs = kwargs

    class API:
        def __init__(self, auth):
            self.auth = auth

        def verify_credentials(self):
            return types.SimpleNamespace(id=1, screen_name="stub-user")

    class Client:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def retweet(self, user_id, tweet_id):
            return {"user_id": user_id, "tweet_id": tweet_id}

    tweepy_module.OAuthHandler = OAuthHandler
    tweepy_module.API = API
    tweepy_module.Client = Client
    sys.modules["tweepy"] = tweepy_module


_install_llm_stub()
_install_tweepy_stub()


def load_module_from_repo(relative_path: str, module_name: str):
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def clear_payment_policy_env(monkeypatch):
    for key in (
        "ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD",
        "ADMIN_PAYMENT_SUCCESS_DM_PROVIDERS",
        "ADMIN_PAYOUT_PER_PAYMENT_USD_CAP",
        "ADMIN_PAYOUT_DAILY_USD_CAP",
    ):
        monkeypatch.delenv(key, raising=False)
