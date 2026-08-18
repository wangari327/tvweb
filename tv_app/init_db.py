"""Backward-compatible entry point for creating a new database schema.

Use ``python scripts/initialize_database.py`` for new deployments. This file
remains so existing operational notes do not invoke the obsolete hand-written
schema from older releases.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.initialize_database import main


if __name__ == "__main__":
    main()
