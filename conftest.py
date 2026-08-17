"""Pytest configuration — make the repository importable as ``src.*``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
