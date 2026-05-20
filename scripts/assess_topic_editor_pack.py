#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.features.summarising.topic_editor_agentic import assess_topic_editor_pack


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess a topic-editor replay evidence pack.")
    parser.add_argument("pack", help="Evidence pack directory")
    args = parser.parse_args()
    report = assess_topic_editor_pack(Path(args.pack))
    print(f"assessment={report['status']} pack={Path(args.pack).resolve()}")


if __name__ == "__main__":
    main()
