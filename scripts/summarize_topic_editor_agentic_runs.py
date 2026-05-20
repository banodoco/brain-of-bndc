#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.features.summarising.topic_editor_agentic import summarize_topic_editor_agentic_runs


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize assessed topic-editor replay packs.")
    parser.add_argument("root", help="Run root containing evidence packs")
    args = parser.parse_args()
    result = summarize_topic_editor_agentic_runs(Path(args.root))
    print(f"packs={result['pack_count']} root={Path(args.root).resolve()}")


if __name__ == "__main__":
    main()
