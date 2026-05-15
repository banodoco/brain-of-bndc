import discord
import asyncio
import random
import logging
import time
import traceback

class RateLimiter:
    """Manages rate limiting for Discord API calls with exponential backoff."""

    def __init__(self):
        self.backoff_times = {}  # Store backoff times per channel (used as global cooldown hint)
        self.base_delay = 1.0    # Base delay in seconds
        self.max_delay = 64.0    # Maximum delay in seconds
        self.jitter = 0.1        # Random jitter factor
        self.logger = logging.getLogger('DiscordBot')

    async def execute(self, key, coroutine_or_factory):
        """
        Executes a coroutine or coroutine factory with rate limit handling.

        Uses local backoff per call to prevent accumulated delays from cascading
        across calls. The instance-level backoff_times dict is only used as a
        short-lived cooldown hint (decays after 60s).

        Args:
            key: Identifier for the rate limit (e.g., channel_id)
            coroutine_or_factory: The coroutine or factory function to execute

        Returns:
            The result of the coroutine execution

        Raises:
            discord.HTTPException: If all retries are exhausted for a 429, or on
                non-retryable HTTP errors after max retries
        """
        max_retries = 5
        attempt = 0

        # Use local backoff for this call — don't inherit accumulated state.
        # Only apply a short initial delay if there's a recent global cooldown hint.
        local_backoff = self.base_delay
        if key in self.backoff_times:
            stored_backoff, stored_time = self.backoff_times[key]
            age = time.monotonic() - stored_time
            if age < 60:
                # Apply a fraction of the stored backoff as a courtesy delay
                local_backoff = min(stored_backoff * 0.5, self.base_delay * 4)
            else:
                # Stale hint — discard it
                del self.backoff_times[key]

        while attempt < max_retries:
            try:
                # Add jitter to prevent thundering herd (skip on first attempt unless cooldown)
                if attempt > 0 or (key in self.backoff_times):
                    jitter = random.uniform(-self.jitter, self.jitter)
                    sleep_time = local_backoff * (1 + jitter)
                    self.logger.debug(f"[RateLimiter] Key={key} attempt {attempt+1}/{max_retries}: sleeping {sleep_time:.1f}s")
                    await asyncio.sleep(sleep_time)

                # Ensure coroutine_or_factory is a callable factory
                if not callable(coroutine_or_factory):
                    self.logger.error("RateLimiter.execute expects a callable coroutine factory.")
                    raise TypeError("coroutine_or_factory must be a callable that returns a coroutine")

                # Get a new coroutine object from the factory for each attempt
                current_coro = coroutine_or_factory()
                if not asyncio.iscoroutine(current_coro):
                    self.logger.error("Coroutine factory did not return a coroutine.")
                    raise TypeError("coroutine_or_factory must return a coroutine")

                result = await current_coro

                # Reset global cooldown hint on success
                if key in self.backoff_times:
                    del self.backoff_times[key]
                return result

            except discord.HTTPException as e:
                attempt += 1

                if e.status == 429:  # Rate limit hit
                    retry_after = e.retry_after if hasattr(e, 'retry_after') else None

                    if retry_after:
                        self.logger.warning(f"[RateLimiter] 429 for key={key} (attempt {attempt}/{max_retries}). Discord says retry after {retry_after}s")
                        await asyncio.sleep(retry_after)
                    else:
                        # Calculate exponential backoff locally
                        local_backoff = min(local_backoff * 2, self.max_delay)
                        self.logger.warning(f"[RateLimiter] 429 for key={key} (attempt {attempt}/{max_retries}). Backoff: {local_backoff:.1f}s")
                        jitter = random.uniform(-self.jitter, self.jitter)
                        await asyncio.sleep(local_backoff * (1 + jitter))

                    if attempt >= max_retries:
                        # Store cooldown hint for subsequent calls, then RAISE
                        self.backoff_times[key] = (local_backoff, time.monotonic())
                        self.logger.error(f"[RateLimiter] 429 exhausted {max_retries} retries for key={key}. Raising.")
                        raise

                elif attempt >= max_retries:
                    self.logger.error(f"[RateLimiter] Failed after {max_retries} attempts for key={key}: {e}")
                    raise
                else:
                    # Non-429 HTTP error — exponential backoff locally
                    local_backoff = min(local_backoff * 2, self.max_delay)
                    self.logger.warning(f"[RateLimiter] HTTP {e.status} for key={key} (attempt {attempt}/{max_retries}): {e}. Backoff: {local_backoff:.1f}s")
                    await asyncio.sleep(local_backoff)

            except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
                attempt += 1
                if attempt >= max_retries:
                    self.logger.error(f"[RateLimiter] Network error exhausted {max_retries} retries for key={key}: {e}")
                    raise
                else:
                    # Use exponential backoff locally for network errors
                    local_backoff = min(local_backoff * 2, self.max_delay)
                    self.logger.warning(f"[RateLimiter] Network error for key={key} (attempt {attempt}/{max_retries}): {e}. Backoff: {local_backoff:.1f}s")
                    await asyncio.sleep(local_backoff)

            except Exception as e:
                self.logger.error(f"[RateLimiter] Unexpected error for key={key}: {e}")
                self.logger.debug(traceback.format_exc())
                raise
