"""
Central dispatcher for LLM client interactions.
"""
import logging
from typing import List, Dict, Any, Type, Union

# Import client classes directly. 
# If underlying libraries (anthropic, openai) are missing,
# this will raise the original ImportError from the client file.
from .claude_client import ClaudeClient
from .openai_client import OpenAIClient
from .deepseek_client import DeepSeekClient
# Import BaseLLMClient from its own file for type hinting if needed
from .base_client import BaseLLMClient
from .gemini_client import GeminiClient  # Import the new client

logger = logging.getLogger(__name__)

# Populate supported clients (assuming imports succeeded)
SUPPORTED_CLIENTS: Dict[str, Type[BaseLLMClient]] = {
    "claude": ClaudeClient,
    "openai": OpenAIClient,
    "deepseek": DeepSeekClient,
    "gemini": GeminiClient,
}

__all__ = [
    "BaseLLMClient",
    "OpenAIClient",
    "ClaudeClient",
    "DeepSeekClient",
    "GeminiClient",  # Add the new client to __all__
]

async def get_llm_response(client_name: str, model: str, system_prompt: str, 
                             # Use updated typing
                             messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]], 
                             **kwargs: Any) -> str:
    """
    Gets a response from the specified LLM provider and model asynchronously.

    Args:
        client_name: The name of the client (e.g., 'claude', 'openai'). Case-insensitive.
        model: The specific model name (e.g., 'claude-sonnet-4-5-20250929', 'gpt-4o').
        system_prompt: The system prompt for the LLM.
        messages: A list of message dictionaries. Supports text-only (content: str) 
                  and multimodal (content: List[Dict]).
        **kwargs: Additional provider-specific parameters (e.g., temperature, max_tokens).

    Returns:
        The LLM's response content as a string.

    Raises:
        ValueError: If the client_name is not supported.
        ImportError: If a required client library (e.g., anthropic, openai) is missing.
        Exception: Can re-raise exceptions from the underlying client's API call.
    """
    client_key = client_name.lower()
    client_class = SUPPORTED_CLIENTS.get(client_key)

    # Simpler check now - if client name is bad, it won't be in the dict
    if not client_class:
        raise ValueError(f"Unsupported LLM client: '{client_name}'. Supported: {list(SUPPORTED_CLIENTS.keys())}")

    try:
        # Instantiate the client (API keys handled within client __init__)
        client_instance = client_class()
    except Exception as e:
        logger.error(f"Failed to initialize LLM client '{client_name}': {e}", exc_info=True)
        # Re-raise initialization error clearly
        raise RuntimeError(f"Failed to initialize client '{client_name}'") from e

    try:
        # Call the standardized method - now awaited
        logger.info(f"Making LLM call to {client_name} with model {model}")
        response = await client_instance.generate_chat_completion(
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            **kwargs
        )
        logger.info(f"LLM call to {client_name} completed successfully")
        # Ensure response is string
        if not isinstance(response, str):
             logger.warning(f"LLM client '{client_name}' returned a non-string response type: {type(response)}. Attempting conversion.")
             try:
                 response = str(response)
             except Exception as convert_e:
                 logger.error(f"Failed to convert LLM response to string: {convert_e}", exc_info=True)
                 raise TypeError(f"LLM client '{client_name}' failed to provide a string-convertible response.") from convert_e
        return response
    except Exception as e:
        logger.error(f"Error during LLM call via client '{client_name}' with model '{model}': {e}", exc_info=True)
        # Re-raise the exception to be handled by the caller
        raise
