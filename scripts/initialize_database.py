"""Create the current iBOX TV schema in a brand-new database.

This command is safe to run repeatedly: SQLAlchemy only creates objects that
do not already exist. It is not a replacement for a targeted data migration
when an existing production schema changes.
"""

import sys
from pathlib import Path

from sqlalchemy import text

# Running ``python scripts/initialize_database.py`` makes ``scripts`` the
# import root; add the repository root so the application package resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tv_app.app import app
from tv_app.models import db


def main() -> None:
    with app.app_context():
        if db.engine.dialect.name == "postgresql":
            # The title-search index uses gin_trgm_ops. Creating the extension
            # before create_all keeps a clean PostgreSQL install reproducible.
            with db.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

        db.create_all()

    print("Database schema is ready.")


if __name__ == "__main__":
    main()
