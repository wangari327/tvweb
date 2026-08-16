"""TMDB detail enrichment shared by imports and the catalogue backfill."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from sqlalchemy.exc import IntegrityError

from .models import Genre, TVShow, db


TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"
TMDB_PROFILE_BASE_URL = "https://image.tmdb.org/t/p/w185"
YOUTUBE_KEY = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def _first_positive(values: Iterable[Any]) -> Optional[int]:
    for value in values or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _official_trailer(videos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candidates = []
    for video in (videos or {}).get("results", []):
        key = video.get("key") or ""
        if (
            video.get("site") == "YouTube"
            and video.get("type") == "Trailer"
            and video.get("official") is True
            and YOUTUBE_KEY.fullmatch(key)
        ):
            candidates.append(video)
    if not candidates:
        return None
    candidates.sort(
        key=lambda video: (
            video.get("iso_639_1") == "en",
            video.get("published_at") or "",
            int(video.get("size") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _cast_members(details: Dict[str, Any], category: str, limit: int = 10):
    credits_key = "credits" if category == "movie" else "aggregate_credits"
    members = []
    seen = set()
    for member in (details.get(credits_key) or {}).get("cast", []):
        person_id = member.get("id")
        name = (member.get("name") or "").strip()
        if not name or person_id in seen:
            continue
        seen.add(person_id)
        if category == "movie":
            character = (member.get("character") or "").strip()
        else:
            roles = sorted(
                member.get("roles") or [],
                key=lambda role: int(role.get("episode_count") or 0),
                reverse=True,
            )
            character = next(
                ((role.get("character") or "").strip() for role in roles if role.get("character")),
                "",
            )
        profile_path = member.get("profile_path")
        members.append(
            {
                "id": person_id,
                "name": name,
                "character": character,
                "profile_url": f"{TMDB_PROFILE_BASE_URL}{profile_path}" if profile_path else None,
            }
        )
        if len(members) >= limit:
            break
    return members


def extract_enrichment(details: Dict[str, Any], category: str) -> Dict[str, Any]:
    """Normalize a TMDB movie or TV response into catalogue fields."""
    trailer = _official_trailer(details.get("videos") or {})
    date_value = details.get("release_date") if category == "movie" else details.get("first_air_date")
    title = details.get("title") if category == "movie" else details.get("name")
    runtime = details.get("runtime") if category == "movie" else _first_positive(details.get("episode_run_time") or [])
    return {
        "show_name": title,
        "overview": details.get("overview"),
        "poster_path": (
            f"{TMDB_POSTER_BASE_URL}{details.get('poster_path')}"
            if details.get("poster_path")
            else None
        ),
        "vote_average": details.get("vote_average"),
        "rating": details.get("vote_average"),
        "year": int(date_value[:4]) if date_value and date_value[:4].isdigit() else None,
        "genres": [
            genre.get("name", "").strip()
            for genre in details.get("genres") or []
            if genre.get("name", "").strip()
        ],
        "tagline": (details.get("tagline") or "").strip() or None,
        "runtime_minutes": runtime,
        "number_of_seasons": details.get("number_of_seasons") if category != "movie" else None,
        "release_status": (details.get("status") or "").strip() or None,
        "original_language": (details.get("original_language") or "").strip() or None,
        "cast_data": _cast_members(details, category),
        "official_trailer_key": trailer.get("key") if trailer else None,
        "official_trailer_name": trailer.get("name") if trailer else None,
        "official_trailer_published_at": trailer.get("published_at") if trailer else None,
    }


async def fetch_tmdb_details(
    session,
    tmdb_id: int,
    category: str,
    token: str,
    retries: int = 3,
) -> Optional[Dict[str, Any]]:
    """Fetch top-level details, credits and videos in one TMDB request."""
    namespace = "movie" if category == "movie" else "tv"
    append = "credits,videos" if category == "movie" else "aggregate_credits,videos"
    url = f"{TMDB_BASE_URL}/{namespace}/{tmdb_id}"
    params = {
        "language": "en-US",
        "append_to_response": append,
        "include_video_language": "en,null",
    }
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, headers=headers, timeout=20) as response:
                if response.status == 200:
                    return await response.json()
                if response.status == 404:
                    return None
                if response.status == 429 or response.status >= 500:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return None
        except Exception:
            if attempt + 1 >= retries:
                return None
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


def _genre_records(names):
    records = []
    for name in names:
        genre = Genre.query.filter_by(name=name).first()
        if genre is None:
            genre = Genre(name=name)
            try:
                with db.session.begin_nested():
                    db.session.add(genre)
                    db.session.flush()
            except IntegrityError:
                genre = Genre.query.filter_by(name=name).first()
        if genre is not None:
            records.append(genre)
    return records


def apply_enrichment(show: TVShow, details: Dict[str, Any]) -> None:
    """Apply normalized metadata to a show inside the caller's transaction."""
    data = extract_enrichment(details, show.category)
    for field in (
        "show_name",
        "overview",
        "poster_path",
        "vote_average",
        "rating",
        "year",
    ):
        value = data.get(field)
        if value not in (None, ""):
            setattr(show, field, value)
    for field in (
        "tagline",
        "runtime_minutes",
        "number_of_seasons",
        "release_status",
        "original_language",
        "cast_data",
        "official_trailer_key",
        "official_trailer_name",
        "official_trailer_published_at",
    ):
        setattr(show, field, data.get(field))
    show.genres = _genre_records(data.get("genres") or [])
    show.metadata_status = "enriched"
    show.metadata_updated_at = datetime.utcnow()
