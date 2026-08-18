"""
Handles interactions with the OpenAI API (v1.x.x).

Subclasses DeepSeekClient to reuse its OpenAI-compatible message/tool
converters (`_to_openai_messages`, `_to_openai_tools`,
`_to_openai_tool_choice`, `_to_anthropic_like_response`) so the admin-agent
tool loop can run against any OpenAI-hosted model (e.g. GPT Sol) unchanged.
"""
import os
import logging
from typing import List, Dict, Any, Union

# Need AsyncOpenAI for async calls
from openai import AsyncOpenAI

from .deepseek_client import DeepSeekClient

logger = logging.getLogger(__name__)

class OpenAIClient(DeepSeekClient):
    """Handles interactions with the OpenAI API (v1.x.x syntax).

    Text-only calls return the assistant text (BaseLLMClient compatibility);
    tool calls return the Anthropic-like block shape the AdminChatAgent loop
    expects, exactly like DeepSeekClient.
    """
    def __init__(self):
        """Initializes the OpenAI client using v1.x.x syntax."""
        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            logger.warning("OPENAI_API_KEY environment variable not set. OpenAIClient will not function.")
            # Set client to None or raise error if key is strictly needed at init
            self.client = None 
        else:
            # Use the new client initialization
            try:
                base_url = os.environ.get("OPENAI_BASE_URL") or None
                self.client = (
                    AsyncOpenAI(api_key=self.api_key, base_url=base_url)
                    if base_url
                    else AsyncOpenAI(api_key=self.api_key)
                )
                logger.info("Initializing OpenAI Client (API Key presence: Found)")
            except Exception as e:
                 logger.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
                 self.client = None # Ensure client is None on init failure
                 raise # Re-raise the error

    async def generate_chat_completion(self, model: str, system_prompt: str, 
                                         messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]],
                                         **kwargs: Any) -> Any:
        """Generates a chat completion using the OpenAI API v1.x.x asynchronously.

        When ``tools`` are supplied, converts to OpenAI tool format and returns
        an Anthropic-like response (``content`` blocks) so the AdminChatAgent
        tool loop works unchanged. Without tools, returns the assistant text
        for BaseLLMClient compatibility.
        """
        
        # Check if client initialized properly
        if self.client is None:
            error_msg = "OpenAIClient cannot generate completion: Client not initialized (check API key or init error)."
            logger.error(error_msg)
            raise ValueError(error_msg)

        tools = kwargs.get("tools")
        # Reuse DeepSeekClient's OpenAI-compatible converter — it preserves the
        # assistant tool_calls history and tool-result roles a multi-iteration
        # tool loop depends on.
        formatted_messages = self._to_openai_messages(system_prompt, messages)

        # Filter allowed additional parameters for the new API structure
        allowed_params = [
            "response_format", "temperature", "max_tokens", "max_completion_tokens", # Include both token params
            "top_p", "frequency_penalty", "presence_penalty", "seed",
            "reasoning_effort", "store" # Keep potentially non-standard ones for flexibility
            ]
        params = {"model": model, "messages": formatted_messages}
        if tools:
            params["tools"] = self._to_openai_tools(tools)
            tool_choice = self._to_openai_tool_choice(kwargs.get("tool_choice"))
            if tool_choice is not None:
                params["tool_choice"] = tool_choice
        
        # Populate params, handling potential max_tokens variations later
        for key in allowed_params:
            if key in kwargs and kwargs[key] is not None:
                 # Temporarily store both possible token keys if provided
                 params[key] = kwargs[key]

        # --- Model-Specific Parameter Adjustment --- 
        # o-series AND gpt-5-class models (e.g. a GPT Sol variant) require
        # max_completion_tokens instead of max_tokens.
        is_o_model = model.startswith("o") or "gpt-5" in model
        
        if is_o_model:
            # Expect max_completion_tokens for 'o' models
            if "max_tokens" in params and "max_completion_tokens" not in params:
                 logger.debug(f"Model '{model}' expects 'max_completion_tokens', translating from 'max_tokens'.")
                 params["max_completion_tokens"] = params.pop("max_tokens")
            elif "max_tokens" in params and "max_completion_tokens" in params:
                 logger.warning(f"Both 'max_tokens' and 'max_completion_tokens' provided for model '{model}'. Using 'max_completion_tokens'.")
                 params.pop("max_tokens") # Prioritize the expected one
        else:
            # Expect max_tokens for standard models
            if "max_completion_tokens" in params and "max_tokens" not in params:
                 logger.debug(f"Model '{model}' expects 'max_tokens', translating from 'max_completion_tokens'.")
                 params["max_tokens"] = params.pop("max_completion_tokens")
            elif "max_tokens" in params and "max_completion_tokens" in params:
                 logger.warning(f"Both 'max_tokens' and 'max_completion_tokens' provided for model '{model}'. Using 'max_tokens'.")
                 params.pop("max_completion_tokens") # Prioritize the expected one

        # Remove non-standard params if they cause issues, or keep if 'o3' needs them
        # Example: Remove if not 'o' model
        # if not is_o_model:
        #     params.pop("reasoning_effort", None)
        #     params.pop("store", None)
        # --- End Parameter Adjustment ---

        # Log call details (use adjusted params)
        # Define loggable params *after* adjustments
        final_allowed_params = [p for p in allowed_params if p in params]
        loggable_params = {k: params[k] for k in final_allowed_params}
        is_multimodal = any(isinstance(m.get('content'), list) for m in formatted_messages)
        logger.info(f"Making OpenAI call: model={model}, multimodal={is_multimodal}, additional_params={loggable_params}")

        try:
            # Use the new API call structure
            response = await self.client.chat.completions.create(**params)

            if tools:
                # Same Anthropic-like shape DeepSeekClient returns — the
                # AdminChatAgent loop consumes this unchanged.
                return self._to_anthropic_like_response(response)

            # Access response differently
            if response.choices and response.choices[0].message and response.choices[0].message.content:
                 generated_text = response.choices[0].message.content.strip()
                 return generated_text
            else:
                 logger.error(f"OpenAI API response missing expected content structure: {response}")
                 raise RuntimeError("OpenAI API response format unexpected.")
                 
        except Exception as e:
            # Catch specific OpenAI errors if needed, e.g., openai.APIError
            logger.error(f"Error during OpenAI API call: {e}", exc_info=True)
            raise # Re-raise the original error or a custom one 