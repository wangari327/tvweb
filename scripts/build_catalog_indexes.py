"""Build the public catalogue indexes without locking readers or writers.

Run this as a one-off maintenance process, not from a web request:
    python scripts/build_catalog_indexes.py
"""
from sqlalchemy import text

from tv_app.app import app, db


INDEXES = (
    (
        "ix_tv_shows_category_created",
        "CREATE INDEX CONCURRENTLY ix_tv_shows_category_created "
        "ON public.tv_shows (category, created_at DESC)",
    ),
    (
        "ix_tv_shows_category_clicks_availability",
        "CREATE INDEX CONCURRENTLY ix_tv_shows_category_clicks_availability "
        "ON public.tv_shows (category, clicks DESC, availability_updated_at DESC)",
    ),
)


def _index_state(connection, name):
    return connection.execute(
        text(
            """
            SELECT i.indisvalid
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE n.nspname = 'public' AND c.relname = :name
            """
        ),
        {"name": name},
    ).scalar()


def main():
    with app.app_context():
        for name, create_statement in INDEXES:
            with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text("SET statement_timeout TO 0"))
                connection.execute(text("SET lock_timeout TO '10s'"))
                if _index_state(connection, name) is False:
                    print(f"Dropping incomplete index: {name}", flush=True)
                    connection.execute(text(f"DROP INDEX CONCURRENTLY {name}"))
                if _index_state(connection, name) is not True:
                    print(f"Creating index: {name}", flush=True)
                    connection.execute(text(create_statement))
                if _index_state(connection, name) is not True:
                    raise RuntimeError(f"Index is not valid: {name}")
                print(f"Ready: {name}", flush=True)


if __name__ == "__main__":
    main()
