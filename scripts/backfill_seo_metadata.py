"""Backfill cached TMDB genres, cast, trailer and detail metadata."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp

from tv_app.app import app
from tv_app.models import TVShow, db
from tv_app.tmdb_enrichment import apply_enrichment, fetch_tmdb_details


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", choices=("tv", "anime", "movie", "all"), default="all")
    parser.add_argument("--limit", type=int, default=250, help="0 processes every pending row")
    parser.add_argument("--batch-size", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=6)
    return parser.parse_args()


def tokens():
    configured = [
        value.strip()
        for value in os.environ.get("TMDB_BACKFILL_TOKENS", "").split(",")
        if value.strip()
    ]
    fallback = os.environ.get("TMDB_BEARER_TOKEN", "").strip()
    if fallback and fallback not in configured:
        configured.append(fallback)
    return configured


async def fetch_batch(session, rows, token_cycle, concurrency):
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(row):
        async with semaphore:
            details = await fetch_tmdb_details(
                session,
                row.tmdb_id,
                row.category,
                next(token_cycle),
            )
            return row.id, details

    return await asyncio.gather(*(fetch(row) for row in rows))


async def run(args):
    configured_tokens = tokens()
    if not configured_tokens:
        raise SystemExit("No TMDB bearer token is configured")
    token_cycle = itertools.cycle(configured_tokens)
    categories = ("tv", "anime", "movie") if args.category == "all" else (args.category,)
    processed = 0
    enriched = 0
    unavailable = 0

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=max(args.concurrency * 2, 12))
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for category in categories:
            while args.limit == 0 or processed < args.limit:
                remaining = args.batch_size if args.limit == 0 else min(args.batch_size, args.limit - processed)
                if remaining <= 0:
                    break
                rows = (
                    TVShow.query.filter(
                        TVShow.category == category,
                        TVShow.tmdb_id.isnot(None),
                        TVShow.metadata_updated_at.is_(None),
                    )
                    .order_by(
                        TVShow.clicks.desc(),
                        TVShow.availability_updated_at.desc().nullslast(),
                        TVShow.id.asc(),
                    )
                    .limit(remaining)
                    .all()
                )
                if not rows:
                    break

                results = await fetch_batch(session, rows, token_cycle, args.concurrency)
                for show_id, details in results:
                    show = db.session.get(TVShow, show_id)
                    if show is None:
                        continue
                    if details:
                        apply_enrichment(show, details)
                        enriched += 1
                    else:
                        show.metadata_status = "unavailable"
                        show.metadata_updated_at = datetime.utcnow()
                        unavailable += 1
                    processed += 1
                db.session.commit()
                print(
                    f"processed={processed} enriched={enriched} unavailable={unavailable} "
                    f"category={category}",
                    flush=True,
                )

    print(
        f"complete processed={processed} enriched={enriched} unavailable={unavailable}",
        flush=True,
    )


if __name__ == "__main__":
    arguments = parse_args()
    with app.app_context():
        asyncio.run(run(arguments))
