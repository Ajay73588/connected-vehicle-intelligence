#!/usr/bin/env python
"""Entry point: python services/simulator/simulator.py --vehicles 100000 --events-per-second 100000 --duration 300"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cvi.simulator.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
