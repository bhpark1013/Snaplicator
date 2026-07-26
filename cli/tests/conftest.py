import sys
from pathlib import Path

# Make `snaplicator_init` importable when pytest runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
