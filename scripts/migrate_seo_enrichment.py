"""Idempotently add the SEO enrichment columns to an existing database."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

from tv_app.app import app
from tv_app.models import db


COLUMNS = {
    "availability_updated_at": "TIMESTAMP",
    "tagline": "TEXT",
    "runtime_minutes": "INTEGER",
    "number_of_seasons": "INTEGER",
    "release_status": "VARCHAR(50)",
    "original_language": "VARCHAR(12)",
    "cast_data": "JSON",
    "official_trailer_key": "VARCHAR(64)",
    "official_trailer_name": "VARCHAR(255)",
    "official_trailer_published_at": "VARCHAR(40)",
    "metadata_status": "VARCHAR(20)",
    "metadata_updated_at": "TIMESTAMP",
}


with app.app_context():
    existing = {column["name"] for column in inspect(db.engine).get_columns("tv_shows")}
    with db.engine.begin() as connection:
        for name, definition in COLUMNS.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE tv_shows ADD COLUMN {name} {definition}"))
                print(f"added tv_shows.{name}")
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tv_shows_metadata_status "
                "ON tv_shows (metadata_status)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tv_shows_metadata_updated_at "
                "ON tv_shows (metadata_updated_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tv_shows_availability_updated_at "
                "ON tv_shows (availability_updated_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_show_genres_genre_tvshow "
                "ON show_genres (genre_id, tvshow_id)"
            )
        )
        connection.execute(
            text(
                "UPDATE tv_shows SET availability_updated_at = "
                "COALESCE(updated_at, created_at, CURRENT_TIMESTAMP) "
                "WHERE availability_updated_at IS NULL"
            )
        )
    print("SEO enrichment schema ready")
