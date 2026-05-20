#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.features.summarising.topic_editor_agentic import replay_topic_editor_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a frozen topic-editor agentic scenario.")
    parser.add_argument("scenario", help="Path to tests/agentic/scenarios/<name>.yaml")
    parser.add_argument("--out", required=True, help="Evidence-pack output directory")
    parser.add_argument("--actor", choices=["mock", "deepseek"], default="mock", help="Actor to drive the replay")
    parser.add_argument("--mock-actor", action="store_true", default=None, help="Deprecated alias for --actor mock")
    parser.add_argument("--mock-mode", choices=["submit", "watch"], default="submit")
    parser.add_argument("--model", default="mock-topic-editor")
    args = parser.parse_args()
    actor = "mock" if args.mock_actor else args.actor
    summary = asyncio.run(
        replay_topic_editor_scenario(
            Path(args.scenario),
            Path(args.out),
            actor_kind=actor,
            mock_actor=args.mock_actor,
            mock_mode=args.mock_mode,
            model=args.model,
        )
    )
    print(f"wrote evidence pack: {Path(args.out).resolve()}")
    print(f"status={summary.get('status')} scenario={summary.get('scenario')} draft_status={summary.get('draft_status')}")


if __name__ == "__main__":
    main()
