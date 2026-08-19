"""Shared model defaults for the live-update editors.

Home for the model fallback that both the topic editor and the daily digest
consume. The legacy ``LIVE_UPDATE_EDITOR_MODEL`` env name is retained so
existing configs keep working; editors override it with their own env var
(``TOPIC_EDITOR_MODEL``, ``DAILY_DIGEST_MODEL``).
"""

import os

DEFAULT_LIVE_UPDATE_MODEL = os.getenv("LIVE_UPDATE_EDITOR_MODEL", "claude-opus-4-6")
